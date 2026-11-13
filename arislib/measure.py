"""Power readings at one frequency on several nodes at the same time.

Levels are reported "input-referred": the channel power in dBFS minus the receiver gain, so dongles running at
different gains can be compared. Each node's per-band offset (antenna / cable differences, from calibration) is
then added on top by the caller (see locate.py).

Local dongles are measured through the ARIS engine's dongle locks, so a reading waits for (or politely skips)
anything else that is using that dongle. Remote nodes are asked over HTTP (aris_node.py).
"""
from __future__ import annotations

import json
import os
import select
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import nodes as _nodes
from . import radioproc
from . import rfpower

MAX_DWELL_S = 15.0
OVERLOAD_CLIP = 0.002            # more than 0.2 % clipped samples: the receiver is overloaded, reading too low
R820T_GAINS = [0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7, 22.9, 25.4, 28.0, 29.7,
               32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48.0, 49.6]


class MeasureError(Exception):
    pass


def snap_gain(g: float) -> float:
    return min(R820T_GAINS, key=lambda x: abs(x - float(g)))


USB_SETTLE_S = 0.35          # time a dongle needs between being closed and opened again
RTL_SDR_BUF = 16 * 16384     # rtl_sdr's output buffer in bytes (its -b default)


def rtl_capture(serial: str, tune_hz: float, fs: float, n: int, gain_db: float, ppm: int = 0,
                timeout: float = 30.0) -> np.ndarray:
    """Raw uint8 IQ from rtl_sdr, straight into memory (no temporary file). The caller makes sure nothing else
    is using the dongle. Read as it streams: once the samples are in, rtl_sdr gets a moment to end by itself and is
    then stopped (radioproc: Ctrl+C, SIGKILL when that hangs), and it has exited before this returns. rtl_sdr never
    ends at a count that is a whole number of its 256 kB buffers, so it is asked for a few samples more; before,
    such a capture waited out the whole timeout and its samples were thrown away."""
    from . import radios
    if not radios.have_tool("rtl_sdr", serial):
        raise MeasureError(f"{radios.tool_for('rtl_sdr', serial) or 'IQ capture'} not found for this receiver")
    want = 2 * int(n)
    n_req = int(n) + 4096
    if (2 * n_req) % RTL_SDR_BUF == 0:
        n_req += 1
    cmd = ["rtl_sdr", "-d", str(serial), "-f", str(int(tune_hz)), "-s", str(int(fs)), "-g", str(gain_db)]
    if ppm:
        cmd += ["-p", str(int(ppm))]
    cmd += ["-n", str(n_req), "-"]
    from . import radios                      # other receivers get their own tool (arislib.radios)
    try:
        cmd = radios.translate(cmd)
    except radios.Unsupported as e:
        raise MeasureError(str(e))
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    err = []
    th = threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True)   # stderr never fills up
    th.start()
    buf, end, fd, late = bytearray(), time.time() + float(timeout), p.stdout.fileno(), False
    try:
        while len(buf) < want:
            if time.time() > end:
                late = True
                break
            r, _w, _x = select.select([fd], [], [], 0.25)
            if r:
                chunk = os.read(fd, min(1 << 20, want - len(buf)))
                if not chunk:
                    break                            # rtl_sdr ended
                buf += chunk
        if len(buf) >= want:                         # a moment for its own clean exit (the extra samples dropped)
            t_end = time.time() + 2.5
            while p.poll() is None and time.time() < t_end:
                r, _w, _x = select.select([fd], [], [], 0.25)
                if r and not os.read(fd, 1 << 20):
                    break
    finally:
        radioproc.stop(p)
        th.join(2)
        for f in (p.stdout, p.stderr):
            if f is p.stderr and th.is_alive():
                continue
            try:
                f.close()
            except (OSError, ValueError):
                pass
    if len(buf) < min(want, int(fs)):
        if late:
            raise MeasureError("rtl_sdr timed out (dongle hung?). Replug it.")
        msg = (err[0] if err and err[0] else b"").decode(errors="replace").strip()
        raise MeasureError(f"capture failed (rc={p.returncode}): {msg[-160:]}")
    return np.frombuffer(bytes(buf), dtype=np.uint8)


def measure_raw(serial, freq_mhz, bw_khz, dwell_s, gain_db, ppm=0) -> dict:
    """One reading on a dongle this process may use right now. Retries once at lower gain if overloaded."""
    fs, off = rfpower.plan_capture(bw_khz)
    n = int(fs * max(0.5, min(MAX_DWELL_S, float(dwell_s))))
    tune = float(freq_mhz) * 1e6 + off               # the signal sits `off` below the tuning point
    gain = snap_gain(gain_db)
    tried = []
    for attempt in range(2):
        if attempt:
            time.sleep(USB_SETTLE_S)                 # the dongle was just closed
        raw = rtl_capture(serial, tune, fs, n, gain, ppm, timeout=n / fs + 20)
        r = rfpower.channel_power_u8(raw, fs, -off, float(bw_khz) * 1e3)
        tried.append(gain)
        if r["clipped_fraction"] <= OVERLOAD_CLIP or gain <= 0.0:
            break
        gain = snap_gain(max(0.0, gain - 12.0))
    r["gain_db"] = gain
    r["overloaded"] = r["clipped_fraction"] > OVERLOAD_CLIP
    if len(tried) > 1:
        r["note"] = f"Overloaded at {tried[0]} dB gain; measured again at {gain} dB."
    for k in ("level_db", "peak_db", "mean_db", "floor_db"):
        r[k + "fs"] = r[k]
        r[k] = round(r[k] - gain, 2)                 # input-referred: comparable across gains
    r["carrier_mhz"] = round(float(freq_mhz) + r["carrier_offset_hz"] / 1e6, 6)
    return r


def _local(eng, node, freq_mhz, bw_khz, dwell_s, gain_db=None):
    serial = node["serial"]
    gain = eng._gain_for(serial, freq_mhz, freq_mhz) if gain_db is None else float(gain_db)
    try:
        ppm = int(eng._ppm_of(serial))
    except Exception:
        ppm = 0
    with eng._use_dongle(serial, timeout=15, holder="locate"):
        return measure_raw(serial, freq_mhz, bw_khz, dwell_s, gain, ppm)


def _remote(node, freq_mhz, bw_khz, dwell_s, gain_db=None):
    token = _nodes.token_for(node["id"])
    if not token:
        raise MeasureError("no token saved for this node; add it again")
    body = json.dumps({"freq_mhz": float(freq_mhz), "bw_khz": float(bw_khz), "dwell_s": float(dwell_s),
                       **({"gain_db": float(gain_db)} if gain_db is not None else {})}).encode()
    req = urllib.request.Request(node["url"] + "/measure", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-ARIS-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=float(dwell_s) + 25) as resp:
            out = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("error")
        except Exception:
            msg = None
        raise MeasureError(msg or f"node answered HTTP {e.code}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise MeasureError(f"cannot reach the node at {node['url']} ({e})")
    if out.get("error"):
        raise MeasureError(out["error"])
    return out


def remote_health(node) -> dict:
    token = _nodes.token_for(node["id"])
    req = urllib.request.Request(node["url"] + "/health", headers={"X-ARIS-Token": token or ""})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": "wrong token" if e.code == 401 else f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "error": f"cannot reach {node['url']} ({e})"}


def measure_nodes(eng, node_list, freq_mhz: float, bw_khz: float = 25.0, dwell_s: float = 5.0) -> dict:
    """{node_id: reading} for every node, all at the same time. A reading that failed has 'error'."""
    if not node_list:
        return {}

    def one(node):
        t0 = time.time()
        try:
            if node.get("kind") == "remote":
                r = _remote(node, freq_mhz, bw_khz, dwell_s)
            else:
                if eng is None:
                    raise MeasureError("the ARIS engine is not loaded")
                r = _local(eng, node, freq_mhz, bw_khz, dwell_s)
        except Exception as e:                     # RFError (busy dongle), MeasureError, anything
            return {"error": str(e)[:200], "t": t0}
        r["t"] = t0
        return r

    with ThreadPoolExecutor(max_workers=max(1, len(node_list))) as pool:
        futs = {n["id"]: pool.submit(one, n) for n in node_list}
        return {nid: f.result() for nid, f in futs.items()}
