"""Spectrum tab API. The GUI drives the dongles directly through the ARIS engine, with the same dongle locks as
the console: a frame waits briefly for, or politely skips, a dongle that ARIS is already using.

Each dongle's trace comes with its own noise floor and a health check, because the antennas differ: comparing raw
dB between dongles mostly shows gain and antenna differences, so the page can plot "dB above each dongle's own
floor" instead.

Levels are true dBFS (0 dB = an 8-bit full-scale sine, so every level sits at or below 0; not a calibrated
strength). rtl_power's own numbers have no full-scale reference and can be positive (v4.8.9 plotted +10 to +15 dB
for stations on Dongle 2): `arislib.rtlpower.fullscale_db` works the reference out from rtl_power's arithmetic,
and `_frame` subtracts it. Live IQ frames are referred to the same full-scale sine. Only the display converts:
baselines, thresholds and the engine's sweeps keep rtl_power's raw numbers. Other receivers' sweeps are on their
own scale (`arislib.radios.sweep_unit`: calibrated dBm for a Signal Hound, the receiver's own dB for SoapySDR) and
are shown as they are, with that unit; a comparison of receivers on different scales says "mixed" (the page then
reads best in dB above floor).

The view may be any range from 0 to 6000 MHz, whether or not a receiver here tunes it (`arislib.specplan`): each
receiver is asked only for the part it tunes, a part wider than the engine's widest sweep is swept in pieces, and
what no receiver tunes comes back as a gap with a plain note. Nothing is ever returned as a level that no receiver
measured; a view nothing tunes opens no radio at all.
"""
from __future__ import annotations

import math
import time

from flask import Blueprint, jsonify, request

from arislib import radios as _radios
from arislib import rtlpower as _rp
from arislib import specplan as _sp

from .auth import api_login_required
from .engine import engine as _engine, version

bp = Blueprint("spectrum", __name__)

SPECTRUM_PRESETS = [
    {"id": "full", "name": "Full 0-6 GHz", "start": _sp.VIEW_MIN_MHZ, "stop": _sp.VIEW_MAX_MHZ},
    {"id": "fm", "name": "FM 88-108", "start": 88.0, "stop": 108.0},
    {"id": "433", "name": "433 MHz", "start": 433.0, "stop": 435.0},
    {"id": "915", "name": "915 MHz", "start": 902.0, "stop": 928.0},
    {"id": "air", "name": "Airband", "start": 118.0, "stop": 137.0},
    {"id": "2m", "name": "2 m ham", "start": 144.0, "stop": 148.0},
    {"id": "vhf", "name": "VHF 148-174", "start": 148.0, "stop": 174.0},
    {"id": "uhf", "name": "UHF 450-470", "start": 450.0, "stop": 470.0},
    {"id": "pager", "name": "Pagers", "start": 929.0, "stop": 932.0},
    {"id": "adsb", "name": "ADS-B", "start": 1085.0, "stop": 1095.0},
    {"id": "live433", "name": "Live 433.92", "start": 433.52, "stop": 434.32},
    {"id": "live915", "name": "Live 915.0", "start": 914.6, "stop": 915.4},
    {"id": "phones", "name": "Phones 700-900", "start": 698.0, "stop": 894.0},
    {"id": "gnss", "name": "GPS 1575", "start": 1565.0, "stop": 1585.0},
    # above an RTL-SDR's reach: listed all the same, and api_meta says whether a receiver here tunes them
    {"id": "phones2", "name": "Phones 1.7-1.9 GHz", "start": 1710.0, "stop": 1910.0},
    {"id": "ism24", "name": "2.4 GHz", "start": 2400.0, "stop": 2483.5},
    {"id": "wifi5", "name": "5 GHz Wi-Fi", "start": 5150.0, "stop": 5350.0},
    {"id": "ism58", "name": "5.8 GHz", "start": 5725.0, "stop": 5875.0},
]
LIVE_MAX_SPAN_MHZ = 0.8          # one IQ capture: the window sits 0.05-0.85 MHz below the tuning point, so the
LIVE_TUNE_ABOVE_MHZ = 0.05       # receiver's own centre spike is always just outside it
LIVE_FS = 2048000                # ... at this sample rate; a receiver that cannot stream it sweeps narrow views instead
MAX_POINTS = 1600
COARSE_STEP_KHZ = 200.0          # the widest step the automatic choice uses. A coarser one (the user's own choice)
                                 # leaves a hop only a few points (one point per hop at 1000 kHz), too few to tell a
                                 # receiver's own spike from a signal: there nothing is filled in, every point is
                                 # shown as measured, and the signal finder is not told to skip the hop centres.
SPIKE_MAX_DB = 12.0              # above the fine steps (30 kHz) a hop's spike is its centre point standing at most
                                 # this far over its surroundings; anything stronger there is a signal and stays.
                                 # Set from the simulated receiver (10 dB): one real sweep at 100-200 kHz must show
                                 # how high a real spike stands.
FLAT_DB = 4.0                    # a trace whose strongest point is less than this above its floor hears nothing


def _nice_step_khz(span_mhz):
    """The automatic step for a sweep this wide (about 1500 points across it)."""
    return _sp._step(span_mhz * 1000.0, None, MAX_POINTS)[0]


def _pool(freqs, db, n):
    """Max-pool to at most n points so narrow peaks survive the downsampling."""
    if len(db) <= n:
        return list(freqs), list(db)
    k = int(math.ceil(len(db) / float(n)))
    fs, ds = [], []
    for i in range(0, len(db), k):
        seg = db[i:i + k]
        j = max(range(len(seg)), key=seg.__getitem__)
        fs.append(freqs[i + j])
        ds.append(seg[j])
    return fs, ds


def _fill_hop_spikes(freqs, db, hops, step_khz, guard, core_mhz=None, coarse=False):
    """rtl_power leaves a narrow false spike at the centre of every hop. Fill a spike in only when it stands
    alone; if signal energy reaches out past the spike core, a real signal sits there and is kept.
    freqs must be sorted (they are): each hop only looks at the few bins around it (binary search).
    core_mhz: how far from a hop's centre the spike reaches (1.5 steps unless given).
    coarse: the step leaves a hop only a handful of points, so the core is a large part of it. Then only the
    hop's own centre point, standing at most SPIKE_MAX_DB over its surroundings, counts as a spike: a stronger
    point, or a strongest point beside the centre, is a signal and every point there stays as it was measured.
    (A weak signal exactly on a hop centre cannot be told from the spike and is still filled in.)"""
    import numpy as np
    f = np.asarray(freqs, dtype=np.float64)
    v = np.asarray(db, dtype=np.float64)
    core_w = 1.5 * step_khz / 1000.0 if core_mhz is None else float(core_mhz)
    cleaned = 0
    for h in sorted(hops):
        lo = int(np.searchsorted(f, h - 2.0 * guard, side="left"))
        hi = int(np.searchsorted(f, h + 2.0 * guard, side="right"))
        if hi <= lo:
            continue
        d = np.abs(f[lo:hi] - h)
        seg = v[lo:hi]
        core = d <= core_w
        ring = seg[(d > guard) & (d <= 2.0 * guard)]
        shoulder = seg[(d > core_w) & (d <= guard)]
        if not core.any() or not len(ring):
            continue
        base = float(np.median(ring))
        if len(shoulder) and float(shoulder.max()) - base > 6.0:
            continue
        top = float(seg[core].max()) - base
        if coarse and (top > SPIKE_MAX_DB or int(np.argmax(np.where(core, seg, -np.inf))) != int(np.argmin(d))):
            continue
        if top > 2.0:
            seg[core] = base
            cleaned += 1
    return [round(float(x), 2) for x in v], cleaned


def _live_frame(eng, serial, s_mhz, e_mhz, gain):
    """One direct IQ capture of a narrow view: (freqs, dBFS, step_khz)."""
    import numpy as np
    fs = LIVE_FS                                         # every receiver's IQ arrives as 8-bit CU8
    centre = e_mhz + LIVE_TUNE_ABOVE_MHZ
    with eng._use_dongle(serial, timeout=3, holder="spectrum view"):
        z = eng._capture_iq(serial, int(round(centre * 1e6)), fs, 262144, gain, timeout=10)
    nfft = 4096 if e_mhz - s_mhz > 0.3 else 8192
    f, P = eng._psd(z, fs, nfft)
    # dBFS: _psd windows each segment with a Hann window, so a full-scale sine (127/128 after _capture_iq's
    # scaling) on one bin gives |X|^2 = (127/128 * sum(window))^2
    fs_lin = (127.0 / 128.0 * float(np.sum(eng._hann32(nfft)))) ** 2
    db = 10.0 * np.log10(P / fs_lin + 1e-20)
    freqs = centre + f / 1e6
    keep = (freqs >= s_mhz) & (freqs <= e_mhz)
    return ([round(float(x), 5) for x in freqs[keep]], [round(float(x), 1) for x in db[keep]], fs / nfft / 1000.0)


def _hops_spike(serial):
    """Does this receiver's sweep come from rtl_power or its kin, which hop across the range and leave a false
    spike at the centre of every hop? A receiver that sweeps through its own helper program does not."""
    return not isinstance(_radios.backend_for(serial), getattr(_radios, "HelperBackend", ()))


def _sweep_piece(eng, serial, s_mhz, e_mhz, step_khz, gain):
    """One rtl_power pass over s-e (at most the engine's widest sweep): (freqs, levels, hop centres, spikes filled).
    RTL-SDR levels come back in dBFS; other receivers' on their own scale, as measured."""
    # rtl_power's single pass ends at the next whole second: "-i 1" can mean one 3 ms look per hop, in which an
    # FM station's bass swings it tens of kHz off its channel. Where the FM channel plan is checked, integrate
    # 1-2 s (several rounds per hop) so stations sit on their channels.
    fm_check = s_mhz < eng.FM_BROADCAST[1] and e_mhz > eng.FM_BROADCAST[0] and \
        step_khz <= eng.FM_PLAN_MAX_STEP_KHZ
    acc, hop_hz = eng._rtl_power(s_mhz, e_mhz, step_khz, gain, serial, integration_s=2 if fm_check else 1,
                                 lock_timeout=3, holder="spectrum view")
    hops = [h / 1e6 for h in hop_hz]
    pts = sorted((round(hz / 1e6, 4), sum(v) / len(v)) for hz, v in acc.items())
    pts = [(f, v) for f, v in pts if s_mhz - 1e-6 <= f <= e_mhz + 1e-6]
    freqs, db = [f for f, _ in pts], [v for _, v in pts]
    cleaned = 0
    if not _hops_spike(serial):                  # a receiver with its own sweep: every point is as it measured it
        hops = []
    elif db and step_khz <= COARSE_STEP_KHZ:     # a coarser step: nothing is filled in (see COARSE_STEP_KHZ)
        db, cleaned = _fill_hop_spikes(freqs, db, hops, step_khz, eng._guard_mhz(step_khz),
                                       coarse=step_khz > eng.FM_PLAN_MAX_STEP_KHZ)
    if _radios.sweep_unit(serial) == "dBFS":     # rtl_power's raw numbers, referred to an 8-bit full-scale sine
        fs_db = _sweep_fullscale_db(eng, s_mhz, e_mhz, step_khz)
        db = [round(v - fs_db, 1) for v in db]
    else:                                        # calibrated dBm, or the receiver's own scale: shown as they are
        db = [round(v, 1) for v in db]
    return freqs, db, hops, cleaned


def _frame(eng, serial, s_mhz, e_mhz, gain, step_khz=None, points=MAX_POINTS, live=None):
    """What one receiver measures between s and e MHz (a range it tunes). gain is a number, or a function of
    (low MHz, high MHz) for sweeps that cross bands with their own gain. A range wider than the engine's widest
    sweep is swept in pieces, one after another, and joined."""
    span = e_mhz - s_mhz
    gain_of = gain if callable(gain) else (lambda lo, hi: gain)
    if live is None:
        live = span <= LIVE_MAX_SPAN_MHZ + 1e-6
    hops, cleaned, gains = [], 0, []
    if live:
        mode, unit = "live", "dBFS"
        gains.append(gain_of(s_mhz, e_mhz))
        freqs, db, step_khz = _live_frame(eng, serial, s_mhz, e_mhz, gains[0])
    else:
        mode, unit = "sweep", _radios.sweep_unit(serial)
        step_khz = float(step_khz or _nice_step_khz(span))
        freqs, db = [], []
        for lo, hi in _sp.cut(s_mhz, e_mhz, eng.MAX_SPAN_MHZ):
            g = gain_of(lo, hi)
            f, v, h, c = _sweep_piece(eng, serial, lo, hi, step_khz, g)
            if freqs:                            # the point two pieces share is kept once
                k = next((i for i, x in enumerate(f) if x > freqs[-1] + 1e-9), len(f))
                f, v = f[k:], v[k:]
            freqs += f
            db += v
            hops += h
            cleaned += c
            if g not in gains:
                gains.append(g)
    if not db:
        raise eng.RFError(f"{eng._short(serial)} gave no points between {s_mhz:g} and {e_mhz:g} MHz.")
    floor = eng._robust_floor(db)
    raw = eng._group_broadcast(_signals(eng, freqs, db, step_khz, floor,
                                        hops if mode == "sweep" and hops and step_khz <= COARSE_STEP_KHZ else None),
                               step_khz)
    top = max(db)
    freqs, db = _pool(freqs, db, max(_sp.MIN_CHUNK_POINTS, int(points)))
    return {"mode": mode, "freqs": freqs, "db": db, "floor_db": floor, "max_db": top, "_raw_peaks": raw,
            "step_khz": round(step_khz, 3), "spikes_removed": cleaned, "unit": unit,
            "spikes_kept": mode == "sweep" and step_khz > COARSE_STEP_KHZ and _hops_spike(serial),
            "range_mhz": [s_mhz, e_mhz], "gain_db": gains[0] if len(gains) == 1 else " to ".join(f"{g}" for g in gains)}


DIP_DB = 4.0                     # a dip this far below the tops on both sides parts two FM stations (coarse steps)


def _valleys(vals, prom_db=DIP_DB):
    """The lowest point of every dip that lies at least prom_db below the tops on both sides (indexes)."""
    out, top, low = [], (vals[0] if vals else 0.0), None
    for i in range(1, len(vals)):
        v = vals[i]
        if low is None:
            if v > top:
                top = v
            elif v <= top - prom_db:
                low = i
        elif v < vals[low]:
            low = i
        elif v >= vals[low] + prom_db:
            out.append(low)
            top, low = v, None
    return out


def _signals(eng, freqs, db, step_khz, floor, hops):
    """The signals in one receiver's points. Points of one signal join when they are neighbours: 35 kHz apart, or
    one step where the step is wider than that (else every point of a strong signal in a wide view counts as a
    signal of its own). With that wider rule two FM stations a few points apart were chained into one entry through
    the points between them, so in the FM broadcast band the lowest point of a clear dip between two tops joins
    neither side. A receiver's own spike at a hop centre is no top: its points count as the lower of the points
    either side of it, so a spike left beside a station stays part of that station's entry."""
    gap = max(35.0, 1.25 * step_khz)
    bins = dict(zip(freqs, db))
    if gap > 35.0 and freqs and freqs[0] <= eng.FM_BROADCAST[1] and freqs[-1] >= eng.FM_BROADCAST[0]:
        fm = [i for i, f in enumerate(freqs) if eng._in_fm_band(f)]
        vals = [db[i] for i in fm]
        core = 1.5 * step_khz / 1000.0
        spike = [any(abs(freqs[i] - h) <= core for h in hops or ()) for i in fm]
        k = 0
        while k < len(fm):                       # each run of spike points takes the lower level beside it
            if not spike[k]:
                k += 1
                continue
            e = k
            while e < len(fm) and spike[e]:
                e += 1
            side = [vals[x] for x in (k - 1, e) if 0 <= x < len(fm)]
            for x in range(k, e):
                vals[x] = min([vals[x]] + side)
            k = e
        for k in _valleys(vals):
            bins.pop(freqs[fm[k]], None)
    return eng._extract_peak_clusters(bins, 6.0, gap, step_khz, floor=floor, hops=hops)[1]


def _sweep_fullscale_db(eng, s_mhz, e_mhz, step_khz):
    """What rtl_power prints for a full-scale sine in this sweep (its rate, FFT size and window), from the same
    arguments _rtl_power gives it."""
    cfg = eng._load_config()
    crop = _rp.parse_crop(cfg.get("crop_percent", "20%"), 0.2)
    return _rp.sweep_fullscale_db(int(s_mhz * 1e6), int(e_mhz * 1e6), int(step_khz * 1e3), crop,
                                  cfg.get("window_fn", "hamming"))


def _peak_list(merged):
    """Peaks for the page: off-plan FM signals first (a weak FM bug must never be crowded out by strong stations),
    then everything else by SNR."""
    out = []
    flagged = lambda p: bool(p.get("channel_mhz")) and p.get("on_standard_fm_channel") is False
    for p in sorted(merged, key=lambda x: (not flagged(x), -x["snr_db"]))[:40]:
        ch, chk = p.get("channel_mhz"), p.get("checked")
        mhz = ch or p["frequency_mhz"]
        if ch and p.get("known_station"):        # its RDS name was decoded here: a known public station
            kind, label = "fm", f"{ch:.1f} {p['known_station']}"
            band = (f"FM station, RDS scrolling text \"{p['known_station']}\"" if p.get("name_scrolls") else
                    f"FM station \"{p['known_station']}\" (RDS)")
        elif ch:
            if p.get("on_standard_fm_channel") is False:
                kind, label, band = "fm_offplan", f"{ch:.1f} off-plan", "FM band, not a station channel"
                if chk == "off_channel" and p.get("measured_carrier_mhz"):    # the IQ check measured the carrier
                    mhz = float(p["measured_carrier_mhz"])
                    label, band = f"{mhz:.3f} off-plan", f"Not a station channel (checked: carrier at {mhz:.3f} MHz)"
            else:
                kind, label = "fm", f"{ch:.1f} FM"
                when = str(p.get("probable_allocation") or "")
                when = when[when.rfind(", ") + 2:-1] if chk in ("not_heard", "too_weak") and when.endswith(")") else ""
                band = {"no_name": "Station channel: no RDS name read in the last check",
                        "rds_weak": "Station channel, checked: RDS too weak to read its name",
                        "not_heard": f"Station channel, checked: not heard in the last check ({when})",
                        "too_weak": f"Station channel, checked: too weak here to read in the last check ({when})"}.get(chk) or (
                    "FM band, station channel" if p.get("on_standard_fm_channel") else
                    "FM band, weak: the sweep cannot place it on a channel (Decode measures it)" if p.get("fm_weak")
                    else "FM band (zoom to 88-108 to check the channel)")
        else:
            kind, label = "signal", f"{p['frequency_mhz']:.3f}"
            band = str(p.get("probable_allocation") or "").split(" (")[0]
        out.append({"mhz": round(mhz, 4), "lo_mhz": p.get("_lo"), "hi_mhz": p.get("_hi"),
                    "snr_db": p["snr_db"], "label": label, "band": band, "kind": kind,
                    "flag": kind == "fm_offplan", "best": p.get("strongest_dongle"),
                    "heard_by": p.get("heard_by") or {}, "name": p.get("known_station"), "checked": chk,
                    "checked_epoch": p.get("checked_epoch")})
    return out


def _health(traces, span_mhz):
    """Warn about a dongle that looks dead or deaf here: a flat trace while the others hear strong signals.
    Possible causes are named; the trace alone cannot tell which."""
    ok = [t for t in traces if "db" in t]
    for t in ok:
        t["health"] = "ok"
        t["above_floor_max_db"] = round(t["max_db"] - t["floor_db"], 1) if t.get("max_db") is not None else None
    warnings = []
    # Only receivers that looked at the same range are compared: a signal outside one receiver's range says
    # nothing about that receiver.
    same = len({tuple(t.get("range_mhz") or ()) for t in ok}) <= 1
    loud = [t for t in ok if (t.get("above_floor_max_db") or 0) >= 15 and t.get("peaks", 0) >= 1] if same else []
    flat = []
    for t in ok:
        if (t.get("above_floor_max_db") or 0) < FLAT_DB and span_mhz >= 1.5:
            if loud:
                t["health"] = "flat"
                flat.append(t["name"])
            elif len(ok) == 1:
                t["health"] = "quiet"
    if flat:
        many = len(flat) > 1
        warnings.append(f"{' and '.join(flat)} show{'' if many else 's'} a flat line here while "
                        f"{' and '.join(x['name'] for x in loud)} hear{'' if len(loud) > 1 else 's'} strong signals. "
                        f"{'Their antennas' if many else 'Its antenna'} may be loose, unplugged or not suited to this "
                        f"range, or the dongle may need replugging. Test: unplug the antenna; if the line does not "
                        f"change, that dongle is not receiving. Run setup to retest.")
    for t in traces:
        if "error" in t:
            t["health"] = "busy" if any(w in t["error"].lower() for w in ("busy", "occupied")) else "error"
    return warnings


def _here(eng, cfg):
    """The receivers present (every one when none is marked present)."""
    return [s for s in eng.DONGLE_NAMES if eng._dongle_cfg(s, cfg).get("present") is not False] or \
        list(eng.DONGLE_NAMES)


def _ranges(serials):
    """{receiver: (lowest MHz, highest MHz) it tunes}."""
    return {s: tuple(float(x) for x in _radios.backend_for(s).freq_range_mhz) for s in serials}


def _live_ok(serial):
    """Can this receiver make the direct capture of a narrow view (some stream IQ too slowly for it)?"""
    rate = getattr(_radios.backend_for(serial), "max_iq_rate", None)
    return not rate or float(rate) >= LIVE_FS


def _plan_for(eng, cfg, args):
    """(plan, receivers asked) for a request's dongle, start, stop, step and points. The plan carries the plain
    note about what cannot be shown and names the receivers that tune none of the view. ValueError in plain
    words for a bad request."""
    want = str(args.get("dongle") or "all").strip().lower()
    # a name no receiver has is refused (it must not fall back to another receiver, or to one with no name)
    serials = _here(eng, cfg) if want == "all" else [eng._resolve_dongle_serial(want, "")]
    if want != "all" and serials[0] not in eng.DONGLE_NAMES:
        raise ValueError("No receiver here has that name.")
    pts = args.get("points")
    if pts in (None, ""):
        pts = MAX_POINTS
    else:
        try:
            pts = int(str(pts).strip())
        except ValueError:
            raise ValueError("The number of points must be a whole number.")
        if not _sp.MIN_CHUNK_POINTS <= pts <= MAX_POINTS:
            raise ValueError(f"The number of points must be between {_sp.MIN_CHUNK_POINTS} and {MAX_POINTS}.")
    rbw = args.get("step_khz") or args.get("rbw") or None
    ranges = _ranges(serials)
    pl = _sp.plan(args.get("start"), args.get("stop"), ranges, LIVE_MAX_SPAN_MHZ, rbw_khz=rbw, max_points=pts,
                  max_chunk_mhz=eng.MAX_SPAN_MHZ, live_receivers=[s for s in serials if _live_ok(s)])
    names = {s: eng._short(s) for s in serials}
    pl["gap_note"] = _sp.gap_note(pl, names, ranges)
    used = pl["live_receivers"] if pl["mode"] == "live" else [s for s in serials if pl["receivers"].get(s)]
    # left out: "range" = it tunes none of the view; "live" = it tunes it, but cannot make the fast narrow capture
    # the others are making (chosen on its own, it sweeps the same view)
    view = (pl["start_mhz"], pl["stop_mhz"])
    pl["left_out"] = [{"dongle": s, "name": names[s], "range_mhz": list(ranges[s]),
                       "why": "live" if _sp._part(view[0], view[1], ranges[s]) else "range"}
                      for s in serials if s not in used]
    pl["dongle"] = "all" if want == "all" else serials[0]
    pl["dongle_name"] = "All dongles" if want == "all" else names[serials[0]]
    return pl, serials


def frames(eng, serials, s_mhz, e_mhz, gain_arg, cfg, pl=None):
    """One frame per receiver, captured at the same time, plus a combined (strongest) trace and merged peaks.
    Each receiver is asked only for the part of the view it tunes (the plan's); one that tunes none is left out."""
    if pl is None:
        pl = _sp.plan(s_mhz, e_mhz, _ranges(serials), LIVE_MAX_SPAN_MHZ, max_chunk_mhz=eng.MAX_SPAN_MHZ,
                      live_receivers=[s for s in serials if _live_ok(s)])
    live = pl["mode"] == "live"
    use = [s for s in serials if (s in pl["live_receivers"] if live else pl["receivers"].get(s))]
    if not use:
        raise eng.RFError("No receiver here tunes this range.")
    total = max(pl["covered_mhz"], 1e-9)

    def one(ser):
        lo, hi = (s_mhz, e_mhz) if live else pl["receivers"][ser]
        if gain_arg in ("", "auto", None):
            gain = lambda a, b: eng._gain_for(ser, a, b, cfg)       # noqa: E731
        else:
            gain = eng._snap_gain(gain_arg)
        share = pl["points"] if live else int(round(pl["points"] * (hi - lo) / total))
        return _frame(eng, ser, lo, hi, gain, step_khz=pl["step_khz"], points=min(MAX_POINTS, share), live=live)
    res = eng._parallel(one, use)
    traces, ok, errors = [], {}, []
    for ser in use:
        r, name = res[ser], eng._short(ser)
        note = eng._dongle_cfg(ser, cfg).get("antenna_note") or ""
        if isinstance(r, Exception):
            traces.append({"dongle": ser, "name": name, "antenna": note, "error": str(r)[:160]})
            errors.append(r)
            continue
        ok[ser] = r
        traces.append({"dongle": ser, "name": name, "antenna": note, "freqs": r["freqs"], "db": r["db"],
                       "floor_db": r["floor_db"], "max_db": r["max_db"], "gain_db": r["gain_db"], "unit": r["unit"],
                       "range_mhz": r["range_mhz"],
                       "peaks": sum(1 for p in r["_raw_peaks"] if p["snr_db"] >= 10)})
    if not ok:
        raise errors[0]
    first = next(iter(ok.values()))
    same = all(len(r["db"]) == len(first["db"]) and r["range_mhz"] == first["range_mhz"] for r in ok.values())
    combined = [max(v) for v in zip(*(r["db"] for r in ok.values()))] if same else first["db"]
    merged = eng._merge_peaks({ser: r["_raw_peaks"] for ser, r in ok.items()})
    floors = [r["floor_db"] for r in ok.values() if r["floor_db"] is not None]
    plan_checked = first["step_khz"] <= eng.FM_PLAN_MAX_STEP_KHZ
    warnings = _health(traces, pl["covered_mhz"])
    units = {r["unit"] for r in ok.values()}
    unit = units.pop() if len(units) == 1 else "mixed"   # receivers on different scales: compare above floor
    for t in traces:
        t.pop("max_db", None)
    return {"mode": first["mode"], "freqs": first["freqs"], "db": combined, "floor_db": min(floors) if floors else None,
            "traces": traces, "peaks": _peak_list(merged), "step_khz": first["step_khz"],
            "fm_plan_checked": plan_checked, "warnings": warnings,
            "spikes_removed": sum(r["spikes_removed"] for r in ok.values()), "unit": unit,
            "spikes_kept": any(r["spikes_kept"] for r in ok.values()),
            "gain_db": " / ".join(f"{t['gain_db']}" for t in traces if "gain_db" in t)}


def _nothing():
    """The answer for a view no receiver here tunes: no trace, no level, no signal. Nothing was measured."""
    return {"mode": "none", "freqs": [], "db": [], "floor_db": None, "traces": [], "peaks": [], "step_khz": None,
            "fm_plan_checked": False, "warnings": [], "spikes_removed": 0, "spikes_kept": False, "unit": None,
            "gain_db": ""}


def _plan_fields(pl):
    return {k: pl[k] for k in ("covered", "gaps", "gap_note", "left_out", "step_note", "step_raised",
                               "step_lowered", "step_auto")}


@bp.route("/api/spectrum/meta")
@api_login_required
def api_meta():
    try:
        eng = _engine()
        cfg = eng._load_config()
        labels = {"433-435": "433", "902-928": "915", "88-108": "FM"}
        here = _here(eng, cfg)
        ranges = _ranges(list(eng.DONGLE_NAMES))
        dongles = []
        for ser in eng.DONGLE_NAMES:
            best = [labels.get(b, b) for b, x in cfg.get("band_dongles", {}).items() if x == ser]
            dongles.append({"serial": ser, "name": eng._short(ser), "best_for": best,
                            "antenna_note": eng._dongle_cfg(ser, cfg).get("antenna_note"),
                            "range_mhz": list(ranges[ser]), "present": ser in here})
        extra = [p for p in getattr(eng, "PLUGIN_PRESETS", []) if p["id"] not in {q["id"] for q in SPECTRUM_PRESETS}]
        presets = []
        for p in SPECTRUM_PRESETS + extra:       # every preset is listed; "tuned" says whether a receiver here
            lo, hi = float(p["start"]), float(p["stop"])                       # tunes all of it
            parts = [(max(lo, ranges[s][0]), min(hi, ranges[s][1])) for s in here]
            presets.append(dict(p, tuned=any(a <= lo + 1e-9 and hi <= b + 1e-9 for a, b in parts),
                                tuned_part=any(b - a >= _sp.MIN_SPAN_MHZ for a, b in parts)))
        return jsonify({"dongles": dongles, "default": "all", "presets": presets,
                        "live_max_span_mhz": LIVE_MAX_SPAN_MHZ, "view_min_mhz": _sp.VIEW_MIN_MHZ,
                        "view_max_mhz": _sp.VIEW_MAX_MHZ, "min_span_mhz": _sp.MIN_SPAN_MHZ,
                        "steps_khz": list(_sp.STEP_CHOICES_KHZ), "max_points": MAX_POINTS, "version": version()})
    except Exception as e:
        return jsonify({"error": f"ARIS engine not available: {type(e).__name__}: {e}"}), 500


@bp.route("/api/spectrum/plan")
@api_login_required
def api_plan():
    """What the chosen receivers can show of a view and the pieces to sweep in turn. Opens no radio."""
    try:
        eng = _engine()
    except Exception as e:
        return jsonify({"error": f"ARIS engine not available: {type(e).__name__}: {e}"}), 500
    try:
        pl, _ = _plan_for(eng, eng._load_config(), request.args)
    except (ValueError, eng.RFError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    return jsonify(pl)


@bp.route("/api/spectrum")
@api_login_required
def api_spectrum():
    try:
        eng = _engine()
    except Exception as e:
        return jsonify({"error": f"ARIS engine not available: {type(e).__name__}: {e}"}), 500
    t0 = time.time()
    try:
        cfg = eng._load_config()
        pl, serials = _plan_for(eng, cfg, request.args)
        s_mhz, e_mhz = pl["start_mhz"], pl["stop_mhz"]
        if pl["mode"] == "none":                 # nothing here tunes any of it: no radio is opened
            out = _nothing()
        else:
            out = frames(eng, serials, s_mhz, e_mhz, request.args.get("gain", "auto"), cfg, pl)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except eng.RFError as e:
        msg = str(e)
        busy = any(w in msg.lower() for w in ("busy", "occupied"))
        return jsonify({"error": msg, "busy": busy}), 409 if busy else 400
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    used = [t["dongle"] for t in out["traces"]]
    out.update(_plan_fields(pl))
    out.update({"dongle": "all" if len(used) > 1 else (used[0] if used else pl["dongle"]),
                "dongle_name": "All dongles" if len(used) > 1 else (eng._short(used[0]) if used else pl["dongle_name"]),
                "start_mhz": s_mhz, "stop_mhz": e_mhz, "took_ms": int((time.time() - t0) * 1000),
                "time": time.strftime("%H:%M:%S")})
    return jsonify(out)


@bp.route("/api/fm_known")
@api_login_required
def api_fm_known():
    """The FM stations ARIS has checked (fm_stations.json), so the Spectrum list picks up names read since its last
    sweep: by a capture, a Decode, the chat or Name all in another browser. ?have=<tag> answers `unchanged`."""
    eng = _engine()
    try:
        st = eng.FM_STATIONS.stat()
        tag = f"{st.st_mtime_ns}-{st.st_size}"
    except OSError:
        return jsonify({"tag": "none", "named": {}, "checked": []})
    if request.args.get("have") == tag:
        return jsonify({"unchanged": True, "tag": tag})
    named, checked = {}, []
    for ch, x in eng._known_stations().items():
        if not isinstance(x, dict):
            continue
        if x.get("name"):
            named[ch] = {"name": x["name"], "names": x.get("names"), "pi": x.get("pi"), "scrolls": x.get("scrolls"),
                         "text": x.get("text")}
        elif eng._unheard_state(x):
            state, epoch, when = eng._unheard_state(x)
            checked.append({"channel_mhz": float(ch), "state": state, "epoch": epoch,
                            "band": f"Checked: {eng._UNHEARD_WORDS[state]} ({str(when or '')[:16]})"})
        elif x.get("on_channel") is False and x.get("carrier_mhz"):
            checked.append({"channel_mhz": float(ch), "state": "off_channel", "carrier_mhz": x["carrier_mhz"],
                            "band": f"Not a station channel (checked: carrier at {float(x['carrier_mhz']):.3f} MHz)"})
        elif x.get("on_channel"):
            state = "rds_weak" if x.get("rds") else "no_name"
            checked.append({"channel_mhz": float(ch), "state": state, "epoch": x.get("epoch"),
                            "band": eng._NAME_BAND[state] + (f" ({str(x['time'])[:16]})" if state == "no_name" and
                                                              x.get("time") else "")})
    return jsonify({"tag": tag, "named": named, "checked": checked})
