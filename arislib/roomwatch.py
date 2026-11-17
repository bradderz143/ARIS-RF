"""Room watch: a long, quiet-room watch of the bands where small transmitters are found. Pure Python, no engine
imports.

  WATCH_BANDS   the bands, with the receivers' own limits applied by the caller (`chunks`, `coverage`)
  chunks()      a band cut into sweeps of at most 200 MHz, each with a step that keeps the bin count sensible
  Tracker       what the rounds of an audit found, kept for the whole watch: a new carrier, one that grew, one that
                came back, with the wording of each alert; `state()` / `from_state()` carry it across a restart
  report()      the watch as a timeline per day, from the log rows

An alert is "worth a look", never a finding: a carrier that was not there when the reference was recorded is what a
transmitter that was not there before looks like, and also what a neighbour's new router or a phone in a pocket looks
like. A band no receiver here tunes is listed as not covered, never as clear."""
import math
import time

# lo, hi (MHz), label. Where a small transmitter is commonly found; the caller keeps what its receivers tune.
WATCH_BANDS = (
    (25.0, 88.0, "VHF low and wireless microphones"),
    (88.0, 108.0, "FM broadcast band"),
    (108.0, 174.0, "Air and VHF two-way radio"),
    (174.0, 230.0, "VHF TV and DAB"),
    (230.0, 300.0, "VHF and UHF air"),
    (300.0, 330.0, "315 MHz devices"),
    (330.0, 380.0, "UHF air and two-way radio"),
    (380.0, 470.0, "UHF two-way radio and 433 MHz devices"),
    (470.0, 700.0, "UHF TV band and wireless microphones"),
    (700.0, 960.0, "Mobile phones, trunked radio and sub-GHz devices"),
    (960.0, 1200.0, "Air navigation and radar"),
    (1200.0, 1300.0, "1.2 GHz video links"),
    (1300.0, 1710.0, "Satellite navigation and L band"),
    (1710.0, 2170.0, "Mobile phones"),
    (2170.0, 2400.0, "2.3 GHz"),
    (2400.0, 2500.0, "2.4 GHz band"),
    (2500.0, 2700.0, "2.6 GHz mobile"),
    (2700.0, 3300.0, "Radar and 3 GHz"),
    (3300.0, 3800.0, "3.5 GHz mobile"),
    (3800.0, 5150.0, "C band"),
    (5150.0, 5925.0, "5 GHz band"),
    (5925.0, 6000.0, "6 GHz band"),
)

MAX_CHUNK_MHZ = 200.0
MAX_CHUNK_BINS = 20000          # a chunk's step is raised until a sweep has at most this many bins (a 200 MHz
                                # sweep keeps the 10 kHz asked: wider bins dilute a narrow carrier by many dB)
NEW_ALERT_DB = 10.0             # a new carrier alerts from this far over the reference, unless the watch was
MIN_NEW_DB = 4.0                # started with its own level (`Tracker(new_db=)`, never under this) ...
STRONGER_DB = 10.0              # ... a known one when one receiver reads it this much higher than its highest
                                # reading up to the first alert
REALERT_DB = 6.0                # ... and again only after growing this much more
BACK_AFTER_S = 2 * 3600         # seen, gone this long, then there again: a note
REPEAT_AFTER_S = 6 * 3600       # a steady alert is repeated as a note after this long if it is still there
GRID_KHZ = 25.0                 # carriers are kept on this grid ...
MAX_NEAR_CELLS = 8              # ... and one that moved at most this many cells (a wide signal's peak wanders) is the same
STRONG_DB = 25.0                # a new steady carrier this far over the reference alerts at once ...
CONFIRM_GAP_S = 45.0            # ... a weaker one when it is there again at least this much later (a passing
CONFIRM_HOLD_S = 20 * 60        # two-way radio call is over by then) within this long
WIDE_KHZ = 300.0                # a rise on a frequency that was occupied alerts for signals up to this wide, or
WIDE_RISE_DB = 15.0             # wider ones from this many dB (broadcast and phone blocks move with their load)
RECUR_SIGHTINGS = 3             # bursts on one new frequency heard this often (5 min apart or more) over
RECUR_MIN_S = 30 * 60           # this long, outside the bands where sensors and phones burst: an alert
RECUR_HOLD_S = 6 * 3600
BUSY_HOLD_S = 30 * 60           # in a phone, Wi-Fi or Bluetooth band a new steady signal is a note at first and an
BUSY_SIGHTINGS = 3              # alert once it was steady in this many sweeps (at least BUSY_GAP_S apart) spread
BUSY_GAP_S = 100                # over BUSY_MIN_S or more within BUSY_HOLD_S
BUSY_MIN_S = 8 * 60
LONG_GAP_S = 300                # a steady carrier that comes and goes: its sightings at least this far apart are
LONG_KEEP = 12                  # kept for RECUR_HOLD_S (this many at most), and RECUR_SIGHTINGS of them over
LONG_BUSY_SIGHTINGS = 6         # RECUR_MIN_S or more alert; in a phone, Wi-Fi or Bluetooth band it takes this many
LONG_BUSY_MIN_S = 2 * 3600      # over this long
OVER_SIGHTINGS = 3              # a steady carrier seen only in sweeps that were lifted as a whole alerts once it was
                                # in this many of them, LONG_GAP_S apart or more (twice as many in a busy band)
BROAD_KHZ = 2 * GRID_KHZ        # a record wider than this also takes peaks further away, by its own width (the
                                # strongest point of a wide signal is somewhere else at every look)
SAY_WIDTH_KHZ = 1000.0          # an alert says how wide the signal is from this width
TYPED_KHZ = 50.0                # a frequency typed by the user means the nearest alerted carrier within this
MINE_WIDER_KHZ = 50.0           # an accepted carrier takes signals up to this much (or a quarter) wider than it was
KEEP_FREQS = 400
STATE_VERSION = 2


def chunks(lo, hi, base_step_khz=10.0):
    """A band as sweeps: [(start, stop, step_khz)], each at most 200 MHz wide, with the step raised so that none
    has more than MAX_CHUNK_BINS bins (never lowered below base_step_khz)."""
    lo, hi = float(lo), float(hi)
    n = max(1, int(math.ceil((hi - lo) / MAX_CHUNK_MHZ - 1e-9)))
    w = (hi - lo) / n
    out = []
    for i in range(n):
        s, e = round(lo + i * w, 4), round(lo + (i + 1) * w, 4)
        step = max(float(base_step_khz), math.ceil((e - s) * 1000.0 / MAX_CHUNK_BINS))
        out.append((s, e, float(step)))
    return out


def coverage(bands, ranges):
    """Which of the bands the receivers tune. `ranges`: [(lo_mhz, hi_mhz)] of every receiver. Returns
    [{lo, hi, label, parts: [(lo, hi)], share}] where parts is what at least one receiver tunes and share the
    fraction of the band that is (0 to 1): a band with share 0 is not covered, and is said so."""
    out = []
    for lo, hi, label in bands:
        cuts = sorted((max(lo, a), min(hi, b)) for a, b in ranges if min(hi, b) > max(lo, a))
        parts = []
        for a, b in cuts:
            if parts and a <= parts[-1][1] + 1e-9:
                parts[-1] = (parts[-1][0], max(parts[-1][1], b))
            else:
                parts.append((a, b))
        got = sum(b - a for a, b in parts)
        out.append({"lo": lo, "hi": hi, "label": label, "parts": parts, "share": round(got / (hi - lo), 3)})
    return out


def gaps(bands, ranges):
    """The parts of the bands that no receiver tunes, merged: [(lo, hi)] in MHz."""
    out = []
    for c in coverage(bands, ranges):
        at = c["lo"]
        for a, b in c["parts"] + [(c["hi"], c["hi"])]:
            if a - at > 1e-6:
                if out and abs(out[-1][1] - at) < 1e-6:
                    out[-1] = (out[-1][0], a)
                else:
                    out.append((at, a))
            at = max(at, b)
    return out


def _key(mhz):
    return int(round(float(mhz) * 1000.0 / GRID_KHZ))


def _fmt(mhz):
    return f"{float(mhz):.4f}".rstrip("0").rstrip(".")


def _clock(t):
    return time.strftime("%H:%M", time.localtime(t))


def _when(t, now):
    """A clock time for a sentence: "08:00", with the weekday when that was not today ("Mon 08:00")."""
    lt = time.localtime(t)
    today = time.strftime("%Y-%j", lt) == time.strftime("%Y-%j", time.localtime(now))
    return time.strftime("%H:%M" if today else "%a %H:%M", lt)


def _span(s):
    """A length of time in words: "45 s", "12 min", "2 h 5 min"."""
    s = int(round(float(s)))
    if s < 90:
        return f"{s} s"
    if s < 2 * 3600:
        return f"{int(round(s / 60.0))} min"
    return f"{s // 3600} h {s % 3600 // 60} min"


def _names(by):
    ds = sorted(d for d in by if d)
    return (" on " + (ds[0] if len(ds) == 1 else ", ".join(ds[:-1]) + " and " + ds[-1])) if ds else ""


def _alerted(rec):
    was = rec.get("alerted") or {}
    return was.get("new") is not None or was.get("recurring") is not None


def _off(rec, mhz):
    """How far mhz lies outside the peaks a record has seen, in kHz (0 inside them)."""
    lo, hi = float(rec.get("lo", rec["mhz"])), float(rec.get("hi", rec["mhz"]))
    return max(0.0, lo - mhz, mhz - hi) * 1000.0


class Tracker:
    """The carriers a watch has seen. update(events, now) takes an audit's events (peak_mhz, surge_delta_db,
    pattern, new_signal, bandwidth_khz, hint, flagged_by: {receiver: dB over that receiver's own reference}) and
    returns the alerts and notes to raise: [{kind: new | surge | held | recurring | stronger | back | bursts |
    first | busy | overload | still, level: alert | note, mhz, text}].

    `new_db`: a carrier counts from this many dB over the reference (the trigger the watch was started with).

    Growth is judged per receiver, each against its own highest reading up to the first alert: receivers have
    different antennas, gains and even scales (dBm next to a dongle's own dB), so one receiver's level is never
    compared with another's. A carrier the user accepted (`accept`) raises nothing more unless a receiver reads it
    much stronger than when it was accepted, and is listed as accepted.

    A record has a width (the widest it was seen) and, when it is wide, the range of its peaks: the strongest point
    of a wide signal is somewhere else at every look, and its edges come and go as narrow signals of their own."""

    def __init__(self, freqs=None, new_db=None):
        self.freqs = dict(freqs or {})
        try:
            self.new_db = max(MIN_NEW_DB, float(new_db)) if new_db is not None else NEW_ALERT_DB
        except (TypeError, ValueError):
            self.new_db = NEW_ALERT_DB
        self.broad, self.widest = {}, 0.0       # the wide records by whole MHz, for a quick search (`_find`)
        for k, v in self.freqs.items():
            if float(v.get("bw") or 0.0) > BROAD_KHZ:
                self._broad(k, v)

    def _broad(self, k, rec):
        self.broad.setdefault(int(float(rec["mhz"])), set()).add(k)
        self.widest = max(self.widest, float(rec.get("bw") or 0.0))

    def state(self):
        keep = sorted(self.freqs.items(), key=lambda kv: (bool(kv[1].get("mine")), kv[1].get("last", 0)),
                      reverse=True)[:KEEP_FREQS]
        return {"v": STATE_VERSION, "freqs": {str(k): v for k, v in keep}}

    @classmethod
    def from_state(cls, st, new_db=None):
        out = {}
        src = (st or {}).get("freqs") if isinstance(st, dict) and "freqs" in (st or {}) else (st or {})
        for k, v in (src or {}).items():
            try:
                rec = dict(v)
                rec.setdefault("by", {})
                rec.setdefault("alerted", {})
                out[int(k)] = rec
            except (TypeError, ValueError):
                continue
        return cls(out, new_db=new_db)

    @staticmethod
    def _fits(rec, width_khz, cells):
        """Whether a record takes an event this wide, this many grid cells away (None: reached by its width). Any
        record does, but one the user accepted takes only what fits the carrier that was accepted: within one cell
        or its own width, and not much wider. One accepted before it was heard takes its first signal as it comes."""
        if not rec.get("mine") or (rec.get("count") and "mine_bw" not in rec):
            return True                         # (accepted in an older state, which kept no width: as it was)
        if cells is not None and cells > 1:
            return False
        if not rec.get("count"):
            return cells is not None
        own = float(rec.get("mine_bw", rec.get("bw") or 0.0) or 0.0)
        return width_khz <= own + max(MINE_WIDER_KHZ, own / 4.0)

    def _find(self, mhz, width_khz=0.0, now=None):
        """The record of a carrier at mhz, or None: the one in its grid cell, else the nearest within the event's
        own width (at least one cell), else the nearest wide record that reaches it with its own width: its peaks
        lie within one width of each other, and its edges within half a width of them. A signal much narrower
        than the record is one of its edges only while the record itself is on (seen in this same update)."""
        k = _key(mhz)
        w = float(width_khz or 0.0)
        if k in self.freqs and self._fits(self.freqs[k], w, 0):
            return k
        near = int(min(MAX_NEAR_CELLS, max(1, math.ceil(max(GRID_KHZ, w / 2.0) / GRID_KHZ))))
        for d in range(1, near + 1):
            for kk in (k - d, k + d):
                if kk in self.freqs and self._fits(self.freqs[kk], w, d):
                    return kk
        best, best_off = None, 0.0
        far = (2.0 * self.widest + GRID_KHZ) / 1000.0     # (no record reaches further than twice its width)
        b0, b1 = int(mhz - far), int(mhz + far)
        for b in (range(b0, b1 + 1) if b1 - b0 < len(self.broad) else list(self.broad)):
            for kk in self.broad.get(b, ()):
                rec = self.freqs.get(kk)
                if rec is None:
                    continue
                own = float(rec.get("bw") or 0.0)
                if w < own / 2.0 and now is not None and rec.get("last") != now:
                    continue
                off = _off(rec, mhz)
                seen = (float(rec.get("hi", rec["mhz"])) - float(rec.get("lo", rec["mhz"]))) * 1000.0
                if off <= max(own / 2.0, own - seen) + GRID_KHZ and (best is None or off < best_off) \
                        and self._fits(rec, w, None):
                    best, best_off = kk, off
        return best

    def _typed(self, mhz, want):
        """The record a frequency given by the user means, among those `want` passes: the one in its grid cell or
        the next cell (one that alerted first), else the nearest that alerted within TYPED_KHZ (a wide one: within
        half its width). None when there is none."""
        k = _key(mhz)
        if k in self.freqs and want(self.freqs[k]):
            return k
        side = [kk for kk in (k - 1, k + 1) if kk in self.freqs and want(self.freqs[kk])]
        if side:
            return min(side, key=lambda kk: (not _alerted(self.freqs[kk]), abs(float(self.freqs[kk]["mhz"]) - mhz)))
        best, best_off = None, 0.0
        for kk, rec in self.freqs.items():
            if not (want(rec) and (_alerted(rec) or rec.get("mine"))):
                continue
            bw = float(rec.get("bw") or 0.0)
            off = _off(rec, mhz)
            if off <= TYPED_KHZ + (bw / 2.0 if bw > WIDE_KHZ else 0.0) and (best is None or off < best_off):
                best, best_off = kk, off
        return best

    def accept(self, mhz, label, now=None):
        """The user knows this carrier ("mine"): no more alerts for it unless it grows much stronger. Returns its
        record, whose `mhz` is the frequency really accepted: the carrier in that grid cell or the next one, else
        the nearest one that alerted within TYPED_KHZ; with none there, an empty record at mhz for a carrier that
        was not heard yet."""
        now = time.time() if now is None else float(now)
        mhz = float(mhz)
        k = self._typed(mhz, lambda rec: True)
        if k is None:
            k = _key(mhz)
            self.freqs[k] = {"mhz": round(mhz, 4), "first": now, "last": 0.0, "count": 0, "by": {}, "alerted": {}}
        rec = self.freqs[k]
        rec["mine"] = str(label or "known")[:60]
        # what it read and how wide it was when it was accepted: growth and a wider signal are judged against these
        rec["mine_at"] = {d: float(r.get("max", r.get("last", 0.0))) for d, r in (rec.get("by") or {}).items()}
        rec["mine_bw"] = float(rec.get("bw") or 0.0)
        rec.setdefault("alerted", {}).pop("mine_grown", None)
        return rec

    def unaccept(self, mhz):
        k = self._typed(float(mhz), lambda rec: bool(rec.get("mine")))
        if k is None:
            return None
        rec = self.freqs[k]
        for name in ("mine", "mine_at", "mine_bw"):
            rec.pop(name, None)
        (rec.get("alerted") or {}).pop("mine_grown", None)
        return rec

    def accepted(self):
        return sorted(({"mhz": v["mhz"], "label": v["mine"]} for v in self.freqs.values() if v.get("mine")),
                      key=lambda x: x["mhz"])

    def update(self, events, now=None):
        now = time.time() if now is None else float(now)
        todo = []
        for ev in events or []:
            try:
                mhz = float(ev["peak_mhz"])
                delta = float(ev.get("surge_delta_db"))
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            if not (math.isfinite(mhz) and math.isfinite(delta)):
                continue
            by_now = {}
            for d, v in (ev.get("flagged_by") or {"": delta}).items():
                try:
                    by_now[str(d)] = float(v)
                except (TypeError, ValueError):
                    continue
            if not by_now:
                by_now = {"": delta}
            try:
                bw = float(ev.get("bandwidth_khz") or 0.0)
            except (TypeError, ValueError):
                bw = 0.0
            todo.append((ev, mhz, delta, by_now, bw if math.isfinite(bw) else 0.0))
        todo.sort(key=lambda x: -x[4])          # the widest first: a wide signal's edges then find its record
        alerts = []
        for ev, mhz, delta, by_now, bw in todo:
            for al in self._one(ev, mhz, delta, by_now, bw, now):
                al.update(over_db=round(delta, 1), by={d: round(v, 1) for d, v in by_now.items() if d},
                          bw_khz=ev.get("bandwidth_khz"), pattern=ev.get("pattern"))
                alerts.append(al)
        self._prune()
        return alerts

    def _prune(self):
        """Never grow without end (a hopper leaves a record per channel): past 3 x KEEP_FREQS the oldest records
        that neither alerted nor were accepted go. What a record holds is bounded too: its lists of sightings
        keep a few entries each."""
        if len(self.freqs) <= 3 * KEEP_FREQS:
            return
        plain = sorted((k for k, v in self.freqs.items() if not (v.get("mine") or _alerted(v))),
                       key=lambda k: self.freqs[k].get("last") or 0)
        for k in plain[:len(self.freqs) - 2 * KEEP_FREQS]:
            self.broad.get(int(float(self.freqs[k].get("mhz") or 0.0)), set()).discard(k)
            del self.freqs[k]

    def _steady_text(self, kind, rec, mhz, delta, tail, heard, now, looks, since):
        first = _when(rec["first"], now)
        if kind == "held":
            return (f"{_fmt(mhz)} MHz{tail} was on in {looks} looks over {_span(now - since)}, "
                    f"{delta:.0f} dB over the quiet reference{heard}. Phones and Wi-Fi come and go; one that "
                    f"keeps being there is worth a look.")
        if kind == "recurring":
            return (f"{_fmt(mhz)} MHz{tail} has now been on in {looks} looks since {_when(since, now)}, "
                    f"{delta:.0f} dB over the quiet reference{heard}. It comes and goes. Worth a look: a "
                    f"transmitter that is only on at times looks like this, and so do calls on a shared radio "
                    f"channel.")
        if rec.get("pattern") != "steady":
            return (f"{_fmt(mhz)} MHz{tail} was first seen as bursts at {first} and is steady in this look, "
                    f"{delta:.0f} dB over the quiet reference{heard}. Worth a look.")
        when = f"first heard {first} and there again now" if looks >= 2 else f"first heard {first}"
        if kind == "surge":
            return (f"{_fmt(mhz)} MHz{tail} is {delta:.0f} dB stronger than in the quiet reference{heard}, {when}. "
                    f"Something was already on this frequency then; now there is much more. Worth a look: a "
                    f"transmitter switched on beside or on top of an existing signal looks like this.")
        return (f"New steady signal at {_fmt(mhz)} MHz{tail}, {delta:.0f} dB over the quiet reference{heard}, "
                f"{when}. Worth a look: a new carrier is what a transmitter that was not there before looks like, "
                f"and also a new router, charger or phone.")

    def _one(self, ev, mhz, delta, by_now, bw, now):
        """One event against the records: the alerts and notes it raises."""
        out = []
        steady = ev.get("pattern") == "steady"
        busy, new = bool(ev.get("busy")), bool(ev.get("new_signal"))
        overload, quiet = bool(ev.get("overload")), bool(ev.get("quiet"))
        where = str(ev.get("hint") or "")
        tail = (f" ({where})" if where else "") + (f", about {bw / 1000.0:.1f} MHz wide" if bw >= SAY_WIDTH_KHZ
                                                    else "")
        heard = _names(by_now)
        k = self._find(mhz, bw, now)
        if k is None and _key(mhz) in self.freqs:
            k = _key(mhz)                       # (an accepted carrier's own cell: judged as its growth below)
        rec = self.freqs.get(k) if k is not None else None
        fresh = rec is None or not rec.get("count")         # (also one accepted before it was ever heard)
        if rec is None:
            k = _key(mhz)
            rec = self.freqs[k] = {"by": {}, "alerted": {}}
        if fresh:
            rec.update(mhz=round(mhz, 4), first=now, last=None, count=0, pattern=ev.get("pattern"),
                       hint=where[:80])
            if rec.get("mine"):
                rec["mine_bw"] = bw
        gap = now - float(rec["last"]) if rec.get("last") is not None else 0.0
        rec["count"] = int(rec.get("count", 0)) + 1
        rec["last"] = now
        own = float(rec.get("bw") or 0.0)
        if bw > own:
            rec["bw"] = own = bw
        if own > BROAD_KHZ:
            self._broad(k, rec)
            if bw >= own / 2.0:                 # the signal itself, not one of its edges: its peaks' range grows
                lo = min(float(rec.get("lo", rec["mhz"])), mhz)
                hi = max(float(rec.get("hi", rec["mhz"])), mhz)
                if (hi - lo) * 1000.0 <= own:
                    rec["lo"], rec["hi"] = round(lo, 4), round(hi, 4)
        # the frequency an alert names: the record's own, or this peak when it lies far from it in a wide signal
        at = rec["mhz"] if abs(mhz - float(rec["mhz"])) * 1000.0 <= MAX_NEAR_CELLS * GRID_KHZ else round(mhz, 4)
        mine = bool(rec.get("mine"))
        if mine and "mine_bw" not in rec:
            rec["mine_bw"] = own
        by = rec.setdefault("by", {})
        was = rec.setdefault("alerted", {})
        grown, who = 0.0, ""
        for d, v in by_now.items():
            r = by.get(d)
            if r is None:
                by[d] = {"first": v, "last": v, "max": v}
                if mine:
                    rec.setdefault("mine_at", {})[d] = v
                continue
            r["last"], r["max"] = v, max(float(r.get("max", v)), v)
            base = float(rec.setdefault("mine_at", {}).setdefault(d, r["max"]) if mine
                         else r.get("ref", r.get("first", v)))
            if v - base > grown:
                grown, who = v - base, d
        if mine:
            prev = was.get("mine_grown")
            if delta >= self.new_db and grown >= STRONGER_DB and (prev is None or grown >= float(prev) + REALERT_DB):
                was["mine_grown"] = grown
                out.append({"kind": "stronger", "level": "alert", "mhz": at, "text":
                            f"The signal you accepted at {_fmt(rec['mhz'])} MHz ({rec['mine']}) reads "
                            f"{grown:.0f} dB stronger{(' on ' + who) if who else ''} than when you accepted it. "
                            f"Worth a look: a second transmitter on the same frequency looks like this, and so "
                            f"does yours moved closer."})
            return out
        if delta < self.new_db:
            return out
        if not fresh and gap >= BACK_AFTER_S and not float(was.get("back", 0)) > now - BACK_AFTER_S:
            was["back"] = now
            out.append({"kind": "back", "level": "note", "mhz": at, "text":
                        f"{_fmt(mhz)} MHz{tail} was seen again after {gap / 3600:.1f} h without a sighting. First "
                        f"heard {_when(rec['first'], now)}."})
        lifted = None
        if overload:
            # a receiver driven too hard makes signals of its own: nothing seen then is called new at once, but a
            # steady carrier that is there in sweep after sweep is told all the same
            ot = [t for t in rec.get("over_t") or [] if now - t <= RECUR_HOLD_S]
            if not ot or now - ot[-1] >= LONG_GAP_S:
                ot.append(now)
            rec["over_t"] = ot[-2 * OVER_SIGHTINGS:]
            if was.get("overload") is None:
                was["overload"] = now
                out.append({"kind": "overload", "level": "note", "mhz": at, "text":
                            f"Signal at {_fmt(mhz)} MHz{tail}, {delta:.0f} dB over the reference{heard}, seen "
                            f"while the receiver was overloaded by a very strong signal: it may be the "
                            f"receiver's own mixing product."})
            if busy:
                rec["busy"] = True
            if not (steady and was.get("new") is None
                    and len(ot) >= OVER_SIGHTINGS * (2 if rec.get("busy") else 1)):
                return out
            lifted = ot
        if steady and was.get("new") is None:
            if busy:
                rec["busy"] = True
            in_busy = bool(rec.get("busy"))
            wide_rise = not new and bw > WIDE_KHZ and delta < WIDE_RISE_DB
            hold, need_gap = (BUSY_HOLD_S, BUSY_GAP_S) if in_busy else (CONFIRM_HOLD_S, CONFIRM_GAP_S)
            ts = [t for t in rec.get("steady_t") or [] if now - t <= hold]
            seen_before = bool(ts)
            if not ts or now - ts[-1] >= need_gap:
                ts.append(now)
            rec["steady_t"] = ts[-8:]
            seen = [t for t in rec.get("seen_t") or [] if now - t <= RECUR_HOLD_S]
            if not seen or now - seen[-1] >= LONG_GAP_S:
                seen.append(now)
            rec["seen_t"] = seen = seen[-LONG_KEEP:]
            kind = "held" if in_busy else ("new" if new else "surge")
            if in_busy:
                ready = len(ts) >= BUSY_SIGHTINGS and now - ts[0] >= BUSY_MIN_S
                late = len(seen) >= LONG_BUSY_SIGHTINGS and now - seen[0] >= LONG_BUSY_MIN_S
            else:
                ready = (delta >= STRONG_DB or len(ts) >= 2) and not wide_rise
                late = len(seen) >= RECUR_SIGHTINGS and now - seen[0] >= RECUR_MIN_S and not wide_rise
            text = None
            if lifted is not None and not wide_rise:
                text = (f"Signal at {_fmt(mhz)} MHz{tail}, {delta:.0f} dB over the reference{heard}, was on in "
                        f"{len(lifted)} sweeps since {_when(lifted[0], now)}, each of them lifted as a whole by a "
                        f"very strong signal. It may be the receiver's own mixing product, and it may be a "
                        f"transmitter that was not there before. Worth a look.")
            elif lifted is None and ready:
                text = self._steady_text(kind, rec, mhz, delta, tail, heard, now, len(ts), ts[0])
            elif lifted is None and late:
                kind = "held" if in_busy else "recurring"
                text = self._steady_text(kind, rec, mhz, delta, tail, heard, now, len(seen), seen[0])
            if text is not None:
                was["new"] = now
                out.append({"kind": kind, "level": "alert", "mhz": at, "text": text})
                rec["pattern"] = "steady"
                for r in by.values():           # growth is judged from here: the highest reading up to this alert
                    r["ref"] = float(r.get("max", r.get("last", 0.0)))
            elif lifted is None and not seen_before and was.get("first") is None:
                was["first"] = now
                out.append({"kind": "busy" if in_busy else "first", "level": "note", "mhz": at,
                            "text": f"Signal at {_fmt(mhz)} MHz{tail}, {delta:.0f} dB over the quiet reference"
                                    f"{heard}, heard at {_clock(now)}. "
                                    + ("A wide signal that was already there moves with its load; it is only "
                                       "noted." if wide_rise else
                                       "Phones and Wi-Fi come and go here; it becomes an alert if it keeps "
                                       "being there." if in_busy else
                                       "It becomes an alert if a look within 20 minutes finds it again, or if "
                                       "it keeps coming back.")})
            return out
        prev = was.get("grown")
        if steady and grown >= STRONGER_DB and (prev is None or grown >= float(prev) + REALERT_DB):
            was["grown"] = grown
            out.append({"kind": "stronger", "level": "alert", "mhz": at, "text":
                        f"{_fmt(mhz)} MHz{tail} is {grown:.0f} dB stronger{(' on ' + who) if who else ''} than "
                        f"in any look up to its first alert at {_when(was.get('new', rec['first']), now)}. "
                        f"Something may have moved closer or been switched to higher power. Worth a look."})
        elif steady and now - float(max(was.get("new", 0), was.get("still", 0))) >= REPEAT_AFTER_S:
            was["still"] = now
            out.append({"kind": "still", "level": "note", "mhz": at, "text":
                        f"Still there: the steady signal at {_fmt(mhz)} MHz{tail}, first heard "
                        f"{_when(rec['first'], now)}, seen in {rec['count']} sweeps."})
        elif not steady and was.get("new") is None:
            if was.get("bursts") is None and not quiet:
                was["bursts"] = now
                out.append({"kind": "bursts", "level": "note", "mhz": at, "text":
                            f"{'New bursting' if new else 'Stronger bursting'} signal at {_fmt(mhz)} MHz{tail}, "
                            f"{delta:.0f} dB over the reference{heard}, first heard {_clock(now)}. Bursts are "
                            f"normal for phones, Wi-Fi and sensors."})
            # (one of many bursts in a sweep counts here too: a hopper does not come back to one frequency)
            if new and not busy and not ev.get("sensor") and was.get("recurring") is None:
                bt = [t for t in rec.get("burst_t") or [] if now - t <= RECUR_HOLD_S]
                if not bt or now - bt[-1] >= LONG_GAP_S:
                    bt.append(now)
                rec["burst_t"] = bt[-8:]
                if len(bt) >= RECUR_SIGHTINGS and now - bt[0] >= RECUR_MIN_S:
                    was["recurring"] = now
                    out.append({"kind": "recurring", "level": "alert", "mhz": at, "text":
                                f"A bursting signal at {_fmt(mhz)} MHz{tail} has now been heard {len(bt)} times "
                                f"since {_when(bt[0], now)}{heard}, outside the bands where sensors and phones "
                                f"burst. Worth a look: a device that sends in short bursts looks like this, and so "
                                f"do short calls on a shared radio channel."})
        return out

    def open_alerts(self, now=None, within_s=3 * 3600):
        """The carriers that alerted and were heard in the last hours, not accepted: what is on now."""
        now = time.time() if now is None else float(now)
        out = [{"mhz": v["mhz"], "first": v.get("first"), "last": v.get("last"), "count": v.get("count", 0),
                "hint": v.get("hint", ""), "receivers": sorted(d for d in (v.get("by") or {}) if d)}
               for v in self.freqs.values()
               if not v.get("mine") and (v.get("alerted") or {}).get("new") is not None
               and now - float(v.get("last") or 0) <= within_s]
        return sorted(out, key=lambda x: x["mhz"])


REPORT_NOTES_PER_DAY = 150
REPORT_SHORT_ALERTS = 20
REPORT_LATE_REFS = 12           # the header names this many parts of the reference that were recorded later
ROW_KINDS = {"start": "start", "alert": "ALERT", "note": "note", "mark": "MARK", "stop": "end", "skip": "gap",
             "ack": "seen", "health": "HEALTH", "mine": "accepted", "late": "reference"}


def inventory_lines(inv):
    """The carriers that were on the air when the reference was recorded, as lines for the report. `inv`:
    {total, fm, tv, busy, own, listed: [{mhz, db, receivers, band, note}], more}."""
    if not inv or not inv.get("total"):
        return []
    parts = [f"{inv[k]} {name}" for k, name in (("fm", "FM stations"), ("tv", "in TV or radio broadcast bands"),
                                                 ("busy", "in phone, Wi-Fi or Bluetooth bands"),
                                                 ("own", "at multiples of the receivers' own clock")) if inv.get(k)]
    out = [f"On the air when the reference was recorded: {inv['total']} steady signals"
           + (f" ({', '.join(parts)})" if parts else "") + "."]
    listed = inv.get("listed") or []
    if listed:
        out.append("The others, strongest first (a transmitter that was already on then is in the reference and "
                   "never alerts: Decode the ones you do not know):")
        for x in listed:
            rc = x.get("receivers") or []
            what = "; ".join(str(t) for t in (x.get("band"), x.get("note")) if t)
            out.append(f"  {_fmt(x['mhz'])} MHz, {x.get('db', 0):.0f} dB over noise"
                       + (f", {' and '.join(rc)}" if rc else "") + (f": {what}" if what else ""))
        if inv.get("more"):
            out.append(f"  and {inv['more']} weaker ones.")
    return out


def summary_line(state):
    """One plain sentence for the status: rounds, alerts and when the last one was, or notes when there were none."""
    n = int(state.get("rounds") or 0)
    a = int(state.get("alerts_total") or 0)
    if not n:
        return "No round finished yet."
    took = state.get("round_s")
    pace = f" (a round takes {_took(took)})" if took else ""
    if not a:
        try:
            m = int(state.get("notes_total") or 0)
        except (TypeError, ValueError):
            m = 0
        return f"{n} round{'s' if n != 1 else ''}{pace}, no alerts" \
            + (f"; {m} note{'s' if m != 1 else ''} in the timeline." if m > 0 else ".")
    last = state.get("last_alert")
    return f"{n} round{'s' if n != 1 else ''}{pace}, {a} alert{'s' if a != 1 else ''}" \
        + (f", the last at {last}." if last else ".")


def _took(s):
    s = int(round(float(s)))
    return f"{s} s" if s < 90 else f"{s // 60} min {s % 60} s"


def _late_line(late):
    """The parts of the reference that were recorded after the start, as one header line."""
    said = []
    for x in late[:REPORT_LATE_REFS]:
        try:
            said.append(f"{float(x['s']):g}-{float(x['e']):g} MHz"
                        + (f" on {x['dongle']}" if x.get("dongle") else "")
                        + (f" at {str(x['time'])[5:16]}" if x.get("time") else ""))
        except (KeyError, TypeError, ValueError):
            continue
    if not said:
        return None
    more = len(late) - len(said)
    return ("Recorded later (what was on the air then counts as normal there): " + "; ".join(said)
            + (f"; and {more} more" if more > 0 else "") + ".")


def report(rows, state=None):
    """The watch as text: a title, what was covered and what was not, then a timeline per day from the log rows
    ({t, time, kind: start | alert | note | late | mark | stop ..., text}). Rows from before `state["log_since"]`
    belong to earlier watches and are counted, not shown. Returns {days: [{day, lines}], text, short}."""
    state = state or {}
    rows = [x for x in rows if isinstance(x, dict) and x.get("kind") in ROW_KINDS]
    since, earlier = state.get("log_since"), 0
    if isinstance(since, (int, float)):
        own = [x for x in rows if not (isinstance(x.get("t"), (int, float)) and x["t"] < since)]
        earlier, rows = len(rows) - len(own), own
    days, order = {}, []
    for r in sorted(rows, key=lambda x: x.get("t", 0)):
        t = r.get("t")
        day = time.strftime("%Y-%m-%d", time.localtime(t)) if isinstance(t, (int, float)) else str(r.get("time"))[:10]
        if day not in days:
            days[day] = []
            order.append(day)
        tag = ROW_KINDS[r["kind"]]
        days[day].append((r["kind"], f"{str(r.get('time') or '')[11:16] or _clock(t)}  {tag}: {r.get('text', '')}"))
    lines = ["Room watch report"]
    if state.get("started"):
        lines.append(f"Started {state['started']}" + (f", ended {state['ended']}" if state.get("ended") else
                                                       ", still running" if state.get("running") else ""))
    lines.append(summary_line(state))
    if state.get("failed_rounds"):
        lines.append(f"{state['failed_rounds']} rounds in which no sweep worked are not counted.")
    gap = state.get("gaps_s")
    if isinstance(gap, (int, float)) and gap >= 60:
        lines.append(f"Not watched for {_span(gap)} in total (the gaps are in the timeline).")
    bands = [b for b in state.get("bands") or [] if isinstance(b, dict)]
    if bands:
        recv = sorted({d for b in bands for d in b.get("dongles") or []})
        num = lambda v: f"{v:g}" if isinstance(v, (int, float)) else "?"
        lines.append(f"Watched: {len(bands)} band{'s' if len(bands) != 1 else ''} on "
                     f"{', '.join(recv) or 'no receiver'}; a round about every "
                     f"{num(state.get('interval_min'))} min; a rise of {num(state.get('trigger_db'))} dB over the "
                     f"reference counts.")
        lines.append("Bands: " + "; ".join(f"{b['s']:g}-{b['e']:g} MHz (step {b.get('step', 0):g} kHz, "
                                           f"{' + '.join(b.get('dongles') or [])})" for b in bands) + ".")
    types = state.get("receiver_types")
    if isinstance(types, dict) and any(str(v) != "RTL-SDR" for v in types.values()):
        lines.append("Receivers: " + "; ".join(f"{d}: {types[d]}" for d in sorted(types)) + ".")
    late = [x for x in state.get("late_refs") or [] if isinstance(x, dict)]
    late_said = _late_line(late)
    if isinstance(state.get("watch_since"), (int, float)):
        at = time.strftime("%Y-%m-%d %H:%M", time.localtime(state["watch_since"]))
        lines.append(f"The reference was complete at {at}, except for the parts recorded later. Anything already "
                     f"transmitting when its part was recorded counts as normal and never alerts." if late_said else
                     f"The reference was complete at {at}. Anything already transmitting then counts as normal "
                     f"and never alerts.")
    if late_said:
        lines.append(late_said)
    lines.append(f"Times are the computer's local time (UTC{time.strftime('%z')}).")
    unc = state.get("not_covered") or []
    if unc:
        lines.append("Not covered by any receiver here: " + "; ".join(unc) + ". A clear result says nothing about them.")
    part = state.get("partly_covered") or []
    if part:
        lines.append("Only partly covered: " + "; ".join(part) + ".")
    acc = state.get("accepted") or []
    if acc:
        lines.append("Accepted as known by you: " + "; ".join(f"{_fmt(x['mhz'])} MHz ({x['label']})" for x in acc) + ".")
    lines.extend(inventory_lines(state.get("inventory")))
    if earlier:
        lines.append(f"{earlier} earlier row{'s' if earlier != 1 else ''} of previous watches "
                     f"{'are' if earlier != 1 else 'is'} not shown here.")
    lines.append("A clear watch means nothing obvious appeared in the bands that were covered. It does not prove a "
                 "room is clean: transmitters that hop, spread under the noise or send only short bursts can be "
                 "missed, and recorders that never transmit cannot be seen this way.")
    out_days = []
    for d in order:
        notes = sum(1 for kind, _ in days[d] if kind == "note")
        drop = max(0, notes - REPORT_NOTES_PER_DAY)
        shown, cut = [], 0
        for kind, text in days[d]:
            if kind == "note" and cut < drop:
                cut += 1                                  # (the earliest notes of a long day)
                continue
            shown.append(text)
        out_days.append({"day": d, "lines": shown})
        lines.append("")
        lines.append(d + (f"  ({notes} notes that day; the first {drop} are left out here, every alert, mark, gap, "
                          f"check and reference recorded late is listed)" if drop else ""))
        lines.extend("  " + x for x in shown)
    if not order:
        lines.append("")
        lines.append("Nothing was logged.")
    # the short form (the chat): the header, a count per day and the last alerts; the whole timeline stays on the page
    head = lines[:lines.index("")] if "" in lines else list(lines)
    short = list(head)
    for d in order:
        kinds = [k for k, _ in days[d]]
        short.append(f"{d}: {kinds.count('alert')} alerts, {kinds.count('health')} checks of the watch itself, "
                     f"{kinds.count('note')} notes, {kinds.count('mark')} marks, {kinds.count('skip')} gaps"
                     + (f", {kinds.count('late')} parts of the reference recorded late" if "late" in kinds else "")
                     + ".")
    last = [(d, text) for d in order for kind, text in days[d] if kind in ("alert", "health")][-REPORT_SHORT_ALERTS:]
    if last:
        short.append("The last alerts:")
        short.extend(f"  {d[5:]} {text}" for d, text in last)
    short.append("The whole timeline: Activity tab, Report (it can be downloaded).")
    return {"days": out_days, "text": "\n".join(lines), "short": "\n".join(short)}
