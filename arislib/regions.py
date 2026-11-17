"""Band plans per region: what normally transmits where, which FM channel plan applies, where sensors live.

Every band fact ARIS uses to label a frequency or to judge a signal comes from one region file in
`arislib/data/regions/<id>.json`, so a station anywhere gets labels that are right where it stands. A label is a band
plan, never an identification. Where a table has nothing, the label says so ("Not in this region's band table") and
never borrows a label from another region.

A region file holds (all frequencies in MHz, edges inclusive):
  name, short             what the page and the chat call it
  base                    another region whose lists this one inherits (every region builds on "world", the
                          shared list of what the ITU Radio Regulations harmonise worldwide)
  fm                      {lo, hi, first, last, raster_khz, plan: odd_tenths | any_raster, label, exceptions_mhz}
  sensor_bands            [[lo, hi]] where rtl_433-type devices live (scores bursts, picks decoders)
  busy_bands              [[lo, hi]] Wi-Fi, Bluetooth and phone bands (short bursts are normal there)
  hop_bands               [[lo, hi, hint]] where frequency hoppers are normal (an audit folds them into one finding)
  ism_bands               {name: [lo, hi, label]} the bands a band capture records ("433", "868", "915" ...)
  ism_default             which of them "the ISM band" means here
  setup                   {low, subghz, fm}: the bands /setup calibrates and patrol watches ("lo-hi")
  gain_band               [lo, hi] where the dongle's own sub-GHz gain applies (config key gain_915_db, kept for
                          old configs: it means "the region's sub-GHz band", 902-928 in the Americas, 863-870 in Europe)
  presets                 Spectrum chips: a dict, or the id of one of GENERIC_PRESETS
  device_hints            [[lo, hi, OOK | FSK | ANY, words]] band-plan device guesses for the Signals list
  named                   {"2m": [lo, hi]} band names whose edges differ by region
  allocations             [[lo, hi, label]], or [lo, hi, label, [country codes]] for a label written for some of the
                          region's countries only: with another country known it is left out
  tv                      [{lo, hi, width_mhz, first_channel, standard, skip, countries, channel_names}]
  dab                     [{block, lo, hi, label, countries}] (countries optional, as for the TV tables)
  countries               {code: {name, allocations, fm, sensor_bands}}: used only when the country is known
  fallback                the words for a frequency no table covers

The label of a frequency is the NARROWEST entry that contains it across: the country's list, the DAB blocks and TV
channels, the region's allocations and the base region's (on a tie the more local list wins). A file needs no code:
see docs/REGIONS.md. json and the standard library only.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data" / "regions"
REGION_IDS = ("americas", "europe", "asia_pacific", "japan", "australia", "world")
DEFAULT_REGION = "world"
FALLBACK = "Not in this region's band table"
NAV_BAND = (108.0, 117.975)        # aeronautical navigation (VOR, ILS localizer): the same in every region
AIR_VOICE = (117.975, 137.0)       # aeronautical voice and data

GENERIC_PRESETS = {
    "air": {"id": "air", "name": "Airband", "start": 118.0, "stop": 137.0},
    "adsb": {"id": "adsb", "name": "ADS-B", "start": 1085.0, "stop": 1095.0},
    "gnss": {"id": "gnss", "name": "GPS 1575", "start": 1565.0, "stop": 1585.0},
    "ism24": {"id": "ism24", "name": "2.4 GHz", "start": 2400.0, "stop": 2483.5},
    "wifi5": {"id": "wifi5", "name": "5 GHz Wi-Fi", "start": 5150.0, "stop": 5350.0},
    "ism58": {"id": "ism58", "name": "5.8 GHz", "start": 5725.0, "stop": 5875.0},
}

# The time zone says roughly where a computer is. Zone prefix -> region (a guess, never written to the config).
_ZONE_PREFIX = (("America/", "americas"), ("US/", "americas"), ("Canada/", "americas"), ("Brazil/", "americas"),
                ("Chile/", "americas"), ("Mexico/", "americas"), ("Europe/", "europe"), ("Africa/", "europe"),
                ("Australia/", "australia"), ("Asia/", "asia_pacific"), ("Pacific/", "asia_pacific"),
                ("Indian/", "asia_pacific"))
_ZONE_EXACT = {"Asia/Tokyo": "japan", "Japan": "japan", "Pacific/Auckland": "australia",
               "Pacific/Chatham": "australia", "NZ": "australia", "NZ-CHAT": "australia",
               "Atlantic/Canary": "europe", "Atlantic/Madeira": "europe", "Atlantic/Azores": "europe",
               "Atlantic/Faroe": "europe", "Atlantic/Reykjavik": "europe", "Atlantic/Bermuda": "americas",
               "Atlantic/Stanley": "americas", "Atlantic/Cape_Verde": "europe", "Atlantic/St_Helena": "europe",
               "Iceland": "europe", "Eire": "europe", "GB": "europe", "Portugal": "europe", "Poland": "europe",
               "Turkey": "europe", "Israel": "europe", "Egypt": "europe", "Libya": "europe", "Cuba": "americas",
               "Jamaica": "americas", "Navajo": "americas",
               # Pacific islands east of the ITU's line C are Region 2 (also on systems without a zone table)
               "Pacific/Honolulu": "americas", "Pacific/Johnston": "americas", "Pacific/Galapagos": "americas",
               "Pacific/Easter": "americas",
               # old names the zone table does not list
               "Asia/Tel_Aviv": "europe", "Asia/Istanbul": "europe"}
# Countries whose zones start with Asia/ but follow ITU Region 1 (the Middle East west of Iran, the Caucasus, Russia,
# Central Asia and Mongolia: RR 5.3).
_REGION1_ASIA = {"CY", "IL", "PS", "LB", "SY", "JO", "IQ", "KW", "SA", "BH", "QA", "AE", "OM", "YE", "GE", "AM",
                 "AZ", "RU", "TR", "KZ", "UZ", "KG", "TJ", "TM", "MN"}
# Zones under Pacific/ or Indian/ whose country is in another ITU region than the prefix says: Hawaii, Galapagos and
# Easter Island are Region 2; the African islands of the Indian Ocean (west of 60 E) are Region 1.
_ASIA_PREFIX_REGION2 = {"US", "EC", "CL"}
_ASIA_PREFIX_REGION1 = {"MG", "RE", "MU", "SC", "KM", "YT"}
_ZONE_NAME_RX = re.compile(r"[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+)*")
_ZONE_TAB = ("/usr/share/zoneinfo/zone1970.tab", "/usr/share/zoneinfo/zone.tab")

_CACHE: dict = {}
log = logging.getLogger("aris")


class RegionError(ValueError):
    """An unknown region or country, or a region file that cannot be read, in plain words."""


def _read(rid):
    """A region file's data. RegionError naming the file when it is missing or is no valid JSON (the files ship with
    ARIS: test/test_regions.py checks every one)."""
    path = DATA_DIR / f"{rid}.json"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise RegionError(f"The region file {path.name} cannot be read ({e.strerror or e}).") from e
    except ValueError as e:                       # JSONDecodeError, or bytes that are no UTF-8
        raise RegionError(f"The region file {path.name} is not valid JSON ({e}); "
                          "check it with test/test_regions.py.") from e
    if not isinstance(data, dict):
        raise RegionError(f"The region file {path.name} does not hold a region (a JSON object).")
    return data


class Region:
    """One region's band plan, with its base region's lists folded in (see the module docstring)."""

    def __init__(self, data, base=None):
        self.data = data
        self.base = base
        self.id = data["id"]
        self.name = data["name"]
        self.short = data.get("short") or data["name"]
        get = lambda k, d=None: data[k] if k in data else (getattr(base, "data", {}).get(k, d) if base else d)  # noqa: E731
        self.fm = dict(get("fm"))
        self.sensor_bands = [tuple(x) for x in get("sensor_bands", [])]
        self.busy_bands = [tuple(x) for x in get("busy_bands", [])]
        self.hop_bands = [tuple(x) for x in get("hop_bands", [])]
        self.ism_bands = {k: tuple(v) for k, v in (get("ism_bands", {}) or {}).items()}
        self.ism_default = get("ism_default", "433")
        self.setup = dict(get("setup", {}))
        self.gain_band = tuple(get("gain_band")) if get("gain_band") else None
        self.device_hints = [tuple(x) for x in get("device_hints", [])]
        self.named = {k: tuple(v) for k, v in (get("named", {}) or {}).items()}
        self.fallback = data.get("fallback") or FALLBACK
        # an allocation's optional 4th element: the country codes its label was written for (left out when another
        # country is known; shown when none is)
        self.allocations = [tuple(x[:3]) for x in data.get("allocations", [])]
        own = {tuple(x[:3]): frozenset(str(c).upper() for c in x[3])
               for x in data.get("allocations", []) if len(x) > 3 and x[3]}
        self.only = {**(base.only if base else {}), **own}
        self.shared = list(base.allocations) + list(base.shared) if base else []
        self.tv = [dict(t) for t in data.get("tv", [])]
        self.dab = [dict(x) for x in data.get("dab", [])]
        self.countries = {k: dict(v) for k, v in (data.get("countries") or {}).items()}
        self.notes = list(data.get("notes", []))
        self._presets = list(get("presets", []))

    # --- what applies -----------------------------------------------------------------------------------------
    def country(self, code):
        """The country's own entry (allocations, fm, sensor_bands), or {} (unknown here, or none set)."""
        return self.countries.get(str(code or "").upper(), {}) if code else {}

    def fm_plan(self, country=None):
        """The FM plan: {lo, hi, first, last, raster_khz, plan, exceptions_mhz, label}."""
        p = dict(self.fm)
        p.update(self.country(country).get("fm") or {})
        p.setdefault("exceptions_mhz", [])
        return p

    def sensor_bands_for(self, country=None):
        own = self.country(country).get("sensor_bands")
        return [tuple(x) for x in own] if own else list(self.sensor_bands)

    def presets(self):
        """The Spectrum chips of this region, in order (generic ones by id)."""
        out = []
        for p in self._presets:
            q = GENERIC_PRESETS.get(p) if isinstance(p, str) else p
            if q:
                out.append(dict(q))
        return out

    def setup_bands(self):
        """The bands /setup calibrates and patrol watches by default: ['433-435', '902-928', '88-108']."""
        return [self.setup[k] for k in ("low", "subghz", "fm") if self.setup.get(k)]

    # --- labels -----------------------------------------------------------------------------------------------
    def tv_channel(self, f_mhz, country=None):
        """{'channel', 'lo', 'hi', 'standard', 'countries'} of the TV channel f lies in, else None. With a country
        known, only the tables for it (or for every country) apply; without one, every table of the region does,
        and the answer lists every standard that uses that channel."""
        f = float(f_mhz)
        hits = []
        for t in self.tv:
            cs = [str(c).upper() for c in t.get("countries") or []]
            if country and cs and str(country).upper() not in cs:
                continue
            ch = channel_of(t, f)
            if ch:
                hits.append(dict(ch, standard=t["standard"], countries=cs))
        if not hits:
            return None
        first = hits[0]
        same = [h for h in hits if h["channel"] == first["channel"] and abs(h["lo"] - first["lo"]) < 1e-6]
        out = dict(first)
        out["standards"] = list(dict.fromkeys(h["standard"] for h in same))
        out["limited"] = not country and all(h["countries"] for h in same)
        out["countries"] = sorted({c for h in same for c in h["countries"]})
        return out

    def _tv_label(self, f, country):
        ch = self.tv_channel(f, country)
        if not ch:
            return None
        std = " or ".join(ch["standards"])
        if len(ch["standards"]) > 1:
            std += ", by country"
        elif ch["limited"]:
            std += ", " + ", ".join(self.countries.get(c, {}).get("name", c) for c in ch["countries"])
        return (ch["hi"] - ch["lo"], f"Digital TV channel {ch['channel']} ({std})")

    def candidates(self, f_mhz, country=None):
        """Every entry that contains f: (width, tier, label), tier 0 = the country's list, 1 = DAB / TV, 2 = the
        region's allocations, 3 = the base region's (shared) list."""
        f = float(f_mhz)
        c = str(country or "").upper()
        other = lambda codes: bool(c and codes and c not in codes)      # noqa: E731  written for other countries
        out = []
        for lo, hi, lab in self.country(country).get("allocations") or []:
            if lo <= f <= hi:
                out.append((hi - lo, 0, lab))
        for x in self.dab:
            if x["lo"] <= f <= x["hi"] and not other([str(k).upper() for k in x.get("countries") or []]):
                out.append((x["hi"] - x["lo"], 1, x["label"]))
        tv = self._tv_label(f, country)
        if tv:
            out.append((tv[0], 1, tv[1]))
        for tier, lst in ((2, self.allocations), (3, self.shared)):
            for lo, hi, lab in lst:
                if lo <= f <= hi and not other(self.only.get((lo, hi, lab))):
                    out.append((hi - lo, tier, lab))
        return out

    def label(self, f_mhz, country=None):
        """The narrowest entry containing f (ties: the more local list), else None."""
        c = self.candidates(f_mhz, country)
        if not c:
            p = self.fm_plan(country)
            return p.get("label") if p["lo"] <= float(f_mhz) <= p["hi"] else None
        return min(c, key=lambda x: (round(x[0], 6), x[1]))[2]

    def hop_hint(self, lo_mhz, hi_mhz):
        """The hint of the hop band that holds most of lo-hi, else None."""
        best, best_ov = None, 0.0
        for lo, hi, hint in self.hop_bands:
            ov = min(hi, hi_mhz) - max(lo, lo_mhz)
            if ov > best_ov or (ov == best_ov == 0.0 and lo <= lo_mhz <= hi and best is None):
                best, best_ov = hint, ov
        return best


def channel_of(table, f_mhz):
    """The channel of a TV table that f lies in: {'channel': '24', 'lo', 'hi'} or None (channels in `skip` are
    never TV)."""
    f = float(f_mhz)
    lo, hi, w = float(table["lo"]), float(table["hi"]), float(table["width_mhz"])
    if not lo <= f <= hi:
        return None
    k = min(int((f - lo) // w), int(round((hi - lo) / w)) - 1)
    a, b = lo + k * w, lo + (k + 1) * w
    for s0, s1 in table.get("skip") or []:
        if s0 <= f <= s1:
            return None
    names = table.get("channel_names")
    name = names[k] if names and k < len(names) else str(int(table["first_channel"]) + k)
    return {"channel": name, "lo": round(a, 6), "hi": round(b, 6)}


def channel_edges(table, channel):
    """(lo, hi) MHz of a channel of a TV table (by its name or number), else None."""
    w = float(table["width_mhz"])
    n = int(round((float(table["hi"]) - float(table["lo"])) / w))
    names = table.get("channel_names") or [str(int(table["first_channel"]) + k) for k in range(n)]
    if str(channel) not in names:
        return None
    k = names.index(str(channel))
    return round(table["lo"] + k * w, 6), round(table["lo"] + (k + 1) * w, 6)


def load(rid) -> Region:
    """The region with this id (cached; the files ship with ARIS). RegionError for an unknown id."""
    rid = str(rid or "").strip().lower()
    if rid not in REGION_IDS:
        raise RegionError(f"There is no region '{rid}'. Choose one of: {', '.join(REGION_IDS)}.")
    if rid not in _CACHE:
        data = _read(rid)
        base = load(data["base"]) if data.get("base") and data["base"] != rid else None
        try:
            _CACHE[rid] = Region(data, base)
        except (KeyError, TypeError, ValueError, IndexError) as e:
            raise RegionError(f"The region file {rid}.json is missing a part or has one of the wrong kind ({e!r}); "
                              "check it with test/test_regions.py.") from e
    return _CACHE[rid]


def available():
    """Every region that loads, in order. A broken file costs only its own region here (logged): the lists of
    choices, countries and sensor bands go on without it. The region in force is loaded with load() and fails
    loudly instead (its labels would otherwise quietly come from somewhere else)."""
    out = []
    for r in REGION_IDS:
        try:
            out.append(load(r))
        except RegionError as e:
            log.warning("band plan: %s", e)
    return out


def choices():
    return [{"id": reg.id, "name": reg.name} for reg in available()]


def countries():
    """Every country with its own layout: [{'id', 'name', 'region'}]."""
    return [{"id": c, "name": v.get("name", c), "region": reg.id}
            for reg in available() for c, v in sorted(reg.countries.items())]


def country_region(code):
    code = str(code or "").upper()
    return next((reg.id for reg in available() if code in reg.countries), None)


# --- the time-zone guess ------------------------------------------------------------------------------------------
def _plain_zone(name):
    """'posix/Europe/Berlin' and 'right/Europe/Berlin' (other copies of the zone files) -> 'Europe/Berlin'."""
    z = str(name or "").strip()
    for p in ("posix/", "right/"):
        if z.startswith(p):
            z = z[len(p):]
    return z or None


def zone_name(environ=None, localtime="/etc/localtime", timezone_file="/etc/timezone"):
    """The computer's time zone name ('Europe/Berlin'), from TZ, /etc/timezone or the /etc/localtime link; None.
    Never raises: an unreadable or garbled source is skipped."""
    env = os.environ if environ is None else environ
    tz = str(env.get("TZ") or "").strip().lstrip(":")
    if tz.startswith("/") and "zoneinfo/" in tz:
        return _plain_zone(tz.split("zoneinfo/", 1)[1])
    # a zone name: 'Area/City', or a one-word name the guess knows ('Japan', 'NZ', 'GB'); a POSIX rule such as
    # 'EST5EDT,M3.2.0,M11.1.0' or a bare 'UTC' leaves the decision to the files, as before
    if tz and _ZONE_NAME_RX.fullmatch(tz) and ("/" in tz or tz in _ZONE_EXACT):
        return _plain_zone(tz)
    try:
        with open(timezone_file, encoding="utf-8") as f:
            t = f.read().strip()
        if t and _ZONE_NAME_RX.fullmatch(t):
            return _plain_zone(t)
    except (OSError, ValueError):
        pass
    try:
        target = os.path.realpath(localtime)
        if "zoneinfo/" in target:
            return _plain_zone(target.split("zoneinfo/", 1)[1])
    except (OSError, ValueError):
        pass
    return None


def zone_country(zone, tab_files=_ZONE_TAB):
    """The country code the system's zone table gives a zone ('Europe/Berlin' -> 'DE'), else None (also for a table
    that cannot be read)."""
    for path in tab_files:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("#"):
                        continue
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) >= 3 and parts[2] == zone:
                        return parts[0].split(",")[0].upper()
        except (OSError, ValueError):
            continue
    return None


def guess(zone, tab_files=_ZONE_TAB):
    """(region id, country code or None) from a time zone name. The country is given only when the region has its
    own layout for it (US, CA, MX, BR, AU, NZ ...)."""
    z = str(zone or "").strip()
    if not z or z.startswith(("Etc/", "UTC", "GMT", "Universal", "Zulu")):
        return DEFAULT_REGION, None
    code = zone_country(z, tab_files)
    rid = _ZONE_EXACT.get(z)
    if rid is None:
        rid = next((r for p, r in _ZONE_PREFIX if z.startswith(p)), DEFAULT_REGION)
        if code == "JP":
            rid = "japan"
        elif code in ("AU", "NZ"):
            rid = "australia"
        elif rid == "asia_pacific" and code in _REGION1_ASIA | _ASIA_PREFIX_REGION1:
            rid = "europe"
        elif rid == "asia_pacific" and code in _ASIA_PREFIX_REGION2:
            rid = "americas"
    try:
        country = code if code and code in load(rid).countries else None
    except RegionError:                 # that region's file is broken: the guess still names the region
        country = None
    return rid, country


def resolve(region=None, country=None, country_set=False, zone=None, tab_files=_ZONE_TAB):
    """What applies: the configured region (else the time zone's guess, else the world), and the country (the
    configured one when country_set, else the guess when it belongs to the region). An unknown id in the config falls
    back to the guess, with 'problem' saying so, and a time zone that cannot be read counts as none. The one error:
    RegionError when the file of the region in force cannot be read (it names the file; the labels never quietly
    come from another region)."""
    problem = None
    rid = str(region or "").strip().lower()
    if rid and rid not in REGION_IDS:
        problem, rid = f"unknown region '{rid}' in the config", ""
    if zone is None:
        try:
            z = zone_name()
        except Exception:                   # zone_name skips what it cannot read; this is the last guard
            z = None
    else:
        z = zone
    try:
        g_rid, g_country = guess(z, tab_files) if z else (DEFAULT_REGION, None)
    except Exception:
        g_rid, g_country = DEFAULT_REGION, None
    if rid:
        source, guessed = "config", False
    elif z and (g_rid != DEFAULT_REGION or g_country):
        rid, source, guessed = g_rid, "time zone", True
    else:
        rid, source, guessed = DEFAULT_REGION, "default", True
    reg = load(rid)
    if country_set:
        c = str(country or "").upper() or None
        if c and c not in reg.countries:
            problem, c = (problem or f"country '{c}' does not belong to {reg.short}"), None
        c_guessed = False
    else:
        c = g_country if g_country and g_country in reg.countries else None
        c_guessed = bool(c)
    return {"region": reg, "id": rid, "name": reg.name, "short": reg.short, "country": c,
            "country_name": reg.countries.get(c, {}).get("name") if c else None, "guessed": guessed,
            "country_guessed": c_guessed, "source": source, "zone": z, "problem": problem}


def describe(state):
    """'Americas (guessed from the time zone; change it under Spectrum)' for /status, /check and the page."""
    name = state["short"] + (f", {state['country_name']}" if state.get("country_name") else "")
    if state["source"] == "time zone":
        return f"{name} (guessed from the time zone; change it under Spectrum)"
    if state["source"] == "default":
        return f"{name} (no region set; choose one under Spectrum)"
    return name


# --- the FM channel plan ------------------------------------------------------------------------------------------
def fm_channels(plan):
    """Every channel of the plan (MHz), lowest first."""
    step = float(plan["raster_khz"]) / 1000.0
    first, last = float(plan["first"]), float(plan["last"])
    n = int(round((last - first) / step))
    out = [round(first + k * step, 3) for k in range(n + 1)]
    return sorted(set(out) | {round(float(x), 3) for x in plan.get("exceptions_mhz") or []})


def nearest_channel(plan, f_mhz):
    """(nearest channel MHz, offset kHz) on the plan, the plan's band edges included."""
    f = float(f_mhz)
    step = float(plan["raster_khz"]) / 1000.0
    first, last = float(plan["first"]), float(plan["last"])
    k = round((f - first) / step)
    k = max(0, min(int(round((last - first) / step)), int(k)))
    best = round(first + k * step, 3)
    for x in plan.get("exceptions_mhz") or []:
        if abs(f - float(x)) < abs(f - best):
            best = round(float(x), 3)
    return best, (f - best) * 1000.0


def on_fm_plan(plan, f_mhz, tol_khz):
    """Is f within tol_khz of a channel of the plan? Odd tenths in the Americas; any raster step elsewhere (where every
    tenth is a channel, so a station on any tenth is never off-plan)."""
    f = float(f_mhz)
    if not (float(plan["lo"]) <= f <= float(plan["hi"])):
        return False
    ch, off = nearest_channel(plan, f)
    return abs(off) <= float(tol_khz) and float(plan["first"]) - 0.05 <= ch <= float(plan["last"]) + 0.05 or \
        any(abs(f - float(x)) * 1000.0 <= float(tol_khz) for x in plan.get("exceptions_mhz") or [])


def clearly_off(plan, carrier_mhz, off_khz, off_limit_khz):
    """A carrier is clearly off the channels when it sits nearer a frequency that is no channel (an even tenth under
    an odd-tenths plan) or off_limit_khz or more from its channel."""
    if abs(float(off_khz or 0.0)) >= float(off_limit_khz):
        return True
    if plan.get("plan") == "odd_tenths":
        tenth = int(round(float(carrier_mhz) * 10.0))
        exc = {int(round(float(x) * 10)) for x in plan.get("exceptions_mhz") or []}
        return tenth % 2 == 0 and tenth not in exc
    return False


def all_sensor_bands():
    """The union of every region's and country's sensor bands (a decode outside all of them is doubtful)."""
    out = set()
    for reg in available():
        out |= {tuple(x) for x in reg.sensor_bands}
        for c in reg.countries.values():
            out |= {tuple(x) for x in c.get("sensor_bands") or []}
    return sorted(out)


def is_nav(f_mhz):
    """Aeronautical navigation (VOR / ILS localizer beacons, 108-117.975 MHz): steady AM carriers with tones."""
    return NAV_BAND[0] <= float(f_mhz) < NAV_BAND[1]


def band_key(text):
    """'433-435' -> (433.0, 435.0); None when it is no 'lo-hi' text."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*", str(text or ""))
    return (float(m.group(1)), float(m.group(2))) if m else None
