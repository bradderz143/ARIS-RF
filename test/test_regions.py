"""Band plans per region (arislib.regions) and how the engine uses them: the region files, TV channel arithmetic,
labels per region, the FM channel plan, the time-zone guess, the /region console command and the engine's wiring.
python3 -m unittest test_regions   (a few seconds; no radio is opened)"""
import itertools
import json
import os
import tempfile
import unittest
from unittest import mock

import harness  # noqa: F401  (fake radios, a data folder with region "americas" and no country)

from arislib import regions as R

TAB = ("US\t+404251-0740023\tAmerica/New_York\n"
       "CA\t+4339-07923\tAmerica/Toronto\n"
       "BR\t-2332-04637\tAmerica/Sao_Paulo\n"
       "AR\t-3436-05827\tAmerica/Argentina/Buenos_Aires\n"
       "DE\t+5230+01322\tEurope/Berlin\n"
       "JP\t+353916+1394441\tAsia/Tokyo\n"
       "AU\t-3352+15113\tAustralia/Sydney\n"
       "NZ\t-3652+17446\tPacific/Auckland\n"
       "AE\t+2518+05518\tAsia/Dubai\n"
       "IN\t+2232+08822\tAsia/Kolkata\n"
       "ZA\t-2615+02800\tAfrica/Johannesburg\n"
       "US\t+211825-1575130\tPacific/Honolulu\n"
       "EC\t-0054-08936\tPacific/Galapagos\n"
       "KZ\t+4315+07657\tAsia/Almaty\n"
       "MN\t+4755+10653\tAsia/Ulaanbaatar\n"
       "RE\t-2052+05528\tIndian/Reunion\n"
       "IR\t+3540+05126\tAsia/Tehran\n"
       "GU\t+1328+14445\tPacific/Guam\n")


def _tab():
    d = tempfile.mkdtemp(prefix="aris_zones_")
    p = os.path.join(d, "zone.tab")
    with open(p, "w") as f:
        f.write("# test zone table\n" + TAB)
    return (p,)


class RegionFiles(unittest.TestCase):
    """Every shipped region file: its schema, ordered ranges, labels, no overlaps that would hide an entry."""

    def test_every_region_loads_and_has_its_parts(self):
        self.assertEqual(set(R.REGION_IDS), {os.path.basename(p)[:-5] for p in os.listdir(R.DATA_DIR)
                                             if p.endswith(".json")})
        for rid in R.REGION_IDS:
            reg = R.load(rid)
            self.assertEqual(reg.id, rid)
            self.assertTrue(reg.name and reg.short, rid)
            p = reg.fm_plan()
            self.assertIn(p["plan"], ("odd_tenths", "any_raster"), rid)
            self.assertLess(p["lo"], p["hi"])
            self.assertTrue(p["lo"] <= p["first"] <= p["last"] <= p["hi"], rid)
            self.assertIn(p["raster_khz"], (50, 100, 200), rid)
            self.assertTrue(p.get("label"), rid)
            self.assertTrue(reg.setup.get("low") and reg.setup.get("fm"), rid)
            for b in reg.setup_bands():
                lo, hi = R.band_key(b)
                self.assertLess(lo, hi, b)
            self.assertEqual(R.band_key(reg.setup["fm"])[1], p["hi"], rid)
            if reg.setup.get("subghz"):
                lo, hi = R.band_key(reg.setup["subghz"])
                self.assertTrue(reg.gain_band and reg.gain_band[0] <= lo and hi <= reg.gain_band[1], rid)
            self.assertIn(reg.ism_default, reg.ism_bands, rid)
            for k, (lo, hi, lab) in reg.ism_bands.items():
                self.assertTrue(lab and 0 < hi - lo <= 60.0, (rid, k))
            for x in reg.sensor_bands + reg.busy_bands:
                self.assertLess(x[0], x[1], (rid, x))
            for lo, hi, hint in reg.hop_bands:
                self.assertTrue(lo < hi and hint, (rid, lo))
            for lo, hi, mod, words in reg.device_hints:
                self.assertTrue(lo < hi and mod in ("OOK", "FSK", "ANY") and words, (rid, lo))
            ids = [q["id"] for q in reg.presets()]
            self.assertEqual(len(ids), len(set(ids)), rid)
            self.assertEqual(len(reg.presets()), len(reg.data["presets"]), rid)    # every generic id exists
            for q in reg.presets():
                self.assertTrue(q["name"] and q["start"] < q["stop"], (rid, q))
            self.assertTrue(reg.data.get("prompt"), rid)

    def test_ranges_are_ordered_labelled_and_never_partly_overlap(self):
        """Within one list an entry either holds another whole or stays clear of it (touching at an edge is
        fine), so 'the narrowest entry wins' always means 'the most specific one'."""
        for rid in R.REGION_IDS:
            reg = R.load(rid)
            lists = [("allocations", reg.allocations)] + [(c, [tuple(x) for x in v.get("allocations") or []])
                                                          for c, v in reg.countries.items()]
            for name, lst in lists:
                for lo, hi, lab in lst:
                    self.assertLess(lo, hi, (rid, name, lab))
                    self.assertTrue(str(lab).strip(), (rid, name, lo))
                for a, b in itertools.combinations(lst, 2):
                    self.assertFalse(a[0] == b[0] and a[1] == b[1], (rid, name, a, b))
                    (l1, h1, _), (l2, h2, _) = sorted([a, b])
                    partial = l2 < h1 and h2 > h1 and l1 != l2
                    self.assertFalse(partial, (rid, name, a, b))

    def test_tv_tables_hold_whole_channels_and_dab_blocks_are_ordered(self):
        for rid in R.REGION_IDS:
            reg = R.load(rid)
            for t in reg.tv:
                n = (t["hi"] - t["lo"]) / t["width_mhz"]
                self.assertAlmostEqual(n, round(n), 6, (rid, t["lo"]))
                if t.get("channel_names"):
                    self.assertEqual(len(t["channel_names"]), round(n), rid)
                self.assertTrue(t["standard"], rid)
            for x in reg.dab:
                self.assertTrue(x["lo"] < x["hi"] and x["label"], (rid, x))
                self.assertAlmostEqual(x["hi"] - x["lo"], 1.536, 3)

    def test_no_label_names_a_city_or_a_station(self):
        """A label is a band plan for a region or a country, never a place or a broadcaster."""
        import re
        for rid in R.REGION_IDS:
            txt = json.dumps(R.load(rid).data).lower()
            self.assertIsNone(re.search(r"\bcity\b|\bciudad\b|\bstation [a-z]{3,4}-fm\b", txt), rid)


class TvChannels(unittest.TestCase):
    def _table(self, rid, f, country=None):
        reg = R.load(rid)
        return next(t for t in reg.tv if t["lo"] <= f <= t["hi"] and (not country or not t.get("countries")
                                                                       or country in t["countries"]))

    def test_channel_arithmetic_per_region(self):
        self.assertEqual(R.channel_edges(self._table("americas", 470.5, "US"), 14), (470.0, 476.0))
        self.assertEqual(R.channel_edges(self._table("americas", 600.0, "US"), 36), (602.0, 608.0))
        self.assertEqual(R.channel_edges(self._table("americas", 60.0, "US"), 3), (60.0, 66.0))
        self.assertEqual(R.channel_edges(self._table("europe", 470.5), 21), (470.0, 478.0))
        self.assertEqual(R.channel_edges(self._table("europe", 690.0), 48), (686.0, 694.0))
        self.assertEqual(R.channel_edges(self._table("japan", 470.5), 13), (470.0, 476.0))
        self.assertEqual(R.channel_edges(self._table("japan", 700.0), 52), (704.0, 710.0))
        self.assertEqual(R.channel_edges(self._table("australia", 530.0), 28), (526.0, 533.0))
        self.assertEqual(R.channel_edges(self._table("australia", 205.0), "9A"), (202.0, 209.0))

    def test_a_skipped_channel_is_never_tv(self):
        br = self._table("americas", 650.0, "BR")
        self.assertIsNone(R.channel_of(br, 610.0))                       # channel 37: radio astronomy
        self.assertEqual(R.channel_of(br, 616.0)["channel"], "38")

    def test_labels_name_the_channel_and_the_standard(self):
        am, eu, jp = R.load("americas"), R.load("europe"), R.load("japan")
        self.assertEqual(am.label(540.0, "US"), "Digital TV channel 25 (ATSC 1.0 / 3.0)")
        self.assertEqual(am.label(540.0, "BR"), "Digital TV channel 25 (ISDB-Tb)")
        self.assertIn("Digital TV channel 25", am.label(540.0))          # no country: both standards, by country
        self.assertIn("by country", am.label(540.0))
        self.assertEqual(eu.label(540.0), "Digital TV channel 29 (DVB-T / DVB-T2)")
        self.assertEqual(jp.label(540.0), "Digital TV channel 24 (ISDB-T)")
        self.assertIn("block 8C", eu.label(199.36))                       # DAB before the TV channel it sits in


class Labels(unittest.TestCase):
    def test_868_band_is_european_only(self):
        am, eu = R.load("americas"), R.load("europe")
        for f in (864.3, 866.6):
            self.assertNotIn("868", am.label(f))
            self.assertNotIn("ISM", am.label(f))
            self.assertIn("two-way radio", am.label(f))
        self.assertIn("SRD 868", eu.label(864.3))
        self.assertIn("RFID", eu.label(866.6))                            # the narrower 865-868 entry

    def test_navigation_band_is_navigation_everywhere(self):
        for rid in R.REGION_IDS:
            lab = R.load(rid).label(114.55)
            self.assertIn("navigation", lab, rid)
            self.assertNotIn("voice", lab.lower(), rid)
            self.assertIn("voice", R.load(rid).label(125.3).lower(), rid)
        self.assertTrue(R.is_nav(108.0) and R.is_nav(117.9) and not R.is_nav(117.975) and not R.is_nav(121.5))

    def test_narrowest_entry_wins_across_lists(self):
        am = R.load("americas")
        self.assertIn("channel 16", am.label(156.8))                      # a shared spot inside the region's VHF band
        self.assertIn("two-way radio", am.label(155.0))
        self.assertEqual(am.label(1880.0, "US"), "USA: PCS 1900 mobile phones transmit")
        self.assertIn("layout differs by country", am.label(1880.0))       # no country: the neutral label
        self.assertIn("Brazil", am.label(1850.0, "BR"))

    def test_without_an_entry_the_label_says_so(self):
        self.assertIsNone(R.load("americas").label(30.0))
        self.assertIsNone(R.load("world").label(864.3))
        self.assertEqual(R.load("world").fallback, "Depends on the country: set your region")
        self.assertEqual(R.load("americas").fallback, R.FALLBACK)

    def test_country_layouts_only_with_the_country(self):
        am = R.load("americas")
        self.assertIn("NOAA", am.label(162.45, "US"))
        self.assertNotIn("NOAA", am.label(162.45))
        self.assertIn("former Weatheradio", am.label(162.45, "CA"))


class FmPlan(unittest.TestCase):
    def test_odd_tenths_only_in_the_americas(self):
        am = R.load("americas").fm_plan()
        self.assertFalse(R.on_fm_plan(am, 94.8, 50.0))
        self.assertTrue(R.on_fm_plan(am, 94.9, 50.0))
        self.assertFalse(R.on_fm_plan(am, 87.9, 50.0))                    # below the first channel
        for rid in ("europe", "asia_pacific", "japan", "australia", "world"):
            self.assertTrue(R.on_fm_plan(R.load(rid).fm_plan(), 94.8, 50.0), rid)
        self.assertTrue(R.on_fm_plan(R.load("europe").fm_plan(), 104.85, 10.0))   # 50 kHz steps there

    def test_country_plans(self):
        au = R.load("australia")
        self.assertFalse(R.on_fm_plan(au.fm_plan("AU"), 94.8, 30.0))
        self.assertTrue(R.on_fm_plan(au.fm_plan("AU"), 87.6, 10.0))       # low-power narrowcast channel
        self.assertTrue(R.on_fm_plan(au.fm_plan("NZ"), 94.8, 10.0))
        self.assertTrue(R.on_fm_plan(R.load("americas").fm_plan("BR"), 80.1, 10.0))   # Brazil's extended band
        jp = R.load("japan").fm_plan()
        self.assertEqual((jp["lo"], jp["hi"]), (76.0, 99.0))
        self.assertTrue(R.on_fm_plan(jp, 80.0, 10.0))

    def test_clearly_off_and_channels(self):
        am = R.load("americas").fm_plan()
        self.assertTrue(R.clearly_off(am, 94.8, 0.0, 25.0))
        self.assertFalse(R.clearly_off(am, 94.9, 3.0, 25.0))
        self.assertFalse(R.clearly_off(R.load("europe").fm_plan(), 94.8, 0.0, 25.0))
        ch = R.fm_channels(am)
        self.assertEqual((ch[0], ch[-1], len(ch)), (88.1, 107.9, 100))
        self.assertEqual(R.nearest_channel(am, 94.93)[0], 94.9)


class ZoneGuess(unittest.TestCase):
    def test_zones_give_regions_and_countries(self):
        tab = _tab()
        g = lambda z: R.guess(z, tab)      # noqa: E731
        self.assertEqual(g("America/New_York"), ("americas", "US"))
        self.assertEqual(g("America/Toronto"), ("americas", "CA"))
        self.assertEqual(g("America/Sao_Paulo"), ("americas", "BR"))
        self.assertEqual(g("America/Argentina/Buenos_Aires"), ("americas", None))   # no layout of its own
        self.assertEqual(g("Europe/Berlin"), ("europe", None))
        self.assertEqual(g("Africa/Johannesburg"), ("europe", None))
        self.assertEqual(g("Atlantic/Canary"), ("europe", None))
        self.assertEqual(g("Asia/Tokyo"), ("japan", None))
        self.assertEqual(g("Australia/Sydney"), ("australia", "AU"))
        self.assertEqual(g("Pacific/Auckland"), ("australia", "NZ"))
        self.assertEqual(g("Asia/Dubai"), ("europe", None))                 # ITU Region 1
        self.assertEqual(g("Asia/Kolkata"), ("asia_pacific", "IN"))
        self.assertEqual(g("UTC"), ("world", None))
        self.assertEqual(g("Antarctica/Troll"), ("world", None))

    def test_the_zone_tables_country_corrects_the_prefix(self):
        """Pacific/ and Indian/ zones and Central Asia lie in other ITU regions than their prefix suggests."""
        tab = _tab()
        g = lambda z: R.guess(z, tab)      # noqa: E731
        self.assertEqual(g("Pacific/Honolulu"), ("americas", "US"))        # Region 2 (east of line C)
        self.assertEqual(g("Pacific/Galapagos"), ("americas", None))
        self.assertEqual(g("Asia/Almaty"), ("europe", None))               # RR 5.3: Region 1
        self.assertEqual(g("Asia/Ulaanbaatar"), ("europe", None))
        self.assertEqual(g("Indian/Reunion"), ("europe", None))            # west of 60 E
        self.assertEqual(g("Asia/Tehran"), ("asia_pacific", None))         # unchanged
        self.assertEqual(g("Pacific/Guam"), ("asia_pacific", None))
        self.assertEqual(g("Asia/Tel_Aviv"), ("europe", None))             # old names the table lacks
        self.assertEqual(R.guess("Pacific/Easter", ()), ("americas", None))   # no zone table at all

    def test_a_bad_zone_never_raises(self):
        d = tempfile.mkdtemp(prefix="aris_tz_")
        bad = os.path.join(d, "timezone")
        with open(bad, "wb") as f:
            f.write(b"\xff\xfe\x00Eur\xc3ope/Berlin\n")
        self.assertIsNone(R.zone_name({}, "/nonexistent", bad))             # garbled bytes: skipped
        os.makedirs(os.path.join(d, "zoneinfo", "posix", "Europe"))
        open(os.path.join(d, "zoneinfo", "posix", "Europe", "Berlin"), "w").close()
        os.symlink(os.path.join(d, "zoneinfo", "posix", "Europe", "Berlin"), os.path.join(d, "localtime"))
        self.assertEqual(R.zone_name({}, os.path.join(d, "localtime"), bad), "Europe/Berlin")
        self.assertEqual(R.zone_name({"TZ": "Japan"}, "/nonexistent", "/nonexistent"), "Japan")
        self.assertEqual(R.zone_name({"TZ": "right/Asia/Tokyo"}, "/nonexistent", "/nonexistent"), "Asia/Tokyo")
        with open(os.path.join(d, "tz"), "w") as f:
            f.write("America/New_York\n")
        self.assertEqual(R.zone_name({"TZ": "EST5EDT,M3.2.0,M11.1.0"}, "/nonexistent", os.path.join(d, "tz")),
                         "America/New_York")                                # a POSIX rule: the files decide
        badtab = os.path.join(d, "zone.tab")
        with open(badtab, "wb") as f:
            f.write(b"\xff\xfeDE\t+5230+01322\tEurope/Berlin\n")
        self.assertIsNone(R.zone_country("Asia/Tokyo", (badtab,)))
        with mock.patch.object(R, "zone_name", side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")):
            st = R.resolve(None, None, False)
        self.assertEqual((st["id"], st["source"]), ("world", "default"))

    def test_zone_name_sources(self):
        self.assertEqual(R.zone_name({"TZ": ":Europe/Berlin"}, "/nonexistent", "/nonexistent"), "Europe/Berlin")
        d = tempfile.mkdtemp(prefix="aris_tz_")
        with open(os.path.join(d, "timezone"), "w") as f:
            f.write("Asia/Tokyo\n")
        self.assertEqual(R.zone_name({}, "/nonexistent", os.path.join(d, "timezone")), "Asia/Tokyo")
        os.makedirs(os.path.join(d, "zoneinfo", "Australia"))
        open(os.path.join(d, "zoneinfo", "Australia", "Perth"), "w").close()
        os.symlink(os.path.join(d, "zoneinfo", "Australia", "Perth"), os.path.join(d, "localtime"))
        self.assertEqual(R.zone_name({}, os.path.join(d, "localtime"), "/nonexistent"), "Australia/Perth")

    def test_resolve_prefers_the_config_and_never_raises(self):
        tab = _tab()
        st = R.resolve(None, None, False, zone="Europe/Berlin", tab_files=tab)
        self.assertEqual((st["id"], st["source"], st["guessed"]), ("europe", "time zone", True))
        self.assertEqual(R.describe(st), "Europe (guessed from the time zone; change it under Spectrum)")
        st = R.resolve("japan", None, False, zone="Europe/Berlin", tab_files=tab)
        self.assertEqual((st["id"], st["source"], st["guessed"]), ("japan", "config", False))
        st = R.resolve("narnia", None, False, zone="America/New_York", tab_files=tab)
        self.assertEqual((st["id"], st["country"], st["country_guessed"]), ("americas", "US", True))
        self.assertIn("unknown region", st["problem"])
        st = R.resolve("americas", None, True, zone="America/New_York", tab_files=tab)
        self.assertIsNone(st["country"])                                    # "no country" set on purpose
        st = R.resolve("americas", "AU", True, zone="", tab_files=tab)
        self.assertIsNone(st["country"])
        self.assertIn("does not belong", st["problem"])
        st = R.resolve(None, None, False, zone="", tab_files=tab)
        self.assertEqual((st["id"], st["source"]), ("world", "default"))
        with self.assertRaises(R.RegionError):
            R.load("narnia")


class BrokenFile(unittest.TestCase):
    """A region file that cannot be read costs only its own region; the region in force fails loudly, naming it."""

    def setUp(self):
        import shutil
        self.d = tempfile.mkdtemp(prefix="aris_regions_")
        for name in os.listdir(R.DATA_DIR):
            shutil.copy(os.path.join(R.DATA_DIR, name), self.d)
        with open(os.path.join(self.d, "japan.json"), "r+") as f:
            f.truncate(2000)                                                # cut off half way
        self.patches = [mock.patch.object(R, "DATA_DIR", R.Path(self.d)), mock.patch.object(R, "_CACHE", {})]
        for x in self.patches:
            x.start()

    def tearDown(self):
        for x in self.patches:
            x.stop()

    def test_only_that_region_is_left_out(self):
        bands = R.all_sensor_bands()
        self.assertIn((433.05, 434.79), bands)
        self.assertNotIn("japan", [c["id"] for c in R.choices()])
        self.assertIn("americas", [c["id"] for c in R.choices()])
        self.assertTrue(R.countries())
        st = R.resolve("americas", None, False, zone="Asia/Tokyo", tab_files=_tab())
        self.assertEqual((st["id"], st["source"]), ("americas", "config"))

    def test_the_region_in_force_fails_naming_the_file(self):
        with self.assertRaises(R.RegionError) as e:
            R.load("japan")
        self.assertIn("japan.json", str(e.exception))
        with self.assertRaises(R.RegionError):                              # never quietly another region's labels
            R.resolve(None, None, False, zone="Asia/Tokyo", tab_files=_tab())


class CountryLabels(unittest.TestCase):
    """With a country known, a label written for other countries of the region is left out."""

    def test_labels_for_other_countries_are_left_out(self):
        am, au = R.load("americas"), R.load("australia")
        self.assertIn("USA", am.label(420.5))                               # no country: the region-wide labels
        self.assertIn("USA", am.label(420.5, "US"))
        for c in ("MX", "BR"):
            self.assertNotIn("USA", am.label(420.5, c) or "", c)
            self.assertNotIn("UAT", am.label(978.0, c) or "", c)
            self.assertNotIn("ACARS aircraft data (", am.label(131.55, c) or "", c)
        self.assertNotIn("weather", (am.label(162.475, "BR") or "").lower())
        self.assertIn("early-warning", am.label(162.475, "MX"))
        self.assertIn("block 9A", au.label(202.928, "AU"))
        self.assertIsNone(au.label(202.928, "NZ"))                          # DAB only where it is planned
        self.assertIsNone(au.label(27.185, "NZ"))
        self.assertIn("Australia", au.label(27.185, "AU"))
        self.assertNotIn("Australia", au.label(27.185).split("(")[0])      # no country prefix on the label

    def test_country_codes_of_restricted_entries_belong_to_the_region(self):
        for rid in R.REGION_IDS:
            reg = R.load(rid)
            for entry, codes in reg.only.items():
                if entry in reg.allocations:
                    self.assertTrue(codes and codes <= set(reg.countries), (rid, entry))
            for x in reg.dab:
                self.assertTrue(set(x.get("countries") or []) <= set(reg.countries), (rid, x))

    def test_short_range_and_amateur_wording(self):
        eu = R.load("europe")
        lab = eu.label(867.3)
        self.assertIn("RFID", lab)
        self.assertIn("LoRaWAN", lab)                                       # 867.1-867.9 MHz channels
        self.assertEqual(R.load("world").label(145.0), "Amateur radio 2 m band")
        self.assertEqual(R.load("americas").label(145.0), "Amateur radio 2 m band")
        ap = R.load("asia_pacific")                                         # India's 2022 exemption: label and score agree
        self.assertIn((433.05, 434.79), ap.sensor_bands_for("IN"))
        self.assertIn((865.0, 868.0), ap.sensor_bands_for("IN"))
        jp = R.load("japan").label(433.92)                                  # hedged World entry, kept on purpose
        self.assertIn("in most countries", jp)
        self.assertIn("amateur 70 cm", jp)


class EngineWiring(unittest.TestCase):
    """The engine reads the region in force for every label, the FM plan, sensor bands, band names and presets; a
    change applies at once and the harness's Americas come back afterwards."""

    @classmethod
    def setUpClass(cls):
        import aris
        cls.A = aris

    def tearDown(self):
        self.A._set_region("americas", None)

    def test_the_americas_by_default_in_the_tests(self):
        A = self.A
        self.assertEqual(A._region_state()["id"], "americas")
        self.assertEqual(A._region_line(), "Region: Americas")
        self.assertEqual(A._setup_bands(), ["433-435", "902-928", "88-108"])
        self.assertEqual(A._fm_band(), (87.5, 108.0))
        self.assertIn("navigation", A._classify_frequency_allocation(114.55, 25))
        self.assertNotIn("868", A._classify_frequency_allocation(864.3, 25))
        self.assertEqual(A._classify_frequency_allocation(30.0, 25.0),
                         "Not in this region's band table: a narrowband carrier")
        self.assertEqual(A._classify_frequency_allocation(30.0, 200.0),
                         "Not in this region's band table: a wideband signal")
        self.assertTrue(A._in_sensor_band(915.0) and not A._in_sensor_band(864.3))
        self.assertFalse(A._on_fm_plan(94.8, 50.0))
        self.assertIn("ODD tenths".lower(), A._system_prompt().lower())
        self.assertEqual(A._band_args("ism")[:2], (433.05, 434.79))
        self.assertTrue(A._in_nav_band(114.55))

    def test_europe_changes_labels_plan_bands_and_presets_at_once(self):
        A = self.A
        st = A._set_region("europe")
        self.assertEqual(st["id"], "europe")
        self.assertIn("SRD 868", A._classify_frequency_allocation(864.3, 25))
        self.assertTrue(A._in_sensor_band(864.3) and not A._in_sensor_band(915.0))
        self.assertTrue(A._on_fm_plan(94.8, 50.0))
        self.assertEqual(A._setup_bands(), ["433-435", "863-870", "87.5-108"])
        self.assertEqual(A._band_short("863-870"), "868 MHz")
        self.assertEqual(A._band_args("868")[:2], (863.0, 870.0))
        self.assertEqual(A._named_band("scan the FM band")[0], (87.5, 108.0))
        self.assertEqual(A._named_band("the 2 m band")[0], (144.0, 146.0))
        self.assertIn("868", [p["id"] for p in A._region().presets()])
        self.assertIn("LoRaWAN", A._hopping_hint(863.1, 869.9))
        self.assertIn("Europe", A._system_prompt())
        self.assertNotIn("odd tenths", A._system_prompt())
        cfg = A._load_config()
        cfg["dongles"] = {"00017350": {"gain_915_db": 12.5, "gain_db": 33.8, "fm_gain_db": 8.7}}
        self.assertEqual(A._gain_for("00017350", 863.0, 870.0, cfg), 12.5)   # the region's sub-GHz gain
        self.assertEqual(A._gain_for("00017350", 902.0, 928.0, cfg), 33.8)
        self.assertEqual(A._gain_for("00017350", 87.6, 87.9, cfg), 8.7)

    def test_japan_fm_band_and_its_ism_band(self):
        A = self.A
        A._set_region("japan")
        self.assertEqual(A._fm_band(), (76.0, 99.0))
        self.assertTrue(A._in_fm_band(80.0) and not A._in_fm_band(101.0))
        self.assertEqual(A._band_kind(80.0), "wide")
        self.assertEqual(A._band_args("ism")[:2], (920.5, 928.1))
        self.assertEqual(A._band_of_freq(922.4), "920")

    def test_a_country_layout_and_an_unknown_region(self):
        A = self.A
        A._set_region("americas", "us")
        self.assertEqual(A._region_state()["country"], "US")
        self.assertIn("PCS 1900", A._classify_frequency_allocation(1880.0, 200))
        with self.assertRaises(R.RegionError):
            A._set_region("americas", "DE")
        with self.assertRaises(R.RegionError):
            A._set_region("narnia")

    def test_decodes_are_doubtful_only_outside_every_regions_sensor_bands(self):
        A = self.A
        for f in (169.43, 426.05, 922.4, 864.3):        # a European, a Japanese and an Asian band, all still sensors
            self.assertTrue(any(lo <= f <= hi for lo, hi in A.SENSOR_WINDOWS_MHZ), f)
        self.assertEqual(A._decode_doubts({"freq_mhz": 169.43, "packets": 3}), [])
        self.assertTrue(A._decode_doubts({"freq_mhz": 31.4, "packets": 3}))

    def test_region_console_command(self):
        A = self.A
        out = A._region_reply("")
        self.assertIn("Region: Americas", out)
        self.assertIn("europe", out)
        self.assertEqual(A._region_reply("europe", admin=False), "Only the admin account can change the region.")
        self.assertEqual(A._region_state()["id"], "americas")
        self.assertIn("Europe", A._region_reply("europe"))
        self.assertEqual(A._region_state()["id"], "europe")
        self.assertIn("There is no region", A._region_reply("narnia"))
        import re
        self.assertIsNone(re.search(r"\([^()]*\(", out), out)               # no bracket inside a bracket
        self.assertIn("asia_pacific (Asia-Pacific)", out)
        self.assertTrue(A._ADMIN_COMMAND_RX.match("/region europe"))
        self.assertFalse(A._ADMIN_COMMAND_RX.match("/region"))
        from arisweb import bridge
        for cmd in ("/region", "/region europe", "/region americas us", "/region asia_pacific"):
            self.assertTrue(bridge.SLASH_COMMANDS.match(cmd), cmd)
        self.assertFalse(bridge.SLASH_COMMANDS.match("/region europe; rm"))

    def test_capture_band_names(self):
        A = self.A
        from arislib import bandwatch
        for k in ("433", "915", "868", "315"):
            self.assertIn(k, bandwatch.BANDS)
        self.assertEqual(A._band_args("915")[:2], (902.0, 928.0))
        A._set_region("australia")
        self.assertEqual(A._band_args("915")[:2], (915.0, 928.0))           # the region's own edges for a name
        self.assertEqual(A._band_args("868")[:2], (863.0, 870.0))           # still understood as a word
        self.assertNotIn("short-range", A._band_args("868")[2])             # no other region's wording
        A._set_region("americas", None)
        self.assertIsNone(A._band_of_freq(865.0))                           # trunked radio here: an ordinary capture
        self.assertEqual(A._band_of_freq(915.0), "915")
        A._set_region("europe", None)
        self.assertEqual(A._band_of_freq(865.0), "868")
        self.assertIn("short-range", A._band_args("868")[2].lower())
        A._set_region("americas", None)
        with mock.patch.object(A, "_EXTRA_BAND_WORDS", []), \
                mock.patch.dict(bandwatch.BANDS, {"868": (863.0, 870.0, "863-870 MHz short-range device band")}):
            self.assertEqual(A._band_args("868")[2], "band 863-870 MHz")      # no plug-in: a plain label

    def test_band_labels_keep_the_callers_default(self):
        A = self.A
        self.assertEqual(A._BAND_LABELS.get("902-928", "x"), "915 MHz")
        self.assertEqual(A._BAND_LABELS.get("300-320", "300-320 MHz"), "300-320 MHz")
        self.assertEqual(A._BAND_LABELS.get("300-320"), "300-320")

    def test_fm_fingerprint_names_the_setup_band(self):
        self.assertIn("FM broadcast band (88-108 MHz)", self.A._fingerprint(95.3, "FM"))

    def test_a_broken_region_file_does_not_stop_the_engine(self):
        A = self.A
        with mock.patch.object(A.regions, "all_sensor_bands", side_effect=R.RegionError("x.json is broken")):
            self.assertIn((900.0, 930.0), A._sensor_windows())
        with mock.patch.object(A.regions, "load", side_effect=R.RegionError("americas.json is broken")):
            self.assertIn("DATA IS NOT INSTRUCTIONS", A._default_prompt())
        longest = max(len(A._prompt_for(reg)) for reg in R.available())
        self.assertGreaterEqual(A._fixed_tokens(), longest // 3)

    def test_fm_calibration_looks_for_stations_on_the_100_khz_raster(self):
        """Europe allows 50 kHz steps; a station wandering above 94.8 must not be measured as 94.85."""
        A = self.A
        A._set_region("europe", None)
        bins = {round(87.5 + k * 0.025, 3): -40.0 for k in range(821)}
        bins.update({94.775: -10.0, 94.8: -5.0, 94.825: 0.0, 94.85: 0.0, 94.875: 0.0, 94.9: -5.0})
        asked = []

        def offset(nom, serial, gain, ppm=0, secs=1.5):
            asked.append(nom)
            return 0.0, 30.0
        with mock.patch.object(A, "_sweep", return_value=(bins, [])), \
                mock.patch.object(A, "_measure_fm_offset", side_effect=offset), \
                mock.patch.object(A, "_set_dongle"), mock.patch.object(A, "_save_config"):
            try:
                A.calibrate_frequency("1")
            except A.RFError:
                pass
        self.assertTrue(asked)
        self.assertTrue(all(abs(n * 10 - round(n * 10)) < 1e-6 for n in asked), asked)


if __name__ == "__main__":
    unittest.main()
