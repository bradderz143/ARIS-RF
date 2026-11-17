"""The look profile (arislib/siglook.py): what one narrowband look measures, and what it refuses to say.
python3 -m unittest test_siglook   (seeded synthetic channel samples at 50 kS/s: no radios, no engine)

A wrong kind is worse than "unknown" or "untested": where a signal is not reliable at a level, the tests assert
that it is never called something it is not. Simulation only: white noise and ideal modulators."""
import json
import math
import os
import sys
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arislib import siglook  # noqa: E402

FS, HW, SIGMA = 50000.0, 12500.0, 0.02
# The levels of the matrix: half a dB over the gates at 8 and 12 dB, so a row tests the measurement and not the
# gate's edge (a look at exactly 8.0 dB falls either side of it).
LEVELS = (8.5, 12.5, 16.0, 25.0)


def _fir(cut, taps, fs=FS):
    n = np.arange(taps) - (taps - 1) / 2.0
    h = np.sinc(2.0 * cut / fs * n) * np.hamming(taps)
    return h / h.sum()


def _lowpass(x, cut, fs=FS):
    """Zero-phase low-pass with a soft edge (in the frequency domain: quick for long test signals)."""
    f = np.fft.rfftfreq(len(x), 1.0 / fs)
    edge = np.clip((1.3 * cut - f) / (0.3 * cut), 0.0, 1.0)
    return np.fft.irfft(np.fft.rfft(x) * (0.5 - 0.5 * np.cos(np.pi * edge)), len(x))


def _voice(n, rng, talk=0.7):
    """Voice-like audio: noise between 300 and 3000 Hz with a speech tilt, syllables and pauses; peaks near 1."""
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1.0 / FS)
    spec[(f < 300) | (f > 3000)] = 0.0
    x = np.fft.irfft(spec / np.maximum(f, 300.0) ** 0.5, n)
    t = np.arange(n) / FS
    syllables = 0.55 + 0.45 * np.sin(2 * np.pi * 3.7 * t + rng.uniform(0, 6.28))
    talking = _lowpass((np.sin(2 * np.pi * 0.45 * t + rng.uniform(0, 6.28)) > 1 - 2 * talk).astype(float), 20.0)
    return np.clip(x / np.std(x) * syllables * talking / 3.0, -1, 1)


def _symbols(n, baud, rng, levels=(-1, 1), bt=None, preamble=0):
    per = FS / baud
    sy = rng.choice(levels, int(n / per) + 2).astype(float)
    if preamble:
        sy[:preamble] = np.where(np.arange(preamble) % 2 == 0, max(levels), min(levels))
    x = sy[np.minimum((np.arange(n) / per).astype(int), len(sy) - 1)]
    return _lowpass(x, bt * baud) if bt else x


def look(kind, snr_db, seed, seconds=3.0, hw=HW, drift=0.0, key=None, offset=0.0, fade=None):
    """(channel samples, noise power per sample) of a signal snr_db over the channel's noise. drift in Hz per
    second; key = (on ms, off ms); fade = (depth dB, period s); offset in Hz from the centre."""
    rng = np.random.default_rng(seed)
    h = _fir(hw, 27)                         # the channel filter (like the engine's 129 taps at 250 kS/s)
    n = int(FS * seconds) + len(h) - 1
    t = np.arange(n) / FS
    env, dev, extra = np.ones(n), np.zeros(n), 0.0
    if kind in ("carrier", "noise"):
        pass
    elif kind == "am_voice":
        env = 1.0 + 0.8 * _voice(n, rng)
    elif kind == "am_tone":
        env = 1.0 + 0.5 * np.sin(2 * np.pi * 1000 * t)
    elif kind == "fm_voice":                 # a 12.5 kHz channel: 2.5 kHz peak deviation
        dev = 2500.0 * _voice(n, rng)
    elif kind == "fm_voice_wide":            # a 25 kHz channel: 5 kHz peak
        dev = 5000.0 * _voice(n, rng)
    elif kind == "fm_voice_tone":            # voice with a 123.0 Hz sub-audible tone
        dev = 2200.0 * _voice(n, rng) + 500.0 * np.sin(2 * np.pi * 123.0 * t)
    elif kind == "fm_tone_only":             # keyed up, nobody talking
        dev = 500.0 * np.sin(2 * np.pi * 123.0 * t)
    elif kind.startswith("hum_"):            # mains hum on a carrier: hum_<rms deviation in Hz>, at 120 Hz
        dev = float(kind[4:]) * math.sqrt(2.0) * np.sin(2 * np.pi * 120.0 * t)
    elif kind.startswith("fm_audio_tone_"):  # one audio tone at 3 kHz deviation (tone signalling)
        dev = 3000.0 * np.sin(2 * np.pi * float(kind[14:]) * t)
    elif kind == "fsk2_1200_preamble":       # 576 bits of 1010 first, as a pager sends
        dev = 4500.0 * _symbols(n, 1200, rng, bt=2.0, preamble=576)
    elif kind.startswith("fsk2_"):           # two levels, 4.5 kHz either side
        dev = 4500.0 * _symbols(n, float(kind[5:]), rng, bt=2.0)
    elif kind == "fsk4_4800":                # four levels at 0.6 and 1.8 kHz
        dev = 600.0 * _symbols(n, 4800, rng, levels=(-3, -1, 1, 3), bt=0.6)
    elif kind == "fsk4_2400":                # four levels at 0.35 and 1.05 kHz
        dev = 350.0 * _symbols(n, 2400, rng, levels=(-3, -1, 1, 3), bt=0.6)
    elif kind == "audio_fsk":                # 1200 / 2200 Hz audio tones on FM (packet radio)
        dev = 3000.0 * np.sin(2 * np.pi * np.cumsum(np.where(_symbols(n, 1200, rng) > 0, 1200.0, 2200.0)) / FS)
    elif kind.startswith("two_"):            # a second carrier: two_<Hz away>_<dB weaker>
        _, away, weaker = kind.split("_")
        extra = 10 ** (-float(weaker) / 20.0) * np.exp(2j * np.pi * float(away) * t + 1j * rng.uniform(0, 6.28))
    else:
        raise ValueError(kind)
    if key:
        period = (key[0] + key[1]) / 1000.0
        env = env * (((t + rng.uniform(0, period)) % period) < key[0] / 1000.0)
    if fade:
        env = env * 10 ** (-(fade[0] / 2.0) * (1 - np.cos(2 * np.pi * t / fade[1])) / 20.0)
    sig = (env * np.exp(2j * np.pi * np.cumsum(dev) / FS) + extra) * np.exp(2j * np.pi * (offset * t + 0.5 * drift * t * t))
    noise = 2 * SIGMA ** 2 * float(np.sum(h ** 2))
    amp = 0.0 if kind == "noise" else math.sqrt(noise * 10 ** (snr_db / 10.0))
    z = amp * sig + SIGMA * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    return np.convolve(z, h, mode="valid").astype(np.complex64), noise


def prof(kind, snr_db, seed=1, mode="full", clip=0.0, **kw):
    y, noise = look(kind, snr_db, seed, **kw)
    return siglook.profile(y, FS, noise, kw.get("hw", HW), mode=mode, clip_frac=clip)


def _plain(v):
    """Only bool, int, float, str, None, list and dict (no numpy scalars: the card goes out as JSON)."""
    if isinstance(v, dict):
        return all(type(k) is str and _plain(x) for k, x in v.items())
    if isinstance(v, list):
        return len(v) <= 60 and all(_plain(x) for x in v)
    return v is None or type(v) in (bool, int, float, str)


CLAIMS = ("none", "lines", "tone", "fsk2", "fsk4", "data", "am", "fm_voice")      # kinds that say what it is


class Shape(unittest.TestCase):
    def test_the_profile_has_the_contracts_keys_and_plain_values(self):
        p = prof("carrier", 16)
        self.assertEqual(set(p), {"seconds", "snr_on_db", "quality", "overloaded", "folded", "key", "line", "width",
                                  "level", "mod", "strip", "strip_slot_s"})
        self.assertEqual(set(p["key"]), {"state", "on_pct", "runs", "longest_on_s", "longest_off_s", "shortest_on_ms",
                                         "period_s", "open_start", "open_end", "why"})
        self.assertEqual(set(p["line"]), {"ok", "hz", "share", "slope_hz_s", "span_s", "scatter_hz", "pieces",
                                          "others_hz", "why"})
        self.assertEqual(set(p["width"]), {"hz", "share", "limit", "why"})
        self.assertEqual(set(p["level"]), {"range_db", "why"})
        self.assertEqual(set(p["mod"]), {"kind", "tone_hz", "rate_baud", "levels", "am_index", "dev_rms_hz",
                                         "hum_hz", "stretch_s", "why", "trial"})
        self.assertTrue(_plain(p), p)
        self.assertEqual(json.loads(json.dumps(p, allow_nan=False)), p)
        self.assertEqual((p["seconds"], p["quality"], p["overloaded"], p["strip"], p["strip_slot_s"]),
                         (3.0, "A", False, [1, 1, 1, 1, 1, 1], 0.5))
        self.assertEqual(p["key"], {"state": "on_whole", "on_pct": 100.0, "runs": 1, "longest_on_s": 3.0,
                                    "longest_off_s": 0.0, "shortest_on_ms": None, "period_s": None,
                                    "open_start": True, "open_end": True, "why": None})
        self.assertAlmostEqual(p["snr_on_db"], 16.0, delta=0.5)

    def test_light_mode_leaves_out_width_level_and_modulation_and_agrees_on_the_rest(self):
        y, noise = look("carrier", 14, 3, drift=40.0, offset=-2100.0)
        full, light = siglook.profile(y, FS, noise, HW), siglook.profile(y, FS, noise, HW, mode="light")
        self.assertEqual(set(light), {"seconds", "snr_on_db", "quality", "overloaded", "folded", "key", "line",
                                      "strip", "strip_slot_s"})
        self.assertEqual(light, {k: full[k] for k in light})
        self.assertTrue(_plain(light))

    def test_every_kind_of_result_is_plain_json(self):
        for kind, snr, kw in (("noise", 0, {}), ("carrier", 4, {}), ("carrier", 12, {"key": (50, 950)}),
                              ("fsk2_1200", 16, {}), ("fm_voice_tone", 16, {}), ("am_tone", 16, {}),
                              ("two_1400_3", 16, {}), ("carrier", 16, {"seconds": 0.2}), ("fsk4_4800", 16, {"hw": 6000.0})):
            for mode in ("full", "light"):
                p = prof(kind, snr, 2, mode=mode, **kw)
                self.assertTrue(_plain(p), (kind, p))
                json.dumps(p, allow_nan=False)


class Modulation(unittest.TestCase):
    """The matrix: each signal at each level reads its own kind where that is reliable, and elsewhere "unknown",
    "untested" or (a rate without a level count) "data", never another kind."""
    #        kind             what it is  rate / tone   level -> kinds it may read (one entry: it must)
    MATRIX = (
        ("carrier",           "none",     None, ({"none"}, {"none"}, {"none"}, {"none"})),
        ("am_tone",           "am",       None, ({"unknown"}, {"am"}, {"am"}, {"am"})),
        ("am_voice",          "am",       None, ({"unknown"}, {"am", "unknown"}, {"am", "unknown"}, {"am", "unknown"})),
        ("fm_voice",          "fm_voice", None, ({"fm_voice", "unknown"}, {"fm_voice"}, {"fm_voice"}, {"fm_voice"})),
        ("fm_voice_wide",     "fm_voice", None, ({"fm_voice", "unknown"}, {"fm_voice"}, {"fm_voice"}, {"fm_voice"})),
        ("fm_voice_tone",     "tone",     123.0, ({"tone", "fm_voice", "unknown"}, {"tone"}, {"tone"}, {"tone"})),
        ("fm_tone_only",      "tone",     123.0, ({"tone", "unknown"}, {"tone"}, {"tone"}, {"tone"})),
        ("fsk2_512",          "fsk2",     512, ({"data", "unknown"}, {"data", "unknown"},
                                                {"fsk2", "data", "unknown"}, {"fsk2"})),
        # (a count of levels only from 15 dB: at 12 to 14 dB four levels were often counted as two)
        ("fsk2_1200",         "fsk2",     1200, ({"data", "unknown"}, {"data"}, {"fsk2"}, {"fsk2"})),
        ("fsk2_1200_preamble", "fsk2",    1200, ({"data", "unknown"}, {"data"}, {"fsk2"}, {"fsk2"})),
        ("fsk2_2400",         "fsk2",     2400, ({"data", "unknown"}, {"data"}, {"fsk2"}, {"fsk2"})),
        ("fsk4_4800",         "fsk4",     4800, ({"data", "unknown"}, {"data"}, {"fsk4"}, {"fsk4"})),
        ("fsk4_2400",         "fsk4",     2400, ({"data", "unknown"}, {"data", "unknown"}, {"fsk4"}, {"fsk4"})),
    )

    def test_each_kind_at_each_level(self):
        for kind, name, number, by_level in self.MATRIX:
            for snr, may in zip(LEVELS, by_level):
                for seed in ((1, 2) if snr < 16 else (1,)):
                    m = prof(kind, snr, seed)["mod"]
                    where = (kind, snr, seed, m)
                    self.assertIn(m["kind"], may, where)
                    self.assertIsNone(m["why"], where)
                    # whatever it reads, a number it names is the right one, and only its own kind names one
                    self.assertEqual(m["tone_hz"], number if m["kind"] == "tone" else None, where)
                    self.assertEqual(m["rate_baud"], number if m["kind"] in ("fsk2", "fsk4", "data") else None, where)
                    self.assertEqual(m["levels"], {"fsk2": 2, "fsk4": 4}.get(m["kind"]), where)
                    self.assertGreater(m["stretch_s"], 2.9, where)

    def test_a_level_count_needs_12_db(self):
        for kind in ("fsk2_1200", "fsk4_4800"):
            m = prof(kind, 10.5)["mod"]
            self.assertEqual((m["kind"], m["levels"]), ("data", None), m)       # the rate, "levels unclear"

    def test_plain_carriers_and_voices_give_no_rate_and_no_tone(self):
        for seed in range(30):
            for kind, may in (("carrier", {"none"}), ("fm_voice", {"fm_voice"})):
                m = prof(kind, 12.5, 500 + seed)["mod"]
                self.assertIn(m["kind"], may, (kind, seed, m))
                self.assertEqual((m["rate_baud"], m["tone_hz"], m["levels"]), (None, None, None), (kind, seed, m))

    def test_numbers_measured_on_the_way(self):
        m = prof("carrier", 16)["mod"]
        self.assertLess(m["am_index"], 0.03)
        self.assertLess(m["dev_rms_hz"], 40.0)
        m = prof("am_tone", 16)["mod"]                       # 50 % modulation by one tone: 0.35 rms
        self.assertAlmostEqual(m["am_index"], 0.35, delta=0.04)
        m = prof("fm_audio_tone_1000", 16)["mod"]            # 3 kHz peak: 2.1 kHz rms
        self.assertAlmostEqual(m["dev_rms_hz"], 2121.0, delta=250.0)

    def test_look_alikes_are_never_called_what_they_are_not(self):
        cases = (
            # mains hum on a carrier is no sub-audible tone: at 40 Hz rms the carrier reads unmodulated,
            # at 200 Hz rms (strong, but next to a hum line) it is "modulated, kind not determined"
            ("hum_40", {}, {"none"}),
            ("hum_200", {}, {"unknown"}),
            # one audio tone on FM gives a line at twice its frequency where a symbol rate would be:
            # 600 Hz looks like 1200 baud, 1200 Hz like 2400
            ("fm_audio_tone_600", {}, {"unknown"}),
            ("fm_audio_tone_1200", {}, {"unknown"}),
            ("fm_audio_tone_1000", {}, {"unknown"}),
            ("audio_fsk", {}, {"unknown"}),
            # two carriers closer than 0.5 kHz beat: not a tone, not voice, not AM
            ("two_123_0", {}, {"unknown"}),
            ("two_250.3_0", {}, {"unknown"}),
            ("two_400_6", {}, {"unknown"}),
            # on-off data faster than the 10 ms blocks is not "no modulation" and not FM
            ("carrier", {"key": (3, 3)}, {"unknown", "am"}),
            # a deep slow fade: only its solid part is looked at, and noise is not read as voice
            ("carrier", {"fade": (30, 3.0)}, {"none", "unknown"}),
            # data switched on and off too briefly for a rate
            ("fsk2_1200", {"key": (400, 100)}, {"unknown", "data"}),
        )
        for kind, kw, may in cases:
            for snr in (12.5, 20.0):
                for seed in (1, 2):
                    m = prof(kind, snr, seed, **kw)["mod"]
                    self.assertIn(m["kind"], may, (kind, kw, snr, seed, m))
                    self.assertEqual((m["tone_hz"], m["levels"]), (None, None), (kind, m))

    def test_a_tone_next_to_a_hum_line_needs_more_deviation(self):
        y, noise = look("noise", 0, 4)
        t = np.arange(len(y)) / FS
        amp = math.sqrt(noise * 10 ** 1.6)
        for dev_rms, kind in ((200.0, "unknown"), (330.0, "tone")):       # 100.0 Hz: a standard tone, and 2 x 50 Hz
            ph = dev_rms * math.sqrt(2.0) / 100.0 * -np.cos(2 * np.pi * 100.0 * t)
            m = siglook.profile(y + (amp * np.exp(1j * ph)).astype(np.complex64), FS, noise, HW)["mod"]
            self.assertEqual((m["kind"], m["tone_hz"]), (kind, 100.0 if kind == "tone" else None), (dev_rms, m))

    def test_untested_says_why(self):
        for p, why in ((prof("carrier", 6.0), "too_weak"), (prof("carrier", 16, key=(60, 180)), "too_short"),
                       (prof("carrier", 16, seconds=0.25), "too_short"), (prof("carrier", 20, clip=0.01), "overloaded"),
                       (prof("noise", 0), "not_on")):
            self.assertEqual((p["mod"]["kind"], p["mod"]["why"]), ("untested", why), p["mod"])
            self.assertEqual((p["mod"]["tone_hz"], p["mod"]["rate_baud"], p["mod"]["levels"]), (None, None, None))

    def test_modulation_is_found_on_a_signal_off_the_centre_and_on_a_drifting_one(self):
        self.assertEqual(prof("fm_voice", 16, offset=4000.0)["mod"]["kind"], "fm_voice")
        self.assertEqual(prof("fsk4_4800", 16, offset=-3000.0)["mod"]["kind"], "fsk4")
        self.assertEqual(prof("am_tone", 16, drift=200.0)["mod"]["kind"], "am")
        self.assertEqual(prof("fsk2_1200", 16, drift=300.0)["mod"]["kind"], "fsk2")
        m = prof("fm_voice_tone", 16, drift=100.0)["mod"]
        self.assertEqual((m["kind"], m["tone_hz"]), ("tone", 123.0))


class Keying(unittest.TestCase):
    def test_keying_patterns_come_back(self):
        for on, off, state in ((1000, 500, "keyed"), (500, 500, "keyed"), (200, 100, "keyed"), (50, 950, "bursts"),
                               (30, 30, "bursts"), (60, 180, "bursts")):
            for snr in (9.0, 16.0):
                for seed in (1, 2, 3):
                    p = prof("carrier", snr, seed, key=(on, off))
                    k, where = p["key"], (on, off, snr, seed, p["key"])
                    self.assertEqual((k["state"], k["why"]), (state, None), where)
                    self.assertAlmostEqual(k["on_pct"], 100.0 * on / (on + off), delta=18.0, msg=where)
                    self.assertAlmostEqual(k["runs"], 3000.0 / (on + off), delta=1.01, msg=where)
                    self.assertAlmostEqual(k["longest_on_s"], on / 1000.0, delta=0.021, msg=where)
                    if k["shortest_on_ms"] is not None:     # complete on periods only; each edge is +-10 ms
                        self.assertAlmostEqual(k["shortest_on_ms"], on, delta=21, msg=where)
                    if k["period_s"] is not None:
                        self.assertAlmostEqual(k["period_s"], (on + off) / 1000.0, delta=0.011, msg=where)
        self.assertEqual(prof("carrier", 16, key=(200, 100))["key"]["period_s"], 0.3)
        self.assertEqual(prof("carrier", 12, key=(50, 950))["key"]["period_s"], 1.0)

    def test_a_very_short_strong_burst_still_counts(self):
        k = prof("carrier", 22, key=(5, 495))["key"]
        self.assertEqual((k["state"], k["runs"]), ("bursts", 6), k)
        self.assertLessEqual(k["shortest_on_ms"], 20)

    def test_open_ends_are_marked(self):
        y, noise = look("carrier", 16, 1)
        cut = y.copy()
        cut[:40000] = (cut[:40000] - np.mean(cut[:40000])) * 0.0     # silent for the first 0.8 s
        k = siglook.profile(cut, FS, noise, HW)["key"]
        self.assertEqual((k["state"], k["runs"], k["open_start"], k["open_end"]), ("keyed", 1, False, True), k)
        self.assertIsNone(k["shortest_on_ms"])               # the one on period has no end in the look
        self.assertAlmostEqual(k["longest_on_s"], 2.2, delta=0.02)
        self.assertAlmostEqual(k["longest_off_s"], 0.8, delta=0.02)

    def test_a_weak_steady_carrier_shows_no_keying(self):
        for snr in (2.0, 3.0, 4.0, 5.0, 6.0):
            for seed in range(8):
                p = prof("carrier", snr, 300 + seed)
                self.assertEqual((p["key"]["state"], p["key"]["why"]), ("unclear", "too_weak"), (snr, seed, p["key"]))
                self.assertEqual((p["line"]["ok"], p["mod"]["kind"], p["quality"]), (False, "untested", "C"))

    def test_noise_alone_is_off(self):
        for seed in range(10):
            p = prof("noise", 0, 400 + seed)
            self.assertEqual((p["key"]["state"], p["key"]["why"], p["key"]["runs"], p["key"]["on_pct"]),
                             ("off", "not_on", 0, 0.0), p["key"])
            self.assertEqual((p["snr_on_db"], p["strip"], p["line"]["why"], p["width"]["why"], p["level"]["why"]),
                             (None, [0] * 6, "not_on", "not_on", "not_on"))

    def test_a_slow_fade_is_not_keying(self):
        p = prof("carrier", 6.0, fade=(10, 2.0))             # fades into the noise at a level too weak to count
        self.assertEqual((p["key"]["state"], p["key"]["why"]), ("unclear", "too_weak"), p["key"])
        for snr, fade in ((12.0, (20, 2.0)), (16.0, (25, 1.5)), (20.0, (30, 3.0))):      # strong, fading right out
            p = prof("carrier", snr, fade=fade)
            self.assertEqual((p["key"]["state"], p["key"]["why"]), ("unclear", "unsteady"), (snr, fade, p["key"]))
        p = prof("carrier", 12.5, fade=(10, 2.0))            # a fade that stays over the noise: on, level varied
        self.assertEqual(p["key"]["state"], "on_whole")
        self.assertAlmostEqual(p["level"]["range_db"], 9.7, delta=1.0)

    def test_keying_faster_than_the_blocks_is_not_counted(self):
        for on in (20, 15, 10, 3):                           # 25 to 170 times a second
            for seed in (1, 2, 3):
                k = prof("carrier", 16, seed, key=(on, on))["key"]
                self.assertIn((k["state"], k["why"]), (("unclear", "unsteady"), ("on_whole", None)), (on, seed, k))


class LineAndDrift(unittest.TestCase):
    def test_drift_is_recovered(self):
        for snr in (8.5, 16.0):
            for drift in (0.0, 10.0, 300.0, -300.0, 1000.0):
                p = prof("carrier", snr, 11, drift=drift, offset=137.3)
                ln, where = p["line"], (snr, drift, p["line"])
                self.assertTrue(ln["ok"], where)
                self.assertAlmostEqual(ln["slope_hz_s"], drift, delta=0.5, msg=where)
                self.assertAlmostEqual(ln["hz"], 137.3 + 1.5 * drift, delta=3.0 + 0.02 * abs(drift), msg=where)
                self.assertLess(ln["scatter_hz"], 1.0, where)
                self.assertGreaterEqual(ln["share"], 0.95, where)        # still one line while it moves
                self.assertEqual((ln["pieces"], ln["span_s"], ln["others_hz"], ln["why"]), (12, 2.75, [], None), where)
                # the drift is not mistaken for width or for modulation
                self.assertEqual((p["width"]["hz"], p["width"]["limit"], p["mod"]["kind"]), (100.0, "under", "none"),
                                 (snr, drift, p["width"], p["mod"]))

    def test_the_line_of_a_keyed_carrier(self):
        ln = prof("carrier", 12.5, key=(500, 500), offset=-820.0)["line"]
        self.assertTrue(ln["ok"])
        self.assertGreaterEqual(ln["pieces"], 4)
        self.assertAlmostEqual(ln["hz"], -820.0, delta=1.0)
        self.assertAlmostEqual(ln["slope_hz_s"], 0.0, delta=0.5)

    def test_too_little_of_it_for_a_drift(self):
        ln = prof("carrier", 16, seconds=0.6, offset=55.0)["line"]
        self.assertEqual((ln["ok"], ln["slope_hz_s"], ln["scatter_hz"], ln["why"]), (True, None, None, "too_short"), ln)
        self.assertAlmostEqual(ln["hz"], 55.0, delta=1.0)
        ln = prof("carrier", 16, key=(200, 100))["line"]     # no quarter second in one piece
        self.assertEqual((ln["ok"], ln["hz"], ln["pieces"], ln["why"]), (False, None, 0, "too_short"), ln)

    def test_no_line_without_a_clean_one(self):
        for kind in ("fsk2_1200", "fsk4_4800", "fm_tone_only"):
            ln = prof(kind, 16)["line"]
            self.assertEqual((ln["ok"], ln["hz"], ln["slope_hz_s"], ln["why"]), (False, None, None, "no_single_line"), ln)
            self.assertLess(ln["share"], 0.3)
        ln = prof("carrier", 6.0)["line"]
        self.assertEqual((ln["ok"], ln["hz"], ln["why"]), (False, None, "too_weak"), ln)
        for kind in ("am_voice", "am_tone"):                 # an AM carrier is a clean line
            self.assertTrue(prof(kind, 16)["line"]["ok"], kind)

    def test_other_carriers_are_listed_and_sidebands_are_not(self):
        for weaker in (0, 3, 10, 20):
            for snr in (12.5, 20.0):
                p = prof("two_1400_%d" % weaker, snr)
                self.assertEqual(p["mod"]["kind"], "lines", (weaker, snr, p["mod"]))
                self.assertEqual(len(p["line"]["others_hz"]), 1, p["line"])
                self.assertAlmostEqual(p["line"]["others_hz"][0], 1400.0, delta=4.0)
                self.assertAlmostEqual(p["line"]["hz"], 0.0, delta=1.0)
        # the sidebands of an AM tone, a data preamble and hum are the signal's own lines
        for kind in ("am_tone", "fsk2_1200_preamble", "hum_200", "fm_audio_tone_1000", "fm_voice", "carrier"):
            for snr in (16.0, 30.0):
                self.assertEqual(prof(kind, snr)["line"]["others_hz"], [], (kind, snr))

    def test_three_carriers_are_lines_with_no_single_one(self):
        y, noise = look("noise", 0, 9)
        t = np.arange(len(y)) / FS
        amp = math.sqrt(noise * 10 ** 1.6 / 3.0)
        y = y + (amp * (np.exp(2j * np.pi * 173.0 * t) + np.exp(2j * np.pi * -1209.0 * t + 1j)
                        + np.exp(2j * np.pi * 2590.0 * t + 2j))).astype(np.complex64)
        p = siglook.profile(y, FS, noise, HW)
        self.assertEqual((p["mod"]["kind"], p["line"]["ok"], p["line"]["why"]), ("lines", False, "no_single_line"), p)
        self.assertEqual(len(p["line"]["others_hz"]), 2)


class WidthAndLevel(unittest.TestCase):
    def test_a_plain_carrier_is_under_a_tenth_of_a_khz(self):
        for snr, share in zip(LEVELS, (90, 99, 99, 99)):
            for seed in (1, 2, 3):
                w = prof("carrier", snr, seed)["width"]
                self.assertEqual(w, {"hz": 100.0, "share": share, "limit": "under", "why": None}, (snr, w))

    def test_widths_of_modulated_signals(self):
        for kind, lo, hi in (("am_tone", 1900, 2200), ("fsk2_1200", 9500, 14500), ("fsk4_4800", 5300, 6800),
                             ("fsk4_2400", 3000, 3900), ("fm_voice", 2400, 4600), ("fm_voice_wide", 4500, 7800)):
            for snr in (12.5, 16.0, 25.0):
                w = prof(kind, snr)["width"]
                self.assertTrue(lo <= w["hz"] <= hi, (kind, snr, w))
                self.assertEqual((w["share"], w["limit"], w["why"]), (99, None, None), (kind, snr, w))
        # between 8 and 12 dB only "most of its power within": the 90 % width, never wider than the 99 % one
        w = prof("fsk2_1200", 9.5)["width"]
        self.assertEqual(w["share"], 90)
        self.assertTrue(7500 <= w["hz"] <= 11000, w)

    def test_no_width_under_8_db_or_overloaded_or_too_short(self):
        self.assertEqual(prof("fsk2_1200", 6.0)["width"], {"hz": None, "share": None, "limit": None, "why": "too_weak"})
        self.assertEqual(prof("carrier", 20, clip=0.01)["width"]["why"], "overloaded")
        self.assertEqual(prof("carrier", 12, key=(50, 950))["width"], {"hz": None, "share": None, "limit": None,
                                                                      "why": "too_short"})

    def test_a_signal_that_fills_the_filter_is_at_least_that_wide(self):
        w = prof("fsk2_1200", 16, hw=6000.0)["width"]
        self.assertEqual((w["limit"], w["why"]), ("at_least", "wider_than_filter"), w)
        self.assertGreaterEqual(w["hz"], 0.8 * 2 * 0.9 * 6000.0)

    def test_the_noise_reference_may_be_a_little_off(self):
        for db in (-2.0, -1.0, 1.0):                         # the look measures its own floor for the width
            y, noise = look("carrier", 14, 5)
            p = siglook.profile(y, FS, noise * 10 ** (db / 10.0), HW)
            self.assertEqual((p["width"]["hz"], p["width"]["limit"], p["mod"]["kind"]), (100.0, "under", "none"), (db, p))

    def test_level_range(self):
        for snr in LEVELS:
            lv = prof("carrier", snr)["level"]
            self.assertLessEqual(lv["range_db"], 0.5, (snr, lv))
            self.assertIsNone(lv["why"])
        self.assertAlmostEqual(prof("carrier", 16, fade=(6, 0.7))["level"]["range_db"], 5.6, delta=0.8)
        self.assertLessEqual(prof("carrier", 12, key=(500, 500))["level"]["range_db"], 0.5)      # while on
        self.assertEqual(prof("carrier", 16, seconds=0.8)["level"], {"range_db": None, "why": "too_short"})
        self.assertEqual(prof("carrier", 6.0)["level"], {"range_db": None, "why": "too_weak"})
        self.assertEqual(prof("carrier", 20, clip=0.01)["level"], {"range_db": None, "why": "overloaded"})


class Gates(unittest.TestCase):
    def test_quality(self):
        self.assertEqual(prof("carrier", 12.5)["quality"], "A")
        self.assertEqual(prof("carrier", 9.5)["quality"], "B")
        self.assertEqual(prof("carrier", 6.0)["quality"], "C")
        self.assertEqual(prof("carrier", 16, seconds=1.5)["quality"], "B")       # under 2 s on
        self.assertEqual(prof("carrier", 16, seconds=0.8)["quality"], "C")       # under 1 s on
        self.assertEqual(prof("carrier", 16, clip=0.002)["quality"], "C")

    def test_overload(self):
        ok, over = prof("carrier", 20, clip=0.001), prof("carrier", 20, clip=0.0011)
        self.assertEqual((ok["overloaded"], ok["quality"], ok["mod"]["kind"]), (False, "A", "none"))
        self.assertEqual((over["overloaded"], over["quality"], over["mod"]["kind"], over["mod"]["why"]),
                         (True, "C", "untested", "overloaded"))
        self.assertEqual(over["key"]["state"], "on_whole")   # on and off, and the line, are still measured
        self.assertTrue(over["line"]["ok"])
        self.assertTrue(prof("noise", 0, clip=0.5)["overloaded"])

    def test_clip_fraction(self):
        self.assertEqual(siglook.clip_fraction(bytes([0, 255, 1, 254, 127, 128, 3, 9])), 0.25)
        self.assertEqual(siglook.clip_fraction(np.array([0, 0, 5, 255], dtype=np.uint8)), 0.75)
        self.assertEqual(siglook.clip_fraction(bytearray(b"\x00\x10")), 0.5)
        self.assertEqual(siglook.clip_fraction(memoryview(b"\xff\xff")), 1.0)
        self.assertEqual(siglook.clip_fraction(b""), 0.0)
        self.assertEqual(siglook.clip_fraction(None), 0.0)
        self.assertIs(type(siglook.clip_fraction(b"\x00\x01")), float)
        # a capture as a dongle delivers it: a strong carrier clipped at the converter's rails
        t = np.arange(20000)
        iq = np.clip(np.round(127.4 + 160.0 * np.cos(0.3 * t)), 0, 255).astype(np.uint8)
        self.assertGreater(siglook.clip_fraction(iq.tobytes()), 0.2)
        self.assertEqual(siglook.clip_fraction(np.clip(np.round(127.4 + 60.0 * np.cos(0.3 * t)), 0, 255).astype(np.uint8)), 0.0)

    def test_the_strip_is_never_finer_than_half_a_second(self):
        p = prof("carrier", 16, key=(1000, 500))
        self.assertEqual((len(p["strip"]), p["strip_slot_s"]), (6, 0.5))
        self.assertTrue(set(p["strip"]) <= {0, 0.5, 1} and 1 in p["strip"] and 0.5 in p["strip"], p["strip"])
        p = prof("carrier", 16, key=(30, 30))                # 17 on periods a second: the strip shows none of them
        self.assertEqual(p["strip"], [0.5] * 6)
        y, noise = look("carrier", 16, 1)                    # one 60 ms burst at 1.1 s: its slot is not "off"
        gate = np.zeros(len(y), dtype=np.complex64)
        gate[55000:58000] = 1.0
        self.assertEqual(siglook.profile(y * gate, FS, noise, HW)["strip"], [0, 0, 0.5, 0, 0, 0])
        for seconds, slots, slot in ((0.2, 1, 0.5), (1.0, 2, 0.5), (10.0, 20, 0.5), (40.0, 40, 1.0)):
            p = prof("carrier", 14, seconds=seconds, mode="light")
            self.assertEqual((len(p["strip"]), p["strip_slot_s"], set(p["strip"])), (slots, slot, {1}), seconds)
            self.assertGreaterEqual(p["strip_slot_s"], 0.5)
            self.assertLessEqual(len(p["strip"]), 60)


class NeverRaises(unittest.TestCase):
    def test_input_that_cannot_be_measured(self):
        rng = np.random.default_rng(1)
        noise = (rng.standard_normal(150000) + 1j * rng.standard_normal(150000)).astype(np.complex64)
        cases = {
            "empty": (np.zeros(0, np.complex64), FS, 1e-4, HW),
            "a few samples": (np.zeros(7, np.complex64), FS, 1e-4, HW),
            "all zeros": (np.zeros(150000, np.complex64), FS, 1e-4, HW),
            "noise reference 0": (noise, FS, 0.0, HW),
            "noise reference negative": (noise, FS, -1.0, HW),
            "noise reference not a number": (noise, FS, float("nan"), HW),
            "sample rate 0": (noise, 0, 1.0, HW),
            "filter width 0": (noise, FS, 1.0, 0),
            "real samples": (rng.standard_normal(150000), FS, 1.0, HW),
            "absurdly strong": (1e30 * noise, FS, 1e-4, HW),
            "not-a-number samples": (np.full(150000, np.nan + 0j, np.complex64), FS, 1e-4, HW),
            "some not-a-number samples": (np.where(np.arange(150000) % 1000 == 0, np.nan, 1.0).astype(np.complex64), FS, 1e-4, HW),
            "infinite samples": (np.where(np.arange(150000) % 777 == 0, np.inf, 0.01).astype(np.complex64), FS, 1e-4, HW),
            "a list": ([1 + 1j] * 3000, FS, 1e-4, HW),
            "None": (None, FS, 1e-4, HW),
            "words": ("abc", "x", "y", "z"),
            "a low sample rate": (noise[:5000], 500.0, 0.01, 100.0),
            "8 kS/s": (noise[:24000] * 0.01 + 1.0, 8000.0, 2e-4, 3000.0),
            "integers": (rng.integers(-100, 100, 150000) + 1j * rng.integers(-100, 100, 150000), FS, 1.0, HW),
            "two dimensions": (np.ones((300, 500), np.complex64), FS, 1e-4, HW),
            "a constant": (np.ones(150000, np.complex64), FS, 1e-9, HW),
            "a filter wider than the sample rate": (noise * 0.01 + 0.2, FS, 2e-4, 40000.0),
        }
        for name, args in cases.items():
            for mode in ("full", "light"):
                p = siglook.profile(*args, mode=mode)        # (an exception here fails the test)
                self.assertTrue(_plain(p), (name, p))
                json.dumps(p, allow_nan=False)
                self.assertEqual("mod" in p, mode == "full", name)
                self.assertIn(p["key"]["state"], ("on_whole", "keyed", "bursts", "unclear", "off"), name)
                if p["key"]["state"] in ("off", "unclear"):
                    self.assertIn(p["key"]["why"], ("too_weak", "too_short", "overloaded", "unsteady", "not_on"), name)
                if mode == "full":
                    self.assertEqual(p["mod"]["kind"] == "untested", p["mod"]["why"] is not None, (name, p["mod"]))
        for name in ("empty", "a few samples", "sample rate 0", "None", "words", "a low sample rate"):
            p = siglook.profile(*cases[name])
            self.assertEqual((p["key"]["state"], p["key"]["why"], p["mod"]["kind"]), ("unclear", "too_short", "untested"), name)
        for name in ("noise reference 0", "noise reference negative", "noise reference not a number", "filter width 0"):
            p = siglook.profile(*cases[name])
            self.assertEqual((p["key"]["state"], p["key"]["why"]), ("unclear", "too_weak"), name)
        for name in ("all zeros", "not-a-number samples", "real samples"):
            self.assertEqual(siglook.profile(*cases[name])["key"]["state"], "off", name)
        self.assertEqual(siglook.profile(*cases["absurdly strong"])["key"]["why"], "overloaded")

    def test_unknown_mode_is_the_full_profile(self):
        y, noise = look("carrier", 14, 1)
        self.assertIn("mod", siglook.profile(y, FS, noise, HW, mode="everything"))


class Budget(unittest.TestCase):
    def test_a_three_second_look_is_quick(self):
        """Budget on one core: 250 ms full, 80 ms light (measured here: about 50 and 10). The bound is four times
        that, for a slow or busy machine."""
        for kind in ("carrier", "fsk2_1200", "fm_voice_tone"):
            y, noise = look(kind, 16, 9)
            for mode, bound in (("full", 1.0), ("light", 0.32)):
                best = 9.0
                for _ in range(3):
                    t0 = time.perf_counter()
                    siglook.profile(y, FS, noise, HW, mode=mode)
                    best = min(best, time.perf_counter() - t0)
                self.assertLess(best, bound, (kind, mode))


def _chan(make, snr_db, seed=1, seconds=3.0, hw=HW):
    """(channel samples, noise power) of make(t, rng), a complex signal of unit carrier power, snr_db over the noise."""
    rng = np.random.default_rng(seed)
    h = _fir(hw, 27)
    n = int(FS * seconds) + len(h) - 1
    t = np.arange(n) / FS
    noise = 2 * SIGMA ** 2 * float(np.sum(h ** 2))
    z = math.sqrt(noise * 10 ** (snr_db / 10.0)) * make(t, rng) + SIGMA * (rng.standard_normal(n)
                                                                         + 1j * rng.standard_normal(n))
    return np.convolve(z, h, mode="valid").astype(np.complex64), noise


def _clicks(rate, inr_db, snr_db, width=2):
    """A bare carrier with impulses `rate` times a second (power-line noise), their mean power inr_db over the
    channel's noise."""
    def make(t, rng):
        z = np.exp(2j * np.pi * 37.0 * t)
        per = FS / rate
        a = math.sqrt(10 ** ((inr_db - snr_db) / 10.0) * per / width)
        k = rng.uniform(0, per)
        while k < len(t) - width:
            i = int(k + rng.normal(0, 0.02 * per))
            if 0 <= i < len(t) - width:
                z[i:i + width] += a * np.exp(1j * rng.uniform(0, 6.28))
            k += per
        return z
    return make


def _fm(audio):
    return lambda t, rng: np.exp(2j * np.pi * np.cumsum(audio(t)) / FS)


class SecondLook(unittest.TestCase):
    """Edge cases of the look: folded signals, FSK levels, clicks and hum."""

    def test_a_filter_wider_than_the_samples_hold_gives_no_line(self):
        # two carriers 60 kHz apart round the frequency picked reach the 50 kS/s look folded to +-20 kHz: a line
        # read there is at a frequency where nothing transmits
        y, noise = look("two_-40000_0", 20, 1, offset=20000.0, hw=24000.0)
        wide = siglook.profile(y, FS, noise, 30000.0)
        self.assertTrue(wide["folded"])
        self.assertEqual((wide["line"]["ok"], wide["line"]["hz"], wide["line"]["others_hz"], wide["line"]["why"]),
                         (False, None, [], "no_single_line"))
        self.assertNotIn(wide["mod"]["kind"], ("lines", "none"))
        one = prof("carrier", 20)                       # a look that holds what its filter passes is not folded
        self.assertEqual((one["folded"], one["line"]["ok"]), (False, True))

    def test_a_very_narrow_filter_is_not_an_error(self):
        # (the audio band's upper edge fell under its lower edge: a square root of a negative number)
        y, noise = look("carrier", 8.5, 1, seconds=0.5, hw=300.0)
        p = siglook._profile(y, FS, noise, 300.0, True, 0.0)
        self.assertIn(p["mod"]["kind"], ("untested", "none", "unknown"))

    def test_clicks_on_a_bare_carrier_are_not_called_am_or_voice(self):
        for rate, inr, snr in ((60, 10, 12.5), (60, 6, 12.5), (100, 10, 25.0), (120, 10, 30.0), (50, 10, 16.0)):
            for seed in (1, 2, 3):
                y, noise = _chan(_clicks(rate, inr, snr), snr, seed)
                m = siglook.profile(y, FS, noise, HW)["mod"]
                self.assertIn(m["kind"], ("unknown", "none"), (rate, inr, snr, seed, m))

    def test_mains_hum_is_not_voice_and_a_tone_on_a_mains_multiple_says_so(self):
        saw = lambda f, rms: _fm(lambda t: rms * math.sqrt(3.0) * (2.0 * ((t * f) % 1.0) - 1.0))
        sine = lambda f, rms: _fm(lambda t: rms * math.sqrt(2.0) * np.sin(2 * np.pi * f * t))
        for f, base in ((100.0, 50.0), (120.0, 60.0)):
            y, noise = _chan(saw(f, 300.0), 25.0)       # ripple with its harmonics: read as voice before
            m = siglook.profile(y, FS, noise, HW)["mod"]
            self.assertEqual((m["kind"], m["hum_hz"]), ("unknown", base), m)
        y, noise = _chan(sine(100.0, 600.0), 20.0)      # 100.0 Hz is a standard access tone and twice 50 Hz
        m = siglook.profile(y, FS, noise, HW)["mod"]
        self.assertEqual((m["kind"], m["tone_hz"], m["hum_hz"]), ("tone", 100.0, 50.0), m)
        m = prof("fm_tone_only", 20)["mod"]             # 123.0 Hz is no mains multiple
        self.assertEqual((m["kind"], m["tone_hz"], m["hum_hz"]), ("tone", 123.0, None), m)
        for kind in ("fm_voice", "fm_voice_wide"):      # speech stays speech
            for snr in (12.5, 16.0, 25.0):
                m = prof(kind, snr)["mod"]
                self.assertEqual((m["kind"], m["hum_hz"]), ("fm_voice", None), (kind, snr, m))

    def test_levels_are_not_counted_at_9600_symbols_a_second(self):
        make = _fm(lambda t: 800.0 * _symbols(len(t), 9600, np.random.default_rng(5), levels=(-3, -1, 1, 3), bt=0.6))
        for snr in (16.0, 25.0):
            y, noise = _chan(make, snr)
            m = siglook.profile(y, FS, noise, HW)["mod"]
            self.assertIsNone(m["levels"], m)
            self.assertIn(m["kind"], ("data", "unknown"), m)




class DemodulatorTrial(unittest.TestCase):
    """The trial rows: each demodulator's numbers side by side, no audio."""

    def test_trial_rows_follow_the_kind(self):
        from arislib import siglook
        t = siglook._trial("am", 0.4, 0.4, True, 0.2, 5.0, None, None, None)
        self.assertEqual({r["mode"]: r["fit"] for r in t}["AM"], "fits")
        self.assertEqual({r["mode"]: r["fit"] for r in t}["Narrow FM"], "no")
        t = siglook._trial("fm_voice", 0.02, 0.02, False, 9.0, 1800.0, None, None, None)
        self.assertEqual({r["mode"]: r["fit"] for r in t}["Narrow FM"], "fits")
        t = siglook._trial("none", 0.0, 0.0, False, 0.1, 0.0, None, None, None)
        self.assertIn("Bare carrier", [r["mode"] for r in t])

    def test_report_sentence_says_numbers_only(self):
        from arislib import sigreport
        s = sigreport._tried({"trial": [{"mode": "AM", "fit": "no", "value": 1.0, "unit": "% depth"},
                                         {"mode": "Narrow FM", "fit": "fits", "value": 1800.0, "unit": "Hz rms deviation"}]})
        self.assertIn("numbers only", s)
        self.assertIn("Narrow FM: fits (1800 Hz rms deviation)", s)
        self.assertEqual(sigreport._tried({}), "")


if __name__ == "__main__":
    unittest.main()
