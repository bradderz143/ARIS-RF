"""The Spectrum view's plan: which parts of a view the receivers here tune, the step, and the pieces to sweep.
python3 -m unittest test_specplan   (pure arithmetic: no radios, no engine)"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arislib import specplan as sp  # noqa: E402

RTL = {"00001001": (24.0, 1766.0)}
THREE = {"00001001": (24.0, 1766.0), "00003003": (24.0, 1766.0), "00004004": (24.0, 1766.0)}
WIDE = {"sa-1": (0.1, 12400.0)}
NAMES = {"00001001": "Dongle 1", "00003003": "Dongle 2", "00004004": "Dongle 3", "sa-1": "Dongle 4"}


class Plan(unittest.TestCase):
    def test_a_tuned_view_is_one_sweep_with_no_gaps(self):
        pl = sp.plan(88, 108, THREE, 0.8)
        self.assertEqual((pl["mode"], pl["covered"], pl["gaps"]), ("sweep", [[88.0, 108.0]], []))
        self.assertEqual(pl["step_khz"], 25.0)                   # the step the FM channel check was tuned with
        self.assertTrue(pl["step_auto"])
        self.assertEqual(pl["chunks"], [{"start": 88.0, "stop": 108.0, "points": 1600, "receivers": list(THREE)}])
        self.assertEqual(sp.gap_note(pl, NAMES, THREE), "")

    def test_the_automatic_step_gives_about_the_points_asked_for(self):
        for lo, hi, step in ((433, 435, 2.5), (902, 928, 25.0), (30, 200, 200.0), (118, 137, 25.0), (144, 148, 5.0), (433, 434, 1.0),
                             (24, 1766, 200.0), (300, 800, 200.0)):
            pl = sp.plan(lo, hi, RTL, 0.8)
            self.assertEqual(pl["step_khz"], step, (lo, hi))
            self.assertIn(pl["step_khz"], sp.STEPS_KHZ)
            if hi - lo <= 200 and step > 1.0:
                self.assertLessEqual(pl["bins"], 1600, (lo, hi))
                self.assertGreater(pl["bins"], 300, (lo, hi))

    def test_full_span_with_rtl_sdr_dongles_only(self):
        pl = sp.plan(0, 6000, THREE, 0.8)
        self.assertEqual(pl["mode"], "sweep")
        self.assertEqual(pl["covered"], [[24.0, 1766.0]])
        self.assertEqual(pl["gaps"], [[0.0, 24.0], [1766.0, 6000.0]])
        self.assertEqual(pl["step_khz"], sp.AUTO_MAX_STEP_KHZ)   # never coarser on its own: it would not be faster
        n = len(pl["chunks"])
        self.assertTrue(8 <= n <= 16, n)                         # the picture fills in step by step
        self.assertEqual(pl["chunks"][0]["start"], 24.0)
        self.assertEqual(pl["chunks"][-1]["stop"], 1766.0)
        for a, b in zip(pl["chunks"], pl["chunks"][1:]):
            self.assertEqual(a["stop"], b["start"])               # no hole and no overlap between pieces
        for c in pl["chunks"]:
            self.assertLessEqual(c["stop"] - c["start"], 200.0 + 1e-6)
            self.assertEqual(c["receivers"], list(THREE))
        self.assertLess(abs(sum(c["points"] for c in pl["chunks"]) - 1600), 16)
        self.assertEqual(sp.gap_note(pl, NAMES, THREE), "No receiver here tunes 0-24 MHz or 1766-6000 MHz.")

    def test_one_receiver_is_named_with_its_range(self):
        pl = sp.plan(1000, 3000, RTL, 0.8)
        self.assertEqual(pl["gaps"], [[1766.0, 3000.0]])
        self.assertEqual(sp.gap_note(pl, NAMES, RTL),
                         "Dongle 1 tunes 24-1766 MHz, so there is nothing to show above that.")
        self.assertIn("below that", sp.gap_note(sp.plan(0, 100, RTL, 0.8), NAMES, RTL))
        self.assertIn("outside that", sp.gap_note(sp.plan(0, 6000, RTL, 0.8), NAMES, RTL))

    def test_a_view_nothing_tunes_has_no_chunks(self):
        pl = sp.plan(2400, 2500, THREE, 0.8)
        self.assertEqual((pl["mode"], pl["covered"], pl["chunks"], pl["step_khz"]), ("none", [], [], None))
        self.assertEqual(pl["gaps"], [[2400.0, 2500.0]])
        self.assertEqual(pl["receivers"], {r: None for r in THREE})
        self.assertEqual(sp.gap_note(pl, NAMES, THREE), "No receiver here tunes 2400-2500 MHz.")
        self.assertEqual(sp.gap_note(sp.plan(2400, 2500, RTL, 0.8), NAMES, RTL),
                         "Dongle 1 tunes 24-1766 MHz, so there is nothing to show here.")
        self.assertEqual(sp.plan(100, 200, {}, 0.8)["mode"], "none")
        self.assertIn("No receiver is set up", sp.gap_note(sp.plan(100, 200, {}, 0.8)))

    def test_each_receiver_gets_only_its_own_part(self):
        both = dict(THREE, **WIDE)
        pl = sp.plan(0, 6000, both, 0.8)
        self.assertEqual(pl["covered"], [[0.1, 6000.0]])
        self.assertEqual(pl["gaps"], [[0.0, 0.1]])
        self.assertEqual(pl["receivers"]["00001001"], [24.0, 1766.0])
        self.assertEqual(pl["receivers"]["sa-1"], [0.1, 6000.0])
        for c in pl["chunks"]:
            self.assertLessEqual(c["stop"] - c["start"], 200.0 + 1e-6)
            self.assertIn("sa-1", c["receivers"])
            self.assertEqual("00001001" in c["receivers"], c["start"] < 1766.0 and c["stop"] > 24.0, c)
        pl = sp.plan(2400, 2500, both, 0.8)
        self.assertEqual(pl["mode"], "sweep")
        self.assertEqual(pl["receivers"]["00001001"], None)
        self.assertEqual(pl["chunks"][0]["receivers"], ["sa-1"])
        self.assertEqual(pl["gaps"], [])

    def test_live_needs_a_narrow_view_that_one_receiver_tunes_whole(self):
        pl = sp.plan(433.52, 434.32, THREE, 0.8)
        self.assertEqual((pl["mode"], pl["step_khz"]), ("live", None))
        self.assertEqual(pl["live_receivers"], list(THREE))
        self.assertEqual(len(pl["chunks"]), 1)
        self.assertEqual(sp.plan(433, 434, THREE, 0.8)["mode"], "sweep")            # too wide for one capture
        edge = sp.plan(23.8, 24.4, THREE, 0.8)                                       # half of it is not tuned
        self.assertEqual((edge["mode"], edge["covered"], edge["gaps"]), ("sweep", [[24.0, 24.4]], [[23.8, 24.0]]))
        self.assertEqual(sp.plan(2437.0, 2437.5, THREE, 0.8)["mode"], "none")
        mixed = sp.plan(2437.0, 2437.5, dict(THREE, **WIDE), 0.8)
        self.assertEqual((mixed["mode"], mixed["live_receivers"]), ("live", ["sa-1"]))
        self.assertIsNone(mixed["receivers"]["00001001"])
        # a receiver that cannot make the direct capture sweeps a narrow view instead
        slow = sp.plan(2437.0, 2437.5, dict(THREE, **WIDE), 0.8, live_receivers=list(THREE))
        self.assertEqual((slow["mode"], slow["step_khz"], slow["live_receivers"]), ("sweep", 1.0, []))
        self.assertEqual(slow["chunks"][0]["receivers"], ["sa-1"])
        both = sp.plan(433.52, 434.32, dict(THREE, **WIDE), 0.8, live_receivers=list(THREE))
        self.assertEqual((both["mode"], both["live_receivers"]), ("live", list(THREE)))
        self.assertIsNone(both["receivers"]["sa-1"])              # left out of the fast view, not out of range

    def test_a_step_that_would_mean_too_many_points_is_raised_and_says_so(self):
        pl = sp.plan(24, 1766, RTL, 0.8, rbw_khz=1)
        self.assertTrue(pl["step_raised"])
        self.assertEqual(pl["step_asked_khz"], 1.0)
        self.assertEqual(pl["step_khz"], 25.0)                   # the smallest listed step that keeps it sane
        self.assertLessEqual(pl["bins"], sp.MAX_SWEEP_BINS)
        self.assertGreater(1742000 / 12.5, sp.MAX_SWEEP_BINS)    # ... the next smaller one would not
        self.assertIn("raised from 1 to 25 kHz", pl["step_note"])
        ok = sp.plan(88, 108, RTL, 0.8, rbw_khz="5")
        self.assertEqual((ok["step_khz"], ok["step_raised"], ok["step_auto"], ok["step_note"]), (5.0, False, False, ""))
        self.assertEqual(sp.plan(88, 108, RTL, 0.8, rbw_khz="auto")["step_khz"], 25.0)
        coarse = sp.plan(433, 435, RTL, 0.8, rbw_khz=1000)       # 2 points would be no picture at all
        self.assertTrue(coarse["step_lowered"])
        self.assertGreaterEqual(coarse["bins"], sp.MIN_SWEEP_BINS)
        self.assertIn("lowered", coarse["step_note"])
        finest = sp.plan(1765.99, 1766.5, RTL, 0.8, rbw_khz=1)   # 10 kHz of it is tuned: 10 points at the finest step
        self.assertEqual((finest["step_khz"], finest["step_lowered"], finest["step_note"]), (1.0, False, ""))
        for lo, hi in ((0, 6000), (24, 1766), (0.5, 30), (88, 108)):
            for rbw in sp.STEP_CHOICES_KHZ:
                for ranges in (RTL, WIDE):
                    p2 = sp.plan(lo, hi, ranges, 0.8, rbw_khz=rbw)
                    self.assertLessEqual(p2["bins"], sp.MAX_SWEEP_BINS, (lo, hi, rbw))

    def test_slivers_are_not_swept(self):
        pl = sp.plan(1765.995, 1800, RTL, 0.8)                   # 5 kHz of it is tuned: not worth opening a radio
        self.assertEqual((pl["mode"], pl["gaps"]), ("none", [[1765.995, 1800.0]]))

    def test_pieces_keep_their_width_and_share_of_points(self):
        pl = sp.plan(100, 700, RTL, 0.8, max_points=1200, max_chunk_mhz=150.0)
        self.assertEqual([(c["start"], c["stop"]) for c in pl["chunks"]],
                         [(100.0, 250.0), (250.0, 400.0), (400.0, 550.0), (550.0, 700.0)])
        self.assertEqual([c["points"] for c in pl["chunks"]], [300, 300, 300, 300])
        self.assertEqual(sp.cut(0, 500, 200), [[0.0, 166.6667], [166.6667, 333.3333], [333.3333, 500.0]])

    def test_bad_input_is_refused_in_plain_words(self):
        for args, word in (((None, 100), "start"), (("abc", 100), "number"), ((100, ""), "stop"),
                           ((200, 100), "below"), ((100, 100), "below"), ((-1, 100), "between 0 and 6000"),
                           ((100, 6000.5), "between 0 and 6000"), ((100, 100.001), "at least 10 kHz"),
                           ((float("nan"), 5), "number"), ((True, 5), "start")):
            with self.assertRaises(ValueError) as cm:
                sp.plan(args[0], args[1], RTL, 0.8)
            self.assertIn(word, str(cm.exception), args)
            self.assertNotIn("_", str(cm.exception))
        for rbw in ("fast", 0.5, 5000, float("nan")):
            with self.assertRaises(ValueError):
                sp.plan(88, 108, RTL, 0.8, rbw_khz=rbw)
            with self.assertRaises(ValueError):                  # also where nothing would be swept
                sp.plan(2400, 2500, RTL, 0.8, rbw_khz=rbw)
        with self.assertRaises(ValueError):
            sp.plan(88, 108, RTL, 0.8, max_points=4)


if __name__ == "__main__":
    unittest.main()
