"""Micro-benchmarks of ARIS hot paths (no radios).  python3 bench.py
Times the DSP filter, sweep parsing, baseline statistics and lookups, and the spectrum frame clean-up."""
import os
import random
import sys
import time

import harness  # noqa: F401
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import load  # noqa: E402

A = load.aris


def t(label, fn, n=3):
    fn()
    best = min(_time(fn) for _ in range(n))
    print(f"{label:<44} {best * 1000:8.1f} ms")
    return best


def _time(fn):
    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


rng = np.random.default_rng(0)
z1 = (rng.normal(0, .1, 625000) + 1j * rng.normal(0, .1, 625000)).astype(np.complex64)   # stage 1: 2.5 s @ 250k
z2 = (rng.normal(0, .1, 1536000) + 1j * rng.normal(0, .1, 1536000)).astype(np.complex64)  # wideband 1.5 s @ 1.024M
t("mix/filter/decimate stage1 (129 taps, /5)", lambda: A._mix_filter_decimate(z1, 250000, 40000, 12500, 5))
t("mix/filter/decimate wideband (63 taps, /4)", lambda: A._mix_filter_decimate(z2, 1024000, 250000, 110e3, 4, 63))

# timed sweep CSV: 902-928 MHz, 10 kHz, 30 rows
path = os.path.join(harness.DATA, "bench.csv")
with open(path, "w") as f:
    for r in range(30):
        for h in range(12):
            lo = 902e6 + h * 2.2e6
            vals = ", ".join(f"{random.uniform(-40, -20):.2f}" for _ in range(220))
            f.write(f"2026-11-01, 10:00:{r:02d}, {lo:.0f}, {lo + 2.2e6:.0f}, 10000.00, 4096, {vals}\n")
t("parse rtl_power CSV (30 rows x 2640 bins)", lambda: A._parse_rtl_power_csv(path))
acc, hops = A._parse_rtl_power_csv(path)
t("collapse to baseline grid", lambda: A._collapse(acc, 10000.0))

# baselines: 3 bands x 3 dongles
A.BASELINE_DIR.mkdir(parents=True, exist_ok=True)
grid = A._collapse(acc, 10000.0)
for band, (s, e) in {"433": (433.0, 435.0), "915": (902.0, 928.0), "fm": (88.0, 108.0)}.items():
    for ser in A.DONGLE_NAMES:
        A._atomic_write_json(A.BASELINE_DIR / f"{s:g}-{e:g}MHz_{ser}.json", {
            "version": 6, "band": {"start_mhz": s, "stop_mhz": e}, "serial": ser, "grid_hz": 10000.0,
            "bins": {str(i): [round(v["mean"], 2), round(v["max"], 2), round(v["std"], 2)] for i, v in grid.items()}})
t("find baseline (1 lookup, 9 files)", lambda: A._find_baseline(902.0, 928.0, "00003003"))
t("patrol round baseline lookups (18)", lambda: [A._find_baseline(s, e, ser) for s, e in
                                                 ((433, 435), (902, 928), (88, 108)) for ser in A.DONGLE_NAMES
                                                 for _ in range(2)])
t("load config x100", lambda: [A._load_config() for _ in range(100)])
vals = [random.uniform(-40, -20) for _ in range(2640)]
t("robust floor x100 (2640 bins)", lambda: [A._robust_floor(vals) for _ in range(100)])
hl = sorted(h / 1e6 for h in hops)
fr = [902 + i * 0.01 for i in range(2600)]
t("near-hop test for 2600 bins", lambda: [A._near_hop(f, hl, 0.025) for f in fr])

from arisweb import spectrum as S  # noqa: E402
freqs = [30 + i * 0.2 for i in range(851)]
db = [random.uniform(-50, -40) for _ in freqs]
hops2 = [30 + 2.0 * i + 1.0 for i in range(85)]
t("spectrum hop-spike fill (30-200 MHz)", lambda: S._fill_hop_spikes(freqs, list(db), hops2, 200.0, 0.5))
from arislib import specplan as SPL  # noqa: E402
rng4 = {"a": (24.0, 1766.0), "b": (24.0, 1766.0), "c": (24.0, 1766.0), "d": (0.1, 12400.0)}
t("spectrum plan x100 (0-6000 MHz, 4 receivers)", lambda: [SPL.plan(0, 6000, rng4, 0.8) for _ in range(100)])
big_f = [24 + i * 0.01 for i in range(100000)]
big_d = [random.uniform(-50, -40) for _ in big_f]
t("spectrum pool 100000 points to 1600", lambda: S._pool(big_f, big_d, 1600))

# v4.7: RDS station name from a 3 s look (256 kS/s MPX), rtl_433 output of a busy 60 s listen
from arislib import rds as R  # noqa: E402
fs_mpx = 256000.0
tt = np.arange(int(fs_mpx * 3.0)) / fs_mpx
mpx = (0.1 * np.sin(2 * np.pi * R.PILOT_HZ * tt) + 0.04 * R.rds_waveform(tt, 0x1A2B, "ARIS RDS", -np.pi / 2, 0.7)
       + 0.4 * np.sin(2 * np.pi * 1000 * tt) + 0.02 * rng.standard_normal(len(tt)))
t("RDS decode (3 s of FM multiplex)", lambda: R.decode(mpx, fs_mpx))
pkg = ("Detected OOK package\t@0.1s\n" + '{"model" : "Acurite-Tower", "id" : 42, "temperature_C" : 21.5, "snr" : 20.1}\n'
       + "Analyzing pulses...\nTotal count:   25,  width: 37.72 ms\t\t( 9430 S)\nGap width distribution:\n"
       + " [ 0] count:   60,  width: 1196 us [1188;1200]\t( 299 S)\nRSSI: -6.7 dB SNR: 27.7 dB Noise: -34.4 dB\n"
       + "Guessing modulation: Pulse Width Modulation with fixed period\n"
       + "Use a flex decoder with -X 'n=name,m=OOK_PWM,s=400,l=1200,r=10404,g=1204,t=320,y=0'\n")
big = pkg * 600
t("parse rtl_433 output (600 packages)", lambda: A._parse_rtl433(big))
