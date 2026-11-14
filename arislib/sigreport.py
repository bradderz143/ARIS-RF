"""What a Decode measured, judged and put into plain words.

The engine gathers facts about one investigated frequency (what each dongle's look measured, what the decoders
read, what is on record for the frequency); this module decides what may be said about them and words it: one
headline, how sure and what that rests on, what the signal did, the spur check, the history and the one next
step. Every sentence of the Decode card and of the chat's report lines is made here, so the page, the chat and
the AI say the same thing.

The rules are about never saying more than was measured:
- only a decoder's result is an identification; a tone, a symbol rate or a burst shape is a pattern;
- a confidence word needs a test that could have failed, and the same line says what it rests on;
- a steady frequency never makes a spur (a licensed transmitter and an equipment clock are both crystal
  controlled), and levels are never compared between dongles (each has its own antenna, gain and noise);
- history counts saved checks, so it says "saved on N days", never when a signal first went on air.

Pure Python, standard library only: no engine, no web server, no radio is touched here.
"""
from __future__ import annotations

import datetime
import math
import re
import time

REF_CLOCK_MHZ = 28.8             # an RTL-SDR's crystal: every such dongle shows lines on its multiples
OWN_TIGHT_PPM = 1.5              # a dongle's own line reads within this (plus OWN_TIGHT_HZ) of where it is predicted
OWN_TIGHT_HZ = 50.0
OWN_NEAR_PPM = 100.0             # "next to" a clock multiple: another receiver's crystal reads this close
EQUIP_CLOCKS_MHZ = (8.0, 10.0, 12.0, 14.31818, 16.0, 20.0, 24.0, 25.0, 26.0, 27.0, 40.0, 48.0, 50.0)
EQUIP_MAX_MULT = 40
EQUIP_PPM = 60.0
HARMONIC_PPM = 5.0               # per multiple of the earlier carrier, plus HARMONIC_HZ
HARMONIC_HZ = 300.0
DC_SPIKE_HZ = 300.0              # a line this close to the look's tuning centre may be the receiver's own spike
SAME_MIN_HZ = 500.0              # the dongles "read the same frequency" within max(this, SAME_PPM x f)
SAME_PPM = 20.0
SAME_LOOSE_HZ = 3000.0           # ... or within this when a dongle gave no single clear line
DRIFT_FLOOR_HZ_S = 2.0           # movement under max(this, DRIFT_FLOOR_PPM_S x f) is "held its frequency"
DRIFT_FLOOR_PPM_S = 0.01
DRIFT_ONE_PPM_S = 0.1            # one dongle alone: this much is as likely the dongle's own clock
DRIFT_AGREE = 0.3                # two dongles show "the same movement" within this share of it
FREE_RUN_PPM_S = 0.3             # faster than any crystal moves: a free-running oscillator
CRYSTAL_PPM = 0.1                # "crystal-controlled": this little movement over CRYSTAL_MIN_S on two dongles
CRYSTAL_MIN_S = 10.0
WANDER_HZ = 150.0                # readings this far apart from look to look (or WANDER_PPM x f) wander: no crystal
WANDER_PPM = 0.5
OFF_ASKED_KHZ = 3.0              # a carrier this far from the frequency picked is said to be elsewhere
DECODER_REACH_KHZ = 6.0          # the audio decoders hear about this far either side of their tuning
STRONG_DB = 8.0                  # a dongle that hears it this clearly does not miss it by chance
HIST_NEAR_KHZ = 25.0             # saved checks this close are "next to it", not counted
HIST_WIDE_KHZ = 60.0             # FM channels and sensor bands: one window for everything on the frequency
HIST_REGULAR_DAYS = 3
HIST_REGULAR_SPAN = 7
HIST_LIST_DAYS = 3               # a saved list younger than this cannot show that something is not on record
HIST_RECENT_H = 6
BASELINE_CLEAR_DB = 6.0          # a baseline "had it": this far above its floor (3 to 6 dB is a weak rise, not clear)
BASELINE_QUIET_DB = 3.0
BAR_FULL_DB = 40.0

CONFIDENCE_TEXT = {"confirmed": "Confirmed by a decoder", "high": "High confidence", "medium": "Medium confidence",
                   "low": "Low confidence", "unknown": "", "check": "Check first"}
BARS_NOTE = "Above each dongle's own noise. Not a distance."
_TAG_KIND = {"IDENTIFIED": "confirmed_decode", "CHECK FIRST": "doubtful", "OWN CLOCK": "own",
             "DECODER FAILED": "failed", "NOTHING HEARD": "none", "NOT IDENTIFIED": "signal_present",
             "HEARD": "signal_present"}
_HISTORY_KIND = {"NO EARLIER RECORD": "new", "SEEN BEFORE": "seen", "REGULAR HERE": "regular", "NEW DEVICE": "device",
                 "KNOWN DEVICE": "device"}
_WHY = {"too_weak": "the signal was too weak", "too_short": "it was on too briefly",
        "overloaded": "the look was overloaded", "no_single_line": "it has no single clear line",
        "wider_than_filter": "it is wider than the look's filter", "unsteady": "its level was unsteady",
        "not_on": "it was not on during the look"}
# who uses a symbol rate with that many levels: a pattern to mention, never a name for the signal
_RATE_USERS = {(512, 2): "POCSAG pagers", (1200, 2): "POCSAG pagers", (2400, 2): "POCSAG pagers",
               (1600, 2): "FLEX pagers", (3200, 2): "FLEX pagers", (1600, 4): "FLEX pagers", (3200, 4): "FLEX pagers",
               (2400, 4): "digital two-way radio", (4800, 4): "digital two-way radio"}
_CAPTURE_ENDS = {"identified": "it identified a device", "demodulated": "it read packets that no decoder knows",
                 "timeout": "nothing was decoded", "stopped": "it was stopped before anything was decoded"}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_NOT_RTL = ("soapy-", "bb60-", "sa-")
_PLAIN = (None, "none", "untested")         # modulation kinds that leave "a carrier" standing
_STEADY_HEAD = "Steady unmodulated carrier"     # how a saved check's first line names a steady carrier
_SPECIFIC_HINT = "EV1527"                   # the one burst-pattern hint that is a test (pulse ratio and packet length);
                                            # the others ("remotes and simple sensors") fit any burst
_QRANK = {"A": 3, "B": 2, "C": 1}
_STEPS = ("unknown", "low", "medium", "high")


# ----------------------------------------------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------------------------------------------
def _num(x):
    """x as a float, or None (also for NaN, infinity, a bool, or text that is no number)."""
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _int(x, default=0) -> int:
    v = _num(x)
    return int(round(v)) if v is not None and abs(v) < 1e9 else default


def _mhz(x):
    """x as a frequency in MHz, or None when it is not one a receiver could name."""
    v = _num(x)
    return v if v is not None and 0.0 < v < 1e5 else None


def _within(x, lo, hi):
    """x as a float when it lies in lo..hi, else None (a wild number never reaches a sentence)."""
    v = _num(x)
    return v if v is not None and lo <= v <= hi else None


def _dict(x) -> dict:
    return x if isinstance(x, dict) else {}


def _dicts(x) -> list:
    return [d for d in x if isinstance(d, dict)] if isinstance(x, (list, tuple)) else []


def _word(x):
    """x when it is a string (a key to look up), else None."""
    return x if isinstance(x, str) else None


def _median(xs):
    s = sorted(xs)
    n = len(s)
    return None if not n else s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def fmt_mhz(f, places=4, fixed=False) -> str:
    """A frequency for a sentence, in MHz: 172.8364, 96.9, 1090 (trailing zeros trimmed; never ':g', which turns
    172.8364 into 172.836). fixed=True keeps the places: a carrier read to 1 kHz is '172.800'."""
    v = _num(f)
    if v is None:
        return "?"
    s = f"{v:.{max(0, min(6, _int(places, 4)))}f}"
    return s if fixed or "." not in s else s.rstrip("0").rstrip(".")


def _cut(text, n) -> str:
    """text in at most n characters, cut at a word and marked with '...'."""
    t = str(text or "")
    if len(t) <= n:
        return t
    head = t[:max(0, n - 4)]
    head = head.rsplit(" ", 1)[0] if " " in head else head
    return head.rstrip(" ,;:(") + " ..."


def _clean(text, n=220) -> str:
    """Free text for a sentence: one line, no control characters, nothing the wording rules forbid, at most n."""
    t = "".join(c if c.isprintable() else " " for c in str(text if text is not None else ""))
    t = re.sub(r"(?<=[A-Za-z])_(?=[A-Za-z])", " ", t).replace("+-", "+ -")
    return _cut(" ".join(t.split()), n)


def _unbracket(t) -> str:
    """t, without its brackets when one is left open or sits inside another (the sentence around it must stay
    readable, and never gets a bracket inside a bracket)."""
    depth = worst = 0
    for c in t:
        depth += (c in "([{") - (c in ")]}")
        worst = max(worst, depth)
        if depth < 0:
            break
    return " ".join(re.sub(r"[()\[\]{}]", " ", t).split()) if depth or worst > 1 else t


def _flat(text, n=220) -> str:
    """Free text from the engine (a verdict, a fingerprint, a doubt) for use inside a sentence."""
    return _unbracket(_clean(text, n))


def _name(text, n=40) -> str:
    """A name that strangers or the user chose (a station's, a device's, a dongle's label) made safe for a
    sentence: cut at n, no double quotes (the sentence may quote it), no bracket left open or nested."""
    t = _clean(text, 10 ** 6).replace('"', "'")
    return _unbracket(t if len(t) <= n else t[:max(0, n - 3)].rstrip() + "...")


def _and(items) -> str:
    """'Dongle 1', 'Dongle 1 and Dongle 2', 'Dongle 1, Dongle 2 and Dongle 3'."""
    items = [str(x) for x in items if x]
    return ", ".join(items[:-1]) + (" and " if len(items) > 1 else "") + items[-1] if items else ""


def _poss(names) -> str:
    """\"Dongle 1's and Dongle 3's\"."""
    return _and(f"{n}'s" for n in names)


def _plural(n, word) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _sentence(text) -> str:
    t = str(text or "").strip()
    return t + ("" if t.endswith((".", "!", "?")) else ".") if t else ""


def _cap(text) -> str:
    t = str(text or "")
    return t[:1].upper() + t[1:]


def _secs(x) -> str:
    """A duration for a sentence: '3 s', '2.6 s'."""
    v = _num(x) or 0.0
    return (f"{v:.0f}" if v >= 10.0 or abs(v - round(v)) < 0.05 else f"{v:.1f}") + " s"


def _dist(khz) -> str:
    """A distance in frequency: '36 kHz', '1.4 kHz', '14.4 MHz'."""
    k = abs(_num(khz) or 0.0)
    if k >= 1000.0:
        return f"{fmt_mhz(k / 1000.0, 1)} MHz"
    return f"{k:.0f} kHz" if k >= 10.0 else f"{k:.1f} kHz"


def _moved(hz) -> str:
    """How far a frequency moved: 'less than 10 Hz', '30 Hz', '0.9 kHz'."""
    h = abs(_num(hz) or 0.0)
    return "less than 10 Hz" if h < 10.0 else f"{round(h / 10.0) * 10:.0f} Hz" if h < 100.0 else _dist(h / 1000.0)


def _off(khz) -> str:
    """A distance from a clock multiple: 'less than 0.1 kHz', '36 kHz'."""
    return "less than 0.1 kHz" if abs(_num(khz) or 0.0) < 0.05 else _dist(khz)


def _mains_of(tone):
    """50.0 or 60.0 when a tone is (to half a per cent) a multiple of that mains frequency, else None: 100.0,
    179.9, 199.5 and 250.3 Hz are standard access tones and also where mains hum sits."""
    t = _within(tone, 20.0, 3000.0)
    for base in (50.0, 60.0) if t else ():
        k = round(t / base)
        if k >= 2 and abs(t - base * k) <= 0.005 * t + 0.35:
            return base
    return None


def _tenth(hz) -> str:
    """A span in kHz rounded up to a tenth, for 'within ...' and 'less than ...': '0.1 kHz', '0.3 kHz'."""
    return f"{max(1, int(math.ceil(abs(_num(hz) or 0.0) / 100.0 - 1e-9))) / 10.0:.1f} kHz"


def _why(code) -> str:
    return _WHY.get(str(code or ""), "it could not be measured")


def _day(ts):
    """An epoch as a local date, 'YYYY-MM-DD', or None."""
    v = _num(ts)
    if v is None or v <= 0:
        return None
    try:
        t = time.localtime(v)
    except (OverflowError, OSError, ValueError):
        return None
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"


def _day_words(iso, now=None) -> str:
    """'5 Oct', or '5 Oct 2025' when that is not this year."""
    try:
        d = datetime.date.fromisoformat(str(iso))
    except ValueError:
        return "an unknown date"
    this = _day(now if now is not None else time.time())
    return f"{d.day} {_MONTHS[d.month - 1]}" + ("" if this and this[:4] == f"{d.year:04d}" else f" {d.year}")


def _days_between(a, b) -> int:
    try:
        return abs((datetime.date.fromisoformat(str(b)) - datetime.date.fromisoformat(str(a))).days)
    except ValueError:
        return 0


def _where(names, n_listen) -> str:
    """Who heard it: 'on all 3 dongles', 'on both dongles', 'on Dongle 1 and Dongle 2', 'on Dongle 2 only', or
    'on Dongle 1' when only one listened. '' when nobody did."""
    names = [n for n in names if n]
    if not names:
        return ""
    if n_listen <= 1:
        return f"on {names[0]}"
    if len(names) >= n_listen:
        return "on both dongles" if n_listen == 2 else f"on all {n_listen} dongles"
    if len(names) > 3:                      # (a list of four or more names does not fit a headline)
        return f"on {len(names)} of the {n_listen} dongles"
    return f"on {names[0]} only" if len(names) == 1 else "on " + _and(names)


def _who(names, n_all, start=True) -> str:
    """The same as a subject: 'All 3 dongles', 'Both dongles', 'Dongle 1 and Dongle 2' (start=False: mid-sentence)."""
    if len(names) >= 2 and len(names) >= n_all:
        text = "both dongles" if len(names) == 2 else f"all {len(names)} dongles"
        return _cap(text) if start else text
    if len(names) > 3:
        return f"{len(names)} of the {n_all} dongles" if n_all > len(names) else f"{len(names)} dongles"
    return _and(names)


# ----------------------------------------------------------------------------------------------------------------
# the dongles' own clock, equipment clocks, harmonics
# ----------------------------------------------------------------------------------------------------------------
def own_clock(line_mhz, ppm, ref_mhz=REF_CLOCK_MHZ) -> dict:
    """Is a measured line a multiple of the dongle's own clock? A dongle shows its own clock line at
    n x ref x (1 + ppm x 1e-6) whatever its crystal's true error, because the same crystal tunes it and the
    correction is applied on top. 'match': within 1.5 ppm of the line + 50 Hz of that prediction. 'near': within
    100 ppm of the plain multiple (another receiver's clock, or a transmitter next to it). offset_khz is the line
    minus the plain multiple."""
    f, ref = _num(line_mhz), _num(ref_mhz)
    ref = ref if ref and ref > 0 else REF_CLOCK_MHZ
    if f is None or f <= 0:
        return {"result": "no", "multiple": None, "nominal_mhz": None, "offset_khz": None, "tolerance_hz": None}
    n = max(1, int(round(f / ref)))
    nominal = n * ref
    tol_hz = OWN_TIGHT_PPM * f + OWN_TIGHT_HZ                      # ppm of a frequency in MHz is Hz
    off_hz = (f - nominal) * 1e6
    own_hz = (f - nominal * (1.0 + (_num(ppm) or 0.0) * 1e-6)) * 1e6
    result = "match" if abs(own_hz) <= tol_hz else "near" if abs(off_hz) <= OWN_NEAR_PPM * nominal else "no"
    return {"result": result, "multiple": n, "nominal_mhz": round(nominal, 6), "offset_khz": round(off_hz / 1000.0, 3),
            "tolerance_hz": int(round(tol_hz))}


def _equip_one_in(f) -> int:
    """How rare a chance match with a common clock is near f: 1 unrelated carrier in N lands on a multiple, from
    the share of the 10 % of spectrum around f that the multiples' windows cover. Rounded down."""
    lo, hi = 0.95 * f, 1.05 * f
    spans = sorted((m * (1 - EQUIP_PPM * 1e-6), m * (1 + EQUIP_PPM * 1e-6))
                   for c in EQUIP_CLOCKS_MHZ for m in (c * n for n in range(1, EQUIP_MAX_MULT + 1)) if lo <= m <= hi)
    covered, end = 0.0, lo
    for a, b in spans:
        a, b = max(a, end), min(b, hi)
        if b > a:
            covered, end = covered + b - a, b
    n = int((hi - lo) / covered) if covered > 0 else 0
    step = 1 if n < 20 else 10 if n < 200 else 10 ** (len(str(n)) - 2)
    return max(1, n // step * step)


def _equipment_clock(f):
    """The lowest multiple of a common equipment clock within 60 ppm of f, or None."""
    best = None
    for c in EQUIP_CLOCKS_MHZ:
        n = int(round(f / c))
        if 1 <= n <= EQUIP_MAX_MULT:
            ppm = (f - n * c) / (n * c) * 1e6
            if abs(ppm) <= EQUIP_PPM and (best is None or n < best[0]):
                best = (n, c, ppm)
    if best is None:
        return None
    return {"clock_mhz": round(best[1], 3), "multiple": best[0], "offset_ppm": round(best[2], 1),
            "one_in": _equip_one_in(f)}


def _harmonic_of(f, carriers):
    """f as the 2nd to 5th harmonic of a carrier measured in an earlier check, or None."""
    for k in (2, 3, 4, 5):
        for f0 in carriers:
            if f0 > 1.0 and abs(f - k * f0) * 1e6 <= k * HARMONIC_PPM * f0 + HARMONIC_HZ:
                return {"mhz": round(f0, 6), "times": k}
    return None


# ----------------------------------------------------------------------------------------------------------------
# the facts, read once
# ----------------------------------------------------------------------------------------------------------------
def _dongles(raw) -> list:
    """The facts' dongle entries with everything looked up once (missing keys become None or empty). 'first_heard'
    (default: as 'heard') says whether the dongle's first look had the signal: a dongle that watched may have found
    it only in a later look, and its profile is then that look's. A later look may carry 'whole': it was on from
    the look's start to its end (default True)."""
    out = []
    for d in _dicts(raw):
        if d.get("_read"):
            out.append(d)
            continue
        prof = _dict(d.get("profile"))
        line, serial = _dict(prof.get("line")), _clean(d.get("serial") or "", 40)
        lm = _mhz(prof.get("line_mhz"))
        err, folded = bool(d.get("error")), bool(prof.get("folded"))
        out.append({"_read": True, "name": _name(d.get("dongle"), 24) or _name(serial, 24) or "A dongle",
                    "serial": serial,
                    "error": err, "heard": bool(d.get("heard")) and not err, "snr": _num(d.get("snr_db")),
                    "first": bool(d.get("first_heard", d.get("heard"))) and not err,
                    "ppm": _num(d.get("ppm")) or 0.0,
                    "rtl": bool(d["rtl"]) if "rtl" in d else not serial.startswith(_NOT_RTL),
                    "continuous": d.get("continuous"),
                    "prof": prof, "key": _dict(prof.get("key")), "line": line,
                    "folded": folded,           # wider than the look holds: its middle is not a carrier
                    "line_mhz": lm if line.get("ok") and not folded else None,
                    "carrier": None if folded else lm or _mhz(d.get("carrier_mhz")),
                    "quality": _word(prof.get("quality")) if _word(prof.get("quality")) in _QRANK else None,
                    "looks": _dicts(d.get("looks"))[:60]})
    return out


def _snr_on(d):
    v = _num(d["prof"].get("snr_on_db"))
    return v if v is not None else d["snr"]


def _rank(d) -> tuple:
    v = _snr_on(d)
    return (_QRANK.get(d["quality"], 0), v if v is not None else -99.0)


_LOOK_PATTERN = {"on_whole": "steady", "keyed": "keyed", "bursts": "bursts", "off": "off", "unclear": "unclear"}


def _watch(v):
    """The looks made after the first one: by the watchers (dongles with no decoder job) during the decoder time,
    or, on the airband and FM, by every dongle. 'gone': a later look missed it on a dongle that hears it clearly,
    so it went off (a weak dongle's miss proves nothing: 'weak_miss'). None without later looks."""
    watchers = [d for d in v["listening"] if d["looks"]]
    if watchers:
        def level(d):
            heard_at = [s for s in (_num(x.get("snr_db")) for x in d["looks"] if x.get("heard")) if s is not None]
            first = _snr_on(d) if d["heard"] else None
            return max(heard_at + ([first] if first is not None else []) + [-99.0])
        ts = [t for d in watchers for t in (_within(x.get("t"), 0.0, 86400.0) for x in d["looks"]) if t is not None]
        missed = [d for d in watchers if any(not x.get("heard") for x in d["looks"])]
        strong = [d["name"] for d in missed if level(d) >= STRONG_DB]
        return {"names": [d["name"] for d in watchers], "per": max(len(d["looks"]) for d in watchers),
                "looks": sum(len(d["looks"]) for d in watchers), "first": sum(1 for d in watchers if d["first"]),
                "heard": sum(1 for d in watchers for x in d["looks"] if x.get("heard")),
                "span_s": int(round(max(ts) - min(ts) + v["look_s"])) if ts else v["listen_s"],
                "gone": bool(strong), "weak_miss": [d["name"] for d in missed if d["name"] not in strong]}
    n = _int(v["air"].get("looks"))
    if n >= 2 and _num(v["air"].get("looks_heard")) is not None:
        h = min(n, max(0, _int(v["air"].get("looks_heard"))))
        return {"names": [], "per": n, "looks": n, "heard": h, "span_s": v["listened_s"] or v["listen_s"],
                "gone": h < n, "weak_miss": []}
    return None


def _view(facts) -> dict:
    """Everything the rules ask of the facts, worked out once: who listened and heard, the clearest look, the
    on / off pattern, the watch, the modulation, the carrier, the quality of the look."""
    f = _dict(facts)
    v = {"asked": _mhz(f.get("asked_mhz")), "kind": str(f.get("band_kind") or "other"),
         "evidence": f.get("evidence"), "verdict": _flat(f.get("verdict") or "", 2000),
         "listen_s": max(0, _int(f.get("listen_s"))), "listened_s": max(0, _int(f.get("listened_s"))),
         "map_ready": bool(f.get("map_ready")), "capture_running": _dict(f.get("capture_running")),
         "new_devices": max(0, _int(f.get("new_devices"))), "known_devices": max(0, _int(f.get("known_devices"))),
         "now": _num(f.get("now")) or time.time(), "audio": _dict(f.get("audio")), "rtl433": _dict(f.get("rtl433")),
         "adsb": _dict(f.get("adsb")), "fm": _dict(f.get("fm")), "air": _dict(f.get("air")),
         "history": _dict(f.get("history")), "tune_offset_hz": _num(f.get("tune_offset_hz")),
         "carriers": f.get("carriers")}
    devs = _dicts(f.get("devices"))
    v["devices_ok"] = [d for d in devs if not d.get("doubts")]
    v["devices_bad"] = [d for d in devs if d.get("doubts")]
    said = [w for w in (f.get("doubtful") if isinstance(f.get("doubtful"), (list, tuple)) else [])]
    said = said or [w for d in v["devices_bad"]
                    for w in (d["doubts"] if isinstance(d["doubts"], (list, tuple)) else [d["doubts"]])]
    v["doubts"] = [w for w in (_flat(x, 120) for x in said) if w]
    v["decoder_failed"] = bool(f.get("decoder_failed")) or v["verdict"].startswith("Decoding was not possible")
    ds = v["dongles"] = _dongles(f.get("dongles"))
    v["listening"] = [d for d in ds if not d["error"]]
    v["n_listen"] = len(v["listening"])
    heard = v["heard"] = [d for d in ds if d["heard"]]
    v["heard_names"] = [d["name"] for d in heard]
    v["where"] = _where(v["heard_names"], v["n_listen"])
    v["where_sp"] = " " + v["where"] if v["where"] else ""
    strongest = _name(f.get("strongest") or "", 24)
    v["strongest"] = strongest if strongest in v["heard_names"] else (v["heard_names"][0] if heard else None)

    looked = [d for d in heard if _word(d["key"].get("state")) in _LOOK_PATTERN]
    c = v["clearest"] = max(looked, key=_rank) if looked else None
    v["look_s"] = (_within(c["prof"].get("seconds"), 0.2, 60.0) if c else None) or 3.0
    v["look_txt"] = _secs(v["look_s"])
    if c:
        pattern = _LOOK_PATTERN[c["key"]["state"]]
    else:                                   # no profile: only what the look's own class said
        flags = [d["continuous"] for d in heard if d["continuous"] is not None]
        pattern = "off" if not heard else "steady" if flags and all(flags) else \
            "bursts" if flags and (v["kind"] == "sensor" or _int(v["rtl433"].get("bursts")) > 0) else "unclear"
    v["look_pattern"] = pattern

    watch = v["watch"] = _watch(v)
    v["scope"] = "watch" if watch else "look"
    v["pattern"] = "keyed" if pattern == "steady" and watch and watch["gone"] else pattern

    full = sorted([d for d in heard if _dict(d["prof"].get("mod")).get("kind")], key=_rank, reverse=True)
    tested = [d for d in full if d["prof"]["mod"]["kind"] != "untested"]
    src = tested[0] if tested else full[0] if full else None
    v["mod"] = _dict(src["prof"]["mod"]) if src else {}
    v["modkind"] = str(v["mod"].get("kind")) if src else None
    v["mod_dongle"] = src["name"] if src else None

    lines = [d["line_mhz"] for d in heard if d["line_mhz"]]
    best = next((d for d in heard if d["name"] == v["strongest"]), None)
    carrier = _median(lines) if lines else _mhz(f.get("measured_mhz"))
    v["carrier"] = carrier if heard else None
    off = abs(v["carrier"] - v["asked"]) * 1000.0 if v["carrier"] and v["asked"] else 0.0
    v["off_khz"] = off
    v["act_mhz"] = round(v["carrier"], 4) if v["carrier"] and (off >= OFF_ASKED_KHZ or not v["asked"]) \
        else round(v["asked"] or 0.0, 4)

    q = (c["quality"] if c else None) or ("B" if best and (best["snr"] or 0.0) >= STRONG_DB else "C")
    if q == "A" and len(heard) < 2 and not watch:
        q = "B"                             # nothing agrees with a single look
    v["q"] = q
    v["overloaded"] = [d["name"] for d in heard if d["prof"].get("overloaded")]
    v["drift"] = compare_drift(ds, v["carrier"] or v["asked"])
    return v


# ----------------------------------------------------------------------------------------------------------------
# drift: the signal's or a dongle's
# ----------------------------------------------------------------------------------------------------------------
def _track(d):
    """(slope in Hz per second, seconds covered, scatter in Hz) of one dongle's line, from its looks over the
    listen when there are enough of them, else from within its first look. The scatter is how far the readings lie
    apart once the slope is taken out. None without a clear line."""
    look_s = _within(d["prof"].get("seconds"), 0.2, 60.0) or 3.0
    pts = sorted((t, m) for t, m in ((_within(x.get("t"), 0.0, 86400.0), _mhz(x.get("line_mhz")))
                                     for x in d["looks"] if x.get("heard")) if t is not None and m)
    if d["line_mhz"] and not any(t < look_s for t, _ in pts):
        pts.insert(0, (look_s / 2.0, d["line_mhz"]))
    if len(pts) >= 3 and pts[-1][0] - pts[0][0] >= 6.0:
        slopes = [(b[1] - a[1]) * 1e6 / (b[0] - a[0]) for i, a in enumerate(pts) for b in pts[i + 1:]
                  if b[0] - a[0] > 0.5]
        if slopes:                                    # Theil-Sen: the median of the pairwise slopes
            sl = _median(slopes)
            res = [m * 1e6 - sl * t for t, m in pts]
            return sl, pts[-1][0] - pts[0][0], max(res) - min(res)
    slope, span = _within(d["line"].get("slope_hz_s"), -1e6, 1e6), _within(d["line"].get("span_s"), 0.1, 60.0)
    if d["line"].get("ok") and slope is not None and span:
        return slope, span, _within(d["line"].get("scatter_hz"), 0.0, 1e6) or 0.0
    return None


def compare_drift(dongles, f_mhz) -> dict:
    """Whose movement a measured drift is. Each dongle's own clock moves too, so: every dongle within the floor =
    'none' (held its frequency); two or more showing the same movement = 'signal'; dongles that differ = 'dongle'
    (their own clocks; 'odd' names the one that differs when the others agree); one dongle alone = 'unknown' up to
    0.1 ppm per second, 'signal' above that (more than a dongle's clock normally moves). 'hz' is the movement
    over 'over_s' seconds ('none': the most any dongle showed; 'dongle': how far they moved apart)."""
    f = _num(f_mhz) or 0.0
    tracks = [(d["name"], t[0], t[1], t[2]) for d in _dongles(dongles) if d["heard"] for t in [_track(d)] if t]
    long = [t for t in tracks if t[2] >= 6.0]
    if len(long) >= 2:                                # the watch covers more time than one look: judge by it
        tracks = long
    out = {"source": "unknown", "hz": None, "over_s": None, "dongles": len(tracks), "odd": None, "ppm_s": None,
           "names": [t[0] for t in tracks], "scatter_hz": int(round(max(t[3] for t in tracks))) if tracks else None}
    if not tracks:
        return out
    slopes, over = [t[1] for t in tracks], min(t[2] for t in tracks)
    floor = max(DRIFT_FLOOR_HZ_S, DRIFT_FLOOR_PPM_S * f)
    med = _median(slopes)
    spread = max(slopes) - min(slopes)
    agree = lambda ss: max(ss) - min(ss) <= max(DRIFT_FLOOR_HZ_S, DRIFT_AGREE * abs(_median(ss)))
    if len(tracks) == 1:
        rate, source = abs(med), "unknown" if abs(med) <= max(DRIFT_FLOOR_HZ_S, DRIFT_ONE_PPM_S * f) else "signal"
    elif max(abs(s) for s in slopes) <= floor:
        rate, source = max(abs(s) for s in slopes), "none"
    elif agree(slopes) and abs(med) > floor:
        rate, source = abs(med), "signal"
    else:
        rate, source = spread, "dongle"
        if len(tracks) >= 3:
            odd = [t[0] for i, t in enumerate(tracks) if agree(slopes[:i] + slopes[i + 1:])]
            out["odd"] = odd[0] if len(odd) == 1 else None
    out.update({"source": source, "hz": int(round(rate * over)), "over_s": round(over, 1),
                "ppm_s": round(rate / f, 3) if f > 0 else None})
    return out


# ----------------------------------------------------------------------------------------------------------------
# spur check
# ----------------------------------------------------------------------------------------------------------------
def spur_check(facts) -> dict:
    """Could the receiver or nearby equipment make this line? Own clock (per RTL-SDR dongle with a clear line, each
    with its own ppm), common equipment clocks (steady plain carriers only, with the odds of a chance match),
    a harmonic of a carrier measured in an earlier check, the receiver's zero-frequency spike, and whether the
    dongles read one and the same frequency. Negative results are results: they are returned too."""
    return _spur(_view(facts))


def _spur(v) -> dict:
    heard = v["heard"]
    f = v["carrier"] or v["asked"] or 0.0
    oc = {"result": "not_tested", "multiple": None, "nominal_mhz": None, "offset_khz": None, "tight": [], "tested": [],
          "apart": False}
    tests = [(d["name"], own_clock(d["line_mhz"], d["ppm"])) for d in heard if d["rtl"] and d["line_mhz"]]
    if tests:
        tight = [n for n, t in tests if t["result"] == "match"]
        # a line every dongle makes itself sits on every dongle's own prediction; one dongle on its prediction
        # while others miss theirs is a carrier that happens to lie there
        own = bool(tight) and (len(tight) >= 2 or len(tight) == len(tests))
        near = bool(tight) or any(t["result"] == "near" for _, t in tests)
        # A transmitter exactly on the multiple reads on a dongle's own line only when that dongle's correction is
        # next to nothing. Two dongles with different corrections that each read it on their OWN line rule a single
        # transmitter out ('apart'); dongles with the same correction, or one dongle, cannot.
        nominal = tests[0][1]["nominal_mhz"] or REF_CLOCK_MHZ
        ppms = [d["ppm"] for d in heard if d["name"] in tight]
        oc["apart"] = len(ppms) >= 2 and \
            max(ppms) - min(ppms) >= 2.0 * (OWN_TIGHT_PPM + OWN_TIGHT_HZ / (nominal * 1e6) * 1e6)
        oc.update({"result": "match" if own else "near" if near else "no",
                   "multiple": tests[0][1]["multiple"], "nominal_mhz": tests[0][1]["nominal_mhz"],
                   "offset_khz": round(_median([t["offset_khz"] for _, t in tests]), 3),
                   "tight": tight, "tested": [n for n, _ in tests],
                   "tolerance_hz": max(t["tolerance_hz"] for _, t in tests)})
    lines = [d["line_mhz"] for d in heard if d["line_mhz"]]
    plain = v["pattern"] == "steady" and v["modkind"] in _PLAIN and bool(lines)
    equip = _equipment_clock(f) if plain and oc["result"] in ("no", "not_tested") else None

    hist = v["history"]
    known = list(v["carriers"]) if isinstance(v["carriers"], (list, tuple)) else []
    known += [r.get("carrier_mhz") for r in _dicts(hist.get("recent")) + _dicts(hist.get("saved"))]
    earlier = sorted({round(c, 6) for c in (_mhz(x.get("mhz")) if isinstance(x, dict) else _mhz(x) for x in known)
                      if c})
    harmonic = _harmonic_of(f, earlier) if lines else None

    centre = v["asked"] + v["tune_offset_hz"] / 1e6 if v["asked"] and v["tune_offset_hz"] is not None else None
    spike = [d["name"] for d in heard if d["line_mhz"] and centre is not None
             and abs(d["line_mhz"] - centre) * 1e6 <= DC_SPIKE_HZ]

    tol = max(SAME_MIN_HZ, SAME_PPM * f)
    same, spread, rough = "unknown", None, False
    if len(heard) == 1:
        same = "one"
    elif len(heard) >= 2:
        if len(lines) < len(heard):           # a dongle without a clear line: the look's coarser carrier, loosely
            lines, tol, rough = [d["carrier"] for d in heard if d["carrier"]], SAME_LOOSE_HZ, True
        if len(lines) >= 2 and len(lines) == len(heard):
            spread = int(round((max(lines) - min(lines)) * 1e6))
            same = "yes" if spread <= tol else "no"
    return {"own_clock": oc, "equipment_clock": equip, "equipment_tested": plain, "harmonic_of": harmonic,
            "dc_spike": spike, "same_signal": same, "same_rough": rough and same in ("yes", "no"),
            "spread_hz": spread, "tolerance_hz": int(round(tol))}


def _check_lines(v, sp):
    """The spur check in words (at most 4 lines, negative results included) and what it could not tell."""
    lines, cant = [], []
    oc, n = sp["own_clock"], v["n_listen"]
    own = "the dongle's" if n <= 1 else "the dongles'"
    mult = f"{oc['multiple']} x {fmt_mhz(REF_CLOCK_MHZ)} MHz" if oc.get("multiple") else ""
    all_tight = bool(oc["tight"]) and len(oc["tight"]) == len(v["heard"])
    if oc["result"] == "match":
        rest = [x for x in oc["tested"] if x not in oc["tight"]]
        tol = f"{oc.get('tolerance_hz', 0) / 1000.0:.1f} kHz"
        if not rest:
            who = _who(oc["tight"], len(oc["tight"]), False)
            lines.append(f"On {own} own clock line: {who} within {tol} of where "
                         f"{'its' if len(oc['tight']) == 1 else 'their'} own {mult} line reads.")
        else:
            one = len(rest) == 1
            lines.append(f"On the own clock line of {_and(oc['tight'])}, {mult}. {_and(rest)} "
                         f"read{'s' if one else ''} it away from {'its' if one else 'their'} own.")
    elif oc["result"] == "near" and oc["tight"]:
        lines.append(f"Right next to {own} own clock: {_off(oc['offset_khz'])} from {mult}. Only {oc['tight'][0]} "
                     f"reads it where its own line would be; a line the dongles make themselves would sit there on "
                     f"each of them.")
    elif oc["result"] == "near":
        lines.append(f"Right next to {own} own clock: {_off(oc['offset_khz'])} from {mult}, but not where "
                     + (f"{oc['tested'][0]}'s own line reads." if len(oc["tested"]) == 1 and n > 1 else
                        "its own line reads." if len(oc["tested"]) == 1 else "their own lines read."))
    elif oc["result"] == "no":
        lines.append(f"Not {own} own clock: {_dist(oc['offset_khz'])} from {mult}.")
    elif v["pattern"] == "steady" and any(d["rtl"] for d in v["heard"]):
        cant.append("whether it is the receivers' own clock: no single clear line to test")
    if sp["dc_spike"]:
        lines.append(f"{_and(sp['dc_spike'])} found it exactly on the look's own tuning centre, where a receiver shows "
                     f"a spike of its own. Decode it again at {fmt_mhz(v['act_mhz'], 3, True)} MHz: a real signal "
                     f"stays, the spike does not.")
    eq = sp["equipment_clock"]
    if eq:
        lines.append(f"On a multiple of a common equipment clock: {eq['multiple']} x {fmt_mhz(eq['clock_mhz'], 3)} "
                     f"MHz. About 1 unrelated carrier in {eq['one_in']} matches one by chance, so this is a pattern, "
                     f"not a finding.")
    elif sp["equipment_tested"] and oc["result"] in ("no", "not_tested"):
        lines.append(f"No match with {len(EQUIP_CLOCKS_MHZ)} common equipment clocks.")
    hm = sp["harmonic_of"]
    if hm:
        lines.append(f"{hm['times']} x {fmt_mhz(hm['mhz'])} MHz, a carrier measured here in an earlier check, lands on "
                     f"this frequency: it may be that signal's harmonic. A pattern, not a finding.")
    if all_tight and len(v["heard"]) >= 2 and oc.get("apart"):
        lines.append("Each dongle makes this line itself, so hearing it on all of them is expected.")
    elif all_tight and len(v["heard"]) >= 2:
        lines.append("Their frequency corrections are too alike to rule out a transmitter exactly there: each "
                     "dongle would read that on its own line too.")
    elif sp["same_signal"] == "yes" and sp["same_rough"]:
        noline = [d["name"] for d in v["heard"] if not d["line_mhz"]]
        why = (f"{_and(noline)} had no clear line to read" if 0 < len(noline) < len(v["heard"]) else
               "there was no single clear line to read")
        lines.append(f"{_who(v['heard_names'], n)} found it within {_tenth(sp['spread_hz'])} of each other. A rough "
                     f"comparison: {why}.")
    elif sp["same_signal"] == "yes":
        them = "both of them" if len(v["heard"]) == 2 else "all of them"
        lines.append(f"{_who(v['heard_names'], n)} read the same frequency within {_tenth(sp['spread_hz'])}: "
                     f"one signal reaches {them}. Whether through the antennas or the wiring is not known from this.")
    elif sp["same_signal"] == "no":
        lines.append(f"The dongles read different frequencies, {_dist(sp['spread_hz'] / 1000.0)} apart: they may not "
                     f"be hearing the same signal.")
    elif sp["same_signal"] == "one" and n >= 2:
        lines.append(f"Only {v['heard_names'][0]} heard it, so its frequency could not be compared with another "
                     f"dongle's.")
    elif sp["same_signal"] == "unknown" and len(v["heard"]) >= 2:
        cant.append("whether the dongles heard the same signal: no clear line to compare")
    if len(lines) > 4:                      # the plain "no match" goes first when there is too much to say
        lines = [x for x in lines if not x.startswith("No match with")]
    return lines[:4], cant


# ----------------------------------------------------------------------------------------------------------------
# history
# ----------------------------------------------------------------------------------------------------------------
def baseline_state(mean_db, max_db, near_hop=False, ppm_moved=False) -> str:
    """What a baseline says about one frequency, from its cells' mean and max above its floor (dB). 'present': the
    mean stands 6 dB or more above. 'part': only the max does (on for part of the recording). 'absent': nothing
    rose 3 dB. 'unclear': a weak rise, the cell of a hop centre (the receiver's own spike), or a baseline recorded
    with another frequency correction."""
    mean, top = _num(mean_db), _num(max_db)
    if near_hop or ppm_moved or mean is None or top is None:
        return "unclear"
    if mean >= BASELINE_CLEAR_DB:
        return "present"
    if top >= BASELINE_CLEAR_DB and mean < BASELINE_QUIET_DB:
        return "part"
    return "absent" if top < BASELINE_QUIET_DB else "unclear"


def history_summary(raw, f_mhz, kind, now, carrier_mhz=None) -> dict:
    """What is on record for a frequency, counted honestly. raw: what the engine gathered (saved checks within
    60 kHz, recent checks, baseline states). f_mhz: the frequency picked; carrier_mhz: the carrier measured now.
    A saved check is 'the same frequency' when its measured carrier is within 3 kHz + 10 ppm of this one, or,
    for rows without one, when its frequency is within max(6, 5 + f x 0.005) kHz of the carrier or of the frequency
    picked; up to 25 kHz is 'next to it' (counted apart). FM channels and sensor bands use 60 kHz for everything.
    status: 'regular' (3 or more days over 7 or more days, or in a baseline a week old plus 2 days), 'seen',
    'none' (nothing, in a list that goes back 3 days or more), 'young' (nothing, and too short a list to say)."""
    raw, f, c = _dict(raw), _mhz(f_mhz) or 0.0, _mhz(carrier_mhz)
    now = _num(now) or time.time()
    wide = kind in ("wide", "sensor")
    win = HIST_WIDE_KHZ if wide else round(max(6.0, 5.0 + f * 0.005), 1)

    def gap(row):
        """(kHz from here, the window that makes it 'the same') for a saved or recent row, or None."""
        rf, rc = _mhz(row.get("mhz")), _mhz(row.get("carrier_mhz"))
        if rc and c and not wide:
            return abs(rc - c) * 1000.0, 3.0 + c * 0.01
        there = rc or rf
        ours = [x for x in (c, f) if x]
        return (min(abs(there - x) for x in ours) * 1000.0, win) if there and ours else None

    same, next_to = [], 0
    for r in _dicts(raw.get("saved")):
        g, ts = gap(r), _num(r.get("ts"))
        if g is None or ts is None or not _day(ts):
            continue
        if g[0] <= g[1]:
            same.append(r)
        elif g[0] <= HIST_NEAR_KHZ:
            next_to += 1
    days = sorted({_day(r.get("ts")) for r in same})
    first, last = (days[0], days[-1]) if days else (None, None)
    recent = [r for r in _dicts(raw.get("recent")) for g in [gap(r)] if g and g[0] <= g[1] and _num(r.get("ts"))]
    lately = [r for r in recent if 0 <= now - _num(r["ts"]) <= HIST_RECENT_H * 3600.0]
    rows = [{"dongle": _name(b.get("dongle"), 24) or "A dongle", "date": _day(b.get("date")), "state": b.get("state")}
            for b in _dicts(raw.get("baselines")) if b.get("state") in ("present", "part", "absent", "unclear")][:6]
    old = [b for b in _dicts(raw.get("baselines")) if b.get("state") == "present" and _num(b.get("date"))
           and now - _num(b["date"]) >= HIST_REGULAR_SPAN * 86400.0]
    list_from = _num(raw.get("list_from")) if _day(raw.get("list_from")) else None
    span = _days_between(first, last) if days else 0
    if (len(days) >= HIST_REGULAR_DAYS and span >= HIST_REGULAR_SPAN) or (old and len(days) >= 2):
        status = "regular"
    elif same or any(b["state"] in ("present", "part") for b in rows) or any(r.get("heard") for r in recent):
        status = "seen"
    elif list_from and now - list_from >= HIST_LIST_DAYS * 86400.0:
        status = "none"
    else:
        status = "young"
    count = lambda rs, key: sum(1 for r in rs if r.get(key))
    watched = max([_int(_within(r.get("listened_s"), 0.0, 3600.0)) for r in lately if r.get("heard")] + [0])
    return {"status": status, "window_khz": win, "saved_checks": len(same), "days": len(days), "first": first,
            "last": last, "span_days": span, "next_to": next_to,
            "steady_each_time": bool(same) and all(_STEADY_HEAD in str(r.get("head") or "") for r in same),
            "identified": count(same, "confirmed"), "doubtful": count(same, "doubtful"),
            "list_from": _day(list_from), "list_trimmed": bool(raw.get("list_trimmed")),
            "unread": bool(raw.get("list_unread")),   # the saved list could not be read this time
            "recent": {"checks": len(lately), "heard": count(lately, "heard"), "hours": HIST_RECENT_H,
                       "watched_s": watched},     # the longest of them that heard it
            "baseline": {"covered": bool(raw.get("baseline_covered")) or bool(rows), "rows": rows}}


def _since(h, now) -> str:
    return ("at least since " if h["list_trimmed"] else "since ") + _day_words(h["first"], now)


def _baseline_words(h, now):
    """(the baselines in one sentence, the clause a confidence can rest on) or (None, None)."""
    by = {}
    for b in h["baseline"]["rows"]:
        by.setdefault(b["state"], []).append(b)

    def whose(state):
        """'Dongle 2's baseline of 3 Nov' / 'Dongle 1's and Dongle 3's baselines' (the date when they share one)."""
        bs = by[state]
        dates = {b["date"] for b in bs}
        return (f"{_poss(b['dongle'] for b in bs)} baseline{'s' if len(bs) > 1 else ''}"
                + (f" of {_day_words(bs[0]['date'], now)}" if len(dates) == 1 and bs[0]["date"] else ""))

    bits, rest = [], None
    if "present" in by:
        bits.append(f"{whose('present')} already had a signal at or next to this frequency")
        rest = f"in {whose('present')}"
    if "part" in by:
        bits.append(f"{whose('part')} had one for part of "
                    + ("their recordings" if len(by["part"]) > 1 else "its recording"))
    if "absent" in by:
        bits.append(f"{whose('absent')} had none while {'they were' if len(by['absent']) > 1 else 'it was'} recorded")
    if "unclear" in by:
        bits.append(f"{whose('unclear')} {'are' if len(by['unclear']) > 1 else 'is'} not clear here")
    return (_cap("; ".join(bits)) + ".", rest) if bits else (None, None)


def _history_words(v, h, cat):
    """(the card's one history line, its pill, the detail lines)."""
    now, kind, raw = v["now"], v["kind"], v["history"]
    f = v["carrier"] or v["asked"] or 0.0
    n, days = h["saved_checks"], h["days"]
    line = tag = None
    quiet = cat in ("nothing", "doubtful", "decoder_failed")      # nothing was established here: no "no record" pill
    unread = bool(h.get("unread")) and not n
    if unread and cat != "device" and kind not in ("wide", "adsb"):
        line = "The saved list could not be read this time."
    elif cat == "device":
        total = len(v["devices_ok"])
        if v["new_devices"]:
            tag = "NEW DEVICE"
            line = "This device was not in the Devices list before." if total <= 1 else \
                f"{min(v['new_devices'], total)} of these {total} devices were not in the Devices list before."
        elif v["known_devices"] >= max(1, total):
            tag, line = "KNOWN DEVICE", "A known device here: heard on 3 or more days, or marked known."
        else:
            line = "In the Devices list from earlier, not yet counted as known."
    elif cat in ("station", "aircraft") or kind == "adsb":
        pass
    elif kind == "wide" and cat != "fm_offplan":
        was, when = _name(_dict(raw.get("fm_name")).get("name"), 16), _day(_dict(raw.get("fm_name")).get("date"))
        if was:
            line = f'Named "{was}" when its name was last read' + (f", on {_day_words(when, now)}." if when else ".")
    elif kind == "sensor":
        if days >= HIST_REGULAR_DAYS:
            line = f"A busy frequency: saved here on {days} days."
        elif n:
            line = f"Saved here on {_plural(days, 'day')} before."
        elif h["status"] == "none" and not quiet:
            line = f"No earlier record here (list from {_day_words(h['list_from'], now)})."
    elif n:
        tag = "REGULAR HERE" if h["status"] == "regular" else "SEEN BEFORE"
        line = (f"Saved on 1 day, {_day_words(h['first'], now)} ({_plural(n, 'check')})." if days == 1 else
                f"Saved on {days} days {_since(h, now)} ({_plural(n, 'check')}).")
    elif h["status"] == "seen":
        tag = "SEEN BEFORE"
        had = [b for b in h["baseline"]["rows"] if b["state"] in ("present", "part")]
        line = (f"Not saved before, but in {_poss(b['dongle'] for b in had[:2])} baseline." if had else
                "Heard in an earlier check, though nothing is saved from it.")
    elif h["status"] == "none" and not quiet:
        tag, line = "NO EARLIER RECORD", f"No earlier record here (list from {_day_words(h['list_from'], now)})."
    elif not quiet:
        line = (f"No earlier record here (list from {_day_words(h['list_from'], now)})." if h["list_from"] else
                "No history kept yet: the saved list is empty.")

    more = []
    was, when = _name(_dict(raw.get("fm_name")).get("name")), _day(_dict(raw.get("fm_name")).get("date"))
    if kind == "wide" and was:
        more.append(f'Its name "{was}" was read here before' + (f", last on {_day_words(when, now)}." if when else "."))
    if kind != "adsb" and (kind != "wide" or n or cat == "fm_offplan"):
        back = ("The saved list could not be read this time." if unread else
                f"The saved list goes back to {_day_words(h['list_from'], now)}." if h["list_from"] else
                "The saved list is empty.") + (" Older entries were removed from it." if h["list_trimmed"] else "")
        if n:
            last, steady = _day_words(h["last"], now), h["steady_each_time"]
            more.append((f"1 saved check within {h['window_khz']:g} kHz, on {last}."
                         + (" A steady carrier then." if steady else "") if n == 1 else
                         f"{n} saved checks within {h['window_khz']:g} kHz, the last on {last}."
                         + (" Each time a steady carrier." if steady else "")) + f" {back}")
        else:
            more.append(back if unread else f"No saved check within {h['window_khz']:g} kHz. {back}")
        words, _ = _baseline_words(h, now)
        if words:
            more.append(words)
        elif not h["baseline"]["covered"] and kind != "wide":
            more.append(f"No baseline covers {fmt_mhz(f, 2)} MHz, so there is no normal day to compare with.")
        r = h["recent"]
        if r["checks"]:
            heard = ("heard every time" if r["heard"] == r["checks"] else "not heard in any of them" if not r["heard"]
                     else f"heard in {r['heard']} of them")
            more.append(f"Checked {_plural(r['checks'], 'time')} in the last {r['hours']} hours: {heard}.")
        here = _dict(raw.get("devices_here"))
        names = [x for x in (_name(x, 24) for x in (here.get("names") if isinstance(here.get("names"), (list, tuple))
                                                    else [])[:2]) if x]
        if names:
            extra = max(0, _int(here.get("more")))
            more.append(f"Devices decoded here before: {_and(names + ([_plural(extra, 'other')] if extra else []))}.")
        cap = _dict(raw.get("last_capture"))
        if _day(cap.get("ts")):
            mins = _int(cap.get("minutes"))
            more.append(f"A capture here on {_day_words(_day(cap['ts']), now)}"
                        + (f" ran {mins} min" if mins > 0 else " ran")
                        + f": {_CAPTURE_ENDS.get(str(cap.get('outcome')), 'it ended without a result')}.")
        if h["next_to"]:
            more.append(f"{_plural(h['next_to'], 'more saved check')} within {HIST_NEAR_KHZ:g} kHz: a neighbouring "
                        f"frequency, not counted here.")
    return (_cut(line, 70) if line else None), tag, [_cut(x, 300) for x in more[:4]]


# ----------------------------------------------------------------------------------------------------------------
# what it did, how it was modulated
# ----------------------------------------------------------------------------------------------------------------
def _drift_line(v):
    """The frequency's movement as one line, or (None, why it could not be told)."""
    dr, n = v["drift"], v["drift"]["dongles"]
    if not n:
        c = v["clearest"]
        return None, "whether its frequency moved: " + _why(c["line"].get("why") if c else None)
    over = _secs(dr["over_s"])
    names = dr["names"]
    alike = (("the same on both dongles" if n == 2 else f"the same on all {n} dongles")
             if n >= 2 and set(names) == set(v["heard_names"]) else
             f"on {n} dongles alike" if n > 3 else f"on {_and(names)} alike")
    keyed = (" Keyed transmitters often slide as they switch on, so this does not say what kind it is."
             if v["pattern"] in ("keyed", "bursts") else "")
    wander = dr.get("scatter_hz") or 0
    if dr["source"] in ("none", "unknown") and wander >= max(WANDER_HZ, WANDER_PPM * (v["carrier"] or 0.0)):
        # no steady direction, but the readings do not sit still either: not a crystal's behaviour
        return (f"Its frequency wandered by {_moved(wander)} from look to look, with no steady direction, "
                f"{alike if n >= 2 else 'on ' + names[0]}."), None
    if dr["source"] == "none":
        line = f"Held its frequency: moved less than {_tenth(dr['hz'])} in {over}, {alike}."
        if dr["over_s"] >= CRYSTAL_MIN_S and dr["hz"] <= CRYSTAL_PPM * (v["carrier"] or 0.0):
            line += " A crystal-controlled source: that fits a licensed transmitter and an equipment clock equally."
        return line, None
    if dr["source"] == "signal" and n >= 2:
        if (dr["ppm_s"] or 0.0) < DRIFT_ONE_PPM_S:            # two dongles switched on together warm up together
            return (f"Its frequency moved {_moved(dr['hz'])} in {over}, {alike}. That little can also be the dongles "
                    f"warming up together."), None
        return (f"Its frequency moved {_moved(dr['hz'])} in {over}, {alike}, so the movement is the signal's own."
                + keyed), None
    if dr["source"] == "signal":
        return (f"Its frequency moved {_moved(dr['hz'])} in {over} on {names[0]}: more than a dongle's own clock "
                f"normally moves." + keyed), None
    if dr["source"] == "dongle":
        odd = f", {dr['odd']}'s above all" if dr["odd"] else ""
        return (f"The dongles' readings moved apart by {_moved(dr['hz'])} in {over}: that is their own clocks{odd}. "
                f"The signal moved less than that."), None
    return (f"Held within {_tenth(dr['hz'])} over {over} on {names[0]}. One dongle only, so its own clock is part "
            f"of this."), None


def _behaviour(v):
    """What the signal did, in at most 4 lines (on and off, the watch, the frequency, width and level), and what
    could not be told."""
    lines, cant = [], []
    kind, c, look = v["kind"], v["clearest"], f"the {v['look_txt']} look"
    w, n_listen = v["watch"], v["n_listen"]
    if not v["heard"]:
        return lines, cant
    if kind == "adsb":
        return ["Not measured: the ADS-B decoder reports messages, not the signal's shape."], cant
    key = c["key"] if c else {}
    pat = v["look_pattern"]
    runs = max(1, _int(key.get("runs"), 1))
    on_s = (_within(key.get("on_pct"), 0.0, 100.0) or 0.0) / 100.0 * v["look_s"]
    late = [d["name"] for d in v["heard"] if not d["first"]]
    if late:
        rest = "" if len(late) == len(v["heard"]) else " The others had it from the start."
        lines.append(f"{_and(late)} found it in a later look, not in the first {v['look_txt']} look.{rest}")
    if kind == "wide" and not c:
        if w and w["names"] and not late:   # the line after this one starts "then looked"
            lines.append(f"Heard in the first look{v['where_sp']}.")
    elif pat == "steady" and c and not c["first"]:
        lines.append(f"On for the whole of the {v['look_txt']} look that found it.")
    elif pat == "steady":
        ons = [d["name"] for d in v["heard"] if d["key"].get("state") == "on_whole" and d["first"]]
        lines.append(f"On for the whole {v['look_txt']} look {_where(ons, n_listen)}." if ons else
                     f"Steady during {look}{v['where_sp']}.")
    elif pat == "keyed":
        every = f", about every {_secs(key['period_s'])}" if _within(key.get("period_s"), 0.01, 60.0) else ""
        line = f"Switched on and off: on {_plural(runs, 'time')} in {look}, about {_secs(on_s / runs)} each{every}."
        if key.get("open_start") or key.get("open_end"):
            ends = "at the start and the end" if key.get("open_start") and key.get("open_end") else \
                "at the start" if key.get("open_start") else "at the end"
            line += f" It was on {ends} of the look, so it may stay on longer than that."
        lines.append(line)
    elif pat == "bursts":
        longest = (_within(key.get("longest_on_s"), 0.0, 60.0) or 0.0) * 1000.0
        shortest = _within(key.get("shortest_on_ms"), 0.0, 60000.0)
        each = ("each under 30 ms" if longest < 30.0 else f"up to {longest:.0f} ms long" if shortest is None
                or shortest < 30.0 else f"each about {longest:.0f} ms" if longest - shortest < 10.0
                else f"{shortest:.0f} to {longest:.0f} ms long")
        every = f", about every {_secs(key['period_s'])}" if _within(key.get("period_s"), 0.01, 60.0) else ""
        lines.append(f"Short bursts: {runs} in {look}, {each}{every}." if c else f"Short bursts during {look}.")
    elif pat == "off":
        lines.append(f"Not on during {look} itself.")
    elif c:
        lines.append(f"On and off could not be told apart in {look}.")
        cant.append(f"whether it switched on and off: {_why(key.get('why'))}")

    r4, gone = v["rtl433"], max(0, v["listened_s"] - int(round(v["look_s"])))
    on = "on" if pat == "steady" else "heard"          # a keyed signal is heard in a look, not on all through it
    if w and w["names"]:
        some = f"{on} in {w['heard']} of {'them' if len(w['names']) == 1 else 'their ' + str(w['looks']) + ' looks'}"
        if w["per"] == 1:
            result = f"{on} again" if w["heard"] == w["looks"] else f"not {on} then" if not w["heard"] else some
            lines.append(f"{_and(w['names'])} then looked once more: {result}. The time between the looks was not "
                         f"watched.")
        else:
            result = f"{on} every time" if w["heard"] == w["looks"] else f"not {on} in any of them" \
                if not w["heard"] else some
            lines.append(f"{_and(w['names'])} then looked {_plural(w['per'], 'more time')} over {w['span_s']} s: "
                         f"{result}. The gaps between looks were not watched.")
        if w["weak_miss"] and not w["gone"]:
            cant.append(f"whether it went off: {_and(w['weak_miss'])} missed it in some looks, but "
                        f"hear{'s' if len(w['weak_miss']) == 1 else ''} it only weakly")
    elif w:
        result = f"{on} every time" if w["heard"] == w["looks"] else f"{on} in {w['heard']} of them"
        lines.append(f"Looked {w['looks']} times over {w['span_s']} s: {result}.")
    elif r4 and _int(r4.get("seconds")) > 0:
        b = _int(r4.get("bursts"))
        lines.append(f"rtl_433 then counted {_plural(b, 'burst')} in {_int(r4['seconds'])} s." if b else
                     f"rtl_433 then heard no bursts in {_int(r4['seconds'])} s.")
    elif gone > 0 and kind in ("audio", "other", "sensor"):
        lines.append(f"Only {look} was watched: the other {gone} s went to the decoders.")

    if kind != "wide" and pat != "off":
        line, why = _drift_line(v)
        if line:
            lines.append(line)
        elif c and pat in ("steady", "keyed"):
            cant.append(why)
    if c:
        width, level = _dict(c["prof"].get("width")), _dict(c["prof"].get("level"))
        hz, rng, bits = _within(width.get("hz"), 0.0, 1e7), _within(level.get("range_db"), 0.0, 200.0), []
        if hz is not None and width.get("limit") == "under":
            bits.append("Very narrow: under 0.1 kHz wide.")
        elif hz is not None and width.get("limit") == "at_least" and c.get("folded"):
            bits.append(f"At least {_dist(hz / 1000.0)} wide: one wide signal, or several side by side that this "
                        f"look cannot separate. Decode each peak on the plot on its own.")
        elif hz is not None and width.get("limit") == "at_least":
            bits.append(f"At least {_dist(hz / 1000.0)} wide: wider than the look's filter.")
        elif hz is not None:
            bits.append(f"About {_dist(hz / 1000.0)} wide.")
        if rng is not None:
            when = " while on" if pat in ("keyed", "bursts") else ""
            bits.append(f"Level steady{when}." if rng <= 1.0 else f"Level varied by {rng:.0f} dB{when}." if rng >= 3.0
                        else f"Level within {rng:.0f} dB{when}.")
        if bits:
            lines.append(" ".join(bits))
    return [_cut(x, 220) for x in lines[:4]], cant


def _modulation(v):
    """(the modulation in one sentence or None, what could not be told)."""
    m, kind, src = v["mod"], v["modkind"], v["mod_dongle"]
    look = f"the {v['look_txt']} look"
    rate, levels = _int(_within(m.get("rate_baud"), 1.0, 1e6)), _int(_within(m.get("levels"), 2.0, 16.0))
    tone = _within(m.get("tone_hz"), 20.0, 3000.0)
    bits, cant = [], []
    fm = v["fm"]
    if not v["heard"]:
        return None, cant
    if v["kind"] == "wide" and fm and fm.get("wideband", True):
        pilot, rds = bool(fm.get("pilot")), bool(fm.get("rds"))
        bits.append("Wideband FM " + ("with a stereo pilot and RDS." if pilot and rds else
                                       "with a stereo pilot, no RDS." if pilot else "with RDS, no stereo pilot." if rds
                                       else "in mono, with no RDS."))
    elif kind == "none" and v["look_pattern"] in ("keyed", "bursts"):
        bits.append(f"No modulation while it was on in {look}: a bare carrier, switched on and off.")
    elif kind == "none":
        bits.append(f"No modulation during {look}: a bare carrier.")
    elif kind == "lines":
        bits.append(f"2 or 3 separate lines during {look}, no other modulation seen.")
    elif kind == "tone" and tone and _mains_of(tone):
        bits.append(f"FM with a steady {tone:.1f} Hz tone on {src}. That is a standard access tone of two-way radio "
                    f"and also a multiple of the {_mains_of(tone):.0f} Hz mains: hum on a transmitter's power "
                    f"supply reads the same.")
    elif kind == "tone" and tone:
        bits.append(f"FM with a {tone:.1f} Hz sub-audible tone on {src}, a standard access tone of two-way radio. "
                    f"Voice is never listened to or recorded.")
    elif kind in ("fsk2", "fsk4", "data") and rate:
        levels = levels or {"fsk2": 2, "fsk4": 4}.get(kind, 0)
        users = _RATE_USERS.get((rate, levels))
        bits.append((f"{levels}-level FSK at {rate} symbols a second on {src}." if levels else
                     f"Data at {rate} symbols a second on {src}; the number of levels was not clear.")
                    + (f" That is the rate and level count of {users}: a pattern, not an identification." if users
                       else " A pattern, not an identification."))
    elif kind == "am":
        bits.append(f"AM, measured on {src}. Nothing is listened to or recorded.")
    elif kind == "fm_voice":
        bits.append(f"FM, voice-like, measured on {src}. Nothing is listened to or recorded.")
    elif kind == "untested":
        cant.append("its modulation: " + _why(m.get("why")))
    elif kind and _within(m.get("hum_hz"), 40.0, 70.0):
        bits.append(f"Modulated with a steady hum on multiples of {m['hum_hz']:.0f} Hz on {src}, as ripple from the "
                    f"mains on a transmitter's power supply gives. Not speech.")
    elif kind:
        bits.append("Modulated; the kind was not determined.")
    a = v["audio"]
    if a and _int(a.get("seconds")) > 0:
        who = _cap(_name(a.get("decoders"), 60) or "the audio decoders")
        on = f" on {_name(a.get('dongle'), 24)}" if _name(a.get("dongle"), 24) else ""
        frames, atone = _int(a.get("frames")), _within(a.get("tone_hz"), 20.0, 3000.0)
        if frames:
            bits.append(f"{who} read {_plural(frames, 'frame')} in {_int(a['seconds'])} s{on}.")
        elif atone:
            bits.append(f"{who} found a {atone:.1f} Hz tone in {_int(a['seconds'])} s{on}.")
        else:
            bits.append(f"{who} read nothing in {_int(a['seconds'])} s{on}.")
    fp = _flat(v["rtl433"].get("fingerprint") or "", 260).rstrip(".")
    if fp and not v["devices_ok"]:
        hint = _flat(v["rtl433"].get("pattern_hint") or "", 130).rstrip(".")
        bits.append(f"rtl_433 read its bursts but none of its decoders knows them: {fp}."
                    + ("" if "not an identification" in fp else
                       f" This pattern is used by {hint}. A pattern match, not an identification." if hint else
                       " A pattern, not an identification."))
    return (_cut(" ".join(bits), 400) if bits else None), cant


# ----------------------------------------------------------------------------------------------------------------
# headline and confidence: the ladder, first match wins
# ----------------------------------------------------------------------------------------------------------------
def _cat(category, headline, **more) -> dict:
    """One rung: p is the power of the test that decided it (A: an unrelated signal passes 1 time in 1000 or
    less; B: 1 in 50 or less; C: no test, a class named from the band or the waveform alone)."""
    out = {"category": category, "headline": headline, "p": "C", "ceiling": None, "two": False, "confirmed": False,
           "rests": [], "against": [], "rider": "", "sure": ""}
    out.update(more)
    return out


def _on_channel(v) -> str:
    ch = _mhz(v["fm"].get("channel_mhz")) or v["asked"]
    return f" on {fmt_mhz(ch, 1, True)} MHz" if ch else ""


def _judge(v, sp, h) -> dict:
    """What the check found, as the first rung that fits: the decoders' own results, then a doubtful decode, the
    receivers' own clock, nothing heard, FM, a tone, data, the airband, drift, bursts, keying, a steady carrier."""
    kind, pat, mk, w = v["kind"], v["pattern"], v["modkind"], v["where_sp"]
    m, dr, fm, look = v["mod"], v["drift"], v["fm"], f"the {v['look_txt']} look"
    devs, confirmed = v["devices_ok"], v["evidence"] == "confirmed_decode"
    heard, n = bool(v["heard"]), v["n_listen"]
    total = v["listened_s"]
    if v["decoder_failed"]:
        return _cat("decoder_failed", "The decoder could not run, so this check proves nothing.",
                    sure="Nothing was decoded because the decoder did not run, not because nothing was there.")
    if devs and confirmed:
        d = devs[0]
        name, label = _name(d.get("name")) or _name(d.get("model")) or "A device", _name(d.get("label"), 16)
        packets = sum(max(0, _int(x.get("packets"))) for x in devs)
        basis = (f"{_plural(packets, 'packet')} matched rtl_433's decoder for this device" if len(devs) == 1 else
                 f"{_plural(packets, 'packet')} matched rtl_433's decoders for these devices")
        return _cat("device", name + (f", ID {label}." if label else ".")
                    + (f" And {len(devs) - 1} more." if len(devs) > 1 else ""), confirmed=True, rests=[basis],
                    rider="One more decode in this check looks like noise and is not counted." if v["doubts"] else "")
    if confirmed and _int(v["adsb"].get("aircraft")) > 0:
        return _cat("aircraft", f"Aircraft transponders: {_int(v['adsb']['aircraft'])} aircraft.", confirmed=True,
                    rests=[f"{_plural(_int(v['adsb'].get('verified')), 'checksum-verified message')}"])
    name = _name(fm.get("name"))
    if name and confirmed:
        if fm.get("scrolls"):
            return _cat("station", f'FM radio station{_on_channel(v)}. Its RDS text reads "{name}".', confirmed=True,
                        rests=["its own RDS data, read with checksums"],
                        rider="It scrolls text through its name field, so this is its text, not a fixed name.")
        return _cat("station", f'FM radio station "{name}".', confirmed=True,
                    rests=["its own RDS name, read with checksums"])
    if confirmed:
        text = re.sub(r"^Identified:\s*", "", v["verdict"])
        n_fr = _int(v["audio"].get("frames"))
        who = _name(v["audio"].get("decoders"), 60) or "the decoders"
        return _cat("frame", _sentence(_unbracket(_cut(_cap(text), 119)).rstrip(".")) if text else
                    "A decoder identified it.",
                    confirmed=True, rests=[f"{_plural(n_fr, 'validated frame')} from {who}" if n_fr else
                                           "the decoder's own result, in Details"])
    if v["doubts"]:
        bad = v["devices_bad"][0] if v["devices_bad"] else {}
        name = _name(bad.get("name")) or _name(bad.get("model"))
        return _cat("doubtful", "Probably noise, not a real device.",
                    sure=(f'A decoder named "{name}", but ' if name else "A decoder printed a result, but ")
                    + "; ".join(v["doubts"][:3]) + ".")

    oc = sp["own_clock"] if sp else {"result": "not_tested", "tight": [], "tested": []}
    mult = f"{oc.get('multiple')} x {fmt_mhz(REF_CLOCK_MHZ)} MHz"
    plain = mk in _PLAIN
    if oc["result"] == "match" and pat == "steady" and plain:
        tight, rest = oc["tight"], [x for x in oc["tested"] if x not in oc["tight"]]
        tol = f"{oc.get('tolerance_hz', 0) / 1000.0:.1f} kHz"
        rests = [f"{_who(tight, len(v['heard']), False)} within {tol} of {'its' if len(tight) == 1 else 'their'} "
                 f"own {mult} line"]
        if dr["source"] == "none":
            rests.append("no drift")
        if mk == "none":
            rests.append("no modulation")
        carrier = fmt_mhz(oc.get("nominal_mhz"), 3, True)
        against = [f"{_and(rest)} read{'s' if len(rest) == 1 else ''} it away from "
                   f"{'its' if len(rest) == 1 else 'their'} own clock line"] if rest else []
        if oc.get("apart"):                  # (see _spur: only dongles with different corrections can tell)
            return _cat("own_clock", f"The receivers' own clock, not a transmitter: {mult}.", p="A", two=True,
                        rests=rests + ["their frequency corrections differ, so one transmitter could not sit on "
                                       "each one's line"][:max(0, 3 - len(rests))],
                        tight=tight, against=against,
                        rider=f"A transmitter exactly on {carrier} MHz would hide behind this line.")
        same = ("one dongle cannot tell" if len(tight) == 1 else
                "the dongles' corrections are too alike to tell")
        return _cat("own_clock", f"Most likely the receivers' own clock: {mult}." if n != 1 else
                    f"Most likely the receiver's own clock: {mult}.", p="A", two=False, ceiling="medium",
                    rests=rests, tight=tight, against=against,
                    rider=f"A transmitter exactly on {carrier} MHz would read the same: {same}.")
    if oc["result"] == "near" and pat == "steady" and plain:
        f = v["carrier"] or v["asked"] or REF_CLOCK_MHZ
        one_in = max(1, int(REF_CLOCK_MHZ / (2.0 * OWN_NEAR_PPM * 1e-6 * f)))
        return _cat("near_clock",
                    "Steady carrier right next to the dongles' own clock line. Possibly a receiver's clock.",
                    p="B", ceiling="low",
                    rests=[f"{_off(oc['offset_khz'])} from {mult}, where about 1 unrelated carrier in {one_in} sits"],
                    against=[f"only {oc['tight'][0]} reads it where its own line would be" if oc["tight"] else
                             "not where these dongles' own lines read"],
                    rider="Another receiver's clock or a transmitter fits too.")
    if v["evidence"] == "none" or (not heard and v["evidence"] != "signal_present"):
        nobody = "" if not n else f" on {v['listening'][0]['name']}" if n == 1 else \
            " on either dongle" if n == 2 else f" on any of the {n} dongles"
        took = f" in {total} s" if total else ""
        if kind == "adsb":
            return _cat("nothing", f"No aircraft heard{took}{nobody}.",
                        sure="Only checksum-verified messages count. Aircraft out of range look the same as none.")
        return _cat("nothing", f"Nothing heard{took}{nobody}.",
                    sure=(f"This covers these {total} s only: " if total else "This covers this check only: ")
                    + "a transmitter that was silent just now looks the same.")
    if kind == "wide" and fm and fm.get("wideband", True) and fm.get("on_channel") is False:
        f = v["carrier"] or v["asked"]
        at = f" at {fmt_mhz(f, 3, True)} MHz" if f else ""
        return _cat("fm_offplan", f"FM transmitter{at}, between the station channels. Not a normal station.",
                    p="B", rests=["wideband FM measured", "its carrier is off the station channels"],
                    rider="Could be a car transmitter, a wireless microphone, a baby monitor or a bug.")
    if kind == "wide" and fm and fm.get("wideband", True) and fm.get("on_channel") is True:
        marks = [x for x, ok in (("a stereo pilot", fm.get("pilot")), ("RDS", fm.get("rds"))) if ok]
        if marks:
            return _cat("station_noname", f"FM radio station{_on_channel(v)}. Its name was not read.", p="B",
                        rests=[_and(marks) + " measured", "on a station channel"])
        return _cat("station_noname", f"Wideband FM{_on_channel(v)}, a station channel. No station name was read.",
                    sure="No stereo pilot, RDS or name was read, so what sends it is not known.")
    look_tone = _within(m.get("tone_hz"), 20.0, 3000.0) if mk == "tone" else None
    audio_tone = _within(v["audio"].get("tone_hz"), 20.0, 3000.0)
    if (look_tone or audio_tone) and kind not in ("wide", "adsb", "air") and _mains_of(look_tone or audio_tone):
        tone = look_tone or audio_tone
        return _cat("modulated", f"FM carrier with a steady {tone:.1f} Hz tone{w}: an access tone or mains hum.",
                    sure=f"{tone:.1f} Hz is a standard access tone of two-way radio and also a multiple of the "
                    f"{_mains_of(tone):.0f} Hz mains, so hum on a transmitter's power supply reads the same.")
    if (look_tone or audio_tone) and kind not in ("wide", "adsb", "air"):
        tone = look_tone or audio_tone
        head = f"Two-way radio voice channel: FM with a {tone:.1f} Hz access tone."
        if look_tone:
            rests = [f"a standard sub-audible tone measured on {v['mod_dongle']}"]
            if audio_tone and abs(audio_tone - look_tone) < 2.0:
                rests.append("the audio decoders found the same tone")
            return _cat("voice_channel", head, p="B", rests=rests,
                        rider="ARIS never listens to or records the voice.")
        return _cat("voice_channel", head, p="B", ceiling="low",
                    rests=[f"the audio decoders found the tone in {_int(v['audio'].get('seconds'))} s"],
                    against=[f"{look} did not show it"] if mk not in (None, "untested") else [],
                    rider="ARIS never listens to or records the voice.")
    if mk in ("fsk2", "fsk4", "data") and _within(m.get("rate_baud"), 1.0, 1e6):
        rate = _int(m["rate_baud"])
        levels = _int(_within(m.get("levels"), 2.0, 16.0)) or {"fsk2": 2, "fsk4": 4}.get(mk, 0)
        if levels:
            return _cat("data_pattern", f"Data, not decoded: {levels}-level FSK, {rate} symbols a second.", p="B",
                        rests=[f"{levels} clear frequency levels",
                               f"a steady symbol rate, measured on {v['mod_dongle']}"],
                        rider="A symbol rate is a pattern: it does not name the system.")
        return _cat("data_pattern", f"Data, not decoded: {rate} symbols a second, levels not clear.", p="B",
                    ceiling="low", rests=[f"a steady symbol rate, measured on {v['mod_dongle']}"],
                    rider="A symbol rate is a pattern: it does not name the system.")
    if kind == "air":
        wt = v["watch"]
        seen = ""
        if wt and wt["looks"] >= 2:         # with a record per dongle, each dongle's first look counts too
            first = len(wt["names"])
            on, of = wt["heard"] + wt.get("first", first), wt["looks"] + first
            seen = f", on in all {wt['per'] + (1 if first else 0)} looks" if on == of else f", on in {on} of {of} looks"
        if mk == "am":
            return _cat("air", f"Aviation-band transmission: AM{seen}.", p="B", am=True,
                        rests=[f"AM measured on {v['mod_dongle']}", "a frequency in the aircraft voice band"])
        if mk == "none" and v["q"] in ("A", "B"):
            return _cat("air", f"Carrier in the aviation band{seen}, no voice during "
                        f"{'the looks' if seen else look}.", am=False,
                        sure="A bare carrier was measured. What sends it is not known.")
        return _cat("air", f"A transmission in the aviation band{seen}. Whether it is AM voice could not be told.",
                    am=False, sure="AM was not measured: " + _why(m.get("why") if mk == "untested" else None) + ".")
    if pat == "steady" and plain and dr["source"] == "signal" and (dr["ppm_s"] or 0.0) >= FREE_RUN_PPM_S:
        two = dr["dongles"] >= 2
        how = ("the same on both dongles" if dr["dongles"] == 2 else f"the same on {dr['dongles']} dongles") if two \
            else f"on {dr['names'][0]}"
        return _cat("free_running", f"Carrier that drifts{w}: a free-running oscillator, not crystal-controlled.", p="B",
                    ceiling=None if two else "low",
                    rests=[f"it drifted {_moved(dr['hz'])} in {_secs(dr['over_s'])}, {how}"],
                    against=[] if two else ["one dongle only, so its own clock is part of this"],
                    rider="Could be a cheap transmitter, a simple bug or a power supply.")
    r4 = v["rtl433"]
    if pat == "bursts" or (pat in ("off", "unclear") and (r4.get("fingerprint") or _int(r4.get("bursts")) > 0)):
        if kind == "sensor" and _SPECIFIC_HINT in str(r4.get("pattern_hint") or ""):
            return _cat("bursts", f"Short bursts{w}, not decoded: an unknown sensor or remote.", p="B", ceiling="low",
                        rests=["the pulse shape and packet length of simple fixed-code remotes, a pattern match, not "
                               "an identification", "a band that sensors and remotes use"])
        return _cat("bursts", f"Short bursts{w}, not decoded.",
                    sure=("Sensors and remotes send bursts like these in this band. Which device this is, is not known."
                          if kind == "sensor" else "Source not known: no decoder here knows these bursts."))
    what = "Carrier" if plain or mk == "lines" else "Signal"
    if pat == "keyed":
        wt = v["watch"]
        if v["look_pattern"] == "steady" and wt:
            first = len(wt["names"])                  # each watcher's first look counts too, heard or not
            return _cat("keyed", f"{what} that comes and goes{w}: on in {wt['heard'] + wt.get('first', first)} of "
                        f"{wt['looks'] + first} looks.",
                        sure="Source not known: the looks show that it comes and goes, not what sends it.")
        pct = _num(v["clearest"]["key"].get("on_pct")) if v["clearest"] else None
        return _cat("keyed", f"{what} that switches on and off{w}"
                    + (f": on {pct:.0f} % of {look}." if pct is not None else "."),
                    sure="Source not known: a short look shows that it switches, not what sends it.")
    # "a fixed source" rests on the same kind of signal each time: other saved checks may be other signals
    if pat == "steady" and mk == "none" and h and h["status"] == "regular" and h["steady_each_time"] \
            and kind not in ("wide", "sensor"):
        long = "here for weeks" if h["span_days"] >= 14 else "here for over a week"
        rests = [f"saved on {h['days']} days {_since(h, v['now'])}, a steady carrier each time"]
        base = _baseline_words(h, v["now"])[1]
        if base:
            rests.append(base)
        return _cat("steady_fixed", f"Steady carrier{w}: a fixed source, {long}.", p="B", rests=rests,
                    rider="What sends it is still not known.")
    if pat == "steady" and mk == "none":
        return _cat("steady_unknown", f"Steady carrier{w}. What sends it is not known.",
                    sure="Source not known: an idle transmitter and an equipment clock look the same in a short look.")
    if pat == "steady" and mk not in _PLAIN:
        how = {"fm_voice": "Continuous FM signal, voice-like,", "am": "Continuous AM signal",
               "lines": "Continuous signal of 2 or 3 separate lines", "tone": "Continuous FM signal with a tone"}.get(
                   mk, "Continuous modulated signal")
        return _cat("modulated", f"{how}{w}. Not identified.",
                    sure="Source not known. What it carries is never listened to or recorded.")
    return _cat("heard", f"A signal was heard{w}, but nothing decoded it.",
                sure=("It was on for the whole look, but too weak or unclear to measure more." if pat == "steady" else
                      "Too little was measured to say what it is."))


def _confidence(j, q) -> str:
    """The look's quality times the power of the deciding test. High: both A and two dongles matching their own
    prediction. Medium: both B or better. Low: a named class on a poor look. No confidence word without a test.
    Each thing that speaks against it takes one step off."""
    if j["confirmed"]:
        return "confirmed"
    if j["category"] == "doubtful":
        return "check"
    if j["p"] == "C":
        return "unknown"
    c = "high" if j["p"] == "A" and q == "A" and j["two"] else "medium" if q in ("A", "B") else "low"
    if j["ceiling"] and _STEPS.index(c) > _STEPS.index(j["ceiling"]):
        c = j["ceiling"]
    return _STEPS[max(1, _STEPS.index(c) - len(j["against"]))]


def _sure(j, conf) -> str:
    """The one line under the headline: how sure, what it rests on, what speaks against it."""
    if conf in ("unknown", "check"):
        return _cut(j["sure"], 240)
    rests = "; ".join(j["rests"][:3])
    base = CONFIDENCE_TEXT[conf] + (f": {rests}." if conf == "confirmed" else f". Rests on: {rests}.")
    for extra in ((f"Against: {'; '.join(j['against'][:2])}." if j["against"] else ""), j["rider"]):
        if extra and len(base) + 1 + len(extra) <= 240:
            base += " " + extra
    return _cut(base, 240)


def _tag(v, j, conf) -> str:
    if j["category"] == "decoder_failed":
        return "DECODER FAILED"
    if v["doubts"]:
        return "CHECK FIRST"
    if conf == "confirmed":
        return "IDENTIFIED"
    if j["category"] == "own_clock":
        return "OWN CLOCK"
    if j["category"] in ("station_noname", "voice_channel", "air"):
        return "HEARD"
    return "NOTHING HEARD" if j["category"] == "nothing" else "NOT IDENTIFIED"


# ----------------------------------------------------------------------------------------------------------------
# the one next step, first match wins
# ----------------------------------------------------------------------------------------------------------------
_LONGER = ((30, "Decode for 30 s"), (60, "Decode for 1 min"), (120, "Decode for 2 min"))


def _step(v, j, h) -> dict:
    cat, kind, pat, listen = j["category"], v["kind"], v["pattern"], v["listen_s"]
    f = fmt_mhz(v["act_mhz"])
    best = v["strongest"] or "one dongle"

    def step(action, label, reason, say=None, seconds=None, dongle=None):
        return {"action": action, "label": _cut(label, 26), "reason": _cut(reason, 130),
                "say": _cut(say or reason, 300), "mhz": v["act_mhz"], "seconds": seconds, "minutes": None,
                "dongle": dongle}

    def decode(seconds, label, reason, say):
        return step("decode", label, reason, say.replace("{cmd}", f'"investigate {f} for {seconds} seconds"'), seconds)

    def capture(label, reason, say):
        return step("capture", label, reason, say.replace("{cmd}", f'"capture {f}"'))

    if cat == "decoder_failed":
        return step("check", "Check the system", "The decoder could not run. The system check says why.",
                    "Run /check to see why the decoder could not run.")
    run = v["capture_running"]
    if run:
        who = _name(run.get("dongle"), 24) or "a dongle"
        return step("activity", "See the capture", f"A capture is already listening here on {who}.",
                    f"A capture is already listening here on {who}: the Activity tab shows what it has heard.",
                    dongle=run.get("dongle") and who)
    if not v["act_mhz"]:
        return step("none", "", "")
    if cat == "doubtful":
        secs = 120 if listen >= 60 else 60
        if any("single packet" in str(x) for x in v["doubts"]):
            return decode(secs, f"Decode again for {secs // 60} min", "One weak packet is not proof. A real device "
                          "decodes again.", "One weak packet is not proof: say {cmd}. A real device decodes again.")
        return decode(secs, f"Decode again for {secs // 60} min", "A decoder can read another signal as this device. "
                      "A real one decodes again.", "A decoder can read another signal as this device: say {cmd}. A "
                      "real one decodes again.")
    if cat in ("own_clock", "near_clock"):
        tight = (j.get("tight") or [best])
        best = best if best in tight else tight[0]
        return decode(10, "Decode again (10 s)", f"To be sure, unplug {best}'s antenna first. If the level stays, it "
                      f"is the receiver.", f"To be sure, unplug {best}'s antenna and say " + "{cmd}: if the level "
                      "stays, it is the receiver.")
    if cat == "device" and v["new_devices"]:
        return step("devices", "See it in Devices", "It is in the Devices list now.",
                    'It is in the Devices list now: say "list devices" to see it.')
    if cat in ("device", "frame", "aircraft", "station"):
        return step("none", "", "Nothing to do: identified.")
    if cat == "fm_offplan":
        if v["map_ready"]:
            return step("locate", "Find where it is", "Not a normal station. The Map shows roughly where it comes "
                        "from.", f'To find roughly where it comes from, say "where is {f} coming from".')
        if listen < 120:
            secs = 60 if listen < 60 else 120
            return decode(secs, f"Decode for {secs // 60} min", "Not a normal station. A longer look shows whether "
                          "it stays on.", "For a longer look, say {cmd}: it shows whether it stays on.")
        return step("none", "", "Not a normal station. Antennas placed apart on the Map would show roughly where it "
                    "comes from.")
    if cat == "station_noname":
        return capture("Wait for its name", "A capture waits in the background until the station sends its name.",
                       "To read its name, say {cmd}: it waits in the background until the station sends it.")
    if cat == "nothing":
        if kind == "adsb":
            return capture("Wait for aircraft", "A capture waits in the background for the next aircraft.",
                           "To wait for aircraft in the background, say {cmd}.")
        longer = next((x for x in _LONGER if x[0] > listen), None)
        if kind == "wide" and longer:
            return decode(longer[0], longer[1], "A longer look, in case it was silent just now.",
                          "To look again for longer, say {cmd}.")
        before = bool(h) and h["status"] in ("seen", "regular")
        return capture("Wait for a transmission", "It was heard here before: a capture listens in the background "
                       "until it transmits again." if before else "A capture listens in the background until "
                       "something transmits.", "To wait for the next transmission in the background, say {cmd}.")
    if cat == "voice_channel":
        return step("none", "", "A voice channel. ARIS never records or decodes voice.")
    if cat == "air" and j.get("am"):
        return step("none", "", "Aircraft voice is not decoded and never recorded.")
    if cat == "air" and pat != "steady":
        return capture("Wait for a transmission", "A capture waits in the background for the next transmission on "
                       "this channel.", "To wait for the next transmission in the background, say {cmd}.")
    packets = ("Capture until decoded", "The dongle that hears it best keeps listening until a packet decodes.",
               "To identify it, say {cmd}: the dongle that hears it best keeps listening until a packet decodes.")
    tones = ("Capture until decoded", "A capture waits for the next transmission and runs the pager and tone decoders "
             "on it.", "To catch the next transmission, say {cmd}: it runs the pager and tone decoders on it.")
    if cat in ("bursts", "keyed", "data_pattern", "heard") and pat in ("bursts", "keyed"):
        if kind in ("sensor", "other"):
            return capture(*packets)
        if kind == "audio":
            return capture(*tones)
    if cat == "steady_fixed":
        return step("none", "", "It is not new. Nothing to do unless it changes.")
    if pat == "steady" and cat in ("steady_unknown", "modulated", "free_running", "air", "heard"):
        status = h["status"] if h else "young"
        w = v["watch"]
        watched = _int(_dict(h.get("recent")).get("watched_s")) if h else 0
        if watched >= 120 and listen < 120:  # (it was watched, heard, for 2 min or more in the last hours)
            return step("none", "", f"It was already watched for {watched // 60} min in the last {HIST_RECENT_H} "
                        f"hours. The next Decode says whether it is still there.")
        if listen < 120 and (w or kind in ("air", "wide")) and status != "regular":
            moves = "keeps drifting" if cat == "free_running" else "drifts"
            return decode(120, "Watch it for 2 min", f"To see whether it ever switches off, {moves} or carries "
                          f"anything.", "To watch it for longer, say {cmd}.")
        if listen >= 120 and w and w["heard"] == w["looks"] and v["map_ready"] and status in ("none", "seen"):
            return step("locate", "Find where it is", f"It was on in every look over {v['listened_s'] or listen} s. "
                        f"The Map shows roughly where it comes from.",
                        f'To find roughly where it comes from, say "where is {f} coming from".')
        if not w and listen < 120:          # nobody could watch: a longer Decode would not see more of the carrier
            return step("none", "", f"Only the first look watched it: the dongle{'s' if len(v['heard']) > 1 else ''} "
                        f"that hear{'' if len(v['heard']) > 1 else 's'} it ran the decoders. The next Decode says "
                        f"whether it is still there.")
        return step("none", "", "A longer look adds nothing here. The next Decode says whether it is still there.")
    return capture(*(tones if kind == "audio" else packets))


# ----------------------------------------------------------------------------------------------------------------
# the report
# ----------------------------------------------------------------------------------------------------------------
def _strip(v):
    """The on / off record as a row of marks: the look in half-second steps or, with a watch, the whole check
    with a gap wherever nobody looked. Never finer than half a second: a finer record of a keyed carrier would be
    its message."""
    c, w = v["clearest"], v["watch"]
    mark = lambda x: x if x in (0, 0.5, 1) and not isinstance(x, bool) else None
    if w and w["names"] and v["listened_s"] > 0:
        total = float(v["listened_s"])
        slot = max(1.0, total / 60.0)
        # a look that heard it marks its slots "on" only when it was on from start to end, else "on for part"
        part = lambda on, whole: 0 if not on else 1 if whole else 0.5
        spans = [(0.0, v["look_s"], part(d["first"] and d["key"].get("state") != "off",
                                         d["key"].get("state") == "on_whole")) for d in v["heard"] if d["key"]]
        spans += [(t, t + v["look_s"], part(x.get("heard"), x.get("whole", True)))
                  for d in v["listening"] if d["heard"] and d["looks"]
                  for x in d["looks"] for t in [_within(x.get("t"), 0.0, 86400.0)] if t is not None]
        slots = []
        for i in range(min(60, max(1, int(math.ceil(total / slot))))):
            hits = [on for a, b, on in spans if a <= (i + 0.5) * slot < b]
            slots.append(None if not hits else 1 if min(hits) == 1 else 0 if max(hits) == 0 else 0.5)
        return {"slots": slots, "slot_s": round(slot, 2),
                "label": f"The {total:.0f} s of the check, start to end. Gaps were not watched."}
    raw, slot = (c["prof"].get("strip"), _num(c["prof"].get("strip_slot_s"))) if c else (None, None)
    if not isinstance(raw, (list, tuple)) or not raw or not slot or slot < 0.5:
        return None
    return {"slots": [mark(x) for x in raw[:60]], "slot_s": round(slot, 2),
            "label": f"The {v['look_txt']} look on {c['name']}, start to end."}


def _carrier_words(v):
    """(the frequency as the card shows it, a note when the carrier is not where the user picked)."""
    kind, fm, asked, c = v["kind"], v["fm"], v["asked"], v["carrier"]
    channel = _mhz(fm.get("channel_mhz"))
    if not (asked or c or channel):
        return "", None
    if kind == "adsb":
        return f"{fmt_mhz(asked or c)} MHz", None
    if kind == "wide" and fm.get("on_channel") is not False and (channel or not c):
        return f"{fmt_mhz(channel or asked, 1, True)} MHz", None
    text = f"{fmt_mhz(c or asked, 3, True)} MHz"
    if not c or not asked or v["off_khz"] < OFF_ASKED_KHZ:
        return text, None
    note = f"Found at {text}, {v['off_khz']:.1f} kHz {'below' if c < asked else 'above'} the frequency picked."
    tuned = _mhz(v["audio"].get("tuned_mhz"))
    if tuned and abs(tuned - c) * 1000.0 <= OFF_ASKED_KHZ and abs(tuned - asked) * 1000.0 >= OFF_ASKED_KHZ:
        note += " The decoders listened there."
    elif tuned and abs(tuned - c) * 1000.0 > DECODER_REACH_KHZ:
        note += f" The decoders listened at {fmt_mhz(tuned)} MHz, {_dist(abs(tuned - c) * 1000.0)} away."
    return text, note


def _build(facts) -> dict:
    v = _view(facts)
    kind, heard = v["kind"], bool(v["heard"])
    narrow = heard and kind not in ("wide", "adsb")
    sp = _spur(v) if narrow else None
    carrier = v["carrier"] if heard and kind != "adsb" else None
    took_part = {d["name"] for d in v["dongles"]}             # a baseline of a dongle that did not listen is not named
    v["history"] = dict(v["history"], baselines=[b for b in _dicts(v["history"].get("baselines"))
                                                 if _name(b.get("dongle") or "", 24) in took_part])
    h = history_summary(v["history"], v["asked"], kind, v["now"], carrier) if v["asked"] else None
    j = _judge(v, sp, h)
    cat = j["category"]
    if v["overloaded"] and j["p"] != "C" and len(j["against"]) < 2:
        j["against"].append(f"the look was overloaded on {_and(v['overloaded'][:2])}")
    if sp and sp["same_signal"] == "no" and j["p"] != "C" and cat != "own_clock" and len(j["against"]) < 2:
        j["against"].append("the dongles read different frequencies")
    conf = _confidence(j, v["q"])
    step = _step(v, j, h)

    behaviour, cant_did = _behaviour(v)
    modulation, cant_mod = _modulation(v)
    checks, cant_spur = _check_lines(v, sp) if sp else ([], [])
    line, pill, more = _history_words(v, h, cat) if h else (None, None, [])
    text, note = _carrier_words(v)

    cant = []
    if sp and sp["same_signal"] == "yes" and not sp["same_rough"] and cat != "own_clock":
        cant.append("whether it reaches the dongles through the antennas or the wiring")
    if narrow:
        cant.append(f"what it did between the looks and outside these {v['listened_s']} s" if v["watch"] else
                    f"what it did outside the {v['look_txt']} look")
    elif not heard and cat == "nothing":
        cant.append("whether anything transmits here at other times")
    cant += cant_mod + cant_did + cant_spur
    tuned = _mhz(v["audio"].get("tuned_mhz"))
    if narrow and tuned and v["carrier"] and abs(tuned - v["carrier"]) * 1000.0 > DECODER_REACH_KHZ:
        cant.append("whether the audio decoders would read it: they listened "
                    f"{_dist(abs(tuned - v['carrier']) * 1000.0)} away")
    if conf == "confirmed":                 # a decoder named it: nothing is left that needs telling
        cant = []

    c = v["clearest"]
    profile = None
    if heard and (c or narrow):
        best = c or next((d for d in v["heard"] if d["name"] == v["strongest"]), v["heard"][0])
        width, level = _dict(best["prof"].get("width")), _dict(best["prof"].get("level"))
        profile = {"dongle": best["name"], "pattern": v["pattern"], "scope": v["scope"],
                   "on_percent": _within(best["key"].get("on_pct"), 0.0, 100.0), "drift": v["drift"],
                   "width_hz": _within(width.get("hz"), 0.0, 1e7),
                   "level_range_db": _within(level.get("range_db"), 0.0, 200.0), "quality": v["q"], "strip": _strip(v)}
    m = v["mod"]
    mod = {"kind": v["modkind"], "tone_hz": _within(m.get("tone_hz"), 20.0, 3000.0),
           "rate_baud": _int(_within(m.get("rate_baud"), 1.0, 1e6)) or None,
           "levels": _int(_within(m.get("levels"), 2.0, 16.0)) or None} if v["modkind"] else None
    all_tight = bool(sp) and bool(sp["own_clock"]["tight"]) and len(sp["own_clock"]["tight"]) == len(v["heard"])
    if cat == "own_clock" and all_tight and sp["own_clock"].get("apart"):
        n = len(v["heard"])
        note_bars = f"Each dongle makes this line itself, so {'both' if n == 2 else 'all ' + str(n)} hear it."
    elif cat == "own_clock" and all_tight:
        n = len(v["heard"])
        note_bars = "A receiver's own clock line reads like this." if n == 1 else \
            f"Each dongle's own clock line reads like this, so {'both' if n == 2 else 'all ' + str(n)} would show it."
    else:
        note_bars = BARS_NOTE if heard and kind != "adsb" else ""
    return {"v": 1, "category": cat, "tag": _tag(v, j, conf), "headline": _cut(j["headline"], 120), "confidence": conf,
            "confidence_text": CONFIDENCE_TEXT[conf], "rests_on": [_cut(x, 160) for x in j["rests"][:3]],
            "against": [_cut(x, 160) for x in j["against"][:2]], "sure": _sure(j, conf),
            "could_not_tell": [_cut(x, 160) for x in cant[:4]],
            "carrier_mhz": round(carrier, 6) if carrier else None, "carrier_text": _cut(text, 40), "carrier_note": note,
            "behaviour": behaviour, "modulation": modulation, "checks": [_cut(x, 220) for x in checks],
            "history": line, "history_tag": pill, "history_more": more, "dongles_note": note_bars,
            "profile": profile, "mod": mod, "spur": sp, "freq_history": h, "next": step}


def _plain_report(facts) -> dict:
    """The least that can be said, for facts the rules could not read."""
    ev = _dict(facts).get("evidence")
    head = {"confirmed_decode": "A decoder identified it.", "none": "Nothing heard."}.get(
        ev, "A signal was heard, but nothing decoded it.")
    conf = "confirmed" if ev == "confirmed_decode" else "unknown"
    asked = _mhz(_dict(facts).get("asked_mhz"))
    return {"v": 1, "category": "frame" if conf == "confirmed" else "nothing" if ev == "none" else "heard",
            "tag": {"confirmed_decode": "IDENTIFIED", "none": "NOTHING HEARD"}.get(ev, "NOT IDENTIFIED"),
            "headline": head, "confidence": conf, "confidence_text": CONFIDENCE_TEXT[conf], "rests_on": [],
            "against": [], "sure": "", "could_not_tell": [], "carrier_mhz": None,
            "carrier_text": f"{fmt_mhz(asked, 3, True)} MHz" if asked else "", "carrier_note": None, "behaviour": [],
            "modulation": None, "checks": [], "history": None, "history_tag": None, "history_more": [],
            "dongles_note": "", "profile": None, "mod": None, "spur": None, "freq_history": None,
            "next": {"action": "none", "label": "", "reason": "", "say": "", "mhz": round(asked or 0.0, 4),
                     "seconds": None, "minutes": None, "dongle": None}}


def build(facts) -> dict:
    """The whole signal report for one investigation, from the facts the engine gathered (every key optional).
    Never raises: facts the rules cannot read give the least that can be said."""
    try:
        return _build(facts)
    except Exception:               # a report must never break an investigation
        return _plain_report(facts)


# ----------------------------------------------------------------------------------------------------------------
# the card and the chat
# ----------------------------------------------------------------------------------------------------------------
def bars(rows, strongest) -> list:
    """One bar per dongle from an investigation's rows: how far it stood above that dongle's own noise (the number
    the row's sentence gives), 'not heard', 'not used', or for ADS-B what was counted. [] when nobody heard it."""
    out = []
    for r in _dicts(rows):
        name = _name(r.get("dongle"), 24) or _name(r.get("serial"), 24) or "A dongle"
        bar = {"dongle": name, "serial": _clean(r.get("serial") or "", 40) or None, "state": "not_heard", "db": None,
               "pct": None, "best": False, "text": "not heard"}
        snr = _num(r.get("snr_db"))
        if snr is None or (r.get("heard_by_rtl433") and _num(r.get("rtl_433_snr_db")) is not None):
            snr = _num(r.get("rtl_433_snr_db"))               # only rtl_433 heard it: its level, not the quiet look's
        if r.get("error"):
            bar.update({"state": "not_used", "text": "not used"})
        elif _int(r.get("adsb_verified")) > 0:
            bar.update({"state": "count", "best": bool(strongest) and name == strongest,
                        "text": f"{_int(r.get('aircraft'))} aircraft, {_plural(_int(r['adsb_verified']), 'message')}"})
        elif r.get("heard"):
            bar.update({"state": "heard", "best": bool(strongest) and name == strongest, "text": "heard"})
            if snr is not None:
                db = round(min(max(snr, 0.0), 200.0), 1)
                bar.update({"db": db, "pct": max(4, int(round(100.0 * min(db, BAR_FULL_DB) / BAR_FULL_DB))),
                            "text": f"{db:.0f} dB" if db >= 1.0 else "at noise"})
        out.append(bar)
    return out if any(b["state"] in ("heard", "count") for b in out) else []


_NO_REPORT = {"tag": None, "tag_kind": None, "history_tag": None, "history_kind": None, "carrier": None,
              "headline": None, "sure": None, "confidence": None, "history_line": None, "step": None}


def _card_fields(report, rows, strongest, card=None) -> dict:
    b = bars(rows, strongest)
    level = any(x["pct"] is not None for x in b)
    r = _dict(report)
    if not r.get("headline"):
        return dict(_NO_REPORT, bars=b, bars_note=BARS_NOTE if level else "", details=[])
    lines = lambda key: [str(x) for x in (r.get(key) if isinstance(r.get(key), (list, tuple)) else []) if x]
    k = _dict(card)
    old = []
    if k.get("verdict"):
        old.append(_sentence(_clean(k["verdict"], 600).split(" Measured:")[0]))
    if k.get("band"):
        old.append(f"Band plan: {_sentence(_flat(k['band'], 200))}")
    if k.get("antenna_note"):
        old.append(_flat(k["antenna_note"], 400))
    if k:
        old.append("Saved to the Signals list." if k.get("saved") else _sentence(_flat(k.get("saved_note") or
                   "Not saved to the Signals list", 200)))
    sections = (("WHAT IT DID", lines("behaviour"), _dict(r.get("profile")).get("strip")),
                ("MODULATION", [r["modulation"]] if r.get("modulation") else [], None),
                ("SPUR CHECK", lines("checks"), None), ("HISTORY", lines("history_more"), None),
                ("COULD NOT TELL", [_sentence(_cap(x)) for x in lines("could_not_tell")], None),
                ("DECODERS", old, None))
    nxt = _dict(r.get("next"))
    return {"report_v": 1, "tag": r.get("tag"), "tag_kind": _TAG_KIND.get(_word(r.get("tag")), "signal_present"),
            "history_tag": r.get("history_tag"), "history_kind": _HISTORY_KIND.get(_word(r.get("history_tag"))),
            "carrier": {"mhz": r.get("carrier_mhz") or nxt.get("mhz"), "text": r.get("carrier_text") or "",
                        "note": r.get("carrier_note")},
            "headline": r["headline"], "sure": r.get("sure") or "", "confidence": r.get("confidence") or "unknown",
            "history_line": r.get("history"), "bars": b, "bars_note": (r.get("dongles_note") or "") if level else "",
            "step": {x: nxt.get(x) for x in ("action", "label", "reason", "mhz", "seconds", "minutes", "dongle")},
            "details": [{"title": t, "lines": ls, "strip": s if isinstance(s, dict) else None}
                        for t, ls, s in sections if ls]}


def card_fields(report, rows, strongest, card=None) -> dict:
    """The Decode card's new keys. report: the signal report, or None for a result without one (the bars are still
    built from the rows; no 'report_v', so the page draws the old card). card: the card's existing keys (verdict,
    band, antenna_note, saved, and 'saved_note' when it was not saved), for the DECODERS section of the details.
    Never raises."""
    try:
        return _card_fields(report, rows, strongest, card)
    except Exception:               # the old card must still be drawn
        return dict(_NO_REPORT, bars=[], bars_note="", details=[])


def text_lines(report) -> dict:
    """The report for the chat reply: 'short' (In short), 'did' (What it did), 'checks', 'history' and 'next' (the
    words to type). '' where there is nothing to say."""
    r = _dict(report)
    strs = lambda key: [str(x) for x in (r.get(key) if isinstance(r.get(key), (list, tuple)) else []) if x]
    more, nxt = strs("history_more"), _dict(r.get("next"))
    history = ([str(r["history"])] + more[1:3]) if r.get("history") else more[:2]
    return {"short": " ".join(str(x) for x in (r.get("headline"), r.get("sure")) if x),
            "did": " ".join(strs("behaviour") + ([str(r["modulation"])] if r.get("modulation") else [])),
            "checks": " ".join(strs("checks")), "history": " ".join(history),
            "next": str(nxt.get("say") or nxt.get("reason") or "")}


# ----------------------------------------------------------------------------------------------------------------
# the example cards: eleven checks as the engine would describe them, for tests and the page's fixtures
# ----------------------------------------------------------------------------------------------------------------
EXAMPLE_CASES = ("1a", "1b", "2", "3", "4", "5", "6", "7", "8", "9", "10")
EXAMPLE_NOW = time.mktime((2026, 11, 14, 12, 0, 0, 0, 0, -1))


def _ex_day(month, day, hour=10) -> float:
    return time.mktime((2026, month, day, hour, 0, 0, 0, 0, -1))


def _ex_profile(snr, quality, line_mhz, state="on_whole", kind="none", **more) -> dict:
    """A 3 s look's profile as arislib.siglook gives it."""
    on = state == "on_whole"
    p = {"seconds": 3.0, "snr_on_db": snr, "quality": quality, "overloaded": False,
         "key": {"state": state, "on_pct": 100.0 if on else 50.0, "runs": 1 if on else 3,
                 "longest_on_s": 3.0 if on else 0.5, "longest_off_s": 0.0 if on else 0.5, "shortest_on_ms": None,
                 "period_s": None, "open_start": on, "open_end": on, "why": None},
         "line": {"ok": True, "hz": 12.5, "share": 0.97, "slope_hz_s": 0.4, "span_s": 2.75, "scatter_hz": 1.2,
                  "pieces": 12, "others_hz": [], "why": None},
         "width": {"hz": 80.0, "share": 99, "limit": "under", "why": None}, "level": {"range_db": 0.6, "why": None},
         "mod": {"kind": kind, "tone_hz": None, "rate_baud": None, "levels": None, "am_index": 0.03,
                 "dev_rms_hz": 12.0, "stretch_s": 3.0, "why": None},
         "strip": [1, 1, 1, 1, 1, 1] if on else [1, 0, 1, 0, 1, 0], "strip_slot_s": 0.5, "line_mhz": line_mhz}
    for key, val in more.items():
        if isinstance(val, dict) and isinstance(p.get(key), dict):
            p[key].update(val)
        else:
            p[key] = val
    return p


def _ex_burst(snr, line_mhz, runs) -> dict:
    """The look's profile of 25 ms bursts every half second: too short for a line, a width or a modulation."""
    short = {"why": "too_short"}
    return _ex_profile(snr, "C", line_mhz, "bursts", "untested", strip=[0.5] * 6,
                       key={"on_pct": round(runs * 2.5 / 3.0, 1), "runs": runs, "longest_on_s": 0.025,
                            "longest_off_s": 0.48, "shortest_on_ms": 25.0, "period_s": 0.5, "open_start": False,
                            "open_end": False},
                       line=dict(short, ok=False, slope_hz_s=None, span_s=None), width=dict(short, hz=None, limit=None),
                       level=dict(short, range_db=None), mod=dict(short, am_index=None, dev_rms_hz=None))


def _ex_looks(line_mhz, n=7, heard=None, snr=9.0, step_hz=0.0) -> list:
    """A watcher's later looks, 4.1 s apart from 4.1 s on."""
    return [{"t": round(4.1 + 4.1 * i, 1), "heard": heard is None or i in heard,
             "line_mhz": round(line_mhz + step_hz * (4.1 + 4.1 * i) * 1e-6, 6), "snr_db": snr} for i in range(n)]


def _ex_dongle(n, serial, heard, snr, ppm=0.0, role="look", **more) -> dict:
    d = {"dongle": f"Dongle {n}", "serial": serial, "heard": heard, "snr_db": snr, "error": None, "ppm": ppm,
         "rtl": True, "role": role, "continuous": None, "profile": None, "looks": []}
    d.update(more)
    return d


def _ex_carrier(asked, lines, ppm=(0.0, 8.0, 2.0)) -> list:
    """Three dongles on a steady carrier in an audio band: Dongle 2 hears it best and runs the decoders, the
    other two watch."""
    return [_ex_dongle(1, "00001001", True, 8.0, ppm[0], "watch", continuous=True, looks=_ex_looks(lines[0], snr=8.2),
                       profile=_ex_profile(8.0, "B", lines[0])),
            _ex_dongle(2, "00003003", True, 16.0, ppm[1], "decoder", continuous=True,
                       profile=_ex_profile(16.2, "A", lines[1])),
            _ex_dongle(3, "00004004", True, 10.0, ppm[2], "watch", continuous=True, looks=_ex_looks(lines[2], snr=10.1),
                       profile=_ex_profile(10.0, "B", lines[2]))]


def example_facts(case) -> dict:
    """The facts of one of the eleven example checks ('1a', '1b', '2' to '10'): what the engine would hand to
    build() for it. The rows a card's bars are drawn from are the facts' own 'dongles'."""
    case = str(case)
    land = "VHF High-Band Land Mobile / Public Safety / Pager / SCADA"
    steady = ("Not identified: no decoder recognised it. Measured: Steady unmodulated carrier (an idle transmitter "
              "carrier or a clock signal from equipment)")
    head = "• ARIS verdict (heuristic): " + steady
    base = {"asked_mhz": 172.8364, "band_kind": "audio", "band_label": land, "evidence": "signal_present",
            "verdict": steady, "doubtful": [], "decoder_failed": False, "listen_s": 30, "listened_s": 33, "timed": True,
            "strongest": "Dongle 2", "tune_offset_hz": -40000.0, "devices": [], "rtl433": None,
            "audio": {"frames": 0, "tone_hz": None, "seconds": 30, "decoders": "the pager, APRS and tone decoders",
                      "dongle": "Dongle 2", "tuned_mhz": 172.8364},
            "adsb": None, "fm": None, "air": None, "map_ready": False, "capture_running": None, "new_devices": 0,
            "known_devices": 0, "now": EXAMPLE_NOW,
            "history": {"saved": [], "list_from": _ex_day(10, 5), "list_trimmed": False, "recent": [], "baselines": [],
                        "baseline_covered": False, "last_capture": None, "fm_name": None, "devices_here": None}}
    quiet = [_ex_dongle(1, "00001001", False, -1.2), _ex_dongle(2, "00003003", False, 0.4),
             _ex_dongle(3, "00004004", False, -0.8)]
    if case == "1a":
        base["dongles"] = _ex_carrier(172.8364, (172.836350, 172.836412, 172.836500))
    elif case == "1b":
        base["dongles"] = _ex_carrier(172.8364, (172.800010, 172.801390, 172.800352))
        base["audio"]["tuned_mhz"] = 172.8014
        base["history"]["saved"] = [{"ts": _ex_day(11, d), "mhz": 172.8364, "carrier_mhz": None, "head": head,
                                     "confirmed": False, "doubtful": False} for d in (9, 10, 12, 13)]
    elif case == "2":
        base["dongles"] = _ex_carrier(172.8364, (172.836350, 172.836412, 172.836500))
        days = ((10, 24), (10, 26), (10, 29), (11, 1), (11, 3), (11, 5), (11, 8), (11, 11), (11, 13))
        base["history"].update({
            "saved": [{"ts": _ex_day(m, d, 9 + i), "mhz": 172.8364, "head": head, "confirmed": False, "doubtful": False,
                       "carrier_mhz": 172.83641 if (m, d) >= (11, 8) else None}
                      for m, d in days for i in range(2 if (m, d) in ((11, 3), (11, 11), (11, 13)) else 1)],
            "recent": [{"ts": EXAMPLE_NOW - 3600.0 * x, "mhz": 172.8364, "carrier_mhz": 172.83641, "heard": True}
                       for x in (1, 4)],
            "baselines": [{"dongle": "Dongle 1", "serial": "00001001", "date": _ex_day(11, 3), "state": "unclear"},
                          {"dongle": "Dongle 2", "serial": "00003003", "date": _ex_day(11, 3), "state": "present"},
                          {"dongle": "Dongle 3", "serial": "00004004", "date": _ex_day(11, 3), "state": "unclear"}],
            "baseline_covered": True})
    elif case == "3":
        base.update({
            "asked_mhz": 433.92, "band_kind": "sensor", "band_label": "433.92 MHz ISM Band", "listen_s": 10,
            "listened_s": 13, "evidence": "confirmed_decode", "strongest": "Dongle 1", "audio": None, "new_devices": 1,
            "verdict": "Identified: Generic fixed-code remote, a remote control (6 packets; ID #3fa2c1)",
            "devices": [{"model": "Generic-Remote", "label": "#3fa2c1", "packets": 6, "doubts": [],
                         "name": "Generic fixed-code remote"}],
            "rtl433": {"bursts": 6, "packets": 6, "fingerprint": None, "seconds": 10, "pattern_hint": None},
            "dongles": [_ex_dongle(1, "00001001", True, 14.0, 0.0, "decoder", continuous=False, rtl_433_packets=6,
                                   profile=_ex_burst(14.0, 433.9187, 5)),
                        _ex_dongle(2, "00003003", False, 0.3, 8.0, "decoder"),
                        _ex_dongle(3, "00004004", True, 9.0, 2.0, "decoder", continuous=False, rtl_433_packets=4,
                                   profile=_ex_burst(9.0, 433.9189, 5))]})
        base["history"].update({"saved": [{"ts": _ex_day(11, d), "mhz": 433.92, "carrier_mhz": None,
                                           "head": "• ARIS verdict (confirmed decode): Identified: Nexus-TH",
                                           "confirmed": True, "doubtful": False} for d in (2, 6, 9, 12)],
                                "devices_here": {"names": ["Nexus-TH weather station"], "more": 3}})
    elif case == "4":
        doubts = ["28.790 MHz is outside the bands these devices use", "it is a single packet, only 4 dB above noise",
                  "it is a single packet with ID 0"]
        base.update({
            "asked_mhz": 28.79, "band_kind": "other", "band_label": "10 m Amateur Band", "strongest": "Dongle 1",
            "audio": None, "doubtful": sorted(doubts),
            "verdict": "Doubtful: rtl_433 decoded something, but it is probably noise and not that device",
            "devices": [{"model": "ERT-SCM", "label": "0", "packets": 1, "doubts": doubts, "name": "ERT-SCM"}],
            "rtl433": {"bursts": 1, "packets": 1, "fingerprint": None, "seconds": 30, "pattern_hint": None},
            "dongles": [_ex_dongle(1, "00001001", True, 4.0, 0.0, "decoder", heard_by_rtl433=True, rtl_433_snr_db=4.0,
                                   rtl_433_packets=1), _ex_dongle(2, "00003003", False, -0.5, 8.0, "decoder"),
                        _ex_dongle(3, "00004004", False, 0.2, 2.0, "decoder")]})
    elif case == "5":
        base.update({"asked_mhz": 162.55, "evidence": "none", "listen_s": 10, "listened_s": 13, "strongest": None,
                     "band_label": "VHF Marine / Weather Radio", "dongles": quiet,
                     "verdict": "Nothing transmitted during this 13 s check on any dongle"})
        base["audio"].update({"seconds": 10, "tuned_mhz": 162.55, "dongle": "Dongle 1"})
    elif case == "6":
        base.update({
            "asked_mhz": 96.9, "band_kind": "wide", "band_label": "FM Broadcast Radio", "evidence": "confirmed_decode",
            "listen_s": 10, "listened_s": 10, "strongest": "Dongle 1", "audio": None, "tune_offset_hz": -250000.0,
            "verdict": 'Identified: FM broadcast station on 96.9 MHz, RDS station name "HITS FM" (PI code 0x5969). '
                       "It sends its own name, so this is a public radio station",
            "fm": {"name": "HITS FM", "channel_mhz": 96.9, "on_channel": True, "pilot": True, "rds": True,
                   "scrolls": False},
            "air": {"looks": 3, "looks_heard": 3},
            "dongles": [_ex_dongle(1, "00001001", True, 28.0), _ex_dongle(2, "00003003", True, 10.0, 8.0),
                        _ex_dongle(3, "00004004", False, 1.5, 2.0)]})
        base["history"]["fm_name"] = {"name": "HITS FM", "date": _ex_day(11, 12)}
    elif case == "7":
        base.update({
            "asked_mhz": 433.92, "band_kind": "sensor", "band_label": "433.92 MHz ISM Band", "listen_s": 10,
            "listened_s": 13, "strongest": "Dongle 1", "audio": None,
            "verdict": "Not identified: rtl_433 demodulated its packets, but none of its decoders knows this device. "
                       "Its signal: OOK pulse-width modulation, 300/900 us pulses, 28-bit packets sent 4 times",
            "rtl433": {"bursts": 9, "packets": 0, "fingerprint": "OOK, pulses 300 / 900 us, 28-bit packets",
                       "seconds": 10, "pattern_hint": None},
            "dongles": [_ex_dongle(1, "00001001", True, 15.0, 0.0, "decoder", continuous=False,
                                   profile=_ex_burst(15.0, 433.9211, 6)),
                        _ex_dongle(2, "00003003", True, 6.0, 8.0, "decoder", continuous=False,
                                   profile=_ex_burst(6.0, 433.9209, 6)),
                        _ex_dongle(3, "00004004", True, 11.0, 2.0, "decoder", continuous=False,
                                   profile=_ex_burst(11.0, 433.9213, 6))]})
        days = (18, 20, 22, 24, 26, 28, 30, 1, 3, 5, 7, 9, 11, 13)             # from 18 Oct to 13 Nov
        base["history"].update({"saved": [{"ts": _ex_day(10 if d > 14 else 11, d), "mhz": 433.92, "carrier_mhz": None,
                                           "head": "• ARIS verdict (heuristic): Not identified", "confirmed": False,
                                           "doubtful": False} for d in days],
                                "devices_here": {"names": ["Nexus-TH weather station"], "more": 3}})
    elif case == "8":
        keyed = lambda snr, q, f: _ex_profile(snr, q, f, "keyed", "none", line={"slope_hz_s": 300.0, "span_s": 2.5,
                                              "pieces": 6}, key={"open_start": True, "open_end": False})
        base.update({
            "asked_mhz": 151.25, "strongest": "Dongle 1",
            "verdict": "Not identified: no decoder recognised it. Measured: Intermittent carrier (keyed on and off)",
            "dongles": [_ex_dongle(1, "00001001", True, 14.0, 0.0, "decoder", continuous=False,
                                   profile=keyed(14.2, "B", 151.251020)),
                        _ex_dongle(2, "00003003", True, 11.0, 8.0, "watch", continuous=False,
                                   profile=keyed(11.3, "B", 151.251090),
                                   looks=_ex_looks(151.2511, heard=(0, 2, 3, 5), snr=11.0, step_hz=300.0))]})
        base["audio"].update({"tuned_mhz": 151.25, "dongle": "Dongle 1"})
    elif case == "9":
        am = lambda snr, q, f: _ex_profile(snr, q, f, "on_whole", "am", mod={"am_index": 0.6}, level={"range_db": 4.0},
                                           width={"hz": 5600.0, "limit": None})
        base.update({
            "asked_mhz": 118.1, "band_kind": "air", "band_label": "Aeronautical Band", "listen_s": 30, "listened_s": 30,
            "strongest": "Dongle 1", "audio": None, "air": {"looks": 8, "looks_heard": 3},
            "verdict": "Aviation-band transmission (AM voice or data; content not recorded)",
            "dongles": [_ex_dongle(1, "00001001", True, 15.0, 0.0, continuous=True, profile=am(15.0, "A", 118.100040)),
                        _ex_dongle(2, "00003003", True, 9.0, 8.0, continuous=True, profile=am(9.2, "B", 118.100110)),
                        _ex_dongle(3, "00004004", False, 0.6, 2.0)]})
    elif case == "10":
        base.update({
            "asked_mhz": 1090.0, "band_kind": "adsb", "band_label": "ADS-B Aircraft Transponders", "listen_s": 10,
            "listened_s": 10, "evidence": "confirmed_decode", "strongest": "Dongle 1", "audio": None,
            "tune_offset_hz": None, "adsb": {"aircraft": 2, "verified": 9},
            "verdict": "Identified: aircraft ADS-B transponders, 2 aircraft (9 checksum-verified position or identity "
                       "messages, public broadcast)",
            "dongles": [_ex_dongle(1, "00001001", True, None, 0.0, "decoder", adsb_messages=14, adsb_verified=9,
                                   aircraft=2),
                        _ex_dongle(2, "00003003", False, None, 8.0, "decoder", adsb_messages=3)]})
    else:
        raise ValueError(f"No example case {case!r}: use one of {', '.join(EXAMPLE_CASES)}.")
    return base
