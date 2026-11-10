"""Receivers ARIS can use, and how one command line becomes each receiver's own.

The engine builds every radio command the way the rtl-sdr tools take it (rtl_sdr, rtl_power, rtl_fm, rtl_433,
rtl_adsb, rtl_test, each with `-d <device>`), and everything that reads their output expects what those tools print:
8-bit unsigned IQ (CU8) from rtl_sdr, rtl_power's CSV, rtl_fm's 16-bit audio, rtl_433's JSON. A backend turns such a
command into the one its receiver understands, so the rest of ARIS never changes:

- `rtlsdr` (built in): RTL-SDR dongles. Commands run as they are. Device ids are the dongles' EEPROM serials.
- `soapy` (built in): any receiver with a SoapySDR module (HackRF, Airspy, SDRplay, LimeSDR, PlutoSDR, USRP,
  SoapyRemote ...), through rx_tools (rx_sdr, rx_power, rx_fm: the rtl-sdr tools' command lines on SoapySDR) and
  rtl_433's own SoapySDR support. Device ids look like `soapy-<driver>-<serial>`.
- `HelperBackend`: a receiver that only offers IQ and sweeps through a helper program which speaks the rtl_sdr /
  rtl_power command lines (e.g. the Signal Hound BB60 plug-in, plugins/signalhound_bb60.py). Live rtl_433 and
  narrow-band audio then run through `iqpipe` (the helper's IQ piped into rtl_433, or demodulated in Python).
- Plug-ins register more backends with `register_backend`.

A backend's device ids start with `<prefix>-`; ids without a known prefix are RTL-SDR serials. An id is a file
name, a lock name and an argv word, so it is letters, digits, '.', '_' and '-' only (`ID_OK`).

Receive-only: no backend may run anything that transmits. Standard library only, no ARIS imports.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

ID_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
RTL_TOOLS = ("rtl_sdr", "rtl_power", "rtl_fm", "rtl_433", "rtl_adsb", "rtl_test")
IQPIPE = str(Path(__file__).with_name("iqpipe.py"))


class Unsupported(Exception):
    """This receiver cannot do what the command asks (e.g. ADS-B decoding on a receiver rtl_adsb cannot open)."""


def _sanitize(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", str(s or "")).strip("._-")
    return s[:48]


def _opt(cmd, flag):
    return cmd[cmd.index(flag) + 1] if flag in cmd and cmd.index(flag) + 1 < len(cmd) else None


def _without(cmd, flags_with_value=(), flags=()):
    """cmd minus the given options (and their values)."""
    out, skip = [], False
    for i, a in enumerate(cmd):
        if skip:
            skip = False
            continue
        if a in flags_with_value:
            skip = True
            continue
        if a in flags:
            continue
        out.append(a)
    return out


def _rate_hz(s) -> int:
    s = str(s).strip().lower()
    mult = 1
    if s.endswith("k"):
        mult, s = 1000, s[:-1]
    elif s.endswith("m"):
        mult, s = 1000000, s[:-1]
    return int(float(s) * mult)


class Backend:
    """One kind of receiver. Subclasses set the class attributes and override what differs."""
    name = ""                       # short name ("soapy")
    prefix = ""                     # device ids look like "<prefix>-..."; "" only for the RTL-SDR catch-all
    label = "receiver"              # what to call it in plain words
    freq_range_mhz = (24.0, 1766.0)
    gains = None                    # gain steps in dB, or None (the engine's RTL-SDR steps)
    tools = {}                      # rtl tool -> the program actually run (None: not available on this receiver)
    max_iq_rate = None              # the fastest IQ it streams, samples a second (None: no limit known)
    sweep_unit = "dB"               # what its rtl_power-style rows are in: "dBFS" (rtl_power's own arithmetic, which
                                    # the Spectrum tab refers to an 8-bit full-scale sine), "dBm" (calibrated), or
                                    # "dB" (the receiver's own uncalibrated scale: shown as it is)

    def available(self) -> bool:
        need = [t for t in self.tools.values() if t]
        return bool(need) and all(shutil.which(t) for t in need[:1])

    def owns(self, dev) -> bool:
        return bool(self.prefix) and str(dev).startswith(self.prefix + "-")

    def list_devices(self) -> list:
        """[{"serial": id, "name": plain words}] for the receivers of this kind plugged in now (opens none)."""
        return []

    def tool(self, rtl_name):
        return self.tools.get(rtl_name, rtl_name)

    def translate(self, cmd: list, dev: str):
        """cmd (an rtl_* command naming dev) -> this receiver's command; None when the step does not apply to
        this receiver (the RTL-SDR USB test); raises Unsupported when it cannot be done."""
        return list(cmd)


class RtlSdr(Backend):
    name, prefix, label = "rtlsdr", "", "RTL-SDR dongle"
    tools = {t: t for t in RTL_TOOLS}
    sweep_unit = "dBFS"

    def owns(self, dev) -> bool:
        return not any(b.owns(dev) for b in _BACKENDS if b is not self)

    def list_devices(self) -> list:
        """`rtl_test -d 99` lists the dongles without opening any (99 is no device index)."""
        if shutil.which("rtl_test") is None:
            return []
        try:
            p = subprocess.run(["rtl_test", "-d", "99"], capture_output=True, text=True, timeout=8)
            text = (p.stdout or "") + (p.stderr or "")
        except subprocess.TimeoutExpired as e:
            text = " ".join(x.decode(errors="ignore") if isinstance(x, bytes) else (x or "")
                            for x in (e.stdout, e.stderr))
        except OSError:
            return []
        out = []
        for i, n, sn in re.findall(r"^\s*(\d+):\s+(.+?),\s+SN:\s*(\S+)", text, re.M):
            out.append({"index": int(i), "name": n.strip()[:60], "serial": sn, "backend": self.name})
        return out

    def translate(self, cmd, dev):
        return list(cmd)


class Soapy(Backend):
    """SoapySDR receivers through rx_tools (github.com/rxseger/rx_tools) and rtl_433's SoapySDR input."""
    name, prefix, label = "soapy", "soapy", "SoapySDR receiver"
    freq_range_mhz = (1.0, 6000.0)
    gains = [float(g) for g in range(0, 61, 3)]
    tools = {"rtl_sdr": "rx_sdr", "rtl_power": "rx_power", "rtl_fm": "rx_fm", "rtl_433": "rtl_433",
             "rtl_adsb": None, "rtl_test": None}
    SKIP_DRIVERS = ("rtlsdr", "audio")          # RTL-SDR dongles are the rtlsdr backend's; sound cards are no SDR

    def available(self) -> bool:
        return bool(shutil.which("SoapySDRUtil") and shutil.which("rx_sdr"))

    def list_devices(self) -> list:
        if not shutil.which("SoapySDRUtil"):
            return []
        try:
            p = subprocess.run(["SoapySDRUtil", "--find"], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return []
        out, seen = [], set()
        for block in re.split(r"(?m)^Found device \d+\s*$", p.stdout or "")[1:]:
            kv = dict(re.findall(r"(?m)^\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$", block))
            drv = kv.get("driver", "")
            if not drv or drv in self.SKIP_DRIVERS:
                continue
            ser = kv.get("serial", "")
            tail = _sanitize(ser)[-16:] if ser else f"{len([x for x in out if x['driver'] == drv])}"
            dev = f"soapy-{_sanitize(drv)}-{tail}"
            if dev in seen:
                continue
            seen.add(dev)
            out.append({"serial": dev, "name": (kv.get("label") or drv)[:60], "backend": self.name,
                        "driver": drv, "args": f"driver={drv}" + (f",serial={ser}" if ser else "")})
        _remember_args({d["serial"]: d["args"] for d in out})
        return out

    def args_for(self, dev) -> str:
        cfg = configured().get(dev) or {}
        if cfg.get("args"):
            return str(cfg["args"])
        known = _ARGS.get(dev)
        if known:
            return known
        m = re.fullmatch(r"soapy-([A-Za-z0-9._]+)-(.+)", str(dev))
        if not m:
            raise Unsupported(f"{dev} is not a SoapySDR device id")
        drv, tail = m.groups()
        if tail.isdigit() and len(tail) < 3:           # no serial: the n-th device of that driver
            return f"driver={drv}"
        return f"driver={drv},serial={tail}"

    def translate(self, cmd, dev):
        tool = os.path.basename(cmd[0])
        args = self.args_for(dev)
        rest = _without(cmd[1:], flags_with_value=("-d",))
        if tool == "rtl_sdr":
            return ["rx_sdr", "-d", args, "-F", "CU8"] + rest
        if tool == "rtl_power":
            return ["rx_power", "-d", args] + rest
        if tool == "rtl_fm":
            return ["rx_fm", "-d", args] + rest
        if tool == "rtl_433":
            return ["rtl_433", "-d", args] + rest
        if tool == "rtl_test":
            return None
        raise Unsupported(f"{self._what(tool)} needs an RTL-SDR dongle; this is a {self.label}")

    @staticmethod
    def _what(tool):
        return {"rtl_adsb": "ADS-B decoding (rtl_adsb)"}.get(tool, tool)


class HelperBackend(Backend):
    """A receiver driven by a helper program that takes rtl_sdr's and rtl_power's command lines:
        <helper> list                      -> one device per line: "<serial>\\t<name>"
        <helper> sdr -d S -f HZ -s RATE -g GAIN [-n N] -   -> CU8 IQ on stdout
        <helper> power -d S -f LO:HI:STEP -i S [-1|-e Ns] [-g GAIN] -   -> rtl_power CSV on stdout
    Subclasses set name, prefix, label, freq_range_mhz, gains and helper_argv()."""
    tools = {"rtl_sdr": "helper", "rtl_power": "helper", "rtl_fm": "helper", "rtl_433": "rtl_433",
             "rtl_adsb": None, "rtl_test": None}
    FM_OVERSAMPLE = 10          # IQ for the Python demodulator: 10 x the audio rate (an exact decimation)

    def helper_argv(self) -> list:
        raise NotImplementedError

    def tool(self, rtl_name):
        t = self.tools.get(rtl_name, rtl_name)
        return sys.executable if t == "helper" else t

    def available(self) -> bool:
        try:
            p = subprocess.run(self.helper_argv() + ["list"], capture_output=True, text=True, timeout=10)
            return p.returncode == 0
        except (OSError, subprocess.TimeoutExpired, NotImplementedError):
            return False

    def list_devices(self) -> list:
        try:
            p = subprocess.run(self.helper_argv() + ["list"], capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired, NotImplementedError):
            return []
        out = []
        for line in (p.stdout or "").splitlines():
            parts = line.split("\t", 1)
            if not parts[0].strip():
                continue
            dev = f"{self.prefix}-{_sanitize(parts[0])}"
            out.append({"serial": dev, "name": (parts[1] if len(parts) > 1 else self.label)[:60],
                        "backend": self.name})
        return out

    def hw(self, dev) -> str:
        return str(dev)[len(self.prefix) + 1:]

    def _sdr(self, dev, freq, rate, gain, n=None, ppm=None) -> list:
        c = self.helper_argv() + ["sdr", "-d", self.hw(dev), "-f", str(int(float(freq))), "-s", str(int(rate))]
        if gain is not None:
            c += ["-g", str(gain)]
        if ppm:
            c += ["-p", str(ppm)]
        if n:
            c += ["-n", str(int(n))]
        return c + ["-"]

    def _rate_ok(self, rate, what: str) -> None:
        """A step that needs more IQ bandwidth than this receiver streams is refused in plain words (an upsampled
        stream would look like a quiet band, and a station or burst outside it would be reported as not heard)."""
        cap = self.max_iq_rate
        if cap and float(rate) > float(cap) * 1.001:
            raise Unsupported(f"{what} needs {float(rate) / 1e6:.3g} MS/s of IQ, and a {self.label} streams at most "
                              f"{float(cap) / 1e3:.0f} kS/s: use an RTL-SDR or another wideband receiver for it")

    def translate(self, cmd, dev):
        tool = os.path.basename(cmd[0])
        rest = _without(cmd[1:], flags_with_value=("-d",))
        if tool == "rtl_sdr":
            self._rate_ok(_rate_hz(_opt(cmd, "-s") or "2048000"), "This step")
            return self.helper_argv() + ["sdr", "-d", self.hw(dev)] + rest
        if tool == "rtl_power":
            return self.helper_argv() + ["power", "-d", self.hw(dev)] + rest
        if tool == "rtl_fm":
            mode = _opt(cmd, "-M") or "fm"
            sr = _opt(cmd, "-s") or "24000"
            out = cmd[-1] if not cmd[-1].startswith("-") or cmd[-1] == "-" else "-"
            fs = int(_rate_hz(sr)) * self.FM_OVERSAMPLE
            self._rate_ok(fs, "The audio decoders")
            src = self._sdr(dev, _opt(cmd, "-f") or 0, fs, _opt(cmd, "-g"), ppm=_opt(cmd, "-p"))
            return [sys.executable, IQPIPE, "fm", "--rate", str(fs), "--mode", mode, "--sr", str(_rate_hz(sr)),
                    "--out", out, "--"] + src
        if tool == "rtl_433":
            rate = _rate_hz(_opt(cmd, "-s") or "250k")
            if self.max_iq_rate and rate > self.max_iq_rate:
                rate = int(self.max_iq_rate)      # rtl_433 decodes at the receiver's own rate (it takes any -s)
            src = self._sdr(dev, _opt(cmd, "-f") or 433920000, rate, _opt(cmd, "-g"), ppm=_opt(cmd, "-p"))
            sink = ["rtl_433"] + _without(cmd[1:], flags_with_value=("-d", "-g", "-p", "-s")) + \
                   ["-s", str(rate), "-r", "cu8:-"]
            return [sys.executable, IQPIPE, "rtl433", "--"] + src + ["---"] + sink
        if tool == "rtl_test":
            return None
        raise Unsupported(f"{'ADS-B decoding (rtl_adsb)' if tool == 'rtl_adsb' else tool} needs an RTL-SDR "
                          f"dongle; this is a {self.label}")


# ----------------------------------------------------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------------------------------------------------
_RTL = RtlSdr()
_BACKENDS: list = [Soapy(), _RTL]          # the RTL-SDR catch-all stays last
_ARGS: dict = {}                           # soapy id -> device args, from the last listing
_LOCK = threading.Lock()


def _remember_args(m: dict) -> None:
    with _LOCK:
        _ARGS.update(m)


def register_backend(backend: Backend) -> Backend:
    """Add a receiver type (a plug-in calls this). A backend with the same name replaces the old one."""
    if not isinstance(backend, Backend) or not backend.name or not backend.prefix:
        raise ValueError("a backend needs a name and a device-id prefix")
    if not ID_OK.fullmatch(backend.prefix) or "-" in backend.prefix:
        raise ValueError("a backend prefix is letters, digits, '.' and '_' only")
    with _LOCK:
        for i, b in enumerate(_BACKENDS):
            if b.name == backend.name:
                _BACKENDS[i] = backend
                break
        else:
            _BACKENDS.insert(len(_BACKENDS) - 1, backend)
    return backend


def backends() -> list:
    return list(_BACKENDS)


def backend_for(dev) -> Backend:
    for b in _BACKENDS:
        if b is not _RTL and b.owns(dev):
            return b
    return _RTL


def is_rtl(dev) -> bool:
    return backend_for(dev) is _RTL


def _device_of(cmd):
    tool = os.path.basename(cmd[0]) if cmd else ""
    dev = _opt(cmd, "-d")
    if dev is None:
        return None
    return dev[1:] if tool == "rtl_433" and dev.startswith(":") else dev


RANGE_SLACK_MHZ = 1.0          # rtl_power pads its hops past the band asked for


def _mhz_of(s):
    """'433920000', '433.92M', '88M' -> MHz; None if it does not parse."""
    try:
        t = str(s).strip().lower()
        m = {"k": 1e-3, "m": 1.0, "g": 1e3}.get(t[-1:])
        return float(t[:-1]) * m if m else float(t) / 1e6
    except (TypeError, ValueError):
        return None


def covers(dev, lo_mhz, hi_mhz=None) -> bool:
    """Does this receiver tune from lo to hi MHz (one frequency when hi is None)?"""
    a, b = backend_for(dev).freq_range_mhz
    hi_mhz = lo_mhz if hi_mhz is None else hi_mhz
    return a - RANGE_SLACK_MHZ <= float(lo_mhz) and float(hi_mhz) <= b + RANGE_SLACK_MHZ


def _check_range(cmd, dev) -> None:
    """A frequency outside the receiver's range is refused in plain words before anything opens it (with receivers
    of different ranges in one station, a 2.4 GHz sweep must never be sent to an RTL-SDR dongle)."""
    f = _opt(cmd, "-f")
    if not f:
        return
    parts = [_mhz_of(x) for x in str(f).split(":")[:2]]
    if any(p is None for p in parts):
        return
    lo, hi = min(parts), max(parts)
    if not covers(dev, lo, hi):
        a, b = backend_for(dev).freq_range_mhz
        what = f"{lo:g}-{hi:g} MHz is" if hi - lo > 1e-6 else f"{lo:g} MHz is"
        raise Unsupported(f"{what} outside what this {backend_for(dev).label} tunes ({a:g}-{b:g} MHz)")


def translate(cmd: list):
    """The command to run for an rtl_* command line. Commands that name no device (rtl_433 -r on a file) and
    RTL-SDR dongles run unchanged. None: this step does not apply to that receiver. Raises Unsupported."""
    if not cmd or os.path.basename(cmd[0]) not in RTL_TOOLS:
        return list(cmd)
    dev = _device_of(cmd)
    if dev is None:
        return list(cmd)
    b = backend_for(dev)
    _check_range(cmd, dev)
    return list(cmd) if b is _RTL else b.translate(list(cmd), dev)


def tool_for(rtl_name: str, dev) -> str | None:
    """The program that will run for rtl_name on this device (for 'is it installed?' checks); None: unsupported."""
    return backend_for(dev).tool(rtl_name)


def have_tool(rtl_name: str, dev) -> bool:
    t = tool_for(rtl_name, dev)
    return bool(t) and shutil.which(t) is not None


def list_devices(include_rtl: bool = True) -> list:
    """Every receiver plugged in now, of every kind, without opening any. Ids that fail ID_OK are dropped."""
    out = []
    for b in _BACKENDS:
        if b is _RTL and not include_rtl:
            continue
        try:
            found = b.list_devices()
        except Exception:                            # a broken helper must never stop the others being listed
            found = []
        for d in found:
            if ID_OK.fullmatch(str(d.get("serial", ""))):
                d.setdefault("backend", b.name)
                out.append(d)
    for dev, c in configured().items():              # receivers named in the config (e.g. a SoapyRemote server)
        if ID_OK.fullmatch(dev) and not any(d["serial"] == dev for d in out) and c.get("listed", True):
            out.append({"serial": dev, "name": str(c.get("name") or dev)[:60], "backend": backend_for(dev).name,
                        "configured": True})
    return out


def label_for(dev) -> str:
    return backend_for(dev).label


def gains_for(dev):
    return backend_for(dev).gains


def sweep_unit(dev) -> str:
    """The scale of this receiver's sweep rows: "dBFS" (RTL-SDR: the display converts), "dBm" or "dB"."""
    u = getattr(backend_for(dev), "sweep_unit", "dB")
    return u if u in ("dBFS", "dBm", "dB") else "dB"


def freq_range_mhz(devs=None) -> tuple:
    """The widest range any of these receivers covers (all registered kinds when devs is None)."""
    bs = {backend_for(d) for d in devs} if devs else set(_BACKENDS)
    lo = min(b.freq_range_mhz[0] for b in bs) if bs else _RTL.freq_range_mhz[0]
    hi = max(b.freq_range_mhz[1] for b in bs) if bs else _RTL.freq_range_mhz[1]
    return lo, hi


_CFG = {"key": None, "radios": {}}


def configured() -> dict:
    """Receivers named in the config's "radios" section: {id: {"args": "driver=remote,remote=...", "name": ...}}.
    Read from $ARIS_DATA_DIR/config.json, cached by modification time."""
    path = Path(os.environ.get("ARIS_DATA_DIR") or (Path.home() / "aris_data")) / "config.json"
    try:
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return {}
    if _CFG["key"] != key:
        try:
            with open(path) as f:
                r = (json.load(f) or {}).get("radios") or {}
            _CFG.update(key=key, radios={str(k): dict(v) for k, v in r.items() if isinstance(v, dict)})
        except (OSError, ValueError, AttributeError):
            _CFG.update(key=key, radios={})
    return dict(_CFG["radios"])
