#!/usr/bin/env python3
"""Signal Hound SA44B / SA124B spectrum analyzers as ARIS receivers (a device plug-in).

Loaded by ARIS from plugins/: it registers the receiver type "sa" (device ids `sa-<serial>`), so /check lists them
next to the other receivers. What they are good at here: sweeps from 100 kHz to 12.4 GHz (SA124B; 4.4 GHz on an
SA44B) in calibrated dBm, so Spectrum, baselines, patrol and the threat audit reach bands an RTL-SDR cannot (2.4 and
5.8 GHz, mobile-phone bands); investigations' rtl_433 (it runs at the SA's own 486 kS/s); the pager / APRS
decoders; locate readings. What they cannot do: their IQ stream is at most 486 kS/s and 250 kHz wide, so steps that
need 2.048 MS/s (Name all FM stations in slices, band captures, the wide IQ look of an investigation) are refused in
plain words and done by another receiver. ADS-B decoding needs an RTL-SDR.

Run as a program it is the helper the receiver type calls, with rtl_sdr's and rtl_power's command lines:
    signalhound_sa.py list
    signalhound_sa.py sdr -d SERIAL -f HZ -s RATE [-g GAIN] [-n SAMPLES] -          (CU8 IQ on stdout)
    signalhound_sa.py power -d SERIAL -f LO:HI:STEP [-i S] [-1 | -e Ns] [-g GAIN] -  (rtl_power CSV on stdout)

Needs Signal Hound's SA API for Linux (libsa_api.so, from signalhound.com; set ARIS_SA_LIB to its path if it is not on
the library path) and numpy. The signatures and constants below follow the SA API header (sa_api.h); check them
against the SDK you install. Not yet tested on a real SA44B / SA124B: `ARIS_SA_SIM=1` runs a simulated one (noise and
a few carriers, one of them at 2.437 GHz) for the tests and for trying the page without hardware.

Gain: ARIS passes a gain in dB like an RTL-SDR's; here it sets the reference level (ref = 10 dBm - gain, at most
+20 dBm), with the SA's automatic gain and attenuation, the pre-amplifier on for low reference levels. IQ samples
come "scaled to mW" from the API; they are divided by the reference level so that it maps to the 8-bit full scale
(if real levels look tens of dB off, this scaling is the first thing to check).
Receive-only: the SA44B / SA124B cannot transmit. A Signal Hound tracking generator can be attached to these
analyzers for network measurements, and that does transmit: this plug-in never uses it, and never calls the API's
tracking-generator functions or sweep mode.
"""
from __future__ import annotations

import ctypes
import datetime
import os
import signal
import sys
import time

ABOUT = "Signal Hound SA44B / SA124B receivers (sweeps to 12.4 GHz in dBm; IQ up to 486 kS/s)"

# --- SA API constants (sa_api.h) -----------------------------------------------------------------------------
SA_SWEEPING, SA_IQ = 0x0, 0x2
SA_AVERAGE, SA_LOG_SCALE = 0x1, 0x0
SA_AUTO_ATTEN = SA_AUTO_GAIN = -1
SA_IQ_SAMPLE_RATE = 486111.111          # IQ streaming: this rate divided by a power of two (1 ... 128)
SA_MAX_IQ_DECIMATION = 128
SA_MAX_IQ_BANDWIDTH = 250.0e3
SA_MAX_REF = 20.0
SA_MIN_RBW, SA_MAX_RBW = 100.0, 250.0e3
DEVICE_TYPES = {1: "SA44", 2: "SA44B", 3: "SA124A", 4: "SA124B"}


def ref_level(gain_db) -> float:
    try:
        g = float(gain_db)
    except (TypeError, ValueError):
        g = 20.0
    return max(-100.0, min(SA_MAX_REF, 10.0 - g))


# ----------------------------------------------------------------------------------------------------------------
# The library (or a simulated device)
# ----------------------------------------------------------------------------------------------------------------
class SAError(RuntimeError):
    pass


class Lib:
    def __init__(self):
        path = os.environ.get("ARIS_SA_LIB") or "libsa_api.so"
        self.c = ctypes.CDLL(path)
        self.c.saGetErrorString.restype = ctypes.c_char_p

    def _ok(self, st, what):
        if st < 0:
            msg = self.c.saGetErrorString(st)
            raise SAError(f"{what}: {msg.decode(errors='replace') if msg else st}")
        return st

    def serials(self) -> list:
        arr, n = (ctypes.c_int * 8)(), ctypes.c_int(0)
        self._ok(self.c.saGetSerialNumberList(arr, ctypes.byref(n)), "list")
        return [int(arr[i]) for i in range(n.value)]

    def open(self, serial) -> int:
        h = ctypes.c_int(-1)
        self._ok(self.c.saOpenDeviceBySerialNumber(ctypes.byref(h), int(serial)), "open")
        return h.value

    def model(self, h) -> str:
        t = ctypes.c_int(0)
        try:
            self._ok(self.c.saGetDeviceType(h, ctypes.byref(t)), "device type")
        except SAError:
            return ""
        return DEVICE_TYPES.get(t.value, "")

    def close(self, h):
        self.c.saAbort(h)
        self.c.saCloseDevice(h)

    def _level(self, h, ref):
        c = self.c
        self._ok(c.saConfigLevel(h, ctypes.c_double(ref)), "reference level")
        self._ok(c.saConfigGainAtten(h, SA_AUTO_ATTEN, SA_AUTO_GAIN, ctypes.c_bool(ref < -10.0)), "gain")

    def start_iq(self, h, centre_hz, decimation, bandwidth_hz, ref):
        c, d = self.c, ctypes.c_double
        self._ok(c.saConfigCenterSpan(h, d(centre_hz), d(bandwidth_hz)), "centre frequency")   # span: ignored
        self._level(h, ref)
        self._ok(c.saConfigIQ(h, int(decimation), d(bandwidth_hz)), "IQ rate")
        self._ok(c.saInitiate(h, SA_IQ, 0), "start streaming")
        self.scale = 1.0 / (10 ** (ref / 20.0))       # mW-scaled samples -> the reference level at full scale

    def read_iq(self, h, n):
        import numpy as np
        buf = np.zeros(2 * n, dtype=np.float32)
        z = ctypes.c_int(0)
        self._ok(self.c.saGetIQDataUnpacked(h, buf.ctypes.data_as(ctypes.c_void_p), int(n), 0, ctypes.byref(z),
                                            ctypes.byref(z), ctypes.byref(z), ctypes.byref(z)), "read IQ")
        return (buf[0::2] + 1j * buf[1::2]) * self.scale

    def sweep(self, h, centre_hz, span_hz, rbw_hz, ref):
        import numpy as np
        c, d = self.c, ctypes.c_double
        self._ok(c.saConfigCenterSpan(h, d(centre_hz), d(span_hz)), "centre / span")
        self._level(h, ref)
        self._ok(c.saConfigSweepCoupling(h, d(rbw_hz), d(rbw_hz), ctypes.c_bool(True)), "sweep coupling")
        self._ok(c.saConfigAcquisition(h, SA_AVERAGE, SA_LOG_SCALE), "acquisition")
        self._ok(c.saInitiate(h, SA_SWEEPING, 0), "start sweeping")
        n, start, bin_hz = ctypes.c_int(0), ctypes.c_double(0), ctypes.c_double(0)
        self._ok(c.saQuerySweepInfo(h, ctypes.byref(n), ctypes.byref(start), ctypes.byref(bin_hz)), "sweep info")
        lo, hi = np.zeros(n.value, dtype=np.float32), np.zeros(n.value, dtype=np.float32)
        self._ok(c.saGetSweep_32f(h, lo.ctypes.data_as(ctypes.c_void_p), hi.ctypes.data_as(ctypes.c_void_p)),
                 "fetch sweep")
        return start.value, bin_hz.value, hi


class SimLib:
    """A simulated SA124B (ARIS_SA_SIM=1): noise at -105 dBm per bin, carriers from ARIS_SA_SIM_CARRIERS
    ("MHz:dBm,MHz:dBm"; default an FM-band station, a 433.92 MHz carrier and a 2.437 GHz one)."""
    def __init__(self):
        import numpy as np
        self.np = np
        self.rng = np.random.default_rng(2)
        spec = os.environ.get("ARIS_SA_SIM_CARRIERS", "96.9:-40,433.92:-60,2437:-55")
        self.carriers = [(float(a) * 1e6, float(b)) for a, b in (x.split(":") for x in spec.split(",") if ":" in x)]
        self.state = {}

    def serials(self):
        return [int(x) for x in os.environ.get("ARIS_SA_SIM_SERIALS", "19184001").split(",") if x.strip()]

    def open(self, serial):
        if int(serial) not in self.serials():
            raise SAError("open: device not found")
        return int(serial)

    def model(self, h):
        return "SA124B"

    def close(self, h):
        self.state.pop(h, None)

    def start_iq(self, h, centre_hz, decimation, bandwidth_hz, ref):
        self.state[h] = {"c": centre_hz, "fs": SA_IQ_SAMPLE_RATE / decimation, "bw": bandwidth_hz, "ref": ref, "t": 0}

    def read_iq(self, h, n):
        np, s = self.np, self.state[h]
        t = (s["t"] + np.arange(n)) / s["fs"]
        s["t"] += n
        z = (self.rng.normal(size=n) + 1j * self.rng.normal(size=n)) * 0.002
        for f, dbm in self.carriers:
            off = f - s["c"]
            if abs(off) < s["bw"] / 2:
                z += 10 ** ((dbm - s["ref"]) / 20.0) * np.exp(2j * np.pi * off * t)
        time.sleep(n / s["fs"])                       # real time, as from the device
        return z.astype(np.complex64)

    def sweep(self, h, centre_hz, span_hz, rbw_hz, ref):
        np = self.np
        n = max(2, int(span_hz / rbw_hz))
        start = centre_hz - span_hz / 2
        f = start + np.arange(n) * rbw_hz
        p = 10 ** ((-105.0 + self.rng.normal(scale=1.0, size=n)) / 10)
        for fc, dbm in self.carriers:
            p += 10 ** (dbm / 10) * np.exp(-0.5 * ((f - fc) / max(rbw_hz, 25e3)) ** 2)
        return start, rbw_hz, (10 * np.log10(p)).astype(np.float32)


def lib():
    return SimLib() if os.environ.get("ARIS_SA_SIM") else Lib()


# ----------------------------------------------------------------------------------------------------------------
# The helper's commands (each plug-in file stands alone, so these few helpers are its own)
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
            print(f"{s}\tSignal Hound SA44B / SA124B ({s})")
    except (OSError, SAError) as e:
        print(f"SA44B / SA124B: {e}", file=sys.stderr)
        return 1
    return 0


def decimation_for(rate) -> int:
    """The largest decimation whose rate is still at least `rate` (resampled down to it afterwards)."""
    d = 1
    while d < SA_MAX_IQ_DECIMATION and SA_IQ_SAMPLE_RATE / (d * 2) >= float(rate):
        d *= 2
    return d


class Resampler:
    """Fractional linear resampling of a complex stream, keeping its phase across blocks."""
    def __init__(self, fs_in, fs_out):
        self.r = float(fs_in) / float(fs_out)
        self.t = 0.0
        self.last = None

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
    freq, rate = _hz(_opt(a, "-f", "100e6")), _hz(_opt(a, "-s", "250000"))
    n_total = int(float(_opt(a, "-n", "0") or 0))
    if rate > SA_IQ_SAMPLE_RATE * 1.001:
        print(f"SA44B / SA124B {serial}: {rate / 1e6:.3g} MS/s asked; it streams IQ at {SA_IQ_SAMPLE_RATE / 1e3:.0f} "
              "kS/s at most", file=sys.stderr)
        return 1
    L = lib()
    try:
        h = L.open(serial)
    except (OSError, SAError, ValueError) as e:
        print(f"SA44B / SA124B {serial}: {e}", file=sys.stderr)
        return 1
    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    try:
        d = decimation_for(rate)
        fs_in = SA_IQ_SAMPLE_RATE / d
        L.start_iq(h, freq, d, min(fs_in * 0.8, SA_MAX_IQ_BANDWIDTH), ref_level(_opt(a, "-g")))
        rs = Resampler(fs_in, rate)
        sent, block = 0, 1 << 14
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
    except SAError as e:
        print(f"SA44B / SA124B {serial}: {e}", file=sys.stderr)
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
    except (OSError, SAError, ValueError) as e:
        print(f"SA44B / SA124B {serial}: {e}", file=sys.stderr)
        return 1
    import numpy as np
    grid = lo + np.arange(int((hi - lo) / step)) * step
    per_row = max(1, int(1e6 / step))
    rbw = max(SA_MIN_RBW, min(SA_MAX_RBW, step))
    try:
        while True:
            t0, acc, k = time.time(), np.zeros(len(grid)), 0
            while True:
                start, bin_hz, trace = L.sweep(h, (lo + hi) / 2, hi - lo, rbw, ref_level(_opt(a, "-g")))
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
    except SAError as e:
        print(f"SA44B / SA124B {serial}: {e}", file=sys.stderr)
        return 1
    finally:
        L.close(h)
    return 0


def main(argv=None) -> int:
    a = list(sys.argv[1:] if argv is None else argv)
    try:
        ctypes.CDLL(None).prctl(15, b"aris-sa", 0, 0, 0)
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

    class SignalHoundSA(radios.HelperBackend):
        name, prefix, label = "sa", "sa", "Signal Hound SA44B / SA124B"
        freq_range_mhz = (0.1, 12400.0)
        gains = [float(g) for g in range(0, 51, 5)]
        sweep_unit = "dBm"                  # calibrated sweeps: shown as they are
        max_iq_rate = SA_IQ_SAMPLE_RATE     # 486 kS/s, 250 kHz wide: wider steps are refused in plain words

        def helper_argv(self):
            return [sys.executable, os.path.abspath(__file__)]

    aris.register_backend(SignalHoundSA())


if __name__ == "__main__":
    sys.exit(main())
