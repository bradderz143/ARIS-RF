#!/usr/bin/env python3
"""Signal Hound BB60C / BB60D spectrum analyzers as ARIS receivers (a device plug-in).

Loaded by ARIS from plugins/: it registers the receiver type "bb60" (device ids `bb60-<serial>`), so /check lists
BB60s next to the RTL-SDR dongles and every feature that works from IQ or sweeps can use them (sweeps, Spectrum,
Name all FM stations, band captures, investigations' IQ analysis, live rtl_433 through iqpipe, locate readings).
ADS-B decoding needs an RTL-SDR (rtl_adsb), and the USB test in /check is for RTL-SDR dongles only.

Run as a program it is the helper the receiver type calls, with rtl_sdr's and rtl_power's command lines:
    signalhound_bb60.py list
    signalhound_bb60.py sdr -d SERIAL -f HZ -s RATE [-g GAIN] [-n SAMPLES] -          (CU8 IQ on stdout)
    signalhound_bb60.py power -d SERIAL -f LO:HI:STEP [-i S] [-1 | -e Ns] [-g GAIN] -  (rtl_power CSV on stdout)

Needs Signal Hound's BB API for Linux (libbb_api.so, from signalhound.com; set ARIS_BB_LIB to its path if it is not
on the library path) and numpy. The function signatures and constants below follow the BB API 5 header (bb_api.h);
check them against the SDK you install. Not yet tested on a real BB60: `ARIS_BB60_SIM=1` runs a simulated one
(noise and a few carriers) for the tests and for trying the page without hardware.

Gain: ARIS passes a gain in dB like an RTL-SDR's; here it sets the reference level (ref = 10 dBm - gain, so more
gain means a lower reference level and more sensitivity), with the BB60's own automatic gain and attenuation.
Receive-only: a BB60 cannot transmit, and nothing here asks it to.
"""
from __future__ import annotations

import ctypes
import datetime
import math
import os
import signal
import sys
import time

ABOUT = "Signal Hound BB60C / BB60D receivers (IQ, sweeps, live rtl_433 through a pipe)"

# --- BB API 5 constants (bb_api.h) ---------------------------------------------------------------------------
BB_SWEEPING, BB_STREAMING = 0, 4
BB_STREAM_IQ = 0
BB_AVERAGE, BB_LOG_SCALE = 1, 0
BB_RBW_SHAPE_NUTTALL, BB_NO_SPUR_REJECT = 0, 0
BB_AUTO_GAIN = BB_AUTO_ATTEN = -1
BB_DATA_32FC = 0
BB_BASE_RATE = 40.0e6            # IQ streaming: 40 MS/s divided by a power of two (1 ... 8192)


def ref_level(gain_db) -> float:
    try:
        g = float(gain_db)
    except (TypeError, ValueError):
        g = 20.0
    return max(-100.0, min(20.0, 10.0 - g))


# ----------------------------------------------------------------------------------------------------------------
# The library (or a simulated device)
# ----------------------------------------------------------------------------------------------------------------
class BBError(RuntimeError):
    pass


class Lib:
    def __init__(self):
        path = os.environ.get("ARIS_BB_LIB") or "libbb_api.so"
        self.c = ctypes.CDLL(path)
        c = self.c
        c.bbGetErrorString.restype = ctypes.c_char_p

    def _ok(self, st, what):
        if st < 0:
            msg = self.c.bbGetErrorString(st)
            raise BBError(f"{what}: {msg.decode(errors='replace') if msg else st}")
        return st

    def serials(self) -> list:
        arr, n = (ctypes.c_int * 8)(), ctypes.c_int(0)
        self._ok(self.c.bbGetSerialNumberList(arr, ctypes.byref(n)), "list")
        return [int(arr[i]) for i in range(n.value)]

    def open(self, serial) -> int:
        h = ctypes.c_int(-1)
        self._ok(self.c.bbOpenDeviceBySerialNumber(ctypes.byref(h), int(serial)), "open")
        return h.value

    def close(self, h):
        self.c.bbAbort(h)
        self.c.bbCloseDevice(h)

    def start_iq(self, h, centre_hz, decimation, bandwidth_hz, ref):
        c, d = self.c, ctypes.c_double
        self._ok(c.bbConfigureRefLevel(h, d(ref)), "reference level")
        self._ok(c.bbConfigureGainAtten(h, BB_AUTO_GAIN, BB_AUTO_ATTEN), "gain")
        self._ok(c.bbConfigureIQCenter(h, d(centre_hz)), "centre frequency")
        self._ok(c.bbConfigureIQ(h, int(decimation), d(bandwidth_hz)), "IQ rate")
        self._ok(c.bbConfigureIQDataType(h, BB_DATA_32FC), "IQ data type")
        self._ok(c.bbInitiate(h, BB_STREAMING, BB_STREAM_IQ), "start streaming")

    def read_iq(self, h, n):
        import numpy as np
        buf = np.zeros(2 * n, dtype=np.float32)
        z = ctypes.c_int(0)
        self._ok(self.c.bbGetIQUnpacked(h, buf.ctypes.data_as(ctypes.c_void_p), int(n), None, 0, 0,
                                        ctypes.byref(z), ctypes.byref(z), ctypes.byref(z), ctypes.byref(z)), "read IQ")
        return buf[0::2] + 1j * buf[1::2]

    def sweep(self, h, centre_hz, span_hz, rbw_hz, ref):
        import numpy as np
        c, d = self.c, ctypes.c_double
        self._ok(c.bbConfigureRefLevel(h, d(ref)), "reference level")
        self._ok(c.bbConfigureGainAtten(h, BB_AUTO_GAIN, BB_AUTO_ATTEN), "gain")
        self._ok(c.bbConfigureCenterSpan(h, d(centre_hz), d(span_hz)), "centre / span")
        self._ok(c.bbConfigureSweepCoupling(h, d(rbw_hz), d(rbw_hz), d(0.001), BB_RBW_SHAPE_NUTTALL,
                                            BB_NO_SPUR_REJECT), "sweep coupling")
        self._ok(c.bbConfigureAcquisition(h, BB_AVERAGE, BB_LOG_SCALE), "acquisition")
        self._ok(c.bbInitiate(h, BB_SWEEPING, 0), "start sweeping")
        n, bin_hz, start = ctypes.c_uint32(0), ctypes.c_double(0), ctypes.c_double(0)
        self._ok(c.bbQueryTraceInfo(h, ctypes.byref(n), ctypes.byref(bin_hz), ctypes.byref(start)), "trace info")
        lo, hi = np.zeros(n.value, dtype=np.float32), np.zeros(n.value, dtype=np.float32)
        self._ok(c.bbFetchTrace_32f(h, int(n.value), lo.ctypes.data_as(ctypes.c_void_p),
                                    hi.ctypes.data_as(ctypes.c_void_p)), "fetch trace")
        return start.value, bin_hz.value, hi


class SimLib:
    """A simulated BB60 (ARIS_BB60_SIM=1): noise at -95 dBm per bin, carriers from ARIS_BB60_SIM_CARRIERS
    ("MHz:dBm,MHz:dBm"; default an FM-band station and a 433.92 MHz carrier)."""
    def __init__(self):
        import numpy as np
        self.np = np
        self.rng = np.random.default_rng(1)
        spec = os.environ.get("ARIS_BB60_SIM_CARRIERS", "96.9:-40,433.92:-60")
        self.carriers = [(float(a) * 1e6, float(b)) for a, b in (x.split(":") for x in spec.split(",") if ":" in x)]
        self.state = {}

    def serials(self):
        return [int(x) for x in os.environ.get("ARIS_BB60_SIM_SERIALS", "24100123").split(",") if x.strip()]

    def open(self, serial):
        if int(serial) not in self.serials():
            raise BBError("open: device not found")
        return int(serial)

    def close(self, h):
        self.state.pop(h, None)

    def start_iq(self, h, centre_hz, decimation, bandwidth_hz, ref):
        self.state[h] = {"c": centre_hz, "fs": BB_BASE_RATE / decimation, "ref": ref, "t": 0}

    def read_iq(self, h, n):
        np, s = self.np, self.state[h]
        t = (s["t"] + np.arange(n)) / s["fs"]
        s["t"] += n
        z = (self.rng.normal(size=n) + 1j * self.rng.normal(size=n)) * 0.003
        for f, dbm in self.carriers:
            off = f - s["c"]
            if abs(off) < s["fs"] / 2:
                z += 10 ** ((dbm - s["ref"]) / 20.0) * np.exp(2j * np.pi * off * t)
        time.sleep(n / s["fs"])                       # real time, as from the device
        return z.astype(np.complex64)

    def sweep(self, h, centre_hz, span_hz, rbw_hz, ref):
        np = self.np
        n = max(2, int(span_hz / rbw_hz))
        start = centre_hz - span_hz / 2
        f = start + np.arange(n) * rbw_hz
        p = 10 ** ((-95.0 + self.rng.normal(scale=1.0, size=n)) / 10)
        for fc, dbm in self.carriers:
            p += 10 ** (dbm / 10) * np.exp(-0.5 * ((f - fc) / max(rbw_hz, 25e3)) ** 2)
        return start, rbw_hz, (10 * np.log10(p)).astype(np.float32)


def lib():
    return SimLib() if os.environ.get("ARIS_BB60_SIM") else Lib()


# ----------------------------------------------------------------------------------------------------------------
# The helper's commands
# ----------------------------------------------------------------------------------------------------------------
def _opt(a, flag, default=None):
    return a[a.index(flag) + 1] if flag in a and a.index(flag) + 1 < len(a) else default


def _hz(s) -> float:
    s = str(s).strip().lower()
    m = {"k": 1e3, "m": 1e6, "g": 1e9}.get(s[-1:], 1.0)
    return float(s[:-1] if s[-1:] in "kmg" else s) * m


def cmd_list(a) -> int:
    try:
        for s in lib().serials():
            print(f"{s}\tSignal Hound BB60 ({s})")
    except (OSError, BBError) as e:
        print(f"BB60: {e}", file=sys.stderr)
        return 1
    return 0


def decimation_for(rate) -> int:
    """The largest decimation whose rate is still at least `rate` (resampled down to it afterwards)."""
    d = 1
    while d < 8192 and BB_BASE_RATE / (d * 2) >= float(rate):
        d *= 2
    return d


class Resampler:
    """Fractional linear resampling of a complex stream, keeping its phase across blocks."""
    def __init__(self, fs_in, fs_out):
        self.r = float(fs_in) / float(fs_out)       # input samples per output sample (>= 1)
        self.t = 0.0                                # next output position, in input samples of the buffer
        self.last = None                            # the previous block's last sample (index 0 of the buffer)

    def __call__(self, z):
        import numpy as np
        if self.last is not None:
            z = np.concatenate([[self.last], z])
        n = len(z)
        if n < 2:
            if n:
                self.last = z[-1]
            return z[:0]
        idx = np.arange(self.t, n - 1, self.r)
        i0 = idx.astype(int)
        fr = (idx - i0).astype(np.float32)
        out = z[i0] * (1 - fr) + z[i0 + 1] * fr
        self.t = ((idx[-1] + self.r) if len(idx) else self.t) - (n - 1)
        self.last = z[-1]
        return out


def to_cu8(z):
    import numpy as np
    out = np.empty(2 * len(z), dtype=np.float32)
    out[0::2], out[1::2] = z.real, z.imag
    return np.clip(np.rint(out * 127.0 + 127.5), 0, 255).astype(np.uint8).tobytes()


def cmd_sdr(a) -> int:
    serial = _opt(a, "-d")
    freq, rate = _hz(_opt(a, "-f", "100e6")), _hz(_opt(a, "-s", "2048000"))
    n_total = int(float(_opt(a, "-n", "0") or 0))
    L = lib()
    try:
        h = L.open(serial)
    except (OSError, BBError, ValueError) as e:
        print(f"BB60 {serial}: {e}", file=sys.stderr)
        return 1
    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    try:
        d = decimation_for(rate)
        fs_in = BB_BASE_RATE / d
        L.start_iq(h, freq, d, min(fs_in * 0.8, 27e6), ref_level(_opt(a, "-g")))
        rs = Resampler(fs_in, rate)
        sent, block = 0, 1 << 16
        out = sys.stdout.buffer
        while not stop["now"] and (not n_total or sent < n_total):
            z = rs(L.read_iq(h, block))
            if n_total:
                z = z[: n_total - sent]
            out.write(to_cu8(z))
            sent += len(z)
        out.flush()
    except BrokenPipeError:
        pass
    except BBError as e:
        print(f"BB60 {serial}: {e}", file=sys.stderr)
        return 1
    finally:
        L.close(h)
    return 0


def cmd_power(a) -> int:
    serial = _opt(a, "-d")
    lo, hi, step = (_hz(x) for x in _opt(a, "-f", "88M:108M:10k").split(":"))
    integ = float(_opt(a, "-i", "1"))
    single = "-1" in a
    dur = _opt(a, "-e")
    end = time.time() + (float(dur.rstrip("s")) if dur else 0)
    L = lib()
    try:
        h = L.open(serial)
    except (OSError, BBError, ValueError) as e:
        print(f"BB60 {serial}: {e}", file=sys.stderr)
        return 1
    import numpy as np
    grid = lo + np.arange(int((hi - lo) / step)) * step
    per_row = max(1, int(1e6 / step))
    try:
        while True:
            t0, acc, k = time.time(), np.zeros(len(grid)), 0
            while True:
                start, bin_hz, trace = L.sweep(h, (lo + hi) / 2, hi - lo, step, ref_level(_opt(a, "-g")))
                f = start + np.arange(len(trace)) * bin_hz
                acc += 10 ** (np.interp(grid, f, trace) / 10)
                k += 1
                if time.time() - t0 >= integ:
                    break
            db = 10 * np.log10(acc / max(k, 1))
            now = datetime.datetime.now()
            rows = []
            for i in range(0, len(grid), per_row):
                vals = db[i:i + per_row]
                f0 = grid[i]
                vs = [f"{v:.2f}" for v in vals] + [f"{vals[-1]:.2f}"]          # rtl_power prints the last twice
                rows.append(f"{now:%Y-%m-%d}, {now:%H:%M:%S}, {int(f0)}, {int(f0 + len(vals) * step)}, "
                            f"{step:.2f}, {k}, " + ", ".join(vs))
            sys.stdout.write("\n".join(rows) + "\n")
            sys.stdout.flush()
            if single or time.time() >= end:
                break
    except BBError as e:
        print(f"BB60 {serial}: {e}", file=sys.stderr)
        return 1
    finally:
        L.close(h)
    return 0


def main(argv=None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    try:
        ctypes.CDLL(None).prctl(15, b"aris-bb60", 0, 0, 0)
    except Exception:
        pass
    cmds = {"list": cmd_list, "sdr": cmd_sdr, "power": cmd_power}
    if not a or a[0] not in cmds:
        print(__doc__, file=sys.stderr)
        return 2
    return cmds[a[0]](a[1:])


# ----------------------------------------------------------------------------------------------------------------
# The plug-in side
# ----------------------------------------------------------------------------------------------------------------
def setup(aris):
    from arislib import radios

    class BB60(radios.HelperBackend):
        name, prefix, label = "bb60", "bb60", "Signal Hound BB60"
        freq_range_mhz = (0.009, 6000.0)
        gains = [float(g) for g in range(0, 51, 5)]
        sweep_unit = "dBm"          # the BB60's sweeps are calibrated: shown as they are, never as dBFS

        def helper_argv(self):
            return [sys.executable, os.path.abspath(__file__)]

    aris.register_backend(BB60())


if __name__ == "__main__":
    sys.exit(main())
