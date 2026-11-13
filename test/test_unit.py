"""Pure maths and storage: no radios, no engine.  python3 -m unittest test_unit  (or pytest)"""
import math
import json
import struct
import time
import os
import unittest
from pathlib import Path
import zlib

import harness  # noqa: F401  (fresh data folder)
import numpy as np

from arislib import adsb, bandwatch, devices, floorplan, localize, nodes, radioproc, rds, rfpower, rtlpower, store


def _png(w, h):
    raw = b"".join(b"\x00" + b"\x80" * (w * 3) for _ in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class Rds(unittest.TestCase):
    """The FM station-name decoder: real names come out, noise never produces one."""
    FS = 256000.0

    def _mpx(self, secs=2.0, rds_amp=0.04, noise=0.02, seed=5, name="TEST FM", pi=0x4BCD, pilot=True):
        t = np.arange(int(self.FS * secs)) / self.FS
        rng = np.random.default_rng(seed)
        x = 0.4 * np.sin(2 * np.pi * 1000 * t) + noise * rng.standard_normal(len(t))
        if pilot:
            x += 0.1 * np.sin(2 * np.pi * rds.PILOT_HZ * t)
        if rds_amp:
            x += rds_amp * rds.rds_waveform(t, pi, name, -np.pi / 2, 1.1)
        return x

    def test_selftest(self):
        self.assertTrue(rds.selftest()["ok"])

    def test_name_and_pi(self):
        r = rds.decode(self._mpx(), self.FS)
        self.assertEqual((r["ps"], r["pi"]), ("TEST FM", "0x4BCD"), r)

    def test_crc_matches_the_standard(self):
        # IEC 62106 Annex B generator matrix: the last row (data word 0x0001) has checkword 0110111001 = 0x1B9
        self.assertEqual(rds.crc10(0), 0)
        self.assertEqual(rds.crc10(1), 0x1B9)
        self.assertEqual(rds.crc10(1 << 15) ^ rds.crc10(1 << 14), rds.crc10((1 << 15) | (1 << 14)))   # linear

    def test_noise_gives_no_name(self):
        r = rds.decode(self._mpx(rds_amp=0, noise=0.2), self.FS)
        self.assertIsNone(r.get("ps"))
        self.assertFalse(r["rds_present"])
        t = np.arange(int(self.FS * 2)) / self.FS                   # a stereo coder's plain 57 kHz leak is not RDS
        r = rds.decode(self._mpx(rds_amp=0) + 0.02 * np.cos(2 * np.pi * 57000 * t), self.FS)
        self.assertIsNone(r.get("ps"))
        self.assertFalse(r["rds_present"])

    def test_mono_station(self):
        # no stereo pilot to lock to: the subcarrier is found by itself (here 4 Hz off 57 kHz, its own clock)
        t = np.arange(int(self.FS * 3)) / self.FS
        x = (0.4 * np.sin(2 * np.pi * 700 * t) + 0.02 * np.random.default_rng(2).standard_normal(len(t))
             + 0.04 * rds.rds_waveform(t, 0x2C3D, "NOTICIAS", rds_phase=0.4, carrier_hz=57004.0))
        r = rds.decode(x, self.FS)
        self.assertEqual((r["ps"], r["pi"], r["stereo_pilot"]), ("NOTICIAS", "0x2C3D", False), r)
        self.assertAlmostEqual(r["rds_carrier_hz"], 57004.0, delta=0.2)

    def test_rds_not_locked_to_the_pilot(self):
        # a stereo station whose RDS coder runs on its own clock: the old pilot-locked decoder lost it in long looks
        t = np.arange(int(self.FS * 8)) / self.FS
        x = (0.4 * np.sin(2 * np.pi * 1000 * t) + 0.1 * np.sin(2 * np.pi * rds.PILOT_HZ * t)
             + 0.04 * rds.rds_waveform(t, 0x4BCD, "UNLOCKED", rds_phase=2.0, carrier_hz=57000 - 3.7)
             + 0.02 * np.random.default_rng(4).standard_normal(len(t)))
        r = rds.decode(x, self.FS)
        self.assertEqual(r["ps"], "UNLOCKED", r)
        self.assertFalse(r["locked_to_pilot"])

    def test_bit_errors_are_corrected_and_never_misname(self):
        bits = np.array(rds.encode_bits(0x1A2B, "CORRECTD", 40), dtype=np.int64)
        e = np.zeros(1, dtype=np.int64)
        d = np.concatenate([e, np.cumsum(bits) % 2])            # what the differential decoding hands over
        raw = d[1:] ^ d[:-1]
        rng = np.random.default_rng(1)
        hit = raw.copy()
        for blk in range(1, len(bits) // 26, 3):                # one wrong bit in every third block
            hit[blk * 26 + int(rng.integers(0, 26))] ^= 1
        groups, exact, corrected = rds._groups(hit)
        self.assertGreater(corrected, 5)
        self.assertGreaterEqual(len(groups), 38)
        for i, blk in groups:                                    # every corrected block is the word that was sent
            sent = rds.group_0a(0x1A2B, "CORRECTD", i % 4)
            self.assertEqual([b[0] for b in blk if b], [w for w, b in zip(sent, blk) if b])
        junk = rng.integers(0, 2, 5000)                          # random bits: no groups at all
        self.assertEqual(rds._groups(junk)[0], [])

    def test_scrolling_name_is_not_a_station_name(self):
        # 88.9 showed "y en": one word of text the station scrolls through its name field
        t = np.arange(int(self.FS * 6.0)) / self.FS
        words = ["Escucha", "y en", "vivo", "la", "mejor", "musica"]
        x = (0.4 * np.sin(2 * np.pi * 1000 * t) + 0.1 * np.sin(2 * np.pi * rds.PILOT_HZ * t)
             + 0.04 * rds.rds_waveform(t, 0x0889, words, -np.pi / 2, 0.9)
             + 0.02 * np.random.default_rng(3).standard_normal(len(t)))
        r = rds.decode(x, self.FS)
        self.assertIsNone(r["ps"], r)
        self.assertEqual(sorted(r["ps_scrolling"].split()), sorted(" ".join(words).split()))
        # a fixed name that comes back between the pieces is still the name
        fixed = ["TOP 40", "Escucha", "TOP 40", "y en", "TOP 40", "vivo"]
        x = (0.4 * np.sin(2 * np.pi * 1000 * t) + 0.1 * np.sin(2 * np.pi * rds.PILOT_HZ * t)
             + 0.04 * rds.rds_waveform(t, 0x91E3, fixed, -np.pi / 2, 0.9)
             + 0.02 * np.random.default_rng(4).standard_normal(len(t)))
        self.assertEqual(rds.decode(x, self.FS)["ps"], "TOP 40")
        self.assertEqual(rds.scrolling_text(["THE BEAT", "E BEAT 1", "BEAT 101"]), "THE BEAT 101")

    def test_two_pieces_seen_once_are_scrolling_text(self):
        # the live station (v4.8.6) kept 'RUSH' / 'HOUR' (pieces of "RUSH HOUR") as two rotating station names
        t = np.arange(int(self.FS * 6.0)) / self.FS
        blank = "        "
        x = (0.4 * np.sin(2 * np.pi * 1000 * t) + 0.1 * np.sin(2 * np.pi * rds.PILOT_HZ * t)
             + 0.04 * rds.rds_waveform(t, 0x1009, [blank, blank, "RUSH", "HOUR"] + [blank] * 12, -np.pi / 2, 0.9)
             + 0.02 * np.random.default_rng(6).standard_normal(len(t)))
        r = rds.decode(x, self.FS)
        self.assertIsNone(r["ps"], r)
        self.assertEqual(r["ps_scrolling"], "RUSH HOUR")
        # words cut into 8-character blocks join again: 'Alejandr' + 'o Sanz' (shown as 'Alejandr o Sanz')
        x = (0.4 * np.sin(2 * np.pi * 1000 * t) + 0.1 * np.sin(2 * np.pi * rds.PILOT_HZ * t)
             + 0.04 * rds.rds_waveform(t, 0xFB89, ["Noche", "Alejandr", "o Sanz"], -np.pi / 2, 0.9)
             + 0.02 * np.random.default_rng(7).standard_normal(len(t)))
        r = rds.decode(x, self.FS)
        self.assertIn("Alejandro Sanz", r["ps_scrolling"], r)
        self.assertEqual(rds.scrolling_text(["Noche", "Alejandr", "o Sanz"], full={"Alejandr"}), "Noche Alejandro Sanz")
        self.assertEqual(rds.scrolling_text(["MUSICALE", "2024"], full={"MUSICALE"}), "MUSICALE 2024")
        # a capital after a full block is a new word (the live station: 'IglesiasY SantosBravos', 'Nada conLuis')
        self.assertEqual(rds.scrolling_text(["Iglesias", "Y Santos", "Bravos"], full={"Iglesias", "Y Santos"}),
                         "Iglesias Y Santos Bravos")
        self.assertEqual(rds.scrolling_text(["Nada con", "Luis Mig", "uel En A", "mor"],
                                            full={"Nada con", "Luis Mig", "uel En A"}), "Nada con Luis Miguel En Amor")

    def test_block_search_always_ends(self):
        # v4.7.3 hung on real, noisy stations: an exact block pair near the end of the capture that does not start a
        # group sent the search back to the same pair for ever. Every position near the end, every pair kind.
        import signal as _sig
        def boom(*_a):
            raise TimeoutError("the RDS block search did not end")
        old = _sig.signal(_sig.SIGALRM, boom)
        try:
            rng = np.random.default_rng(7)
            g = rds.encode_bits(0x1234, "TESTNAME", 2)
            for first in range(4):                               # pairs A-B, B-C, C-D, D-A
                pair = g[26 * first:26 * first + 52]
                for tail in range(0, 110, 3):
                    bits = np.array(list(rng.integers(0, 2, 400)) + pair + list(rng.integers(0, 2, tail)), np.int64)
                    _sig.alarm(2)
                    rds._groups(bits)
                    _sig.alarm(0)
            for seed in range(40):                               # and whole noisy captures through the decoder
                x = self._mpx(secs=1.5, rds_amp=0.012, noise=0.25, seed=seed)
                _sig.alarm(5)
                rds.decode(x, self.FS)
                _sig.alarm(0)
        finally:
            _sig.alarm(0)
            _sig.signal(_sig.SIGALRM, old)

    def test_too_short(self):
        self.assertFalse(rds.decode(self._mpx(secs=0.2), self.FS)["ok"])

    def test_rotating_name_is_never_stitched(self):
        # 94.5 alternates "NOVA" and "ECHO": both are reported, never "NOHO" or "ECVA"
        t = np.arange(int(self.FS * 6.0)) / self.FS
        x = (0.4 * np.sin(2 * np.pi * 1000 * t) + 0.1 * np.sin(2 * np.pi * rds.PILOT_HZ * t)
             + 0.04 * rds.rds_waveform(t, 0x0945, "NOVA", -np.pi / 2, 0.9, ps2="ECHO", switch_every=3)
             + 0.02 * np.random.default_rng(3).standard_normal(len(t)))
        r = rds.decode(x, self.FS)
        self.assertEqual(sorted(r["ps_all"]), ["ECHO", "NOVA"])
        self.assertIn(r["ps"], ("ECHO", "NOVA"))


class BandWatch(unittest.TestCase):
    """The band capture's burst detector: every burst in time and frequency, a steady carrier never."""
    def test_bursts_found_carrier_ignored(self):
        fs, rng = 2.4e6, np.random.default_rng(1)
        n = int(fs * 2.0)
        t = np.arange(n) / fs
        z = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64) * 0.02
        z += 0.05 * np.exp(2j * np.pi * 300e3 * t)                     # steady: part of the floor
        z += 0.1 * ((t > 0.30) & (t < 0.36)) * np.exp(2j * np.pi * 120e3 * t)
        z += 0.1 * ((t > 1.10) & (t < 1.1025)) * np.exp(-2j * np.pi * 450e3 * t)
        d, out = bandwatch.BurstDetector(fs), []
        for i in range(0, n, 600777):                                   # blocks not aligned to frames
            out += d.feed(z[i:i + 600777].astype(np.complex64))
        out += d.flush()
        self.assertEqual(len(out), 2, out)
        a, b = out
        self.assertAlmostEqual(a["offset_hz"], 120e3, delta=3e3)
        self.assertAlmostEqual(a["t0"], 0.30, delta=0.002)
        self.assertAlmostEqual(a["t1"], 0.36, delta=0.002)
        self.assertAlmostEqual(b["offset_hz"], -450e3, delta=3e3)
        self.assertGreater(a["snr_db"], 20)

    def test_side_bands_of_a_strong_burst_are_not_devices(self):
        # a strong on/off remote: its sharp edges spread side-bands hundreds of kHz out, at the same moments
        fs, rng = 2.4e6, np.random.default_rng(3)
        n = int(fs * 1.0)
        t = np.arange(n) / fs
        key = ((t * 1e6 // 465) % 4 == 0) & (t > 0.2) & (t < 0.55)       # 465 us pulses for 350 ms
        z = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * 0.02 + 0.9 * key * np.exp(2j * np.pi * 5e4 * t)
        d = bandwatch.BurstDetector(fs)
        out = d.feed(z.astype(np.complex64)) + d.flush()
        self.assertEqual(len(out), 1, out)
        self.assertAlmostEqual(out[0]["offset_hz"], 5e4, delta=3e3)
        self.assertGreater(d.ghosts, 0)

    @staticmethod
    def _run(z, fs=2.4e6, blk=600000):
        d, out = bandwatch.BurstDetector(fs), []
        for i in range(0, len(z), blk):                                # 0.25 s blocks, as BandWatch feeds it
            out += d.feed(z[i:i + blk].astype(np.complex64))
        return out + d.flush(), d

    def test_held_remote_is_not_cut_off(self):
        # real PC: a remote held down at 17 dB in one FFT bin ended after 757 ms, its bins had become "floor"
        fs, rng = 2.4e6, np.random.default_rng(4)
        n = int(fs * 3.5)
        t = np.arange(n) / fs
        key = ((t * 1e6 // 465) % 4 == 0) & (t > 0.5) & (t < 2.0)      # held for 1.5 s
        a = 0.02 * np.sqrt(10 ** 1.7 / 170.7)                          # 17 dB in a 512-point Hann bin
        z = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * 0.02 + a * key * np.exp(2j * np.pi * 3e4 * t)
        out, _d = self._run(z)
        self.assertEqual(len(out), 1, out)
        self.assertGreater(out[0]["t1"] - out[0]["t0"], 1.4)

    def test_fsk_tones_are_one_burst(self):
        # real PC: TPMS bursts were logged twice, once per tone (433.888 and 433.955 MHz at the same moment)
        fs, rng = 2.4e6, np.random.default_rng(5)
        n = int(fs * 1.5)
        t = np.arange(n) / fs
        bits = ((t * 1e6 // 500) % 3 == 0)
        on = (t > 0.4) & (t < 0.5)
        ph = 2 * np.pi * np.cumsum(np.where(bits, 135e3, 65e3)) / fs
        z = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * 0.02 + 0.05 * on * np.exp(1j * ph)
        out, _d = self._run(z)
        self.assertEqual(len(out), 1, out)
        self.assertAlmostEqual(out[0]["offset_hz"], 100e3, delta=6e3)
        self.assertGreater(out[0]["width_hz"], 60e3)

    @staticmethod
    def _noise(n, seed, sig=0.02):
        rng = np.random.default_rng(seed)
        return (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * sig

    @staticmethod
    def _bin_amp(db, sig=0.02):                                          # level in one 512-point Hann FFT bin
        return sig * np.sqrt(10 ** (db / 10.0) / 170.7)

    def test_channel_hears_again_soon_after_a_carrier(self):
        # v4.8.6: after a 40 dB carrier ended, its bins' floor fell 15% per block: a remote 2 kHz away was not heard
        # 0.5, 1.5, 3 or 5 s later (a held remote did the same to presses that followed it)
        fs = 2.4e6
        n = int(fs * 5.0)
        t = np.arange(n) / fs
        car = ((t > 0.5) & (t < 3.0)) * self._bin_amp(40) * np.exp(2j * np.pi * 32e3 * t)
        key = ((t * 1e6 // 465) % 4 == 0) & (t > 3.5) & (t < 3.86)
        z = self._noise(n, 7) + car + self._bin_amp(20) * key * np.exp(2j * np.pi * 30e3 * t)
        out, _d = self._run(z)
        self.assertTrue([b for b in out if abs(b["t0"] - 3.5) < 0.05 and abs(b["offset_hz"] - 30e3) < 6e3], out)

    def test_samples_missing_for_a_while_give_no_false_burst(self):
        # the floor falls at once to a lower block median, but not when the whole band falls (samples missing for
        # 0.75 s): it then followed the near-silence down and the noise coming back was one band-wide 2 s "burst".
        # (Falling slowly, as v4.8.6 did, can still let a single noise cell through just after: not a device.)
        fs = 2.4e6
        n = int(fs * 8.0)
        z = self._noise(n, 9)
        z[int(fs * 3.0):int(fs * 3.75)] *= 0.001
        out, _d = self._run(z)
        self.assertFalse([b for b in out if b["t1"] - b["t0"] > 0.1 or b["width_hz"] > 50e3], out)
        self.assertLessEqual(len(out), 2, out)

    def test_fsk_burst_with_another_signal_on_is_one_burst(self):
        # v4.8.6: with any other signal on in the band each FSK tone closed at its own row: logged twice, each cut
        # to one tone
        fs = 2.4e6
        n = int(fs * 1.5)
        t = np.arange(n) / fs
        bits = ((t * 1e6 // 52) % 3 == 0)
        on = (t > 0.4) & (t < 0.45)
        ph = 2 * np.pi * np.cumsum(np.where(bits, 135e3, 65e3)) / fs
        other = ((t * 1e6 // 465) % 4 == 0) & (t > 0.3) & (t < 0.9)            # a remote 600 kHz away, held
        z = (self._noise(n, 8) + self._bin_amp(20) * on * np.exp(1j * ph)
             + self._bin_amp(20) * other * np.exp(-2j * np.pi * 500e3 * t))
        out, _d = self._run(z)
        fsk = [b for b in out if abs(b["offset_hz"] - 100e3) < 80e3]
        self.assertEqual(len(fsk), 1, out)
        self.assertGreater(fsk[0]["width_hz"], 60e3)

    def test_two_remotes_ending_together_stay_apart(self):
        # v4.8.6 joined any two bursts within 200 kHz that shared half the shorter one's time: two remotes 120 kHz
        # apart became one burst between them, and neither decoded
        fs = 2.4e6
        n = int(fs * 2.0)
        t = np.arange(n) / fs
        a = ((t * 1e6 // 465) % 4 == 0) & (t > 1.0) & (t < 1.359)
        b = ((t * 1e6 // 400) % 3 == 0) & (t > 1.17) & (t < 1.36)
        z = (self._noise(n, 9) + self._bin_amp(20) * a * np.exp(2j * np.pi * 30e3 * t)
             + self._bin_amp(20) * b * np.exp(-2j * np.pi * 90e3 * t))
        out, _d = self._run(z)
        self.assertEqual(sorted(round(o["offset_hz"] / 1e4) for o in out), [-9, 3], out)

    def test_no_blind_spot_at_the_tuning(self):
        # v4.8.6 ignored +-9.4 kHz around 0 Hz: at the 433 band's tuning (433.87) devices at 433.863-433.877 were
        # never heard. The receiver's DC offset (also when it wanders) still never makes a burst.
        fs = 2.4e6
        n = int(fs * 3.0)
        t = np.arange(n) / fs
        dc = 0.03 + 0.01 * np.sin(2 * np.pi * 0.7 * t)
        z = self._noise(n, 10) + dc
        blk = 600000
        z = np.concatenate([z[i:i + blk] - z[i:i + blk].mean() for i in range(0, n, blk)])   # as _raw_to_iq does
        quiet, _d = self._run(z)
        self.assertEqual(quiet, [])
        key = ((t * 1e6 // 465) % 4 == 0) & (t > 1.0) & (t < 1.36)
        out, _d = self._run(z + self._bin_amp(20) * key * np.exp(2j * np.pi * 3e3 * t))
        self.assertEqual(len(out), 1, out)
        self.assertAlmostEqual(out[0]["offset_hz"], 3e3, delta=3e3)

    def test_carrier_that_appears_is_one_burst_then_floor(self):
        fs, rng = 2.4e6, np.random.default_rng(6)
        n = int(fs * 6.0)
        t = np.arange(n) / fs
        z = (rng.standard_normal(n) + 1j * rng.standard_normal(n)) * 0.02 + 0.05 * (t > 1.0) * np.exp(2j * np.pi * 2e5 * t)
        out, _d = self._run(z)
        self.assertEqual(len(out), 1, out)
        self.assertGreaterEqual(out[0]["t1"] - out[0]["t0"], 1.9)

    def test_noise_alone_gives_nothing(self):
        rng = np.random.default_rng(2)
        z = ((rng.standard_normal(4800000) + 1j * rng.standard_normal(4800000)) * 0.02).astype(np.complex64)
        d = bandwatch.BurstDetector(2.4e6)
        self.assertEqual(d.feed(z) + d.flush(), [])

    def test_running_mean_and_median_are_the_plain_forms_bit_for_bit(self):
        # the detector's main cost, worked bin by bin along the memory: the same numbers as the plain form, to the
        # last bit (a detector that is faster and finds other bursts would be another detector)
        rng = np.random.default_rng(7)
        for frames, bins, a in ((1171, 512, 4), (1, 512, 4), (2, 64, 1), (333, 256, 6), (5, 512, 2), (40, 16, 40)):
            P = (rng.exponential(1.0, (frames, bins)) * 10 ** rng.uniform(-4, 5, (frames, 1))).astype(np.float32)
            P[frames // 2] = 0.0                                         # a block of zeros (samples missing)
            Q = np.concatenate([np.repeat(P[:1], a - 1, axis=0), P]) if a > 1 else P
            c = np.cumsum(Q, axis=0, dtype=np.float64)
            S = ((c[a - 1:] - np.concatenate([np.zeros((1, bins)), c[:-a]])) / a).astype(np.float32)
            med = np.median(S, axis=0)
            S2, med2 = bandwatch.BurstDetector._smooth(Q, a)
            self.assertEqual((S2.dtype, S2.shape, med2.dtype, med2.shape), (S.dtype, S.shape, med.dtype, med.shape))
            self.assertEqual(S2.tobytes(), S.tobytes(), (frames, bins, a))
            self.assertEqual(med2.tobytes(), med.tobytes(), (frames, bins, a))
            self.assertTrue(S2.flags["C_CONTIGUOUS"])                    # the row loop reads it frame by frame

    def test_slices_cover_the_band(self):
        s = bandwatch.slices(902, 928, 1.6)
        self.assertEqual(len(s), 17)
        self.assertLess(s[0] - 0.8, 902.0 + 1e-6)
        self.assertGreater(s[-1] + 0.8, 928.0 - 1e-6)
        self.assertEqual(bandwatch.slices(433.05, 434.79, 1.8), [433.92])


class RadioProc(unittest.TestCase):
    # real PC (2026-11-02): rtl_sdr writing into a pipe nobody read never ended on Ctrl+C (blocked in its write, so
    # its cancel never ran); with the pipe read it ended 0.06 s after Ctrl+C. This child behaves the same way.
    CHILD = ("import os, signal, sys\n"
             "stop = []\n"
             "signal.signal(signal.SIGINT, lambda *a: stop.append(1))\n"
             "while not stop:\n"
             "    os.write(1, b'\\0' * 65536)\n")

    def _child(self):
        import subprocess
        import sys
        import time
        p = subprocess.Popen([sys.executable, "-c", self.CHILD], stdout=subprocess.PIPE)
        time.sleep(0.4)                                 # the pipe is full: the child is blocked writing
        return p

    def test_a_blocked_writer_needs_its_pipe_read(self):
        p = self._child()
        try:
            self.assertEqual(radioproc.stop(p, grace=0.5, kill_wait=2), "killed")
        finally:
            p.stdout.close()

    def test_drain_lets_it_exit_on_ctrl_c(self):
        import time
        p = self._child()
        t0 = time.time()
        try:
            self.assertEqual(radioproc.stop(p, grace=1.5, kill_wait=2, drain=True), "signal")
        finally:
            p.stdout.close()
        self.assertLess(time.time() - t0, 1.5)


class Adsb(unittest.TestCase):
    """rtl_adsb prints noise as frames too: only checksum-verified squitters name an aircraft."""
    def test_reference_frame(self):
        # the worked example of "The 1090 MHz Riddle" (KLM1023, ICAO 4840D6)
        self.assertEqual(adsb.crc_remainder("8D4840D6202CC371C32CE0576098"), 0)
        self.assertNotEqual(adsb.crc_remainder("8D4840D6202CC371C32CE0576099"), 0)

    def test_noise_is_no_aircraft(self):
        r = adsb.parse("*5D4840D6E8D7E1;\n*8D4840D6202CC371C32CE0576099;\n*02E19718E05A32;\n")
        self.assertEqual((r["messages"], r["valid"], r["aircraft"]), (3, 0, []))
        f = adsb.make_frame(0xA1B200)
        r = adsb.parse(f"*8D4840D6202CC371C32CE0576098;\n*{f};\n*{f};\n")
        self.assertEqual(r["aircraft"], ["4840D6", "A1B200"])


class RtlPower(unittest.TestCase):
    """rtl_power's own arithmetic (src/rtl_power.c), worked by hand for the sweeps ARIS makes."""
    def test_fm_view_plan(self):
        hops = rtlpower.plan(88e6, 108e6, 25e3, 0.2)            # the Spectrum tab's FM preset
        self.assertEqual(len(hops), 9)
        h = hops[0]
        self.assertEqual((h["rate"], h["len"], h["i1"], h["i2"], h["bw2"]), (2777777, 128, 12, 115, 1106770))
        self.assertAlmostEqual(h["step"], 21701.38, places=1)
        self.assertEqual(h["freq"], 88000000 + 2222222 // 2)

    def test_old_reading_was_a_bin_high(self):
        self.assertAlmostEqual(rtlpower.old_bias_hz(433e6, 435e6, 10e3, 0.2), 9766, delta=1)     # one bin
        self.assertAlmostEqual(rtlpower.old_bias_hz(88e6, 108e6, 25e3, 0.2), 21701, delta=2)     # one bin
        self.assertAlmostEqual(rtlpower.old_bias_hz(88e6, 108e6, 25e3, 0.0), 0, delta=1)         # no crop: none

    def test_row_bins_from_centre(self):
        h = rtlpower.plan(88e6, 108e6, 25e3, 0.2)[0]
        true = rtlpower.hop_bins(h)
        first, step, n = rtlpower.bin_freqs(h["freq"] - h["bw2"], h["freq"] + h["bw2"], h["step"], len(true) + 1)
        self.assertEqual(n, len(true))                           # the repeated last value is dropped
        self.assertAlmostEqual(first, true[0], delta=1)
        self.assertEqual(rtlpower.parse_crop("20%"), 0.2)

    def _csv_dbm(self, hop, window, amp, nffts=4):
        """rtl_power's level for a sine of amplitude `amp` (8-bit units), with its arithmetic done in floating
        point: integer window, FFT / len, |X|^2 summed over the FFTs, / rate / (ds per FFT)."""
        n, ds = hop["len"], hop["downsample"]
        w = np.array(rtlpower.window_coefs(window, n), dtype=float)
        k = 17                                                   # the bin the sine sits on
        t = np.arange(n)
        x = amp * ds * np.exp(2j * np.pi * k * t / n)            # boxcar downsampling sums ds samples into one
        X = np.fft.fft(x * w) / n
        avg = nffts * abs(X[k]) ** 2
        return 10 * math.log10(avg / hop["rate"] / (nffts * ds))

    def test_full_scale_reference(self):
        """0 dBFS = a full-scale sine: the reference follows rtl_power's own numbers for every sweep ARIS makes,
        whatever the bin width, crop or window, and a half-amplitude sine sits 6 dB below it."""
        for lo, hi, step in ((88e6, 108e6, 25e3), (433e6, 435e6, 10e3), (902e6, 928e6, 10e3), (30e6, 200e6, 100e3)):
            for crop in (0.0, 0.2):
                for window in ("hamming", "rectangle", "blackman", "hann", "blackman-harris", "bartlett"):
                    hop = rtlpower.plan(lo, hi, step, crop)[0]
                    ref = rtlpower.fullscale_db(hop, window)
                    self.assertAlmostEqual(ref, self._csv_dbm(hop, window, 127.0), delta=0.05, msg=(lo, step, window))
                    self.assertAlmostEqual(self._csv_dbm(hop, window, 63.5) - ref, -6.02, delta=0.05)
        # the FM view: rtl_power prints about +20 dB for a full-scale sine with a Hamming window at 2.78 MS/s, which
        # is why v4.8.9's plot showed stations above 0 dB
        self.assertAlmostEqual(rtlpower.sweep_fullscale_db(88e6, 108e6, 25e3, 0.2, "hamming"), 20.4, delta=0.3)
        self.assertEqual(rtlpower.window_coefs("rectangle", 4), [256] * 4)
        self.assertEqual(rtlpower.window_coefs("no such window", 8), [256] * 8)     # rtl_power keeps its rectangle
        # a downsampled sweep (narrow band, rate below rtl_power's minimum) keeps the same reference arithmetic
        narrow = rtlpower.plan(433.8e6, 434.0e6, 1e3, 0.2)[0]
        self.assertGreater(narrow["downsample"], 1)
        self.assertAlmostEqual(rtlpower.fullscale_db(narrow, "hamming"), self._csv_dbm(narrow, "hamming", 127.0),
                               delta=0.05)


class RfPower(unittest.TestCase):
    def setUp(self):
        self.fs, self.off = rfpower.plan_capture(25)
        n = self.fs * 2
        self.t = np.arange(n) / self.fs
        rng = np.random.default_rng(0)
        self.noise = (rng.normal(0, 0.02, n) + 1j * rng.normal(0, 0.02, n)).astype(np.complex64)

    def test_carrier_level_and_floor(self):
        z = self.noise + 0.1 * np.exp(2j * np.pi * -self.off * self.t)
        r = rfpower.channel_power(z.astype(np.complex64), self.fs, -self.off, 25e3)
        self.assertAlmostEqual(r["level_db"], -20.0, delta=0.5)
        self.assertAlmostEqual(r["floor_db"], 10 * math.log10(8e-4 * 25 / 1024), delta=0.7)
        self.assertTrue(r["heard"])
        self.assertGreater(r["active_fraction"], 0.95)

    def test_bursts_report_on_level(self):
        on = (self.t % 0.5) < 0.02
        z = self.noise + 0.1 * np.exp(2j * np.pi * -self.off * self.t) * on
        r = rfpower.channel_power(z.astype(np.complex64), self.fs, -self.off, 25e3)
        self.assertAlmostEqual(r["level_db"], -20.0, delta=1.0)     # level while on, not averaged away
        self.assertLess(r["active_fraction"], 0.1)

    def test_nothing_there(self):
        r = rfpower.channel_power(self.noise, self.fs, -self.off, 25e3)
        self.assertFalse(r["heard"])


class Localize(unittest.TestCase):
    def _pts(self, src, nodes_xy, noise=2.0, seed=1):
        rng = np.random.default_rng(seed)
        out = []
        for i, (x, y) in enumerate(nodes_xy):
            d = max(math.hypot(x - src[0], y - src[1]), 0.5)
            out.append({"x": x, "y": y, "level_db": -30 - 27 * math.log10(d) + rng.normal(0, noise),
                        "floor_db": -75, "heard": True, "label": f"N{i}"})
        return out

    def test_four_corners(self):
        pts = self._pts((7, 2), [(0.5, 0.5), (9.5, 0.5), (9.5, 7.5), (0.5, 7.5)])
        r = localize.source_map(pts, localize.bounds_for(10, 8, pts))
        self.assertTrue(r["ok"])
        self.assertLess(math.hypot(r["best"]["x"] - 7, r["best"]["y"] - 2), 1.5)

    def test_single_receiver_is_honest(self):
        pts = self._pts((7, 2), [(1, 1)])
        r = localize.source_map(pts, localize.bounds_for(10, 8, pts))
        self.assertEqual(r["quality"], "nearest receiver only")

    def test_nobody_heard(self):
        r = localize.source_map([{"x": 1, "y": 1, "heard": False, "floor_db": -70}], (0, 0, 5, 5))
        self.assertFalse(r["ok"])

    def test_strength_map_peaks_near_strongest(self):
        pts = self._pts((7, 2), [(1, 1), (7, 2.2), (5, 7), (9, 7)], noise=0.5)
        r = localize.strength_map(pts, localize.bounds_for(10, 8, pts))
        self.assertLess(math.hypot(r["best"]["x"] - 7, r["best"]["y"] - 2.2), 1.0)


class PlansAndNodes(unittest.TestCase):
    def test_png_size_and_plan(self):
        data = _png(400, 200)
        self.assertEqual(floorplan.image_size(data), ("png", 400, 200))
        p = floorplan.add("Test", data, width_m=20)
        self.assertEqual(p["height_m"], 10.0)
        self.assertEqual(floorplan.active()["id"], p["id"])
        self.assertTrue(floorplan.image_path(p["id"]).is_file())
        with self.assertRaises(ValueError):
            floorplan.image_size(b"GIF89a....")

    def test_scale_keeps_node_spot(self):
        p = floorplan.add("Blank", None, width_m=10)
        ns = nodes.ensure_local([("1001", "D1")])
        nodes.place(ns[0]["id"], p["id"], 5, 3)
        floorplan.scale_from_points(p["id"], 0, 0, 5, 0, 10)       # 5 m on the plan is really 10 m
        n = nodes.get(ns[0]["id"])
        self.assertAlmostEqual(n["x"], 10.0)
        self.assertAlmostEqual(n["y"], 6.0)
        self.assertAlmostEqual(floorplan.get(p["id"])["width_m"], 20.0)

    def test_remote_token_kept_private(self):
        n = nodes.add_remote("Kitchen Pi", "http://192.168.1.50:5055", "x" * 32)
        self.assertNotIn("token", n)
        self.assertEqual(nodes.token_for(n["id"]), "x" * 32)
        with open(nodes.config.NODES_PATH) as f:
            self.assertNotIn("x" * 32, f.read())
        with self.assertRaises(ValueError):
            nodes.add_remote("bad", "ftp://x", "y" * 32)
        nodes.remove(n["id"])
        self.assertIsNone(nodes.token_for(n["id"]))

    def test_band_offsets(self):
        ns = nodes.ensure_local([("2002", "D2")])
        nid = [n for n in ns if n["serial"] == "2002"][0]["id"]
        nodes.set_offsets(433.92, {nid: 4.5})
        self.assertEqual(nodes.offset_for(nodes.get(nid), 434.0), 4.5)
        self.assertEqual(nodes.offset_for(nodes.get(nid), 915.0), 0.0)

    def test_store_points_average(self):
        s = store.new_session(433.92, 25, "plan")
        store.add_readings(s["id"], [{"node_id": "a", "x": 1, "y": 1, "level_db": -40, "floor_db": -70, "heard": True},
                                     {"node_id": "a", "x": 1, "y": 1, "level_db": -44, "floor_db": -70, "heard": True},
                                     {"node_id": "b", "x": 5, "y": 1, "floor_db": -71, "heard": False,
                                      "level_db": -66}])
        pts = {p["node_id"]: p for p in store.points(s["id"])}
        self.assertAlmostEqual(pts["a"]["level_db"], -42.0)
        self.assertFalse(pts["b"]["heard"])


class Wireless(unittest.TestCase):
    """arislib.wireless (v4.10.0): the Wi-Fi scan's text, the baseline and what is worth a look."""
    DUMP = ("BSS aa:bb:cc:00:00:01(on wlx) -- associated\n\tfreq: 5745.0\n\tcapability: ESS Privacy (0x1011)\n"
            "\tsignal: -48.00 dBm\n\tSSID: Casa\n\tRSN:\t * Version: 1\n\t\t * Authentication suites: PSK\n"
            "BSS aa:bb:cc:00:00:02(on wlx)\n\tfreq: 2437\n\tcapability: ESS (0x0401)\n\tsignal: -80.00 dBm\n"
            "\tSSID: \\x00\\x00\\x00\n"
            "BSS aa:bb:cc:00:00:03(on wlx)\n\tfreq: 2412\n\tcapability: ESS Privacy (0x0411)\n\tsignal: -70.00 dBm\n"
            "\tSSID: Wpa3Only\n\tRSN:\t * Version: 1\n\t\t * Authentication suites: SAE\n"
            "BSS aa:bb:cc:00:00:04(on wlx)\n\tfreq: 5180\n\tcapability: ESS Privacy (0x0411)\n\tsignal: -60.00 dBm\n"
            "\tSSID: Mixed\n\tRSN:\t * Version: 1\n\t\t * Authentication suites: PSK SAE\n")

    def test_parse_the_scan_dump(self):
        from arislib import wireless as W
        n = {x["bssid"]: x for x in W.parse_iw_dump(self.DUMP)}
        self.assertEqual(len(n), 4)
        a = n["aa:bb:cc:00:00:01"]
        self.assertEqual((a["ssid"], a["chan"], a["band"], a["security"], a["signal_dbm"], a["associated"]),
                         ("Casa", 149, "5 GHz", "WPA2", -48.0, True))
        b = n["aa:bb:cc:00:00:02"]
        self.assertEqual((b["hidden"], b["ssid"], b["chan"], b["band"], b["security"]), (True, "", 6, "2.4 GHz", "open"))
        self.assertEqual(n["aa:bb:cc:00:00:03"]["security"], "WPA3")
        self.assertEqual(n["aa:bb:cc:00:00:04"]["security"], "WPA2")          # PSK next to SAE: transition mode
        self.assertEqual([W.channel_of(f) for f in (2412, 2484, 5180, 5745, 5955, 100)], [1, 14, 36, 149, 1, None])

    def test_baseline_new_and_worth_a_look(self):
        from arislib import wireless as W
        t = 1_762_100_000.0
        st = W.new_state()
        W.update(st, W.parse_iw_dump(self.DUMP), t)
        r = W.rows(st, t)
        self.assertEqual(r["counts"]["unrated"], 4)                          # no baseline: nothing is rated
        self.assertIsNone(r["baseline_n"])
        self.assertEqual(W.record_baseline(st, t), 4)
        later = W.parse_iw_dump(
            "BSS aa:bb:cc:00:00:09(on wlx)\n\tfreq: 2462\n\tcapability: ESS (0x0401)\n\tsignal: -85.00 dBm\n\tSSID: Far\n"
            "BSS aa:bb:cc:00:00:0a(on wlx)\n\tfreq: 2462\n\tcapability: ESS (0x0401)\n\tsignal: -50.00 dBm\n\tSSID: Close\n"
            "BSS aa:bb:cc:77:00:0b(on wlx)\n\tfreq: 2462\n\tcapability: ESS (0x0401)\n\tsignal: -85.00 dBm\n\tSSID: \\x00\n"
            "BSS aa:bb:cc:00:00:0c(on wlx)\n\tfreq: 2462\n\tcapability: ESS (0x0401)\n\tsignal: -85.00 dBm\n\tSSID: Casa\n")
        W.update(st, later, t + 60)
        by = {x["bssid"]: x for x in W.rows(st, t + 70)["rows"]}
        self.assertEqual(by["aa:bb:cc:00:00:01"]["status"], "baseline")
        self.assertEqual(by["aa:bb:cc:00:00:09"]["status"], "new")           # new, but far and named: only new
        self.assertEqual(by["aa:bb:cc:00:00:0a"]["status"], "look")          # new and strong
        self.assertIn("strong signal", by["aa:bb:cc:00:00:0a"]["reasons"][0])
        self.assertEqual(by["aa:bb:cc:77:00:0b"]["status"], "look")          # new with a hidden name
        self.assertTrue(any("hidden" in x for x in by["aa:bb:cc:77:00:0b"]["reasons"]))
        self.assertEqual(by["aa:bb:cc:00:00:0c"]["status"], "look")          # a known name, open instead of WPA2
        self.assertTrue(any("weaker security" in x for x in by["aa:bb:cc:00:00:0c"]["reasons"]))
        first = W.rows(st, t + 70)["rows"][0]
        self.assertEqual(first["status"], "look")                            # worth a look first
        key = lambda now: {x["bssid"]: x for x in W.rows(st, now)["rows"]}["aa:bb:cc:00:00:01"]["heard_now"]
        self.assertTrue(key(t + 400))                                        # last in a scan 400 s ago: heard now
        self.assertFalse(key(t + W.HEARD_S + 100))                           # not for 11 minutes: not heard now

    # v4.12.0: what a beacon announces besides name and security, the same device's radios as one row, makers, marks
    RICH = (
        "BSS e4:c3:2a:11:22:30(on wlx)\n\tTSF: 1546304410006 usec (17d, 21:31:44)\n\tfreq: 2432.0\n\tcapability: ESS Privacy (0x0411)\n"
        "\tsignal: -72.00 dBm\n\tSSID: Casa Vecino\n\tCountry: US\tEnvironment: Indoor/Outdoor\n\tRSN:\t * Version: 1\n"
        "\t\t * Authentication suites: PSK\n\tHT operation:\n\t\t * primary channel: 5\n\t\t * STA channel width: 20 MHz\n"
        "\tWPS:\t * Version: 1.0\n\t\t * Manufacturer: TP-Link\n\t\t * Model: Deco M5\n\t\t * Device name: Whole-home Wi-Fi\n"
        "BSS e4:c3:2a:11:22:31(on wlx)\n\tTSF: 1546304410006 usec (17d, 21:31:44)\n\tfreq: 5745.0\n\tcapability: ESS Privacy (0x0411)\n"
        "\tsignal: -60.00 dBm\n\tSSID: Casa Vecino\n\tRSN:\t * Version: 1\n\t\t * Authentication suites: PSK\n"
        "\tVHT operation:\n\t\t * channel width: 1 (80 MHz)\n"
        "BSS e4:c3:2a:11:22:32(on wlx)\n\tfreq: 5745.0\n\tcapability: ESS Privacy (0x0411)\n\tsignal: -64.00 dBm\n\tSSID: \\x00\\x00\n"
        "\tRSN:\t * Version: 1\n\t\t * Authentication suites: PSK\n"
        "BSS 4e:c3:2a:aa:bb:01(on wlx)\n\tTSF: 600000000 usec (0d, 00:10:00)\n\tfreq: 2412\n\tcapability: ESS (0x0401)\n"
        "\tsignal: -70.00 dBm\n\tSSID: Hotspot\n")

    def test_beacon_extras_are_read(self):
        from arislib import wireless as W
        n = {x["bssid"]: x for x in W.parse_iw_dump(self.RICH)}
        a, b = n["e4:c3:2a:11:22:30"], n["e4:c3:2a:11:22:31"]
        self.assertEqual((a["uptime_s"], a["country"], a["width"]), (1546304, "US", 20))
        self.assertEqual(a["wps"], {"manufacturer": "TP-Link", "model": "Deco M5", "device_name": "Whole-home Wi-Fi"})
        self.assertEqual(b["width"], 80)                                      # VHT's width wins over the HT one
        self.assertNotIn("wps", b)
        self.assertEqual(n["4e:c3:2a:aa:bb:01"]["uptime_s"], 600)

    def test_one_device_s_radios_are_one_row_with_a_maker(self):
        from arislib import wireless as W
        t = 1_762_100_000.0
        st = W.new_state()
        W.update(st, W.parse_iw_dump(self.RICH), t)
        r = W.rows(st, t)
        self.assertEqual((r["networks"], r["bssids"]), (3, 4))                # the two named radios are one row; the hidden one is its own
        by = {x["key"]: x for x in r["rows"]}
        casa = by["e4:c3:2a:11:22:30"]
        self.assertEqual((casa["bands"], len(casa["members"]), casa["maker"], casa["private_address"]), (["2.4 GHz", "5 GHz"], 2, "TP-LINK", False))
        self.assertEqual((casa["signal_dbm"], casa["wps"]["model"], casa["up_text"]), (-60.0, "Deco M5", "up 17 days"))
        hot = by["4e:c3:2a:aa:bb:01"]
        self.assertEqual((hot["private_address"], hot["maker"]), (True, ""))   # second hex digit E: made up by the device (a hotspot)
        self.assertEqual(by["e4:c3:2a:11:22:32"]["hidden_sibling"], "Casa Vecino")   # a hidden radio of a named device
        from arislib import oui
        self.assertEqual((oui.maker("54:13:79:aa:bb:cc"), oui.maker("02:00:00:00:00:01"), oui.locally_administered("a6:00:00:00:00:00")),
                         ("Hon Hai Precision Ind", "", True))

    def test_uptime_hidden_twins_and_marks_in_the_rules(self):
        from arislib import wireless as W
        t = 1_762_100_000.0
        st = W.new_state()
        W.update(st, W.parse_iw_dump("BSS aa:bb:cc:00:00:01(on wlx)\n\tfreq: 2412\n\tcapability: ESS (0x0401)\n\tsignal: -80.00 dBm\n\tSSID: Mine\n"), t)
        W.record_baseline(st, t)
        extra = ("BSS 10:00:00:00:00:01(on wlx)\n\tTSF: 5000000000 usec\n\tfreq: 2412\n\tcapability: ESS (0x0401)\n\tsignal: -45.00 dBm\n\tSSID: Old\n"   # up for 83 minutes
                 "BSS 10:00:00:00:00:02(on wlx)\n\tTSF: 1500000000000 usec\n\tfreq: 2412\n\tcapability: ESS (0x0401)\n\tsignal: -45.00 dBm\n\tSSID: Fixture\n"
                 "BSS 10:00:00:00:00:03(on wlx)\n\tTSF: 90000000 usec\n\tfreq: 2412\n\tcapability: ESS (0x0401)\n\tsignal: -85.00 dBm\n\tSSID: JustNow\n")
        W.update(st, W.parse_iw_dump(extra), t + 60)
        by = {x["key"]: x for x in W.rows(st, t + 70)["rows"]}
        self.assertEqual(by["10:00:00:00:00:01"]["status"], "look")           # strong, up for 83 min
        self.assertEqual(by["10:00:00:00:00:02"]["status"], "new")            # strong, but up for 17 days: an old fixture, only new
        self.assertEqual(by["10:00:00:00:00:03"]["status"], "look")           # far, but up for 25 min: it came up just now
        self.assertTrue(any("came up just now" in x for x in by["10:00:00:00:00:03"]["reasons"]))
        # marks: mine / known leave the "worth a look" list, "no" rates even a baseline network, back to the rule undoes it
        self.assertEqual(W.set_mark(st, ["10:00:00:00:00:01", "10:00:00:00:00:03"], "known"), 2)
        by = {x["key"]: x for x in W.rows(st, t + 70)["rows"]}
        self.assertEqual((by["10:00:00:00:00:01"]["status"], by["10:00:00:00:00:03"]["status"]), ("known", "known"))
        self.assertEqual(W.set_mark(st, ["aa:bb:cc:00:00:01"], "no"), 1)
        by = {x["key"]: x for x in W.rows(st, t + 70)["rows"]}
        self.assertEqual(by["aa:bb:cc:00:00:01"]["status"], "new")            # in the baseline, but marked not known
        self.assertEqual(W.set_mark(st, ["aa:bb:cc:00:00:01", "10:00:00:00:00:01"], None), 2)
        by = {x["key"]: x for x in W.rows(st, t + 70)["rows"]}
        self.assertEqual((by["aa:bb:cc:00:00:01"]["status"], by["10:00:00:00:00:01"]["status"]), ("baseline", "look"))
        with self.assertRaises(ValueError):
            W.set_mark(st, ["aa:bb:cc:00:00:01"], "maybe")

    def test_networks_are_capped_and_the_baseline_is_kept(self):
        from arislib import wireless as W
        st = W.new_state()
        t = 1_762_100_000.0
        W.update(st, [{"bssid": "00:00:00:00:00:01", "ssid": "Mine", "hidden": False, "freq": 2412, "chan": 1,
                       "band": "2.4 GHz", "security": "WPA2", "signal_dbm": -40.0}], t)
        W.record_baseline(st, t)
        for i in range(W.MAX_NETS + 20):
            W.update(st, [{"bssid": "02:00:00:00:%02x:%02x" % (i // 256, i % 256), "ssid": "n%d" % i, "hidden": False,
                           "freq": 2412, "chan": 1, "band": "2.4 GHz", "security": "WPA2", "signal_dbm": -70.0}], t + 1 + i)
        self.assertEqual(len(st["nets"]), W.MAX_NETS)
        self.assertIn("00:00:00:00:00:01", st["nets"])                       # a baseline network is dropped last

    # ---- v4.13.0: history, notices, chat answers ----
    @staticmethod
    def _n(bssid, ssid, chan, dbm, sec="WPA2", hidden=False, up=None):
        from arislib import wireless as W
        f = 2407 + 5 * chan if chan <= 13 else 5000 + 5 * chan
        n = {"bssid": bssid, "ssid": ssid, "hidden": hidden, "freq": f, "chan": chan, "band": W.band_of(f),
             "security": sec, "signal_dbm": float(dbm)}
        if up is not None:
            n["uptime_s"] = up
        return n

    def test_signal_history_per_quarter_hour(self):
        from arislib import wireless as W
        t0 = 1_762_099_200.0                                                 # a quarter-hour boundary
        st = W.new_state()
        A, B = "aa:bb:cc:00:10:01", "aa:bb:cc:00:20:01"
        W.update(st, [self._n(A, "A", 1, -50), self._n(B, "B", 6, -70)], t0)
        W.update(st, [self._n(A, "A", 1, -45)], t0 + 60)                    # same quarter hour: the best counts
        W.update(st, [self._n(B, "B", 6, -72)], t0 + 900)                   # A not heard in this one
        W.update(st, [self._n(A, "A", 1, -60)], t0 + 2700)                  # no scan at all at t0 + 1800
        rep = W.rows(st, t0 + 2710)
        by = {r["key"]: r for r in rep["rows"]}
        self.assertEqual(len(by[A]["hist"]), W.HIST_SLOTS)
        self.assertEqual(by[A]["hist"][-4:], [-45, 0, None, -60])
        self.assertEqual(by[B]["hist"][-4:], [-70, -72, None, 0])
        self.assertEqual((by[A]["heard_slots"], by[A]["scanned_slots"]), (2, 3))
        self.assertTrue(all(v is None for v in by[A]["hist"][:-4]))
        self.assertNotIn("hist", st["nets"][A]["hist"][0])                  # stored as [slot, dBm] pairs
        W.update(st, [self._n(A, "A", 1, -50)], t0 + 2 * 86400)             # a day later the old quarter hours are gone
        self.assertEqual(len(st["nets"][A]["hist"]), 1)
        self.assertEqual(len(st["scan_slots"]), 1)

    def test_notices_new_gone_once_a_day_and_never_old_news(self):
        from arislib import wireless as W
        t = 1_762_099_200.0
        st = W.new_state()
        home, edge, marked = "aa:bb:cc:00:00:01", "aa:bb:cc:00:30:01", "aa:bb:cc:00:40:01"
        self.assertEqual(W.events(st, t), [])                                # no baseline: nothing to say
        for i in range(12):                                                  # 3 hours: Home steady, Edge now and then
            nets = [self._n(home, "Home", 6, -55, up=40 * 86400), self._n(marked, "Neighbour", 11, -60)]
            if i % 3 == 0:
                nets.append(self._n(edge, "Edge", 1, -88))
            W.update(st, nets, t + i * 900)
            if i == 0:
                W.record_baseline(st, t)
                W.set_mark(st, [marked], "known")
        now = t + 11 * 900
        W.update(st, [self._n(home, "Home", 6, -55), self._n(marked, "Neighbour", 11, -60),
                      self._n("de:ad:00:00:00:01", "Old news", 3, -40)], now)
        self.assertEqual(W.events(st, now + 5), [])                         # first call: notes what is there, says nothing
        W.update(st, [self._n(home, "Home", 6, -55), self._n("aa:bb:cc:00:50:01", "Close\nby", 1, -45, up=900)], now + 60)
        ev = W.events(st, now + 65)
        self.assertEqual(len(ev), 1, ev)
        self.assertTrue(ev[0].startswith("Wi-Fi: a new network worth a look: 'Close?by'"), ev)   # control characters go
        self.assertIn("strong signal (-45 dBm)", ev[0])
        self.assertIn("up 15 min", ev[0])
        W.update(st, [self._n(home, "Home", 6, -55), self._n("aa:bb:cc:00:50:01", "Close\nby", 1, -45)], now + 120)
        self.assertEqual(W.events(st, now + 125), [])                       # once per network per day
        later = now + 120
        for k in range(1, 12):                                               # Home, Edge and Neighbour go quiet
            later = now + 120 + k * 900
            W.update(st, [self._n("aa:bb:cc:00:50:01", "Close\nby", 1, -45)], later)
        ev = W.events(st, later + 5)
        self.assertEqual(len(ev), 1, ev)
        self.assertIn("Wi-Fi: 'Home' (in the baseline) has not been heard for 2.8 h", ev[0])
        self.assertIn("it had been up 40 days", ev[0])                      # Edge came and went: no notice
        self.assertNotIn("Neighbour", " ".join(ev))                         # marked known: never
        self.assertEqual(W.events(st, later + 65), [])
        many = [self._n("0e:00:00:00:%02x:01" % i, "Cafe%d" % i, 1, -50) for i in range(5)]
        W.update(st, many, later + 120)
        ev = W.events(st, later + 125)
        self.assertEqual(len(ev), 1, ev)
        self.assertTrue(ev[0].startswith("Wi-Fi: 5 new networks worth a look: 'Cafe"), ev)
        self.assertIn("(-50 dBm, strong signal)", ev[0])
        st2 = W.new_state()                                                  # the helper stopped: no notices
        W.update(st2, [self._n(home, "Home", 6, -55)], t)
        W.record_baseline(st2, t)
        W.events(st2, t + 1)
        W.update(st2, [self._n(home, "Home", 6, -55), self._n("aa:bb:cc:00:50:01", "Close", 1, -45)], t + 60)
        self.assertEqual(W.events(st2, t + 60 + 400), [])

    def test_chat_answers_from_the_list(self):
        from arislib import wireless as W
        self.assertTrue(W.answer(W.new_state(), "what wifi is there").startswith("No Wi-Fi networks heard yet"))
        noon = time.mktime(time.localtime(1_762_099_200)[:3] + (12, 0, 0, 0, 0, -1))
        st = W.new_state()
        W.update(st, [self._n("aa:bb:cc:00:00:01", "Home", 6, -40, up=20 * 86400)], noon - 86400)
        self.assertIn("No baseline yet", W.answer(st, "wifi", noon - 86400))
        self.assertIn("no Wi-Fi baseline yet", W.answer(st, "which wifi networks are new", noon - 86400))
        W.record_baseline(st, noon - 86400)
        W.update(st, [self._n("aa:bb:cc:00:00:01", "Home", 6, -40, up=21 * 86400),
                      self._n("de:ad:be:ef:00:07", "", 2, -65, hidden=True),
                      self._n("de:ad:be:ef:01:07", "Cafe Libre", 11, -70, sec="open")], noon)
        a = W.answer(st, "what is the hidden network on channel 2?", noon + 5)
        self.assertTrue(a.startswith("Hidden networks on channel 2:\n- a hidden network"), a)
        self.assertIn("2.4 GHz, channel 2, WPA2, -65 dBm now", a)
        self.assertIn("new, worth a look (hidden name)", a)
        a = W.answer(st, "what's the strongest network?", noon + 5)
        self.assertTrue(a.startswith("The strongest networks heard now: 3.\n- 'Home'"), a)
        self.assertIn("up 21 days; in the baseline", a)
        a = W.answer(st, "which wi-fi networks are new today?", noon + 5)
        self.assertTrue(a.startswith("Networks first heard today: 2."), a)
        self.assertNotIn("'Home'", a)
        a = W.answer(st, "any open wifi networks?", noon + 5)
        self.assertIn("'Cafe Libre'", a)
        self.assertNotIn("'Home'", a)
        a = W.answer(st, "tell me about the network called cafe libre", noon + 5)
        self.assertTrue(a.startswith("That network:\n- 'Cafe Libre'"), a)
        a = W.answer(st, "wifi", noon + 5)
        self.assertTrue(a.startswith("Wi-Fi: 3 networks (3 addresses), 3 heard now, 1 with a hidden name."), a)
        self.assertIn("1 in the baseline, 2 new (1 worth a look)", a)
        self.assertEqual(W.answer(st, "what's on channel 44 on the wifi", noon + 5), "Networks on channel 44: none.")
        self.assertIn("helper may have stopped", W.answer(st, "wifi", noon + 3600))


class Bluetooth(unittest.TestCase):
    """arislib.bluetooth (v4.11.0): the Ubertooth's advertisement text, what is kept, the baseline."""
    # three advertisers (public, random-static, random-private), a tracker-type one, a scan request and a connect request
    RAW = (
        "systime=1793768772 freq=2402 addr=8e89bed6 delta_t=3.332 ms rssi=-60\n10 23 7f\nAdvertising / AA 8e89bed6 (valid)/ 35 bytes\n"
        "    Channel Index: 37\n    Type:  ADV_IND\n    AdvA:  84:2a:fd:30:07:7f (public)\n    AdvData: 02 01 06\n"
        "        Type 01 (Flags)\n           00000110\n\n        Type 09 (Complete Local Name)\n           Name: Alex's iPhone\n\n"
        "        Type ff (Manufacturer Specific Data)\n           Company: Apple, Inc.\n           Data: 10 05 01\n\n    CRC:   ce cb 14\n\n"
        "systime=1793768773 freq=2402 addr=8e89bed6 delta_t=0.8 ms rssi=-45\n04 11\nAdvertising / AA 8e89bed6 (valid)/ 17 bytes\n"
        "    Channel Index: 37\n    Type:  ADV_NONCONN_IND\n    AdvA:  c5:cc:17:44:db:b4 (random)\n"
        "        Type 02 (16-bit Service UUIDs, more available)\n           fd5a\n\n    CRC:   aa 0c a4\n\n"
        "systime=1793768774 freq=2402 addr=8e89bed6 delta_t=0.9 ms rssi=-71\n60 1b\nAdvertising / AA 8e89bed6 (valid)/ 27 bytes\n"
        "    Channel Index: 37\n    Type:  ADV_IND\n    AdvA:  45:cc:17:44:db:b5 (random)\n"
        "        Type ff (Manufacturer Specific Data)\n           Company: Apple, Inc.\n           Data: 12 19 00\n\n    CRC:   1 2 3\n\n"
        "systime=1793768775 freq=2402 addr=8e89bed6 delta_t=0.9 ms rssi=-50\n60 1b\nAdvertising / AA 8e89bed6 (valid)/ 12 bytes\n"
        "    Channel Index: 37\n    Type:  SCAN_REQ\n    ScanA: 11:22:33:44:55:66 (random)\n    AdvA:  84:2a:fd:30:07:7f (public)\n\n"
        "systime=1793768776 freq=2402 addr=8e89bed6 delta_t=0.9 ms rssi=-52\n60 1b\nAdvertising / AA 8e89bed6 (valid)/ 12 bytes\n"
        "    Channel Index: 37\n    Type:  CONNECT_REQ\n    InitA: 99:88:77:66:55:44 (random)\n    AdvA:  84:2a:fd:30:07:7f (public)\n")

    def _packets(self):
        from arislib import bluetooth as B
        p, out = B.AdvParser(), []
        for ln in self.RAW.splitlines():
            x = p.feed(ln)
            if x:
                out.append(x)
        x = p.finish()
        if x:
            out.append(x)
        return out

    def test_only_the_advertisers_own_packets_are_read(self):
        pk = self._packets()
        self.assertEqual([x["pdu"] for x in pk], ["ADV_IND", "ADV_NONCONN_IND", "ADV_IND"])   # scan and connect requests dropped
        self.assertEqual(pk[0]["addr"], "84:2a:fd:30:07:7f")
        self.assertEqual((pk[0]["company"], pk[0]["rssi"], pk[0]["find_my"]), ("Apple, Inc.", -60, False))
        self.assertEqual(pk[1]["uuids"], ["fd5a"])
        self.assertTrue(pk[2]["find_my"])                                                    # Apple's offline-finding type 0x12
        for x in pk:
            self.assertNotIn("Alex", json.dumps(x))                                      # a device name is never read
            self.assertNotIn("name", x)

    def test_address_kinds_labels_and_what_is_counted(self):
        from arislib import bluetooth as B
        self.assertEqual(B.addr_class("84:2a:fd:30:07:7f", "public"), "public")
        self.assertEqual(B.addr_class("c5:cc:17:44:db:b4", "random"), "static")             # top bits 11
        self.assertEqual(B.addr_class("45:cc:17:44:db:b5", "random"), "private")            # top bits 01: resolvable, rotates
        self.assertEqual(B.addr_class("05:cc:17:44:db:b5", "random"), "private")            # top bits 00: non-resolvable
        key = b"k" * 32
        self.assertEqual(B.label(key, "84:2a:fd:30:07:7f"), B.label(key, "84:2a:fd:30:07:7f"))
        self.assertNotEqual(B.label(key, "84:2a:fd:30:07:7f"), B.label(b"z" * 32, "84:2a:fd:30:07:7f"))
        w = B.Window(key)
        for x in self._packets():
            w.add(x)
        s = w.snapshot(1_762_100_000.0)
        self.assertEqual((s["packets"], len(s["stable"]), s["rotating"]["distinct"]), (3, 2, 1))   # the rotating one is counted only
        self.assertEqual(s["rotating"]["by_company"], {"Apple, Inc.": 1})
        self.assertEqual({k: v["distinct"] for k, v in s["trackers"].items()}, {"find_my": 1, "smarttag": 1})
        blob = json.dumps(s)
        self.assertNotRegex(blob, r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}")                       # no address anywhere in the summary
        self.assertNotIn("Alex", blob)
        st = [x for x in s["stable"] if x["atype"] == "public"][0]
        self.assertEqual((st["oui"], st["company"]), ("84:2a:fd", "Apple, Inc."))           # the maker code only

    def test_baseline_new_and_worth_a_look(self):
        from arislib import bluetooth as B
        t = 1_762_100_000.0
        key = b"k" * 32
        w = B.Window(key)
        for x in self._packets():
            w.add(x)
        st = B.new_state()
        B.update(st, w.snapshot(t), t)
        r = B.rows(st, t)
        self.assertEqual((r["devices"], r["counts"]["unrated"], r["baseline_n"]), (2, 2, None))   # no baseline: nothing rated
        self.assertEqual(B.record_baseline(st, t), 2)
        w2 = B.Window(key)
        add = lambda addr, rssi, **kw: w2.add(dict({"rssi": rssi, "pdu": "ADV_IND", "addr": addr, "atype": "public", "company": "",
                                                     "uuids": [], "find_my": False}, **kw))
        add("84:2a:fd:30:07:7f", -70)               # in the baseline
        add("10:00:00:00:00:01", -85)               # new, far: only new
        add("10:00:00:00:00:02", -42)               # new, very close
        add("10:00:00:00:00:03", -62, uuids=["feed"])   # new, a Tile
        add("10:00:00:00:00:04", -85, uuids=["feed"])   # new, a Tile far away: only new
        B.update(st, w2.snapshot(t + 60), t + 60)
        by = {x["label"]: x for x in B.rows(st, t + 70)["rows"]}
        L = lambda a: B.label(key, a)
        self.assertEqual(by[L("84:2a:fd:30:07:7f")]["status"], "baseline")
        self.assertEqual(by[L("10:00:00:00:00:01")]["status"], "new")
        self.assertEqual(by[L("10:00:00:00:00:02")]["status"], "look")
        self.assertIn("strong signal", by[L("10:00:00:00:00:02")]["reasons"][0])
        self.assertEqual(by[L("10:00:00:00:00:03")]["status"], "look")
        self.assertTrue(any("Tile" in x for x in by[L("10:00:00:00:00:03")]["reasons"]))
        self.assertEqual(by[L("10:00:00:00:00:04")]["status"], "new")        # a tracker-type signal this weak is not "close"
        self.assertEqual(B.rows(st, t + 70)["rows"][0]["status"], "look")
        self.assertEqual(st["rotating"]["peak"], 1)

    def test_the_helper_only_ever_listens(self):
        """The one command the helper runs is `ubertooth-btle -n`; no other Ubertooth tool or option is used anywhere."""
        import re
        import subprocess
        root = Path(__file__).resolve().parent.parent
        helper = (root / "deploy" / "aris-bt-scan.py").read_text()
        self.assertIn('COMMAND = ["ubertooth-btle", "-n"]', helper)
        used = set()
        for f in subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=root,
                            capture_output=True, text=True).stdout.split():
            if not f.endswith((".py", ".sh", ".service", ".js", ".html")) or f.startswith("test/"):
                continue
            for m in re.finditer(r"ubertooth-(\w+)((?: +-\w+)*)", (root / f).read_text(errors="replace")):
                used.add((m.group(1), m.group(2).strip()))
        allowed = {("btle", "-n"), ("btle", ""), ("util", "-v"), ("util", "")}
        for tool, opts in used:
            self.assertIn(tool, ("btle", "util"), "only ubertooth-btle and ubertooth-util are used: " + tool)
            self.assertIn((tool, opts), allowed, f"ubertooth-{tool} {opts}")
        self.assertNotRegex(helper, r"ubertooth-(tx|rx|scan|specan|dump|follow)")
        self.assertNotRegex(helper, r'"-(i|I|s\S*|f|p|t\S*)"')                            # follow / interfere / fake slave / promiscuous


class StateFiles(unittest.TestCase):
    """The Wi-Fi and Bluetooth lists' state files: read again only when the file changed (the page asks every few
    seconds), written on one line, and never out of step with what the file holds."""

    def test_compact_write_holds_the_same_content_and_stat_key_follows_the_file(self):
        from arislib import config
        path = Path(harness.DATA) / "state_files_test.json"
        path.unlink(missing_ok=True)
        self.assertIsNone(config.stat_key(path))
        data = {"version": 1, "nets": {"aa:bb": {"hist": [[1, -60], [2, -61]], "ssid": "Caf\u00e9 \"x\""}}, "n": None}
        config.write_json(path, data, private=True, compact=True)
        text = path.read_text()
        self.assertEqual(json.loads(text), data)
        self.assertNotIn("\n", text)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertFalse(list(path.parent.glob("state_files_test.json.*")))              # atomic: no leftovers
        k1 = config.stat_key(path)
        self.assertEqual(k1, config.stat_key(path))
        config.write_json(path, dict(data, n=1), private=True, compact=True)             # one character more
        self.assertNotEqual(config.stat_key(path), k1)
        config.write_json(path, data)                                                    # the indented form, as before
        self.assertEqual((config.read_json(path, None), path.read_text().count("\n") > 5), (data, True))
        path.unlink()

    def _counted(self, mod, name):
        calls, real = [], getattr(mod, name)

        def wrapper(*a, **k):
            calls.append(a)
            return real(*a, **k)
        setattr(mod, name, wrapper)
        self.addCleanup(setattr, mod, name, real)
        return calls

    def test_wifi_state_is_read_once_per_file_version(self):
        from arislib import config, wireless as W
        from arisweb import wifi as WF
        scan = Path(harness.DATA) / "state_files_scan.txt"
        keep = os.environ.get("ARIS_WIFI_SCAN_FILE")
        os.environ["ARIS_WIFI_SCAN_FILE"] = str(scan)
        self.addCleanup(lambda: os.environ.pop("ARIS_WIFI_SCAN_FILE") if keep is None
                        else os.environ.__setitem__("ARIS_WIFI_SCAN_FILE", keep))
        self.addCleanup(lambda: (scan.unlink(missing_ok=True), WF._state_file().unlink(missing_ok=True),
                                 WF._STATE.update(key=None, st=None)))
        WF._state_file().unlink(missing_ok=True)
        WF._STATE.update(key=None, st=None)
        reads, parses = self._counted(WF, "read_json"), self._counted(W, "parse_iw_dump")
        now = time.time()
        with WF._LOCK:
            st = WF._load()
            self.assertEqual(st, W.new_state())                             # no file: an empty state, not kept
            self.assertEqual(WF._refresh(st, now), {"helper": "missing", "scan_age_s": None})
            scan.write_text(Wireless.DUMP)
            os.utime(scan, (now - 20, now - 20))
            self.assertEqual(WF._refresh(st, now), {"helper": "ok", "scan_age_s": 20})
            self.assertEqual(len(parses), 1)
            n_reads = len(reads)
            for _ in range(5):                                              # the page's polls: nothing is read again
                st2 = WF._load()
                self.assertIs(st2, st)
                self.assertEqual(WF._refresh(st2, now)["helper"], "ok")
            self.assertEqual((len(reads), len(parses)), (n_reads, 1))
            self.assertEqual(config.read_json(WF._state_file(), None), st)  # the copy kept is what the file holds
            self.assertEqual(len(st["nets"]), 4)
            # another writer changed the file: it is read again
            other = json.loads(WF._state_file().read_text())
            other["marks"] = {"aa:bb:cc:00:00:01": "mine"}
            config.write_json(WF._state_file(), other, private=True)
            st3 = WF._load()
            self.assertEqual((len(reads), st3["marks"]), (n_reads + 1, {"aa:bb:cc:00:00:01": "mine"}))
            # a newer scan is folded in (its text read and parsed once) and written
            os.utime(scan, (now - 2, now - 2))
            self.assertEqual(WF._refresh(st3, now), {"helper": "ok", "scan_age_s": 2})
            self.assertEqual(len(parses), 2)
            self.assertEqual(config.read_json(WF._state_file(), None)["scan_t"], st3["scan_t"])
            self.assertIs(WF._load(), st3)
            # a write that fails: the changed copy is dropped, the next look reads what the file really holds
            real_write = WF.write_json

            def no_write(*a, **k):
                raise OSError("disk full")
            WF.write_json = no_write
            try:
                st3["marks"] = {"never": "written"}
                with self.assertRaises(OSError):
                    WF._save(st3)
            finally:
                WF.write_json = real_write
            self.assertEqual(WF._load()["marks"], {"aa:bb:cc:00:00:01": "mine"})
            # the scan file gone: the helper is missing, whatever is kept
            scan.unlink()
            self.assertEqual(WF._refresh(WF._load(), now)["helper"], "missing")

    def test_bluetooth_summary_is_parsed_once_per_file_version(self):
        from arislib import config
        from arisweb import bluetooth as BF
        scan = Path(harness.DATA) / "state_files_bt.json"
        keep = os.environ.get("ARIS_BT_SCAN_FILE")
        os.environ["ARIS_BT_SCAN_FILE"] = str(scan)
        self.addCleanup(lambda: os.environ.pop("ARIS_BT_SCAN_FILE") if keep is None
                        else os.environ.__setitem__("ARIS_BT_SCAN_FILE", keep))
        self.addCleanup(lambda: (scan.unlink(missing_ok=True), BF._state_file().unlink(missing_ok=True),
                                 BF._STATE.update(key=None, st=None), BF._SNAP.update(key=None, snap=None)))
        BF._state_file().unlink(missing_ok=True)
        BF._STATE.update(key=None, st=None)
        BF._SNAP.update(key=None, snap=None)
        loads = self._counted(BF.json, "loads")
        reads = self._counted(BF, "read_json")
        now = time.time()

        def snap(t, labels):
            return {"v": 1, "t": t, "window_s": 60, "adv": 10,
                    "stable": [{"label": "#" + x, "atype": "public", "oui": "aa:bb:cc", "company": "Maker", "uuids": [],
                                "rssi_max": -70, "rssi_last": -72, "n": 3, "trackers": []} for x in labels],
                    "rotating": {"distinct": 2, "by_company": {}, "rssi_max": -60}, "trackers": {}}
        with BF._LOCK:
            st = BF._load()
            self.assertEqual(BF._refresh(st, now), {"helper": "missing", "scan_age_s": None})
            scan.write_text(json.dumps(snap(now - 5, ["aaaaaa", "bbbbbb"])))
            self.assertEqual(BF._refresh(st, now), {"helper": "ok", "scan_age_s": 5})
            n_loads, n_reads = len(loads), len(reads)
            for _ in range(5):
                st2 = BF._load()
                self.assertIs(st2, st)
                self.assertEqual(BF._refresh(st2, now), {"helper": "ok", "scan_age_s": 5})
            self.assertEqual((len(loads), len(reads)), (n_loads, n_reads))
            self.assertEqual(config.read_json(BF._state_file(), None), st)
            self.assertEqual(sorted(st["devices"]), ["#aaaaaa", "#bbbbbb"])
            scan.write_text(json.dumps(snap(now - 1, ["aaaaaa", "cccccc"])))            # the next summary
            self.assertEqual(BF._refresh(BF._load(), now), {"helper": "ok", "scan_age_s": 1})
            self.assertEqual(sorted(BF._load()["devices"]), ["#aaaaaa", "#bbbbbb", "#cccccc"])
            scan.write_text("{half a file")                                             # caught while being written
            self.assertEqual(BF._refresh(BF._load(), now)["helper"], "missing")
            scan.write_text(json.dumps(snap(now - 1, ["dddddd"])))                      # ... and whole again
            self.assertEqual(BF._refresh(BF._load(), now)["helper"], "ok")
            self.assertEqual(len(BF._load()["devices"]), 3)                             # (not newer: not folded in twice)

    def _bt_scan(self, name):
        """A stand-in for the Bluetooth helper's summary file, with nothing kept from another test."""
        from arisweb import bluetooth as BF
        scan = Path(harness.DATA) / name
        keep = os.environ.get("ARIS_BT_SCAN_FILE")
        os.environ["ARIS_BT_SCAN_FILE"] = str(scan)
        self.addCleanup(lambda: os.environ.pop("ARIS_BT_SCAN_FILE") if keep is None
                        else os.environ.__setitem__("ARIS_BT_SCAN_FILE", keep))
        self.addCleanup(lambda: (scan.unlink(missing_ok=True), BF._state_file().unlink(missing_ok=True),
                                 BF._STATE.update(key=None, st=None), BF._SNAP.update(key=None, snap=None)))
        BF._state_file().unlink(missing_ok=True)
        BF._STATE.update(key=None, st=None)
        BF._SNAP.update(key=None, snap=None)
        return BF, scan

    @staticmethod
    def _bt_snap(t, labels):
        return {"v": 1, "t": t, "window_s": 60, "adv": 10,
                "stable": [{"label": "#" + x, "atype": "public", "oui": "aa:bb:cc", "company": "Maker", "uuids": [],
                            "rssi_max": -70, "rssi_last": -72, "n": 3, "trackers": []} for x in labels],
                "rotating": {"distinct": 2, "by_company": {}, "rssi_max": -60}, "trackers": {}}

    def test_bluetooth_summary_of_the_same_size_at_the_same_file_time_is_taken(self):
        # Many file clocks move in whole ticks (1 to 10 ms), so two summaries written within one tick carry the
        # same time. Written in place with the same size they look like one version of the file: the summary's
        # text has to decide, not the file's time and size.
        from arislib import config
        BF, scan = self._bt_scan("state_files_bt_tick.json")
        now = float(int(time.time()))
        first = json.dumps(self._bt_snap(now - 5, ["aaaaaa", "bbbbbb"]))
        second = json.dumps(self._bt_snap(now - 2, ["aaaaaa", "cccccc"]))
        self.assertEqual(len(first), len(second))
        loads = self._counted(BF.json, "loads")
        with BF._LOCK:
            scan.write_text(first)
            key = config.stat_key(scan)
            self.assertEqual(BF._refresh(BF._load(), now), {"helper": "ok", "scan_age_s": 5})
            scan.write_text(second)                                    # in place: the same file, the same size
            os.utime(scan, ns=(key[0], key[0]))                        # ... and the same tick of the file clock
            self.assertEqual(config.stat_key(scan), key)
            self.assertEqual(BF._refresh(BF._load(), now), {"helper": "ok", "scan_age_s": 2})
            self.assertEqual(sorted(BF._load()["devices"]), ["#aaaaaa", "#bbbbbb", "#cccccc"])
            n_loads = len(loads)
            self.assertEqual(BF._refresh(BF._load(), now)["scan_age_s"], 2)             # the same text: not parsed again
            self.assertEqual(len(loads), n_loads)

    def test_a_change_that_fails_half_way_is_not_kept(self):
        # The kept copy is changed in place before it is written. A change that fails half way must not stay in
        # it: the next look would start from the half and make the same step again on top of it.
        from arislib import config, wireless as W
        from arisweb import wifi as WF
        BF, scan = self._bt_scan("state_files_bt_half.json")
        now = float(int(time.time()))
        bad = self._bt_snap(now - 20, ["aaaaaa", "bbbbbb"])
        bad["stable"].insert(1, "not a device")                        # the first device is counted, then the rule fails
        with BF._LOCK:
            scan.write_text(json.dumps(self._bt_snap(now - 30, ["aaaaaa", "bbbbbb"])))
            BF._refresh(BF._load(), now)
            self.assertEqual(BF._load()["devices"]["#aaaaaa"]["seen"], 3)
            scan.write_text(json.dumps(bad))
            for _ in range(4):                                         # the page keeps asking
                with self.assertRaises(AttributeError):
                    BF._refresh(BF._load(), now)
                self.assertEqual(BF._load(), config.read_json(BF._state_file(), None))
                self.assertEqual(BF._load()["devices"]["#aaaaaa"]["seen"], 3)
            scan.write_text(json.dumps(self._bt_snap(now - 10, ["aaaaaa", "bbbbbb"])))
            self.assertEqual(BF._refresh(BF._load(), now), {"helper": "ok", "scan_age_s": 10})
            self.assertEqual(config.read_json(BF._state_file(), None)["devices"]["#aaaaaa"]["seen"], 6)
        # the Wi-Fi list: a newer scan that cannot be folded in, then a round of notices that fails
        wscan = Path(harness.DATA) / "state_files_scan_half.txt"
        keep = os.environ.get("ARIS_WIFI_SCAN_FILE")
        os.environ["ARIS_WIFI_SCAN_FILE"] = str(wscan)
        self.addCleanup(lambda: os.environ.pop("ARIS_WIFI_SCAN_FILE") if keep is None
                        else os.environ.__setitem__("ARIS_WIFI_SCAN_FILE", keep))
        real_update, real_events = W.update, W.events
        self.addCleanup(lambda: (wscan.unlink(missing_ok=True), WF._state_file().unlink(missing_ok=True),
                                 WF._STATE.update(key=None, st=None),
                                 setattr(W, "update", real_update), setattr(W, "events", real_events)))
        WF._state_file().unlink(missing_ok=True)
        WF._STATE.update(key=None, st=None)

        def half(st, *a, **k):
            st["nets"].clear()
            st["noticed"] = {"half": now}
            raise RuntimeError("half way")
        with WF._LOCK:
            wscan.write_text(Wireless.DUMP)
            os.utime(wscan, (now - 20, now - 20))
            self.assertEqual(WF._refresh(WF._load(), now), {"helper": "ok", "scan_age_s": 20})
            held = config.read_json(WF._state_file(), None)
            self.assertEqual(len(held["nets"]), 4)
            os.utime(wscan, (now - 5, now - 5))
            W.update = half
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    WF._refresh(WF._load(), now)
                self.assertEqual(WF._load(), held)
            W.update = real_update
        W.events = half
        with self.assertRaises(RuntimeError):
            WF.tick(now)                                               # (the newer scan is folded in and written first)
        with WF._LOCK:
            st = WF._load()
            self.assertEqual(st, config.read_json(WF._state_file(), None))
            self.assertEqual((len(st["nets"]), st["scan_t"], "half" in (st.get("noticed") or {})), (4, now - 5, False))


class Devices(unittest.TestCase):
    """The device history's rules (arislib.devices): transmissions, readings without codes, sets of sensors that
    transmit together, caps, and the words the page and the chat show."""
    T0 = 1_762_100_000.0          # 2025-11-02 in local time, whatever the zone: a fixed 'now' for the tests

    def test_groups_split_vehicles_sensors_and_remotes(self):
        # v4.9.7: the Devices page's Vehicles / Sensors / Remotes buttons
        from arislib import devices as D
        t = self.T0
        dev = lambda model, ident, kind, days=1: {"model": model, "id": ident, "kind": kind, "transmissions": 3, "packets": 3,
                                                  "first_t": t, "last_t": t, "days": ["d%d" % i for i in range(days)],
                                                  "dongles": ["Dongle 1"]}
        store = {"devices": {"Citroen|1": dev("Citroen", "1", "tyre sensor (TPMS)"),
                             "Renault|2": dev("Renault", "2", "tyre sensor (TPMS)", days=2),
                             "Nexus-TH|3": dev("Nexus-TH", 3, "temperature sensor"),
                             "Somfy-RTS|#e0fd34": dev("Somfy-RTS", "#e0fd34", "remote")}}
        r = D.rows(store, now=t + 10)
        self.assertEqual(r["groups"], {"vehicle": 2, "sensor": 1, "remote": 1})
        self.assertEqual(r["vehicles"], {"car": 0, "together": 0, "again": 1, "passing": 1})
        by = {x["key"]: x for x in r["rows"]}
        self.assertEqual(by["Renault|2"]["vehicle"], "again")
        self.assertEqual(by["Citroen|1"]["vehicle"], "passing")
        self.assertNotIn("vehicle", by["Nexus-TH|3"])
        self.assertEqual(by["Somfy-RTS|#e0fd34"]["group"], "remote")
        self.assertEqual([x["key"] for x in r["rows"] if D.matches(x, "cars")],
                         [x["key"] for x in r["rows"] if x["group"] == "vehicle"])
        self.assertTrue(D.matches(by["Somfy-RTS|#e0fd34"], "remotes") and not D.matches(by["Nexus-TH|3"], "remotes"))

    def test_sensor_heard_once_and_never_again_is_likely_noise(self):
        # v4.9.11: an 'FS20' at 433.9 and a barbecue thermometer at -200 C, each heard once, sat among the real sensors
        D = devices
        t = self.T0
        dev = lambda model, ident, kind, tx, last, days=1, **kw: {
            "model": model, "id": ident, "kind": kind, "transmissions": tx, "packets": tx, "first_t": last, "last_t": last,
            "days": ["d%d" % i for i in range(days)], "dongles": ["Dongle 1"], **kw}
        store = {"devices": {"FS20|None": dev("FS20", None, "", 1, t),                                   # once, long ago
                             "Gasmate|1": dev("Gasmate-BA1008", 1, "temperature sensor", 1, t),
                             "Fresh|2": dev("Nexus-TH", 2, "temperature sensor", 1, t + D.NOISE_AFTER_S + 50),  # just heard
                             "Nexus-TH|3": dev("Nexus-TH", 3, "temperature sensor", 120, t),               # repeats: real
                             "Back|4": dev("Acurite-986", 4, "temperature sensor", 1, t, days=2),          # on 2 days: real
                             "Marked|5": dev("Marked", 5, "temperature sensor", 1, t, known="yes"),         # user said so
                             "Somfy-RTS|#e0fd34": dev("Somfy-RTS", "#e0fd34", "remote", 1, t),             # a remote: a press
                             "Citroen|1": dev("Citroen", "1", "tyre sensor (TPMS)", 1, t)}}                # a car: its own list
        r = D.rows(store, now=t + D.NOISE_AFTER_S + 100)
        noise = sorted(x["key"] for x in r["rows"] if x["noise"])
        self.assertEqual(noise, ["FS20|None", "Gasmate|1"])
        self.assertEqual(r["noise"], 2)

    def _pk(self, model, ident, t, **fields):
        return {"model": model, "id": ident, "t": t, "time": "2025-11-02 10:00:00", "aris_serial": "00001001",
                "aris_freq_mhz": 433.92, **fields}

    def test_packets_transmissions_and_reading(self):
        st = devices.new_store()
        kind = "tyre-pressure sensor (TPMS) on a vehicle"
        for t, f in ((0, 433.92), (0.5, 433.921), (5.0, 433.918)):
            p = self._pk("Citroen", "92cf949a", self.T0 + t, pressure_kPa=230, temperature_C=21, flags="a0",
                         mic="CRC", snr=17.5, aris_freq_mhz=f)
            self.assertTrue(devices.apply_packet(st, p, kind=kind, dongle="Dongle 1"))
        self.assertFalse(devices.apply_packet(st, {"id": 1, "t": self.T0}))          # no model: no device
        d = st["devices"]["Citroen|92cf949a"]
        self.assertEqual((d["packets"], d["transmissions"]), (3, 2))                  # 0.5 s later = a repeat
        self.assertEqual((d["freq_min"], d["freq_max"]), (433.918, 433.921))
        self.assertEqual((d["dongles"], d["kind"], d["snr_db"]), (["Dongle 1"], kind, 17.5))
        self.assertEqual(d["reading"], {"pressure_kPa": 230, "temperature_C": 21, "flags": "a0"})   # no mic / snr
        self.assertEqual(d["first_t"], self.T0)
        self.assertEqual(d["last_t"], self.T0 + 5)
        self.assertEqual(len(d["days"]), 1)
        rep = devices.rows(st, now=self.T0 + 600)
        r = rep["rows"][0]
        self.assertEqual(r["title"], "Citroen tyre sensor 92cf949a")
        self.assertEqual(r["reading_words"], "2.3 bar, 21 °C")         # 'flags a0' is a field, not a reading
        # another dongle's copy of the last packet, a second earlier by rtl_433's clock: the same transmission
        self.assertTrue(devices.apply_packet(st, self._pk("Citroen", "92cf949a", self.T0 + 4.2, pressure_kPa=230),
                                             kind=kind, dongle=["Dongle 1", "Dongle 2"]))
        self.assertEqual((d["packets"], d["transmissions"], d["dongles"]), (4, 2, ["Dongle 1", "Dongle 2"]))
        self.assertTrue(r["seen_line"].startswith("Seen 2 times since "), r["seen_line"])
        self.assertIn("| 433.918-433.921 MHz | Dongle 1", r["seen_line"])
        self.assertEqual((rep["devices"], rep["sets"], rep["packets"]), (1, 0, 3))

    def test_code_fields_never_stored(self):
        st = devices.new_store()
        p = self._pk("Generic-Remote", "#3fa2c1", self.T0, code="a5c3b1", tristate="0F10FF", cmd=12,
                     button_code=3, rolling_code="77", data="ff", battery_ok=1)
        devices.apply_packet(st, p, kind="remote control / wireless button")
        d = st["devices"]["Generic-Remote|#3fa2c1"]
        self.assertEqual(d["reading"], {"battery_ok": 1})
        text = str(st)
        for bad in ("a5c3b1", "0F10FF", "tristate", "cmd", "button_code", "rolling", "77", "'data'"):
            self.assertNotIn(bad, text, bad)
        r = devices.rows(st, now=self.T0)["rows"][0]
        self.assertEqual((r["title"], r["reading_words"]), ("Generic-Remote #3fa2c1", "battery ok"))
        self.assertEqual(devices.reading_of(p), {"battery_ok": 1})
        self.assertEqual(len(devices.reading_of({f"k{i}": i for i in range(20)})), devices.READING_MAX)
        # rtl_433 25.12's remote decoders report parts of the code under their own names: never stored or shown
        # (Secplus-v1: a garage keypad's PIN and the fixed code's halves; Interlogix: the raw bits; Cardin: the
        # button). A remote keeps only the fields with words; an unknown field of any device is never shown.
        for p, secret in ((self._pk("Secplus-v1", "#638516", self.T0, id0=70, id1=2, pad_id=5, pin="8642",
                                    switch_id=1, battery_ok=1), ("70", "8642", "pad", "id0", "id1", "pin")),
                          (self._pk("Interlogix-Security", "#a1b2c3", self.T0, raw_message="f3e2d1", switch1="OPEN",
                                    switch2="CLOSED", battery_low=0), ("f3e2d1", "raw", "switch1", "OPEN")),
                          (self._pk("Cardin-S466", "#0c0ffe", self.T0, dipswitch="1101", rbutton="01"),
                           ("1101", "rbutton", "dipswitch"))):
            st = devices.new_store()
            devices.apply_packet(st, p, kind="remote control / wireless button")
            r = devices.rows(st, now=self.T0)["rows"][0]
            for bad in secret:
                self.assertNotIn(bad, str(st["devices"]) + r["reading_words"], (bad, st))
            self.assertNotIn("_", r["reading_words"])
        self.assertEqual(devices.reading_words({"temperature_C": 21, "odd_field": "x", "learn": 0}), "21 °C")
        self.assertEqual(devices.reading_words({"flags": "a0", "status": 3}), "")
        # a store from before the labels were keyed: '#' rows go, a sensor's reading loses the code-like fields
        st = {"version": 1, "devices": {
            "Generic-Remote|#3fa2c1": {"model": "Generic-Remote", "id": "#3fa2c1", "kind": "remote", "reading": {}},
            "Secplus-v1|#638516": {"model": "Secplus-v1", "id": "#638516", "kind": "remote control",
                                   "reading": {"pin": "8642", "id0": 70}},
            "Citroen|92cf949a": {"model": "Citroen", "id": "92cf949a", "kind": "tyre-pressure sensor (TPMS)",
                                 "reading": {"pressure_kPa": 230, "flags": "a0", "pin": "1"}}}}
        self.assertEqual(devices.scrub(st), 3)
        self.assertEqual((st["version"], sorted(st["devices"])), (devices.VERSION, ["Citroen|92cf949a"]))
        self.assertEqual(st["devices"]["Citroen|92cf949a"]["reading"], {"pressure_kPa": 230, "flags": "a0"})
        self.assertEqual(devices.scrub(st), 0)

    def test_two_cars_in_one_queue_stay_two_cars(self):
        # cars A and B each heard alone twice (two sets); then one sensor of each heard together twice (two
        # Citroens queued at the same light): v4.8.8's union-find folded all eight into one 'car'
        st = devices.new_store()
        a, b = ("a1", "a2", "a3", "a4"), ("b1", "b2", "b3", "b4")
        for k, ids in enumerate((a, b, a, b)):
            self._car(st, ids, self.T0 + 300 * k)
        two = [["Citroen|a1", "Citroen|a2", "Citroen|a3", "Citroen|a4"], ["Citroen|b1", "Citroen|b2", "Citroen|b3", "Citroen|b4"]]
        self.assertEqual(sorted(sorted(g) for g in devices.sets_of(st["devices"])), two)
        for at in (self.T0 + 1500, self.T0 + 1500 + devices.OCCASION_GAP_S + 5):
            self._car(st, ("a1",), at)
            self._car(st, ("b3",), at + 3)
        self.assertEqual(st["devices"]["Citroen|a1"]["with"]["b3"]["n"], 2)        # linked, but on their own
        self.assertEqual(sorted(sorted(g) for g in devices.sets_of(st["devices"])), two)
        rep = devices.rows(st, now=self.T0 + 3000)
        self.assertEqual([r["title"] for r in rep["rows"]], ["Citroen car: 4 tyre sensors"] * 2)
        # the set's 'Seen N times' is the member heard most often (each sensor once per pass: b3 4 times, 2 of
        # them in the queue), not the members' sum (10)
        car_b = next(r for r in rep["rows"] if "b1" in r["ids"])
        self.assertEqual((car_b["seen"], car_b["transmissions"]), (4, 10))
        self.assertTrue(car_b["seen_line"].startswith("Seen 4 times since "), car_b["seen_line"])
        # a device's plain name (the engine's) in the title, the kind words: 'wireless doorbell' is no door sensor
        st = devices.new_store()
        devices.apply_packet(st, self._pk("Generic-Remote", "#3fa2c1", self.T0), kind="remote control / wireless button",
                             name="Generic fixed-code remote (PT2262 / EV1527-type chip)")
        devices.apply_packet(st, self._pk("Byron-BY", 7, self.T0), kind="wireless doorbell")
        titles = sorted(r["title"] for r in devices.rows(st, now=self.T0)["rows"])
        self.assertEqual(titles, ["Byron-BY doorbell 7", "Generic fixed-code remote (PT2262 / EV1527-type chip) #3fa2c1"])
        self.assertEqual(devices.short_kind("door / window contact sensor"), "door sensor")

    def _car(self, st, ids, at, model="Citroen"):
        for i, ident in enumerate(ids):
            for rep in range(2):
                devices.apply_packet(st, self._pk(model, ident, at + 1.3 * i + 0.4 * rep, pressure_kPa=220),
                                     kind="tyre-pressure sensor (TPMS) on a vehicle", dongle="Dongle 1")

    def test_sets_from_sensors_heard_together(self):
        st = devices.new_store()
        a, b = ("a1", "a2", "a3", "a4"), ("b1", "b2")
        self._car(st, a, self.T0)                       # car A passes: one occasion (pairs n=1, not yet a set)
        self.assertEqual(devices.sets_of(st["devices"]), [])
        self.assertEqual(st["devices"]["Citroen|a2"]["with"]["a1"]["n"], 1)
        self._car(st, a, self.T0 + 3600)                # and again an hour later: a set
        self._car(st, b, self.T0 + 7200)                # car B, twice, 2 min apart (more than one occasion)
        self._car(st, b, self.T0 + 7320)
        self._car(st, ("c1",), self.T0 + 7200 + 4)      # a sensor heard next to car B once: its own row
        self._car(st, ("r1",), self.T0 + 9000, model="Renault")
        self._car(st, ("a5",), self.T0 + 3602)          # a 5th Citroen sensor (the spare) heard with A once
        groups = sorted(sorted(g) for g in devices.sets_of(st["devices"]))
        self.assertEqual(groups, [["Citroen|a1", "Citroen|a2", "Citroen|a3", "Citroen|a4"], ["Citroen|b1", "Citroen|b2"]])
        rep = devices.rows(st, now=self.T0 + 9600)
        self.assertEqual((rep["devices"], rep["sets"]), (9, 2))
        titles = [r["title"] for r in rep["rows"]]
        self.assertEqual(titles, ["Renault tyre sensor r1", "Citroen car: 2 tyre sensors", "Citroen tyre sensor c1",
                                  "Citroen car: 4 tyre sensors", "Citroen tyre sensor a5"])    # (newest last-seen first)
        car = next(r for r in rep["rows"] if r["title"] == "Citroen car: 4 tyre sensors")
        self.assertEqual(sorted(car["ids"]), ["a1", "a2", "a3", "a4"])
        self.assertEqual((car["transmissions"], car["packets"], len(car["members"])), (8, 16, 4))
        self.assertTrue(car["seen_line"].startswith("Seen 2 times since "), car["seen_line"])    # 2 passes
        self.assertEqual(car["members"][0]["title"], "Citroen tyre sensor a4")         # newest member first
        self.assertIn("transmit together", devices.RULE)

    def test_one_pass_is_one_occasion(self):
        # a sensor that repeats 3 s apart within the same pass must not make 2 occasions out of one
        st = devices.new_store()
        kind = "tyre-pressure sensor (TPMS) on a vehicle"
        for t in (0, 3, 6):
            devices.apply_packet(st, self._pk("Citroen", "x1", self.T0 + t), kind=kind)
            devices.apply_packet(st, self._pk("Citroen", "x2", self.T0 + t + 1), kind=kind)
        self.assertEqual(st["devices"]["Citroen|x1"]["with"]["x2"]["n"], 1)
        self.assertEqual(devices.sets_of(st["devices"]), [])
        for t in (devices.OCCASION_GAP_S + 10, devices.OCCASION_GAP_S + 11):
            devices.apply_packet(st, self._pk("Citroen", "x1", self.T0 + t), kind=kind)
            devices.apply_packet(st, self._pk("Citroen", "x2", self.T0 + t + 1), kind=kind)
        self.assertEqual(st["devices"]["Citroen|x1"]["with"]["x2"]["n"], 2)
        self.assertEqual(devices.sets_of(st["devices"]), [["Citroen|x2", "Citroen|x1"]])

    def test_caps(self):
        st = devices.new_store()
        for i in range(devices.MAX_DEVICES + 10):
            devices.apply_packet(st, self._pk("Acurite-Tower", i, self.T0 + i))
        self.assertEqual(devices.prune(st), 10)
        self.assertEqual(len(st["devices"]), devices.MAX_DEVICES)
        self.assertNotIn("Acurite-Tower|0", st["devices"])                          # the oldest went
        self.assertIn(f"Acurite-Tower|{devices.MAX_DEVICES + 9}", st["devices"])
        st = devices.new_store()
        for i in range(12):                                                         # 12 partners: 8 kept
            devices.apply_packet(st, self._pk("Citroen", f"p{i}", self.T0 + 100 * i))
            devices.apply_packet(st, self._pk("Citroen", "hub", self.T0 + 100 * i + 2))
        self.assertEqual(len(st["devices"]["Citroen|hub"]["with"]), devices.SEEN_WITH_MAX)
        st = devices.new_store()
        for day in range(devices.DAYS_MAX + 5):
            devices.apply_packet(st, self._pk("Nexus-TH", 1, self.T0 + 86400 * day))
        self.assertEqual(len(st["devices"]["Nexus-TH|1"]["days"]), devices.DAYS_MAX)

    def test_tyre_sensors_heard_within_5_seconds_are_one_row(self):
        # v4.9.10: four Renault sensors heard at 18:14 were four 'passing?' rows; one make within 5 s is one row
        D = devices
        kind = "tyre-pressure sensor (TPMS) on a vehicle"
        st = D.new_store()
        for i, ident in enumerate(("3d0362", "3d07c2", "8ea131", "8ea09a")):
            D.apply_packet(st, self._pk("Renault", ident, self.T0 + 1.0 * i), kind=kind)       # within 5 s of each other
        D.apply_packet(st, self._pk("Renault", "late", self.T0 + 60), kind=kind)               # a minute later: alone
        D.apply_packet(st, self._pk("Toyota", "t1", self.T0 + 1.5), kind=kind)                  # another make: alone
        r = D.rows(st, now=self.T0 + 100)
        sets = [x for x in r["rows"] if x["kind"] == "set"]
        self.assertEqual(len(sets), 1)
        self.assertTrue(sets[0]["once"])
        self.assertEqual(sorted(sets[0]["ids"]), ["3d0362", "3d07c2", "8ea09a", "8ea131"])
        self.assertIn("4 tyre sensors heard together", sets[0]["title"])
        self.assertEqual(sets[0]["vehicle"], "together")
        self.assertEqual(r["vehicles"], {"car": 0, "together": 1, "again": 0, "passing": 2})
        self.assertEqual(len(r["rows"]), 3)
        # 6 sensors within 5 s are a queue of cars, not one car: single rows
        st = D.new_store()
        for i in range(6):
            D.apply_packet(st, self._pk("Renault", "q%d" % i, self.T0 + 0.5 * i), kind=kind)
        r = D.rows(st, now=self.T0 + 100)
        self.assertEqual([x["kind"] for x in r["rows"]], ["device"] * 6)
        # a gap of 6 s does not link
        st = D.new_store()
        D.apply_packet(st, self._pk("Renault", "x", self.T0), kind=kind)
        D.apply_packet(st, self._pk("Renault", "y", self.T0 + 6), kind=kind)
        self.assertEqual([x["kind"] for x in D.rows(st, now=self.T0 + 100)["rows"]], ["device"] * 2)
        # the same two sensors together on 2 occasions stay a car by the set rule (not 'once')
        st = D.new_store()
        for t in (0, D.OCCASION_GAP_S + 10):
            D.apply_packet(st, self._pk("Renault", "a", self.T0 + t), kind=kind)
            D.apply_packet(st, self._pk("Renault", "b", self.T0 + t + 1), kind=kind)
        sets = [x for x in D.rows(st, now=self.T0 + 500)["rows"] if x["kind"] == "set"]
        self.assertEqual((len(sets), sets[0]["once"], sets[0]["vehicle"]), (1, False, "car"))

    def test_prune_forgets_passing_tyre_sensors_first(self):
        # v4.9.9: passing cars (new tyre-sensor IDs every time) filled the 500 slots within a day and pushed out
        # the devices that matter; a known device is forgotten last (it would be new again)
        D = devices
        t0 = self.T0
        tyre = "tyre-pressure sensor (TPMS) on a vehicle"
        mk = lambda model, ident, kind, last, days=1, **kw: dict({"model": model, "id": ident, "kind": kind, "first_t": last,
                                                                  "last_t": last, "days": ["d%d" % i for i in range(days)]}, **kw)
        now = t0 + 10 * 86400
        st = D.new_store()
        devs = st["devices"]
        devs["Citroen|old"] = mk("Citroen", "old", tyre, now - 4 * 86400)           # passing, not heard for 4 days: gone
        devs["Citroen|new"] = mk("Citroen", "new", tyre, now - 3600)                # passing, an hour ago: stays
        devs["Renault|back"] = mk("Renault", "back", tyre, now - 4 * 86400, days=2)  # heard on 2 days: a car that comes back
        devs["Ford|mine"] = mk("Ford", "mine", tyre, now - 9 * 86400, known="yes")  # marked: stays
        devs["Nexus-TH|1"] = mk("Nexus-TH", 1, "temperature sensor", now - 20 * 86400)  # not a tyre sensor: stays
        self.assertEqual(D.prune(st, now=now), 1)
        self.assertEqual(sorted(devs), ["Citroen|new", "Ford|mine", "Nexus-TH|1", "Renault|back"])
        # a car's set is not "passing"
        for i, ident in enumerate(("a", "b")):
            devs["Toyota|" + ident] = mk("Toyota", ident, tyre, now - 5 * 86400,
                                         **{"with": {("b" if ident == "a" else "a"): {"n": 2, "t": now - 5 * 86400}}})
        self.assertEqual(D.prune(st, now=now), 0)
        self.assertIn("Toyota|a", devs)
        # over PASSING_MAX: the oldest passing ones go, nothing else
        st = D.new_store()
        devs = st["devices"]
        for i in range(D.PASSING_MAX + 7):
            devs["Citroen|%d" % i] = mk("Citroen", str(i), tyre, now - 3000 + i)
        devs["Nexus-TH|1"] = mk("Nexus-TH", 1, "temperature sensor", now - 100)
        self.assertEqual(D.prune(st, now=now), 7)
        self.assertNotIn("Citroen|0", devs)
        self.assertIn("Citroen|%d" % (D.PASSING_MAX + 6), devs)
        self.assertIn("Nexus-TH|1", devs)
        # over the limit: passing, then unmarked, known last
        st = D.new_store()
        devs = st["devices"]
        devs["Citroen|p"] = mk("Citroen", "p", tyre, now - 10)                                    # newest, but passing
        devs["Nexus-TH|a"] = mk("Nexus-TH", "a", "temperature sensor", now - 5000)
        devs["Nexus-TH|k"] = mk("Nexus-TH", "k", "temperature sensor", now - 90000, days=3)       # known by the rule, oldest
        devs["Acurite|m"] = mk("Acurite", "m", "temperature sensor", now - 99999, known="yes")     # marked
        self.assertEqual(D.prune(st, limit=3, now=now), 1)
        self.assertNotIn("Citroen|p", devs)
        self.assertEqual(D.prune(st, limit=2, now=now), 1)
        self.assertNotIn("Nexus-TH|a", devs)                                                       # unmarked, not known
        self.assertEqual(D.prune(st, limit=1, now=now), 1)
        self.assertEqual(list(devs), ["Acurite|m"])                                                # marked goes last

    def test_words(self):
        import time
        now = self.T0
        hm = lambda t: time.strftime("%H:%M", time.localtime(t))
        self.assertEqual(devices.when_words(now - 60, now), f"{hm(now - 60)} (today)")
        self.assertEqual(devices.when_words(now - 86400, now), f"{hm(now - 86400)} (yesterday)")
        old = now - 20 * 86400
        lt = time.localtime(old)
        self.assertEqual(devices.when_words(old, now), f"{lt.tm_mday} {time.strftime('%b', lt)} {hm(old)}")
        self.assertIn(time.strftime("%Y", time.localtime(now - 400 * 86400)), devices.when_words(now - 400 * 86400, now))
        self.assertEqual(devices.when_words(None, now), "?")
        self.assertEqual(devices.reading_words({"pressure_kPa": 231, "temperature_C": 21.5, "battery_ok": 0,
                                                "contact_open": 1, "humidity": 45}),
                         "2.3 bar, 21.5 °C, 45% humidity, battery low, open")
        self.assertEqual(devices.reading_words({"wind_avg_km_h": 12.5, "rain_mm": 3.2, "odd_field": "x"}),
                         "wind 12.5 km/h, rain 3.2 mm")                    # (a field without words is not shown)
        self.assertEqual(devices.reading_words({}), "")
        self.assertEqual(devices.freq_text(433.92, 433.92), "433.92 MHz")
        self.assertEqual(devices.freq_text(433.918, 433.935), "433.918-433.935 MHz")
        self.assertEqual(devices.freq_text(1090.0, None), "1090 MHz")
        self.assertEqual(devices.freq_text(None, None), "")
        self.assertEqual(devices.short_kind("tyre-pressure sensor (TPMS) on a vehicle"), "tyre sensor")
        self.assertEqual(devices.short_kind("temperature / humidity sensor"), "temperature sensor")
        self.assertEqual(devices.short_kind("device decoded by rtl_433"), "device")
        row = {"title": "Citroen car: 4 tyre sensors", "model": "Citroen", "ids": ["92cf949a", "92cf94a1"],
               "type": "tyre-pressure sensor (TPMS) on a vehicle", "freq_text": "433.92 MHz", "dongles": ["Dongle 1"],
               "reading_words": "2.3 bar"}
        for q in ("citroen", "433.9", "92cf94a1", "TPMS", "dongle 1", "bar", ""):
            self.assertTrue(devices.matches(row, q), q)
        self.assertFalse(devices.matches(row, "renault"))

    def test_known_devices_rule_marks_and_folds(self):
        """heard on KNOWN_DAYS different days = known; the user's mark wins either way; known devices fold into
        one line out of a list of what was heard, and passing cars' tyre sensors fold by make."""
        st = devices.new_store()
        day = 86400.0
        kind_t = "tyre-pressure sensor (TPMS) on a vehicle"
        for d in (2, 1, 0):                                   # my car: three days, oldest first
            for ident in ("a1", "a2", "a3", "a4"):
                devices.apply_packet(st, self._pk("Citroen", ident, self.T0 - d * day, pressure_kPa=230), kind=kind_t)
        for d in range(5):                                    # the neighbour's weather station, every day
            devices.apply_packet(st, self._pk("Nexus-TH", 7, self.T0 - d * day, temperature_C=20), kind="weather station")
        devices.apply_packet(st, self._pk("Toyota", "t1", self.T0, pressure_kPa=200), kind=kind_t)    # passing cars
        devices.apply_packet(st, self._pk("Renault", "r1", self.T0, pressure_kPa=200), kind=kind_t)
        devices.apply_packet(st, self._pk("Renault", "r2", self.T0, pressure_kPa=200), kind=kind_t)
        devices.apply_packet(st, self._pk("Acurite-Tower", 9, self.T0, temperature_C=20), kind="temperature / humidity sensor")
        devs = st["devices"]
        self.assertTrue(devices.is_known(devs["Citroen|a1"]) and devices.is_known(devs["Nexus-TH|7"]))
        self.assertFalse(devices.is_known(devs["Toyota|t1"]) or devices.is_known(devs["Acurite-Tower|9"]))
        self.assertEqual(devices.known_by(devs["Citroen|a1"]), "days")
        self.assertEqual(len(devices.known_keys(st)), 5)
        # marks: a set's members all at once; 'no' makes a daily device watched; None goes back to the rule
        self.assertEqual(devices.mark_known(st, "Acurite-Tower|9", True), 1)
        self.assertEqual(devices.mark_known(st, "set:Citroen|a1+Citroen|a2+Citroen|a3+Citroen|a4", False), 4)
        self.assertEqual(devices.mark_known(st, "Nobody|0", True), 0)
        self.assertTrue(devices.is_known(devs["Acurite-Tower|9"]))
        self.assertEqual(devices.known_by(devs["Acurite-Tower|9"]), "you")
        self.assertFalse(devices.is_known(devs["Citroen|a2"]))
        self.assertIsNone(devices.known_by(devs["Citroen|a2"]))
        rows = devices.rows(st, now=self.T0)
        car = next(r for r in rows["rows"] if r["kind"] == "set")
        self.assertEqual((car["known"], car["mark"]), (False, "no"))
        self.assertEqual(rows["known"], 2)                        # Nexus and Acurite (rows, not records)
        self.assertTrue(devices.matches(car, "new") and not devices.matches(car, "known"))
        devices.mark_known(st, "set:Citroen|a1+Citroen|a2+Citroen|a3+Citroen|a4", None)
        self.assertTrue(all(devices.is_known(devs[f"Citroen|a{i}"]) for i in range(1, 5)))
        self.assertTrue(devices.matches(next(r for r in devices.rows(st, now=self.T0)["rows"] if r["kind"] == "set"), "known"))
        # the fold: what a band capture heard
        heard = [{"model": "Citroen", "id": i, "kind": kind_t, "packets": 2, "transmissions": 1} for i in ("a1", "a2", "a3", "a4")]
        heard += [{"model": "Nexus-TH", "id": "7", "kind": "weather station", "packets": 10, "transmissions": 10},
                  {"model": "Toyota", "id": "t1", "kind": kind_t, "packets": 2, "transmissions": 1},
                  {"model": "Renault", "id": "r1", "kind": kind_t, "packets": 2, "transmissions": 1},
                  {"model": "Renault", "id": "r2", "kind": kind_t, "packets": 2, "transmissions": 1},
                  {"model": "Renault", "id": "new", "kind": kind_t, "packets": 1, "transmissions": 1},
                  {"model": "Acurite-Tower", "id": "9", "kind": "temperature / humidity sensor", "packets": 3, "transmissions": 3}]
        line, rest = devices.fold_known(st, heard)
        self.assertEqual(line, "Known: Citroen car: 4 tyre sensors, Nexus-TH weather station, Acurite-Tower temperature sensor; 17 transmissions")
        # two known sensors of one make that were never heard together are not called a car; a known remote keeps
        # its hashed label (review of v4.9.2)
        for ident in ("z1", "z2"):
            devices.apply_packet(st, self._pk("Ford", ident, self.T0, pressure_kPa=200), kind=kind_t)
            devices.mark_known(st, f"Ford|{ident}", True)
        devices.apply_packet(st, self._pk("Generic-Remote", "#3fa2c1", self.T0), kind="remote control / wireless button")
        devices.mark_known(st, "Generic-Remote|#3fa2c1", True)
        line2, _ = devices.fold_known(st, [{"model": "Ford", "id": "z1", "kind": kind_t, "packets": 1},
                                           {"model": "Ford", "id": "z2", "kind": kind_t, "packets": 1},
                                           {"model": "Generic-Remote", "id": "#3fa2c1", "kind": "remote control", "packets": 2}])
        self.assertEqual(line2, "Known: 2 Ford tyre sensors, Generic-Remote #3fa2c1; 4 packets")
        self.assertEqual([h["id"] for h in rest], ["t1", "r1", "r2", "new"])
        pline, rest2 = devices.fold_passing(rest)
        self.assertEqual(pline, "Tyre sensors not known here (probably passing cars): Renault (3), Toyota; 4 transmissions")
        self.assertEqual(rest2, [])
        self.assertEqual(devices.fold_passing(rest[:1]), (None, rest[:1]))          # one make: no fold
        self.assertEqual(devices.fold_known(devices.new_store(), heard), (None, heard))
        # a rebuilt store keeps the marks
        self.assertEqual(devices.scrub(st), 0)
        self.assertEqual(devs["Acurite-Tower|9"]["known"], "yes")


class FakeDeviceSearch(unittest.TestCase):
    """The fake rtl_* tools pick a dongle the way librtlsdr does (v4.9.5 review): strtol with base 0 first,
    so a serial with a leading 0 is read as octal and used as a device INDEX when it is below the count."""

    def test_the_factory_serial_is_an_index_and_the_stations_serials_are_not(self):
        import importlib.util, os
        spec = importlib.util.spec_from_file_location(
            "_fake_common", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake", "bin", "_common.py"))
        fc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fc)
        devs = ["00001001", "00003003", "00004004", "00000001"]
        self.assertEqual(fc.resolve_dev("00000001", devs), (1, "00003003"))    # octal 1 -> the second dongle
        self.assertEqual(fc.resolve_dev("00000000", devs), (0, "00001001"))
        self.assertEqual(fc.resolve_dev("1", devs), (1, "00003003"))
        self.assertEqual(fc.resolve_dev("00001001", devs), (0, "00001001"))    # 513: not an index, exact serial
        self.assertEqual(fc.resolve_dev("00003003", devs), (1, "00003003"))
        self.assertEqual(fc.resolve_dev("00004004", devs), (2, "00004004"))
        self.assertEqual(fc.resolve_dev("99", devs), (None, None))              # the listing-only call
        self.assertEqual(fc.resolve_dev("4004", devs), (2, "00004004"))        # a suffix match
        self.assertEqual(fc.resolve_dev("", devs), (0, "00001001"))            # (strtol of '' is 0: never pass one)


class DeployUnprivileged(unittest.TestCase):
    """The service runs as its own user and the installer is safe by default: static checks only (installing needs
    root on the station, run by its owner, dry run first)."""
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _read(self, *p):
        with open(os.path.join(self.ROOT, *p), encoding="utf-8") as f:
            return f.read()

    def test_the_service_runs_as_aris_with_no_root_paths(self):
        u = self._read("deploy", "aris.service")
        for want in ("User=aris", "Group=aris", "SupplementaryGroups=plugdev", "ARIS_DATA_DIR=/var/lib/aris",
                     "StateDirectory=aris", "UMask=0077", "NoNewPrivileges=yes", "ProtectSystem=strict",
                     "ProtectHome=yes", "@REPO@/aris_gui.py"):
            self.assertIn(want, u)
        self.assertNotIn("User=root", u)
        self.assertNotIn("/root", u)

    def test_the_installer_is_safe_by_default(self):
        import subprocess
        p = os.path.join(self.ROOT, "deploy", "install.sh")
        self.assertEqual(subprocess.run(["bash", "-n", p]).returncode, 0)
        s = self._read("deploy", "install.sh")
        self.assertIn("MODE=dry", s)                          # no argument = dry run
        self.assertIn("--apply", s)
        self.assertIn("--remove", s)
        self.assertNotIn("rm -rf", s)                         # it never deletes code or data
        self.assertNotIn("/root", s)
        self.assertEqual(subprocess.run(["bash", "-n", os.path.join(self.ROOT, "deploy", "update.sh")]).returncode, 0)


class DeployEverything(unittest.TestCase):
    """deploy/install.sh --everything and deploy/get.sh, run for real against stand-in programs in a temporary
    folder: as an ordinary user, with no network and no systemd.

    The installer insists on root and has no switch to skip that. So the tests run it as the "root" of a user
    namespace (unshare -r): root inside, an ordinary user outside, who can change nothing on the computer even by
    mistake. Every path it writes is pointed into the temporary folder (ARIS_ROOT, ARIS_REPO, ARIS_DATA), and its
    PATH holds only the stand-ins and a short list of plain tools. Where user namespaces are not allowed, the tests
    that run the installer are skipped; the static ones still run."""
    ROOT = DeployUnprivileged.ROOT
    INSTALL = os.path.join(ROOT, "deploy", "install.sh")
    GET = os.path.join(ROOT, "deploy", "get.sh")
    UBUNTU = 'PRETTY_NAME="Ubuntu 24.04.1 LTS"\nNAME="Ubuntu"\nVERSION_ID="24.04"\nID=ubuntu\nID_LIKE=debian\n'
    DEBIAN = 'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nNAME="Debian GNU/Linux"\nVERSION_ID="12"\nID=debian\n'
    FEDORA = 'PRETTY_NAME="Fedora Linux 41"\nNAME="Fedora Linux"\nVERSION_ID=41\nID=fedora\n'
    # plain tools the scripts may use besides the stand-ins (whatever the computer has of them)
    BASE = ("bash sh env cat cp mv rm mkdir rmdir ls ln chmod touch head tail sed awk grep tr cut sort uniq wc "
            "dirname basename readlink mktemp tee find stat true false expr seq date tar gzip").split()
    # A stand-in is a few lines of bash. Each call is written to state/calls.log first (see _world).
    STUBS = {
        "systemctl": r'''
case "$1" in
  is-system-running) echo "${STUB_SYSTEMD:-running}"; [[ ${STUB_SYSTEMD:-running} == running ]];;
  is-active) [[ -e $STUB_STATE/enabled && -s $ARIS_DATA/gui_secrets.json ]];;   # the real service needs a login
  enable) touch "$STUB_STATE/enabled";;
  stop|disable) [[ -e $STUB_STATE/enabled ]] && rm -f "$STUB_STATE/enabled";;
esac''',
        "apt-get": r'''
if [[ $1 == update ]]; then [[ -z ${STUB_APT_OFFLINE:-} ]] || exit 100; touch "$STUB_STATE/lists"; exit 0; fi
[[ $1 == install ]] || exit 0
shift; skip=0
for a in "$@"; do
  if [[ $skip -eq 1 ]]; then skip=0; continue; fi
  case "$a" in -o) skip=1; continue;; -*) continue;; esac
  if [[ " ${STUB_APT_FAIL:-} " == *" $a "* ]]; then echo "E: stand-in apt-get: cannot install $a" >&2; exit 100; fi
  touch "$STUB_STATE/pkg_$a"
  case "$a" in
    rtl-sdr) tools="rtl_sdr rtl_power rtl_test rtl_adsb";; rtl-433) tools=rtl_433;; direwolf) tools=atest;;
    python3-numpy|python3-flask|python3-werkzeug|python3-ollama) tools=""; touch "$STUB_STATE/mod_${a#python3-}";;
    python3-pip) tools=""; touch "$STUB_STATE/pip";;
    *) tools=$a;;
  esac
  for t in $tools; do [[ ! -e $STUB_SHELF/$t ]] || cp -P "$STUB_SHELF/$t" "$STUB_BIN/$t"; done
done''',
        "apt-cache": r'''
[[ $1 == policy ]] || exit 0
word=Candidato; [[ ${LC_ALL:-} != C ]] || word=Candidate               # apt answers in the user's language
if [[ -n ${STUB_NO_LISTS:-} && ! -e $STUB_STATE/lists ]]; then         # no package lists yet: no package source is
  [[ -n ${2:-} && -e $STUB_STATE/pkg_$2 ]] || exit 0                   # listed, and only what is installed is known
  printf '%s\n' "$2:" "  Installed: 1.0-1" "  $word: 1.0-1"; exit 0
fi
[[ -n ${2:-} ]] || { echo " 500 http://archive.example/ubuntu noble/main amd64 Packages"; exit 0; }
[[ " ${STUB_APT_NONE-python3-ollama} " != *" $2 "* ]] || exit 0        # a package this release does not have
v=1.0-1; [[ $2 != rtl-433 ]] || v=${STUB_R433_PKG:-23.11-1build1}
printf '%s\n' "$2:" "  Installed: (none)" "  $word: $v"''',
        "dpkg-query": r'''
p=${*: -1}; [[ -e $STUB_STATE/pkg_$p ]] || exit 1; printf installed''',
        "runuser": r'''
[[ $1 != -u ]] || shift 2; [[ $1 != -- ]] || shift; exec "$@"''',
        "id": r'''
if [[ $# -eq 1 && $1 == aris ]]; then [[ -e $STUB_STATE/user_aris ]]; exit; fi
exec "@ID@" "$@"''',
        "adduser": r'''
touch "$STUB_STATE/user_aris"''',
        "getent": r'''
[[ $1 == group && $2 == plugdev ]] || exit 2; echo "plugdev:x:46:"''',
        "hostname": r'''
echo "192.0.2.10 "''',
        "df": r'''
echo "Filesystem 1024-blocks Used Available Capacity Mounted on"
echo "/dev/x 999999999 1 $(( ${STUB_FREE_GB:-50} * 1048576 )) 1% /"''',
        "curl": r'''
out=""; url=""
while [[ $# -gt 0 ]]; do case "$1" in -o) out=$2; shift;; --proto|-m) shift;; -*) ;; *) url=$1;; esac; shift; done
case "$url" in
  https://ollama.com/install.sh)
    [[ -z ${STUB_OLLAMA_DOWN:-} ]] || exit 22
    printf '%s\n' '#!/bin/sh' 'echo "(a stand-in for the installer of Ollama)"' \
      'cp "$STUB_SHELF/ollama" "$STUB_BIN/ollama" && touch "$STUB_STATE/ollama_up"' > "$out";;
  http://127.0.0.1:11434/*) [[ -e $STUB_STATE/ollama_up ]];;
  *) exit 6;;
esac''',
        "ollama": r'''
case "$1" in
  list) echo "NAME ID SIZE MODIFIED"; cat "$STUB_STATE/models" 2>/dev/null;;
  pull) [[ -z ${STUB_PULL_FAIL:-} ]] || exit 1; echo "$2 0123abcd 4.7 GB now" >> "$STUB_STATE/models";;
esac
exit 0''',
        "rtl_433": r'''
case "$1" in
  -V) echo "rtl_433 version ${STUB_R433:-23.11} (a date) inputs file rtl_tcp RTL-SDR" >&2;;
  -h) if [[ ${STUB_R433:-23.11} < 23.11 ]]; then        # the real help lines of 21.12 and of 23.11
        echo "  [-F kv | json | csv | mqtt | influx | syslog | null | help] Produce decoded output in given format."
        echo "       Append output to file with :<filename> (e.g. -F csv:log.csv), defaults to stdout."; exit 1
      fi
      echo "  [-F log | kv | json | csv | mqtt | influx | syslog | trigger | null | help] Produce decoded output in given format.";;
esac''',
        "python3": r'''
if [[ $1 == -c && $2 =~ ^import\ [a-z0-9_,\ ]+$ ]]; then                  # "can an ordinary user import these?"
  for m in ${2#import }; do [[ -e $STUB_STATE/mod_${m%,} ]] || exit 1; done; exit 0
fi
if [[ $1 == -m && $2 == pip ]]; then
  echo "pip ${*:3}" >> "$STUB_STATE/calls.log"
  [[ -e $STUB_STATE/pip ]] || { echo "No module named pip" >&2; exit 1; }
  [[ $3 != --version ]] || { echo "pip 24.0"; exit 0; }
  if [[ " $* " == *" --help "* ]]; then
    [[ -n ${STUB_PIP_OLD:-} ]] || printf '%s\n' "  --break-system-packages" "  --root-user-action <action>"; exit 0
  fi
  [[ -z ${STUB_PIP_FAIL:-} ]] || exit 1
  for a in "${@:4}"; do [[ $a == -* ]] || touch "$STUB_STATE/mod_$a"; done; exit 0
fi
exec "@PY@" "$@"''',
        "rsync": r'''
src=${*: -2:1}; dst=${*: -1}; mkdir -p "$dst" && cp -a "$src". "$dst"     # only where the computer has no rsync''',
    }
    QUIET = ("id", "python3", "runuser", "sleep", "getent", "df", "hostname")       # not written to calls.log
    PLAIN = ("usermod", "chown", "modprobe", "sleep", "zstd", "rtl_sdr", "rtl_power", "rtl_test", "rtl_adsb",
             "multimon-ng", "atest")                                                # stand-ins that only say yes
    ALWAYS = ("systemctl", "apt-get", "apt-cache", "dpkg-query", "runuser", "id", "getent", "hostname", "df",
              "usermod", "chown", "modprobe", "sleep")
    FRESH = ("python3", "git", "curl", "adduser")          # a new computer: the radio tools, rsync, Ollama are missing
    READY = FRESH + ("rsync", "rtl_sdr", "rtl_power", "rtl_test", "rtl_adsb", "rtl_433", "multimon-ng", "atest", "ollama")

    _fr = False

    @classmethod
    def _fake_root(cls):
        """The command prefix that makes a harmless stand-in root, or None where that cannot be had."""
        import shutil
        import subprocess
        if cls._fr is False:
            cls._fr = None
            unshare = shutil.which("unshare")
            pre = [unshare, "-r"] if unshare else None
            if pre and os.geteuid() == 0:           # the tests themselves run as root: become nobody first
                setpriv = shutil.which("setpriv")
                pre = [setpriv, "--reuid=65534", "--regid=65534", "--clear-groups"] + pre if setpriv else None
            if pre:
                probe = 'test "$(id -u)" = 0 && ! test -w /etc/passwd && ! test -w /etc'
                try:
                    if subprocess.run(pre + ["sh", "-c", probe], capture_output=True, timeout=20).returncode == 0:
                        cls._fr = pre
                except (OSError, subprocess.SubprocessError):
                    pass
        return cls._fr

    def setUp(self):
        self._dirs = []

    def tearDown(self):
        import shutil
        for d in self._dirs:
            shutil.rmtree(d, ignore_errors=True)

    def _tmp(self):
        import tempfile
        d = tempfile.mkdtemp(prefix="aris_inst_")
        self._dirs.append(d)
        if os.geteuid() == 0:
            os.chmod(d, 0o755)
        return d

    def _world(self, have=FRESH, mods=(), pkgs=("ca-certificates",), os_release=UBUNTU, user=False, **env):
        """A temporary folder that stands in for a computer: a copy of the code, an empty "/" and the stand-ins."""
        import shutil
        import sys
        if not self._fake_root():
            self.skipTest("no user namespaces here (unshare -r): the installer cannot run as a harmless stand-in root")
        w = self._tmp()
        p = {k: os.path.join(w, k) for k in ("src", "root", "bin", "shelf", "base", "state", "home", "t")}
        p["repo"] = os.path.join(w, "opt", "aris")
        p["data"] = os.path.join(w, "var", "lib", "aris")
        for k in ("bin", "shelf", "base", "state", "home", "t"):
            os.makedirs(p[k])
        for d in ("etc/systemd/system", "etc/modprobe.d"):
            os.makedirs(os.path.join(p["root"], d))
        if os_release:
            with open(os.path.join(p["root"], "etc", "os-release"), "w") as f:
                f.write(os_release)
        def skip(d, names):                           # the code only: no caches, no data files, no pictures
            return [n for n in names if n == "__pycache__" or (os.path.isfile(os.path.join(d, n))
                                                               and not n.endswith((".py", ".sh", ".service")))]
        os.makedirs(os.path.join(p["src"], "deploy"))
        for name in os.listdir(os.path.join(self.ROOT, "deploy")):
            if name.endswith((".py", ".sh", ".service")):
                shutil.copy(os.path.join(self.ROOT, "deploy", name), os.path.join(p["src"], "deploy"))
        for name in ("engine", "arislib", "arisweb"):
            shutil.copytree(os.path.join(self.ROOT, name), os.path.join(p["src"], name), ignore=skip)
        for name in ("aris.py", "aris_gui.py"):
            shutil.copy(os.path.join(self.ROOT, name), p["src"])
        bash = shutil.which("bash")
        for t in self.BASE:
            real = shutil.which(t)
            if real:
                os.symlink(real, os.path.join(p["base"], t))
        names = set(self.STUBS) | set(self.PLAIN)
        for name in names:                           # every stand-in waits on the shelf until it is "installed"
            body = self.STUBS.get(name, "\nexit 0").replace("@PY@", sys.executable).replace("@ID@", shutil.which("id"))
            log = "" if name in self.QUIET else 'echo "$(basename "$0") $*" >> "$STUB_STATE/calls.log"'
            path = os.path.join(p["shelf"], name)
            with open(path, "w") as f:
                f.write("#!%s\n%s%s\n" % (bash, log, body))
            os.chmod(path, 0o755)
        for name in ("git", "rsync"):                # the real ones, where the computer has them
            real = shutil.which(name)
            if real:
                if name in names:
                    os.remove(os.path.join(p["shelf"], name))
                os.symlink(real, os.path.join(p["shelf"], name))
        for name in set(self.ALWAYS) | set(have):
            shutil.copy(os.path.join(p["shelf"], name), os.path.join(p["bin"], name), follow_symlinks=False)
        for m in mods:
            open(os.path.join(p["state"], "mod_" + m), "w").close()
        for k in pkgs:
            open(os.path.join(p["state"], "pkg_" + k), "w").close()
        if "pip" in pkgs:
            open(os.path.join(p["state"], "pip"), "w").close()
        if user:
            open(os.path.join(p["state"], "user_aris"), "w").close()
        if "ollama" in have:
            open(os.path.join(p["state"], "ollama_up"), "w").close()
        open(os.path.join(p["state"], "calls.log"), "w").close()
        p["env"] = dict({"PATH": p["bin"] + os.pathsep + p["base"], "HOME": p["home"], "LANG": "C", "TMPDIR": p["t"],
                         "ARIS_ROOT": p["root"], "ARIS_REPO": p["repo"], "ARIS_DATA": p["data"],
                         "STUB_STATE": p["state"], "STUB_BIN": p["bin"], "STUB_SHELF": p["shelf"],
                         "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}, **env)
        p["w"] = w
        self._own(w)
        return p

    def _own(self, top):
        """When the tests run as root the installer runs as nobody: hand the temporary folder over."""
        if os.geteuid() != 0:
            return
        for d, dirs, files in os.walk(top):
            for n in [""] + dirs + files:
                os.lchown(os.path.join(d, n), 65534, 65534)

    @staticmethod
    def _plain(b):
        import re
        return re.sub(r"\x1b\[[0-9;]*m", "", b.decode("utf-8", "replace")).replace("\r", "")

    def _run(self, w, *args, script="install.sh", stdin=None, **env):
        """Run a deploy script in the world, with no terminal. Returns (exit code, what it printed)."""
        import subprocess
        cmd = self._fake_root() + [os.path.join(w["base"], "bash")]
        cmd += ["-s", "--"] if stdin else [os.path.join(w["src"], "deploy", script)]
        with (open(stdin, "rb") if stdin else open(os.devnull, "rb")) as f:
            r = subprocess.run(cmd + list(args), env=dict(w["env"], **env), stdin=f, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, start_new_session=True, timeout=180)
        return r.returncode, self._plain(r.stdout)

    def _run_tty(self, w, args, answers, script="install.sh", stdin=None, **env):
        """The same on a pseudo-terminal, typing `answers` [(prompt, reply)] as the prompts appear."""
        import pty
        import select
        cmd = self._fake_root() + [os.path.join(w["base"], "bash")]
        cmd += ["-s", "--"] if stdin else [os.path.join(w["src"], "deploy", script)]
        cmd += list(args)
        pid, fd = pty.fork()
        if pid == 0:                                  # the child: its terminal is the pseudo-terminal
            try:
                if stdin:                             # the script comes on standard input, as from "curl ... |"
                    os.dup2(os.open(stdin, os.O_RDONLY), 0)
                os.execve(cmd[0], cmd, dict(w["env"], **env))
            finally:
                os._exit(127)
        buf, pos, todo, end, status = b"", 0, list(answers), time.time() + 180, None
        while time.time() < end:
            if select.select([fd], [], [], 0.2)[0]:
                try:
                    d = os.read(fd, 4096)
                except OSError:
                    d = b""
                if not d:
                    break
                buf += d
                while todo and todo[0][0].encode() in buf[pos:]:
                    pos = buf.index(todo[0][0].encode(), pos) + len(todo[0][0])
                    os.write(fd, todo.pop(0)[1].encode() + b"\n")
            else:
                done, st = os.waitpid(pid, os.WNOHANG)
                if done:
                    status = st
                    break
        if status is None:
            try:
                os.kill(pid, 0)
                if time.time() >= end:
                    os.kill(pid, 9)
            except OSError:
                pass
            _, status = os.waitpid(pid, 0)
        os.close(fd)
        return (os.WEXITSTATUS(status) if os.WIFEXITED(status) else -1), self._plain(buf)

    @staticmethod
    def _calls(w, *words):
        """The stand-ins' calls, in order; with words, only the calls that contain all of them."""
        with open(os.path.join(w["state"], "calls.log")) as f:
            lines = [ln.rstrip("\n") for ln in f]
        return [ln for ln in lines if all(x in ln for x in words)]

    def _ai_calls(self, w):
        """Calls of the ollama program and fetches from ollama.com (the Python module "ollama" is another thing)."""
        return [c for c in self._calls(w) if c.startswith("ollama ") or "ollama.com" in c]

    def _untouched(self, w):
        """Nothing was installed or changed: no user, no code, no data, no unit, no blacklist, no package."""
        self.assertFalse(os.path.exists(w["repo"]))
        self.assertFalse(os.path.exists(w["data"]))
        self.assertEqual(os.listdir(os.path.join(w["root"], "etc", "systemd", "system")), [])
        self.assertEqual(os.listdir(os.path.join(w["root"], "etc", "modprobe.d")), [])
        self.assertFalse(os.path.exists(os.path.join(w["state"], "user_aris")))
        changing = [c for c in self._calls(w) if c.split()[0] in ("apt-get", "pip", "adduser", "usermod", "chown",
                                                                    "modprobe", "ollama", "rsync")
                    and not c.startswith(("ollama list", "pip --version", "pip install --help"))]
        changing += [c for c in self._calls(w, "systemctl") if c.split()[1] not in ("is-system-running", "is-active")]
        changing += self._calls(w, "curl", "ollama.com")
        self.assertEqual(changing, [])

    # ---- static: what the scripts are -------------------------------------------------------------------------
    def test_both_scripts_parse_and_are_lint_clean(self):
        import shutil
        import subprocess
        for p in (self.INSTALL, self.GET):
            self.assertEqual(subprocess.run(["bash", "-n", p]).returncode, 0, p)
        if shutil.which("shellcheck"):               # warnings and errors; the notes left are the "A && B || C" style
            r = subprocess.run(["shellcheck", "-S", "warning", self.INSTALL, self.GET], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout)

    def test_the_installer_stays_safe_by_default(self):
        with open(self.INSTALL, encoding="utf-8") as f:
            s = f.read()
        for want in ("MODE=dry", "--everything", "--no-ai", "--apply", "--remove", "set -euo pipefail",
                     "https://ollama.com/install.sh", "--proto '=https'"):
            self.assertIn(want, s)
        for never in ("rm -rf", "/root", "eval ", "curl -k", "--insecure", "| sh", "| bash", "http://ollama"):
            self.assertNotIn(never, s)
        self.assertNotIn("EUID", s.replace("[[ $EUID -eq 0 ]] ||", "", 1))      # one root check, and no way round it

    def test_get_is_one_function_called_on_its_last_line(self):
        with open(self.GET, encoding="utf-8") as f:
            lines = f.read().split("\n")
        self.assertEqual(lines[-1], "")                                # ends with a newline
        self.assertEqual(lines[-2], 'aris_get "$@"')
        self.assertEqual(lines[-3], "}")
        start = lines.index("aris_get() {")
        for ln in lines[:start]:                                       # before it: the #! line and comments only
            self.assertTrue(ln.startswith("#") or ln == "", ln)
        for ln in lines[start + 1:-3]:                                 # the body: indented, so inside the function
            self.assertTrue(ln.startswith("  ") or ln == "", ln)

    def test_get_fetches_over_https_only_and_removes_only_its_own_folder(self):
        import re
        with open(self.GET, encoding="utf-8") as f:
            s = f.read()
        for never in ("curl -k", "--insecure", "eval ", "http://", "git://", "sslVerify", "GIT_SSL_NO_VERIFY"):
            self.assertNotIn(never, s)
        self.assertIn("protocol.allow=never", s)                       # git may use nothing but what is allowed next
        self.assertIn("protocol.https.allow=always", s)
        self.assertIn('mktemp -d "${TMPDIR:-/tmp}/aris-get.XXXXXXXX"', s)
        self.assertEqual(re.findall(r"\brm\b.*", s), ['rm -rf -- "$ARIS_GET_TMP" && echo "Removed the temporary folder $ARIS_GET_TMP."'])
        self.assertIn('if [[ -n ${ARIS_GET_TMP:-} && -d $ARIS_GET_TMP && $ARIS_GET_TMP == */aris-get.* ]]; then', s)
        self.assertNotIn("/opt/aris", s)                               # the station's folders are install.sh's business
        self.assertNotIn("/var/lib/aris", s)

    def test_a_download_of_get_that_was_cut_off_runs_nothing(self):
        import shutil
        import subprocess
        with open(self.GET, "rb") as f:
            text = f.read()
        empty = self._tmp()                                            # no programs at all on PATH, and an empty "/"
        os.chmod(empty, 0o755)
        pre = []
        if os.geteuid() == 0:                                          # as root the whole script would go on: be nobody
            if not shutil.which("setpriv"):
                self.skipTest("the tests run as root and cannot become an ordinary user (no setpriv)")
            pre = [shutil.which("setpriv"), "--reuid=65534", "--regid=65534", "--clear-groups"]
        env = {"PATH": empty, "ARIS_ROOT": empty, "HOME": empty}

        def out(script):
            r = subprocess.run(pre + [shutil.which("bash")], input=script, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, start_new_session=True, timeout=30)
            return r.stdout
        self.assertIn(b"Run as root", out(text))                       # whole: it starts (and stops: not root)
        cuts = [i + 1 for i, c in enumerate(text) if c == 10] + list(range(len(text) - 60, len(text)))
        last = text.rstrip().rfind(b"\n") + 1                          # where the last line, the call, begins
        for n in sorted(set(cuts)):
            got = out(text[:n])
            if text[:n].rstrip() in (text.rstrip(), text[:last] + b"aris_get"):
                self.assertIn(b"Run as root", got)                     # the call is whole (cut right after the name:
            else:                                                      # the options are lost, so it asks first)
                self.assertEqual(got, b"", "cut at byte %d of %d" % (n, len(text)))

    # ---- run: the installer against stand-ins ------------------------------------------------------------------
    def test_it_refuses_without_root(self):
        import shutil
        import subprocess
        pre = []
        if os.geteuid() == 0:
            if not shutil.which("setpriv"):
                self.skipTest("the tests run as root and cannot become an ordinary user (no setpriv)")
            pre = [shutil.which("setpriv"), "--reuid=65534", "--regid=65534", "--clear-groups"]
        empty = self._tmp()
        here = self._tmp()                  # copies an ordinary user can read, wherever the checkout lives
        for f in (self.INSTALL, self.GET):
            shutil.copy(f, here)
            os.chmod(os.path.join(here, os.path.basename(f)), 0o644)
        os.chmod(here, 0o755)
        inst, get = (os.path.join(here, os.path.basename(f)) for f in (self.INSTALL, self.GET))
        for script, args in ((inst, ["--everything", "--apply"]), (inst, ["--apply"]), (get, ["--yes"])):
            r = subprocess.run(pre + ["bash", script] + args, env={"PATH": os.environ["PATH"], "ARIS_ROOT": empty,
                               "ARIS_REPO": os.path.join(empty, "r"), "ARIS_DATA": os.path.join(empty, "d")},
                               capture_output=True, text=True, start_new_session=True, timeout=30)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertTrue(r.stdout.startswith("Run as root"), r.stdout)
            self.assertEqual(len(r.stdout.strip().split("\n")), 1)
            self.assertEqual(os.listdir(empty), [])

    def test_everything_dry_run_shows_the_whole_plan_and_changes_nothing(self):
        w = self._world()
        rc, out = self._run(w, "--everything")
        self.assertEqual(rc, 0, out)
        for want in ("1. This computer", "Ubuntu 24.04.1 LTS", "systemd is running", "2. Packages", "3. AI part",
                     "4. Checks", "5. Plan",
                     "with apt-get: rsync python3-numpy python3-flask python3-werkzeug python3-pip python3-httpx rtl-sdr rtl-433 "
                     "multimon-ng direwolf zstd",
                     "install with pip, system-wide, from PyPI: ollama",
                     "download https://ollama.com/install.sh and run it as root", "a third-party", "--no-ai",
                     "ollama pull qwen2.5:7b (about 4.7 GB)", "aris-rtl-sdr.conf", "dvb_usb_rtl28xxu",
                     "create the system user aris", "ask for the web page's login",
                     "Dry run: nothing was changed. To install:  sudo bash", "--everything --apply"):
            self.assertIn(want, out)
        self.assertNotIn("FIX", out)
        self._untouched(w)
        rc, out = self._run(w, "--everything", "--no-ai")
        self.assertEqual(rc, 0, out)
        self.assertIn("the AI part is left out (--no-ai)", out)
        self.assertIn("--everything --no-ai --apply", out)
        self.assertNotIn("ollama.com/install.sh and run", out)
        self.assertNotIn("zstd", out)
        self._untouched(w)

    def test_everything_apply_installs_all_of_it_and_a_second_run_changes_nothing_more(self):
        w = self._world()
        rc, out = self._run(w, "--everything", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("FIX", out)
        self.assertEqual(self._calls(w, "apt-get")[0], "apt-get update -q")
        self.assertEqual(self._calls(w, "apt-get", "install")[0].split()[-5:],            # the needed ones in one go
                         ["rsync", "python3-numpy", "python3-flask", "python3-werkzeug", "python3-pip"])
        for c in self._calls(w, "apt-get", "install"):
            self.assertIn("-y -q --no-install-recommends", c)
        self.assertEqual([c.split()[-1] for c in self._calls(w, "apt-get", "install")[1:]],     # the others one by one
                         ["python3-httpx", "rtl-sdr", "rtl-433", "multimon-ng", "direwolf", "zstd"])
        self.assertLess(self._calls(w).index(self._calls(w, "apt-get", "install", "python3-httpx")[0]),
                        self._calls(w).index(self._calls(w, "pip install", "ollama")[0]))   # before pip: pip adds less
        pip = self._calls(w, "pip install", "ollama")
        self.assertEqual(len(pip), 1)                                  # this release has no python3-ollama package
        self.assertIn("--break-system-packages", pip[0])
        self.assertEqual(self._calls(w, "curl", "ollama.com"),
                         [c for c in self._calls(w, "curl") if "--proto =https" in c and c.endswith("https://ollama.com/install.sh")])
        self.assertEqual(len(self._calls(w, "curl", "ollama.com")), 1)
        self.assertEqual(self._calls(w, "ollama pull"), ["ollama pull qwen2.5:7b"])
        self.assertEqual(len(self._calls(w, "adduser", "--system", "--shell /usr/sbin/nologin aris")), 1)
        self.assertEqual(self._calls(w, "systemctl enable"), ["systemctl enable --now aris.service"])
        self.assertTrue(os.path.isfile(os.path.join(w["repo"], "aris.py")))
        self.assertEqual(os.stat(w["data"]).st_mode & 0o777, 0o700)
        with open(os.path.join(w["root"], "etc", "systemd", "system", "aris.service")) as f:
            unit = f.read()
        self.assertIn("ExecStart=/usr/bin/env python3 %s/aris_gui.py" % w["repo"], unit)
        self.assertNotIn("@REPO@", unit)
        with open(os.path.join(w["root"], "etc", "modprobe.d", "aris-rtl-sdr.conf")) as f:
            self.assertEqual([ln for ln in f.read().split("\n") if ln and not ln.startswith("#")], ["blacklist dvb_usb_rtl28xxu"])
        for want in ("the service waits for a login", "no terminal here to ask for the login", "--set-password",
                     "http://localhost:5000 on this computer", "http://192.0.2.10:5000 from another device",
                     "/check", "/setup", "unplug it and plug it in again", "deploy/update.sh"):
            self.assertIn(want, out)
        with open(os.path.join(w["data"], "baseline.keep"), "w") as f:      # the station's own data
            f.write("mine")
        self._own(w["data"])
        open(os.path.join(w["state"], "calls.log"), "w").close()
        self._own(w["state"])
        rc, out = self._run(w, "--everything", "--apply")                   # again: an upgrade
        self.assertEqual(rc, 0, out)
        self.assertIn("nothing to install", out)
        self.assertIn("user aris exists", out)
        self.assertIn("already kept off RTL-SDR dongles", out)
        for never in ("apt-get", "pip install", "ollama pull", "adduser", "ollama.com", "modprobe"):
            self.assertEqual(self._calls(w, never), [], never)
        with open(os.path.join(w["data"], "baseline.keep")) as f:
            self.assertEqual(f.read(), "mine")

    def test_a_computer_without_python_or_git_gets_them_first(self):
        w = self._world(have=("curl", "adduser"))
        rc, out = self._run(w, "--everything", "--no-ai")
        self.assertEqual(rc, 0, out)
        for want in ("NOTE python3: not here yet, to install", "NOTE git: not here yet, to install",
                     "NOTE python3 is not here yet: the install brings it",
                     "NOTE python modules numpy, flask, werkzeug, ollama: not all there yet",
                     "NOTE the code is compiled as a check once python3 is there",
                     "with apt-get: python3 rsync git python3-numpy python3-flask python3-werkzeug python3-pip"):
            self.assertIn(want, out)
        self.assertNotIn("FIX", out)
        self._untouched(w)
        rc, out = self._run(w, "--everything", "--apply", "--no-ai")
        self.assertEqual(rc, 0, out)
        self.assertIn("OK   code compiles", out)
        self.assertIn("OK   python modules numpy, flask, werkzeug, ollama (visible to an ordinary user)", out)
        self.assertTrue(os.path.isfile(os.path.join(w["repo"], "deploy", "install.sh")))

    def test_a_release_with_its_own_ollama_package_needs_no_pip(self):
        w = self._world(STUB_APT_NONE="")
        rc, out = self._run(w, "--everything", "--apply", "--no-ai")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._calls(w, "apt-get", "install")[0].split()[-5:],
                         ["rsync", "python3-numpy", "python3-flask", "python3-werkzeug", "python3-ollama"])
        self.assertEqual(self._calls(w, "pip install"), [])
        self.assertEqual(self._calls(w, "python3-pip"), [])

    def test_old_pip_and_a_computer_without_package_lists(self):
        w = self._world(STUB_PIP_OLD="1", STUB_NO_LISTS="1")
        rc, out = self._run(w, "--everything", "--no-ai")
        self.assertEqual(rc, 0, out)
        self.assertIn("this computer has no package lists yet", out)
        self.assertIn("python3-ollama where this release has it, else with pip", out)
        self._untouched(w)
        rc, out = self._run(w, "--everything", "--apply", "--no-ai")
        self.assertEqual(rc, 0, out)
        self.assertIn("no package python3-ollama in this release: the module comes from pip instead", out)
        pip = self._calls(w, "pip install", "ollama")
        self.assertEqual(len(pip), 1)
        self.assertNotIn("--break-system-packages", pip[0])            # a pip that does not know the flag
        self.assertNotIn("--root-user-action", pip[0])

    def test_no_ai_leaves_ollama_alone(self):
        w = self._world()
        rc, out = self._run(w, "--everything", "--apply", "--no-ai")
        self.assertEqual(rc, 0, out)
        self.assertIn("left out (--no-ai)", out)
        self.assertEqual(self._ai_calls(w), [])                        # neither the installer nor the program
        self.assertEqual(self._calls(w, "zstd"), [])
        self.assertTrue(os.path.isfile(os.path.join(w["repo"], "aris.py")))

    def test_the_ai_part_never_fails_the_install(self):
        for env, said in (({"STUB_OLLAMA_DOWN": "1"}, "Ollama could not be installed"),
                          ({"STUB_PULL_FAIL": "1"}, "the model could not be downloaded"),
                          ({"STUB_FREE_GB": "7"}, "only 7 GB free")):
            w = self._world(**env)
            rc, out = self._run(w, "--everything", "--apply")
            self.assertEqual(rc, 0, out)
            self.assertIn(said, out)
            self.assertNotIn("FIX", out)
            self.assertEqual(self._calls(w, "systemctl enable"), ["systemctl enable --now aris.service"])
            if "STUB_FREE_GB" in env:                                  # too little room: not even Ollama's installer
                self.assertEqual(self._ai_calls(w), [])
        w = self._world(have=self.FRESH + ("ollama",), STUB_FREE_GB="7")   # Ollama is there: the model alone fits
        rc, out = self._run(w, "--everything", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._calls(w, "ollama pull"), ["ollama pull qwen2.5:7b"])
        self.assertEqual(self._calls(w, "ollama.com"), [])

    def test_an_rtl_433_too_old_for_aris_is_named_with_what_to_do(self):
        w = self._world(have=self.FRESH + ("rtl_433",), STUB_R433="21.12")           # the program is here, and old
        rc, out = self._run(w, "--everything", "--no-ai")
        self.assertEqual(rc, 0, out)
        for want in ("NOTE rtl_433 21.12 is older than 23.11", '"-F log"', "most sensor decoding will not work",
                     "https://github.com/merbanan/rtl_433", "/usr/local/bin", "Debian 13, Ubuntu 24.04 or later"):
            self.assertIn(want, out)
        self.assertNotIn("FIX", out)
        w = self._world(os_release=self.DEBIAN, STUB_R433_PKG="22.11-1", STUB_R433="22.11")   # only the package is old
        rc, out = self._run(w, "--everything", "--no-ai")
        self.assertEqual(rc, 0, out)
        self.assertIn("NOTE rtl_433 in this release (22.11-1) is older than 23.11", out)
        rc, out = self._run(w, "--everything", "--apply", "--no-ai")                 # it is installed all the same
        self.assertEqual(rc, 0, out)
        self.assertEqual(out.count("is older than 23.11"), 1)
        self.assertIn("rtl_433 22.11 is too old for ARIS (see above)", out)
        w = self._world(have=self.READY, mods=("numpy", "flask", "werkzeug", "ollama"), STUB_R433="21.12")
        rc, out = self._run(w)                                                        # the plain dry run says it too
        self.assertEqual(rc, 0, out)
        self.assertIn("NOTE rtl_433 21.12 is older than 23.11", out)
        w = self._world(have=self.READY, mods=("numpy", "flask", "werkzeug", "ollama"), STUB_R433="25.12")
        rc, out = self._run(w)
        self.assertIn("OK   rtl_433 (sensor decoding)", out)
        self.assertNotIn("older than", out)

    def test_without_systemd_or_on_another_system_it_stops_before_anything(self):
        w = self._world(STUB_SYSTEMD="offline")
        for args in (("--everything",), ("--everything", "--apply")):
            rc, out = self._run(w, *args)
            self.assertEqual(rc, 1, out)
            self.assertIn("FIX  systemd is not running here (a container", out)
            self.assertIn("Nothing was changed.", out)
            self.assertNotIn("2. Packages", out)
            self._untouched(w)
        os.makedirs(os.path.join(w["root"], "proc"))
        with open(os.path.join(w["root"], "proc", "version"), "w") as f:
            f.write("Linux version 5.15.167.4-microsoft-standard-WSL2 (a build line)\n")
        self._own(os.path.join(w["root"], "proc"))
        rc, out = self._run(w, "--everything", "--apply")
        self.assertEqual(rc, 1, out)
        for want in ("systemd is not running in this WSL system", "/etc/wsl.conf", "[boot]", "systemd=true", "wsl --shutdown"):
            self.assertIn(want, out)
        self._untouched(w)
        rc, out = self._run(w, script="get.sh", STUB_SYSTEMD="offline")
        self.assertEqual(rc, 1, out)
        for want in ("/etc/wsl.conf", "[boot]", "systemd=true", "wsl --shutdown"):
            self.assertIn(want, out)
        os.makedirs(os.path.join(w["root"], "run", "systemd", "system"))         # systemd's own mark that it runs
        self._own(os.path.join(w["root"], "run"))
        rc, out = self._run(w, "--everything", "--no-ai")
        self.assertEqual(rc, 0, out)
        self.assertIn("OK   systemd is running", out)
        w = self._world(os_release=self.FEDORA)
        for script, args in (("install.sh", ("--everything", "--apply")), ("get.sh", ("--yes",))):
            rc, out = self._run(w, *args, script=script)
            self.assertEqual(rc, 1, out)
            self.assertIn("for Debian and Ubuntu", out)
            self.assertIn("Fedora Linux 41", out)
            self._untouched(w)

    def test_a_package_that_cannot_be_had(self):
        w = self._world(STUB_APT_NONE="python3-ollama multimon-ng", STUB_APT_FAIL="direwolf")   # decoders: a NOTE
        rc, out = self._run(w, "--everything", "--apply", "--no-ai")
        self.assertEqual(rc, 0, out)
        self.assertIn("NOTE no package multimon-ng in this computer's package sources: left out", out)
        self.assertIn("NOTE apt-get could not install direwolf", out)
        self.assertIn("multimon-ng not found (optional decoder)", out)
        self.assertEqual(self._calls(w, "systemctl enable"), ["systemctl enable --now aris.service"])
        w = self._world(STUB_APT_FAIL="python3-flask")                                         # needed: stop
        rc, out = self._run(w, "--everything", "--apply", "--no-ai")
        self.assertEqual(rc, 1, out)
        self.assertIn("FIX  apt-get could not install: rsync python3-numpy python3-flask python3-werkzeug", out)
        self.assertIn("ARIS itself was not installed.", out)
        self.assertFalse(os.path.exists(w["repo"]))
        self.assertEqual(self._calls(w, "adduser"), [])

    def test_the_plain_install_does_none_of_the_extra_steps(self):
        w = self._world(have=self.READY, mods=("numpy", "flask", "werkzeug", "ollama"))
        rc, out = self._run(w)
        self.assertEqual(rc, 0, out)
        self.assertEqual([ln for ln in out.split("\n") if ln[:1].isdigit()], ["1. Checks", "2. Plan"])
        self.assertTrue(out.rstrip().endswith("install.sh --apply"))
        self._untouched(w)
        rc, out = self._run(w, "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual([ln for ln in out.split("\n") if ln[:1].isdigit()],
                         ["1. Checks", "2. Plan", "3. Installing", "4. Next"])
        self.assertIn("NOTE the service waits for a login", out)        # a new install: no FIX for the missing login
        self.assertIn("Set the login:   sudo -u aris env ARIS_DATA_DIR=", out)
        for never in ("apt-get", "pip", "ollama.com", "ollama pull", "modprobe"):
            self.assertEqual(self._calls(w, never), [], never)
        self.assertEqual(os.listdir(os.path.join(w["root"], "etc", "modprobe.d")), [])
        self.assertEqual(os.listdir(os.path.join(w["root"], "etc", "systemd", "system")), ["aris.service"])
        w2 = self._world(have=self.READY)                               # modules missing: plain mode does not fetch them
        rc, out = self._run(w2, "--apply")
        self.assertEqual(rc, 1, out)
        self.assertIn("FIX  an ordinary user cannot import numpy / flask / werkzeug / ollama", out)
        self.assertIn("Nothing was changed.", out)
        self._untouched(w2)
        for bad in (("--no-ai",), ("--frobnicate",), ("--everything", "now")):
            rc, out = self._run(w2, *bad)
            self.assertEqual(rc, 2, out)

    def test_the_login_is_asked_on_the_terminal_and_never_shown(self):
        w = self._world(have=self.READY, mods=("numpy", "flask", "werkzeug", "ollama"))
        with open(os.path.join(w["state"], "models"), "w") as f:
            f.write("qwen2.5:7b 0123abcd 4.7 GB now\n")
        self._own(w["state"])
        secret = "correct-horse-battery"
        rc, out = self._run_tty(w, ["--everything", "--apply"], [("Username [admin]: ", "keeper"),
                                ("New password (min 8 chars): ", secret), ("Repeat password: ", secret)])
        self.assertEqual(rc, 0, out)
        self.assertIn("Choose the name and the password for the web page", out)
        self.assertIn("OK   login set", out)
        self.assertIn("OK   service running", out)
        self.assertNotIn(secret, out)                                   # typed, not echoed
        path = os.path.join(w["data"], "gui_secrets.json")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with open(path) as f:
            saved = f.read()
        self.assertIn('"admin_user": "keeper"', saved)
        self.assertNotIn(secret, saved)                                 # a hash only
        rc, out = self._run_tty(w, ["--everything", "--apply"], [])     # again: it does not ask a second time
        self.assertEqual(rc, 0, out)
        self.assertIn("a login is set already", out)
        self.assertNotIn("Username", out)
        with open(path) as f:
            self.assertEqual(f.read(), saved)

    def test_remove_takes_the_unit_and_the_blacklist_and_keeps_code_and_data(self):
        w = self._world()
        rc, out = self._run(w, "--everything", "--apply", "--no-ai")
        self.assertEqual(rc, 0, out)
        rc, out = self._run(w, "--remove")
        self.assertEqual(rc, 0, out)
        self.assertIn("unit removed", out)
        self.assertIn("aris-rtl-sdr.conf", out)
        self.assertEqual(os.listdir(os.path.join(w["root"], "etc", "systemd", "system")), [])
        self.assertEqual(os.listdir(os.path.join(w["root"], "etc", "modprobe.d")), [])
        self.assertTrue(os.path.isfile(os.path.join(w["repo"], "aris.py")))
        self.assertTrue(os.path.isdir(w["data"]))
        self.assertEqual(self._calls(w, "systemctl disable"), ["systemctl disable --now aris.service"])

    def _repo(self, w):
        """The world's copy of the code as a git repository, as GitHub would hand it out."""
        import subprocess
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1", HOME=w["home"])
        for cmd in (["init", "-q"], ["symbolic-ref", "HEAD", "refs/heads/main"], ["add", "-A"],
                    ["-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
                     "commit", "-q", "-m", "a copy for the test"]):
            subprocess.run(["git", "-c", "safe.directory=*"] + cmd, cwd=w["src"], env=env, check=True, capture_output=True)
        self._own(w["w"])
        return w["src"]

    def test_get_shows_the_plan_and_asks_before_it_installs(self):
        w = self._world()
        url = self._repo(w)
        rc, out = self._run(w, stdin=self.GET, ARIS_GET_URL=url)        # piped like the one-liner, no terminal
        self.assertEqual(rc, 1, out)
        for want in ("Fetching ARIS (branch main)", "The plan:", "5. Plan", "ollama.com/install.sh",
                     "Dry run: nothing was changed.", "There is no terminal to ask on, so nothing was installed",
                     "| sudo bash -s -- --yes", "Removed the temporary folder"):
            self.assertIn(want, out)
        self.assertNotIn("To install:  sudo bash", out)                 # (the folder it ran from is gone by then)
        self.assertEqual(os.listdir(w["t"]), [])
        self._untouched(w)
        rc, out = self._run_tty(w, [], [("Install? [y/N] ", "n")], stdin=self.GET, ARIS_GET_URL=url)
        self.assertEqual(rc, 0, out)
        self.assertIn("Nothing was installed.", out)
        self.assertEqual(os.listdir(w["t"]), [])
        self._untouched(w)
        rc, out = self._run(w, "--yes", "--no-ai", stdin=self.GET, ARIS_GET_URL=url)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("Install? [y/N]", out)
        self.assertIn("left out (--no-ai)", out)
        self.assertIn("Removed the temporary folder", out)
        self.assertEqual(os.listdir(w["t"]), [])
        self.assertTrue(os.path.isfile(os.path.join(w["repo"], "aris.py")))
        self.assertTrue(os.path.isdir(os.path.join(w["repo"], ".git")))   # a checkout: deploy/update.sh can pull
        self.assertEqual(self._calls(w, "systemctl enable"), ["systemctl enable --now aris.service"])
        self.assertEqual(self._ai_calls(w), [])

    def test_a_strict_umask_still_leaves_the_code_readable_for_the_service_user(self):
        import stat
        w = self._world()
        url = self._repo(w)
        old = os.umask(0o077)                 # a hardened root shell: what git fetches is readable by root only
        try:
            rc, out = self._run(w, "--yes", "--no-ai", stdin=self.GET, ARIS_GET_URL=url)
        finally:
            os.umask(old)
        self.assertEqual(rc, 0, out)
        for p in (w["repo"], os.path.join(w["repo"], "aris_gui.py"), os.path.join(w["repo"], "arisweb"),
                  os.path.join(w["repo"], "arisweb", "app.py")):
            m = stat.S_IMODE(os.stat(p).st_mode)
            self.assertEqual(m & 0o004, 0o004, "%s is %o: the service user cannot read it" % (p, m))
            self.assertEqual(m & 0o022, 0, "%s is %o: others can write to it" % (p, m))
        self.assertEqual(stat.S_IMODE(os.stat(w["repo"]).st_mode) & 0o001, 0o001)       # and enter the folder

    def test_get_on_a_terminal_installs_on_yes_and_fetches_git_when_it_is_missing(self):
        w = self._world(have=("python3", "curl", "adduser"))            # no git yet
        url = self._repo(w)
        rc, out = self._run_tty(w, ["--no-ai"], [("Install? [y/N] ", "y"), ("Username [admin]: ", ""),
                                ("New password (min 8 chars): ", "a-long-password"), ("Repeat password: ", "a-long-password")],
                                stdin=self.GET, ARIS_GET_URL=url)
        self.assertEqual(rc, 0, out)
        self.assertIn("git is needed to fetch ARIS", out)
        self.assertIn("git ca-certificates", self._calls(w, "apt-get", "install")[0])
        self.assertIn("OK   login set", out)
        self.assertIn("OK   service running", out)
        self.assertEqual(os.listdir(w["t"]), [])
        rc, out = self._run(w, "--yes", stdin=self.GET, ARIS_GET_URL="http://example.org/aris.git")
        self.assertEqual(rc, 2, out)
        self.assertIn("must be an https:// address", out)
        rc, out = self._run(w, "--yes", "--branch", "no-such-branch", stdin=self.GET, ARIS_GET_URL=url)
        self.assertEqual(rc, 1, out)
        self.assertIn("The code could not be fetched", out)
        self.assertEqual(os.listdir(w["t"]), [])


if __name__ == "__main__":
    unittest.main()
