"""What a spectrum view can show, and how to sweep it.

The Spectrum page lets the user pick any range from 0 to 6000 MHz, whether or not a receiver here tunes it. This
module works out, from the ranges the receivers tune, which parts of a view can be measured and which cannot, the
step between points, and the pieces to sweep one after another so the picture fills in step by step. A part no
receiver tunes is a gap: it is never swept and never drawn as a level.

Pure Python: no engine, no web server, no radio is touched here.
"""
from __future__ import annotations

import math

VIEW_MIN_MHZ = 0.0
VIEW_MAX_MHZ = 6000.0
MIN_SPAN_MHZ = 0.01
STEPS_KHZ = (1.0, 2.5, 5.0, 10.0, 12.5, 25.0, 50.0, 100.0, 200.0, 500.0, 1000.0)
STEP_CHOICES_KHZ = (1.0, 2.5, 5.0, 10.0, 25.0, 50.0, 100.0, 200.0, 500.0, 1000.0)    # what the page offers
MAX_SWEEP_BINS = 120000          # one whole sweep never has more points than this, whatever step was asked for
MIN_SWEEP_BINS = 16              # ... and a step so coarse that fewer are left is made finer
MIN_CHUNK_POINTS = 32
AUTO_FILL = 0.9375               # the automatic step aims a little under max_points (1500 of 1600)
AUTO_MAX_STEP_KHZ = 200.0        # ... and never goes coarser than this: a coarser step does not make a sweep faster
                                 # (the receiver still visits every part of the range), it only blurs the picture;
                                 # the points sent to the page are thinned to max_points, strongest kept


def _f(x) -> str:
    """A frequency for a sentence: 24, 2483.5, 0.1."""
    s = f"{float(x):.3f}".rstrip("0").rstrip(".")
    return s or "0"


def span_text(lo, hi) -> str:
    return f"{_f(lo)}-{_f(hi)} MHz"


def _num(x, what) -> float:
    if isinstance(x, bool) or x is None or (isinstance(x, str) and not x.strip()):
        raise ValueError(f"Enter a {what} frequency in MHz.")
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise ValueError(f"The {what} frequency must be a number in MHz.")
    if math.isnan(v) or math.isinf(v):
        raise ValueError(f"The {what} frequency must be a number in MHz.")
    return v


def view(start, stop) -> tuple:
    """The view as (start, stop) in MHz, or ValueError in plain words."""
    s, e = round(_num(start, "start"), 4), round(_num(stop, "stop"), 4)
    if s < VIEW_MIN_MHZ or e > VIEW_MAX_MHZ:
        raise ValueError(f"The view must stay between {_f(VIEW_MIN_MHZ)} and {_f(VIEW_MAX_MHZ)} MHz.")
    if s >= e:
        raise ValueError("The start frequency must be below the stop frequency.")
    if e - s < MIN_SPAN_MHZ - 1e-9:
        raise ValueError(f"The view must be at least {_f(MIN_SPAN_MHZ * 1000)} kHz wide.")
    return s, e


def _part(s, e, rng):
    """The part of s-e inside a receiver's range, or None (slivers under the smallest span are not swept)."""
    try:
        a, b = float(rng[0]), float(rng[1])
    except (TypeError, ValueError, IndexError):
        return None
    lo, hi = max(s, a), min(e, b)
    if hi - lo < MIN_SPAN_MHZ - 1e-9:
        return None
    return [round(lo, 4), round(hi, 4)]


def _union(parts) -> list:
    out = []
    for lo, hi in sorted(parts):
        if out and lo <= out[-1][1] + 1e-9:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return out


def _gaps(s, e, covered) -> list:
    out, at = [], s
    for lo, hi in covered:
        if lo - at > 1e-9:
            out.append([round(at, 4), round(lo, 4)])
        at = max(at, hi)
    if e - at > 1e-9:
        out.append([round(at, 4), round(e, 4)])
    return out


def cut(lo, hi, max_mhz) -> list:
    """lo-hi in equal pieces of at most max_mhz."""
    n = max(1, int(math.ceil((hi - lo) / float(max_mhz) - 1e-9)))
    edges = [round(lo + (hi - lo) * i / n, 4) for i in range(n)] + [round(hi, 4)]
    return [[edges[i], edges[i + 1]] for i in range(n)]


def _step(covered_khz, rbw_khz, max_points):
    """(step, automatic?, raised?, lowered?) for a sweep of covered_khz in all."""
    if rbw_khz is None or (isinstance(rbw_khz, str) and rbw_khz.strip().lower() in ("", "auto")):
        want = covered_khz / (AUTO_FILL * max_points)
        return next((s for s in STEPS_KHZ if want - 1e-9 <= s <= AUTO_MAX_STEP_KHZ), AUTO_MAX_STEP_KHZ), \
            True, False, False
    try:
        asked = float(rbw_khz)
    except (TypeError, ValueError):
        raise ValueError("The step must be a number in kHz, or automatic.")
    if isinstance(rbw_khz, bool) or math.isnan(asked) or not STEPS_KHZ[0] <= asked <= STEPS_KHZ[-1]:
        raise ValueError(f"The step must be between {_f(STEPS_KHZ[0])} and {_f(STEPS_KHZ[-1])} kHz.")
    if covered_khz / asked > MAX_SWEEP_BINS:
        need = covered_khz / MAX_SWEEP_BINS
        return next((s for s in STEPS_KHZ if s >= need - 1e-9), STEPS_KHZ[-1]), False, True, False
    if covered_khz / asked < MIN_SWEEP_BINS:
        need = covered_khz / MIN_SWEEP_BINS
        finer = [s for s in STEPS_KHZ if s <= need + 1e-9]
        low = finer[-1] if finer else STEPS_KHZ[0]
        return low, False, False, low < asked - 1e-9             # already the finest step: nothing was lowered
    return asked, False, False, False


def plan(start, stop, ranges, live_max_span_mhz, rbw_khz=None, max_points=1600, max_chunk_mhz=200.0,
         live_receivers=None) -> dict:
    """How to show start-stop MHz with these receivers. ranges = {receiver id: (lowest MHz, highest MHz) it tunes}.

    mode "live": the view is narrow enough for one direct capture and a receiver tunes all of it; "sweep": some of
    it is tuned; "none": no receiver tunes any of it (nothing may be opened, nothing may be drawn as a level).
    "covered" is what the receivers tune inside the view, "gaps" what none of them does, "receivers" each one's own
    part (or None), "chunks" the pieces to sweep in turn, each with its share of max_points.
    live_receivers: the receivers able to make the direct capture (all of them when None); a narrow view that only
    the others tune is swept instead."""
    s, e = view(start, stop)
    try:
        max_points = int(max_points)
    except (TypeError, ValueError):
        raise ValueError("The number of points must be a whole number.")
    if max_points < MIN_CHUNK_POINTS:
        raise ValueError(f"The number of points must be at least {MIN_CHUNK_POINTS}.")
    max_chunk_mhz = float(max_chunk_mhz)
    if max_chunk_mhz <= 0:
        raise ValueError("The largest piece of a sweep must be wider than 0 MHz.")
    parts = {str(r): _part(s, e, rng) for r, rng in (ranges or {}).items()}
    covered = _union([list(p) for p in parts.values() if p])
    gaps = _gaps(s, e, covered)
    total = sum(hi - lo for lo, hi in covered)
    out = {"start_mhz": s, "stop_mhz": e, "span_mhz": round(e - s, 4), "covered": covered, "gaps": gaps,
           "covered_mhz": round(total, 4), "receivers": parts, "live_receivers": [], "chunks": [],
           "step_khz": None, "step_auto": True, "step_asked_khz": None, "step_raised": False, "step_lowered": False,
           "step_note": "", "bins": 0, "points": 0}
    asked = None if rbw_khz is None or (isinstance(rbw_khz, str) and rbw_khz.strip().lower() in ("", "auto")) \
        else rbw_khz
    if not covered:
        if asked is not None:
            _step(1000.0, asked, max_points)                     # a bad step is still a bad step
        out["mode"] = "none"
        return out
    can = None if live_receivers is None else {str(r) for r in live_receivers}
    whole = [r for r, rng in (ranges or {}).items()
             if float(rng[0]) <= s + 1e-9 and e <= float(rng[1]) + 1e-9 and (can is None or str(r) in can)]
    if e - s <= float(live_max_span_mhz) + 1e-6 and whole:
        if asked is not None:
            _step(total * 1000.0, asked, max_points)
        out.update({"mode": "live", "live_receivers": [str(r) for r in whole], "points": max_points,
                    "receivers": {r: (p if r in {str(w) for w in whole} else None) for r, p in parts.items()},
                    "chunks": [{"start": s, "stop": e, "points": max_points, "receivers": [str(r) for r in whole]}]})
        return out
    step, auto, raised, lowered = _step(total * 1000.0, asked, max_points)
    out.update({"mode": "sweep", "step_khz": step, "step_auto": auto, "step_raised": raised,
                "step_lowered": lowered, "step_asked_khz": None if auto else float(asked),
                "bins": int(total * 1000.0 / step)})
    if raised:
        out["step_note"] = (f"The step was raised from {_f(asked)} to {_f(step)} kHz: {_f(asked)} kHz across "
                            f"{_f(total)} MHz would be {int(total * 1000.0 / float(asked)):,} points in one sweep.")
    elif lowered:
        out["step_note"] = (f"The step was lowered from {_f(asked)} to {_f(step)} kHz: {_f(asked)} kHz would leave "
                            f"fewer than {MIN_SWEEP_BINS} points across {_f(total)} MHz.")
    for lo, hi in covered:
        for a, b in cut(lo, hi, max_chunk_mhz):
            pts = max(MIN_CHUNK_POINTS, int(round(max_points * (b - a) / total)))
            who = [r for r, p in parts.items() if p and p[0] < b - 1e-9 and p[1] > a + 1e-9]
            out["chunks"].append({"start": a, "stop": b, "points": pts, "receivers": who})
    out["points"] = sum(c["points"] for c in out["chunks"])
    return out


def _or_list(items) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


def gap_note(pl, names=None, ranges=None) -> str:
    """One plain sentence about the parts of the view that cannot be shown; "" when all of it is tuned.
    names = {receiver id: its name on the page}; with exactly one receiver the sentence names it and its range."""
    if not pl.get("gaps"):
        return ""
    ids = list(pl.get("receivers") or {})
    if not ids:
        return "No receiver is set up here, so there is nothing to show."
    s, e = pl["start_mhz"], pl["stop_mhz"]
    if len(ids) == 1 and ranges and ids[0] in ranges:
        a, b = float(ranges[ids[0]][0]), float(ranges[ids[0]][1])
        name = (names or {}).get(ids[0]) or "This receiver"
        below, above = s < a - 1e-9, e > b + 1e-9
        if pl.get("mode") == "none":
            where = "here"
        elif below and above:
            where = "outside that"
        else:
            where = "below that" if below else "above that"
        return f"{name} tunes {span_text(a, b)}, so there is nothing to show {where}."
    return f"No receiver here tunes {_or_list(span_text(lo, hi) for lo, hi in pl['gaps'])}."
