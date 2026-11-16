"""The Decode card's rules and words (arislib.sigreport): the clock arithmetic, the ladder from facts to a headline
and a confidence, the one next step, the history statuses, whose drift it is, the bars, and the wording rules over a
grid of fact combinations.
python3 -m unittest test_sigreport   (pure rules: no radios, no engine, no numpy)"""
import copy
import json
import os
import re
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arislib import sigreport as R  # noqa: E402

NOW = R.EXAMPLE_NOW
STEADY = "• ARIS verdict (heuristic): Not identified: no decoder recognised it. Measured: Steady unmodulated carrier"


def day(month, d, hour=10) -> float:
    return time.mktime((2026, month, d, hour, 0, 0, 0, 0, -1))


def facts(case="1a", **changes) -> dict:
    f = copy.deepcopy(R.example_facts(case))
    f.update(changes)
    return f


def build(f) -> dict:
    """The report as build() gives it, which must be the rules' own answer and never its safety net's (the rules
    raise here if they break)."""
    strict = R._build(copy.deepcopy(f))
    assert R.build(f) == strict
    return strict


def profiles(f) -> list:
    return [d["profile"] for d in f["dongles"] if d.get("profile")]


def set_lines(f, *mhz):
    for d, m in zip(f["dongles"], mhz):
        d["profile"]["line_mhz"] = m
        for x in d["looks"]:
            x["line_mhz"] = m


def set_mod(f, kind, **more):
    for p in profiles(f):
        p["mod"].update(dict(more, kind=kind))


def set_key(f, state, **more):
    for p in profiles(f):
        p["key"].update(dict(more, state=state))


def set_slope(f, *hz_s):
    """A drift inside the first look and over the watchers' later looks alike."""
    for d, s in zip(f["dongles"], hz_s):
        d["profile"]["line"].update({"slope_hz_s": s, "span_s": 2.75})
        for x in d["looks"]:
            x["line_mhz"] = round(d["profile"]["line_mhz"] + s * x["t"] * 1e-6, 6)


def no_watch(f):
    for d in f["dongles"]:
        d["looks"], d["role"] = [], "look"
    return f


def saved(days, mhz=172.8231, carrier=None, head=STEADY) -> list:
    return [{"ts": day(m, d), "mhz": mhz, "carrier_mhz": carrier, "head": head, "confirmed": False, "doubtful": False}
            for m, d in days]


def fm_facts(**fm) -> dict:
    f = facts("6", evidence="signal_present", verdict="FM broadcast radio station on 98.3 MHz", asked_mhz=98.3)
    f["fm"].update(dict({"name": None, "channel_mhz": 98.3}, **fm))
    f["history"]["fm_name"] = None
    return f


class Clock(unittest.TestCase):
    def test_own_clock_arithmetic(self):
        no = R.own_clock(172.8231, 0)
        self.assertEqual((no["result"], no["multiple"], no["nominal_mhz"], no["tolerance_hz"]), ("no", 6, 172.8, 309))
        self.assertAlmostEqual(no["offset_khz"], 23.1, places=2)
        self.assertEqual(R.own_clock(172.8, 0)["result"], "match")
        self.assertEqual(R.own_clock(172.79896, -6)["result"], "match")      # 6 x 28.8 x (1 - 6 ppm) = 172.7989632
        self.assertEqual(R.own_clock(172.8005, 0)["result"], "near")         # 500 Hz off: over 309 Hz, within 100 ppm
        self.assertEqual(R.own_clock(28.8, 0), {"result": "match", "multiple": 1, "nominal_mhz": 28.8,
                                                "offset_khz": 0.0, "tolerance_hz": 93})
        # each dongle against its own correction: the other dongle's line is only "near"
        self.assertEqual(R.own_clock(172.8, -6)["result"], "near")
        self.assertEqual(R.own_clock(172.79896, 0)["result"], "near")
        self.assertEqual(R.own_clock(921.618432, 20)["result"], "match")     # 32 x 28.8 at 20 ppm
        self.assertEqual(R.own_clock(921.6, 20)["result"], "near")
        self.assertEqual(R.own_clock(172.8 + 0.0173, 0)["result"], "no")     # just past 100 ppm
        self.assertEqual(R.own_clock(14.4, 0)["multiple"], 1)                # never a multiple of zero
        for junk in (None, "x", -5, float("nan"), True):
            self.assertEqual(R.own_clock(junk, 0)["result"], "no")
            self.assertIsNone(R.own_clock(junk, 0)["multiple"])
        self.assertEqual(R.own_clock(172.8, "x")["result"], "match")

    def test_each_dongle_is_tested_against_its_own_correction(self):
        sp = R.spur_check(facts("1b"))
        self.assertEqual(sp["own_clock"]["result"], "match")
        self.assertEqual(sp["own_clock"]["tight"], ["Dongle 1", "Dongle 2", "Dongle 3"])
        self.assertEqual((sp["own_clock"]["multiple"], sp["own_clock"]["nominal_mhz"]), (6, 172.8))
        sp = R.spur_check(facts("1a"))
        self.assertEqual((sp["own_clock"]["result"], sp["own_clock"]["tight"]), ("no", []))
        self.assertAlmostEqual(sp["own_clock"]["offset_khz"], 23.1, places=1)
        # a real carrier on 172.800 reads the same on every well corrected dongle: only the 0 ppm dongle "matches"
        f = facts("1a")
        set_lines(f, 172.8, 172.8, 172.8)
        sp = R.spur_check(f)
        self.assertEqual((sp["own_clock"]["result"], sp["own_clock"]["tight"]), ("near", ["Dongle 1"]))
        # a receiver that is no RTL-SDR, or a look without a single clear line, is not tested
        f = facts("1b")
        f["dongles"][0]["rtl"] = False
        f["dongles"][1]["serial"], f["dongles"][2]["profile"]["line"]["ok"] = "soapy-hackrf-1", False
        del f["dongles"][1]["rtl"]
        self.assertEqual(R.spur_check(f)["own_clock"]["result"], "not_tested")

    def test_common_equipment_clocks_and_their_odds(self):
        f = facts("1a", asked_mhz=175.0)
        set_lines(f, 175.00048, 175.0005, 175.00052)
        r = build(f)
        eq = r["spur"]["equipment_clock"]
        self.assertEqual((eq["clock_mhz"], eq["multiple"]), (25.0, 7))
        self.assertAlmostEqual(eq["offset_ppm"], 2.9, places=1)
        self.assertGreater(eq["one_in"], 50)
        line = [x for x in r["checks"] if "equipment clock" in x][0]
        self.assertIn("7 x 25 MHz", line)
        self.assertIn(f"About 1 unrelated carrier in {eq['one_in']}", line)
        self.assertIn("a pattern, not a finding", line)
        self.assertEqual(r["category"], "steady_unknown")                    # a pattern never names the source
        self.assertEqual(r["confidence"], "unknown")
        # the lowest multiple is named; 14.318 is the colour-burst crystal (14.31818 MHz)
        self.assertEqual(R._equipment_clock(200.0)["multiple"], 4)
        self.assertEqual((R._equipment_clock(171.81816)["clock_mhz"], R._equipment_clock(171.81816)["multiple"]),
                         (14.318, 12))
        self.assertIsNone(R._equipment_clock(172.8231))
        self.assertIsNone(R._equipment_clock(175.0 * (1 + 70e-6)))           # past 60 ppm
        # the odds are the share of nearby spectrum the multiples cover
        self.assertTrue(20 <= R._equip_one_in(172.8) <= 400, R._equip_one_in(172.8))
        self.assertIn("No match with 13 common equipment clocks.", build(facts("1a"))["checks"])
        # steady plain carriers only: nothing is said about a keyed or a modulated signal
        for change in (lambda f: set_key(f, "keyed"), lambda f: set_mod(f, "fm_voice")):
            f = facts("1a", asked_mhz=175.0)
            set_lines(f, 175.00048, 175.0005, 175.00052)
            change(f)
            r = build(f)
            self.assertIsNone(r["spur"]["equipment_clock"])
            self.assertFalse([x for x in r["checks"] if "equipment clock" in x], r["checks"])

    def test_harmonic_of_a_carrier_measured_earlier(self):
        f = facts("1a", carriers=[57.6077, 99.9, None, "x"])
        r = build(f)
        self.assertEqual(r["spur"]["harmonic_of"], {"mhz": 57.6077, "times": 3})
        line = [x for x in r["checks"] if "harmonic" in x][0]
        self.assertIn("3 x 57.6077 MHz", line)
        self.assertIn("A pattern, not a finding.", line)
        f = facts("1a")                                   # ... or from a recent check the history carries
        f["history"]["recent"] = [{"ts": NOW - 3600, "mhz": 86.41, "carrier_mhz": 86.4116, "heard": True}]
        self.assertEqual(build(f)["spur"]["harmonic_of"], {"mhz": 86.4116, "times": 2})
        f = facts("1a", carriers=[57.6157])               # 3 x 57.6157 = 172.8471: 24 kHz off
        self.assertIsNone(build(f)["spur"]["harmonic_of"])
        self.assertFalse([x for x in build(f)["checks"] if "harmonic" in x])
        self.assertIsNone(build(facts("1a"))["spur"]["harmonic_of"])

    def test_zero_frequency_spike(self):
        f = facts("1a")
        set_lines(f, 172.78311, 172.78308, 172.7835)      # the look tunes 40 kHz below the frequency picked
        r = build(f)
        self.assertEqual(r["spur"]["dc_spike"], ["Dongle 1", "Dongle 2"])    # Dongle 3 is 400 Hz off the centre
        line = [x for x in r["checks"] if "tuning centre" in x][0]
        self.assertTrue(line.startswith("Dongle 1 and Dongle 2 found it exactly on the look's own tuning centre"))
        self.assertIn("Decode it again at 172.783 MHz", line)
        self.assertEqual(build(facts("1a"))["spur"]["dc_spike"], [])
        f = facts("1a", tune_offset_hz=None)
        set_lines(f, 172.78311, 172.78308, 172.7835)
        self.assertEqual(build(f)["spur"]["dc_spike"], [])                   # not known where the look was tuned

    def test_same_signal_tolerance(self):
        for mhz, tol in ((10.0, 500), (30.0, 600), (172.8231, 3456), (915.0, 18300)):
            f = facts("1a", asked_mhz=mhz)
            set_lines(f, mhz, mhz + 0.0001, mhz + 0.0002)
            sp = R.spur_check(f)
            self.assertEqual((sp["tolerance_hz"], sp["same_signal"], sp["spread_hz"]), (tol, "yes", 200), mhz)
        f = facts("1a", asked_mhz=30.0)
        set_lines(f, 30.0, 30.0004, 30.0009)
        sp = R.spur_check(f)
        self.assertEqual((sp["same_signal"], sp["spread_hz"]), ("no", 900))
        r = build(f)
        self.assertIn("The dongles read different frequencies, 0.9 kHz apart: they may not be hearing the same signal.",
                      r["checks"])
        f = facts("1a")
        f["dongles"][0]["heard"] = f["dongles"][2]["heard"] = False
        r = build(f)
        self.assertEqual(r["spur"]["same_signal"], "one")
        self.assertIn("Only Dongle 2 heard it, so its frequency could not be compared with another dongle's.", r["checks"])
        # a look with no single clear line: its coarser reading is compared at 3 kHz, and the words say so
        f = facts("1a")
        f["dongles"][0]["profile"]["line"]["ok"] = False
        sp = R.spur_check(f)
        self.assertEqual((sp["same_signal"], sp["same_rough"], sp["tolerance_hz"]), ("yes", True, 3000))
        r = build(f)
        self.assertTrue([x for x in r["checks"] if x.endswith("A rough comparison: Dongle 1 had no clear line to "
                                                              "read.")], r["checks"])
        self.assertNotIn("whether it reaches the dongles through the antennas or the wiring", r["could_not_tell"])

    def test_same_frequency_says_one_signal_and_never_which_path(self):
        r = build(facts("1a"))
        self.assertIn("All 3 dongles read the same frequency within 0.2 kHz: one signal reaches all of them. Whether "
                      "through the antennas or the wiring is not known from this.", r["checks"])
        self.assertIn("whether it reaches the dongles through the antennas or the wiring", r["could_not_tell"])
        for c in R.EXAMPLE_CASES:
            for s in _words(build(facts(c))):
                self.assertNotIn("over-the-air", s)
                self.assertNotIn("real signal,", s)


class Drift(unittest.TestCase):
    F = 172.8231

    def dongles(self, *slopes, looks=False):
        f = facts("1a") if looks else no_watch(facts("1a"))
        f["dongles"] = f["dongles"][:len(slopes)]
        set_slope(f, *slopes)
        return f["dongles"]

    def test_whose_drift_it_is(self):
        d = R.compare_drift(self.dongles(0.4, -0.9, 1.5), self.F)            # every dongle within 2 Hz per second
        self.assertEqual((d["source"], d["dongles"], d["odd"]), ("none", 3, None))
        self.assertEqual((d["hz"], d["over_s"]), (4, 2.8))                   # the most any dongle showed
        d = R.compare_drift(self.dongles(300.0, 310.0, 290.0), self.F)       # the same movement on all three
        self.assertEqual((d["source"], d["dongles"]), ("signal", 3))
        self.assertEqual(d["hz"], 825)
        self.assertAlmostEqual(d["ppm_s"], 1.736, places=2)
        d = R.compare_drift(self.dongles(0.5, 40.0, -0.5), self.F)           # one differs: that dongle's clock
        self.assertEqual((d["source"], d["odd"]), ("dongle", "Dongle 2"))
        d = R.compare_drift(self.dongles(0.5, 40.0), self.F)                 # two that differ: nobody can be named
        self.assertEqual((d["source"], d["odd"]), ("dongle", None))
        d = R.compare_drift(self.dongles(12.0), self.F)                      # one dongle, under 0.1 ppm per second
        self.assertEqual((d["source"], d["dongles"]), ("unknown", 1))
        d = R.compare_drift(self.dongles(40.0), self.F)                      # one dongle, well above it
        self.assertEqual((d["source"], d["dongles"]), ("signal", 1))
        f = no_watch(facts("1a"))
        for p in profiles(f):
            p["line"].update({"ok": False, "why": "no_single_line"})
        d = R.compare_drift(f["dongles"], self.F)
        self.assertEqual((d["source"], d["dongles"], d["hz"]), ("unknown", 0, None))
        self.assertEqual(R.compare_drift(None, None)["dongles"], 0)
        self.assertEqual(R.compare_drift([{"dongle": "Dongle 1", "heard": False}], 100.0)["dongles"], 0)

    def test_the_floor_scales_with_frequency(self):
        # 0.01 ppm per second: 9 Hz per second at 915 MHz is still "held", at 172.8 MHz it is movement
        f = no_watch(facts("1a", asked_mhz=915.0))
        set_lines(f, 915.0, 915.0, 915.0)
        set_slope(f, 8.0, 8.5, 7.5)
        self.assertEqual(R.compare_drift(f["dongles"], 915.0)["source"], "none")
        self.assertEqual(R.compare_drift(self.dongles(8.0, 8.5, 7.5), self.F)["source"], "signal")

    def test_the_watch_is_used_when_two_dongles_have_one(self):
        d = R.compare_drift(self.dongles(0.2, 0.3, -0.1, looks=True), self.F)
        self.assertEqual((d["source"], d["dongles"], d["names"]), ("none", 2, ["Dongle 1", "Dongle 3"]))
        self.assertGreater(d["over_s"], 20)
        d = R.compare_drift(self.dongles(300.0, 300.0, 300.0, looks=True), self.F)
        self.assertEqual((d["source"], d["dongles"]), ("signal", 2))
        self.assertGreater(d["hz"], 7000)                                    # 300 Hz per second over the watch

    def test_drift_wording(self):
        r = build(facts("1a"))                                               # 2 watchers, 27 s, a few Hz
        line = [x for x in r["behaviour"] if x.startswith("Held its frequency")][0]
        self.assertEqual(line, "Held its frequency: moved less than 0.1 kHz in 27 s, on Dongle 1 and Dongle 3 alike. "
                               "A crystal-controlled source: that fits a licensed transmitter and an equipment clock "
                               "equally.")
        r = build(no_watch(facts("1a")))                                     # the 3 s look only: no crystal claim
        line = [x for x in r["behaviour"] if x.startswith("Held its frequency")][0]
        self.assertEqual(line, "Held its frequency: moved less than 0.1 kHz in 2.8 s, the same on all 3 dongles.")
        self.assertEqual(r["behaviour"][1], "Only the 3 s look was watched: the other 30 s went to the decoders.")
        self.assertIn("what it did outside the 3 s look", r["could_not_tell"])
        for c in R.EXAMPLE_CASES:                                            # the word never comes without its rider
            for s in _words(build(facts(c))):
                if "crystal-controlled source" in s:
                    self.assertIn("that fits a licensed transmitter and an equipment clock equally", s)
        f = no_watch(facts("1a"))
        f["dongles"] = f["dongles"][1:2]
        f["dongles"][0]["profile"]["line"]["slope_hz_s"] = 9.0
        r = build(f)
        self.assertIn("Held within 0.1 kHz over 2.8 s on Dongle 2. One dongle only, so its own clock is part of this.",
                      r["behaviour"])
        f = no_watch(facts("1a"))
        set_slope(f, 6.0, 6.5, 5.5)                                          # small and common: maybe the dongles
        r = build(f)
        self.assertTrue([x for x in r["behaviour"] if x.endswith("That little can also be the dongles warming up "
                                                                 "together.")], r["behaviour"])
        self.assertEqual(r["category"], "steady_unknown")
        f = no_watch(facts("1a"))
        set_slope(f, 0.5, 40.0, -0.5)
        r = build(f)
        self.assertIn("The dongles' readings moved apart by 0.1 kHz in 2.8 s: that is their own clocks, Dongle 2's "
                      "above all. The signal moved less than that.", r["behaviour"])
        r = build(facts("8"))                                                # a keyed carrier that moves
        line = [x for x in r["behaviour"] if x.startswith("Its frequency moved")][0]
        self.assertEqual(line, "Its frequency moved 0.8 kHz in 2.5 s, the same on both dongles, so the movement is the "
                               "signal's own. Keyed transmitters often slide as they switch on, so this does not say "
                               "what kind it is.")
        f = no_watch(facts("1a"))
        for p in profiles(f):
            p["line"].update({"ok": False, "why": "too_weak"})
        r = build(f)
        self.assertIn("whether its frequency moved: the signal was too weak", r["could_not_tell"])
        self.assertFalse([x for x in r["behaviour"] if "frequency" in x])


class History(unittest.TestCase):
    def raw(self, **more) -> dict:
        raw = {"saved": [], "list_from": day(2, 2), "list_trimmed": False, "recent": [], "baselines": [],
               "baseline_covered": False, "last_capture": None, "fm_name": None, "devices_here": None}
        raw.update(more)
        return raw

    def summary(self, f=172.8231, kind="audio", carrier=None, **more) -> dict:
        return R.history_summary(self.raw(**more), f, kind, NOW, carrier)

    def test_the_four_statuses(self):
        h = self.summary()
        self.assertEqual((h["status"], h["saved_checks"], h["days"], h["first"], h["last"]), ("none", 0, 0, None, None))
        self.assertEqual(self.summary(list_from=None)["status"], "young")              # nothing kept yet
        self.assertEqual(self.summary(list_from=NOW - 2 * 86400)["status"], "young")   # too short a list to say
        self.assertEqual(self.summary(list_from=NOW - 3 * 86400)["status"], "none")
        h = self.summary(saved=saved([(3, 12)]))
        self.assertEqual((h["status"], h["saved_checks"], h["days"], h["first"]), ("seen", 1, 1, "2026-03-12"))
        h = self.summary(saved=saved([(3, 9), (3, 10), (3, 12), (3, 13)]))         # 4 days, but within 4 days
        self.assertEqual((h["status"], h["days"], h["span_days"]), ("seen", 4, 4))
        h = self.summary(saved=saved([(3, 6), (3, 13)]))                             # a week apart, but 2 days
        self.assertEqual(h["status"], "seen")
        h = self.summary(saved=saved([(3, 6), (3, 10), (3, 13), (3, 13)]))
        self.assertEqual((h["status"], h["saved_checks"], h["days"], h["span_days"]), ("regular", 4, 3, 7))
        old = [{"dongle": "Dongle 2", "serial": "00026410", "date": day(3, 3), "state": "present"}]
        h = self.summary(saved=saved([(3, 12), (3, 13)]), baselines=old, baseline_covered=True)
        self.assertEqual(h["status"], "regular")                                       # a week-old baseline plus 2 days
        young = [dict(old[0], date=day(3, 10))]
        self.assertEqual(self.summary(saved=saved([(3, 12), (3, 13)]), baselines=young)["status"], "seen")
        part = [dict(old[0], state="part")]
        self.assertEqual(self.summary(saved=saved([(3, 12), (3, 13)]), baselines=part)["status"], "seen")
        self.assertEqual(self.summary(baselines=old)["status"], "seen")                # a baseline that had it
        self.assertEqual(self.summary(baselines=[dict(old[0], state="absent")])["status"], "none")
        self.assertEqual(self.summary(baselines=[dict(old[0], state="unclear")])["status"], "none")
        heard = [{"ts": NOW - 9 * 3600, "mhz": 172.8231, "carrier_mhz": None, "heard": True}]
        h = self.summary(recent=heard)
        self.assertEqual((h["status"], h["recent"]), ("seen", {"checks": 0, "heard": 0, "hours": 6, "watched_s": 0}))
        quiet = [dict(heard[0], heard=False, ts=NOW - 3600)]
        h = self.summary(recent=quiet)
        self.assertEqual((h["status"], h["recent"]), ("none", {"checks": 1, "heard": 0, "hours": 6, "watched_s": 0}))

    def test_what_counts_as_the_same_frequency(self):
        self.assertEqual(self.summary()["window_khz"], 6.0)
        self.assertEqual(self.summary(f=433.92, kind="other")["window_khz"], 7.2)
        self.assertEqual(self.summary(f=915.0, kind="other")["window_khz"], 9.6)
        self.assertEqual(self.summary(f=433.92, kind="sensor")["window_khz"], 60.0)
        self.assertEqual(self.summary(f=97.1, kind="wide")["window_khz"], 60.0)
        rows = saved([(3, 12)], mhz=172.8231 + 0.0059) + saved([(3, 11)], mhz=172.8231 - 0.0061) \
            + saved([(3, 10)], mhz=172.8231 + 0.024) + saved([(3, 9)], mhz=172.8231 + 0.04)
        h = self.summary(saved=rows)
        self.assertEqual((h["saved_checks"], h["next_to"], h["first"]), (1, 2, "2026-03-12"))
        # both carriers measured: 3 kHz + 10 ppm (4.7 kHz at 172.8 MHz), whatever frequency was picked then
        rows = saved([(3, 12)], carrier=172.8231 + 0.0045) + saved([(3, 11)], carrier=172.8231 + 0.0050) \
            + saved([(3, 10)], mhz=172.9, carrier=172.82312)
        h = self.summary(saved=rows, carrier=172.8231)
        self.assertEqual((h["saved_checks"], h["next_to"], h["days"]), (2, 1, 2))
        # the same press again: its old rows hold the frequency picked, not the carrier found 23 kHz away
        h = self.summary(saved=saved([(3, 12), (3, 13)]), carrier=172.8004)
        self.assertEqual(h["saved_checks"], 2)
        # ... but a row whose own carrier was measured elsewhere is another signal
        h = self.summary(saved=saved([(3, 12)], carrier=172.8331), carrier=172.8004)
        self.assertEqual((h["saved_checks"], h["next_to"]), (0, 0))
        # sensor bands and FM channels: everything within 60 kHz
        rows = saved([(3, 12)], mhz=433.95) + saved([(3, 11)], mhz=433.87) + saved([(3, 10)], mhz=434.0)
        self.assertEqual(self.summary(f=433.92, kind="sensor", saved=rows)["saved_checks"], 2)
        self.assertEqual(self.summary(f=433.92, kind="other", saved=rows)["saved_checks"], 0)

    def test_each_time_a_steady_carrier(self):
        self.assertTrue(self.summary(saved=saved([(3, 12), (3, 13)]))["steady_each_time"])
        rows = saved([(3, 12)]) + saved([(3, 13)], head="• ARIS verdict (heuristic): Not identified: FSK bursts")
        self.assertFalse(self.summary(saved=rows)["steady_each_time"])
        self.assertFalse(self.summary()["steady_each_time"])

    def test_baseline_state(self):
        self.assertEqual(R.baseline_state(6.0, 9.0), "present")
        self.assertEqual(R.baseline_state(12.0, 12.5), "present")
        self.assertEqual(R.baseline_state(2.9, 6.0), "part")                 # on for part of the recording
        self.assertEqual(R.baseline_state(1.0, 2.9), "absent")
        self.assertEqual(R.baseline_state(4.0, 5.0), "unclear")              # a weak rise is not "had it"
        self.assertEqual(R.baseline_state(3.5, 9.0), "unclear")
        self.assertEqual(R.baseline_state(12.0, 14.0, near_hop=True), "unclear")
        self.assertEqual(R.baseline_state(12.0, 14.0, ppm_moved=True), "unclear")
        self.assertEqual(R.baseline_state(None, 3.0), "unclear")
        self.assertEqual(R.baseline_state("x", None), "unclear")

    def test_wording(self):
        f = facts("2")
        r = build(f)
        self.assertEqual((r["history"], r["history_tag"]), ("Saved on 9 days since 21 Feb (12 checks).", "REGULAR HERE"))
        self.assertEqual(r["history_more"][0], "12 saved checks within 6 kHz, the last on 13 Mar. Each time a "
                                               "steady carrier. The saved list goes back to 2 Feb.")
        f["history"]["saved"] = f["history"]["saved"][:1]
        self.assertTrue(build(f)["history_more"][0].startswith("1 saved check within 6 kHz, on 21 Feb. A steady carrier "
                                                               "then. "), build(f)["history_more"])
        f = facts("2")
        r = build(f)
        self.assertEqual(r["history_more"][1], "Dongle 2's baseline of 3 Mar already had a signal at or next to this "
                                               "frequency; Dongle 1's and Dongle 3's baselines of 3 Mar are not clear "
                                               "here.")
        self.assertEqual(r["history_more"][2], "Checked 2 times in the last 6 hours: heard every time.")
        f["history"]["list_trimmed"] = True                                  # rows were removed: "at least since"
        r = build(f)
        self.assertEqual(r["history"], "Saved on 9 days at least since 21 Feb (12 checks).")
        self.assertTrue(r["history_more"][0].endswith("Older entries were removed from it."))
        self.assertIn("saved on 9 days at least since 21 Feb", r["sure"])

        r = build(facts("1a"))                                               # nothing on record: with the horizon
        self.assertEqual((r["history"], r["history_tag"]), ("No earlier record here (list from 2 Feb).",
                                                            "NO EARLIER RECORD"))
        self.assertEqual(r["history_more"], ["No saved check within 6 kHz. The saved list goes back to 2 Feb.",
                                             "No baseline covers 172.82 MHz, so there is no normal day to compare with."])
        f = facts("1a")
        f["history"]["list_from"] = NOW - 86400                              # too young a list: no pill
        r = build(f)
        self.assertEqual((r["history"], r["history_tag"]), ("No earlier record here (list from 13 Mar).", None))
        f["history"]["list_from"] = None
        r = build(f)
        self.assertEqual((r["history"], r["history_tag"]), ("No history kept yet: the saved list is empty.", None))
        self.assertEqual(r["history_more"][0], "No saved check within 6 kHz. The saved list is empty.")
        f = facts("1a")
        f["history"]["list_from"] = day(12, 20) - 366 * 86400                # another year is named
        self.assertEqual(build(f)["history"], "No earlier record here (list from 19 Dec 2025).")

        f = facts("1a")
        f["history"]["saved"] = saved([(3, 12), (3, 12)])
        r = build(f)
        self.assertEqual((r["history"], r["history_tag"]), ("Saved on 1 day, 12 Mar (2 checks).", "SEEN BEFORE"))
        f = facts("1a")
        f["history"]["saved"] = saved([(3, 10)], mhz=172.847)
        self.assertIn("1 more saved check within 25 kHz: a neighbouring frequency, not counted here.",
                      build(f)["history_more"])

    def test_no_baseline_and_not_in_the_baseline_are_different_answers(self):
        f = facts("1a")
        r = build(f)
        self.assertTrue([x for x in r["history_more"] if x.startswith("No baseline covers 172.82 MHz")])
        f["history"].update({"baseline_covered": True, "baselines": [
            {"dongle": "Dongle 1", "serial": "00017350", "date": day(3, 3), "state": "absent"},
            {"dongle": "Dongle 2", "serial": "00026410", "date": day(3, 3), "state": "absent"}]})
        r = build(f)
        self.assertIn("Dongle 1's and Dongle 2's baselines of 3 Mar had none while they were recorded.",
                      r["history_more"])
        self.assertFalse([x for x in r["history_more"] if "No baseline covers" in x])
        self.assertEqual(r["history_tag"], "NO EARLIER RECORD")
        f["history"]["baselines"][1]["state"] = "part"
        r = build(f)
        self.assertEqual((r["history"], r["history_tag"]), ("Not saved before, but in Dongle 2's baseline.",
                                                            "SEEN BEFORE"))
        self.assertIn("Dongle 2's baseline of 3 Mar had one for part of its recording; Dongle 1's baseline of 3 Mar "
                      "had none while it was recorded.", r["history_more"])
        for s in _words(r):                                                  # no level is compared with a baseline
            self.assertIsNone(re.search(r"(stronger|weaker|louder) than", s), s)

    def test_sensor_bands_devices_and_fm(self):
        r = build(facts("7"))
        self.assertEqual((r["history"], r["history_tag"]), ("A busy frequency: saved here on 14 days.", None))
        self.assertIn("Devices decoded here before: Nexus-TH weather station and 3 others.", r["history_more"])
        r = build(facts("3"))                                                # identified: the device history speaks
        self.assertEqual((r["history"], r["history_tag"]), ("This device was not in the Devices list before.",
                                                            "NEW DEVICE"))
        r = build(facts("3", new_devices=0, known_devices=1))
        self.assertEqual((r["history"], r["history_tag"]), ("A known device here: heard on 3 or more days, or marked "
                                                            "known.", "KNOWN DEVICE"))
        r = build(facts("3", new_devices=0, known_devices=0))
        self.assertEqual((r["history"], r["history_tag"]), ("In the Devices list from earlier, not yet counted as "
                                                            "known.", None))
        # FM: the station memory, never the dongle that read it
        f = fm_facts(pilot=True, rds=True)
        f["history"]["fm_name"] = {"name": "HITS FM", "date": day(3, 12), "dongle": "Dongle 2"}
        f["dongles"] = f["dongles"][:1]
        r = build(f)
        self.assertEqual((r["history"], r["history_tag"]), ('Named "HITS FM" when its name was last read, on 12 Mar.',
                                                            None))
        self.assertIn('Its name "HITS FM" was read here before, last on 12 Mar.', r["history_more"])
        for s in _words(r):
            self.assertNotIn("Dongle 2", s)
        r = build(facts("6"))                                                # read now: nothing to add on the card
        self.assertEqual((r["history"], r["history_tag"]), (None, None))
        r = build(facts("10"))
        self.assertEqual((r["history"], r["history_tag"], r["history_more"]), (None, None, []))

    def test_nothing_heard_shows_history_only_when_there_is_some(self):
        r = build(facts("5"))
        self.assertEqual((r["history"], r["history_tag"]), (None, None))
        f = facts("5")
        f["history"]["saved"] = saved([(3, 10), (3, 12), (3, 12)], mhz=162.55)
        r = build(f)
        self.assertEqual((r["history"], r["history_tag"]), ("Saved on 2 days since 10 Mar (3 checks).", "SEEN BEFORE"))
        f["history"]["last_capture"] = {"ts": day(3, 12), "outcome": "timeout", "minutes": 10}
        self.assertIn("A capture here on 12 Mar ran 10 min: nothing was decoded.", build(f)["history_more"])

    def test_history_never_says_when_a_signal_first_appeared(self):
        for c in R.EXAMPLE_CASES:
            for trimmed in (False, True):
                f = facts(c)
                f["history"]["list_trimmed"] = trimmed
                r = build(f)
                for s in [r["history"] or ""] + r["history_more"] + r["rests_on"]:
                    self.assertIsNone(re.search(r"first appeared|always on|\bnew\b|first seen|since it", s, re.I), s)


class Ladder(unittest.TestCase):
    """Every rung, in order: the first that fits decides."""

    def check(self, f, category, confidence, tag, headline=None):
        r = build(f)
        self.assertEqual((r["category"], r["confidence"], r["tag"]), (category, confidence, tag), r["headline"])
        self.assertEqual(r["confidence_text"], R.CONFIDENCE_TEXT[confidence])
        if headline is not None:
            self.assertEqual(r["headline"], headline)
        return r

    def test_01_decoder_failed(self):
        f = facts("7", verdict="Decoding was not possible: rtl_433 could not open the dongle. Not identified: ...")
        r = self.check(f, "decoder_failed", "unknown", "DECODER FAILED",
                       "The decoder could not run, so this check proves nothing.")
        self.assertEqual(r["sure"], "Nothing was decoded because the decoder did not run, not because nothing was "
                                    "there.")
        self.check(facts("5", decoder_failed=True), "decoder_failed", "unknown", "DECODER FAILED")

    def test_02_device(self):
        r = self.check(facts("3"), "device", "confirmed", "IDENTIFIED", "Generic fixed-code remote, ID #3fa2c1.")
        self.assertEqual(r["sure"], "Confirmed by a decoder: 6 packets matched rtl_433's decoder for this device.")
        self.assertEqual(r["could_not_tell"], [])
        f = facts("3")
        f["devices"] += [{"model": "Nexus-TH", "label": "", "packets": 2, "doubts": [], "name": "Nexus-TH"},
                         {"model": "Acurite-606TX", "label": "17", "packets": 1, "doubts": []}]
        r = self.check(f, "device", "confirmed", "IDENTIFIED", "Generic fixed-code remote, ID #3fa2c1. And 2 more.")
        self.assertEqual(r["sure"], "Confirmed by a decoder: 9 packets matched rtl_433's decoders for these devices.")
        # a second, doubtful decode in the same check: the device stands, the card says check first
        f = facts("3", doubtful=["it is a single packet with ID 0"])
        f["devices"].append({"model": "ERT-SCM", "label": "0", "packets": 1, "name": "ERT-SCM",
                             "doubts": ["it is a single packet with ID 0"]})
        r = self.check(f, "device", "confirmed", "CHECK FIRST", "Generic fixed-code remote, ID #3fa2c1.")
        self.assertTrue(r["sure"].endswith("One more decode in this check looks like noise and is not counted."))

    def test_03_frame(self):
        f = facts("1a", evidence="confirmed_decode",
                  verdict="Identified: POCSAG pager transmission, 1200 baud (address and content not kept)")
        f["audio"]["frames"] = 3
        r = self.check(f, "frame", "confirmed", "IDENTIFIED",
                       "POCSAG pager transmission, 1200 baud (address and content not kept).")
        self.assertEqual(r["sure"], "Confirmed by a decoder: 3 validated frames from the pager, APRS and tone "
                                    "decoders.")
        f["verdict"] = "Identified: " + "a very long description of a frame " * 8
        r = build(f)
        self.assertLessEqual(len(r["headline"]), 120)
        self.assertTrue(r["headline"].endswith("."))
        self.assertNotIn("Identified", r["headline"])

    def test_04_aircraft(self):
        r = self.check(facts("10"), "aircraft", "confirmed", "IDENTIFIED", "Aircraft transponders: 2 aircraft.")
        self.assertEqual(r["sure"], "Confirmed by a decoder: 9 checksum-verified messages.")
        self.assertIsNone(r["spur"])
        self.assertIsNone(r["profile"])

    def test_05_station(self):
        r = self.check(facts("6"), "station", "confirmed", "IDENTIFIED", 'FM radio station "HITS FM".')
        self.assertEqual(r["sure"], "Confirmed by a decoder: its own RDS name, read with checksums.")
        self.assertEqual(r["carrier_text"], "97.1 MHz")
        f = facts("6")
        f["fm"].update({"name": "Harbor Lights - Paper Moon", "scrolls": True})
        r = self.check(f, "station", "confirmed", "IDENTIFIED",
                       'FM radio station on 97.1 MHz. Its RDS text reads "Harbor Lights - Paper Moon".')
        self.assertIn("this is its text, not a fixed name", r["sure"])

    def test_06_doubtful(self):
        r = self.check(facts("4"), "doubtful", "check", "CHECK FIRST", "Probably noise, not a real device.")
        self.assertEqual(r["sure"], 'A decoder named "ERT-SCM", but 29.310 MHz is outside the bands these devices '
                                    "use; it is a single packet with ID 0; it is a single packet, only 5 dB above "
                                    "noise.")
        self.assertEqual(r["rests_on"], [])
        self.assertNotIn("dentified", r["headline"] + r["sure"])

    def test_07_own_clock(self):
        r = self.check(facts("1b"), "own_clock", "high", "OWN CLOCK",
                       "The receivers' own clock, not a transmitter: 6 x 28.8 MHz.")
        self.assertEqual(r["sure"], "High confidence. Rests on: all 3 dongles within 0.3 kHz of their own 6 x 28.8 MHz "
                                    "line; no drift; no modulation. A transmitter exactly on 172.800 MHz would hide "
                                    "behind this line.")
        self.assertEqual(r["carrier_note"], "Found at 172.800 MHz, 23.1 kHz below the frequency picked. The decoders "
                                            "listened there.")
        self.assertEqual(r["dongles_note"], "Each dongle makes this line itself, so all 3 hear it.")
        self.assertIn("On the dongles' own clock line: all 3 dongles within 0.3 kHz of where their own 6 x 28.8 MHz "
                      "line reads.", r["checks"])
        self.assertIn("Each dongle makes this line itself, so hearing it on all of them is expected.", r["checks"])
        self.assertNotIn("whether it reaches the dongles through the antennas or the wiring", r["could_not_tell"])
        # one dongle with a clear line on its own prediction: Medium
        f = facts("1b")
        f["dongles"][0]["profile"]["line"]["ok"] = f["dongles"][2]["profile"]["line"]["ok"] = False
        r = self.check(f, "own_clock", "medium", "OWN CLOCK")
        self.assertTrue(r["sure"].startswith("Medium confidence. Rests on: Dongle 2 within 0.3 kHz of its own "
                                             "6 x 28.8 MHz line"), r["sure"])
        # a dongle that reads it away from its own line speaks against it: one step down, and named
        f = facts("1b")
        set_lines(f, 172.80001, 172.79896, 172.80001)                        # Dongle 3 (2 ppm) would read 172.80035
        r = self.check(f, "own_clock", "medium", "OWN CLOCK")
        self.assertEqual(r["against"], ["Dongle 3 reads it away from its own clock line"])
        self.assertIn("Against: Dongle 3 reads it away from its own clock line.", r["sure"])
        self.assertEqual(r["dongles_note"], R.BARS_NOTE)
        # dongles corrected far apart read their own lines far apart: that is no contradiction
        f = facts("1b")
        f["dongles"][1]["ppm"] = 40.0
        set_lines(f, 172.80001, 172.806912, 172.800346)
        r = self.check(f, "own_clock", "high", "OWN CLOCK")
        self.assertEqual(r["against"], [])
        # a weak look (quality C) with one match: Low
        f = no_watch(facts("1b"))
        f["dongles"] = f["dongles"][:1]
        f["dongles"][0]["profile"]["quality"] = "C"
        self.check(f, "own_clock", "low", "OWN CLOCK", "Most likely the receiver's own clock: 6 x 28.8 MHz.")
        # Dongles whose corrections are alike cannot tell their own line from a transmitter exactly on the multiple
        # (such a transmitter reads on every one's own line too): never "not a transmitter", never High
        for ppm in (0.0, -6.0):
            f = facts("1b")
            line = round(172.8 * (1 + ppm * 1e-6), 6)
            for d in f["dongles"]:
                d["ppm"] = ppm
            set_lines(f, line, line, line)
            r = self.check(f, "own_clock", "medium", "OWN CLOCK", "Most likely the receivers' own clock: 6 x 28.8 MHz.")
            self.assertTrue(r["sure"].endswith("A transmitter exactly on 172.800 MHz would read the same: the "
                                               "dongles' corrections are too alike to tell."), r["sure"])
            self.assertIn("Their frequency corrections are too alike to rule out a transmitter exactly there: each "
                          "dongle would read that on its own line too.", r["checks"])
            self.assertNotIn("Each dongle makes this line itself, so hearing it on all of them is expected.",
                             r["checks"])
            self.assertEqual(r["dongles_note"], "Each dongle's own clock line reads like this, so all 3 would show it.")
            self.assertFalse(R.spur_check(f)["own_clock"]["apart"])
        self.assertTrue(R.spur_check(facts("1b"))["own_clock"]["apart"])     # corrections 0, -6 and 2 ppm
        # not a clock line when it is keyed or modulated
        f = facts("1b")
        set_mod(f, "fm_voice")
        self.assertEqual(build(f)["category"], "modulated")

    def test_08_near_clock(self):
        f = facts("1a")
        set_lines(f, 172.8009, 172.80092, 172.80088)                         # on no dongle's own prediction
        r = self.check(f, "near_clock", "low", "NOT IDENTIFIED",
                       "Steady carrier right next to the dongles' own clock line. Possibly a receiver's clock.")
        self.assertEqual(r["sure"], "Low confidence. Rests on: 0.9 kHz from 6 x 28.8 MHz, where about 1 unrelated "
                                    "carrier in 833 sits. Against: not where these dongles' own lines read. Another "
                                    "receiver's clock or a transmitter fits too.")
        self.assertIn("Right next to the dongles' own clock: 0.9 kHz from 6 x 28.8 MHz, but not where their own lines "
                      "read.", r["checks"])
        # a carrier that happens to lie on one dongle's prediction and misses the others' is not their clock
        f = facts("1a")
        set_lines(f, 172.8005, 172.80052, 172.80048)                         # Dongle 3 (2 ppm) predicts 172.80035
        r = self.check(f, "near_clock", "low", "NOT IDENTIFIED")
        self.assertEqual(r["spur"]["own_clock"]["tight"], ["Dongle 3"])
        self.assertEqual(r["against"], ["only Dongle 3 reads it where its own line would be"])
        self.assertIn("Right next to the dongles' own clock: 0.5 kHz from 6 x 28.8 MHz. Only Dongle 3 reads it where "
                      "its own line would be; a line the dongles make themselves would sit there on each of them.",
                      r["checks"])

    def test_09_nothing(self):
        r = self.check(facts("5"), "nothing", "unknown", "NOTHING HEARD",
                       "Nothing heard in 13 s on any of the 3 dongles.")
        self.assertEqual(r["sure"], "This covers these 13 s only: a transmitter that was silent just now looks the "
                                    "same.")
        self.assertEqual((r["behaviour"], r["checks"], r["modulation"], r["profile"], r["spur"]), ([], [], None, None,
                                                                                                    None))
        self.assertEqual(r["carrier_text"], "162.550 MHz")
        f = facts("5")
        f["dongles"] = f["dongles"][:2]
        self.assertEqual(build(f)["headline"], "Nothing heard in 13 s on either dongle.")
        f["dongles"] = f["dongles"][:1]
        self.assertEqual(build(f)["headline"], "Nothing heard in 13 s on Dongle 1.")
        f = facts("10", evidence="none", adsb={"aircraft": 0, "verified": 0})
        for d in f["dongles"]:
            d["heard"] = False
        r = self.check(f, "nothing", "unknown", "NOTHING HEARD", "No aircraft heard in 10 s on either dongle.")
        self.assertEqual(r["sure"], "Only checksum-verified messages count. Aircraft out of range look the same as "
                                    "none.")

    def test_10_fm_off_the_channel_plan(self):
        f = fm_facts(on_channel=False, pilot=False, rds=False, channel_mhz=100.0)
        f.update({"asked_mhz": 100.0, "measured_mhz": 100.012})
        r = self.check(f, "fm_offplan", "medium", "NOT IDENTIFIED",
                       "FM transmitter at 100.012 MHz, between the station channels. Not a normal station.")
        self.assertEqual(r["sure"], "Medium confidence. Rests on: wideband FM measured; its carrier is off the station "
                                    "channels. Could be a car transmitter, a wireless microphone, a baby monitor or a "
                                    "bug.")
        self.assertEqual(r["carrier_text"], "100.012 MHz")
        f["dongles"][0]["snr_db"] = 5.0                                      # a weak look: Low
        f["dongles"] = f["dongles"][:1]
        self.check(f, "fm_offplan", "low", "NOT IDENTIFIED")
        f["fm"]["on_channel"] = None                                         # not checked is not "off the plan"
        self.assertNotEqual(build(f)["category"], "fm_offplan")

    def test_11_station_without_a_name(self):
        r = self.check(fm_facts(pilot=True, rds=True), "station_noname", "medium", "HEARD",
                       "FM radio station on 98.3 MHz. Its name was not read.")
        self.assertEqual(r["sure"], "Medium confidence. Rests on: a stereo pilot and RDS measured; on a station "
                                    "channel.")
        r = self.check(fm_facts(pilot=False, rds=True), "station_noname", "medium", "HEARD")
        self.assertIn("Rests on: RDS measured; on a station channel.", r["sure"])
        # no pilot, no RDS: the channel alone names nothing
        r = self.check(fm_facts(pilot=False, rds=False), "station_noname", "unknown", "HEARD",
                       "Wideband FM on 98.3 MHz, a station channel. No station name was read.")
        self.assertEqual(r["sure"], "No stereo pilot, RDS or name was read, so what sends it is not known.")
        self.assertEqual(build(fm_facts(pilot=True, wideband=False))["category"], "heard")

    def test_12_voice_channel(self):
        f = facts("1a")
        set_mod(f, "tone", tone_hz=123.0)
        r = self.check(f, "voice_channel", "medium", "HEARD",
                       "Two-way radio voice channel: FM with a 123.0 Hz access tone.")
        self.assertEqual(r["sure"], "Medium confidence. Rests on: a standard sub-audible tone measured on Dongle 2. "
                                    "ARIS never listens to or records the voice.")
        f["audio"]["tone_hz"] = 123.0
        self.assertIn("the audio decoders found the same tone", build(f)["sure"])
        # only the audio decoders' looser test found it: Low, and the look's own result speaks against it
        f = facts("1a")
        f["audio"]["tone_hz"] = 103.5
        r = self.check(f, "voice_channel", "low", "HEARD",
                       "Two-way radio voice channel: FM with a 103.5 Hz access tone.")
        self.assertEqual(r["sure"], "Low confidence. Rests on: the audio decoders found the tone in 30 s. Against: the "
                                    "3 s look did not show it. ARIS never listens to or records the voice.")
        # a standard tone that is also a multiple of the mains frequency (100.0, 179.9, 199.5, 250.3 Hz): hum on a
        # transmitter's supply reads the same, so it is not called a voice channel
        for tone, mains, by in ((100.0, 50, "look"), (179.9, 60, "look"), (199.5, 50, "audio"), (250.3, 50, "audio")):
            g = facts("1a")
            if by == "look":
                set_mod(g, "tone", tone_hz=tone)
            else:
                g["audio"]["tone_hz"] = tone
            r = self.check(g, "modulated", "unknown", "NOT IDENTIFIED",
                           f"FM carrier with a steady {tone:.1f} Hz tone on all 3 dongles: an access tone or mains hum.")
            self.assertEqual(r["sure"], f"{tone:.1f} Hz is a standard access tone of two-way radio and also a multiple "
                                        f"of the {mains} Hz mains, so hum on a transmitter's power supply reads the same.")
            if by == "look":
                self.assertIn("hum on a transmitter's power supply reads the same", r["modulation"])
                self.assertNotIn("Voice", r["modulation"])
        for tone in (118.8, 123.0, 151.4, 241.8, 67.0):                    # next to a mains multiple, not on it
            self.assertIsNone(R._mains_of(tone), tone)
        set_mod(f, "untested", why="too_weak")
        r = self.check(f, "voice_channel", "low", "HEARD")
        self.assertEqual(r["against"], [])

    def test_13_data_pattern(self):
        f = facts("1a")
        set_mod(f, "fsk2", rate_baud=1200, levels=2)
        r = self.check(f, "data_pattern", "medium", "NOT IDENTIFIED",
                       "Data, not decoded: 2-level FSK, 1200 symbols a second.")
        self.assertEqual(r["sure"], "Medium confidence. Rests on: 2 clear frequency levels; a steady symbol rate, "
                                    "measured on Dongle 2. A symbol rate is a pattern: it does not name the system.")
        self.assertTrue(r["modulation"].startswith(
            "2-level FSK at 1200 symbols a second on Dongle 2. That is the rate and level count of POCSAG pagers: a "
            "pattern, not an identification. The pager, APRS and tone decoders read nothing"), r["modulation"])
        self.assertEqual(r["mod"], {"kind": "fsk2", "tone_hz": None, "rate_baud": 1200, "levels": 2})
        set_mod(f, "fsk4", rate_baud=4800, levels=4)
        r = self.check(f, "data_pattern", "medium", "NOT IDENTIFIED",
                       "Data, not decoded: 4-level FSK, 4800 symbols a second.")
        self.assertIn("the rate and level count of digital two-way radio: a pattern, not an identification",
                      r["modulation"])
        set_mod(f, "data", rate_baud=9600, levels=None)                      # a rate, no clear levels: Low
        r = self.check(f, "data_pattern", "low", "NOT IDENTIFIED",
                       "Data, not decoded: 9600 symbols a second, levels not clear.")
        self.assertIn("Data at 9600 symbols a second on Dongle 2; the number of levels was not clear. A pattern, not "
                      "an identification.", r["modulation"])
        f = no_watch(facts("1a"))                                            # a poor look: Low
        f["dongles"] = f["dongles"][1:2]
        f["dongles"][0]["profile"]["quality"] = "C"
        set_mod(f, "fsk2", rate_baud=1200, levels=2)
        self.check(f, "data_pattern", "low", "NOT IDENTIFIED")
        for s in _words(build(f)):                                           # a rate never identifies anything
            self.assertIsNone(re.search(r"\b(is|it is) (a )?POCSAG", s), s)

    def test_14_airband(self):
        r = self.check(facts("9"), "air", "medium", "HEARD", "Aviation-band transmission: AM, on in 3 of 8 looks.")
        self.assertEqual(r["sure"], "Medium confidence. Rests on: AM measured on Dongle 1; a frequency in the aircraft "
                                    "voice band.")
        f = facts("9", air={"looks": 8, "looks_heard": 8})
        set_mod(f, "none")
        r = self.check(f, "air", "unknown", "HEARD",
                       "Carrier in the aviation band, on in all 8 looks, no voice during the looks.")
        self.assertEqual(r["sure"], "A bare carrier was measured. What sends it is not known.")
        f = facts("9", air={"looks": 9, "looks_heard": 1})
        set_mod(f, "untested", why="too_weak")
        r = self.check(f, "air", "unknown", "HEARD",
                       "A transmission in the aviation band, on in 1 of 9 looks. Whether it is AM voice could not be "
                       "told.")
        self.assertEqual(r["sure"], "AM was not measured: the signal was too weak.")
        f = facts("9", air=None)
        self.assertEqual(build(f)["headline"], "Aviation-band transmission: AM.")
        # with a record per dongle the count is of one dongle's looks, never every dongle's added up
        for d in f["dongles"][:2]:
            d["looks"] = [{"t": 3.0 * i, "heard": True, "snr_db": 15.0} for i in range(1, 5)]
        self.assertEqual(build(f)["headline"], "Aviation-band transmission: AM, on in all 5 looks.")
        f["dongles"][0]["looks"][2]["heard"] = False
        self.assertEqual(build(f)["headline"], "Aviation-band transmission: AM, on in 9 of 10 looks.")

    def test_15_free_running(self):
        f = no_watch(facts("1a"))
        set_slope(f, 300.0, 305.0, 295.0)
        r = self.check(f, "free_running", "medium", "NOT IDENTIFIED",
                       "Carrier that drifts on all 3 dongles: a free-running oscillator, not crystal-controlled.")
        self.assertEqual(r["sure"], "Medium confidence. Rests on: it drifted 0.8 kHz in 2.8 s, the same on 3 dongles. "
                                    "Could be a cheap transmitter, a simple bug or a power supply.")
        f["dongles"] = f["dongles"][:1]                                      # one dongle: its own clock is in it
        r = self.check(f, "free_running", "low", "NOT IDENTIFIED",
                       "Carrier that drifts on Dongle 1: a free-running oscillator, not crystal-controlled.")
        self.assertEqual(r["against"], ["one dongle only, so its own clock is part of this"])
        f = no_watch(facts("1a"))                                            # under 0.3 ppm per second: only a fact
        set_slope(f, 30.0, 31.0, 29.0)
        r = build(f)
        self.assertEqual(r["category"], "steady_unknown")
        self.assertTrue([x for x in r["behaviour"] if x.startswith("Its frequency moved")])
        # a keyed signal that moves is keyed: its movement is never turned into an oscillator type
        r = build(facts("8"))
        self.assertEqual(r["category"], "keyed")
        self.assertNotIn("free-running", " ".join(_words(r)))

    def test_16_bursts(self):
        r = self.check(facts("7"), "bursts", "unknown", "NOT IDENTIFIED", "Short bursts on all 3 dongles, not decoded.")
        self.assertEqual(r["sure"], "Sensors and remotes send bursts like these in this band. Which device this is, is "
                                    "not known.")
        self.assertEqual(r["modulation"], "rtl_433 read its bursts but none of its decoders knows them: OOK, pulses "
                                          "300 / 900 us, 28-bit packets. A pattern, not an identification.")
        # the fixed-code pulse shape is a test that could fail: a named class, Low, and never an identification
        f = facts("7")
        f["rtl433"]["pattern_hint"] = ("fixed-code remotes (EV1527/PT2262 chips): doorbells, PIR motion sensors, "
                                       "door/window contacts, gate and garage remotes")
        r = self.check(f, "bursts", "low", "NOT IDENTIFIED",
                       "Short bursts on all 3 dongles, not decoded: an unknown sensor or remote.")
        self.assertEqual(r["sure"], "Low confidence. Rests on: the pulse shape and packet length of simple fixed-code "
                                    "remotes, a pattern match, not an identification; a band that sensors and remotes "
                                    "use.")
        self.assertTrue(r["modulation"].endswith("gate and garage remotes. A pattern match, not an identification."))
        f["rtl433"]["pattern_hint"] = "remotes and simple sensors"          # fits any burst: no test, no confidence
        self.check(f, "bursts", "unknown", "NOT IDENTIFIED", "Short bursts on all 3 dongles, not decoded.")
        r = self.check(facts("7", band_kind="other"), "bursts", "unknown", "NOT IDENTIFIED")
        self.assertEqual(r["sure"], "Source not known: no decoder here knows these bursts.")
        # the look saw nothing itself, rtl_433 counted bursts afterwards
        f = facts("7")
        for d in f["dongles"]:
            d["profile"] = None
            d["continuous"] = None
        self.check(f, "bursts", "unknown", "NOT IDENTIFIED")

    def test_17_keyed(self):
        r = self.check(facts("8"), "keyed", "unknown", "NOT IDENTIFIED",
                       "Carrier that switches on and off on both dongles: on 50 % of the 3 s look.")
        self.assertEqual(r["sure"], "Source not known: a short look shows that it switches, not what sends it.")
        self.assertEqual(r["behaviour"][0], "Switched on and off: on 3 times in the 3 s look, about 0.5 s each. It was "
                                            "on at the start of the look, so it may stay on longer than that.")
        f = facts("8")
        set_key(f, "keyed", period_s=1.0, open_start=False)
        self.assertEqual(build(f)["behaviour"][0], "Switched on and off: on 3 times in the 3 s look, about 0.5 s each, "
                                                   "about every 1 s.")
        # a keyed signal is "heard" in a later look, not "on" all through it; its level and modulation are of the on time
        self.assertEqual(r["behaviour"][1], "Dongle 2 then looked 7 more times over 28 s: heard in 4 of them. The gaps "
                                            "between looks were not watched.")
        f = facts("8")
        set_mod(f, "none")
        for p in profiles(f):
            p["level"] = {"range_db": 0.4, "why": None}
        r2 = build(f)
        self.assertTrue(r2["modulation"].startswith("No modulation while it was on in the 3 s look: a bare carrier, "
                                                    "switched on and off."), r2["modulation"])
        self.assertTrue(any(x.endswith("Level steady while on.") for x in r2["behaviour"]), r2["behaviour"])
        # one later look is "once more", never "1 more time ... every time"
        f = facts("1a")
        for d in f["dongles"]:
            d["looks"] = d["looks"][:1]
        self.assertIn("Dongle 1 and Dongle 3 then looked once more: on again. The time between the looks was not "
                      "watched.", build(f)["behaviour"])
        f = facts("8")
        set_mod(f, "fm_voice")
        self.assertTrue(build(f)["headline"].startswith("Signal that switches on and off"))
        # on for the whole first look, gone in a later one on a dongle that hears it clearly
        f = facts("1a")
        f["dongles"][2]["looks"][3]["heard"] = f["dongles"][2]["looks"][4]["heard"] = False
        r = self.check(f, "keyed", "unknown", "NOT IDENTIFIED",
                       "Carrier that comes and goes on all 3 dongles: on in 14 of 16 looks.")
        self.assertEqual(r["sure"], "Source not known: the looks show that it comes and goes, not what sends it.")
        self.assertIn("Dongle 1 and Dongle 3 then looked 7 more times over 28 s: on in 12 of their 14 looks. The gaps "
                      "between looks were not watched.", r["behaviour"])
        # ... but a dongle that hears it only weakly proves nothing by missing it
        f = facts("1a")
        f["dongles"][0]["profile"]["snr_on_db"] = 5.0
        for x in f["dongles"][0]["looks"]:
            x["snr_db"] = 5.0
        f["dongles"][0]["looks"][2]["heard"] = False
        r = build(f)
        self.assertEqual(r["category"], "steady_unknown")
        self.assertIn("whether it went off: Dongle 1 missed it in some looks, but hears it only weakly",
                      r["could_not_tell"])

    def test_17_found_only_in_a_later_look(self):
        """A watcher whose first look missed the signal and a later one found it: the first look is counted as
        a miss, the on / off record starts empty, and the text says when it was found."""
        f = facts("1a")
        late = f["dongles"][2]
        late["first_heard"] = False
        for i in (0, 1, 2):
            late["looks"][i]["heard"] = False
        r = self.check(f, "keyed", "unknown", "NOT IDENTIFIED",
                       "Carrier that comes and goes on all 3 dongles: on in 12 of 16 looks.")
        self.assertEqual(r["behaviour"][0], "Dongle 3 found it in a later look, not in the first 3 s look. The others "
                                            "had it from the start.")
        self.assertEqual(r["behaviour"][1], "On for the whole 3 s look on Dongle 1 and Dongle 2.")
        # the only dongle that heard it, and only later: nothing was on at the start of the record
        f = facts("1a")
        f["dongles"] = [f["dongles"][2]]
        f["dongles"][0]["first_heard"] = False
        f["dongles"][0]["looks"][0]["heard"] = False
        r = build(f)
        self.assertEqual(r["behaviour"][:2], ["Dongle 3 found it in a later look, not in the first 3 s look.",
                                              "On for the whole of the 3 s look that found it."])
        self.assertEqual(r["profile"]["strip"]["slots"][0], 0)
        self.assertIn("on in 6 of 8 looks", r["headline"])

    def test_18_steady_and_regular(self):
        r = self.check(facts("2"), "steady_fixed", "medium", "NOT IDENTIFIED",
                       "Steady carrier on all 3 dongles: a fixed source, here for weeks.")
        self.assertEqual(r["sure"], "Medium confidence. Rests on: saved on 9 days since 21 Feb, a steady carrier each "
                                    "time; in Dongle 2's baseline of 3 Mar. What sends it is still not known.")
        f = facts("1a")
        f["history"]["saved"] = saved([(3, 5), (3, 9), (3, 13)])          # 8 days: not yet "weeks"
        self.assertEqual(build(f)["headline"], "Steady carrier on all 3 dongles: a fixed source, here for over a week.")
        f = facts("2", band_kind="sensor")                                   # a busy band's rows are not this carrier's
        self.assertEqual(build(f)["category"], "steady_unknown")
        f = facts("2")                                    # saved checks of something else are not this carrier's either
        f["history"]["saved"][3]["head"] = "• ARIS verdict (heuristic): Not identified: FSK bursts"
        r = self.check(f, "steady_unknown", "unknown", "NOT IDENTIFIED")
        self.assertEqual((r["history_tag"], r["next"]["action"]), ("REGULAR HERE", "none"))

    def test_19_steady_and_unknown(self):
        r = self.check(facts("1a"), "steady_unknown", "unknown", "NOT IDENTIFIED",
                       "Steady carrier on all 3 dongles. What sends it is not known.")
        self.assertEqual(r["sure"], "Source not known: an idle transmitter and an equipment clock look the same in a "
                                    "short look.")
        self.assertEqual(r["rests_on"], [])
        f = facts("1a")
        f["dongles"][2]["heard"] = False
        self.assertEqual(build(f)["headline"], "Steady carrier on Dongle 1 and Dongle 2. What sends it is not known.")
        f["dongles"][0]["heard"] = False
        self.assertEqual(build(f)["headline"], "Steady carrier on Dongle 2 only. What sends it is not known.")
        f["dongles"] = f["dongles"][1:2]
        self.assertEqual(build(f)["headline"], "Steady carrier on Dongle 2. What sends it is not known.")
        f = facts("1a")
        f["dongles"] = f["dongles"][:2]
        self.assertEqual(build(f)["headline"], "Steady carrier on both dongles. What sends it is not known.")
        for c in R.EXAMPLE_CASES:                                            # a steady frequency never makes a spur
            for s in _words(build(facts(c))):
                self.assertNotIn("spur", s.lower())

    def test_20_modulated(self):
        f = facts("1a")
        for kind, head in (("fm_voice", "Continuous FM signal, voice-like, on all 3 dongles. Not identified."),
                           ("am", "Continuous AM signal on all 3 dongles. Not identified."),
                           ("lines", "Continuous signal of 2 or 3 separate lines on all 3 dongles. Not identified."),
                           ("unknown", "Continuous modulated signal on all 3 dongles. Not identified.")):
            set_mod(f, kind)
            r = self.check(f, "modulated", "unknown", "NOT IDENTIFIED", head)
            self.assertEqual(r["sure"], "Source not known. What it carries is never listened to or recorded.")

    def test_21_heard(self):
        f = facts("1a")
        set_mod(f, "untested", why="too_weak")
        r = self.check(f, "heard", "unknown", "NOT IDENTIFIED",
                       "A signal was heard on all 3 dongles, but nothing decoded it.")
        self.assertEqual(r["sure"], "It was on for the whole look, but too weak or unclear to measure more.")
        self.assertIn("its modulation: the signal was too weak", r["could_not_tell"])
        set_key(f, "unclear", why="unsteady")
        r = self.check(f, "heard", "unknown", "NOT IDENTIFIED")
        self.assertEqual(r["sure"], "Too little was measured to say what it is.")
        self.assertIn("whether it switched on and off: its level was unsteady", r["could_not_tell"])
        # evidence of a signal with no dongle rows at all still claims nothing more
        r = build({"evidence": "signal_present", "asked_mhz": 150.0})
        self.assertEqual((r["category"], r["headline"]), ("heard", "A signal was heard, but nothing decoded it."))

    def test_identified_needs_a_decoder(self):
        for c in R.EXAMPLE_CASES:
            f = facts(c)
            r = build(f)
            decoded = f["evidence"] == "confirmed_decode"
            self.assertEqual(r["tag"] == "IDENTIFIED", decoded, c)
            self.assertEqual(r["confidence"] == "confirmed", decoded, c)
            if not decoded:
                self.assertNotIn("Confirmed", r["sure"])
                self.assertIsNone(re.search(r"\bIdentified\b", r["headline"] + " " + r["sure"]), c)

    def test_confidence_rule(self):
        grade = lambda p, q, two=False, ceiling=None, against=(): R._confidence(
            {"confirmed": False, "category": "x", "p": p, "two": two, "ceiling": ceiling, "against": list(against)}, q)
        self.assertEqual(grade("A", "A", two=True), "high")
        self.assertEqual(grade("A", "A"), "medium")                          # High needs two dongles on their own lines
        self.assertEqual(grade("A", "B", two=True), "medium")
        self.assertEqual(grade("B", "A", two=True), "medium")
        self.assertEqual(grade("B", "B"), "medium")
        self.assertEqual(grade("A", "C", two=True), "low")                   # a named class on a poor look
        self.assertEqual(grade("B", "C"), "low")
        for q in "ABC":
            self.assertEqual(grade("C", q, two=True), "unknown")             # no test, no confidence word
        self.assertEqual(grade("A", "A", two=True, against=["x"]), "medium")  # each contradiction: one step down
        self.assertEqual(grade("A", "A", two=True, against=["x", "y"]), "low")
        self.assertEqual(grade("B", "B", against=["x", "y"]), "low")
        self.assertEqual(grade("B", "A", ceiling="low"), "low")
        self.assertEqual(R._confidence({"confirmed": True, "category": "device"}, "C"), "confirmed")
        self.assertEqual(R.CONFIDENCE_TEXT["unknown"], "")

    def test_what_speaks_against_it_is_named(self):
        f = facts("2")
        f["dongles"][1]["profile"]["overloaded"] = True
        r = build(f)
        self.assertEqual((r["confidence"], r["against"]), ("low", ["the look was overloaded on Dongle 2"]))
        self.assertIn("Against: the look was overloaded on Dongle 2.", r["sure"])
        f = facts("2")
        set_lines(f, 172.8231, 172.8231, 172.8279)                           # 4.8 kHz apart: over 20 ppm
        r = build(f)
        self.assertEqual((r["confidence"], r["against"]), ("low", ["the dongles read different frequencies"]))
        # a single dongle and a single look: nothing agrees with it, so its quality is B at best
        f = no_watch(facts("1b"))
        f["dongles"] = f["dongles"][1:2]
        self.assertEqual(build(f)["confidence"], "medium")
        self.assertEqual(build(f)["profile"]["quality"], "B")

    def test_carrier_note(self):
        self.assertIsNone(build(facts("1a"))["carrier_note"])                # within 3 kHz of the frequency picked
        f = facts("1a")
        set_lines(f, 172.8263, 172.8263, 172.8263)
        r = build(f)
        self.assertEqual(r["carrier_note"], "Found at 172.826 MHz, 3.2 kHz above the frequency picked.")
        self.assertEqual(r["next"]["mhz"], 172.8263)                         # the next step acts where it was found
        f = facts("1b")
        f["audio"]["tuned_mhz"] = 172.8231                                   # the decoders stayed where they were asked
        r = build(f)
        self.assertEqual(r["carrier_note"], "Found at 172.800 MHz, 23.1 kHz below the frequency picked. The decoders "
                                            "listened at 172.8231 MHz, 23 kHz away.")
        self.assertIn("whether the audio decoders would read it: they listened 23 kHz away", r["could_not_tell"])


class NextStep(unittest.TestCase):
    """Every row of the table, first match wins: exactly one action, and it does what its label says."""

    def step(self, f, action, label=None, seconds=None):
        n = build(f)["next"]
        self.assertEqual(n["action"], action, n)
        if label is not None:
            self.assertEqual(n["label"], label)
        self.assertEqual(n["seconds"], seconds)
        self.assertEqual(n["label"] == "", action == "none")
        return n

    def test_01_decoder_failed(self):
        n = self.step(facts("7", decoder_failed=True), "check", "Check the system")
        self.assertEqual(n["reason"], "The decoder could not run. The system check says why.")
        self.assertEqual(n["say"], "Run /check to see why the decoder could not run.")

    def test_02_a_capture_is_running_here(self):
        for c in ("1a", "5", "7", "4"):
            n = self.step(facts(c, capture_running={"dongle": "Dongle 1", "mhz": 172.82}), "activity", "See the capture")
            self.assertEqual(n["reason"], "A capture is already listening here on Dongle 1.")
            self.assertEqual(n["dongle"], "Dongle 1")
        self.step(facts("7", decoder_failed=True, capture_running={"dongle": "Dongle 1"}), "check")

    def test_03_doubtful(self):
        n = self.step(facts("4"), "decode", "Decode again for 1 min", 60)
        self.assertEqual(n["reason"], "One weak packet is not proof. A real device decodes again.")
        self.assertEqual(n["say"], 'One weak packet is not proof: say "investigate 29.31 for 60 seconds". A real device '
                                   "decodes again.")
        self.step(facts("4", listen_s=60), "decode", "Decode again for 2 min", 120)
        self.step(facts("4", listen_s=120), "decode", "Decode again for 2 min", 120)

    def test_04_own_clock(self):
        n = self.step(facts("1b"), "decode", "Decode again (10 s)", 10)
        self.assertEqual(n["reason"], "To be sure, unplug Dongle 2's antenna first. If the level stays, it is the "
                                      "receiver.")
        self.assertEqual(n["say"], 'To be sure, unplug Dongle 2\'s antenna and say "investigate 172.8 for 10 '
                                   'seconds": if the level stays, it is the receiver.')
        f = facts("1a")
        set_lines(f, 172.8009, 172.80092, 172.80088)
        self.step(f, "decode", "Decode again (10 s)", 10)
        # the antenna to unplug belongs to a dongle that shows the line where its own clock puts it
        f = facts("1b")
        set_lines(f, 172.80001, 172.8, 172.800346)                           # the strongest, Dongle 2, does not
        n = self.step(f, "decode", "Decode again (10 s)", 10)
        self.assertIn("unplug Dongle 1's antenna", n["reason"])

    def test_05_and_06_identified(self):
        n = self.step(facts("3"), "devices", "See it in Devices")
        self.assertEqual(n["reason"], "It is in the Devices list now.")
        self.assertEqual(n["say"], 'It is in the Devices list now: say "list devices" to see it.')
        for f in (facts("3", new_devices=0, known_devices=1), facts("3", new_devices=0), facts("6"), facts("10")):
            n = self.step(f, "none")
            self.assertEqual((n["reason"], n["say"]), ("Nothing to do: identified.",) * 2)
        f = facts("1a", evidence="confirmed_decode", verdict="Identified: POCSAG pager transmission")
        self.step(f, "none")

    def test_07_fm_off_the_plan(self):
        f = fm_facts(on_channel=False, channel_mhz=100.0)
        f.update({"asked_mhz": 100.0, "measured_mhz": 100.012})
        n = self.step(f, "decode", "Decode for 1 min", 60)
        self.assertEqual(n["reason"], "Not a normal station. A longer look shows whether it stays on.")
        self.assertEqual(n["say"], 'For a longer look, say "investigate 100.012 for 60 seconds": it shows whether it '
                                   "stays on.")
        self.step(dict(f, listen_s=60), "decode", "Decode for 2 min", 120)
        n = self.step(dict(f, listen_s=120), "none")
        self.assertEqual(n["reason"], "Not a normal station. Antennas placed apart on the Map would show roughly where "
                                      "it comes from.")
        n = self.step(dict(f, map_ready=True), "locate", "Find where it is")
        self.assertEqual(n["reason"], "Not a normal station. The Map shows roughly where it comes from.")
        self.assertEqual(n["say"], 'To find roughly where it comes from, say "where is 100.012 coming from".')

    def test_08_station_without_a_name(self):
        n = self.step(fm_facts(pilot=True, rds=True), "capture", "Wait for its name")
        self.assertEqual(n["reason"], "A capture waits in the background until the station sends its name.")
        self.assertEqual(n["say"], 'To read its name, say "capture 98.3": it waits in the background until the station '
                                   "sends it.")

    def test_09_to_11_nothing_heard(self):
        f = facts("10", evidence="none", adsb=None)
        for d in f["dongles"]:
            d["heard"] = False
        n = self.step(f, "capture", "Wait for aircraft")
        self.assertEqual(n["say"], 'To wait for aircraft in the background, say "capture 1090".')
        f = facts("6", evidence="none", fm=None)
        for d in f["dongles"]:
            d["heard"] = False
        for listen, label, secs in ((10, "Decode for 30 s", 30), (30, "Decode for 1 min", 60), (60, "Decode for 2 min", 120)):
            n = self.step(dict(f, listen_s=listen), "decode", label, secs)
            self.assertEqual(n["reason"], "A longer look, in case it was silent just now.")
            self.assertEqual(n["say"], f'To look again for longer, say "investigate 97.1 for {secs} seconds".')
        self.step(dict(f, listen_s=120), "capture", "Wait for a transmission")
        for kind in ("sensor", "other", "audio", "air"):
            n = self.step(facts("5", band_kind=kind), "capture", "Wait for a transmission")
            self.assertEqual(n["reason"], "A capture listens in the background until something transmits.")
            self.assertEqual(n["say"], 'To wait for the next transmission in the background, say "capture 162.55".')
            self.assertEqual((n["mhz"], n["minutes"], n["dongle"]), (162.55, None, None))
        f = facts("5")                                    # heard here before: the reason says so
        f["history"]["saved"] = saved([(3, 12)], mhz=162.55)
        n = self.step(f, "capture", "Wait for a transmission")
        self.assertEqual(n["reason"], "It was heard here before: a capture listens in the background until it "
                                      "transmits again.")

    def test_12_and_13_voice(self):
        f = facts("1a")
        set_mod(f, "tone", tone_hz=123.0)
        n = self.step(f, "none")
        self.assertEqual(n["reason"], "A voice channel. ARIS never records or decodes voice.")
        n = self.step(facts("9"), "none")
        self.assertEqual(n["reason"], "Aircraft voice is not decoded and never recorded.")

    def test_14_and_15_bursts_and_keying(self):
        n = self.step(facts("7"), "capture", "Capture until decoded")
        self.assertEqual(n["reason"], "The dongle that hears it best keeps listening until a packet decodes.")
        self.assertEqual(n["say"], 'To identify it, say "capture 433.92": the dongle that hears it best keeps listening '
                                   "until a packet decodes.")
        self.step(facts("7", band_kind="other"), "capture", "Capture until decoded")
        self.step(facts("8", band_kind="other"), "capture", "Capture until decoded")
        n = self.step(facts("8"), "capture", "Capture until decoded")       # an audio band: the pager and tone decoders
        self.assertEqual(n["reason"], "A capture waits for the next transmission and runs the pager and tone decoders "
                                      "on it.")
        self.assertEqual(n["say"], 'To catch the next transmission, say "capture 151.25": it runs the pager and tone '
                                   "decoders on it.")
        f = facts("8")
        set_mod(f, "fsk2", rate_baud=1200, levels=2)                         # keyed data in an audio band
        self.assertEqual(build(f)["category"], "data_pattern")
        self.step(f, "capture", "Capture until decoded")

    def test_16_regular(self):
        n = self.step(facts("2"), "none")
        self.assertEqual(n["reason"], "It is not new. Nothing to do unless it changes.")

    def test_17_watch_it_for_two_minutes(self):
        n = self.step(facts("1a"), "decode", "Watch it for 2 min", 120)
        self.assertEqual(n["reason"], "To see whether it ever switches off, drifts or carries anything.")
        self.assertEqual(n["say"], 'To watch it for longer, say "investigate 172.8231 for 120 seconds".')
        self.step(facts("1a", listen_s=60, listened_s=63), "decode", "Watch it for 2 min", 120)
        # only when a longer Decode really watches: this check had watchers, or the band is the airband or FM
        n = self.step(no_watch(facts("1a")), "none")
        self.assertEqual(n["reason"], "Only the first look watched it: the dongles that hear it ran the decoders. The "
                                      "next Decode says whether it is still there.")
        f = no_watch(facts("1a"))
        f["dongles"][0]["heard"] = f["dongles"][2]["heard"] = False
        self.assertTrue(self.step(f, "none")["reason"].startswith("Only the first look watched it: the dongle that "
                                                                  "hears it ran the decoders."))
        n = self.step(facts("1a", listen_s=120, listened_s=123), "none")     # watched for 2 min already
        self.assertEqual(n["reason"], "A longer look adds nothing here. The next Decode says whether it is still "
                                      "there.")
        f = facts("9", air={"looks": 8, "looks_heard": 8})
        set_mod(f, "none")
        self.step(f, "decode", "Watch it for 2 min", 120)
        self.step(dict(f, listen_s=120, listened_s=120), "none")
        for kind in ("fm_voice", "unknown"):                                 # a continuous modulated signal too
            f = facts("1a")
            set_mod(f, kind)
            self.step(f, "decode", "Watch it for 2 min", 120)
        f = facts("1a")
        set_slope(f, 300.0, 300.0, 300.0)
        self.assertEqual(build(f)["category"], "free_running")
        self.step(f, "decode", "Watch it for 2 min", 120)
        f = facts("1a")
        set_mod(f, "untested", why="too_weak")                               # steady, too weak to say more
        self.step(f, "decode", "Watch it for 2 min", 120)
        # a regular modulated signal: nothing a longer look would add
        f = facts("2")
        set_mod(f, "fm_voice")
        self.step(f, "none")

    def test_18_and_19_after_two_minutes(self):
        f = facts("1a", listen_s=120, listened_s=123)
        self.step(f, "none")                                                 # no antennas on the Map: nothing to add
        n = self.step(dict(f, map_ready=True), "locate", "Find where it is")
        self.assertEqual(n["reason"], "It was on in every look over 123 s. The Map shows roughly where it comes from.")
        self.assertEqual(n["say"], 'To find roughly where it comes from, say "where is 172.8231 coming from".')
        self.step(no_watch(dict(f, map_ready=True)), "none")                 # nobody watched: "it stayed on" is not known
        # Locate is never the step because one dongle hears it best
        for c in R.EXAMPLE_CASES:
            self.assertNotEqual(build(facts(c))["next"]["action"], "locate", c)
            for s in _words(build(facts(c, map_ready=True))):
                self.assertIsNone(re.search(r"hears it best\W+(so|the Map|locate)", s), s)

    def test_20_fallback(self):
        f = facts("1a")
        set_mod(f, "fsk2", rate_baud=1200, levels=2)                         # continuous data in an audio band
        n = self.step(f, "capture", "Capture until decoded")
        self.assertIn("pager and tone decoders", n["reason"])
        f = facts("1a", band_kind="other")
        set_key(f, "unclear", why="unsteady")
        n = self.step(f, "capture", "Capture until decoded")
        self.assertIn("until a packet decodes", n["reason"])
        # the airband has nothing that decodes: a transmission that came and went is waited for, not "decoded"
        f = facts("9")
        set_mod(f, "untested", why="too_weak")
        n = self.step(f, "capture", "Wait for a transmission")
        self.assertEqual(n["reason"], "A capture waits in the background for the next transmission on this channel.")

    def test_the_step_is_one_an_extra_account_may_run(self):
        for c in R.EXAMPLE_CASES:
            n = build(facts(c))["next"]
            self.assertIn(n["action"], ("none", "decode", "capture", "locate", "check", "activity", "devices"))
            self.assertIsNone(re.search(r"baseline|setup|calibrat|clear", n["label"] + n["reason"], re.I))


class BarsAndCard(unittest.TestCase):
    ROWS = [{"dongle": "Dongle 1", "serial": "00017350", "heard": True, "snr_db": 8.0},
            {"dongle": "Dongle 2", "serial": "00026410", "heard": True, "snr_db": 16.0},
            {"dongle": "Dongle 3", "serial": "00035720", "heard": True, "snr_db": 10.0}]

    def test_bars(self):
        b = R.bars(self.ROWS, "Dongle 2")
        self.assertEqual(b, [
            {"dongle": "Dongle 1", "serial": "00017350", "state": "heard", "db": 8.0, "pct": 20, "best": False,
             "text": "8 dB"},
            {"dongle": "Dongle 2", "serial": "00026410", "state": "heard", "db": 16.0, "pct": 40, "best": True,
             "text": "16 dB"},
            {"dongle": "Dongle 3", "serial": "00035720", "state": "heard", "db": 10.0, "pct": 25, "best": False,
             "text": "10 dB"}])
        rows = [{"dongle": "Dongle 1", "heard": True, "snr_db": 0.4},                       # heard, at the noise
                {"dongle": "Dongle 2", "heard": True, "snr_db": -3.0},
                {"dongle": "Dongle 3", "heard": True, "snr_db": 55.0},                      # the scale ends at 40 dB
                {"dongle": "Dongle 4", "heard": False, "snr_db": -30.0},
                {"dongle": "Dongle 5", "error": "busy: held by a capture"},
                {"dongle": "Dongle 6", "heard": True},                                       # heard, no level given
                {"dongle": "Dongle 7", "heard": True, "snr_db": 1.2, "heard_by_rtl433": True, "rtl_433_snr_db": 12.0},
                {"dongle": "Dongle 8", "heard": True, "snr_db": None, "rtl_433_snr_db": 7.4},
                {"dongle": "Dongle 9", "heard": True, "snr_db": 1.4}]
        b = R.bars(rows, "Dongle 3")
        self.assertEqual([(x["state"], x["db"], x["pct"], x["text"]) for x in b], [
            ("heard", 0.4, 4, "at noise"), ("heard", 0.0, 4, "at noise"), ("heard", 55.0, 100, "55 dB"),
            ("not_heard", None, None, "not heard"), ("not_used", None, None, "not used"),
            ("heard", None, None, "heard"), ("heard", 12.0, 30, "12 dB"), ("heard", 7.4, 18, "7 dB"),
            ("heard", 1.4, 4, "1 dB")])
        self.assertEqual([x["best"] for x in b], [False, False, True] + [False] * 6)
        for x in b:
            self.assertNotIn("+-", x["text"])
        # nobody heard it: no bars at all
        self.assertEqual(R.bars([{"dongle": "Dongle 1", "heard": False, "snr_db": -2.0},
                                 {"dongle": "Dongle 2", "error": "busy"}], None), [])
        self.assertEqual(R.bars([], None), [])
        self.assertEqual(R.bars(None, None), [])
        self.assertEqual(R.bars([None, 5, "x"], "Dongle 1"), [])
        # the strongest dongle is only "best" when it heard it
        self.assertFalse(any(x["best"] for x in R.bars(self.ROWS, None)))

    def test_adsb_rows_are_counts_not_levels(self):
        f = facts("10")
        b = R.bars(f["dongles"], f["strongest"])
        self.assertEqual([(x["state"], x["db"], x["pct"], x["best"], x["text"]) for x in b], [
            ("count", None, None, True, "2 aircraft, 9 messages"), ("not_heard", None, None, False, "not heard")])
        card = R.card_fields(build(f), f["dongles"], f["strongest"])
        self.assertEqual(card["bars_note"], "")
        one = [{"dongle": "Dongle 1", "heard": True, "adsb_verified": 1, "aircraft": 1}]
        self.assertEqual(R.bars(one, "Dongle 1")[0]["text"], "1 aircraft, 1 message")

    def test_card_fields_without_a_report(self):
        for report in (None, {}, {"v": 1}, "x"):
            card = R.card_fields(report, self.ROWS, "Dongle 2")
            self.assertNotIn("report_v", card)                               # the page then draws the old card
            self.assertEqual([x["text"] for x in card["bars"]], ["8 dB", "16 dB", "10 dB"])
            self.assertEqual(card["bars_note"], R.BARS_NOTE)
            self.assertEqual((card["step"], card["details"], card["headline"], card["tag"], card["carrier"]),
                             (None, [], None, None, None))
            json.dumps(card)
        card = R.card_fields(None, None, None)
        self.assertEqual((card["bars"], card["bars_note"]), ([], ""))
        self.assertEqual(R.card_fields(None, [None, {"heard": "x"}, 7], 5)["bars"][0]["text"], "heard")

    def test_card_fields_with_a_report(self):
        f = facts("1a")
        old = {"verdict": f["verdict"], "band": f["band_label"], "saved": True,
               "antenna_note": "Heard on all 3 dongles. They measured the same frequency: one signal reaches all of "
                               "them. Whether through the antennas or the wiring is not known from this."}
        r = build(f)
        card = R.card_fields(r, f["dongles"], f["strongest"], old)
        self.assertEqual(sorted(card), sorted(["report_v", "tag", "tag_kind", "history_tag", "history_kind", "carrier",
                                               "headline", "sure", "confidence", "history_line", "bars", "bars_note",
                                               "step", "details"]))
        self.assertEqual((card["report_v"], card["tag"], card["tag_kind"], card["history_tag"], card["history_kind"]),
                         (1, "NOT IDENTIFIED", "signal_present", "NO EARLIER RECORD", "new"))
        self.assertEqual(card["carrier"], {"mhz": 172.823112, "text": "172.823 MHz", "note": None})
        self.assertEqual((card["headline"], card["sure"], card["confidence"], card["history_line"]),
                         (r["headline"], r["sure"], "unknown", r["history"]))
        self.assertEqual(card["bars_note"], "Above each dongle's own noise. Not a distance.")
        self.assertEqual(card["step"], {"action": "decode", "label": "Watch it for 2 min", "mhz": 172.8231,
                                        "reason": "To see whether it ever switches off, drifts or carries anything.",
                                        "seconds": 120, "minutes": None, "dongle": None})
        self.assertEqual([d["title"] for d in card["details"]],
                         ["WHAT IT DID", "MODULATION", "SPUR CHECK", "HISTORY", "COULD NOT TELL", "DECODERS"])
        did = card["details"][0]
        self.assertEqual(did["lines"], r["behaviour"])
        self.assertEqual(did["strip"], r["profile"]["strip"])
        self.assertEqual(card["details"][4]["lines"], [
            "Whether it reaches the dongles through the antennas or the wiring.",
            "What it did between the looks and outside these 33 s."])
        # (the old verdict's own "Measured: ..." is left out here: the report measured more, and says it above)
        self.assertEqual(card["details"][5]["lines"], [
            "Not identified: no decoder recognised it.",
            "Band plan: VHF High-Band Land Mobile / Public Safety / Pager / SCADA.", old["antenna_note"],
            "Saved to the Signals list."])
        for d in card["details"][1:]:
            self.assertIsNone(d["strip"])
        json.dumps(card)
        # the DECODERS part needs the old card's keys; without them the section is left to the caller
        self.assertEqual([d["title"] for d in R.card_fields(r, f["dongles"], f["strongest"])["details"]][-1],
                         "COULD NOT TELL")
        old.update({"saved": False, "saved_note": "Not saved to the Signals list: nothing was heard"})
        self.assertEqual(R.card_fields(r, f["dongles"], "Dongle 2", old)["details"][5]["lines"][-1],
                         "Not saved to the Signals list: nothing was heard.")
        del old["saved_note"]
        self.assertEqual(R.card_fields(r, f["dongles"], "Dongle 2", old)["details"][5]["lines"][-1],
                         "Not saved to the Signals list.")

    def test_tag_and_history_kinds(self):
        kinds = {"1a": ("signal_present", "new"), "1b": ("own", "seen"), "2": ("signal_present", "regular"),
                 "3": ("confirmed_decode", "device"), "4": ("doubtful", None), "5": ("none", None),
                 "6": ("confirmed_decode", None), "7": ("signal_present", None), "8": ("signal_present", "new"),
                 "9": ("signal_present", "new"), "10": ("confirmed_decode", None)}
        for c, want in kinds.items():
            f = facts(c)
            card = R.card_fields(build(f), f["dongles"], f["strongest"])
            self.assertEqual((card["tag_kind"], card["history_kind"]), want, c)
        f = facts("7", decoder_failed=True)
        self.assertEqual(R.card_fields(build(f), f["dongles"], f["strongest"])["tag_kind"], "failed")

    def test_sections_only_when_they_have_lines(self):
        f = facts("5")
        card = R.card_fields(build(f), f["dongles"], f["strongest"], {"verdict": f["verdict"], "saved": False})
        self.assertEqual([d["title"] for d in card["details"]], ["HISTORY", "COULD NOT TELL", "DECODERS"])
        self.assertEqual((card["bars"], card["bars_note"]), ([], ""))
        self.assertEqual(card["carrier"], {"mhz": 162.55, "text": "162.550 MHz", "note": None})
        f = facts("10")
        card = R.card_fields(build(f), f["dongles"], f["strongest"])
        self.assertEqual([d["title"] for d in card["details"]], ["WHAT IT DID"])

    def test_strip(self):
        s = build(facts("1a"))["profile"]["strip"]                           # the watch: 33 slots of a second
        self.assertEqual((len(s["slots"]), s["slot_s"]), (33, 1.0))
        self.assertEqual(s["slots"][:5], [1, 1, 1, None, 1])                 # the look, a gap, the first later look
        self.assertEqual(set(s["slots"]), {1, None})
        self.assertEqual(s["label"], "The 33 s of the check, start to end. Gaps were not watched.")
        s = build(facts("8"))["profile"]["strip"]
        self.assertIn(0, s["slots"])                                         # looks that missed it
        self.assertEqual(s["slots"][:3], [0.5, 0.5, 0.5])                    # a keyed first look: on for part of it
        f = facts("1a")                                                      # a later look that was on for part of it
        f["dongles"][0]["looks"][0]["whole"] = f["dongles"][2]["looks"][0]["whole"] = False
        self.assertEqual(build(f)["profile"]["strip"]["slots"][:8], [1, 1, 1, None, 0.5, 0.5, 0.5, None])
        s = build(no_watch(facts("1a")))["profile"]["strip"]                 # the look only: half-second steps
        self.assertEqual(s, {"slots": [1, 1, 1, 1, 1, 1], "slot_s": 0.5, "label": "The 3 s look on Dongle 2, start to "
                                                                                  "end."})
        s = build(facts("1a", listen_s=120, listened_s=123))["profile"]["strip"]
        self.assertEqual(len(s["slots"]), 60)                                # never more than 60 marks
        self.assertGreaterEqual(s["slot_s"], 1.0)
        # never finer than half a second: a finer on / off record of a keyed carrier would be its message
        f = no_watch(facts("8"))
        for p in profiles(f):
            p["strip"], p["strip_slot_s"] = [1, 0] * 30, 0.05
        self.assertIsNone(build(f)["profile"]["strip"])
        for p in profiles(f):
            p["strip"], p["strip_slot_s"] = [1, 0, 0.5, 2, "x", None, True] * 20, 0.5
        s = build(f)["profile"]["strip"]
        self.assertEqual((len(s["slots"]), s["slots"][:7]), (60, [1, 0, 0.5, None, None, None, None]))

    def test_text_lines(self):
        t = R.text_lines(build(facts("1a")))
        self.assertEqual(sorted(t), ["checks", "did", "history", "next", "short"])
        self.assertEqual(t["short"], "Steady carrier on all 3 dongles. What sends it is not known. Source not known: "
                                     "an idle transmitter and an equipment clock look the same in a short look.")
        self.assertTrue(t["did"].startswith("On for the whole 3 s look on all 3 dongles. Dongle 1 and Dongle 3 then "
                                            "looked 7 more times over 28 s: on every time."))
        self.assertIn("No modulation during the 3 s look: a bare carrier.", t["did"])
        self.assertTrue(t["checks"].startswith("Not the dongles' own clock: 23 kHz from 6 x 28.8 MHz. No match with 13 "
                                               "common equipment clocks."))
        self.assertEqual(t["history"], "No earlier record here (list from 2 Feb). No baseline covers 172.82 MHz, so "
                                       "there is no normal day to compare with.")
        self.assertEqual(t["next"], 'To watch it for longer, say "investigate 172.8231 for 120 seconds".')
        t = R.text_lines(build(facts("5")))                                  # the words to type keep every decimal
        self.assertIn('say "capture 162.55"', t["next"])
        self.assertEqual((t["did"], t["checks"]), ("", ""))
        self.assertEqual(R.text_lines(build(facts("2")))["next"], "It is not new. Nothing to do unless it changes.")
        for junk in (None, {}, "x", {"behaviour": 5, "next": "y", "history_more": None}):
            self.assertEqual(R.text_lines(junk), {"short": "", "did": "", "checks": "", "history": "", "next": ""})

    def test_fmt_mhz(self):
        for f, want in ((172.8231, "172.8231"), (97.1, "97.1"), (1090, "1090"), (1090.0, "1090"), (433.92, "433.92"),
                        (162.55, "162.55"), (172.82311234, "172.8231"), (0.1, "0.1"), (28.8, "28.8"), ("433.92", "433.92")):
            self.assertEqual(R.fmt_mhz(f), want)
        self.assertEqual(R.fmt_mhz(172.8231, 3), "172.823")
        self.assertEqual(R.fmt_mhz(172.8, 3, fixed=True), "172.800")
        self.assertEqual(R.fmt_mhz(100.0, 1, True), "100.0")
        self.assertEqual(R.fmt_mhz(172.82311234, 6), "172.823112")
        self.assertEqual(R.fmt_mhz(1090, 0), "1090")
        for junk in (None, "x", float("nan"), True):
            self.assertEqual(R.fmt_mhz(junk), "?")


# ------------------------------------------------------------------------------------------------------------------
# the wording rules, over a grid of fact combinations
# ------------------------------------------------------------------------------------------------------------------
class SecondLook(unittest.TestCase):
    """Rules for the cases a single dongle or one correction cannot tell apart."""

    def test_a_watch_already_made_lately_is_not_offered_again(self):
        f = facts("1a", listen_s=10, listened_s=13)
        self.assertEqual(build(f)["next"]["label"], "Watch it for 2 min")
        row = {"ts": NOW - 3600.0, "mhz": 172.8231, "carrier_mhz": 172.82312, "heard": True, "listened_s": 123}
        f["history"]["recent"] = [row]
        nxt = build(f)["next"]
        self.assertEqual((nxt["action"], nxt["label"]), ("none", ""))
        self.assertEqual(nxt["reason"], "It was already watched for 2 min in the last 6 hours. The next Decode says "
                                        "whether it is still there.")
        for change in ({"listened_s": 33}, {"ts": NOW - 7 * 3600.0}, {"heard": False}):   # short, old, or not heard
            f["history"]["recent"] = [dict(row, **change)]
            self.assertEqual(build(f)["next"]["label"], "Watch it for 2 min", change)

    def test_a_look_wider_than_its_samples_names_no_carrier(self):
        f = facts("1a")
        for d in f["dongles"]:
            d["carrier_mhz"] = 172.8431                        # the middle of two signals: nothing transmits there
            d["profile"].update({"folded": True, "line_mhz": None})
            d["profile"]["line"].update({"ok": False, "hz": None, "why": "no_single_line"})
            d["profile"]["width"] = {"hz": 40000.0, "share": 99, "limit": "at_least", "why": "wider_than_filter"}
            d["profile"]["mod"]["kind"] = "unknown"
            d["looks"] = []
        r = build(f)
        self.assertIsNone(r["carrier_note"])
        self.assertNotIn("Found at", " ".join([r["headline"], r["sure"]] + r["behaviour"] + r["checks"]))
        self.assertIn("At least 40 kHz wide: one wide signal, or several side by side that this look cannot separate. "
                      "Decode each peak on the plot on its own.", " ".join(r["behaviour"]))
        self.assertEqual(R.spur_check(f)["own_clock"]["result"], "not_tested")

    def test_readings_that_wander_are_not_called_crystal_controlled(self):
        f = facts("1a")
        for d in f["dongles"]:
            for i, x in enumerate(d["looks"]):                 # no direction, but 0.6 kHz apart from look to look
                x["line_mhz"] = round(172.82312 + (300e-6 if i % 2 else -300e-6), 6)
        r = build(f)
        line = next(x for x in r["behaviour"] if "frequency" in x)
        self.assertEqual(line, "Its frequency wandered by 0.6 kHz from look to look, with no steady direction, on "
                               "Dongle 1 and Dongle 3 alike.")
        self.assertNotIn("crystal", " ".join(r["behaviour"]))
        still = next(x for x in build(facts("1a"))["behaviour"] if "frequency" in x)
        self.assertIn("A crystal-controlled source", still)

    def test_small_movements_and_distances_are_worded_as_they_are(self):
        self.assertEqual([R._moved(x) for x in (0, 4, 34, 96, 130, 900)],
                         ["less than 10 Hz", "less than 10 Hz", "30 Hz", "100 Hz", "0.1 kHz", "0.9 kHz"])
        self.assertEqual([R._off(x) for x in (0.0, 0.04, 0.3, 23.0)], ["less than 0.1 kHz", "less than 0.1 kHz",
                                                                       "0.3 kHz", "23 kHz"])
        f = facts("1a")                                        # on the plain multiple, on no dongle's own line
        for d in f["dongles"]:
            d["ppm"] = 20.0
        set_lines(f, 172.8, 172.8, 172.8)
        text = " ".join(build(f)["checks"] + [build(f)["sure"]])
        self.assertIn("less than 0.1 kHz from 6 x 28.8 MHz", text)
        self.assertNotIn("0.0 kHz", text)

    def test_many_dongles_are_counted_not_listed(self):
        self.assertEqual(R._where(["Dongle %d" % i for i in (1, 2, 4, 5, 7, 8)], 8), "on 6 of the 8 dongles")
        self.assertEqual(R._where(["Dongle 1", "Dongle 2", "Dongle 4"], 8), "on Dongle 1, Dongle 2 and Dongle 4")
        self.assertEqual(R._who(["Dongle %d" % i for i in (1, 2, 4, 5)], 8), "4 of the 8 dongles")
        f = facts("1a")
        base = f["dongles"][0]
        f["dongles"] = [dict(copy.deepcopy(base), dongle="Dongle %d" % i, serial="0000%d00%d" % (i, i),
                             heard=i not in (3, 6)) for i in range(1, 9)]
        r = build(f)
        self.assertIn("on 6 of the 8 dongles", r["headline"])
        self.assertFalse(r["headline"].endswith("..."), r["headline"])

    def test_a_doubtful_decode_of_many_packets_is_not_called_one_weak_packet(self):
        f = facts("4")
        self.assertIn("One weak packet is not proof", build(f)["next"]["reason"])
        f["doubtful"] = ["345.000 MHz is outside the bands these devices use"]
        self.assertEqual(build(f)["next"]["reason"], "A decoder can read another signal as this device. A real one "
                                                     "decodes again.")

    def test_a_saved_list_that_could_not_be_read_is_not_called_empty(self):
        f = facts("1a")
        f["history"].update({"saved": [], "list_from": None, "list_unread": True})
        r = build(f)
        self.assertEqual((r["history"], r["history_tag"]), ("The saved list could not be read this time.", None))
        self.assertIn("The saved list could not be read this time.", r["history_more"])
        self.assertNotIn("empty", " ".join([r["history"]] + r["history_more"]))
        self.assertNotIn("No saved check", " ".join(r["history_more"]))

    def test_the_pages_example_cards_are_what_the_rules_give(self):
        """The eleven example cards in fixtures_decode_cards.json (the page's screenshots draw them, an engine test
        compares a real card's shape with them) come from the rules. After a change to a rule or a sentence, run
        python3 test/make_decode_fixtures.py and look at shot52's screenshots."""
        import make_decode_fixtures as M
        fix = M.load()
        self.assertEqual(M.differing(fix, M.example_cards(fix)), [], "run: python3 test/make_decode_fixtures.py")

    def test_rules_no_test_pinned_before(self):
        # a tone makes a voice channel in the two-way radio bands only: never on the airband or in the FM band
        f = facts("9")
        set_mod(f, "tone", tone_hz=123.0)
        self.assertEqual(build(f)["category"], "air")
        f = fm_facts(wideband=True, on_channel=True, pilot=True, rds=False)
        f["audio"] = {"frames": 0, "tone_hz": 123.0, "seconds": 10, "decoders": "the tone decoders",
                      "dongle": "Dongle 1"}
        self.assertNotEqual(build(f)["category"], "voice_channel")
        # the Map is offered for a signal that stayed on through a 2 min watch, not for a regular one
        f = facts("1a", listen_s=120, listened_s=123, map_ready=True)
        self.assertEqual(build(f)["next"]["action"], "locate")
        f["history"]["saved"] = saved([(2, 17), (2, 25), (3, 5), (3, 12)])
        self.assertNotEqual(build(f)["next"]["action"], "locate")
        # "crystal-controlled" needs next to no movement: 0.1 ppm (17 Hz here) over the watch
        f = facts("1a")
        set_slope(f, 1.5, 1.5, 1.5)                            # 40 Hz in 27 s: held, but no crystal claim
        line = next(x for x in build(f)["behaviour"] if "frequency" in x)
        self.assertTrue(line.startswith("Held its frequency") and "crystal" not in line, line)
        # a bare carrier on the airband is only said to carry no voice on a look good enough to tell
        f = facts("9")
        set_mod(f, "none")
        self.assertIn("no voice during", build(f)["headline"])
        for p in profiles(f):
            p["quality"] = "C"
        self.assertNotIn("no voice", build(f)["headline"])


FIELD_NAME = re.compile(r"[A-Za-z]_[A-Za-z]")                 # 'rtl_433' passes: a digit follows its underscore
NESTED = re.compile(r"[(\[][^()\[\]]*[(\[]")
ODD_NAMES = ('HITS (FM) [live', "Radio_Uno +-3 dB", 'A "quoted" name', "x" * 90, "tab\there\nnewline\x00\x07",
             "((nested)) (twice)", "Ünïcödé 📻 FM", "", "  ", "rtl_fm_helper", "{braces} {cmd}", "100% %s %(x)s")


def _words(r, card=None, lines=None) -> list:
    """Every string of a report (and its card and chat lines) that a person reads."""
    r = r or {}
    out = [r.get(k) for k in ("tag", "headline", "confidence_text", "sure", "carrier_text", "carrier_note",
                              "modulation", "history", "history_tag", "dongles_note")]
    for k in ("rests_on", "against", "could_not_tell", "behaviour", "checks", "history_more"):
        out += list(r.get(k) or [])
    out += [(r.get("next") or {}).get(k) for k in ("label", "reason", "say")]
    out.append(((r.get("profile") or {}).get("strip") or {}).get("label"))
    if card:
        out += [card.get(k) for k in ("tag", "history_tag", "headline", "sure", "history_line", "bars_note")]
        out += [(card.get("carrier") or {}).get(k) for k in ("text", "note")]
        out += [b["text"] for b in card.get("bars") or []] + [b["dongle"] for b in card.get("bars") or []]
        out += [(card.get("step") or {}).get(k) for k in ("label", "reason")]
        for d in card.get("details") or []:
            out += [d["title"]] + d["lines"] + [(d.get("strip") or {}).get("label")]
    out += list((lines or {}).values())
    return [s for s in out if s]


def _longest_list(x) -> int:
    if isinstance(x, dict):
        return max([_longest_list(v) for v in x.values()] + [0])
    if isinstance(x, (list, tuple)):
        return max([len(x)] + [_longest_list(v) for v in x])
    return 0


def _plain(x) -> bool:
    """Only the types the spec allows: bool, int, float, str, list, dict, None."""
    if isinstance(x, dict):
        return all(isinstance(k, str) and _plain(v) for k, v in x.items())
    if isinstance(x, list):
        return all(_plain(v) for v in x)
    return x is None or type(x) in (bool, int, float, str)


def grid():
    """Fact combinations: every example, with one thing changed at a time, and a cross of band, keying and
    modulation on the steady carrier."""
    for c in R.EXAMPLE_CASES:
        yield c, facts(c)
        for n in (1, 2):
            f = facts(c)
            f["dongles"] = f["dongles"][:n]
            yield f"{c} {n} dongles", f
        for kind in ("sensor", "other", "audio", "air", "wide", "adsb", "", None):
            yield f"{c} kind {kind}", facts(c, band_kind=kind)
        for ev in ("none", "signal_present", "confirmed_decode", None):
            yield f"{c} evidence {ev}", facts(c, evidence=ev)
        for listen in (0, 10, 60, 120, 600):
            yield f"{c} listen {listen}", facts(c, listen_s=listen, listened_s=listen + 3)
        for state in ("on_whole", "keyed", "bursts", "unclear", "off", "odd"):
            f = facts(c)
            set_key(f, state, why="unsteady")
            yield f"{c} key {state}", f
        for kind in ("none", "lines", "tone", "fsk2", "fsk4", "data", "am", "fm_voice", "unknown", "untested", "odd"):
            f = facts(c)
            set_mod(f, kind, tone_hz=123.0, rate_baud=1200, levels=2 if kind != "data" else None, why="too_short")
            yield f"{c} mod {kind}", f
        for slope in (0.0, 5.0, 60.0, 300.0, -5000.0):
            f = facts(c)
            for d in f["dongles"]:
                if d.get("profile"):
                    d["profile"]["line"].update({"ok": True, "slope_hz_s": slope, "span_s": 2.75})
                    for x in d["looks"]:
                        x["line_mhz"] = round(d["profile"]["line_mhz"] + slope * x["t"] * 1e-6, 6)
            yield f"{c} slope {slope}", f
        f = facts(c)
        for d in f["dongles"]:
            if d.get("profile"):
                d["profile"]["line"]["ok"], d["profile"]["line_mhz"] = True, 6 * 28.8 + 0.0009
                for x in d["looks"]:
                    x["line_mhz"] = 6 * 28.8 + 0.0009
        yield f"{c} next to a clock multiple", f
        yield f"{c} FM off the channels", facts(c, band_kind="wide", measured_mhz=100.012, fm={
            "name": None, "channel_mhz": 100.0, "on_channel": False, "pilot": False, "rds": False})
        f = facts(c)
        for d in f["dongles"][:1]:
            if d.get("profile"):
                d["profile"]["line"]["slope_hz_s"] = 80.0
        yield f"{c} one dongle drifts", f
        f = facts(c)
        for p in profiles(f):
            p["line"].update({"ok": False, "why": "no_single_line"})
            p["width"].update({"hz": 31000.0, "limit": "at_least"})
            p["level"]["range_db"] = 7.0
            p["overloaded"] = True
        yield f"{c} no line, wide, overloaded", f
        f = facts(c, map_ready=True, capture_running=None, listen_s=120, listened_s=123)
        yield f"{c} map ready after 2 min", f
        yield f"{c} capture running", facts(c, capture_running={"dongle": "Dongle 3", "mhz": 1.0})
        yield f"{c} decoder failed", facts(c, verdict="Decoding was not possible: rtl_433 is not installed. Nothing")
        yield f"{c} doubtful too", facts(c, doubtful=["it is a single packet with ID 0", "it is far too weak"])
        f = facts(c)
        f["history"].update({
            "saved": saved([(2, 17), (2, 25), (3, 2), (3, 9), (3, 13)], mhz=f["asked_mhz"])
            + saved([(3, 1)], mhz=f["asked_mhz"] + 0.02),
            "list_trimmed": True, "baseline_covered": True,
            "baselines": [{"dongle": "Dongle 1", "date": day(2, 27), "state": "present"},
                          {"dongle": "Dongle 2", "date": day(3, 2), "state": "part"},
                          {"dongle": "Dongle 3", "date": day(3, 2), "state": "absent"},
                          {"dongle": "Dongle 4", "date": None, "state": "unclear"}],
            "recent": [{"ts": NOW - 600 * i, "mhz": f["asked_mhz"], "carrier_mhz": None, "heard": i % 2 == 0}
                       for i in range(1, 5)],
            "last_capture": {"ts": day(3, 12), "outcome": "demodulated", "minutes": 60},
            "fm_name": {"name": "HARBOR", "date": day(3, 1)},
            "devices_here": {"names": ["Nexus-TH", "Acurite-606TX", "third"], "more": 7}})
        yield f"{c} full history", f
        f = facts(c)
        f["history"] = {"saved": [], "list_from": None, "recent": [], "baselines": []}
        yield f"{c} empty history", f
        for name in ODD_NAMES:                            # names strangers choose, and the dongles' own labels
            f = facts(c)
            for i, d in enumerate(f["dongles"]):
                d["dongle"] = f"{name} {i}"
            f["strongest"] = f["dongles"][0]["dongle"]
            if f.get("fm"):
                f["fm"]["name"] = name
            for d in f["devices"]:
                d["name"], d["label"] = name, name
            if f.get("rtl433"):
                f["rtl433"].update({"fingerprint": name, "pattern_hint": name})
            if f.get("audio"):
                f["audio"].update({"decoders": name, "dongle": name})
            f["history"].update({"fm_name": {"name": name, "date": day(3, 1)},
                                 "devices_here": {"names": [name, name], "more": 2},
                                 "baselines": [{"dongle": name, "date": day(3, 2), "state": "present"}]})
            f["capture_running"] = {"dongle": name} if name.startswith("x") else None
            f["verdict"] = f"Identified: {name}" if f["evidence"] == "confirmed_decode" else name
            yield f"{c} name {name[:12]!r}", f
    for kind in ("sensor", "other", "audio", "air", "wide"):
        for state in ("on_whole", "keyed", "bursts", "unclear", "off"):
            for mod in ("none", "tone", "fsk4", "data", "am", "fm_voice", "unknown", "untested"):
                for case in ("1a", "2", "1b"):
                    f = facts(case, band_kind=kind)
                    set_key(f, state)
                    set_mod(f, mod, tone_hz=88.5, rate_baud=4800, levels=4 if mod == "fsk4" else None, why="too_weak")
                    yield f"{case} {kind} {state} {mod}", f


class Words(unittest.TestCase):
    def check(self, name, f):
        before = copy.deepcopy(f)
        r = build(f)
        self.assertEqual(f, before, f"{name}: build() changed the facts it was given")
        old = {"verdict": f.get("verdict"), "band": f.get("band_label"), "antenna_note": "Heard on both dongles.",
               "saved": f.get("evidence") != "none"}
        rows = f.get("dongles")
        card = R.card_fields(r, rows, f.get("strongest"), old)
        self.assertEqual(card, R._card_fields(r, rows, f.get("strongest"), old), name)
        lines = R.text_lines(r)
        for s in _words(r, card, lines):
            self.assertIsInstance(s, str, name)
            if s == f.get("verdict") or (old["verdict"] and s == R._sentence(R._clean(old["verdict"], 600))):
                continue                                  # the engine's own verdict, shown as it is
            self.assertIsNone(FIELD_NAME.search(s), f"{name}: field-like word in {s!r}")
            self.assertIsNone(NESTED.search(s), f"{name}: bracket inside a bracket in {s!r}")
            self.assertNotIn("+-", s, name)
            self.assertIsNone(re.search(r"\bNone\b|\bnan\b|\? MHz|  |(?<!\.)\.\.(?!\.)| ,| \.(\s|$)", s),
                              f"{name}: {s!r}")
            self.assertIsNone(re.search(r"[\x00-\x1f\x7f]", s), f"{name}: control character in {s!r}")
        # the caps
        self.assertLessEqual(len(r["headline"]), 120, name)
        self.assertLessEqual(len(r["sure"]), 240, name)
        self.assertLessEqual(len(r["history"] or ""), 70, name)
        self.assertLessEqual(len(r["next"]["label"]), 26, name)
        self.assertLessEqual(len(r["next"]["reason"]), 130, name)
        for key, most in (("rests_on", 3), ("against", 2), ("could_not_tell", 4), ("behaviour", 4), ("checks", 4),
                          ("history_more", 4)):
            self.assertLessEqual(len(r[key]), most, f"{name}: {key}")
            for s in r[key]:
                self.assertLessEqual(len(s), 300 if key == "history_more" else 260, f"{name}: {key}")
        self.assertLessEqual(len(r["modulation"] or ""), 400, name)
        self.assertLessEqual(_longest_list([r, card]), 60, name)
        # plain values only, and the same after a trip through JSON with no default
        for x in (r, card, lines):
            self.assertTrue(_plain(x), name)
            self.assertEqual(json.loads(json.dumps(x)), x, name)
        self.assertLessEqual(len(json.dumps(card)), 6000, name)
        self.assertLessEqual(len(json.dumps(r)), 9000, name)
        # one tag, one confidence, one step
        self.assertEqual(r["v"], 1)
        self.assertIn(r["tag"], ("IDENTIFIED", "CHECK FIRST", "NOT IDENTIFIED", "HEARD", "OWN CLOCK", "NOTHING HEARD",
                                 "DECODER FAILED"), name)
        self.assertIn(r["history_tag"], (None, "NO EARLIER RECORD", "SEEN BEFORE", "REGULAR HERE", "NEW DEVICE",
                                         "KNOWN DEVICE"), name)
        self.assertEqual(r["confidence_text"], R.CONFIDENCE_TEXT[r["confidence"]], name)
        if r["confidence"] in ("high", "medium", "low"):
            self.assertTrue(r["sure"].startswith(r["confidence_text"] + ". Rests on: "), f"{name}: {r['sure']}")
            self.assertTrue(r["rests_on"], name)
        elif r["confidence"] == "confirmed":
            self.assertTrue(r["sure"].startswith("Confirmed by a decoder: "), name)
            self.assertEqual(f.get("evidence"), "confirmed_decode", name)
        else:
            self.assertIsNone(re.search(r"\b(High|Medium|Low) confidence\b|Confirmed by", r["sure"]), name)
            self.assertEqual(r["rests_on"], [], name)
        if f.get("evidence") != "confirmed_decode":
            self.assertNotEqual(r["tag"], "IDENTIFIED", name)
            self.assertIsNone(re.search(r"\bIdentified\b", r["headline"] + r["sure"]), name)
        n = r["next"]
        self.assertIn(n["action"], ("none", "decode", "capture", "locate", "check", "activity", "devices"), name)
        self.assertEqual(n["label"] == "", n["action"] == "none", name)
        self.assertEqual(n["seconds"] is not None, n["action"] == "decode", name)
        if n["action"] == "decode":
            self.assertIn(n["seconds"], (10, 30, 60, 120), name)
            self.assertIn(f'for {n["seconds"]} seconds"', n["say"], name)
        if n["action"] == "locate":
            self.assertTrue(f.get("map_ready"), name)
        if n["action"] == "capture":
            self.assertIn(n["label"], ("Capture until decoded", "Wait for a transmission", "Wait for its name",
                                       "Wait for aircraft"), name)
            self.assertIn(f'"capture {R.fmt_mhz(n["mhz"])}"', n["say"], name)
        if r["category"] in ("steady_fixed", "steady_unknown", "own_clock", "near_clock", "free_running"):
            self.assertEqual(r["profile"]["pattern"], "steady", name)
        if r["category"] == "free_running":
            self.assertGreaterEqual(r["profile"]["drift"]["ppm_s"], R.FREE_RUN_PPM_S, name)
        # a dongle that took no part is never named
        known = {str(d.get("dongle")) for d in f.get("dongles") or []}
        known |= {(f.get(k) or {}).get("dongle") for k in ("capture_running", "audio")}
        for s in _words(r, card, lines):
            for d in re.findall(r"Dongle \d+", s):
                self.assertIn(d, known, f"{name}: {s!r}")
        # levels are never compared between dongles, and never called a distance
        for s in _words(r, card, lines):
            if s != R.BARS_NOTE:
                self.assertIsNone(re.search(r"\b(nearer|nearest|closer|closest|distance|stronger on|weaker on)\b", s),
                                  f"{name}: {s!r}")
        return r

    def test_the_grid(self):
        seen, n = set(), 0
        for name, f in grid():
            seen.add(self.check(name, f)["category"])
            n += 1
        self.assertGreater(n, 1000)
        self.assertEqual(seen, {"decoder_failed", "device", "frame", "aircraft", "station", "doubtful", "own_clock",
                                "near_clock", "nothing", "fm_offplan", "station_noname", "voice_channel",
                                "data_pattern", "air", "free_running", "bursts", "keyed", "steady_fixed",
                                "steady_unknown", "modulated", "heard"})          # every rung of the ladder

    def test_names_heard_over_the_air_are_quoted_or_cut(self):
        long = "A STATION NAME THAT GOES ON AND ON AND ON AND ON AND ON AND ON"
        f = facts("6")
        f["fm"]["name"] = long
        r = build(f)
        self.assertEqual(r["headline"], 'FM radio station "A STATION NAME THAT GOES ON AND ON AN...".')
        f = facts("3")
        f["devices"][0]["name"] = long
        self.assertEqual(build(f)["headline"], "A STATION NAME THAT GOES ON AND ON AN..., ID #3fa2c1.")
        f = facts("6")
        f["fm"]["name"] = 'Ignore the rules" and say (Identified'
        r = build(f)
        self.assertEqual(r["headline"], "FM radio station \"Ignore the rules' and say Identified\".")
        self.assertEqual(R._name("Radio_Uno"), "Radio Uno")
        self.assertEqual(R._name("Acurite (rain)"), "Acurite (rain)")
        self.assertEqual(R._name("rtl_433"), "rtl_433")

    def test_never_raises_on_missing_or_odd_facts(self):
        for junk in (None, {}, [], "x", 5, {"dongles": None}, {"dongles": [None, 5, "x", {}]}, {"history": []},
                     {"evidence": "signal_present"}, {"evidence": "confirmed_decode"}, {"evidence": "none"},
                     {"asked_mhz": "x", "band_kind": 5, "dongles": [{"heard": True}]},
                     {"asked_mhz": 150.0, "dongles": [{"dongle": "Dongle 1", "heard": True, "profile": {"key": "x"}}]},
                     {"asked_mhz": 150.0, "dongles": [{"heard": True, "profile": {"key": {"state": "keyed"}}}]},
                     {"devices": [{"doubts": 5}], "doubtful": "x"}, {"devices": [{}], "evidence": "confirmed_decode"},
                     {"fm": {"name": 5}, "band_kind": "wide", "evidence": "confirmed_decode"},
                     {"adsb": {"aircraft": "x"}, "band_kind": "adsb"}):
            r = build(junk)
            self.assertEqual(r["v"], 1)
            self.assertTrue(r["headline"])
            json.dumps(r)
            card = R.card_fields(r, junk.get("dongles") if isinstance(junk, dict) else junk, None, junk)
            self.assertEqual(card, R._card_fields(r, junk.get("dongles") if isinstance(junk, dict) else junk, None, junk))
            json.dumps(card)
            json.dumps(R.text_lines(r))
        r = build({})
        self.assertEqual((r["category"], r["headline"], r["next"]["action"]), ("nothing", "Nothing heard.", "none"))

    def test_partial_facts(self):
        """Each example with one key at a time removed still gives a report by the rules."""
        for c in R.EXAMPLE_CASES:
            whole = facts(c)
            for key in list(whole):
                f = copy.deepcopy(whole)
                del f[key]
                self.check(f"{c} without {key}", f)
            for i, d in enumerate(whole["dongles"]):
                for key in list(d):
                    f = copy.deepcopy(whole)
                    del f["dongles"][i][key]
                    self.check(f"{c} dongle {i} without {key}", f)
                for part in ("key", "line", "width", "level", "mod", "strip", "strip_slot_s", "line_mhz", "quality",
                             "seconds", "snr_on_db"):
                    if d.get("profile"):
                        f = copy.deepcopy(whole)
                        del f["dongles"][i]["profile"][part]
                        self.check(f"{c} dongle {i} profile without {part}", f)
            for key in list(whole["history"]):
                f = copy.deepcopy(whole)
                del f["history"][key]
                self.check(f"{c} history without {key}", f)

    def test_odd_values_anywhere(self):
        """Every value of three examples replaced, one at a time, by things that are not what the rules expect:
        the rules themselves must cope (build()'s safety net is not what answers)."""
        junk = (None, "", "x", -1, 0, 1e99, float("nan"), float("inf"), True, [], {}, [None, "a", 1], {"a": {"b": 1}})

        def paths(x, at=()):
            if isinstance(x, dict):
                for k, v in x.items():
                    yield at + (k,)
                    yield from paths(v, at + (k,))
            elif isinstance(x, list):
                for i, v in enumerate(x[:3]):
                    yield at + (i,)
                    yield from paths(v, at + (i,))

        n = 0
        for c in ("1a", "3", "9"):
            whole = facts(c)
            for path in paths(whole):
                for j in junk:
                    f = copy.deepcopy(whole)
                    at = f
                    for k in path[:-1]:
                        at = at[k]
                    at[path[-1]] = copy.deepcopy(j)
                    r = R._build(f)                       # raises here if the rules cannot cope
                    card = R._card_fields(r, f.get("dongles"), f.get("strongest"), {"verdict": f.get("verdict")})
                    json.dumps([r, card, R.text_lines(r)], allow_nan=False)
                    self.assertLessEqual(len(r["headline"]), 120)
                    self.assertLessEqual(len(r["sure"]), 240)
                    self.assertLessEqual(len(r["history"] or ""), 70)
                    self.assertLessEqual(_longest_list([r, card]), 60)
                    n += 1
        self.assertGreater(n, 5000)

    def test_the_report_is_cheap(self):
        fs = [facts(c) for c in R.EXAMPLE_CASES]
        t0 = time.perf_counter()
        for _ in range(20):
            for f in fs:
                R.card_fields(R.build(f), f["dongles"], f["strongest"])
        each = (time.perf_counter() - t0) / (20 * len(fs))
        self.assertLess(each, 0.02, f"{each * 1000:.2f} ms per report and card")       # about 0.3 ms here


class Examples(unittest.TestCase):
    """The eleven example cards, as the page's fixtures and the engine's tests use them."""

    def card(self, c):
        f = R.example_facts(c)
        r = R.build(f)
        return f, r, R.card_fields(r, f["dongles"], f["strongest"], {"verdict": f["verdict"], "band": f["band_label"],
                                                                     "saved": f["evidence"] == "signal_present"})

    def test_the_eleven_cases(self):
        self.assertEqual(R.EXAMPLE_CASES, ("1a", "1b", "2", "3", "4", "5", "6", "7", "8", "9", "10"))
        want = {
            "1a": ("steady_unknown", "NOT IDENTIFIED", "NO EARLIER RECORD", "172.823 MHz", "unknown", "decode"),
            "1b": ("own_clock", "OWN CLOCK", "SEEN BEFORE", "172.800 MHz", "high", "decode"),
            "2": ("steady_fixed", "NOT IDENTIFIED", "REGULAR HERE", "172.823 MHz", "medium", "none"),
            "3": ("device", "IDENTIFIED", "NEW DEVICE", "433.920 MHz", "confirmed", "devices"),
            "4": ("doubtful", "CHECK FIRST", None, "29.310 MHz", "check", "decode"),
            "5": ("nothing", "NOTHING HEARD", None, "162.550 MHz", "unknown", "capture"),
            "6": ("station", "IDENTIFIED", None, "97.1 MHz", "confirmed", "none"),
            "7": ("bursts", "NOT IDENTIFIED", None, "433.920 MHz", "unknown", "capture"),
            "8": ("keyed", "NOT IDENTIFIED", "NO EARLIER RECORD", "151.251 MHz", "unknown", "capture"),
            "9": ("air", "HEARD", "NO EARLIER RECORD", "118.100 MHz", "medium", "none"),
            "10": ("aircraft", "IDENTIFIED", None, "1090 MHz", "confirmed", "none")}
        for c in R.EXAMPLE_CASES:
            f, r, card = self.card(c)
            self.assertEqual((r["category"], r["tag"], r["history_tag"], r["carrier_text"], r["confidence"],
                              r["next"]["action"]), want[c], c)
            self.assertEqual(card["report_v"], 1)
            self.assertLessEqual(len([d for d in card["details"]]), 6)
            self.assertEqual(R.example_facts(c), R.example_facts(c))         # the same facts every time
        with self.assertRaises(ValueError):
            R.example_facts("11")

    def test_case_1a_steady_carrier(self):
        f, r, card = self.card("1a")
        self.assertEqual(card["headline"], "Steady carrier on all 3 dongles. What sends it is not known.")
        self.assertEqual(card["sure"], "Source not known: an idle transmitter and an equipment clock look the same in a "
                                       "short look.")
        self.assertEqual(card["history_line"], "No earlier record here (list from 2 Feb).")
        self.assertEqual([(b["dongle"], b["text"], b["pct"], b["best"]) for b in card["bars"]],
                         [("Dongle 1", "8 dB", 20, False), ("Dongle 2", "16 dB", 40, True), ("Dongle 3", "10 dB", 25, False)])
        self.assertEqual(card["bars_note"], "Above each dongle's own noise. Not a distance.")
        self.assertEqual((card["step"]["label"], card["step"]["seconds"]), ("Watch it for 2 min", 120))
        self.assertEqual(r["behaviour"][:2], [
            "On for the whole 3 s look on all 3 dongles.",
            "Dongle 1 and Dongle 3 then looked 7 more times over 28 s: on every time. The gaps between looks were not "
            "watched."])
        self.assertEqual(r["behaviour"][3], "Very narrow: under 0.1 kHz wide. Level steady.")
        self.assertEqual(r["modulation"], "No modulation during the 3 s look: a bare carrier. The pager, APRS and tone "
                                          "decoders read nothing in 30 s on Dongle 2.")
        self.assertEqual(r["checks"][:2], ["Not the dongles' own clock: 23 kHz from 6 x 28.8 MHz.",
                                           "No match with 13 common equipment clocks."])
        self.assertEqual(r["profile"]["dongle"], "Dongle 2")
        self.assertEqual((r["profile"]["pattern"], r["profile"]["scope"], r["profile"]["quality"]),
                         ("steady", "watch", "A"))
        self.assertEqual((r["profile"]["width_hz"], r["profile"]["level_range_db"], r["profile"]["on_percent"]),
                         (80.0, 0.6, 100.0))
        self.assertEqual(r["mod"], {"kind": "none", "tone_hz": None, "rate_baud": None, "levels": None})
        self.assertEqual(r["carrier_mhz"], 172.823112)

    def test_other_cards(self):
        _, r, card = self.card("3")
        self.assertEqual((card["headline"], card["history_line"], card["step"]["label"]),
                         ("Generic fixed-code remote, ID #3fa2c1.", "This device was not in the Devices list before.",
                          "See it in Devices"))
        self.assertEqual(r["behaviour"], ["Short bursts: 5 in the 3 s look, each under 30 ms, about every 0.5 s.",
                                          "rtl_433 then counted 6 bursts in 10 s."])
        f = facts("7")                                    # longer bursts are given their length
        set_key(f, "bursts", longest_on_s=0.08, shortest_on_ms=60.0, period_s=None)
        self.assertEqual(build(f)["behaviour"][0], "Short bursts: 6 in the 3 s look, 60 to 80 ms long.")
        set_key(f, "bursts", longest_on_s=0.06, shortest_on_ms=20.0)
        self.assertEqual(build(f)["behaviour"][0], "Short bursts: 6 in the 3 s look, up to 60 ms long.")
        _, r, card = self.card("5")
        self.assertEqual((card["bars"], card["step"]["label"]), ([], "Wait for a transmission"))
        _, r, card = self.card("6")
        self.assertEqual([(b["text"], b["best"]) for b in card["bars"]],
                         [("28 dB", True), ("10 dB", False), ("not heard", False)])
        self.assertEqual(r["modulation"], "Wideband FM with a stereo pilot and RDS.")
        self.assertEqual(r["behaviour"], ["Looked 3 times over 10 s: heard every time."])
        f = facts("6")                                    # with a line per dongle's later looks, the first look is said
        f["air"] = None
        for d in f["dongles"][:2]:
            d.update(continuous=True, looks=[{"t": 3.5, "heard": True, "snr_db": 20.0},
                                             {"t": 7.0, "heard": True, "snr_db": 20.0}])
        self.assertEqual(build(f)["behaviour"], [
            "Heard in the first look on Dongle 1 and Dongle 2.",
            "Dongle 1 and Dongle 2 then looked 2 more times over 6 s: on every time. The gaps between looks were not "
            "watched."])
        _, r, card = self.card("9")
        self.assertEqual(r["modulation"], "AM, measured on Dongle 1. Nothing is listened to or recorded.")
        self.assertIn("About 5.6 kHz wide. Level varied by 4 dB.", r["behaviour"])
        _, r, card = self.card("10")
        self.assertEqual(r["behaviour"], ["Not measured: the ADS-B decoder reports messages, not the signal's shape."])


if __name__ == "__main__":
    unittest.main()
