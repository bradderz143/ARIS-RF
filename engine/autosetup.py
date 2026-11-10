"""ARIS engine, part "autosetup": The remaining sweep and settings tools (get_last_investigation, sweep_spectrum, assign_dongles,
set_antenna_note, set_timing_defaults), calibration and the first-run setup.

Executed into aris.py's namespace by aris._include (see aris.py): names from aris.py's imports and from the
parts before this one are available here without imports, and everything defined here becomes aris.<name>.
"""
from __future__ import annotations

@register("get_last_investigation", "The most recent single-frequency investigation or band scan in full, also one "
          "started from the Spectrum tab's Decode button. Use for 'explain the last investigation' or 'what did the "
          "decode find'.", {}, aliases=("last_investigation",))
def get_last_investigation():
    try:
        with open(LAST_REPORT) as f:
            rep = json.load(f)
    except (OSError, ValueError):
        raise RFError("No investigation has been saved yet.")
    if isinstance(rep, dict) and rep.get("final_signal_verdict"):
        rep["plain_summary"] = _investigation_text(rep)
    return rep


@register(
    "sweep_spectrum",
    "Quick sweep only (no decoding) with every dongle at once: noise floors and real signals, with how strongly "
    "each dongle hears each one. Receiver artifacts are filtered.",
    {**_BAND_PROPS, **_DONGLE_PROP,
     "sweep_seconds": {"type": "integer", "description": "How long to watch the band, in seconds. Set this "
                       "when the user asks for an N-second (or N-minute) sweep. 0 = quick 2 s pass (default)."},
     "threshold_db": {"type": "number", "description": "dB above noise floor to flag a signal (default 6)"}},
)
def sweep_spectrum(frequency_range=None, start_mhz=None, stop_mhz=None, dongle=None,
                   sweep_seconds=None, threshold_db=None, step_khz=None):
    cfg = _load_config()
    s, e = _resolve_band(start_mhz, stop_mhz, frequency_range)
    secs = _sweep_seconds_arg(sweep_seconds, cfg["default_sweep_seconds"])
    thresh = _sanitize_threshold(threshold_db, cfg["threshold_db"])
    if step_khz not in (None, ""):
        cfg["step_khz"] = _num(step_khz, "step_khz")
    sw = _multi_sweep(s, e, _pick_serials(dongle, cfg), secs, thresh, cfg)
    return {"source": "rtl_power (real hardware)", "band_mhz": [s, e], **sw["info"], **_fm_summary(sw["peaks"]),
            "dongles_used": sw["per_dongle"], "signals_detected": len(sw["peaks"]),
            "top_signals": [_public(p) for p in sw["peaks"][:12]], "rejected_artifacts": sw["artifacts"],
            **({"dongle_errors": sw["errors"]} if sw["errors"] else {})}


@register(
    "assign_dongles",
    "Change dongle routing: default_dongle (used when a band has no best dongle yet), investigator_dongles "
    "(used when no dongle is named; 'all' by default), or the best dongle for one band (band + band_dongle).",
    {"default_dongle": {"type": "string", "description": "'1', '2' or '3'"},
     "investigator_dongles": {"type": "string", "description": "e.g. 'all' or '1 and 2'"},
     "band": {"type": "string", "description": "A band like '433-435MHz' (with band_dongle)"},
     "band_dongle": {"type": "string", "description": "The dongle to prefer for that band"}},
)
def assign_dongles(default_dongle=None, investigator_dongles=None, band=None, band_dongle=None,
                   sweeper_dongle=None):
    with _CFG_LOCK:
        cfg = _load_config()
        dd = default_dongle or sweeper_dongle
        if dd not in (None, ""):
            cfg["sweeper_serial"] = _resolve_dongle_serial(dd, cfg["sweeper_serial"])
        if investigator_dongles not in (None, ""):
            cfg["investigator_serials"] = _resolve_dongle_list(investigator_dongles, list(DONGLE_NAMES))
        if band not in (None, "") and band_dongle not in (None, ""):
            s, e = _resolve_band(frequency_range=band)
            cfg["band_dongles"][_band_key(s, e)] = _resolve_dongle_serial(band_dongle, cfg["sweeper_serial"])
        _save_config(cfg)
    return {"message": "Dongle routing updated", "default_dongle": _short(cfg["sweeper_serial"]),
            "investigator_dongles": [_short(s) for s in cfg["investigator_serials"]],
            "best_dongle_per_band": {b: _short(s) for b, s in cfg["band_dongles"].items()}}


@register(
    "set_antenna_note",
    "Save what antenna is on a dongle (e.g. '433 MHz whip', 'discone', 'FM dipole') so reports can mention it.",
    {"dongle": {"type": "string", "description": "'1', '2' or '3'"},
     "note": {"type": "string", "description": "Antenna description in the user's words"}},
    required=("dongle", "note"),
)
def set_antenna_note(dongle=None, note=None):
    serial = _resolve_dongle_serial(dongle, "")
    if not serial:
        raise RFError("Say which dongle: '1', '2' or '3'.")
    _set_dongle(serial, antenna_note=str(note or "").strip()[:80])
    return {"message": f"Saved: {_short(serial)} has {str(note).strip()[:80]}"}


@register(
    "set_timing_defaults",
    "Change default timings: sweep_seconds (default length of every sweep; 0 = quick pass), baseline_seconds "
    "(baseline recording length) and audit_seconds (threat audits and patrol rounds). Use when the user says "
    "'always sweep for N seconds' or similar.",
    {"sweep_seconds": {"type": "integer", "description": "0 = quick pass, or 2-600 seconds"},
     "baseline_seconds": {"type": "integer", "description": "5-600 seconds"},
     "audit_seconds": {"type": "integer", "description": "2-600 seconds"}},
)
def set_timing_defaults(sweep_seconds=None, baseline_seconds=None, audit_seconds=None):
    with _CFG_LOCK:
        cfg = _load_config()
        if sweep_seconds not in (None, ""):
            cfg["default_sweep_seconds"] = _sweep_seconds_arg(sweep_seconds, 0)
        if baseline_seconds not in (None, ""):
            cfg["baseline_seconds"] = _int(baseline_seconds, "baseline_seconds", 30, 5, 600)
        if audit_seconds not in (None, ""):
            cfg["audit_seconds"] = _int(audit_seconds, "audit_seconds", 10, 2, 600)
        _save_config(cfg)
    return {"message": "Timing defaults saved",
            "default_sweep_seconds": cfg["default_sweep_seconds"] or "quick pass",
            "baseline_seconds": cfg["baseline_seconds"], "audit_seconds": cfg["audit_seconds"]}


# ============================================================================
# TOOLS: CALIBRATION + FIRST-RUN SETUP
# ============================================================================
FM_CAL_GAINS = [8.7, 14.4, 19.7, 25.4, 29.7, 33.8]


def _calibrate_gain(serial, s_mhz, e_mhz):
    """Try stepped gains on a band and save the best one for this dongle. Returns (gain, reason, trials).
    FM (88-108) and 915 MHz (880-960) get their own gains; everything else uses the 433 MHz gain."""
    cfg = _load_config()
    d = _dongle_cfg(serial, cfg)
    fm = s_mhz < FM_BROADCAST[1] and e_mhz > FM_BROADCAST[0]
    b915 = s_mhz >= 880.0 and e_mhz <= 960.0
    step = 25.0 if (e_mhz - s_mhz) > 6.0 else 10.0
    cands = sorted({_snap_gain(x, gains=d.get("gains")) for x in (FM_CAL_GAINS if fm else CAL_GAINS)})
    guard = _guard_mhz(step)
    tol = 0.1 if fm else max(0.03, 2.0 * step / 1000.0)
    tag = f"[CAL {_short(serial)}]"
    trials, found = [], []
    print(f"{tag} Testing gains {cands} dB on {s_mhz:g}-{e_mhz:g} MHz...")
    for gain in cands:
        try:
            bins, hops = _sweep(s_mhz, e_mhz, step, gain, serial)
        except RFError:
            if not trials:
                raise
            continue
        clean = {f: v for f, v in bins.items() if not _near_hop(f, hops, guard)} or bins
        nf = _robust_floor(list(clean.values()))
        _, peaks = _extract_peak_clusters(clean, 6.0, cfg["cluster_gap_khz"], step, floor=nf)
        if fm:
            peaks = _group_broadcast(peaks, step)
        pk = [(p.get("channel_mhz") or p["frequency_mhz"], p["snr_db"]) for p in peaks]
        # Signals that were already there one gain step lower are real; ones that pop up are not trusted yet.
        prev = found[-1] if found else None
        kept = [x for x in pk if prev is None or any(abs(x[0] - q[0]) <= tol for q in prev)]
        new = len(pk) - len(kept)
        snrs = sorted((x[1] for x in kept), reverse=True)[:3]
        score = round(sum(snrs) / len(snrs), 2) if snrs else 0.0
        found.append(pk)
        trials.append({"gain_db": gain, "noise_floor_db": nf, "peaks": len(pk), "new_peaks": new,
                       "signal_score_db": score})
        print(f"{tag}   gain {gain:4.1f} dB | floor {nf:7.2f} dB | {len(pk):2d} peak(s), {new:2d} new | "
              f"signal SNR +{score:5.2f} dB")
    if not trials:
        raise RFError("Calibration failed: no valid sweeps.")
    # Overload: a gain step that adds several new peaks while the existing signals do not get clearer means the
    # receiver is creating its own (intermodulation) signals. Stop below it.
    limit = len(trials)
    for i in range(1, len(trials)):
        a, b = trials[i - 1], trials[i]
        if a["peaks"] and b["new_peaks"] >= 4 and (
                b["signal_score_db"] < a["signal_score_db"] + 1.0
                or b["new_peaks"] > (b["peaks"] - b["new_peaks"]) + 2):   # new ones outnumber the known signals
            limit = i
            break
    ok = trials[:limit]
    for t in trials[limit:]:
        t["overload"] = True
    best = max(t["signal_score_db"] for t in ok)
    if best >= 8.0:
        choice = min((t for t in ok if t["signal_score_db"] >= best - 1.0), key=lambda t: t["gain_db"])
        reason = "lowest gain within 1 dB of the clearest signals (avoids overload)"
    elif fm:
        choice = min(trials, key=lambda t: abs(t["gain_db"] - 19.7))
        reason = "no FM stations visible on this antenna, so the safe default was kept"
    else:
        under = [t for t in ok if t["gain_db"] <= 33.8]
        choice = max(under, key=lambda t: t["gain_db"]) if under else min(ok, key=lambda t: t["gain_db"])
        reason = "band was quiet, so a safe mid-high gain was chosen for sensitivity"
    gain = choice["gain_db"]
    _set_dongle(serial, **({"fm_gain_db": gain} if fm else {"gain_915_db": gain} if b915 else {"gain_db": gain}))
    return gain, reason, trials


@register(
    "calibrate_gain",
    "Try stepped tuner gains on a band and save the best gain for that dongle. The FM band (88-108 MHz) and the "
    "915 MHz band keep their own gains; other bands use the 433 MHz gain. Receiver artifacts are excluded.",
    {**_BAND_PROPS, **_DONGLE_PROP},
    aliases=("calibrate_lowest_noise_floor",),
)
def calibrate_gain(frequency_range=None, start_mhz=None, stop_mhz=None, dongle=None):
    cfg = _load_config()
    s_mhz, e_mhz = _resolve_band(start_mhz, stop_mhz, frequency_range)
    serial = _resolve_dongle_serial(dongle, _best_for_band(s_mhz, e_mhz, cfg))
    gain, reason, trials = _calibrate_gain(serial, s_mhz, e_mhz)
    fm = s_mhz < FM_BROADCAST[1] and e_mhz > FM_BROADCAST[0]
    return {"message": "Gain calibration complete and applied", "dongle": _short(serial),
            "band_mhz": [s_mhz, e_mhz], "applied_gain_db": gain,
            "applies_to": ("the FM band (88-108 MHz)" if fm else "880-960 MHz" if s_mhz >= 880 and e_mhz <= 960
                           else "all bands except FM and 880-960 MHz"),
            "reason": reason, "all_gain_trials": trials}


def _measure_fm_offset(f_nom_mhz, serial, gain_db, ppm=0, secs=1.5):
    """Carrier offset (Hz) of an FM station from its nominal frequency, and its SNR (dB)."""
    fs, shift, decim = 1024000, 250000, 4
    with _use_dongle(serial, timeout=60, holder="frequency calibration"):
        z = _capture_iq(serial, int(round(f_nom_mhz * 1e6)) - shift, fs, int(fs * secs), gain_db,
                        timeout=int(secs) + 15, ppm=ppm)
    f, P = _psd(z, fs, 4096)
    chan = np.abs(f - shift) <= 100e3
    ref = (np.abs(f) > 20e3) & (np.abs(f - shift) > 150e3) & (np.abs(f) < 450e3)
    snr = 10.0 * math.log10(float(np.mean(P[chan])) / (float(np.median(P[ref])) + 1e-20))
    y = _mix_filter_decimate(z, fs, shift, 110e3, decim, taps=63)
    d = np.angle(y[1:] * np.conj(y[:-1])).astype(np.float64)
    return float(np.mean(d)) * (fs / decim) / (2.0 * np.pi), snr


@register(
    "calibrate_frequency",
    "Measure and save a dongle's frequency error (ppm) using a strong FM broadcast station.",
    {**_DONGLE_PROP},
)
def calibrate_frequency(dongle=None):
    cfg = _load_config()
    serial = _resolve_dongle_serial(dongle, _best_for_band(88.0, 108.0, cfg))
    gain = _gain_for(serial, 88.0, 108.0, cfg)
    tag = f"[CAL {_short(serial)}]"
    print(f"{tag} Looking for a strong FM station to measure the frequency error...")
    bins, hops = _sweep(88.0, 108.0, 25.0, gain, serial)
    g = _guard_mhz(25.0)
    clean = {f: v for f, v in bins.items() if not _near_hop(f, hops, g)} or bins
    floor = _robust_floor(list(clean.values()))
    scores = []
    for k in range(881, 1080):                                # 88.1 ... 107.9 MHz
        nom = k / 10.0
        vals = [v for f, v in clean.items() if abs(f - nom) <= 0.06]
        if vals:
            scores.append((sum(vals) / len(vals) - floor, nom))
    scores.sort(reverse=True)
    stations = []
    for sc, nom in scores:
        if sc < 15.0:
            break
        if all(abs(nom - s2) >= 0.3 for _, s2 in stations):
            stations.append((sc, nom))
        if len(stations) >= 2:
            break
    if not stations:
        raise RFError("no FM station strong enough on this antenna (needs one 15 dB above the floor); "
                      "frequency correction left at 0 ppm")
    st = stations[0][1]
    print(f"{tag} Measuring frequency error on {st} MHz...")
    off0, snr0 = _measure_fm_offset(st, serial, gain, 0)
    if snr0 < 15.0:
        raise RFError(f"station {st} MHz too weak for calibration (SNR {snr0:.1f} dB); left at 0 ppm")
    est = int(round(-off0 / st))                              # Hz offset / MHz = ppm
    tried = [{"ppm": 0, "offset_hz": round(off0)}]
    final = None
    tol = 1.5 * st                                            # 1.5 ppm in Hz
    if abs(est) <= 1:
        final = 0
    else:
        off1, _ = _measure_fm_offset(st, serial, gain, est)
        tried.append({"ppm": est, "offset_hz": round(off1)})
        if abs(off1) <= max(0.5 * abs(off0), tol):
            final = est
        else:                                                 # sign convention check (closed loop)
            off2, _ = _measure_fm_offset(st, serial, gain, -est)
            tried.append({"ppm": -est, "offset_hz": round(off2)})
            if abs(off2) <= max(0.5 * abs(off0), tol):
                final = -est
    if final is None and len(tried) == 3:
        # a station's programme moves each reading by up to ~200 Hz, so a single reading can miss the tolerance:
        # use all three (the reading changes by 1 ppm per ppm applied, in the direction the two tries showed)
        s = 1.0 if (tried[1]["offset_hz"] - tried[2]["offset_hz"]) * est > 0 else -1.0
        icept = [t["offset_hz"] - s * t["ppm"] * st for t in tried]
        if max(icept) - min(icept) <= 4.0 * st:
            final = int(round(-s * (sum(icept) / 3.0) / st))
    if final is None or abs(final) > 200:
        small = abs(off0) <= 5.0 * st
        raise RFError(f"the frequency error could not be measured reliably on {st} MHz (the station's programme moved "
                      f"the readings: " + ", ".join(f"{t['offset_hz']:+d} Hz" for t in tried) + "), so the correction "
                      "was left unchanged" + (f". The error is small (about {abs(off0) / st:.0f} ppm, "
                                              f"{abs(off0) / 1000:.1f} kHz at {st} MHz) and does not affect decoding"
                                              if small else ""))
    check = None
    if len(stations) > 1:
        try:
            offc, _ = _measure_fm_offset(stations[1][1], serial, gain, final)
            check = {"station_mhz": stations[1][1], "residual_ppm": round(offc / stations[1][1], 1)}
        except RFError:
            pass
    with _CFG_LOCK:
        cfg = _load_config()
        cfg["ppm"][serial] = final
        cfg["dongles"].setdefault(serial, {})["ppm_station_mhz"] = st
        _save_config(cfg)
    return {"message": "Frequency correction measured and saved", "dongle": _short(serial),
            "reference_station_mhz": st, "station_snr_db": round(snr0, 1), "ppm_applied": final,
            "attempts": tried, "cross_check": check}


def _usb_self_test(serial, seconds=3.0):
    """rtl_test: open the dongle, stream samples at 2.048 MS/s, report tuner, gain steps and lost samples.
    Other receivers skip it (their own tools test nothing like it) and report their gain steps."""
    if not radios.is_rtl(serial):
        lab = radios.label_for(serial)
        return {"ok": None, "usable": True, "problems": [], "tuner": lab, "gains": radios.gains_for(serial),
                "note": f"the USB test is for RTL-SDR dongles; this is a {lab}"}
    if shutil.which("rtl_test") is None:
        return {"ok": None, "problems": [], "note": "rtl_test not installed; USB test skipped"}
    with _use_dongle(serial, timeout=30, holder="USB self-test"):
        with tempfile.TemporaryFile("w+") as ef:
            p = subprocess.Popen(["rtl_test", "-d", str(serial), "-s", "2048000"],
                                 stdout=subprocess.DEVNULL, stderr=ef)
            try:
                p.wait(timeout=seconds)
            except subprocess.TimeoutExpired:
                pass
            finally:
                # rtl_test prints its loss summary on Ctrl+C; one whose stop hangs is killed (its "lost at least"
                # lines, printed while it ran, still count)
                _end_radio(p)
            ef.seek(0)
            txt = ef.read()
    tuner = re.search(r"Found (.+?) tuner", txt)
    gl = re.search(r"Supported gain values \(\d+\):\s*([\d.\s]+)", txt)
    gains = [float(x) for x in gl.group(1).split()] if gl else None
    lost = sum(int(x) for x in re.findall(r"lost at least (\d+) bytes", txt))
    spm = re.search(r"Samples per million lost \(minimum\):\s*(\d+)", txt)
    problems, notes, usable = [], [], True
    if re.search(r"usb_claim_interface|Kernel driver is active|resource busy", txt, re.I):
        problems.append("dongle busy, or the DVB TV driver has claimed it (blacklist dvb_usb_rtl28xxu)")
        usable = False
    if re.search(r"No supported devices found|No matching devices|Failed to open", txt, re.I):
        problems.append("could not open the dongle")
        usable = False
    if not tuner and not problems:
        problems.append("tuner not detected")
    if lost > 25000:
        problems.append(f"lost {lost} bytes of samples (USB bandwidth or power problem: try another port "
                        f"or a powered hub)")
    elif lost:
        notes.append("small sample loss at start-up (normal)")
    return {"ok": not problems, "usable": usable, "tuner": tuner.group(1) if tuner else None,
            "gain_steps": len(gains) if gains else None, "gains": gains, "lost_bytes": lost,
            "samples_per_million_lost": int(spm.group(1)) if spm else None, "problems": problems, "notes": notes}


def _system_checks():
    have = {b: bool(shutil.which(b)) for b in ("rtl_power", "rtl_sdr", "rtl_fm", "rtl_test", "rtl_433",
                                                "multimon-ng", "atest", "rtl_adsb")}
    missing = [b for b in ("rtl_power", "rtl_sdr", "rtl_fm", "rtl_433") if not have[b]]
    ai = _ollama_check()
    free_tmp = shutil.disk_usage(tempfile.gettempdir()).free / 1e9
    problems = []
    if missing:
        problems.append("missing programs: " + ", ".join(missing))
    if not ai.get("reachable"):
        problems.append("AI model not reachable (systemctl start ollama)")
    elif not ai.get("model_installed"):
        problems.append(f"AI model not installed (ollama pull {MODEL.lower()})")
    if free_tmp < 0.5:
        problems.append(f"low temporary disk space ({free_tmp:.1f} GB)")
    return {"ok": not problems, "programs": have, "ai_model": ai, "free_tmp_gb": round(free_tmp, 1),
            "problems": problems}


def _reset_for_setup():
    """Back up, then clear, old calibration and baselines so setup starts from scratch.
    Kept: timing and scan preferences, antenna notes, the GUI login, and the Saved Signals list."""
    bdir = DATA_DIR / "backups" / f"before-setup-{time.strftime('%Y%m%d-%H%M%S')}"
    bdir.mkdir(parents=True, exist_ok=True)
    for src in (CONFIG_PATH, SETUP_REPORT):
        if src.exists():
            shutil.copy2(src, bdir / src.name)
    moved = 0
    if BASELINE_DIR.exists():
        for f in sorted(BASELINE_DIR.glob("*.json*")):
            shutil.move(str(f), str(bdir / f.name))
            moved += 1
    with _CFG_LOCK:
        cfg = _load_config()
        keep = {k: cfg[k] for k in ("default_sweep_seconds", "baseline_seconds", "audit_seconds",
                                    "patrol_interval_min", "threshold_db", "dwell_seconds", "step_khz",
                                    "cluster_gap_khz", "dongle_order") if k in cfg}     # numbering stays
        notes = {s: d["antenna_note"] for s, d in cfg.get("dongles", {}).items() if d.get("antenna_note")}
        fresh = copy.deepcopy(DEFAULT_CONFIG)
        fresh.update(keep)
        for s, n in notes.items():
            fresh["dongles"].setdefault(s, {})["antenna_note"] = n
        _save_config(fresh)
    return {"backup_folder": str(bdir), "old_baselines_moved_to_backup": moved, "settings_kept": sorted(keep)}


def _calibrate_dongle(serial):
    """One dongle's calibration (runs alongside the others): FM gain, frequency correction from an FM station,
    then the 433 MHz and 915 MHz gains."""
    r = {}

    def gain_step(key, band):
        try:
            g, why, trials = _calibrate_gain(serial, *band)
            r[key] = {"ok": True, "gain_db": g, "reason": why, "trials": trials}
        except RFError as e:
            r[key] = {"ok": False, "error": str(e)}
    gain_step("fm_gain", (88.0, 108.0))           # first: the ppm step listens to FM stations at this gain
    try:
        fr = calibrate_frequency(serial)
        r["frequency"] = {"ok": True, "ppm": fr["ppm_applied"], "station_mhz": fr["reference_station_mhz"],
                          "station_snr_db": fr["station_snr_db"], "measurements": fr["attempts"],
                          "cross_check": fr["cross_check"]}
    except RFError as e:
        r["frequency"] = {"ok": False, "error": str(e)}
    gain_step("gain_433", (433.0, 435.0))
    gain_step("gain_915", (902.0, 928.0))
    return r


@register(
    "auto_setup",
    "Full setup from scratch (same as /setup): finds every dongle plugged in (new ones are added; missing ones and "
    "dongles sharing a serial number are flagged), backs up and clears old calibration and baselines, checks the "
    "programs, decoders and AI model, USB-tests each dongle, calibrates every dongle at the same time (gains for "
    "433 MHz, 915 MHz and FM, frequency correction), records 30 s baselines on every dongle, and picks the best "
    "dongle for each band. Takes about 3-5 minutes. get_setup_report shows the results later.",
    {},
)
def auto_setup():
    global _PATROL
    t0 = time.time()
    prev = _default_serial()
    stopped = []
    if _PATROL and _PATROL.alive():
        _PATROL.stop()
        stopped.append("patrol")
    caps = _running_watchdogs()
    if caps:                                    # setup needs every dongle (also those the GUI's captures hold)
        stopped += [f"capture on {w['dongle']}" for w in caps]
        try:
            stop_background_watchdog()
        except RFError as e:
            log.warning("setup could not stop a capture: %s", e)

    print("[SETUP] 1/6 System and decoder checks...")
    report = {"version": VERSION, "started": _now(), "system": _system_checks(), "decoders": _decoder_selftest()}
    print("[SETUP]     " + _selftest_line(report["decoders"]))
    print("[SETUP] 2/6 Reset: backing up and clearing old calibration and baselines...")
    report["reset"] = {**_reset_for_setup(), "stopped_first": stopped}

    devs = _list_devices()
    sync = _sync_found_dongles(devs)                 # new dongles get numbers and are set up too
    found, listed = sync["found"], sync["listed"]
    dongles, working = {}, []
    if sync["added"]:
        print("[SETUP]     New dongle(s) found: " + ", ".join(f"{_short(x)} ({x})" for x in sync["added"]))
    print("[SETUP] 3/6 USB self-test of every dongle at the same time (the way ARIS uses them)...")
    present = [s for s in DONGLE_NAMES if not (listed and s not in found)]
    usb_res = _parallel(_usb_self_test, [s for s in present if not _generic_serial(s)])
    for serial in DONGLE_NAMES:
        r = {"name": DONGLE_NAMES[serial]}
        dongles[serial] = r
        if serial not in present:
            r.update(present=False, ok=False, note="not detected: check its USB cable")
            _set_dongle(serial, present=False, usable=False)
            print(f"[SETUP]     {_short(serial)}: NOT DETECTED")
            continue
        if _generic_serial(serial):                 # (never opened: the tools would open another dongle by index)
            r.update(present=True, ok=False, note=f"has the factory serial {serial}: not usable until it has its own")
            _set_dongle(serial, present=True, usable=False)
            report.setdefault("notes", []).append(_factory_serial_note(serial))
            print(f"[SETUP]     {_short(serial)}: FACTORY SERIAL {serial}, not usable (see the note)")
            continue
        r["present"] = True if found else None
        usb = usb_res.get(serial)
        if isinstance(usb, Exception):
            usb = {"ok": False, "usable": False, "problems": [str(usb)[:160]]}
        r["usb_test"] = {k: v for k, v in usb.items() if k != "gains"}
        fields = {"present": r["present"], "usb_ok": usb.get("ok"), "usable": usb.get("usable", True)}
        if usb.get("gains"):
            fields.update(gains=usb["gains"], tuner=usb.get("tuner"))
        _set_dongle(serial, **fields)
        print(f"[SETUP]     {_short(serial)}: " + ("USB ok" if usb.get("ok") else "USB test skipped"
                                                    if usb.get("ok") is None else "USB PROBLEM: "
                                                    + "; ".join(usb.get("problems", []))))
        if usb.get("usable") is False:
            r.update(ok=False, note="not calibrated")
            continue
        working.append(serial)
    if sync["added"]:
        report["new_dongles_added"] = [_short(x) for x in sync["added"]]
    if sync["duplicates"]:
        report["duplicate_serials"] = sync["duplicates"]

    if working:
        for ser, rr in _parallel(_rtl433_live_test, working).items():
            rr = rr if isinstance(rr, dict) else {"ok": False, "detail": str(rr)[:160]}
            dongles[ser]["rtl_433"] = rr
            _set_dongle(ser, rtl433_ok=rr.get("ok"))
            print(f"[SETUP]     {_short(ser)}: " + ("rtl_433 works with it" if rr.get("ok") else
                                                    f"rtl_433 PROBLEM: {rr.get('detail')}"))
    bsecs = int(_load_config().get("baseline_seconds", 30))
    baselines, reception = [], {s: {} for s in working}
    if working:
        print(f"[SETUP] 4/6 Calibrating {len(working)} dongle(s) at the same time: FM gain, frequency correction, "
              f"433 and 915 MHz gains...")
        for s, c in _parallel(_calibrate_dongle, working).items():
            dongles[s].update(c if isinstance(c, dict) else {"calibration_error": str(c)})
        print(f"[SETUP] 5/6 Reception check and baselines: every dongle watches each band for {bsecs} s at the "
              f"same time...")
        for band in SETUP_BANDS:
            s_mhz, e_mhz = _resolve_band(frequency_range=band + "MHz")
            print(f"[SETUP]     {band} MHz...")
            for ser, rr in _record_baselines(s_mhz, e_mhz, working, bsecs, "auto setup").items():
                if isinstance(rr, Exception):
                    baselines.append({"band": band, "dongle": _short(ser), "ok": False, "error": str(rr)[:160]})
                    continue
                summ = rr["summary"]
                top = max((x["snr_db"] for x in summ["strongest"]), default=0.0)
                reception[ser][band] = {"signals": summ["signals_present"], "strongest_snr_db": top}
                baselines.append({"band": band, "dongle": _short(ser), "ok": True, "seconds": rr["duration_s"],
                                  "gain_db": rr["gain_db"], "noise_floor_db": rr["noise_floor_db"],
                                  "signals": summ["signals_present"], "strongest_snr_db": top})
    print("[SETUP] 6/6 Choosing the best dongle for each band...")
    band_dongles = {}
    for band in SETUP_BANDS:
        cands = [(reception[s][band]["signals"], reception[s][band]["strongest_snr_db"], s)
                 for s in working if band in reception[s] and reception[s][band]["signals"] > 0]
        if cands:
            band_dongles[band] = max(cands, key=lambda c: (c[0] > 0, c[1], c[0]))[2]
    wins = Counter(band_dongles.values())
    default = (max(working, key=lambda s: (wins.get(s, 0), s == prev, -working.index(s))) if working else prev)
    with _CFG_LOCK:
        cfg = _load_config()
        cfg["band_dongles"], cfg["sweeper_serial"] = band_dongles, default
        cfg["investigator_serials"] = working or [default]
        for s in working:
            d = cfg["dongles"].setdefault(s, {})
            d["reception"] = reception[s]
            d["hears_signals"] = any(v.get("signals") for v in reception[s].values())
        recorded = [b for b in SETUP_BANDS if any(x.get("ok") and x["band"] == b for x in baselines)]
        if recorded:
            cfg["patrol_bands"] = recorded
        cfg["setup_done"] = _now()
        _save_config(cfg)
    report["routing"] = {"best_dongle_per_band": {b: _short(s) for b, s in band_dongles.items()},
                         "default_dongle": _short(default), "investigators": [_short(s) for s in working],
                         "bands_no_dongle_heard": [b for b in SETUP_BANDS if b not in band_dongles] if working else []}
    for s in working:
        dongles[s]["reception"] = reception[s]
    problems = list(report["system"]["problems"])
    problems += [d["fix"] for d in sync["duplicates"]]
    problems += [f"{_short(x)}: {r['rtl_433']['detail']}" for x, r in dongles.items()
                 if (r.get("rtl_433") or {}).get("ok") is False]
    problems += [f"decoder {n}: {r['detail']}" for n, r in report["decoders"].items() if r.get("ok") is False]
    notes = []
    for s, r in dongles.items():
        if r.get("present") is False:
            problems.append(f"{_short(s)} not detected")
        problems += [f"{_short(s)}: {p}" for p in (r.get("usb_test") or {}).get("problems", [])]
        if s in working and not any(v.get("signals") for v in reception[s].values()):
            notes.append(f"{_short(s)} heard nothing in 433, 915 or FM: its antenna may be loose, missing, or "
                         f"made for other bands")
        fr = r.get("frequency") or {}
        if s in working and fr.get("ok") is False:
            notes.append(f"{_short(s)}: {fr.get('error')}")
    if not working:
        problems.append("no dongle could be used: check the USB cables, then run /setup again" if not found
                        else "no dongle could be opened")
    report.update({"dongles": dongles, "baselines": baselines, "notes": notes, "finished": cfg["setup_done"],
                   "took_seconds": round(time.time() - t0),
                   "message": "Setup complete" if not problems else "Setup finished with problems",
                   "problems": problems, "patrol_bands": cfg["patrol_bands"],
                   "next": "Run start_patrol (or /patrol start) to watch these bands for new transmitters."})
    try:
        _atomic_write_json(SETUP_REPORT, report)
    except OSError:
        pass
    return report


@register(
    "get_setup_report",
    "Show the full results of the last setup (/setup): decoder tests, USB tests, gains, frequency correction, "
    "what each dongle heard per band, the best dongle per band and the baselines. Use for any question about "
    "setup or calibration.",
    {},
    aliases=("setup_report",),
)
def get_setup_report():
    if not SETUP_REPORT.exists():
        raise RFError("No saved setup report yet. Run /setup to create one.")
    with open(SETUP_REPORT) as f:
        return json.load(f)


def _sync_found_dongles(found) -> dict:
    """Plugged-in dongles vs the known ones: new dongles get the next numbers (config 'dongle_order'), missing ones
    are marked so nothing tries to use them, and dongles sharing a serial number are flagged (the radio tools pick
    dongles by serial, so only one of a pair can be used)."""
    serials = [d["serial"] for d in found]
    listed = bool(found) or shutil.which("rtl_test") is not None    # rtl_test listing nothing = none plugged in
    for d in found:                                # receivers of other kinds: their gain steps and kind
        if d.get("index") is None and d.get("backend"):
            _set_dongle(d["serial"], tuner=radios.label_for(d["serial"]), gains=radios.gains_for(d["serial"]))
    dup = sorted({x for x in serials if serials.count(x) > 1})
    added = []
    with _CFG_LOCK:
        cfg = _load_config()
        order = list(cfg["dongle_order"])
        for x in serials:
            if x not in order:
                order.append(x)
                added.append(x)
        if listed:
            for x in order:
                cfg["dongles"].setdefault(x, {})["present"] = x in serials
        if added:
            cfg["dongle_order"] = order
            cfg["investigator_serials"] = list(dict.fromkeys(list(cfg.get("investigator_serials") or []) + added))
        if listed or added:
            _save_config(cfg)
    _load_config()                                                 # renumber
    used = set(DONGLE_NAMES)
    k = len(DONGLE_NAMES) + 1
    while f"0000{k}00{k}" in used and k < 9:
        k += 1
    dups = [{"serial": x, "indexes": [d["index"] for d in found if d["serial"] == x and d.get("index") is not None],
             "fix": (f"Two dongles share serial {x}, so ARIS can only use one of them. Give the second one its own "
                     f"serial: rtl_eeprom -d {([d['index'] for d in found if d['serial'] == x and d.get('index') is not None] or [0])[-1]} -s 0000{k}00{k} "
                     f"(answer y), then unplug it and plug it back in, and run /check again.")} for x in dup]
    return {"found": serials, "listed": listed, "added": added,
            "missing": [x for x in DONGLE_NAMES if listed and x not in serials], "duplicates": dups,
            "names": {x: d["name"] for x in serials for d in found if d["serial"] == x}}


def _reception_test(serial) -> dict:
    """Is the dongle really receiving? An FM station's RDS name proves it: the name is checksummed, so it cannot
    come from noise or from the receiver itself. Tries the station the dongle was calibrated on, then the
    strongest ones in its FM baseline, else a quick sweep finds one."""
    cfg = _load_config()
    gain = _gain_for(serial, 88.0, 108.0, cfg)
    cands = []
    st = _dongle_cfg(serial, cfg).get("ppm_station_mhz")
    if st:
        cands.append(round(float(st), 1))
    hit = _find_baseline(88.0, 108.0, serial)
    if hit:
        cands += [round(float(x["mhz"]), 1) for x in (hit[1].get("summary") or {}).get("strongest", [])[:3]
                  if _in_fm_band(x["mhz"])]
    cands = list(dict.fromkeys(cands))[:2]
    if not cands:
        try:
            _b, _fl, peaks, _a, _i = _sweep_peaks(88.0, 108.0, 25.0, gain, serial, 10.0, cfg["cluster_gap_khz"],
                                                  lock_timeout=30.0)
        except RFError as e:
            return {"ok": False, "detail": f"could not sweep the FM band: {e}"}
        peaks = sorted(_group_broadcast(peaks, 25.0), key=lambda p: -p["snr_db"])
        cands = [round(float(p.get("channel_mhz") or p["frequency_mhz"]), 1) for p in peaks
                 if _in_fm_band(p.get("channel_mhz") or p["frequency_mhz"])][:2]
    if not cands:
        return {"ok": None, "detail": "heard no FM station (normal if its antenna is only for 433 / 915 MHz)"}
    heard = None
    with _use_dongle(serial, timeout=30, holder="reception test"):
        for i, f in enumerate(cands):
            if i:
                time.sleep(USB_SETTLE_S)          # opened again at once, same lock
            r = _stage1_wideband(f, serial, gain, 3.0)
            if _stage_failed(r):
                return {"ok": False, "detail": f"capture failed: {str(r.get('status'))[:120]}"}
            if r.get("rds_station_name"):
                return {"ok": True, "station_mhz": f, "rds_name": r["rds_station_name"], "snr_db": r.get("channel_snr_db"),
                        "detail": f"receiving: decoded FM {f:.1f} MHz, RDS name \"{r['rds_station_name']}\" "
                                  f"(+{float(r.get('channel_snr_db') or 0):.0f} dB)"}
            if r.get("signal_present_during_capture") and heard is None:
                heard = (f, r.get("channel_snr_db"))
    if heard:
        return {"ok": True, "station_mhz": heard[0], "snr_db": heard[1],
                "detail": f"receiving: FM {heard[0]:.1f} MHz heard (+{float(heard[1] or 0):.0f} dB), no RDS name in 3 s"}
    return {"ok": None, "detail": f"FM {cands[0]:.1f} MHz was not heard this time (antenna may suit other bands)"}


def _rtl433_live_test(serial, seconds=2) -> dict:
    """rtl_433 opens this dongle and streams from it (rtl_433 picks dongles differently from the other rtl-sdr
    tools, so the USB test alone does not prove it can). The decoders themselves are tested on recordings."""
    if not shutil.which("rtl_433"):
        return {"ok": None, "detail": "rtl_433 not installed (sudo apt install rtl-433)"}
    if not _have_tool("rtl_433", serial):
        return {"ok": None, "detail": _tool_missing("rtl_433", serial)}
    cmd = (["rtl_433"] + _rtl433_device(serial) + ["-f", "433920000", "-s", "250k", "-T", str(int(seconds))]
           + _ppm_args(serial) + ["-F", "json", "-F", "log"])
    try:
        with _use_dongle(serial, timeout=30, holder="rtl_433 test"):
            p = subprocess.run(_radio_cmd(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               timeout=seconds + 20)
    except RFError as e:
        return {"ok": None, "detail": str(e)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "rtl_433 did not stop on time (the dongle may be stuck: replug it)"}
    lines = _rtl433_log_lines(p.stdout)
    if p.returncode == 0:
        return {"ok": True, "detail": "rtl_433 opens it and receives"}
    return {"ok": False, "detail": _rtl433_problem(lines, p.returncode), "log": lines[-4:]}


def _check_one_dongle(serial) -> dict:
    usb = _usb_self_test(serial, 2.0)
    fields = {"present": True, "usb_ok": usb.get("ok"), "usable": usb.get("usable", True)}
    if usb.get("gains"):
        fields.update(gains=usb["gains"], tuner=usb.get("tuner"))
    _set_dongle(serial, **fields)
    out = {"usb": {k: v for k, v in usb.items() if k != "gains"}}
    if usb.get("usable") is not False:
        try:
            out["reception"] = _reception_test(serial)
        except RFError as e:
            out["reception"] = {"ok": False, "detail": str(e)[:160]}
        try:
            out["rtl_433"] = _rtl433_live_test(serial)
        except RFError as e:
            out["rtl_433"] = {"ok": False, "detail": str(e)[:160]}
        _set_dongle(serial, rtl433_ok=out["rtl_433"].get("ok"))
    return out


@register(
    "check_system",
    "Check everything without recalibrating (/check, ~30 s): dongles plugged in (new ones added; missing ones and "
    "shared serial numbers flagged), USB, live reception (an FM station's RDS name) and live rtl_433 test per "
    "dongle, decoder tests, AI model, disk. For 'check the system / are my dongles working'.",
    {},
    aliases=("system_check", "check_dongles"),
)
def check_system():
    t0 = time.time()
    print("[CHECK] 1/4 Looking for dongles...")
    found = _list_devices()
    sync = _sync_found_dongles(found)
    busy = {str(w.get("serial")): w for w in _running_watchdogs()}
    here = [x for x in DONGLE_NAMES if x in sync["found"] or not sync["listed"]]
    test = [x for x in here if x not in busy and not _generic_serial(x)]
    print(f"[CHECK] 2/4 USB and reception test on {', '.join(_short(x) for x in test) or 'no dongle'} at once...")
    per = _parallel(_check_one_dongle, test)
    print("[CHECK] 3/4 Decoders with test signals...")
    dec = _decoder_selftest()
    print("[CHECK] 4/4 Programs, AI model and disk...")
    sysc = _system_checks()
    cfg = _load_config()
    dongles, problems, notes = {}, list(sysc["problems"]), []
    for ser in DONGLE_NAMES:
        name = _short(ser)
        row = {"serial": ser, "name": DONGLE_NAMES[ser], "new": ser in sync["added"],
               "set_up": bool(_dongle_cfg(ser, cfg).get("reception"))}
        if sync["listed"] and ser not in sync["found"]:
            row["state"] = "missing"
            problems.append(f"{name} ({ser}) is not plugged in or not detected: check its USB cable. If you removed "
                            f"it for good, say 'forget {name.lower()}'.")
        elif ser in busy:
            row["state"] = f"busy with a background capture on {busy[ser]['freq_mhz']:g} MHz (not tested)"
        elif _generic_serial(ser):
            row["state"] = (f"has the factory serial {ser}: not tested and not used by ARIS until it has its own "
                            f"serial (see the note below)")
        else:
            r = per.get(ser)
            if isinstance(r, Exception):
                row["state"] = f"test failed: {str(r)[:120]}"
                problems.append(f"{name}: {str(r)[:120]}")
            else:
                usb, rec, r433 = r.get("usb") or {}, r.get("reception") or {}, r.get("rtl_433") or {}
                row.update(usb=usb, reception=rec, rtl_433=r433)
                row["state"] = ("USB problem: " + "; ".join(usb.get("problems") or []) if usb.get("ok") is False
                                else rec.get("detail") or "ok")
                if r433.get("ok") is True:
                    row["state"] += "; rtl_433 works with it"
                elif r433.get("ok") is False:
                    row["state"] += f"; rtl_433 FAILED: {r433.get('detail')}"
                    problems.append(f"{name}: {r433.get('detail')}")
                if ser in {d["serial"] for d in sync["duplicates"]}:
                    row["state"] += " (another dongle has the same serial number: see the problems below)"
                problems += [f"{name}: {x}" for x in usb.get("problems") or []]
                if rec.get("ok") is False:
                    problems.append(f"{name}: {rec.get('detail')}")
        dongles[name] = row
    for d in sync["duplicates"]:
        problems.append(d["fix"])
    if sync["added"]:
        notes.append("New dongle" + ("s" if len(sync["added"]) > 1 else "") + " found and added: "
                     + ", ".join(f"{_short(x)} ({x})" for x in sync["added"])
                     + ". Run /setup to calibrate it (gains, frequency correction, baselines).")
    elif not all(r["set_up"] for r in dongles.values() if r.get("state") != "missing" or _generic_serial(r["serial"])):
        notes.append("Some dongles have not been set up yet: run /setup.")
    for x in here:                                   # every time, not only when it was just added (review)
        if _generic_serial(x):
            notes.append(_factory_serial_note(x))
    problems += [f"decoder {n}: {r['detail']}" for n, r in dec.items() if r.get("ok") is False]
    lines = [f"System check ({round(time.time() - t0)} s): " + (f"{len(problems)} problem(s)." if problems
                                                                  else "everything works.")]
    lines += [f"{n}{' (new)' if r['new'] else ''}: {r['state']}" for n, r in dongles.items()]
    lines.append(_selftest_line(dec))
    ai = sysc.get("ai_model") or {}
    lines.append("AI model: " + ("ready" if ai.get("reachable") and ai.get("model_installed") else
                                 "installed but not reachable" if ai.get("model_installed") else
                                 "NOT ready (see problems)") + f"; free disk {sysc.get('free_tmp_gb')} GB.")
    lines += ["Problem: " + x for x in problems] + ["Note: " + x for x in notes]
    rep_ = {"message": "Everything works" if not problems else "Problems found", "dongles": dongles,
            "dongles_found": len(sync["found"]), "new_dongles": [_short(x) for x in sync["added"]],
            "missing_dongles": [_short(x) for x in sync["missing"]], "duplicate_serials": sync["duplicates"],
            "decoders": dec, "system": sysc, "problems": problems, "notes": notes, "summary": "\n".join(lines),
            "took_seconds": round(time.time() - t0)}
    try:
        _atomic_write_json(DATA_DIR / "check_report.json", {**rep_, "time": _now()})
    except OSError:
        pass
    return rep_


@register("forget_dongle", "Remove a dongle that was unplugged for good from ARIS's list (its baselines are kept). "
          "The remaining dongles are renumbered.", {"dongle": {"type": "string", "description": "Dongle number or serial"}})
def forget_dongle(dongle=None):
    ser = _resolve_dongle_serial(dongle, "")
    if not ser:
        raise RFError(f"Unknown dongle '{dongle}'.")
    if ser in {d["serial"] for d in _list_devices()}:
        raise RFError(f"{_short(ser)} ({ser}) is still plugged in; unplug it first.")
    name = _short(ser)
    before = {s: _short(s) for s in DONGLE_NAMES}
    with _CFG_LOCK:
        cfg = _load_config()
        cfg["dongle_order"] = [x for x in cfg["dongle_order"] if x != ser] or list(DEFAULT_DONGLES)
        cfg["investigator_serials"] = [x for x in cfg.get("investigator_serials") or [] if x != ser]
        orphans = [b for b, x in cfg["band_dongles"].items() if x == ser]
        cfg["band_dongles"] = {b: x for b, x in cfg["band_dongles"].items() if x != ser}
        if cfg.get("sweeper_serial") == ser:
            cfg["sweeper_serial"] = cfg["dongle_order"][0]
        _apply_dongle_order(cfg["dongle_order"])
        moved, unset = [], []
        for b in orphans:            # its bands go to the dongle that heard them next best during /setup
            try:
                lo, hi = (float(x) for x in b.split("-"))
            except ValueError:
                continue
            nxt = _reception_best(lo, hi, cfg, among=[s for s in cfg["dongle_order"] if s != ser])
            if nxt:
                cfg["band_dongles"][b] = nxt
                moved.append((b, nxt))
            else:
                unset.append(b)
        _save_config(cfg)
    _load_config()
    try:
        _nodes_mod()                 # the Map's nodes get the new numbers, and the forgotten one is switched off
    except Exception as e:
        log.warning("forget_dongle: nodes: %s", e)
    now = [f"{_short(s)} = {s}" + (f" (was {before[s]})" if before.get(s) not in (None, _short(s)) else "")
           for s in DONGLE_NAMES]
    bands = "".join(f" The {_BAND_LABELS.get(b, b + ' MHz')} band now goes to {_short(x)} (it heard it next best "
                    f"during /setup)." for b, x in moved)
    if unset:
        bands += (" No other dongle heard the " + ", ".join(_BAND_LABELS.get(b, b + " MHz") for b in unset)
                  + " band during /setup: run /setup again to choose one.")
    return {"message": f"{name} ({ser}) is forgotten (its baselines are kept). Your dongles are now numbered: "
                       + ", ".join(now) + "." + bands}
