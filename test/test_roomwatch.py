"""Room watch: the bands, the tracker's alerts and the report (arislib.roomwatch), then the engine's run on simulated
radios.  python3 -m unittest test_roomwatch"""
import json
import os
import time
import unittest
from unittest import mock

import harness  # noqa: F401  (fake radios, a data folder)
import load

from arislib import roomwatch as RW

A = load.aris
call = load.call


def ev(mhz, delta=20.0, pattern="steady", new=True, hint="a test band", by=None, bw=25.0, busy=False, **more):
    return dict({"peak_mhz": mhz, "surge_delta_db": delta, "pattern": pattern, "new_signal": new, "hint": hint,
                 "bandwidth_khz": bw, "busy": busy, "flagged_by": by if by is not None else {"Dongle 1": delta}},
                **more)


class Bands(unittest.TestCase):
    def test_the_bands_run_without_a_gap_from_25_mhz_to_6_ghz(self):
        for (_, hi, _a), (lo, _b, _c) in zip(RW.WATCH_BANDS, RW.WATCH_BANDS[1:]):
            self.assertEqual(hi, lo)
        self.assertEqual((RW.WATCH_BANDS[0][0], RW.WATCH_BANDS[-1][1]), (25.0, 6000.0))

    def test_chunks_are_at_most_200_mhz_and_keep_the_step_asked(self):
        for lo, hi, _ in RW.WATCH_BANDS:
            ch = RW.chunks(lo, hi, 10.0)
            self.assertAlmostEqual(ch[0][0], lo)
            self.assertAlmostEqual(ch[-1][1], hi)
            for s, e, step in ch:
                self.assertLessEqual(e - s, 200.0 + 1e-6)
                self.assertEqual(step, 10.0)             # (a wider bin would dilute a narrow carrier)
            for x, y in zip(ch, ch[1:]):
                self.assertAlmostEqual(x[1], y[0])
        self.assertEqual(len(RW.chunks(88.0, 108.0)), 1)
        self.assertEqual(len(RW.chunks(5150.0, 5925.0)), 4)
        self.assertGreater(RW.chunks(0.0, 200.0, 1.0)[0][2], 1.0)   # ... unless the sweep would have too many bins

    def test_coverage_and_the_ranges_no_receiver_tunes(self):
        cov = {c["label"]: c for c in RW.coverage(RW.WATCH_BANDS, [(24.0, 1766.0)])}
        self.assertEqual(cov["FM broadcast band"]["share"], 1.0)
        self.assertEqual(cov["2.4 GHz band"]["share"], 0.0)
        self.assertEqual(cov["VHF low and wireless microphones"]["parts"], [(25.0, 88.0)])
        self.assertTrue(all(c["share"] == 1.0 for c in RW.coverage(RW.WATCH_BANDS, [(24.0, 1766.0), (0.1, 12400.0)])))
        self.assertEqual(RW.gaps(RW.WATCH_BANDS, [(24.0, 1766.0)]), [(1766.0, 6000.0)])
        self.assertEqual(RW.gaps(RW.WATCH_BANDS, [(24.0, 1766.0), (0.1, 12400.0)]), [])
        self.assertEqual(RW.gaps(RW.WATCH_BANDS, [(30.0, 1766.0)]), [(25.0, 30.0), (1766.0, 6000.0)])
        self.assertEqual(RW.gaps(RW.WATCH_BANDS, []), [(25.0, 6000.0)])


class TrackerAlerts(unittest.TestCase):
    def test_a_new_steady_carrier_alerts_when_it_is_there_again_and_only_once(self):
        t = RW.Tracker()
        a = t.update([ev(433.5)], now=1000.0)
        self.assertEqual([(x["kind"], x["level"]) for x in a], [("first", "note")])
        self.assertIn("alert if a look within 20 minutes finds it again", a[0]["text"])
        self.assertEqual(t.update([ev(433.5, by={"Dongle 2": 18.0})], now=1010.0), [])     # the same moment
        a = t.update([ev(433.5)], now=1100.0)
        self.assertEqual([(x["kind"], x["level"]) for x in a], [("new", "alert")])
        self.assertIn("Worth a look", a[0]["text"])
        self.assertIn("there again now", a[0]["text"])
        self.assertIn("on Dongle 1", a[0]["text"])
        self.assertEqual((a[0]["over_db"], a[0]["by"]), (20.0, {"Dongle 1": 20.0}))
        self.assertEqual(t.update([ev(433.5)], now=1300.0), [])

    def test_a_strong_new_carrier_alerts_at_once_and_a_passing_call_does_not(self):
        t = RW.Tracker()
        a = t.update([ev(433.5, delta=30.0)], now=1000.0)
        self.assertEqual([(x["kind"], x["level"]) for x in a], [("new", "alert")])
        t2 = RW.Tracker()                                   # a two-way radio call: there once, never again
        self.assertEqual([x["level"] for x in t2.update([ev(155.3)], now=0.0)], ["note"])
        self.assertEqual(t2.update([ev(155.3)], now=4000.0), [])       # long after: a new first look, no alert
        self.assertEqual([x["kind"] for x in t2.update([ev(155.3)], now=4100.0)], ["new"])

    def test_weak_is_nothing_a_rise_on_an_occupied_frequency_alerts_and_bursts_are_notes(self):
        t = RW.Tracker()
        self.assertEqual(t.update([ev(100.0, delta=6.0)], now=1.0), [])
        t.update([ev(900.0, new=False)], now=1.0)           # something was there at the reference; now far more
        a = t.update([ev(900.0, new=False)], now=100.0)
        self.assertEqual([(x["kind"], x["level"]) for x in a], [("surge", "alert")])
        self.assertIn("already on this frequency", a[0]["text"])
        a = t.update([ev(915.2, pattern="intermittent")], now=2.0)
        self.assertEqual([(x["kind"], x["level"]) for x in a], [("bursts", "note")])
        t2 = RW.Tracker()                                    # a wide block that moves with its load is only noted
        self.assertEqual([x["level"] for x in t2.update([ev(620.0, delta=12.0, new=False, bw=6000.0)], now=0.0)], ["note"])
        self.assertEqual(t2.update([ev(620.0, delta=12.0, new=False, bw=6000.0)], now=100.0), [])
        self.assertEqual([x["kind"] for x in t2.update([ev(620.0, delta=16.0, new=False, bw=6000.0)], now=200.0)],
                         ["surge"])

    def test_growth_is_per_receiver_and_alerts_again_only_after_more_growth(self):
        t = RW.Tracker()
        t.update([ev(150.0, delta=12.0)], now=0.0)
        self.assertEqual([x["kind"] for x in t.update([ev(150.0, delta=12.0)], now=100.0)], ["new"])
        self.assertEqual(t.update([ev(150.0, delta=17.0)], now=300.0), [])
        a = t.update([ev(150.0, delta=23.0)], now=600.0)
        self.assertEqual([(x["kind"], x["level"]) for x in a], [("stronger", "alert")])
        self.assertIn("11 dB stronger on Dongle 1", a[0]["text"])
        self.assertEqual(t.update([ev(150.0, delta=25.0)], now=900.0), [])
        self.assertEqual([x["kind"] for x in t.update([ev(150.0, delta=29.0)], now=1200.0)], ["stronger"])

    def test_one_receivers_level_is_never_compared_with_anothers(self):
        t = RW.Tracker()
        t.update([ev(150.0, delta=12.0, by={"Dongle 1": 12.0})], now=0.0)
        t.update([ev(150.0, delta=12.0, by={"Dongle 1": 12.0})], now=100.0)
        # another receiver (another antenna, gain, even another scale) reads it 24 dB over its own reference
        self.assertEqual(t.update([ev(150.0, delta=24.0, by={"Dongle 4": 24.0})], now=300.0), [])
        self.assertEqual(t.update([ev(150.0, delta=24.0, by={"Dongle 1": 13.0, "Dongle 4": 24.0})], now=600.0), [])
        a = t.update([ev(150.0, delta=35.0, by={"Dongle 4": 35.0})], now=900.0)
        self.assertEqual([x["kind"] for x in a], ["stronger"])
        self.assertIn("on Dongle 4", a[0]["text"])

    def test_a_peak_that_wanders_is_one_carrier(self):
        t = RW.Tracker()
        t.update([ev(433.50)], now=0.0)
        self.assertEqual([x["kind"] for x in t.update([ev(433.53)], now=100.0)], ["new"])   # one grid cell on
        self.assertEqual(t.update([ev(433.51)], now=300.0), [])
        t.update([ev(98.10, bw=200.0)], now=0.0)
        self.assertEqual([x["kind"] for x in t.update([ev(98.18, bw=200.0)], now=100.0)], ["new"])
        self.assertEqual(len(t.update([ev(433.80)], now=600.0)), 1)                # 300 kHz away: another one
        self.assertEqual(len(t.freqs), 3)

    def test_in_a_phone_band_a_signal_alerts_only_when_it_stays(self):
        t = RW.Tracker()
        a = t.update([ev(1850.0, delta=30.0, busy=True)], now=0.0)                 # strong, but phones are
        self.assertEqual([(x["kind"], x["level"]) for x in a], [("busy", "note")])
        self.assertEqual(t.update([ev(1850.0, busy=True)], now=20.0), [])          # another receiver, same round
        self.assertEqual(t.update([ev(1850.0, busy=True)], now=300.0), [])
        a = t.update([ev(1850.0, busy=True)], now=600.0)
        self.assertEqual([(x["kind"], x["level"]) for x in a], [("held", "alert")])
        self.assertIn("was on in 3 looks over 10 min", a[0]["text"])     # what was measured, no more
        self.assertEqual(t.update([ev(1850.0, busy=True)], now=900.0), [])
        t2 = RW.Tracker()                                                          # a phone call: on, then gone
        t2.update([ev(1850.0, busy=True)], now=0.0)
        self.assertEqual(t2.update([ev(1850.0, busy=True)], now=3000.0), [])
        self.assertEqual(t2.update([ev(1850.0, busy=True)], now=3300.0), [])

    def test_bursts_that_turn_steady_alert_and_bursts_that_keep_coming_alert(self):
        t = RW.Tracker()
        t.update([ev(433.9, pattern="intermittent")], now=0.0)
        self.assertEqual([x["kind"] for x in t.update([ev(433.9)], now=300.0)], ["first"])
        a = t.update([ev(433.9)], now=400.0)
        self.assertEqual([(x["kind"], x["level"]) for x in a], [("new", "alert")])
        self.assertIn("first seen as bursts", a[0]["text"])
        t2 = RW.Tracker()                                    # a device that sends a burst every quarter of an hour
        self.assertEqual([x["kind"] for x in t2.update([ev(171.3, pattern="intermittent")], now=0.0)], ["bursts"])
        self.assertEqual(t2.update([ev(171.3, pattern="intermittent")], now=900.0), [])
        a = t2.update([ev(171.3, pattern="intermittent")], now=1900.0)
        self.assertEqual([(x["kind"], x["level"]) for x in a], [("recurring", "alert")])
        self.assertEqual(t2.update([ev(171.3, pattern="intermittent")], now=2800.0), [])
        t3 = RW.Tracker()                                    # in a sensor band that is every neighbour's sensor
        for i in range(4):
            a = t3.update([dict(ev(433.92, pattern="intermittent"), sensor=True)], now=900.0 * i)
        self.assertEqual(a, [])

    def test_one_of_many_hopping_bursts_is_recorded_but_not_told_and_memory_stays_bounded(self):
        t = RW.Tracker()
        self.assertEqual(t.update([dict(ev(915.0 + i * 0.05, pattern="intermittent"), quiet=True)
                                   for i in range(40)], now=0.0), [])
        self.assertEqual(len(t.freqs), 40)
        t.update([ev(433.5, delta=30.0)], now=1.0)
        for r in range(8):
            t.update([dict(ev(1000.0 + r * 30 + i * 0.05, pattern="intermittent"), quiet=True)
                      for i in range(400)], now=10.0 + r)
        self.assertLessEqual(len(t.freqs), 3 * RW.KEEP_FREQS)
        self.assertIn(RW._key(433.5), t.freqs)                # what alerted is never dropped

    def test_what_an_overloaded_receiver_sees_is_never_called_new(self):
        t = RW.Tracker()
        a = t.update([dict(ev(410.0, delta=30.0), overload=True)], now=0.0)
        self.assertEqual([(x["kind"], x["level"]) for x in a], [("overload", "note")])
        self.assertEqual(t.update([dict(ev(410.0, delta=30.0), overload=True)], now=100.0), [])
        self.assertEqual([x["kind"] for x in t.update([ev(410.0, delta=30.0)], now=200.0)], ["new"])   # still there after

    def test_an_accepted_carrier_raises_nothing_and_is_listed(self):
        t = RW.Tracker()
        t.update([ev(446.1, delta=30.0)], now=10.0)
        self.assertEqual(t.accept(446.11, "our radio")["mine"], "our radio")
        self.assertEqual(t.update([ev(446.1, delta=33.0)], now=300.0), [])
        self.assertEqual(t.accepted(), [{"mhz": 446.1, "label": "our radio"}])
        t.accept(462.5, "the building's handhelds")                  # accepted before it was ever heard
        self.assertEqual(t.update([ev(462.5, delta=30.0)], now=600.0), [])
        self.assertEqual(t.update([ev(462.5, delta=30.0, bw=200.0)], now=700.0), [])
        self.assertEqual(len(t.accepted()), 2)
        self.assertIsNotNone(t.unaccept(446.1))
        self.assertEqual(len(t.accepted()), 1)
        self.assertEqual(t.open_alerts(now=700.0), [{"mhz": 446.1, "first": 10.0, "last": 300.0, "count": 2,
                                                     "hint": "a test band", "receivers": ["Dongle 1"]}])

    def test_a_carrier_back_after_hours_is_a_note(self):
        t = RW.Tracker()
        t.update([ev(88.3, delta=30.0)], now=0.0)
        a = t.update([ev(88.3, delta=30.0)], now=3 * 3600.0)
        self.assertEqual([(x["kind"], x["level"]) for x in a], [("back", "note")])

    def test_state_round_trips_through_json_and_an_older_state_loads(self):
        t = RW.Tracker()
        t.update([ev(433.5, delta=30.0), ev(2437.0, pattern="intermittent")], now=50.0)
        t.accept(433.5, "mine")
        again = RW.Tracker.from_state(json.loads(json.dumps(t.state())))
        self.assertEqual(again.update([ev(433.5, delta=30.0)], now=400.0), [])        # still known and accepted
        self.assertEqual(len(again.freqs), 2)
        self.assertEqual(again.accepted()[0]["label"], "mine")
        old = RW.Tracker.from_state({"17340": {"mhz": 433.5, "first": 1.0, "last": 2.0, "count": 3,
                                               "first_db": -60.0, "alerted": {"new": 1.0}}})
        self.assertEqual(old.update([ev(433.5)], now=400.0), [])
        kept = RW.Tracker.from_state({"v": 2, "freqs": {"3924": {       # accepted before records had a width
            "mhz": 98.1, "first": 1.0, "last": 2.0, "count": 3, "alerted": {"new": 1.0}, "mine": "the station",
            "by": {"Dongle 1": {"first": 20.0, "last": 20.0, "max": 21.0}}}}})
        self.assertEqual(kept.update([ev(98.18, delta=22.0, bw=200.0)], now=400.0), [])   # its peak wanders
        self.assertEqual(len(kept.freqs), 1)

    def test_a_carrier_seen_in_looks_far_apart_alerts_as_coming_and_going(self):
        t = RW.Tracker()                                     # on for a minute or two every half hour, 18 dB
        got = [t.update([ev(151.3, delta=18.0)], now=1800.0 * i) for i in range(6)]
        self.assertEqual([[(x["kind"], x["level"]) for x in a] for a in got],
                         [[("first", "note")], [], [("recurring", "alert")], [], [], []])
        self.assertIn("has now been on in 3 looks since", got[2][0]["text"])
        self.assertIn("comes and goes", got[2][0]["text"])
        self.assertEqual([x["mhz"] for x in t.open_alerts(now=9000.0)], [151.3])
        t2 = RW.Tracker()                                    # in a phone band it takes more: 6 looks over 2 hours
        got = [t2.update([ev(1850.0, delta=45.0, busy=True)], now=960.0 * i) for i in range(12)]
        kinds = [[x["kind"] for x in a] for a in got]
        self.assertEqual(kinds[0], ["busy"])
        self.assertEqual([i for i, a in enumerate(kinds) if a == ["held"]], [8])        # 128 min after the first
        self.assertIn("was on in 9 looks over 2 h 8 min", got[8][0]["text"])
        t3 = RW.Tracker()                                    # a sensor band: steady in every fifth look
        got = []
        for i in range(16):
            got += t3.update([ev(433.92, pattern="steady" if i % 5 == 0 else "intermittent", sensor=True)],
                             now=300.0 * i)
        self.assertEqual([x["kind"] for x in got if x["level"] == "alert"], ["recurring"])
        t4 = RW.Tracker()                                    # twice in six hours is not enough
        self.assertEqual({x["level"] for i in range(3) for x in t4.update([ev(151.3)], now=5 * 3600.0 * i)},
                         {"note"})

    def test_a_carrier_seen_only_in_lifted_sweeps_is_told_after_three_of_them(self):
        t = RW.Tracker()
        got = [t.update([ev(49.86, delta=17.0, overload=True)], now=300.0 * i) for i in range(6)]
        self.assertEqual([[(x["kind"], x["level"]) for x in a] for a in got],
                         [[("overload", "note")], [], [("new", "alert")], [], [], []])
        self.assertIn("was on in 3 sweeps since", got[2][0]["text"])
        self.assertIn("may be the receiver's own mixing product", got[2][0]["text"])
        self.assertEqual(len(t.open_alerts(now=1500.0)), 1)
        self.assertEqual(t.update([ev(49.86, delta=17.0)], now=1900.0), [])            # told once
        t2 = RW.Tracker()                                    # sweeps of one minute: three of them are too close
        got = [t2.update([ev(49.86, delta=17.0, overload=True)], now=60.0 * i) for i in range(5)]
        self.assertEqual([x["level"] for a in got for x in a], ["note"])
        t3 = RW.Tracker()                                    # bursts seen in that state stay a single note
        got = [t3.update([ev(49.86, pattern="intermittent", overload=True)], now=600.0 * i) for i in range(6)]
        self.assertEqual([x["level"] for a in got for x in a], ["note"])

    def test_one_wide_signal_is_one_carrier_wherever_its_strongest_point_is(self):
        t = RW.Tracker()                                     # a flat 6 MHz signal: another peak at every look
        peaks = [503.55, 500.3, 505.8, 502.59, 504.76, 501.1, 503.8, 500.9, 505.2, 502.33]
        got = [t.update([ev(f, delta=33.0, bw=6000.0),
                         ev(499.9, delta=11.0, pattern="intermittent", bw=10.0),       # its edges come and go
                         ev(506.2 + 0.01 * i, delta=12.0, bw=360.0)], now=300.0 * i) for i, f in enumerate(peaks)]
        self.assertEqual([[(x["kind"], x["mhz"]) for x in a] for a in got], [[("new", 503.55)]] + [[]] * 9)
        self.assertIn("about 6.0 MHz wide", got[0][0]["text"])
        self.assertEqual(len(t.freqs), 1)
        self.assertEqual(t.accept(503.55, "the TV channel")["mhz"], 503.55)
        self.assertEqual([t.update([ev(f, delta=33.0, bw=6000.0)], now=4000.0 + i) for i, f in enumerate(peaks)],
                         [[]] * 10)
        self.assertEqual(len(t.freqs), 1)
        again = RW.Tracker.from_state(json.loads(json.dumps(t.state())))              # the width is in the state
        self.assertEqual(again.update([ev(501.7, delta=33.0, bw=6000.0)], now=5000.0), [])
        self.assertEqual(len(again.freqs), 1)
        t2 = RW.Tracker()                                    # two narrow carriers 1 MHz apart stay two
        t2.update([ev(450.0, delta=30.0), ev(451.0, delta=30.0)], now=0.0)
        self.assertEqual(len(t2.freqs), 2)
        t3 = RW.Tracker()                                    # a wide signal next to a narrow record is its own
        t3.update([ev(498.0, delta=30.0)], now=0.0)
        self.assertEqual([x["kind"] for x in t3.update([ev(501.0, delta=30.0, bw=6000.0)], now=300.0)], ["new"])
        t4 = RW.Tracker()                                    # a far peak of a wide signal names its own frequency
        t4.update([ev(620.0, delta=12.0, new=False, bw=6000.0)], now=0.0)              # (only noted: it was there)
        a = t4.update([ev(622.4, delta=16.0, new=False, bw=6000.0)], now=300.0)
        self.assertEqual([(x["kind"], x["mhz"]) for x in a], [("surge", 622.4)])
        self.assertEqual(t4.accept(622.4, "known")["mhz"], 620.0)                      # accepting it takes the whole
        self.assertEqual(len(t4.freqs), 1)
        t5 = RW.Tracker()                                    # a narrow carrier beside a wide signal that is off
        t5.update([ev(503.0, delta=33.0, bw=6000.0)], now=0.0)                         # now is no edge of it
        a = t5.update([ev(504.5, delta=20.0)], now=300.0) + t5.update([ev(504.5, delta=20.0)], now=600.0)
        self.assertEqual([(x["kind"], x["mhz"]) for x in a], [("first", 504.5), ("new", 504.5)])

    def test_what_an_accepted_carrier_silences_and_what_it_does_not(self):
        t = RW.Tracker()
        t.update([ev(446.1, delta=18.0)], now=0.0)
        t.update([ev(446.1, delta=18.0)], now=100.0)
        t.accept(446.1, "our radio")
        self.assertEqual(t.update([ev(446.1, delta=24.0)], now=300.0), [])             # its level moves a little
        a = t.update([ev(446.1, delta=43.0)], now=600.0)                               # far stronger: a second one?
        self.assertEqual([(x["kind"], x["level"]) for x in a], [("stronger", "alert")])
        self.assertIn("The signal you accepted at 446.1 MHz (our radio) reads 25 dB stronger on Dongle 1",
                      a[0]["text"])
        self.assertEqual(t.update([ev(446.1, delta=44.0)], now=900.0), [])             # ... told once
        self.assertEqual([x["kind"] for x in t.update([ev(446.25, delta=35.0, bw=300.0)], now=1200.0)], ["new"])
        self.assertEqual([x["kind"] for x in t.update([ev(446.2, delta=35.0)], now=1500.0)], ["new"])
        self.assertEqual(t.accepted(), [{"mhz": 446.1, "label": "our radio"}])
        t.update([ev(446.1, delta=43.0, by={"Dongle 2": 43.0})], now=1800.0)           # a receiver new to it: its
        self.assertEqual(t.update([ev(446.1, delta=44.0, by={"Dongle 2": 44.0})], now=2100.0), [])     # own level
        self.assertEqual(t.unaccept(446.1)["mhz"], 446.1)
        t.accept(446.1, "our radio")                                                   # accepted again as it is now
        self.assertEqual(t.update([ev(446.1, delta=44.0)], now=2400.0), [])

    def test_an_accept_typed_with_a_rounded_frequency_takes_the_carrier_that_alerted(self):
        t = RW.Tracker()
        t.update([ev(446.1375, delta=30.0)], now=0.0)
        rec = t.accept(446.1, "our radio")
        self.assertEqual((rec["mhz"], rec["mine"]), (446.1375, "our radio"))           # what was really accepted
        self.assertEqual(t.accepted(), [{"mhz": 446.1375, "label": "our radio"}])
        self.assertEqual(t.update([ev(446.1375, delta=31.0)], now=7 * 3600.0), [])
        self.assertEqual(t.open_alerts(now=7 * 3600.0), [])
        self.assertEqual(t.accept(446.3, "another")["mhz"], 446.3)                     # nothing near: as typed
        self.assertEqual(t.unaccept(446.14)["mhz"], 446.1375)
        self.assertIsNone(t.unaccept(446.14))

    def test_bursts_that_keep_coming_back_alert_also_among_many_other_bursts(self):
        t = RW.Tracker()
        got = [t.update([ev(418.4, delta=29.0, pattern="intermittent", quiet=True)]
                        + [ev(430.0 + 0.3 * j + 0.05 * i, pattern="intermittent", quiet=True) for j in range(7)],
                        now=600.0 * i) for i in range(6)]
        self.assertEqual([[(x["kind"], x["mhz"]) for x in a] for a in got],
                         [[], [], [], [("recurring", 418.4)], [], []])
        self.assertIn("so do short calls on a shared radio channel", got[3][0]["text"])

    def test_the_trigger_given_at_the_start_is_the_alert_level(self):
        self.assertEqual(RW.Tracker().update([ev(420.5, delta=8.0)], now=0.0), [])
        t = RW.Tracker(new_db=6.0)
        self.assertEqual([x["kind"] for x in t.update([ev(420.5, delta=8.0)], now=0.0)], ["first"])
        self.assertEqual([x["kind"] for x in t.update([ev(420.5, delta=8.0)], now=100.0)], ["new"])
        self.assertEqual(t.update([ev(421.5, delta=5.0)], now=100.0), [])
        again = RW.Tracker.from_state(json.loads(json.dumps(t.state())), new_db=6.0)
        self.assertEqual([x["kind"] for x in again.update([ev(425.0, delta=7.0)], now=200.0)], ["first"])
        self.assertEqual((RW.Tracker(new_db=1.0).new_db, RW.Tracker().new_db, RW.Tracker(new_db=15).new_db),
                         (4.0, RW.NEW_ALERT_DB, 15.0))
        t = RW.Tracker(new_db=15.0)                          # a higher trigger: 12 dB is no alert
        self.assertEqual(t.update([ev(420.5, delta=12.0)], now=0.0) + t.update([ev(420.5, delta=12.0)], now=100.0), [])

    def test_growth_counts_from_the_highest_reading_up_to_the_first_alert(self):
        t = RW.Tracker()
        t.update([ev(150.0, delta=14.0)], now=0.0)           # the first look caught it in a fade
        self.assertEqual([x["kind"] for x in t.update([ev(150.0, delta=27.0)], now=100.0)], ["new"])
        self.assertEqual(t.update([ev(150.0, delta=33.0)], now=400.0), [])              # 19 over the first reading
        a = t.update([ev(150.0, delta=38.0)], now=700.0)
        self.assertEqual([x["kind"] for x in a], ["stronger"])
        self.assertIn("11 dB stronger on Dongle 1 than in any look up to its first alert", a[0]["text"])

    def test_sentences_say_what_was_measured_and_which_day(self):
        t = RW.Tracker()
        t.update([ev(88.3, delta=30.0)], now=1000.0)
        a = t.update([ev(88.3, delta=30.0)], now=1000.0 + 30 * 3600.0)
        self.assertEqual([x["kind"] for x in a], ["back", "still"])
        self.assertIn("was seen again after 30.0 h without a sighting", a[0]["text"])
        day = time.strftime("%a %H:%M", time.localtime(1000.0))
        self.assertIn("First heard " + day, a[0]["text"])                              # not today: the weekday too
        self.assertIn("first heard " + day, a[1]["text"])
        self.assertEqual(RW._when(1000.0, 1060.0), time.strftime("%H:%M", time.localtime(1000.0)))
        self.assertEqual((RW._span(45), RW._span(600), RW._span(7680)), ("45 s", "10 min", "2 h 8 min"))

    def test_the_lists_a_record_keeps_stay_short(self):
        t = RW.Tracker()
        for i in range(400):
            t.update([ev(151.3, delta=12.0, new=False, bw=400.0), ev(160.0, pattern="intermittent", sensor=True),
                      ev(49.86, overload=True), ev(1850.0, busy=True, pattern="intermittent")], now=310.0 * i)
        for rec in t.freqs.values():
            for name in ("steady_t", "seen_t", "over_t", "burst_t"):
                self.assertLessEqual(len(rec.get(name) or []), RW.LONG_KEEP, name)
        self.assertLess(len(json.dumps(t.state())), 4000)

    def test_bad_events_are_skipped(self):
        t = RW.Tracker()
        self.assertEqual(t.update([{}, {"peak_mhz": "x"}, None, {"peak_mhz": 10.0},
                                   {"peak_mhz": float("nan"), "surge_delta_db": 20.0}], now=1.0), [])


class Report(unittest.TestCase):
    def test_report_lists_days_gaps_and_the_limit(self):
        now = time.time()
        rows = [{"t": now, "time": "2026-03-14 09:00:00", "kind": "start", "text": "Started"},
                {"t": now + 60, "time": "2026-03-14 09:01:00", "kind": "alert", "text": "New steady signal"},
                {"t": now + 90000, "time": "2026-03-15 11:00:00", "kind": "mark", "text": "break"}]
        r = RW.report(rows, {"started": "2026-03-14 09:00", "running": True, "rounds": 5, "alerts_total": 1,
                             "not_covered": ["2.4 GHz band"]})
        self.assertEqual(len(r["days"]), 2)
        self.assertIn("ALERT", r["text"])
        self.assertIn("Not covered by any receiver here: 2.4 GHz band", r["text"])
        self.assertIn("does not prove", r["text"])

    def test_summary_line(self):
        self.assertEqual(RW.summary_line({"rounds": 4, "alerts_total": 0}), "4 rounds, no alerts.")
        line = RW.summary_line({"rounds": 4, "round_s": 200, "alerts_total": 0, "notes_total": 3})
        self.assertEqual(line, "4 rounds (a round takes 3 min 20 s), no alerts; 3 notes in the timeline.")
        self.assertNotIn("nothing new", line)                # notes are in the timeline: "nothing" would be wrong
        self.assertIn("2 alerts", RW.summary_line({"rounds": 4, "alerts_total": 2, "last_alert": "10:00"}))
        self.assertIn("No round", RW.summary_line({}))

    def test_summary_and_closing_sentence_claim_no_more_than_was_seen(self):
        r = RW.report([], {"rounds": 2, "alerts_total": 0, "notes_total": 1})
        self.assertIn("2 rounds, no alerts; 1 note in the timeline.", r["text"])
        self.assertNotIn("digital and encrypted", r["text"])
        self.assertIn("hop, spread under the noise or send only short bursts can be missed", r["text"])

    def test_a_reference_recorded_late_is_never_left_out(self):
        t0 = time.mktime((2026, 3, 14, 9, 0, 0, 0, 0, -1))
        stamp = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
        rows = [{"t": t0 + 3300, "time": stamp(t0 + 3300), "kind": "late",
                 "text": "150-153 MHz on Dongle 2: the reference was recorded late."}]
        rows += [{"t": t0 + 3600 + i, "time": stamp(t0 + 3600 + i), "kind": "note", "text": f"burst {i}"}
                 for i in range(200)]
        late = [{"s": 150.0 + 3 * i, "e": 153.0 + 3 * i, "dongle": "Dongle 2", "time": "2026-03-14 09:55"}
                for i in range(14)]
        r = RW.report(rows, {"rounds": 5, "watch_since": t0 + 660, "late_refs": late})
        self.assertIn("reference: 150-153 MHz on Dongle 2: the reference was recorded late.", r["text"])
        self.assertIn("the first 50 are left out here", r["text"])
        self.assertNotIn("burst 49", r["text"].split("burst 50")[0])
        head = r["text"].split("\n\n")[0]
        self.assertIn("The reference was complete at 2026-03-14 09:11, except for the parts recorded later.", head)
        self.assertIn("Recorded later (what was on the air then counts as normal there): 150-153 MHz on Dongle 2 "
                      "at 03-14 09:55; 153-156 MHz on Dongle 2 at 03-14 09:55;", head)
        self.assertIn("; and 2 more.", head)
        self.assertLess(head.index("The reference was complete"), head.index("Recorded later"))
        self.assertIn("200 notes, 0 marks, 0 gaps, 1 parts of the reference recorded late.", r["short"])
        plain = RW.report(rows[1:], {"rounds": 5, "watch_since": t0 + 660})["text"]
        self.assertIn("The reference was complete at 2026-03-14 09:11. Anything already transmitting then", plain)
        self.assertNotIn("Recorded later", plain)

    def test_rows_of_earlier_watches_are_counted_not_shown(self):
        now = time.time()
        stamp = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
        row = lambda t, kind, text: {"t": t, "time": stamp(t), "kind": kind, "text": text}
        rows = [row(now - 90000, "start", "The earlier watch started"),
                row(now - 89000, "alert", "An alert of the earlier watch"),
                row(now - 88000, "mark", "a mark of the earlier watch"),
                row(now - 600, "start", "Started"), row(now - 300, "alert", "New steady signal")]
        r = RW.report(rows, {"rounds": 3, "alerts_total": 1, "log_since": now - 600})
        self.assertNotIn("earlier watch", r["text"].replace("earlier rows of previous watches", ""))
        self.assertNotIn("An alert of the earlier watch", r["short"])
        self.assertIn("3 earlier rows of previous watches are not shown here.", r["text"])
        self.assertIn("3 earlier rows of previous watches are not shown here.", r["short"])
        self.assertEqual(sum(len(d["lines"]) for d in r["days"]), 2)
        self.assertEqual(r["text"].count("ALERT"), 1)
        self.assertIn(": 1 alerts,", r["short"])
        self.assertEqual(RW.report(rows, {"rounds": 3})["text"].count("ALERT"), 2)       # no start given: all rows

    def test_header_names_other_receiver_types_and_time_not_watched(self):
        st = {"rounds": 3, "bands": [{"s": 25.0, "e": 88.0, "step": 10.0, "dongles": ["Dongle 1", "Dongle 4"]}],
              "interval_min": 5, "trigger_db": 10.0}
        self.assertNotIn("Receivers:", RW.report([], dict(st, receiver_types={"Dongle 1": "RTL-SDR"}))["text"])
        self.assertNotIn("Not watched for", RW.report([], dict(st, gaps_s=40))["text"])
        r = RW.report([], dict(st, gaps_s=7500, receiver_types={"Dongle 4": "another kind", "Dongle 1": "RTL-SDR"}))
        self.assertIn("Receivers: Dongle 1: RTL-SDR; Dongle 4: another kind.", r["text"])
        self.assertIn("Not watched for 2 h 5 min in total (the gaps are in the timeline).", r["text"])
        self.assertIn("Watched: 1 band on Dongle 1, Dongle 4;", r["text"])

    def test_inventory_counts_own_clock_carriers_and_prints_notes(self):
        lines = RW.inventory_lines({"total": 9, "fm": 3, "own": 2, "more": 1, "listed": [
            {"mhz": 151.4, "db": 31.2, "receivers": ["Dongle 1"], "band": "VHF two-way radio"},
            {"mhz": 94.8, "db": 22.0, "receivers": ["Dongle 1", "Dongle 2"], "band": "FM broadcast band",
             "note": "FM band, off the station channels"},
            {"mhz": 300.0, "db": 12.0, "note": "seen on one receiver only"}]})
        self.assertEqual(lines[0], "On the air when the reference was recorded: 9 steady signals (3 FM stations, "
                                   "2 at multiples of the receivers' own clock).")
        self.assertIn("never alerts", lines[1])
        self.assertEqual(lines[2:], ["  151.4 MHz, 31 dB over noise, Dongle 1: VHF two-way radio",
                                     "  94.8 MHz, 22 dB over noise, Dongle 1 and Dongle 2: FM broadcast band; "
                                     "FM band, off the station channels",
                                     "  300 MHz, 12 dB over noise: seen on one receiver only",
                                     "  and 1 weaker ones."])


class Run(unittest.TestCase):
    """A watch on simulated radios: the reference, rounds with nothing new, a carrier that appears, and what keeps
    it honest (failed rounds, a deaf receiver, a restart, Reset Scanner, carrying on)."""

    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ("FAKE_IQ", "FAKE_PEAKS", "FAKE_ANT")}
        cfg = dict(A._load_config())
        cfg.update(baseline_seconds=2, audit_seconds=2)
        self.cfg = cfg
        self._p = mock.patch.object(A, "_load_config", lambda: dict(cfg))
        self._p.start()
        self._g = mock.patch.object(RW, "CONFIRM_GAP_S", 0.0)       # (rounds follow each other in seconds here)
        self._g.start()
        for f in (A.ROOM_STATE, A.ROOM_STATE_BAK, A.ROOM_LOG):
            try:
                f.unlink()
            except OSError:
                pass
        A._ROOM_ROWS_CACHE.update(key=None, rows=[])

    def tearDown(self):
        w = A._room_here()
        if w:
            w.stop()
        self._g.stop()
        self._p.stop()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        A._ROOM = None

    def _appears(self):
        """A steady carrier at 434.4 MHz comes on: the sweeps see it, and so do the quick looks."""
        os.environ["FAKE_IQ"] = self._env["FAKE_IQ"] + ",434.4:cw:25"
        os.environ["FAKE_PEAKS"] = self._env["FAKE_PEAKS"] + ",434.4:55:20"

    def _watch(self, bands, hours=1.0, **kw):
        cfg = A._load_config()
        plan = A._room_plan(A._working_serials(cfg), cfg, bands)
        self.assertTrue(plan["chunks"])
        w = A.RoomWatch(plan, hours, 5.0, 10.0, True, only=bands, **kw)
        w.resumed = True
        return w

    def _ready(self, bands=((433.0, 435.0),)):
        w = self._watch([list(b) for b in bands])
        self.assertEqual(w._reference(A._room_pairs(w.plan)), 0, list(w.errors))
        w.phase, w.watch_since = "watching", time.time()
        return w

    def test_new_carrier_is_an_alert_with_numbers_a_log_row_and_a_notice(self):
        w = self._ready()
        n0 = len(open(A.NOTICES).read().splitlines()) if A.NOTICES.exists() else 0
        w._round()
        self.assertEqual((w.alerts_total, w.rounds, w.sweeps_last), (0, 1, [3, 3]), list(w.errors))
        self._appears()
        w._round()
        self.assertGreaterEqual(w.alerts_total, 1, list(w.errors))
        alerts = [r for r in A._room_rows() if r.get("kind") == "alert"]
        self.assertEqual(len(alerts), 1, alerts)                    # three receivers heard it: one alert
        self.assertAlmostEqual(alerts[0]["mhz"], 434.4, delta=0.1)
        self.assertIn("Worth a look", alerts[0]["text"])
        self.assertGreaterEqual(alerts[0]["over_db"], 10.0)
        self.assertTrue(alerts[0]["by"])
        self.assertGreater(len(open(A.NOTICES).read().splitlines()), n0)
        rows = A._room_rows()
        looks = [i for i, r in enumerate(rows) if r.get("what") == "look"]
        self.assertTrue(looks and looks[0] > rows.index(alerts[0]), rows)      # the alert first, then its look
        self.assertNotIn("Quick look", alerts[0]["text"])
        self.assertEqual(len(A._room_unseen()), 1)
        self.assertEqual(A.ack_room_watch("the admin account")["acknowledged"], 1)
        self.assertIn("seen by the admin account", A._room_rows()[-1]["text"])
        self.assertEqual(A._room_unseen(), [])
        w._round()
        self.assertEqual(len([r for r in A._room_rows() if r.get("kind") == "alert"]), 1)   # no second alert
        w._save()
        st = A._room_state_read()
        self.assertEqual(st["rounds"], 3)
        self.assertEqual(st["tracker"]["v"], 2)
        self.assertGreater(st["round_s"], 0)

    def test_a_round_in_which_no_sweep_worked_is_not_a_round_and_is_said_the_second_time(self):
        w = self._ready()
        w._round()
        self.assertEqual(w.rounds, 1)
        with mock.patch.object(A, "_audit", side_effect=A.RFError("rtl_power gave no data")):
            w._round()
            self.assertEqual((w.rounds, w.failed_rounds, w.fail_streak), (1, 1, 1))
            self.assertFalse([r for r in A._room_rows() if r.get("kind") == "health"])    # once can be chance
            w._round()
        self.assertEqual((w.rounds, w.failed_rounds), (1, 2))
        health = [r for r in A._room_rows() if r.get("kind") == "health"]
        self.assertEqual(len(health), 1)
        self.assertIn("no sweep worked in the last 2 rounds", health[0]["text"])
        self.assertIn("Nothing is being watched", health[0]["text"])
        self.assertEqual(len(A._room_unseen()), 1)                  # it rings like an alert
        self.assertNotIn("nothing new", A.roomwatch.summary_line({"rounds": 0}))
        w._round()
        self.assertEqual((w.rounds, w.fail_streak), (2, 0))

    def test_busy_receivers_are_a_gap_in_the_timeline_not_an_error(self):
        w = self._ready()
        real = A._audit

        def audit(s, e, ser, *a, **k):
            if ser == "00017350":
                raise A.RFError("Dongle 1 is busy (Decode). Try again shortly.")
            return real(s, e, ser, *a, **k)
        with mock.patch.object(A, "_audit", audit):
            w._round()
        self.assertEqual((w.rounds, w.sweeps_last), (1, [2, 3]))
        self.assertTrue(any(r["kind"] == "skip" and "1 sweep of 3 skipped" in r["text"] for r in A._room_rows()))
        self.assertIn("2 of 3 sweeps worked", A._room_status_line({**w.snapshot(), "running": True}))

    def test_a_band_nobody_checked_for_half_an_hour_is_said(self):
        w = self._ready()
        w._round()
        w.watch_since = w.born = time.time() - 3600
        w.chunk_ok = {k: time.time() - 2400 for k in w.chunk_ok}
        with mock.patch.object(A, "_audit", side_effect=A.RFError("Dongle 1 is busy (a capture). Try again shortly.")):
            w._round()
            w._round()
        self.assertTrue(w.unwatched)
        self.assertTrue([r for r in A._room_rows() if r.get("kind") == "health"])
        w._round()
        self.assertEqual(w.unwatched, [])
        self.assertTrue(any("checked again" in r["text"] for r in A._room_rows()))

    def test_a_receiver_that_lost_its_antenna_is_noticed(self):
        os.environ["FAKE_ANT"] = "00017350:fm=0;00026410:fm=-3;00035720:fm=-6"
        w = self._ready(bands=((88.0, 108.0),))
        w._round()
        w._round()
        self.assertFalse([r for r in A._room_rows() if r.get("kind") == "health"], list(w.errors))
        os.environ["FAKE_ANT"] = "00017350:fm=-45;00026410:fm=-3;00035720:fm=-6"       # Dongle 1's antenna is off
        w._round()
        self.assertFalse([r for r in A._room_rows() if r.get("kind") == "health"])       # once can be chance
        w._round()
        health = [r for r in A._room_rows() if r.get("kind") == "health"]
        self.assertEqual(len(health), 1, health)
        self.assertIn("Dongle 1 no longer hears", health[0]["text"])
        self.assertIn("Dongle 1", w.snapshot()["deaf"])
        os.environ["FAKE_ANT"] = "00017350:fm=0;00026410:fm=-3;00035720:fm=-6"
        w._round()
        self.assertTrue(any("hears its reference" in r["text"] for r in A._room_rows()))

    def test_what_was_on_the_air_at_the_reference_is_listed(self):
        os.environ["FAKE_ANT"] = "00017350:fm=0,915=0;00026410:fm=-3,915=0;00035720:fm=-6,915=0"
        os.environ["FAKE_PEAKS"] = self._env["FAKE_PEAKS"] + ",151.4:60:12"
        w = self._ready(bands=((88.0, 108.0), (150.0, 153.0)))
        w._inventory()
        inv = w.inventory
        self.assertGreaterEqual(inv["fm"], 3, inv)
        self.assertTrue(any(abs(x["mhz"] - 151.4) < 0.05 for x in inv["listed"]), inv)
        self.assertFalse(any(88.0 <= x["mhz"] <= 108.0 and abs(x["mhz"] * 10 % 2 - 1) < 0.2 for x in inv["listed"]))
        text = A.roomwatch.report(A._room_rows(), w.snapshot())["text"]
        self.assertIn("On the air when the reference was recorded", text)
        self.assertIn("151.4", text)
        self.assertIn("never alerts", text)

    def test_state_resumes_without_a_second_alert(self):
        w = self._ready()
        self._appears()
        w._round()
        n = w.alerts_total
        self.assertGreaterEqual(n, 1)
        w._save()
        st = A._room_state_read()
        cfg = A._load_config()
        w2 = A.RoomWatch(A._room_plan(A._working_serials(cfg), cfg, [[433.0, 435.0]]), 0, 5.0, 10.0, False,
                         state=st, only=[[433.0, 435.0]])
        self.assertEqual((w2.rounds, w2.phase), (1, "watching"))
        w2._round()
        self.assertEqual(w2.alerts_total, n)

    def test_a_watch_interrupted_by_a_restart_is_resumed_and_a_stale_pid_is_not_a_running_watch(self):
        w = self._ready()
        w._round()
        st = w.snapshot()
        st.update(running=True, pid=os.getppid(), beat=time.time() - 4000)      # a live pid, but not a watch's
        A._atomic_write_json(A.ROOM_STATE, st)
        self.assertIsNone(A._room_elsewhere())
        d = A.room_watch_status_dict()
        self.assertTrue(d["interrupted"])
        self.assertIn("interrupted", A._room_status_line(d))
        self.assertEqual(A.resume_room_watch(), "room watch")
        try:
            self.assertTrue(A._room_here())
            self.assertEqual(A._room_here().rounds, 1)
            self.assertTrue(any(r["kind"] in ("skip", "health") and "ARIS was not running" in r["text"]
                            for r in A._room_rows()))
            self.assertEqual(A.resume_room_watch(), "")                         # never a second one
        finally:
            A._room_here().stop()
        st.update(running=True, deadline=time.time() - 5)
        A._atomic_write_json(A.ROOM_STATE, st)
        self.assertEqual(A.resume_room_watch(), "")
        self.assertFalse(A._room_state_read()["running"])
        try:
            A.ROOM_STATE_BAK.unlink()
        except OSError:
            pass
        for bad in ("{", "[]", '{"running": true, "deadline": "x"}'):
            A.ROOM_STATE.write_text(bad)
            self.assertEqual(A.resume_room_watch(), "")
            self.assertFalse(A.room_watch_status_dict().get("running"))

    def test_a_state_file_emptied_by_a_power_cut_is_read_from_the_copy_of_the_last_round(self):
        w = self._ready()
        w._round()                                              # a finished round leaves a copy of the state
        self.assertTrue(A.ROOM_STATE_BAK.exists())
        st = A._room_state_read()
        self.assertNotIn("from_backup", st)
        A.ROOM_STATE.write_text("")                             # what a power cut can leave
        back = A._room_state_read()
        self.assertTrue(back["from_backup"])
        self.assertEqual(back["rounds"], 1)
        A.ROOM_STATE.unlink()                                   # removed on purpose (Clear data): no watch
        self.assertEqual(A._room_state_read(), {})

    def test_a_crash_in_the_reference_records_only_what_is_missing(self):
        w = self._watch([[433.0, 435.0]])
        pairs = sorted(A._room_pairs(w.plan))
        w._reference({pairs[0]})                                 # ... and then the computer lost power
        w._save()
        st = A._room_state_read()
        self.assertEqual(len(st["ref_done"]), 1)
        self.assertTrue(st["new_reference"])
        cfg = A._load_config()
        w2 = A.RoomWatch(A._room_plan(A._working_serials(cfg), cfg, [[433.0, 435.0]]), 0, 5.0, 10.0, False,
                         state=st, only=[[433.0, 435.0]])
        self.assertEqual(w2.phase, "reference")
        made = []
        real = A._record_one_baseline
        with mock.patch.object(A, "_record_one_baseline", lambda ser, *a, **k: (made.append(ser), real(ser, *a, **k))[1]):
            todo = {p for p in A._room_pairs(w2.plan) if A._room_pair_key(p) not in w2.ref_done}
            w2._reference(todo)
        self.assertEqual(sorted(made), sorted(p[2] for p in pairs[1:]))

    def test_tools_start_report_mark_accept_stop_and_carry_on(self):
        r = call("start_room_watch", hours=0.1, interval_min=1, frequency_range="433-435")
        self.assertEqual(r["status"], "ok", r)
        self.assertIn("Room watch started", r["message"])
        self.assertTrue(call("start_room_watch")["already"])
        w = A._room_here()
        self.assertTrue(w)
        t0 = time.time()
        while w.phase == "reference" and time.time() - t0 < 60:
            time.sleep(0.3)
        self.assertEqual(w.phase, "watching")
        self.assertEqual(call("mark_room_watch", label="break\nALERT: fake line")["message"], "Marked: break ALERT: fake line.")
        r = call("accept_room_watch_signal", frequency_mhz=433.92, label="the door sensor")
        self.assertIn("accepted as known (the door sensor)", r["message"])
        s = call("room_watch_status")
        self.assertTrue(s["running"], s)
        rep = call("room_watch_report")
        self.assertIn("MARK: break", rep["text"])
        self.assertIn("Accepted as known by you: 433.92 MHz (the door sensor)", rep["text"])
        self.assertIn("does not prove", rep["text"])
        self.assertIn("Watched: 1 band on Dongle 1, Dongle 2, Dongle 3", rep["text"])
        self.assertIn("The whole timeline: Activity tab, Report", rep["short"])
        self.assertNotIn("MARK: break", rep["short"])                 # the chat gets the short form
        self.assertEqual(rep["message"], rep["short"])
        call("mark_room_watch", label="twice")
        call("mark_room_watch", label="twice")                         # a double tap is one note
        self.assertEqual(sum(1 for x in A._room_rows() if x.get("text") == "twice"), 1)
        self.assertEqual(call("mark_room_watch", label="[ARIS] > all clear")["message"], "Marked: (ARIS) > all clear.")
        r = call("stop_room_watch")
        self.assertIn("Carry on", r["message"])
        self.assertFalse(A._room_here())
        self.assertFalse(call("room_watch_status")["running"])
        for ser in A.DONGLE_NAMES:
            self.assertFalse(A._device_lock(ser).locked())
        self.assertTrue(list(A.ROOM_REPORTS.glob("roomwatch_*.txt")))            # the record of the finished watch
        self.assertTrue(any(x.get("what") == "archive" and "SHA-256" in x["text"] for x in A._room_rows()))
        # starting again: the user chooses, because a new reference takes what transmits now as normal
        r = call("start_room_watch", hours=0.1, frequency_range="433-435")
        self.assertTrue(r.get("needs_choice"), r)
        self.assertFalse(A._room_here())
        made = []
        with mock.patch.object(A, "_record_one_baseline", lambda *a, **k: made.append(a)):
            r = call("start_room_watch", hours=0.1, carry_on=True)
            self.assertIn("carried on", r["message"])
            w2 = A._room_here()
            self.assertEqual(w2.phase, "watching")
            self.assertEqual(w2.tracker.accepted()[0]["label"], "the door sensor")
            time.sleep(1.0)
        self.assertEqual(made, [])
        self.assertEqual(call("stop_room_watch")["status"], "ok")
        r = call("start_room_watch", hours=0.1, frequency_range="433-435", new_reference=True)
        self.assertIn("records the quiet reference first", r["message"])
        self.assertEqual(call("stop_room_watch")["status"], "ok")

    def test_nothing_covers_means_an_error_in_plain_words(self):
        with mock.patch.object(A, "_room_plan", lambda *a, **k: {"chunks": [], "not_covered": ["2.4 GHz band"],
                                                                 "partly_covered": []}):
            r = call("start_room_watch", hours=1)
        self.assertEqual(r["status"], "error")
        self.assertIn("No receiver here tunes", r["error"])

    def test_reset_scanner_keeps_the_watch_and_clear_logs_keeps_the_state(self):
        r = call("start_room_watch", hours=0.1, interval_min=1, frequency_range="433-435")
        self.assertEqual(r["status"], "ok", r)
        w = A._room_here()
        out = A.stop_everything(wait_s=1.0)
        self.assertTrue(out["room_watch_kept"])
        self.assertNotIn("the room watch", out["stopped"])
        self.assertTrue(A._room_here())
        self.assertGreater(w.hold_until, time.time())
        self.assertFalse(out["stuck_here"], out)
        A._room_log("note", "x")
        w._save()
        self.assertNotIn(A.ROOM_LOG, A._log_files())           # a watch in progress keeps its timeline
        self.assertNotIn(A.ROOM_STATE, A._log_files())
        w.stop()
        self.assertIn(A.ROOM_LOG, A._log_files())
        self.assertNotIn(A.ROOM_STATE, A._log_files())

    def test_the_alert_address_gets_a_fixed_sentence_unless_detail_is_on(self):
        import http.server
        import threading
        got = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                got.append((self.path, self.headers.get("Title"), self.rfile.read(int(self.headers["Content-Length"])).decode()))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_address[1]}/secret-topic"
        cfg = self.cfg
        try:
            with mock.patch.object(A, "_save_config", lambda c: cfg.update(c)):
                self.assertFalse(A._room_push("x", wait=True))                    # off unless set
                with self.assertRaises(A.RFError):
                    A.set_room_alert_address("ftp://x")
                st = A.set_room_alert_address(url)
                self.assertEqual((st["set"], st["host"], st["detail"]), (True, "127.0.0.1", False))
                self.assertNotIn("secret-topic", json.dumps(st))
                A._room_push("Room watch: New steady signal at 434.4 MHz", wait=True)
                self.assertEqual(got[-1], ("/secret-topic", "ARIS room watch", A.ROOM_PUSH_QUIET))
                A.set_room_alert_address(url, detail=True)
                A._room_push("Room watch: New steady signal at 434.4 MHz", wait=True)
                self.assertIn("434.4", got[-1][2])
                srv.shutdown()
                srv.server_close()
                A._room_push("x", wait=True)
                self.assertTrue(A.room_alert_address()["error"])
                self.assertNotIn("secret-topic", str(A.room_alert_address()))
                self.assertFalse(A.set_room_alert_address("")["set"])
        finally:
            cfg.update(room_alert_url="", room_alert_detail=False)

    def test_all_is_well_twice_a_day(self):
        w = self._ready()
        w._round()
        now = time.time()
        lt = time.localtime(now)
        with mock.patch.object(A, "ROOM_SUMMARY_HOURS", (lt.tm_hour,)):
            w.watch_since = now - 7200
            w._summary(now)
            w._summary(now + 60)
        rows = [r for r in A._room_rows() if r.get("what") == "summary"]
        self.assertEqual(len(rows), 1)
        self.assertIn("Every band the receivers tune was checked", rows[0]["text"])

    def test_chat_routes(self):
        for q, tool in (("start a room watch for 3 days", "start_room_watch"), ("watch the room", "start_room_watch"),
                        ("stop the room watch", "stop_room_watch"), ("room watch report", "room_watch_report"),
                        ("is the room watch running", "room_watch_status"),
                        ("did the room watch find anything", "room_watch_status"),
                        ("446.1 is mine", "accept_room_watch_signal"),
                        ("accept 446.1 as the radio mic", "accept_room_watch_signal")):
            self.assertEqual(A._route_intent(q)[0], tool, q)
        self.assertEqual(A._route_intent("start a room watch for 3 days")[1], {"hours": 72.0})
        self.assertEqual(A._route_intent("start a new room watch for 3 days")[1], {"new_reference": True, "hours": 72.0})
        self.assertEqual(A._route_intent("carry on the room watch")[1], {"carry_on": True})
        self.assertEqual(A._route_intent("accept 446.1 as the radio mic")[1],
                         {"frequency_mhz": 446.1, "label": "the radio mic"})
        self.assertEqual(A._route_intent("capture 433.92")[0], "start_background_watchdog")
        self.assertEqual(A._route_intent("stop the capture")[0], "stop_background_watchdog")
        self.assertEqual(A._route_intent("investigate 446.1")[0], "investigate_single_frequency")
        self.assertEqual(A._route_intent("don't start a room watch"), None)
        self.assertEqual(A._route_intent("433.92 is mine?"), None)
        # only an explicit start phrase starts a watch (a new reference takes what transmits now as normal)
        for q in ("what happened to the room watch", "did the room watch stop", "the room watch",
                  "how do I start a room watch", "tell me about the room watch from yesterday"):
            self.assertEqual(A._route_intent(q)[0], "room_watch_status", q)
        self.assertEqual(A._route_intent("monitor the room for 2 days"), ("start_room_watch", {"hours": 48.0}))
        self.assertEqual(A._route_intent("investigate 433.92 that the room watch flagged")[0],
                         "investigate_single_frequency")
        for name in ("start_room_watch", "stop_room_watch", "accept_room_watch_signal"):
            self.assertIn(name, A._ADMIN_TOOLS)


class Hardened(unittest.TestCase):
    """What keeps a long watch honest: only its own reference counts, a receiver that is slow, stuck or failing
    never holds the others and is named, time the computer slept is a gap, the timeline keeps every alert, and the
    alert address is told apart from a sign of life."""
    setUp, tearDown = Run.setUp, Run.tearDown
    _appears, _watch, _ready = Run._appears, Run._watch, Run._ready

    def test_a_reference_left_by_an_earlier_watch_is_never_used(self):
        self._ready()                                            # an earlier watch: its reference files stay on disk
        w = self._watch([[433.0, 435.0]])
        real = A._record_one_baseline

        def record(ser, *a, **k):
            if ser == "00026410":
                raise A.RFError("Dongle 2 is busy (Decode). Try again shortly.")
            return real(ser, *a, **k)
        with mock.patch.object(A, "_record_one_baseline", record):
            self.assertEqual(w._reference(A._room_pairs(w.plan)), 1)
        key = A._room_pair_key((433.0, 435.0, "00026410"))
        self.assertTrue(A._find_baseline(433.0, 435.0, "00026410"))     # the earlier watch's file is there ...
        self.assertNotIn(key, w.refs)                                    # ... and is not this watch's reference
        health = [r for r in A._room_rows() if r["kind"] == "health"]
        self.assertIn("could not be recorded yet", health[-1]["text"])
        self.assertNotIn("00026410", health[-1]["text"])                 # plain words: no serial, no return code
        w.phase, w.watch_since = "watching", time.time()
        w._round()                                               # its own thread records it now, and says so
        self.assertGreaterEqual(w.refs[key], w.started_epoch - 1)
        late = [r for r in A._room_rows() if r["kind"] == "late"]
        self.assertEqual(len(late), 1, A._room_rows())
        self.assertIn("recorded late", late[0]["text"])
        self.assertEqual([(x["s"], x["e"], x["dongle"]) for x in w.late_refs], [(433.0, 435.0, "Dongle 2")])
        self.assertEqual(w.snapshot()["late_refs"][0]["dongle"], "Dongle 2")
        w._round()
        self.assertEqual(w.sweeps_last, [3, 3], list(w.errors))
        # "no new reference" with no earlier watch to carry on is refused, never a watch without a reference
        A.ROOM_STATE.unlink()
        r = call("start_room_watch", hours=0.1, frequency_range="433-435", new_reference=False)
        self.assertEqual(r["status"], "error")
        self.assertIn("no earlier room watch", r["error"])

    def test_a_stuck_receiver_does_not_hold_the_others_and_is_named(self):
        import threading
        w = self._ready()
        w.interval_s = 2.0                                       # (a round's window, short for the test)
        real, free = A._audit, threading.Event()

        def audit(s, e, ser, *a, **k):
            if ser == "00026410":
                free.wait(60)
            return real(s, e, ser, *a, **k)
        with mock.patch.object(A, "_audit", audit), mock.patch.object(A, "ROOM_RX_STALL_S", 1.0):
            t0 = time.time()
            w._round()
            self.assertLess(time.time() - t0, 10)                # the round did not wait for the stuck sweep
            self.assertEqual((w.rounds, w.sweeps_last), (1, [2, 2]))
            w._round()
            self.assertEqual((w.rounds, w.sweeps_last), (2, [2, 2]))     # the others swept again
            health = [r for r in A._room_rows() if r["kind"] == "health"]
            self.assertEqual(len(health), 1, health)
            self.assertIn("Dongle 2 has not finished its sweep of 433-435 MHz", health[0]["text"])
            self.assertIn("still checked by Dongle 1 and Dongle 3", health[0]["text"])
            self.assertEqual([x["state"] for x in w.receivers()], ["working", "slow", "working"])
            free.set()
            time.sleep(1.5)
            w._round()
        self.assertEqual([x["state"] for x in w.receivers()], ["working"] * 3)
        self.assertTrue(any("Dongle 2 works again" in r["text"] for r in A._room_rows()))

    def test_a_receiver_whose_sweeps_fail_is_named_while_the_others_cover_its_bands(self):
        w = self._ready()
        real = A._audit

        def audit(s, e, ser, *a, **k):
            if ser == "00026410":
                raise A.RFError("rtl_power failed on 00026410 (rc=1): usb_open error -3 No supported devices found")
            return real(s, e, ser, *a, **k)
        with mock.patch.object(A, "_audit", audit):
            w._round()
            self.assertFalse([r for r in A._room_rows() if r["kind"] == "health"])      # once can be chance
            w._round()
            w._round()
        health = [r for r in A._room_rows() if r["kind"] == "health"]
        self.assertEqual(len(health), 1, health)
        text = health[0]["text"]
        self.assertIn("Dongle 2 does not work: the receiver was not found", text)
        self.assertIn("still checked by Dongle 1 and Dongle 3", text)
        self.assertNotIn("00026410", text)
        self.assertNotIn("rc=", text)
        self.assertEqual(health[0]["what"], "receiver")
        st = {**w.snapshot(), "running": True}
        self.assertIn("Dongle 2 does not work", A._room_status_line(st))
        self.assertEqual((w.rounds, w.failed_rounds), (3, 0))
        w._round()
        self.assertTrue(any("Dongle 2 works again" in r["text"] for r in A._room_rows()))

    def test_time_in_which_the_computer_slept_is_a_gap_and_an_alarm(self):
        self.assertEqual(A._room_gap(30.0, 30.2, 30.2, 30.2), (0.0, ""))
        self.assertEqual(A._room_gap(30.0, 7500.0, 7500.0, 30.1), (7470.0, "asleep"))
        self.assertEqual(A._room_gap(30.0, 400.0, 400.0, 400.0), (370.0, "held"))
        self.assertEqual(A._room_gap(30.0, 4000.0, 30.1, 30.1), (0.0, "clock"))      # the clock was set: no gap
        w = self._ready()
        w.gap(7470.0, "asleep")
        row = A._room_rows()[-1]
        self.assertEqual((row["kind"], row["what"], row["s"]), ("health", "gap", 7470))
        self.assertIn("Nothing was watched for 2 h 4 min", row["text"])
        self.assertIn("the computer was asleep", row["text"])
        self.assertEqual(len(A._room_unseen()), 1)               # it rings until someone has seen it
        st = {**w.snapshot(), "running": True}
        self.assertEqual(st["gaps_s"], 7470)
        self.assertIn("Not watched for 2 h 4 min in total", A._room_status_line(st))

    def test_the_timeline_cap_drops_notes_and_never_an_alert(self):
        with mock.patch.object(A, "ROOM_LOG_MAX_LINES", 40):
            A._room_log("start", "Room watch started")
            for i in range(3):
                A._room_log("alert", f"New steady signal {i}", mhz=434.0 + i)
            A._room_log("mark", "break")
            A._room_log("health", "a check of the watch itself")
            for i in range(150):
                A._room_log("note", f"note {i}")
        rows = A._room_rows()
        kinds = [r["kind"] for r in rows]
        self.assertEqual((kinds.count("start"), kinds.count("alert"), kinds.count("mark"), kinds.count("health")),
                         (1, 3, 1, 1))
        self.assertLessEqual(len(rows), 61)
        self.assertEqual(rows[-1]["text"], "note 149")           # the newest notes are the ones kept
        self.assertEqual(len(A._room_unseen()), 4)

    def test_the_alert_address_tells_a_sign_of_life_from_an_alert_and_tries_again(self):
        import http.server
        import threading
        got, fail = [], [0]

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                if fail[0]:
                    fail[0] -= 1
                    self.send_response(404, "no such topic " + self.path)
                else:
                    got.append((self.headers.get("Title"), self.headers.get("Priority"), body))
                    self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        cfg = self.cfg
        try:
            with mock.patch.object(A, "_save_config", lambda c: cfg.update(c)), \
                    mock.patch.object(A, "ROOM_PUSH_RETRY_S", 0.2):
                A.set_room_alert_address(f"http://127.0.0.1:{srv.server_address[1]}/secret-topic")
                A._room_push("Room watch at 08:00: running", kind="summary", wait=True)
                self.assertEqual(got[-1], ("ARIS room watch: daily check", "default", A.ROOM_PUSH_OK))
                A._room_push("Room watch at 08:00: 2 alerts not yet seen", kind="open", wait=True)
                self.assertEqual(got[-1][1:], ("default", A.ROOM_PUSH_OPEN))
                A._room_push("The room watch finished its time.", kind="end", wait=True)
                self.assertEqual(got[-1][1:], ("default", A.ROOM_PUSH_END))
                A._room_push("Room watch: New steady signal", kind="alert", wait=True)
                self.assertEqual(got[-1], ("ARIS room watch", "high", A.ROOM_PUSH_QUIET))
                fail[0] = 1                                      # an error page that repeats the address's path
                A._room_push("x", wait=True)
                st = A.room_alert_address()
                self.assertEqual(st["error"], "HTTP 404")
                self.assertNotIn("secret-topic", json.dumps(st))
                n, fail[0] = len(got), 2                         # a short gap in the network: sent again
                A._room_push("Room watch: New steady signal", kind="alert")
                self.assertEqual(A.room_alert_address()["pending"], 1)
                end = time.time() + 10
                while len(got) == n and time.time() < end:
                    time.sleep(0.1)
                self.assertEqual(len(got), n + 1)
                time.sleep(0.3)
                self.assertEqual((A.room_alert_address()["pending"], A.room_alert_address()["dropped"]), (0, 0))
        finally:
            srv.shutdown()
            srv.server_close()
            cfg.update(room_alert_url="", room_alert_detail=False)

    def test_raised_noise_is_a_note_an_overloaded_receiver_an_alert_and_a_replaced_reference_is_said(self):
        w = self._ready()
        w._check = lambda *a, **k: ""                            # (no quick looks here)
        carrier = {"peak_mhz": 434.4, "baseline_db": -50.0, "detected_db": -30.0, "surge_delta_db": 20.0,
                   "bandwidth_khz": 20.0, "pattern": "steady", "new_signal": True, "hint": "a test band"}

        def audit(lift, shift, events, made=None):
            def f(s, e, ser, *a, **k):
                self.assertTrue(k.get("take_lift"))              # only the room watch asks for the lift step
                return {"dongle": A._short(ser), "events": [dict(x) for x in events], "flagged": len(events),
                        "floor_lift_db": lift, "floor_shift_db": shift, "pilots": {"n": 0, "median_db": None},
                        "baseline_created_epoch": (made or {}).get(ser)}
            return f
        # the band's noise rose by 7 dB with nothing strong in sight: a note; the new carrier alerts as always
        with mock.patch.object(A, "_audit", audit(7.0, 7.0, [carrier])):
            w._round()
            w._round()
        rows = A._room_rows()
        noise = [r for r in rows if r.get("what") == "noise"]
        self.assertEqual(len(noise), 3, rows)                    # once per receiver, not every round
        self.assertTrue(all(r["kind"] == "note" and "7 dB higher than at the reference" in r["text"] for r in noise))
        self.assertTrue(any(r["kind"] == "alert" and r.get("what") == "new" and r["mhz"] == 434.4 for r in rows))
        self.assertFalse([r for r in rows if r.get("what") == "lift"])
        # a lifted sweep with a very strong signal in it: the receiver is overloaded; an alert about the sweep,
        # with no single frequency on it (nothing to decode or locate there)
        with mock.patch.object(A, "_audit", audit(8.0, 8.0, [dict(carrier, peak_mhz=433.5, surge_delta_db=45.0)])):
            w._round()
        lift = [r for r in A._room_rows() if r.get("what") == "lift"]
        self.assertEqual(len(lift), 3)
        self.assertTrue(all(r["kind"] == "alert" and r.get("mhz") is None for r in lift))
        # quieter than its own reference two rounds running: the reference was noisy, said once per receiver
        with mock.patch.object(A, "_audit", audit(0.0, -6.0, [])):
            for _ in range(3):
                w._round()
        low = [r for r in A._room_rows() if "lower than when the reference was recorded" in r["text"]]
        self.assertEqual(len(low), 3)
        # a baseline recorded outside the watch took the reference's place: said once, loudly
        key = A._room_pair_key((433.0, 435.0, "00017350"))
        with mock.patch.object(A, "_audit", audit(0.0, 0.0, [], made={"00017350": w.refs[key] + 500.0})):
            w._round()
            w._round()
        gone = [r for r in A._room_rows() if "was replaced" in r["text"]]
        self.assertEqual(len(gone), 1)
        self.assertEqual(gone[0]["kind"], "health")
        self.assertIn("433-435 MHz on Dongle 1", gone[0]["text"])

    def test_a_reference_that_failed_three_times_is_tried_again_and_said_truthfully(self):
        w = self._watch([[433.0, 435.0]])
        real = A._record_one_baseline
        broken = [True]

        def record(ser, *a, **k):
            if ser == "00026410" and broken[0]:
                raise A.RFError("rtl_power failed on 00026410 (rc=1): usb_open error -3 No supported devices found")
            return real(ser, *a, **k)
        key = A._room_pair_key((433.0, 435.0, "00026410"))
        with mock.patch.object(A, "_record_one_baseline", record):
            self.assertEqual(w._reference(A._room_pairs(w.plan)), 1)
            w.phase, w.watch_since = "watching", time.time()
            for _ in range(5):
                w._round()
            said = [r for r in A._room_rows() if r["kind"] == "health" and "could not be recorded in 3 tries" in r["text"]]
            self.assertEqual(len(said), 1, A._room_rows())           # once, loudly, and what it says is true
            self.assertIn("tried again every half hour", said[0]["text"])
            self.assertNotIn("00026410", said[0]["text"])
            alarms = [r["text"] for r in A._room_rows() if r["kind"] == "health" and "does not work" in r["text"]]
            self.assertTrue(alarms and all("every round" not in x for x in alarms if "no reference" in x), alarms)
            broken[0] = False                                        # it works again; nobody restarts the watch
            w._round()
            self.assertNotIn(key, w.refs)                            # not before its half hour is over ...
            w.ref_last[key] -= A.ROOM_REF_AGAIN_S + 1
            w._round()
        self.assertIn(key, w.refs)                                   # ... then it gets its reference by itself
        self.assertTrue(any(r["kind"] == "late" and "Dongle 2" in r["text"] for r in A._room_rows()))
        w._round()
        self.assertEqual(w.sweeps_last, [3, 3], list(w.errors))

    def test_a_reference_cut_short_by_reset_scanner_is_recorded_again(self):
        w = self._watch([[433.0, 435.0]])
        real, calls = A._record_one_baseline, []

        def record(ser, *a, **k):
            calls.append(ser)
            out = real(ser, *a, **k)
            if ser == "00026410" and calls.count(ser) == 1:
                w.hold(0.5)                              # Reset Scanner came while it recorded, and the sweep
            return out                                   # program ended cleanly: a short reference, no error
        with mock.patch.object(A, "_record_one_baseline", record):
            self.assertEqual(w._reference(A._room_pairs(w.plan)), 0)
        self.assertEqual(calls.count("00026410"), 2)     # the cut one was not taken: recorded again after the hold
        self.assertEqual(len(w.refs), 3)

    def test_what_was_on_at_the_reference_is_read_from_this_watchs_references_only(self):
        self._appears()
        self._ready()                                    # an earlier watch, recorded while 434.4 MHz was on
        os.environ["FAKE_IQ"], os.environ["FAKE_PEAKS"] = self._env["FAKE_IQ"], self._env["FAKE_PEAKS"]
        w = self._watch([[433.0, 435.0]])
        real = A._record_one_baseline

        def record(ser, *a, **k):
            if ser == "00026410":
                raise A.RFError("Dongle 2 is busy (Decode). Try again shortly.")
            return real(ser, *a, **k)
        with mock.patch.object(A, "_record_one_baseline", record):
            w._reference(A._room_pairs(w.plan))
        w._inventory()                                   # Dongle 2's old file still lists the carrier; it is off
        self.assertFalse([x for x in w.inventory["listed"] if abs(x["mhz"] - 434.4) < 0.05], w.inventory)

    def test_a_long_time_without_aris_is_an_alarm_and_the_all_clear_is_said(self):
        w = self._ready()
        w._round()
        st = w.snapshot()
        st.update(running=True, pid=999999, beat=time.time() - 3 * 3600)
        A._room_state_write(st)
        A._ROOM = None
        self.assertEqual(A.resume_room_watch(), "room watch")
        row = [r for r in A._room_rows() if r.get("what") == "gap"][-1]
        self.assertEqual(row["kind"], "health")          # as loud as a sleep, not a quiet note
        self.assertIn("ARIS was not running", row["text"])
        self.assertIn("3 h 0 min", row["text"])
        self.assertEqual(len(A._room_unseen()), 1)
        A._room_here().stop()
        A._ROOM = None
        # "no sweep worked" is followed by an all-clear once a sweep works again
        w2 = self._ready()
        with mock.patch.object(A, "_audit", lambda *a, **k: (_ for _ in ()).throw(A.RFError("rtl_power gave no data"))):
            w2._round()
            w2._round()
        self.assertTrue(any("no sweep worked" in r["text"] for r in A._room_rows() if r["kind"] == "health"))
        w2._round()
        self.assertTrue(any("its sweeps work again" in r["text"] for r in A._room_rows()))

    def test_a_watch_that_was_replaced_writes_nothing_more(self):
        w = self._ready()
        w._round()
        w._save()
        other = self._watch([[433.0, 435.0]])
        A._ROOM = other                                  # (the guard started a new watch object in its place)
        before = A.ROOM_STATE.read_text()
        w.rounds += 5
        w._save()
        self.assertEqual(A.ROOM_STATE.read_text(), before)
        A._ROOM = None

    def test_a_band_beside_a_gain_edge_keeps_every_part(self):
        cfg = A._load_config()
        work = A._working_serials(cfg)
        for only in ([[107.0, 110.0]], [[86.9, 88.2]], [[879.0, 881.0]]):
            plan = A._room_plan(work, cfg, only)
            spans = A._room_merged([(c["s"], c["e"]) for c in plan["chunks"]])
            self.assertEqual(spans, [tuple(only[0])], plan)       # nothing of the band asked is left out ...
            self.assertEqual(plan["not_covered"], [])
        plan = A._room_plan(work, cfg, None)                     # ... and the standard plan is as it was
        self.assertEqual(len(plan["chunks"]), 19)
        self.assertEqual(plan["chunks"][0]["s"], 25.0)

    def test_a_new_watch_does_not_ring_with_an_earlier_watchs_alerts(self):
        A._room_log("alert", "New steady signal at 434.4 MHz", mhz=434.4, what="new")
        self.assertEqual(len(A._room_unseen()), 1)
        r = call("start_room_watch", hours=0.1, frequency_range="433-435")
        self.assertEqual(r["status"], "ok", r)
        self.assertEqual(A._room_unseen(), [])
        self.assertTrue(any(x["kind"] == "ack" and "earlier watch" in x["text"] for x in A._room_rows()))
        self.assertEqual(A._room_here().snapshot()["log_since"], A._room_here().started_epoch)

    def test_reset_scanner_while_the_watch_sweeps_never_finds_a_stuck_dongle(self):
        os.environ["FAKE_POWER_SLOW"] = "0.8"
        try:
            r = call("start_room_watch", hours=0.2, interval_min=1, frequency_range="433-435")
            self.assertEqual(r["status"], "ok", r)
            w = A._room_here()
            end = time.time() + 60
            while w.phase != "watching" and time.time() < end:
                time.sleep(0.2)
            self.assertEqual(w.phase, "watching")
            for k in range(5):
                w.hold_until = 0.0                               # (the last press's hold is over: sweeps again)
                w.held_evt.clear()
                with w.cv:
                    w.round_no += 1                              # (and a walk of every receiver starts)
                    w.cv.notify_all()
                time.sleep(0.25 + 0.3 * k)
                out = A.stop_everything(wait_s=1.0)
                self.assertFalse(out["stuck_here"], out)
                self.assertTrue(out["room_watch_kept"])
                self.assertEqual(len(out["free"]), 3, out)
            self.assertTrue(w.alive())
            self.assertFalse([r for r in A._room_rows() if r["kind"] == "health"], A._room_rows())
        finally:
            os.environ.pop("FAKE_POWER_SLOW", None)


class WithASignalHoundSA(unittest.TestCase):
    """A simulated SA44B / SA124B next to the RTL-SDR dongles: every standard band is covered, up to 5.9 GHz and
    beyond."""
    DEV = "sa-19184001"

    def setUp(self):
        os.environ["ARIS_SA_SIM"] = "1"
        os.environ["ARIS_SA_SIM_CARRIERS"] = "97.1:-40,2437:-55"
        A._load_plugins()
        A._sync_found_dongles(A._list_devices())
        self._patch()
        for f in (A.ROOM_STATE, A.ROOM_LOG):
            try:
                f.unlink()
            except OSError:
                pass

    def _patch(self):
        cfg = dict(A._load_config())
        cfg.update(baseline_seconds=2, audit_seconds=2)
        self._p = mock.patch.object(A, "_load_config", lambda: dict(cfg))
        self._p.start()

    def tearDown(self):
        self._p.stop()
        os.environ.pop("ARIS_SA_SIM", None)
        os.environ.pop("ARIS_SA_SIM_CARRIERS", None)
        if self.DEV in A.DONGLE_NAMES:
            call("forget_dongle", dongle=self.DEV)

    def test_every_standard_band_is_covered_and_nothing_is_called_not_covered(self):
        cfg = A._load_config()
        plan = A._room_plan(A._working_serials(cfg), cfg)
        self.assertEqual(plan["not_covered"], [])
        self.assertEqual(plan["partly_covered"], [])
        by = {(c["s"], c["e"]): c for c in plan["chunks"]}
        ghz = [c for c in plan["chunks"] if c["s"] >= 2400.0]
        self.assertTrue(ghz and all(c["serials"] == [self.DEV] for c in ghz))
        self.assertTrue(any(c["s"] >= 5150.0 for c in plan["chunks"]))
        fm = next(c for c in plan["chunks"] if c["s"] <= 88.0 <= c["e"] or (c["s"], c["e"]) == (88.0, 108.0))
        self.assertGreater(len(fm["serials"]), 1)          # the dongles and the SA all take the lower bands

    def test_reference_and_a_new_carrier_at_2_4_ghz(self):
        cfg = A._load_config()
        only = [(2400.0, 2500.0)]
        plan = A._room_plan(A._working_serials(cfg), cfg, only)
        self.assertEqual([c["serials"] for c in plan["chunks"]], [[self.DEV]])
        w = A.RoomWatch(plan, 1.0, 5.0, 10.0, True, only=only)
        self.assertEqual(w._reference(A._room_pairs(plan)), 0, list(w.errors))
        w.phase, w.watch_since = "watching", time.time()
        w._round()
        self.assertEqual((w.alerts_total, w.rounds), (0, 1), list(w.errors))
        os.environ["ARIS_SA_SIM_CARRIERS"] = "97.1:-40,2437:-55,2462:-40"
        w._round()
        rows = A._room_rows()
        hit = [r for r in rows if r.get("kind") in ("alert", "note") and abs((r.get("mhz") or 0) - 2462.0) < 2.0]
        self.assertTrue(hit, (rows, list(w.errors)))
        self.assertIn("Dongle", json.dumps(hit[0].get("by") or {}))

    def test_a_receiver_added_during_the_watch_joins_it_with_a_late_reference(self):
        self._p.stop()
        os.environ.pop("ARIS_SA_SIM", None)                      # the watch starts with the dongles only
        call("forget_dongle", dongle=self.DEV)
        self.assertNotIn(self.DEV, A.DONGLE_NAMES)
        self._patch()
        cfg = A._load_config()
        only = [[433.0, 435.0], [2400.0, 2500.0]]
        plan = A._room_plan(A._working_serials(cfg), cfg, only)
        self.assertEqual(plan["not_covered"], ["2400-2500 MHz"])
        w = A.RoomWatch(plan, 1.0, 5.0, 10.0, True, only=only)
        self.assertEqual(w._reference(A._room_pairs(plan)), 0, list(w.errors))
        w.phase, w.watch_since = "watching", time.time()
        w._replan()
        self.assertEqual(len(w.plan["chunks"]), 1)
        self._p.stop()
        os.environ["ARIS_SA_SIM"] = "1"                          # plugged in, then "check the system"
        A._sync_found_dongles(A._list_devices())
        self.assertIn(self.DEV, A.DONGLE_NAMES)
        self._patch()
        w._replan()
        self.assertEqual(w.plan["not_covered"], [])
        self.assertTrue(any(c["s"] == 2400.0 and c["serials"] == [self.DEV] for c in w.plan["chunks"]))
        self.assertTrue(any("joined the room watch" in r["text"] and "recorded now" in r["text"]
                            for r in A._room_rows()), A._room_rows())
        w._round()                                               # the dongles sweep on while it records its own
        self.assertEqual(w.sweeps_last, [3, 3], list(w.errors))
        late = [r for r in A._room_rows() if r["kind"] == "late"]
        self.assertEqual(len(late), 1, A._room_rows())
        self.assertIn(A._short(self.DEV), late[0]["text"])
        self.assertIn("2400-2500 MHz", late[0]["text"])
        w._round()
        self.assertEqual(w.sweeps_last, [5, 5], list(w.errors))    # every sweep has a reference now

    def test_the_other_receivers_sweeps_do_not_change_when_a_wider_receiver_is_there(self):
        cfg = A._load_config()
        work = A._working_serials(cfg)
        with_it = A._room_pairs(A._room_plan(work, cfg, None))
        without = A._room_pairs(A._room_plan([s for s in work if s != self.DEV], cfg, None))
        self.assertTrue(without <= with_it, sorted(without - with_it))
        self.assertIn((1710.0, 1766.0, self.DEV), with_it)       # the part they share is a sweep of its own


if __name__ == "__main__":
    unittest.main()
