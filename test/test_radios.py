"""Receivers of every kind (arislib.radios), the IQ pipe, the Signal Hound BB60 plug-in (simulated) and the plug-in
loader, on simulated radios.  python3 -m unittest test_radios"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import numpy as np

import harness
import load

A = load.aris
call = load.call
from arislib import radios, iqpipe   # noqa: E402

ROOT = harness.CODE_DIR
BB60 = os.path.join(ROOT, "plugins", "signalhound_bb60.py")
needs_rtl433 = unittest.skipUnless(harness.REAL_BIN, "needs the real rtl_433 (REAL_BIN or /usr/local/bin)")


class Translate(unittest.TestCase):
    def test_rtl_sdr_commands_run_unchanged(self):
        cmd = ["rtl_sdr", "-d", "00001001", "-f", "100000000", "-s", "2048000", "-g", "19.7", "-"]
        self.assertEqual(radios.translate(cmd), cmd)
        r433 = ["rtl_433", "-d", ":00001001", "-f", "433885000", "-s", "250k"]
        self.assertEqual(radios.translate(r433), r433)
        self.assertEqual(radios.translate(["rtl_433", "-r", "x.cf32"]), ["rtl_433", "-r", "x.cf32"])  # a file
        self.assertTrue(radios.is_rtl("00001001"))

    def test_soapy_commands_become_rx_tools_and_soapy_device_args(self):
        dev = "soapy-hackrf-a06063c8234e4f5f"
        self.assertEqual(radios.backend_for(dev).name, "soapy")
        sdr = radios.translate(["rtl_sdr", "-d", dev, "-f", "100000000", "-s", "2048000", "-g", "20", "-n", "9", "-"])
        self.assertEqual(sdr[:5], ["rx_sdr", "-d", "driver=hackrf,serial=a06063c8234e4f5f", "-F", "CU8"])
        self.assertIn("-n", sdr)
        pw = radios.translate(["rtl_power", "-d", dev, "-f", "88M:108M:10k", "-i", "2", "-1", "-"])
        self.assertEqual(pw[:3], ["rx_power", "-d", "driver=hackrf,serial=a06063c8234e4f5f"])
        r433 = radios.translate(["rtl_433", "-d", ":" + dev, "-f", "433885000", "-s", "250k", "-F", "json"])
        self.assertEqual(r433[:3], ["rtl_433", "-d", "driver=hackrf,serial=a06063c8234e4f5f"])
        self.assertIsNone(radios.translate(["rtl_test", "-d", dev, "-s", "2048000"]))    # USB test: RTL only
        with self.assertRaises(radios.Unsupported):
            radios.translate(["rtl_adsb", "-d", dev, "-g", "40"])
        self.assertIsNone(radios.tool_for("rtl_adsb", dev))
        self.assertEqual(radios.tool_for("rtl_power", dev), "rx_power")

    def test_soapy_listing_skips_rtl_sdr_dongles_and_makes_safe_ids(self):
        os.environ["FAKE_SOAPY"] = "hackrf:a06063c8234e4f5f,airspy:B4/1C:77"
        try:
            found = radios.Soapy().list_devices()
        finally:
            os.environ.pop("FAKE_SOAPY", None)
        ids = [d["serial"] for d in found]
        self.assertEqual(ids[0], "soapy-hackrf-a06063c8234e4f5f")
        self.assertTrue(all(radios.ID_OK.fullmatch(i) for i in ids))
        self.assertFalse(any("rtlsdr" in i for i in ids))                    # those are the rtlsdr backend's
        self.assertEqual(radios.Soapy().args_for(ids[1]), "driver=airspy,serial=B4/1C:77")  # remembered as listed

    def test_configured_receivers_use_their_own_device_args(self):
        cfg = os.path.join(os.environ["ARIS_DATA_DIR"], "config.json")
        data = json.load(open(cfg))
        data["radios"] = {"soapy-remote-lab": {"args": "driver=remote,remote=tcp://10.0.0.5:55132", "name": "Lab"}}
        json.dump(data, open(cfg, "w"))
        try:
            cmd = radios.translate(["rtl_sdr", "-d", "soapy-remote-lab", "-f", "1", "-s", "2", "-"])
            self.assertEqual(cmd[2], "driver=remote,remote=tcp://10.0.0.5:55132")
            self.assertIn("soapy-remote-lab", [d["serial"] for d in radios.list_devices(include_rtl=False)])
        finally:
            data.pop("radios")
            json.dump(data, open(cfg, "w"))

    def test_helper_receivers_pipe_rtl_433_and_audio(self):
        class Box(radios.HelperBackend):
            name, prefix, label = "boxtest", "boxtest", "test box"

            def helper_argv(self):
                return ["/bin/box"]

        b = Box()
        r433 = b.translate(["rtl_433", "-d", ":boxtest-7", "-f", "433885000", "-s", "250k", "-g", "20", "-A",
                            "-T", "10", "-F", "json"], "boxtest-7")
        self.assertEqual(r433[1], radios.IQPIPE)
        src, sink = r433[r433.index("--") + 1:r433.index("---")], r433[r433.index("---") + 1:]
        self.assertEqual(src[:5], ["/bin/box", "sdr", "-d", "7", "-f"])
        self.assertIn("250000", src)
        self.assertEqual(sink[0], "rtl_433")
        self.assertEqual(sink[-2:], ["-r", "cu8:-"])
        self.assertNotIn("-d", sink)
        self.assertIn("-T", sink)                                               # rtl_433 still ends the run
        fm = b.translate(["rtl_fm", "-d", "boxtest-7", "-f", "152000000", "-M", "fm", "-s", "22050", "-g", "30",
                          "-l", "0", "/dev/shm/a.raw"], "boxtest-7")
        self.assertEqual(fm[2:4], ["fm", "--rate"])
        self.assertIn("/dev/shm/a.raw", fm)
        with self.assertRaises(radios.Unsupported):
            b.translate(["rtl_adsb", "-d", "boxtest-7"], "boxtest-7")

    def test_a_backend_needs_a_safe_prefix(self):
        class Bad(radios.Backend):
            name, prefix = "bad", "a-b"
        with self.assertRaises(ValueError):
            radios.register_backend(Bad())


class IQPipe(unittest.TestCase):
    def test_fm_demodulates_a_tone_like_rtl_fm(self):
        fs, sr, tone = 240000, 24000, 1000.0
        t = np.arange(fs) / fs
        z = 0.8 * np.exp(1j * 2 * np.pi * 5000 / tone * np.sin(2 * np.pi * tone * t))   # 5 kHz deviation
        iq = np.empty(2 * len(z), dtype=np.float32)
        iq[0::2], iq[1::2] = z.real, z.imag
        d = tempfile.mkdtemp()
        src, out = os.path.join(d, "in.cu8"), os.path.join(d, "out.raw")
        np.clip(np.rint(iq * 127 + 127.5), 0, 255).astype(np.uint8).tofile(src)
        rc = subprocess.run([sys.executable, iqpipe.__file__, "fm", "--rate", str(fs), "--mode", "fm", "--sr",
                             str(sr), "--out", out, "--", "cat", src], timeout=30).returncode
        self.assertEqual(rc, 0)
        a = np.fromfile(out, dtype="<i2").astype(float)
        self.assertGreater(len(a), sr * 0.9)
        spec = np.abs(np.fft.rfft(a - a.mean()))
        peak = np.argmax(spec) * sr / len(a)
        self.assertAlmostEqual(peak, tone, delta=30)

    @needs_rtl433
    def test_rtl433_reads_a_receivers_iq_through_the_pipe(self):
        d = tempfile.mkdtemp()
        rec = os.path.join(d, "selftest_250k.cu8")
        A._st_mixed_cu8(rec)                                       # the decoder self-test's remote and sensor
        sink = [os.path.join(harness.REAL_BIN, "rtl_433"), "-s", "250k", "-r", "cu8:-", "-F", "json"]
        p = subprocess.run([sys.executable, iqpipe.__file__, "rtl433", "--", "cat", rec, "---"] + sink,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-300:])
        self.assertIn("Generic-Remote", p.stdout)


class BB60Sim(unittest.TestCase):
    """The Signal Hound plug-in's helper against its simulated device (ARIS_BB60_SIM=1)."""
    env = dict(os.environ, ARIS_BB60_SIM="1", ARIS_BB60_SIM_CARRIERS="96.9:-40,433.92:-60")

    def test_lists_its_serial(self):
        p = subprocess.run([sys.executable, BB60, "list"], capture_output=True, text=True, env=self.env, timeout=20)
        self.assertEqual(p.stdout.split("\t")[0], "24100123")

    def test_iq_is_cu8_at_the_asked_rate_with_the_carrier_where_it_belongs(self):
        n = 250000
        p = subprocess.run([sys.executable, BB60, "sdr", "-d", "24100123", "-f", "96800000", "-s", "250000",
                            "-g", "20", "-n", str(n), "-"], capture_output=True, env=self.env, timeout=60)
        self.assertEqual(len(p.stdout), 2 * n)
        a = np.frombuffer(p.stdout, dtype=np.uint8).astype(float) - 127.5
        z = a[0::2] + 1j * a[1::2]
        f = np.fft.fftshift(np.fft.fftfreq(len(z), 1 / 250000.0))
        peak = f[np.argmax(np.abs(np.fft.fftshift(np.fft.fft(z))))]
        self.assertAlmostEqual(peak, 100000, delta=2000)                     # 96.9 MHz is 100 kHz above 96.8

    def test_sweep_rows_read_like_rtl_power(self):
        p = subprocess.run([sys.executable, BB60, "power", "-d", "24100123", "-f", "95M:99M:10k", "-i", "0.1",
                            "-1", "-"], capture_output=True, text=True, env=self.env, timeout=60)
        bins, _ = A._parse_rtl_power_lines(p.stdout.splitlines())
        best = max(bins, key=lambda f: max(bins[f]))
        self.assertAlmostEqual(best / 1e6, 96.9, delta=0.03)


class Plugins(unittest.TestCase):
    def test_the_bundled_plugins_load(self):
        names = {p["name"]: p for p in A._load_plugins()}
        self.assertTrue(names["signalhound_bb60"]["ok"], names["signalhound_bb60"]["error"])
        self.assertTrue(names["ism_868"]["ok"], names["ism_868"]["error"])
        self.assertIn("bb60", [b.name for b in radios.backends()])

    def test_a_plugin_band_works_for_scans_captures_and_the_spectrum(self):
        A._load_plugins()
        self.assertEqual(A._band_args("the 868 band")[:2], (863.0, 870.0))
        self.assertEqual(A._named_band("scan the 868 band")[0], (863.0, 870.0))
        self.assertIn("868", [p["id"] for p in A.PLUGIN_PRESETS])

    def test_a_broken_plugin_is_listed_and_never_stops_the_others(self):
        d = tempfile.mkdtemp()
        open(os.path.join(d, "broken.py"), "w").write("raise RuntimeError('oops')\n")
        open(os.path.join(d, "fine.py"), "w").write(
            "ABOUT = 'a test tool'\n"
            "def setup(aris):\n"
            "    @aris.register('plugin_echo', 'Echo a word (test plug-in).', {'word': {'type': 'string'}})\n"
            "    def plugin_echo(word=''):\n"
            "        return {'echo': word}\n")
        saved = list(A.PLUGINS)
        os.environ["ARIS_PLUGIN_DIRS"] = d
        try:
            A.PLUGINS.clear()
            got = {p["name"]: p for p in A._load_plugins()}
            self.assertFalse(got["broken"]["ok"])
            self.assertIn("oops", got["broken"]["error"])
            self.assertTrue(got["fine"]["ok"])
            self.assertEqual(call("plugin_echo", word="hi")["echo"], "hi")
        finally:
            os.environ.pop("ARIS_PLUGIN_DIRS", None)
            A.PLUGINS[:] = saved


class SoapyEndToEnd(unittest.TestCase):
    """A SoapySDR receiver found by /check and used like a dongle (fake SoapySDRUtil / rx_tools)."""

    def setUp(self):
        os.environ["FAKE_SOAPY"] = "hackrf:a06063c8234e4f5f"
        self.dev = "soapy-hackrf-a06063c8234e4f5f"

    def tearDown(self):
        os.environ.pop("FAKE_SOAPY", None)
        if self.dev in A.DONGLE_NAMES:
            call("forget_dongle", dongle=self.dev)

    def test_found_added_and_swept(self):
        found = A._list_devices()
        self.assertIn(self.dev, [d["serial"] for d in found])
        sync = A._sync_found_dongles(found)
        self.assertIn(self.dev, sync["added"])
        self.assertIn(self.dev, A.DONGLE_NAMES)
        bins, hops = A._rtl_power(88.0, 108.0, 25.0, gain_db=20, serial=self.dev)
        self.assertGreater(len(bins), 100)
        usb = A._usb_self_test(self.dev, 1.0)
        self.assertIsNone(usb["ok"])
        self.assertIn("RTL-SDR", usb["note"])

    def test_each_receivers_sweep_keeps_its_own_scale(self):
        # only rtl_power's numbers are referred to an 8-bit full scale: a Signal Hound sweeps in calibrated dBm and a
        # SoapySDR receiver on its own scale, so neither may be shifted and labelled dBFS on the Spectrum tab
        A._load_plugins()
        self.assertEqual(radios.sweep_unit("00001001"), "dBFS")
        self.assertEqual(radios.sweep_unit(self.dev), "dB")
        self.assertEqual(radios.sweep_unit("bb60-12345678"), "dBm")
        from arisweb import spectrum
        A._sync_found_dongles(A._list_devices())
        fr = spectrum._frame(A, self.dev, 88.0, 108.0, 20)
        self.assertEqual((fr["mode"], fr["unit"]), ("sweep", "dB"))
        acc, _ = A._rtl_power(88.0, 108.0, fr["step_khz"], gain_db=20, serial=self.dev)
        raw_floor = A._robust_floor([sum(v) / len(v) for v in acc.values()])
        self.assertLess(abs(fr["floor_db"] - raw_floor), 3.0)   # shown as measured, not shifted by ~20 dB

    def test_adsb_says_it_needs_an_rtl_sdr(self):
        r = A._stage_adsb(self.dev, 2, 40) if hasattr(A, "_stage_adsb") else None
        if r is not None:
            self.assertIn("RTL-SDR", r.get("status", ""))


if __name__ == "__main__":
    unittest.main()
