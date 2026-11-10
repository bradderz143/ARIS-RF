"""ARIS engine, part "sweep": Stage 0: the rtl_power sweep engine, peak clustering, hop-centre artifact rejection, and the DSP
helpers (filters, PSD, IQ captures with rtl_sdr).

Executed into aris.py's namespace by aris._include (see aris.py): names from aris.py's imports and from the
parts before this one are available here without imports, and everything defined here becomes aris.<name>.
"""
from __future__ import annotations

# ============================================================================
# STAGE 0: rtl_power SWEEP ENGINE, PEAK CLUSTERING, HOP-CENTRE ARTIFACT REJECTION
# rtl_power retunes in hops of up to ~2.2 MHz. Every hop has a false spike at its centre
# (the receiver's DC offset). Peaks sitting on a hop centre are re-checked off-centre.
# ============================================================================
def _parse_rtl_power_lines(lines):
    """rtl_power rows: date, time, hz_low, hz_high, hz_step, samples, dB... -> ({hz: [dB in time order]},
    {hop_centre_hz}), each value at its true frequency (see arislib/rtlpower.py). Each row is converted with numpy and grouped once at the end (a 10-minute sweep has
    millions of values)."""
    hz_parts, db_parts, hops = [], [], set()
    for line in lines:
        row = line.split(",")
        if len(row) < 7:
            continue
        try:
            hz_low, hz_high, hz_step = float(row[2]), float(row[3]), float(row[4])
        except ValueError:
            continue
        try:
            vals = np.array(row[6:], dtype=np.float64)
        except ValueError:                      # a damaged or empty value: convert one by one
            vals = []
            for v in row[6:]:
                try:
                    vals.append(float(v))
                except ValueError:
                    vals.append(float("nan"))
            vals = np.array(vals, dtype=np.float64)
        centre = (hz_low + hz_high) / 2.0
        hops.add(int(round(centre)))
        # rtl_power prints the last bin twice, and "Hz low" is not where its first bin is: the n real bins sit at
        # centre + (j - n/2) * step (arislib/rtlpower.py). Reading them from Hz low put them up to a bin too high.
        n = len(vals)
        if n >= 2 and np.isfinite(vals[-1]) and np.isfinite(vals[-2]) and abs(vals[-1] - vals[-2]) <= 0.011:
            n -= 1
            vals = vals[:n]
        first = centre - (n / 2.0) * hz_step if n > 1 else centre
        hz = np.rint(first + np.arange(n) * hz_step).astype(np.int64)
        ok = np.isfinite(vals)
        hz_parts.append(hz[ok])
        db_parts.append(vals[ok])
    if not hz_parts:
        return {}, hops
    hz = np.concatenate(hz_parts)
    db = np.concatenate(db_parts)
    if not len(hz):
        return {}, hops
    order = np.argsort(hz, kind="stable")       # stable: each frequency keeps its readings in time order
    hz, db = hz[order], db[order]
    keys, first = np.unique(hz, return_index=True)
    return {int(k): g.tolist() for k, g in zip(keys, np.split(db, first[1:]))}, hops


def _parse_rtl_power_csv(path: str):
    with open(path) as f:
        return _parse_rtl_power_lines(f)


def _rtl_power(start_mhz, stop_mhz, step_khz=10.0, gain_db=None, serial=None,
               duration_s=None, integration_s=2, lock_timeout=60.0, holder="sweep"):
    """Run rtl_power. duration_s=None -> single pass (-1). Returns ({hz: [dB]}, {hop_centre_hz})."""
    serial = serial or _default_serial()
    _validate_band(start_mhz, stop_mhz, step_khz)
    if not _have_tool("rtl_power", serial):
        raise RFError(_tool_missing("rtl_power", serial))
    cfg = _load_config()
    hops = max(1, math.ceil((stop_mhz - start_mhz) / 2.0))
    timeout = int((hops * 0.5 + integration_s + (duration_s or 0) + 20) * 1.6) + 10
    cmd = ["rtl_power", "-d", str(serial),
           "-f", f"{start_mhz:.4f}M:{stop_mhz:.4f}M:{step_khz:g}k",
           "-i", str(integration_s),
           "-c", str(cfg.get("crop_percent", "20%")),
           "-w", str(cfg.get("window_fn", "hamming"))]
    if gain_db is not None:
        cmd += ["-g", str(gain_db)]
    cmd += _ppm_args(serial, cfg=cfg)
    cmd += ["-1"] if duration_s is None else ["-e", f"{int(duration_s)}s"]
    cmd.append("-")                                  # CSV straight to stdout, parsed while it arrives
    timed_out = threading.Event()
    with _use_dongle(serial, timeout=lock_timeout, holder=holder):
        with tempfile.TemporaryFile("w+") as ef:
            proc = subprocess.Popen(_radio_cmd(cmd), stdout=subprocess.PIPE, stderr=ef, text=True)
            timer = threading.Timer(timeout, lambda: (timed_out.set(), proc.kill()))
            timer.start()
            try:
                acc, hop_set = _parse_rtl_power_lines(proc.stdout)
                proc.wait()
            finally:
                timer.cancel()
                _end_radio(proc, drain=True)         # (reading failed: it must not outlive the dongle's lock)
                proc.stdout.close()
            ef.seek(0)
            err = ef.read().strip()
    if timed_out.is_set():
        raise RFError("rtl_power timed out (dongle hung?). Replug it or use a narrower band.")
    if proc.returncode != 0:
        hint = " (device busy, unplugged or serial mismatch?)" if re.search(
            r"usb_claim|busy|No supported|not found|No matching", err, re.I) else ""
        raise RFError(f"rtl_power failed on {serial} (rc={proc.returncode}){hint}: {err[-300:]}")
    if not acc:
        raise RFError(f"rtl_power returned no usable bins on {serial}. Check the USB connection.")
    return acc, hop_set


def _sweep(start_mhz, stop_mhz, step_khz=10.0, gain_db=None, serial=None, lock_timeout=60.0):
    """Single-pass sweep -> ({freq_mhz: mean_dB}, [hop_centre_mhz])."""
    serial = serial or _default_serial()
    if gain_db is None:
        gain_db = _gain_for(serial, start_mhz, stop_mhz)
    acc, hops = _rtl_power(start_mhz, stop_mhz, step_khz, gain_db, serial, lock_timeout=lock_timeout)
    out = {}
    for hz, vals in sorted(acc.items()):
        f = round(hz / 1e6, 4)
        if start_mhz - 1e-6 <= f <= stop_mhz + 1e-6:
            out[f] = round(sum(vals) / len(vals), 2)
    if not out:
        raise RFError("Sweep returned no bins inside the requested band.")
    return out, sorted(round(h / 1e6, 4) for h in hops)


def _sweep_hold(start_mhz, stop_mhz, step_khz, gain_db, serial, duration_s, lock_timeout=60.0):
    """Timed sweep: watch the band for duration_s seconds (one reading per second per frequency).
    Returns (mean_bins, peak_hold_bins, {freq: [readings]}, [hop_centre_mhz], readings_per_freq)."""
    acc, hop_hz = _rtl_power(start_mhz, stop_mhz, step_khz, gain_db, serial, duration_s=int(duration_s),
                             integration_s=1, lock_timeout=lock_timeout, holder="timed sweep")
    series = {}
    for hz, vals in sorted(acc.items()):
        f = round(hz / 1e6, 4)
        if start_mhz - 1e-6 <= f <= stop_mhz + 1e-6:
            series[f] = vals
    if not series:
        raise RFError("Sweep returned no bins inside the requested band.")
    mean = {f: round(sum(v) / len(v), 2) for f, v in series.items()}
    hold = {f: round(max(v), 2) for f, v in series.items()}
    rows = max(len(v) for v in series.values())
    return mean, hold, series, sorted(round(h / 1e6, 4) for h in hop_hz), rows


def _sweep_seconds_arg(value, default):
    """None/'' -> default; 0 -> quick single pass; otherwise clamp to 2..600 s."""
    if value is None or value == "":
        value = default
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise RFError(f"sweep_seconds must be a number of seconds, got {value!r}.")
    return 0 if v <= 0 else int(max(2, min(600, round(v))))


def _guard_mhz(step_khz) -> float:
    return max(0.025, 2.5 * float(step_khz) / 1000.0)


def _near_hop(f_mhz, hops, guard) -> bool:
    return any(abs(f_mhz - h) <= guard for h in hops)


def _robust_floor(vals):
    """Noise floor of a sweep. The median works for quiet bands, but in a crowded band (FM broadcast) station
    skirts cover most bins and lift it, hiding weaker signals. Use the quietest 10 % + 1.5 dB when lower."""
    v = sorted(vals)
    n = len(v)
    med = v[n // 2] if n % 2 else 0.5 * (v[n // 2 - 1] + v[n // 2])
    p10 = v[int(0.10 * (n - 1))]
    return round(min(med, p10 + 1.5), 2)


def _split_at_valleys(c, min_prom_db=6.0):
    """A weak signal sitting on a strong neighbour's skirt forms one block of bins above the threshold.
    Split the block at any dip that is at least min_prom_db below the peaks on both sides."""
    if len(c) < 5:
        return [c]
    vals = [db for _, db in c]
    sm = [sum(vals[max(0, i - 1):i + 2]) / len(vals[max(0, i - 1):i + 2]) for i in range(len(vals))]
    parts, start, top, valley = [], 0, sm[0], None
    for i in range(1, len(sm)):
        v = sm[i]
        if valley is None:
            if v > top:
                top = v
            elif v <= top - min_prom_db:
                valley = i
        elif v < sm[valley]:
            valley = i
        elif v >= sm[valley] + min_prom_db:
            parts.append(c[start:valley])
            start, top, valley = valley, v, None
    parts.append(c[start:])
    return [p for p in parts if p]


def _extract_peak_clusters(bins, threshold_db=6.0, cluster_gap_khz=35.0, step_khz=10.0, floor=None, hops=None):
    """Signals in a sweep: clusters of bins above the floor, each with its strongest point and power centre.
    hops: the sweep's hop centres (MHz). Their bins carry the receiver's own spike, so they never count as a signal's
    strongest point or in its power centre when the signal has other bins (a station 144 kHz from a hop centre was
    pulled tens of kHz towards it)."""
    if not bins:
        return None, []
    noise_floor_db = round(statistics.median(list(bins.values())), 2) if floor is None else round(float(floor), 2)
    cutoff_db = noise_floor_db + float(threshold_db)
    active = [(f, db) for f, db in sorted(bins.items()) if db >= cutoff_db]
    if not active:
        return noise_floor_db, []
    max_gap_mhz = float(cluster_gap_khz) / 1000.0
    step_mhz = float(step_khz) / 1000.0
    clusters, cur = [], [active[0]]
    for f, db in active[1:]:
        if (f - cur[-1][0]) <= max_gap_mhz:
            cur.append((f, db))
        else:
            clusters.append(cur)
            cur = [(f, db)]
    clusters.append(cur)
    clusters = [part for c in clusters for part in _split_at_valleys(c)]

    allf = np.fromiter(sorted(bins), dtype=np.float64)
    alld = np.array([bins[f] for f in allf], dtype=np.float64)
    near_hop = lambda f: bool(hops) and any(abs(f - h) <= 1.5 * step_mhz for h in hops)
    spike = np.array([near_hop(f) for f in allf], dtype=bool) if hops else np.zeros(len(allf), dtype=bool)
    peaks = []
    for c in clusters:
        own = [x for x in c if not near_hop(x[0])] or c
        peak_f, max_db = max(own, key=lambda x: x[1])
        lo, hi = c[0][0], c[-1][0]
        bw_khz = round(((hi - lo) + step_mhz) * 1000.0, 1)
        # Power-weighted centre within 100 kHz, re-centred twice (a wideband FM station's strongest point can sit
        # up to 75 kHz from its carrier): the station's own power dominates, so an HD Radio sideband or a
        # neighbour's skirt that the threshold cuts unevenly does not move it.
        centroid = peak_f
        for _ in range(3):
            sel = (np.abs(allf - centroid) <= 0.1 + 1e-9) & (alld >= noise_floor_db + 3.0) & ~spike
            w = np.power(10.0, (alld[sel] - noise_floor_db) / 10.0) - 1.0
            if not sel.any() or np.sum(w) <= 0:
                centroid = (lo + hi) / 2.0
                break
            centroid = float(np.sum(allf[sel] * w) / np.sum(w))
        peaks.append({
            "frequency_mhz": peak_f,
            "center_mhz": round((lo + hi) / 2.0, 4),
            "centroid_mhz": round(centroid, 4),
            "peak_db": max_db,
            "noise_floor_db": noise_floor_db,
            "snr_db": round(max_db - noise_floor_db, 2),
            "occupied_bw_khz": bw_khz,
            "bins_in_cluster": len(c),
            "probable_allocation": _classify_frequency_allocation(peak_f, bw_khz),
            "_lo": lo, "_hi": hi,
        })
    peaks.sort(key=lambda x: x["snr_db"], reverse=True)
    for i, p in enumerate(peaks, start=1):
        p["peak_id"] = i
    return noise_floor_db, peaks


def _confirm_off_center(freq_mhz, step_khz, gain_db, serial, lock_timeout=60.0, duration_s=0):
    """Re-sweep a small window whose hop centre is well away from freq. Returns SNR at freq (dB) or None.
    With duration_s, the re-check is a peak-hold sweep too (so intermittent signals are not lost)."""
    g = _guard_mhz(step_khz)
    d = max(0.1, 3.0 * g)               # distance from freq to the new hop centre
    half = max(0.25, d + 0.1)
    s = max(MIN_MHZ, round(freq_mhz - d - half, 4))
    e = min(MAX_MHZ, round(freq_mhz - d + half, 4))
    if not s < freq_mhz < e:
        return None
    if duration_s:
        _, bins, _, hops, _ = _sweep_hold(s, e, step_khz, gain_db, serial, duration_s, lock_timeout)
    else:
        bins, hops = _sweep(s, e, step_khz, gain_db, serial, lock_timeout=lock_timeout)
    clean = {f: v for f, v in bins.items() if not _near_hop(f, hops, g)}
    if len(clean) < 5:
        return None
    floor = statistics.median(clean.values())
    near = [v for f, v in clean.items() if abs(f - freq_mhz) <= max(g, 1.5 * step_khz / 1000.0)]
    if not near:
        return None
    return round(max(near) - floor, 2)


def _sweep_peaks(start_mhz, stop_mhz, step_khz, gain_db, serial, threshold_db, gap_khz,
                 max_confirm=6, lock_timeout=60.0, duration_s=0):
    """Sweep, find peaks, and reject hop-centre artifacts.
    duration_s=0: quick single pass. duration_s>0: watch the band that long and detect on the strongest
    level each frequency reached (peak hold), which catches intermittent transmitters.
    Returns (bins, floor, peaks, artifacts, info)."""
    g = _guard_mhz(step_khz)
    if duration_s:
        mean, bins, series, hops, rows = _sweep_hold(start_mhz, stop_mhz, step_khz, gain_db, serial,
                                                     duration_s, lock_timeout)
        det_thresh = max(4.0, float(threshold_db) - 1.0)
        mean_clean = [v for f, v in mean.items() if not _near_hop(f, hops, g)] or list(mean.values())
        mean_floor = _robust_floor(mean_clean)
        info = {"sweep_mode": f"timed peak-hold sweep, {duration_s} s ({rows} readings per frequency)",
                "average_noise_floor_db": mean_floor}
    else:
        bins, hops = _sweep(start_mhz, stop_mhz, step_khz, gain_db, serial, lock_timeout=lock_timeout)
        mean, series, rows, det_thresh, mean_floor = bins, None, 1, float(threshold_db), None
        info = {"sweep_mode": "quick 2 s pass: it can MISS devices that transmit only now and then (sensors, "
                              "remotes); a timed sweep, e.g. 'sweep 433 for 60 seconds', catches those"}
    clean_vals = [v for f, v in bins.items() if not _near_hop(f, hops, g)] or list(bins.values())
    floor = _robust_floor(clean_vals)
    _, peaks = _extract_peak_clusters(bins, det_thresh, gap_khz, step_khz, floor=floor, hops=hops)
    if series:
        for p in peaks:
            vals = series.get(p["frequency_mhz"]) or []
            act = 100.0 * sum(1 for v in vals if v >= mean_floor + 3.0) / len(vals) if vals else 0.0
            p["average_db"] = mean.get(p["frequency_mhz"])
            p["active_percent_of_sweep"] = round(act)
            p["pattern"] = "continuous" if act >= 80 else "intermittent"
    recheck_s = min(int(duration_s), 10) if duration_s else 0
    step_mhz = float(step_khz) / 1000.0
    kept, artifacts, checks = [], [], 0
    for p in peaks:
        lo, hi = p.pop("_lo"), p.pop("_hi")
        suspect = any(lo >= h - g - step_mhz and hi <= h + g + step_mhz for h in hops)
        if not suspect:
            kept.append(p)
            continue
        rec = {"frequency_mhz": p["frequency_mhz"], "snr_db": p["snr_db"]}
        if checks >= max_confirm:
            rec["reason"] = "sits exactly on a sweep-hop centre (receiver DC spike); not re-checked"
            artifacts.append(rec)
            continue
        checks += 1
        try:
            snr2 = _confirm_off_center(p["frequency_mhz"], step_khz, gain_db, serial, lock_timeout, recheck_s)
        except RFError as ex:
            snr2 = None
            rec["recheck_error"] = str(ex)[:120]
        if snr2 is not None and snr2 >= max(4.0, float(threshold_db) - 2.0):
            p["rechecked_off_centre_snr_db"] = snr2
            kept.append(p)
        else:
            rec["recheck_snr_db"] = snr2
            rec["reason"] = "only seen at a sweep-hop centre and gone when re-checked off-centre (receiver DC spike)"
            artifacts.append(rec)
    for i, p in enumerate(kept, start=1):
        p["peak_id"] = i
    return bins, floor, kept, artifacts, info


# ============================================================================
# DSP HELPERS
# ============================================================================
def _fir_lowpass(cutoff_hz, fs, taps=129):
    n = np.arange(taps, dtype=np.float64) - (taps - 1) / 2.0
    h = np.sinc(2.0 * cutoff_hz / fs * n) * np.hamming(taps)
    return (h / h.sum()).astype(np.float32)


def _mix_filter_decimate(z, fs, center_hz, cutoff_hz, decim, taps=129):
    """Shift center_hz to 0 Hz, low-pass filter, keep every decim-th sample. Polyphase: only the samples that are
    kept get computed (decim times less work than filtering at the full rate); the result is the same as
    np.convolve(..., 'valid')[::decim]."""
    # Mixer: the phase is worked out in float64 and wrapped to one turn first (stays accurate for long captures),
    # then cos/sin run in float32, which is cheaper than a complex128 exp.
    ph = (2.0 * np.pi * (((center_hz / fs) * np.arange(len(z), dtype=np.float64)) % 1.0)).astype(np.float32)
    lo = np.empty(len(z), dtype=np.complex64)
    lo.real = np.cos(ph)
    lo.imag = -np.sin(ph)
    x = z * lo
    h = _fir_lowpass(cutoff_hz, fs, taps)[::-1]      # convolution = correlation with the reversed filter
    d = max(1, int(decim))
    k = (len(x) - taps) // d + 1
    if k <= 0:
        return np.zeros(0, dtype=np.complex64)
    y = np.zeros(k, dtype=np.complex64)
    for p in range(min(d, taps)):                    # y[k] = sum_p sum_q h[q*d+p] * x[(k+q)*d+p]
        y += np.correlate(x[p::d], h[p::d].astype(np.complex64), mode="valid")[:k]
    return y


@functools.lru_cache(maxsize=16)
def _hann32(nfft):
    return np.hanning(nfft).astype(np.float32)


def _psd(z, fs, nfft=1024):
    nseg = len(z) // nfft
    if nseg < 4:
        raise RFError("IQ capture too short for analysis.")
    seg = z[: nseg * nfft].reshape(nseg, nfft) * _hann32(nfft)
    P = np.mean(np.abs(np.fft.fft(seg, axis=1)) ** 2, axis=0)
    return np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / fs)), np.fft.fftshift(P)


RTL_SDR_BUF = 16 * 16384        # rtl_sdr's output buffer in bytes (its -b default)
SDR_START_S = float(_env("SDR_START_S", "10"))   # how long rtl_sdr may take to send its first samples
_RTL_BENIGN = ("Found ", "Using device", "Detached kernel", "Exact sample rate", "Sampling at", "Tuned to",
               "Tuner gain", "Reading samples", "Allocating", "[R82XX]", "Disabled direct", "Enabled direct",
               "Tuner error set", "Bias-T", "User cancel", "Signal caught", "Supported gain", "  ")


def _end_radio(p, sig=signal.SIGINT, drain=False) -> str:
    """Stop a radio program and wait until it has exited, the way every ARIS stop does it (arislib/radioproc.py):
    `sig` (Ctrl+C by default: the rtl-sdr tools' own clean exit), a short grace, then SIGKILL. librtlsdr's cancel
    can hang (the real PC, 2026-11-02: rtl_sdr stopped writing at Ctrl+C but never exited), and a program still
    alive keeps its dongle, so callers release the dongle's lock only after this returns: its USB settle time then
    counts from the moment the program was really gone. Returns how it ended (radioproc.stop)."""
    how = radioproc.stop(p, sig=sig, drain=drain)
    if how in ("killed", "stuck"):
        args = [str(x) for x in (p.args if isinstance(p.args, (list, tuple)) else [p.args])]
        what = os.path.basename(args[0]) if args else "radio program"
        ser = args[args.index("-d") + 1].lstrip(":") if "-d" in args[:-1] else "?"
        if how == "killed":
            log.info("%s on %s did not exit after %s within %.1f s: killed", what, ser,
                     "Ctrl+C" if sig == signal.SIGINT else (signal.Signals(sig).name if sig else "no signal"),
                     radioproc.STOP_GRACE_S)
        else:
            log.warning("%s on %s is still running %.0f s after SIGKILL: its dongle may stay busy until it is "
                        "replugged", what, ser, radioproc.KILL_WAIT_S)
    return how


def _close_pipes(p, reader=None) -> None:
    """Close a finished program's pipes. Its stderr stays open while a reader thread still reads it (only a
    program that survived SIGKILL keeps one busy; closing under a reader can block)."""
    for f in (p.stdout, p.stderr):
        if f is None or (f is p.stderr and reader is not None and reader.is_alive()):
            continue
        try:
            f.close()
        except (OSError, ValueError):
            pass


def _rtl_err_tail(text) -> str:
    """What rtl_sdr said that matters (its start-up chatter left out): the last two lines, short."""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip() and not ln.startswith(_RTL_BENIGN)]
    return "; ".join(lines[-2:])[:160]


def _capture_raw(serial, tune_hz, fs, n, gain_db, timeout, ppm=None, min_bytes=None, stall_s=5.0) -> bytes:
    """Raw IQ bytes from rtl_sdr straight into memory (caller holds the dongle lock): 2 bytes per sample. Read as it
    streams: a dongle that stops sending for stall_s seconds is given up at once (not after the whole timeout), and
    what did arrive is kept when it is at least min_bytes (default: half a second). Errors carry what rtl_sdr said
    and how much arrived, so a hardware problem can be told from a slow USB link."""
    import select
    _check_stop()
    if not _have_tool("rtl_sdr", serial):
        raise RFError(_tool_missing("rtl_sdr", serial))
    # rtl_sdr never stops when the byte count is a whole number of its 256 kB buffers (seen on the real PC: -n
    # 12582912 ran until killed): ask for a few samples more, keep only what is needed
    n_req = int(n) + 4096
    if (2 * n_req) % RTL_SDR_BUF == 0:
        n_req += 1
    cmd = ["rtl_sdr", "-d", str(serial), "-f", str(int(tune_hz)), "-s", str(int(fs)), "-g", str(gain_db)]
    cmd += _ppm_args(serial, ppm) + ["-n", str(n_req), "-"]
    want = 2 * int(n)
    need = min(want, int(fs)) if min_bytes is None else min(want, int(min_bytes))
    p = subprocess.Popen(_radio_cmd(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    err = []
    th = threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True)   # never let stderr fill up
    th.start()
    buf, t0 = bytearray(), time.time()
    last, why, fd = t0, None, p.stdout.fileno()
    try:
        while len(buf) < want:
            if _stop_requested():
                break
            r, _w, _x = select.select([fd], [], [], 0.5)
            now = time.time()
            if r:
                chunk = os.read(fd, min(1 << 20, want - len(buf)))
                if not chunk:
                    break                            # rtl_sdr ended
                buf += chunk
                last = now
            elif now - last > (stall_s if buf else max(stall_s, SDR_START_S)):
                why = (f"stopped sending samples for {stall_s:.0f} s" if buf else
                       f"sent nothing for {max(stall_s, SDR_START_S):.0f} s after starting")
                break
            if now - t0 > timeout:
                why = "was too slow"
                break
        if len(buf) >= want:                         # let rtl_sdr finish on its own (a clean USB release)
            end = time.time() + 2.5
            while p.poll() is None and time.time() < end:
                r, _w, _x = select.select([fd], [], [], 0.25)
                if r and not os.read(fd, 1 << 20):
                    break
    finally:
        _end_radio(p, drain=True)                    # Ctrl+C ("User cancel") while its pipe is read: a clean exit
        th.join(2)
        _close_pipes(p, th)
    _check_stop()                                    # Reset Scanner stopped the capture part-way
    if len(buf) >= want or (len(buf) >= need and why is None):
        return bytes(buf)
    said = _rtl_err_tail(err[0].decode(errors="replace") if err and err[0] else "")
    got = f"{len(buf) / 2.0 / fs:.1f} of {int(n) / fs:.1f} s arrived"
    if len(buf) >= need:
        log.warning("rtl_sdr on %s %s (%s); using what arrived", serial, why, got)
        return bytes(buf)
    what = why or f"ended early (code {p.returncode})"
    raise RFError(f"rtl_sdr on {_short(serial)} {what}: {got}" + (f" (rtl_sdr said: {said})" if said else "")
                  + (". Replug the dongle if this keeps happening" if why and ("stopped" in why or "nothing" in why)
                     else ""))


def _raw_to_iq(raw, dc_from=None) -> np.ndarray:
    """rtl_sdr bytes -> DC-removed complex64. dc_from: measure the DC offset from the first dc_from samples only
    (a recorded burst's noise before it: a burst at the tuning would otherwise lose its own mean)."""
    raw = np.frombuffer(raw, dtype=np.uint8)
    iq = (raw[: (len(raw) // 2) * 2].astype(np.float32) - 127.4) / 128.0
    z = iq.view(np.complex64)                        # interleaved I/Q float32 is already complex64 in memory
    z -= z[:dc_from].mean() if dc_from and len(z) > dc_from else z.mean()
    return z


def _capture_iq(serial, tune_hz, fs, n, gain_db, timeout, ppm=None):
    """Raw IQ capture with rtl_sdr straight into memory (caller holds the dongle lock). Returns DC-removed
    complex64."""
    return _raw_to_iq(_capture_raw(serial, tune_hz, fs, n, gain_db, timeout, ppm))


def _burst_windows(active, max_gap, min_len):
    a = active.astype(np.int8)
    d = np.diff(np.concatenate(([0], a, [0])))
    starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
    if len(starts) == 0:
        return []
    wins = []
    cs, ce = int(starts[0]), int(ends[0])
    for s, e in zip(starts[1:], ends[1:]):
        if s - ce <= max_gap:
            ce = int(e)
        else:
            wins.append((cs, ce))
            cs, ce = int(s), int(e)
    wins.append((cs, ce))
    return [(s, e) for s, e in wins if e - s >= min_len]
