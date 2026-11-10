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
"""
from __future__ import annotations

import math
import time

from flask import Blueprint, jsonify, request

from arislib import radios as _radios
from arislib import rtlpower as _rp

from .auth import api_login_required
from .engine import engine as _engine, version

bp = Blueprint("spectrum", __name__)

SPECTRUM_PRESETS = [
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
    # above an RTL-SDR's reach: offered only when a receiver here tunes them (api_meta)
    {"id": "phones2", "name": "Phones 1.7-1.9 GHz", "start": 1710.0, "stop": 1910.0},
    {"id": "ism24", "name": "2.4 GHz", "start": 2400.0, "stop": 2483.5},
    {"id": "wifi5", "name": "5 GHz Wi-Fi", "start": 5150.0, "stop": 5350.0},
    {"id": "ism58", "name": "5.8 GHz", "start": 5725.0, "stop": 5875.0},
]
LIVE_MAX_SPAN_MHZ = 0.8          # one IQ capture: the window sits 0.05-0.85 MHz below the tuning point, so the
LIVE_TUNE_ABOVE_MHZ = 0.05       # receiver's own centre spike is always just outside it
MAX_POINTS = 1600
FLAT_DB = 4.0                    # a trace whose strongest point is less than this above its floor hears nothing


def _nice_step_khz(span_mhz):
    want = span_mhz * 1000.0 / 1500.0
    for st in (2.5, 5.0, 10.0, 12.5, 25.0, 50.0, 100.0, 200.0, 500.0):
        if st >= want:
            return st
    return 1000.0


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


def _fill_hop_spikes(freqs, db, hops, step_khz, guard):
    """rtl_power leaves a narrow false spike at the centre of every hop. Fill a spike in only when it stands
    alone; if signal energy reaches out past the spike core, a real signal sits there and is kept.
    freqs must be sorted (they are): each hop only looks at the few bins around it (binary search)."""
    import numpy as np
    f = np.asarray(freqs, dtype=np.float64)
    v = np.asarray(db, dtype=np.float64)
    core_w = 1.5 * step_khz / 1000.0
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
        if float(seg[core].max()) - base > 2.0:
            seg[core] = base
            cleaned += 1
    return [round(float(x), 2) for x in v], cleaned


def _frame(eng, serial, s_mhz, e_mhz, gain):
    import numpy as np
    span = e_mhz - s_mhz
    if span <= LIVE_MAX_SPAN_MHZ + 1e-6:
        mode, fs, unit = "live", 2048000, "dBFS"         # every receiver's IQ arrives as 8-bit CU8
        centre = e_mhz + LIVE_TUNE_ABOVE_MHZ
        with eng._use_dongle(serial, timeout=3, holder="spectrum view"):
            z = eng._capture_iq(serial, int(round(centre * 1e6)), fs, 262144, gain, timeout=10)
        nfft = 4096 if span > 0.3 else 8192
        f, P = eng._psd(z, fs, nfft)
        # dBFS: _psd windows each segment with a Hann window, so a full-scale sine (127/128 after _capture_iq's
        # scaling) on one bin gives |X|^2 = (127/128 * sum(window))^2
        fs_lin = (127.0 / 128.0 * float(np.sum(eng._hann32(nfft)))) ** 2
        db = 10.0 * np.log10(P / fs_lin + 1e-20)
        freqs = centre + f / 1e6
        keep = (freqs >= s_mhz) & (freqs <= e_mhz)
        freqs, db = [round(float(x), 5) for x in freqs[keep]], [round(float(x), 1) for x in db[keep]]
        step_khz = fs / nfft / 1000.0
        cleaned = 0
    else:
        mode, step_khz = "sweep", _nice_step_khz(span)
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
        db, cleaned = _fill_hop_spikes(freqs, db, hops, step_khz, eng._guard_mhz(step_khz))
        unit = _radios.sweep_unit(serial)
        if unit == "dBFS":                   # rtl_power's raw numbers, referred to an 8-bit full-scale sine
            fs_db = _sweep_fullscale_db(eng, s_mhz, e_mhz, step_khz)
            db = [round(v - fs_db, 1) for v in db]
        else:                                # calibrated dBm, or the receiver's own scale: shown as they are
            db = [round(v, 1) for v in db]
    floor = eng._robust_floor(db) if db else None
    raw = []
    if db:
        _, pk = eng._extract_peak_clusters(dict(zip(freqs, db)), 6.0, 35.0, step_khz, floor=floor,
                                           hops=hops if mode == "sweep" else None)
        raw = eng._group_broadcast(pk, step_khz)
    top = max(db) if db else None
    freqs, db = _pool(freqs, db, MAX_POINTS)
    return {"mode": mode, "freqs": freqs, "db": db, "floor_db": floor, "max_db": top, "_raw_peaks": raw,
            "step_khz": round(step_khz, 3), "spikes_removed": cleaned, "unit": unit}


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
        out.append({"mhz": round(mhz, 4), "snr_db": p["snr_db"], "label": label, "band": band, "kind": kind,
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
    loud = [t for t in ok if (t.get("above_floor_max_db") or 0) >= 15 and t.get("peaks", 0) >= 1]
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


def frames(eng, serials, s_mhz, e_mhz, gain_arg, cfg):
    """One frame per dongle, captured at the same time, plus a combined (strongest) trace and merged peaks."""
    def one(ser):
        gain = eng._gain_for(ser, s_mhz, e_mhz, cfg) if gain_arg in ("", "auto", None) else eng._snap_gain(gain_arg)
        fr = _frame(eng, ser, s_mhz, e_mhz, gain)
        fr["gain_db"] = gain
        return fr
    res = eng._parallel(one, serials)
    traces, ok, errors = [], {}, []
    for ser in serials:
        r, name = res[ser], eng._short(ser)
        note = eng._dongle_cfg(ser, cfg).get("antenna_note") or ""
        if isinstance(r, Exception):
            traces.append({"dongle": ser, "name": name, "antenna": note, "error": str(r)[:160]})
            errors.append(r)
            continue
        ok[ser] = r
        traces.append({"dongle": ser, "name": name, "antenna": note, "freqs": r["freqs"], "db": r["db"],
                       "floor_db": r["floor_db"], "max_db": r["max_db"], "gain_db": r["gain_db"], "unit": r["unit"],
                       "peaks": sum(1 for p in r["_raw_peaks"] if p["snr_db"] >= 10)})
    if not ok:
        raise errors[0]
    first = next(iter(ok.values()))
    same = all(len(r["db"]) == len(first["db"]) for r in ok.values())
    combined = [max(v) for v in zip(*(r["db"] for r in ok.values()))] if same else first["db"]
    merged = eng._merge_peaks({ser: r["_raw_peaks"] for ser, r in ok.items()})
    floors = [r["floor_db"] for r in ok.values() if r["floor_db"] is not None]
    plan_checked = first["step_khz"] <= eng.FM_PLAN_MAX_STEP_KHZ
    warnings = _health(traces, e_mhz - s_mhz)
    units = {r["unit"] for r in ok.values()}
    unit = units.pop() if len(units) == 1 else "mixed"   # receivers on different scales: compare above floor
    for t in traces:
        t.pop("max_db", None)
    return {"mode": first["mode"], "freqs": first["freqs"], "db": combined, "floor_db": min(floors) if floors else None,
            "traces": traces, "peaks": _peak_list(merged), "step_khz": first["step_khz"],
            "fm_plan_checked": plan_checked, "warnings": warnings,
            "spikes_removed": sum(r["spikes_removed"] for r in ok.values()), "unit": unit,
            "gain_db": " / ".join(f"{t['gain_db']}" for t in traces if "gain_db" in t)}


@bp.route("/api/spectrum/meta")
@api_login_required
def api_meta():
    try:
        eng = _engine()
        cfg = eng._load_config()
        labels = {"433-435": "433", "902-928": "915", "88-108": "FM"}
        dongles = []
        for ser in eng.DONGLE_NAMES:
            best = [labels.get(b, b) for b, x in cfg.get("band_dongles", {}).items() if x == ser]
            dongles.append({"serial": ser, "name": eng._short(ser), "best_for": best,
                            "antenna_note": eng._dongle_cfg(ser, cfg).get("antenna_note")})
        extra = [p for p in getattr(eng, "PLUGIN_PRESETS", []) if p["id"] not in {q["id"] for q in SPECTRUM_PRESETS}]
        here = [s for s in eng.DONGLE_NAMES if eng._dongle_cfg(s, cfg).get("present") is not False] or \
            list(eng.DONGLE_NAMES)
        presets = [p for p in SPECTRUM_PRESETS + extra       # only bands some receiver here tunes
                   if not here or eng._covering(here, p["start"], p["stop"])]
        return jsonify({"dongles": dongles, "default": "all", "presets": presets,
                        "live_max_span_mhz": LIVE_MAX_SPAN_MHZ, "version": version()})
    except Exception as e:
        return jsonify({"error": f"ARIS engine not available: {type(e).__name__}: {e}"}), 500


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
        want = str(request.args.get("dongle") or "all").strip().lower()
        serials = (list(eng.DONGLE_NAMES) if want == "all"
                   else [eng._resolve_dongle_serial(want, cfg.get("sweeper_serial"))])
        s_mhz, e_mhz = eng._resolve_band(request.args.get("start"), request.args.get("stop"))
        eng._validate_band(s_mhz, e_mhz, 2.5)
        if want == "all":                    # only the receivers that tune this range (kinds differ)
            serials = eng._covering(serials, s_mhz, e_mhz) or serials
        out = frames(eng, serials, s_mhz, e_mhz, request.args.get("gain", "auto"), cfg)
    except eng.RFError as e:
        msg = str(e)
        busy = any(w in msg.lower() for w in ("busy", "occupied"))
        return jsonify({"error": msg, "busy": busy}), 409 if busy else 400
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    out.update({"dongle": "all" if len(serials) > 1 else serials[0],
                "dongle_name": "All dongles" if len(serials) > 1 else eng._short(serials[0]),
                "start_mhz": s_mhz, "stop_mhz": e_mhz, "took_ms": int((time.time() - t0) * 1000),
                "time": time.strftime("%H:%M:%S")})
    return jsonify(out)


@bp.route("/api/fm_known")
@api_login_required
def api_fm_known():
    """The FM stations ARIS has checked (fm_stations.json), so the Spectrum list picks up names read since its last
    sweep: by a capture, a Decode, the chat or Name all on another phone. ?have=<tag> answers `unchanged`."""
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
