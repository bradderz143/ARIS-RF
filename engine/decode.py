"""ARIS engine, part "decode": Stage 1 (noise-referenced IQ analysis, the OOK/FSK bit slicer, wideband FM with RDS), stage 2
(rtl_433: devices, fingerprints, codes stripped by _safe_packet), stage 3 (rtl_fm -> multimon-ng / atest, tones),
and which decoders are installed with their self-tests.

Executed into aris.py's namespace by aris._include (see aris.py): names from aris.py's imports and from the
parts before this one are available here without imports, and everything defined here becomes aris.<name>.
"""
from __future__ import annotations

# ============================================================================
# STAGE 1: NOISE-REFERENCED IQ ANALYSIS + OOK/FSK BIT SLICER
# ============================================================================
def _stage1_wideband(center_mhz, serial, gain_db, duration_sec=3.0):
    """Wide signals (FM broadcast, anything over ~120 kHz) need a wide view: 1 MHz capture, the signal
    sits 250 kHz off the IQ centre, +-110 kHz channel. Confirms broadcast-style wideband FM from the
    waveform itself (constant envelope, large deviation), not just from the band plan, and reads the station's
    RDS data (its name and PI code) when it sends any: that names the station instead of guessing."""
    fs, shift, decim = 1024000, 250000, 4
    fsd = fs / decim
    try:
        z = _capture_iq(serial, int(round(center_mhz * 1e6)) - shift, fs, int(fs * duration_sec), gain_db,
                        timeout=int(duration_sec) + 12)
    except RFError as e:
        return {"status": f"IQ capture failed: {e}", "signal_present_during_capture": False}
    try:
        f, P = _psd(z, fs, 2048)
        ref = (np.abs(f) > 20e3) & (np.abs(f - shift) > 250e3) & (np.abs(f) < 450e3)
        floor = float(np.percentile(P[ref], 20)) if ref.any() else float(np.percentile(P, 20))
        win = np.abs(f - shift) <= 120e3
        snr_db = 10.0 * math.log10(float(np.mean(P[win])) / (floor + 1e-20))
        peak_db = 10.0 * math.log10(float(np.max(P[np.abs(f - shift) <= 100e3])) / (floor + 1e-20))
        band = np.abs(f - shift) <= 250e3
        ex = np.maximum(P[band] - floor, 0.0)
        obw_khz = 0.0
        if ex.sum() > 0:
            cum = np.cumsum(ex) / ex.sum()
            fb = f[band]
            obw_khz = float(fb[int(np.searchsorted(cum, 0.995))] - fb[int(np.searchsorted(cum, 0.005))]) / 1e3
        y = _mix_filter_decimate(z, fs, shift, 110e3, decim, taps=63)
        return _wbfm_analyse(y, fsd, center_mhz, snr_db, obw_khz, serial, peak_snr_db=peak_db)
    except Exception as e:
        log.error("wideband stage failed:\n%s", traceback.format_exc())
        return {"status": f"Wideband IQ analysis error: {e}", "signal_present_during_capture": False}


def _wbfm_analyse(y, fsd, center_mhz, snr_db, obw_khz, serial, peak_snr_db=None) -> dict:
    """A wide channel already at 0 Hz (y at fsd, about 256 kHz, the channel's centre is center_mhz), with its SNR
    and occupied bandwidth measured from the capture: broadcast FM or not, stereo pilot, RDS (name and PI code),
    the carrier against the channel plan. Stations found are remembered for the Spectrum list."""
    env = np.abs(y)
    cv = float(np.std(env) / (np.mean(env) + 1e-12))
    inst = np.angle(y[1:] * np.conj(y[:-1])).astype(np.float64) * fsd / (2.0 * np.pi)
    off = float(np.mean(inst))
    dev = float(np.std(inst - off))
    present = snr_db >= 6.0
    wbfm = bool(present and cv < 0.35 and dev > 8e3 and obw_khz >= 80.0)
    # Broadcast fingerprints in the demodulated signal: a 19 kHz stereo pilot and RDS data at 57 kHz.
    # Real stations nearly always carry one or both; cheap FM bugs and car FM transmitters usually don't.
    pilot = rds = False
    pilot_db = rds_db = None
    nf = 8192
    nseg = len(inst) // nf
    if present and nseg >= 4:
        seg = (inst[: nseg * nf] - off).reshape(nseg, nf) * np.hanning(nf)
        S = np.mean(np.abs(np.fft.rfft(seg, axis=1)) ** 2, axis=0)
        fq = np.fft.rfftfreq(nf, 1.0 / fsd)
        sel = lambda lo, hi: S[(fq >= lo) & (fq <= hi)]
        ref19 = np.concatenate([sel(17500, 18700), sel(19300, 20500)])
        pilot_db = 10.0 * math.log10(float(sel(18900, 19100).max()) / (float(np.median(ref19)) + 1e-20))
        rds_db = 10.0 * math.log10(float(np.mean(sel(55600, 58400))) / (float(np.mean(sel(60000, 64000))) + 1e-20))
        pilot, rds = pilot_db >= 12.0, rds_db >= 6.0
    rds_info = {}
    if present and (wbfm or pilot or rds or dev > 8e3):   # mono stations send RDS too: no pilot needed
        try:
            from arislib import rds as _rds
            rd = _rds.decode(inst - off, fsd)
            rds = bool(rds or rd.get("rds_present") or rd.get("pi"))
            if rd.get("pi"):                     # complete, checksum-verified RDS groups: RDS is there
                rds_info = {"rds_station_name": rd.get("ps"), "rds_pi_code": rd.get("pi"),
                            "rds_groups_decoded": rd.get("groups")}
                if len(rd.get("ps_all") or []) > 1:     # the station rotates its name
                    rds_info["rds_station_names"] = rd["ps_all"]
                if not rd.get("ps") and rd.get("ps_partial"):
                    rds_info["rds_station_name_partial"] = rd["ps_partial"]
                if not rd.get("ps") and rd.get("ps_scrolling"):    # the station scrolls text through its name
                    rds_info.update(rds_station_name=rd["ps_scrolling"], rds_name_scrolls=True)
                    rds_info.pop("rds_station_name_partial", None)
        except Exception as e:              # RDS is a bonus: never let it break the analysis
            log.warning("RDS decode failed: %s", e)
    if not wbfm and present and dev > 8e3 and obw_khz >= 80.0 and (pilot or rds):
        wbfm = True     # a stereo pilot or RDS is a broadcaster's mark, even when the level varies (multipath, an edge)
    carrier = center_mhz + off / 1e6
    k = int(round(carrier * 10.0))
    chan_off_khz = (carrier - k / 10.0) * 1000.0
    # The Americas (ITU Region 2) put FM stations on odd tenths: 88.1, 88.3 ... 107.9 MHz.
    on_channel = bool(k % 2 == 1 and 881 <= k <= 1079 and abs(chan_off_khz) <= FM_CHECK_TOL_KHZ)
    clearly_off = k % 2 == 0 or abs(chan_off_khz) >= FM_CHECK_OFF_KHZ
    named = bool(rds_info.get("rds_station_name")) and not clearly_off   # a name: a station (maybe a ppm error)
    if wbfm and _in_fm_band(carrier) and (on_channel or named or clearly_off):   # the Spectrum list uses this
        _remember_station(k / 10.0, rds_info.get("rds_station_name") if named else None,
                          rds_info.get("rds_pi_code"), serial, rds_info.get("rds_station_names"),
                          carrier_mhz=carrier, on_channel=not clearly_off, stereo=pilot, rds=rds,
                          scrolls=rds_info.get("rds_name_scrolls"))
    if wbfm:
        extras = [x for x, ok in (("stereo pilot", pilot), ("RDS", rds)) if ok]
        wave = ("Wideband FM, the standard FM radio waveform"
                + (f", with {' and '.join(extras)}" if extras else ", mono, no stereo pilot or RDS"))
        if rds_info.get("rds_station_name"):
            wave += (f"; RDS scrolling text \"{rds_info['rds_station_name']}\"" if rds_info.get("rds_name_scrolls")
                     else f"; RDS station name \"{rds_info['rds_station_name']}\"")
    elif present:
        wave = f"Wideband signal about {obw_khz:.0f} kHz wide (not standard broadcast FM)"
    else:
        wave = "Nothing transmitting during this capture"
    return {"status": "Wideband IQ analysed", "signal_present_during_capture": present,
            "physical_waveform_classification": wave, "wideband_fm": wbfm,
            "stereo_pilot_19khz": pilot, "rds_57khz": rds,
            "pilot_prominence_db": round(pilot_db, 1) if pilot_db is not None else None,
            "rds_prominence_db": round(rds_db, 1) if rds_db is not None else None,
            "on_standard_fm_channel": on_channel if present else None,
            "offset_from_channel_khz": round(chan_off_khz, 1) if present else None,
            "channel_snr_db": round(snr_db, 1), "occupied_bw_khz": round(obw_khz, 1),
            "envelope_variation_cv": round(cv, 3), "fm_deviation_std_khz": round(dev / 1e3, 1),
            "measured_carrier_mhz": round(carrier, 4) if present else None,
            "peak_snr_db": round(peak_snr_db, 1) if peak_snr_db is not None else None, **rds_info}


def _sync_word_match(bits):
    """A known radio chip's sync word at the start of a demodulated burst (KNOWN_SYNC_HEADERS), in words, or None.
    Works on the bits in memory and keeps none of them."""
    bits = str(bits or "")
    for shift in range(8):
        sb = bits[shift:]
        if len(sb) < 16:
            break
        tb = sb[:(len(sb) // 8) * 8]
        sh = f"{int(tb, 2):0{len(tb) // 4}X}"
        hit = next((d for k, d in KNOWN_SYNC_HEADERS.items() if k in sh), None)
        if hit:
            return f"{hit} (at +{shift} bit offset)"
    return None


def _extract_raw_digital_bursts(env, inst_freq_hz, fs_dec, active_mask):
    mask_int = active_mask.astype(np.int8)
    diffs = np.diff(np.concatenate(([0], mask_int, [0])))
    starts = np.where(diffs == 1)[0]
    ends = np.where(diffs == -1)[0]

    pulse_widths_us = []
    for s, e in zip(starts, ends):
        dur_us = ((e - s) / float(fs_dec)) * 1e6
        if dur_us >= 80.0:
            pulse_widths_us.append(dur_us)
    if not pulse_widths_us:
        return {"bursts_detected": 0, "estimated_symbol_rate_baud": 0,
                "median_pulse_width_us": 0.0, "raw_demodulated_hex_frames": []}

    short = [p for p in pulse_widths_us if 100.0 <= p <= 5000.0]
    base_us = float(np.percentile(short, 25)) if short else float(np.median(pulse_widths_us))
    base_us = max(100.0, min(base_us, 4000.0))
    est_baud = int(round(1e6 / base_us))
    sps = max(1, int(round((base_us / 1e6) * fs_dec)))

    max_gap = int(0.008 * fs_dec)
    windows = []
    cs, ce = starts[0], ends[0]
    for s, e in zip(starts[1:], ends[1:]):
        if (s - ce) <= max_gap:
            ce = e
        else:
            windows.append((cs, ce))
            cs, ce = s, e
    windows.append((cs, ce))

    def _hex(bits: str) -> str:
        bits += "0" * ((4 - len(bits) % 4) % 4)
        return "0x" + "".join(f"{int(bits[i:i + 4], 2):X}" for i in range(0, len(bits), 4))

    frames, modulation, sync_word = [], None, None
    for ps, pe in windows[:6]:
        if (pe - ps) < sps * 8:
            continue
        seg_env = env[ps:pe]
        n_syms = min(256, len(seg_env) // sps)
        if n_syms < 8:
            continue
        thresh = 0.5 * (float(np.max(seg_env)) + float(np.min(seg_env)))
        ook = "".join("1" if float(np.mean(seg_env[i * sps:(i + 1) * sps])) >= thresh else "0"
                      for i in range(n_syms))
        seg_f = inst_freq_hz[ps:min(pe, len(inst_freq_hz))]
        f_med = float(np.median(seg_f)) if len(seg_f) else 0.0
        fsk = "".join("1" if float(np.mean(seg_f[i * sps:(i + 1) * sps])) >= f_med else "0"
                      for i in range(n_syms) if len(seg_f[i * sps:(i + 1) * sps]) > 0)
        # the demodulated symbols are used here and now (which modulation carries data, a radio-chip sync word)
        # and never kept: before v4.9.4 the hex of every burst went into the Signals list, the Activity API and
        # last_investigation.json, which for a fixed-code remote is the code itself (the security review)
        has_fsk = bool(fsk) and set(fsk) != {"0"} and set(fsk) != {"1"}
        has_ook = bool(ook) and set(ook) != {"0"}
        if modulation is None:
            modulation = "FSK" if has_fsk else "OOK" if has_ook else "CARRIER"
        if sync_word is None:
            sync_word = _sync_word_match(fsk if has_fsk else ook if has_ook else "")
        frames.append({
            "burst_duration_ms": round(((pe - ps) / float(fs_dec)) * 1000.0, 2),
            "symbol_count": n_syms,
        })
    return {
        "bursts_detected": len(windows),
        "total_pulses_counted": len(pulse_widths_us),
        "median_pulse_width_us": round(float(np.median(pulse_widths_us)), 1),
        "estimated_symbol_rate_baud": est_baud,
        "modulation_guess": modulation or "CARRIER",
        "sync_word_match": sync_word,
        "raw_demodulated_hex_frames": frames,     # (lengths only since v4.9.4: no bits, no hex)
    }


def _stage1_iq_analysis(freq_mhz, serial, gain_db=19.7, bw_khz=25.0, duration_sec=2.5):
    """Decide whether anything is actually transmitting, using an empty part of the same capture as
    the noise reference, then classify it. Pure noise reports 'nothing transmitting'."""
    fs, offset, decim = 250000, 40000, 5     # signal sits 40 kHz off the IQ centre (away from the DC spike)
    fsd = fs / decim
    try:
        z = _capture_iq(serial, int(round(freq_mhz * 1e6)) - offset, fs, int(fs * duration_sec), gain_db,
                        timeout=int(duration_sec) + 12)
    except RFError as e:
        return {"status": f"IQ capture failed: {e}", "signal_present_during_capture": False}
    try:
        f, P = _psd(z, fs)
        hw = float(min(max(float(bw_khz or 25.0) * 1e3 / 2.0, 6e3), 30e3))
        Psm = np.convolve(P, np.ones(9) / 9.0, mode="same")
        usable = (np.abs(f) >= 8e3) & (np.abs(f) <= 100e3)
        region = np.abs(f - offset) <= 45e3
        floor0 = float(np.median(P[usable & ~region])) if (usable & ~region).any() else float(np.median(P))
        hot = region & (Psm > 2.5 * floor0)
        if hot.any():
            # Centre on the signal's energy (an FSK signal has two tones; argmax would pick one of them).
            ex = np.maximum(P[hot] - floor0, 0.0)
            fc = float(np.sum(f[hot] * ex) / (np.sum(ex) + 1e-20))
            width = float(f[hot].max() - f[hot].min())
            hw = float(min(max(hw, width / 2.0 + 3e3), 40e3))
        else:
            search = (f >= offset - 20e3) & (f <= offset + 20e3)
            fc = float(f[search][int(np.argmax(Psm[search]))])
        ring = usable & (np.abs(f - fc) > hw + 3e3) & (np.abs(f - fc) <= hw + 25e3)
        local_floor = float(np.median(P[ring])) if ring.any() else float(np.median(P[usable]))
        chan = np.abs(f - fc) <= hw
        psd_ratio = float(np.mean(P[chan])) / (local_floor + 1e-20)
        avg_snr_db = round(10.0 * math.log10(max(psd_ratio - 1.0, 1e-3)), 1)

        y = _mix_filter_decimate(z, fs, fc, hw, decim)
        pch = np.abs(y).astype(np.float64) ** 2
        blk = int(fsd * 0.01)                        # 10 ms blocks
        nb = len(pch) // blk
        bmeans = pch[: nb * blk].reshape(nb, blk).mean(axis=1) if nb else np.array([float(pch.mean())])

        cands = [c for c in (-90e3, -65e3, 85e3, -30e3)
                 if abs(c - fc) >= 2 * hw + 10e3 and abs(c) >= hw + 8e3 and abs(c) + hw <= 105e3]
        if cands:
            ref = min(cands, key=lambda c: float(np.mean(P[np.abs(f - c) <= hw])))
            pref = np.abs(_mix_filter_decimate(z, fs, ref, hw, decim)).astype(np.float64) ** 2
            ref_psd = float(np.median(P[np.abs(f - ref) <= hw]))
            noise = float(np.median(pref)) / math.log(2.0) * (local_floor / (ref_psd + 1e-20))
        else:
            noise = float(np.percentile(bmeans, 20))
        noise = max(noise, 1e-12)

        smooth = np.convolve(pch, np.ones(4) / 4.0, mode="same")
        active = smooth > 5.0 * noise                 # 7 dB above noise, ~80 us smoothing
        block_active = bmeans > 2.0 * noise           # 3 dB above noise over 10 ms
        block_duty = float(block_active.mean())
        active_ms = float(active.sum()) / fsd * 1000.0
        present = bool((block_active.any() and active_ms >= 1.0) or psd_ratio >= 2.0)

        peak_snr_db = round(10.0 * math.log10(max(float(bmeans.max()) / noise - 1.0, 1e-3)), 1)
        res = {"status": "IQ analysed",
               "classification_basis": "heuristic from IQ statistics, not a confirmed identification",
               "signal_present_during_capture": present,
               "channel_avg_snr_db": avg_snr_db,
               "peak_snr_db": peak_snr_db,           # strongest 10 ms: the fair level for short bursts
               "active_time_ms": round(active_ms, 1),
               "block_duty_percent": round(block_duty * 100.0, 1)}
        if not present:
            res["physical_waveform_classification"] = "Nothing transmitting during this capture (idle channel or noise)"
            return res

        inst = np.angle(y[1:] * np.conj(y[:-1])).astype(np.float64) * fsd / (2.0 * np.pi)
        sel = active[1:] if int(active[1:].sum()) >= 50 else np.ones(len(inst), dtype=bool)
        # Midpoint of the 10th/90th percentiles: the true centre for FSK (two tones) as well as a carrier.
        carrier_off = 0.5 * float(np.percentile(inst[sel], 10) + np.percentile(inst[sel], 90))
        dev_std = float(np.std(inst[sel] - carrier_off))
        env = np.abs(y)
        continuous = block_duty >= 0.9
        wins = [(0, len(env))] if continuous else _burst_windows(active, int(0.008 * fsd), int(0.0005 * fsd))
        if wins:
            wmask = np.zeros(len(env), dtype=bool)
            for s_, e_ in wins:
                wmask[s_:e_] = True
            env_in = env[wmask]
        else:
            env_in = env[active] if active.any() else env
        cv = float(np.std(env_in) / (np.mean(env_in) + 1e-12))
        # Share of the signal's power in one narrow spectral line: ~1 for a plain unmodulated carrier.
        exc = np.maximum(P[chan] - local_floor, 0.0)
        fch = f[chan]
        if exc.sum() > 0:
            kpk = int(np.argmax(exc))
            line_frac = float(exc[np.abs(fch - fch[kpk]) <= 600.0].sum() / exc.sum())
        else:
            line_frac = 0.0

        if continuous:
            if line_frac >= 0.6:
                wave = "Steady unmodulated carrier (an idle transmitter carrier or a clock signal from equipment)"
            elif cv < 0.35:
                wave = "Continuous constant-envelope FM/FSK transmission"
            else:
                wave = "Continuous AM or mixed-modulation transmission"
        else:
            ism = _in_sensor_band(freq_mhz)       # device hints only where those devices actually live
            if cv > 0.5:
                wave = "Intermittent OOK/ASK bursts" + (
                    " (typical of remotes, door sensors, simple weather sensors)" if ism else "")
            elif dev_std > 1500.0:
                wave = "Intermittent FSK/FM transmissions" + (" (typical of packet telemetry sensors)" if ism else "")
            else:
                wave = "Intermittent bursts (modulation unclear)"

        total_off_hz = fc - offset + carrier_off
        res.update({
            "physical_waveform_classification": wave,
            "continuous": continuous,
            "bursts_detected": 0 if continuous else len(wins),
            "measured_carrier_mhz": round(freq_mhz + total_off_hz / 1e6, 5),
            "carrier_offset_from_peak_khz": round(total_off_hz / 1e3, 2),
            "fm_fsk_deviation_std_hz": round(dev_std, 1),
            "envelope_variation_cv": round(cv, 3),
            "narrow_line_fraction": round(line_frac, 2),
        })
        if not continuous and wins and _in_sensor_band(freq_mhz):
            res["native_bitstream_extractor"] = _extract_raw_digital_bursts(env, inst - carrier_off, fsd, active)
        return res
    except Exception as e:
        log.error("stage1 failed:\n%s", traceback.format_exc())
        return {"status": f"IQ analysis error: {e}", "signal_present_during_capture": False}


# ============================================================================
# STAGE 2: rtl_433 DEEP SCAN
# ============================================================================
_RTL433_HELP = None


def _rtl433_extra_flags():
    """-Y autolevel adapts the detection level to the noise. Never -G (enables protocols rtl_433 disables because
    they cause false decodes) and never -Y minmax: forcing that FSK detector stops OOK remotes from decoding;
    rtl_433 already picks the right pulse detector for each band by itself."""
    global _RTL433_HELP
    if _RTL433_HELP is None:
        try:
            p = subprocess.run(["rtl_433", "-h"], capture_output=True, text=True, timeout=8)
            _RTL433_HELP = (p.stdout or "") + (p.stderr or "")
        except Exception:
            _RTL433_HELP = ""
    return ["-Y", "autolevel"] if "autolevel" in _RTL433_HELP else []


def _rtl433_device(serial) -> list:
    """rtl_433 picks a dongle by serial only with a ':' in front ('-d :00001001'). A bare number is a device INDEX
    there, so '-d 00001001' asked for dongle number 1001 and rtl_433 exited with code 2 before v4.7.1. (rtl_sdr,
    rtl_power, rtl_fm, rtl_test and rtl_adsb take the bare serial.)"""
    return ["-d", ":" + str(serial)]


# rtl_433 only prints its own errors and warnings with "-F log" (to stderr, as "SOURCE: message" lines)
_R433_NOISE = ("Use \"-F log\"", "rtl_433 version", "Consider using", "Registered", "Trying conf", "Reading conf",
               "Please donate", "New defaults", "Detached kernel", "Allocating", "Exact sample rate", "Tuned to",
               "Sample rate set", "Bit detection level")


def _rtl433_log_lines(text) -> list:
    """rtl_433's own messages from its merged output: not JSON, not the pulse analyzer."""
    out = []
    for raw in (text or "").splitlines():
        s = _ANSI.sub("", raw).strip()
        if s and not s.startswith("{") and not s.startswith(_A_LINES) and not _A_DET.search(s):
            out.append(s)
    return out


def _rtl433_problem(lines, rc=None) -> str:
    """Why rtl_433 could not run, in plain words."""
    low = " | ".join(lines).lower()
    if "could not find device" in low or "no supported devices" in low or "no matching device" in low:
        return "rtl_433 could not find the dongle (unplugged, or its serial number changed)"
    if any(w in low for w in ("usb_claim_interface", "resource busy", "failed to open", "unable to open",
                              "kernel driver")):
        return ("rtl_433 could not open the dongle (another program is using it, or the DVB TV driver has claimed "
                "it: blacklist dvb_usb_rtl28xxu)")
    if "lost" in low and "samples" in low:
        return "the dongle is losing samples (USB bandwidth or power: try another port or a powered hub)"
    useful = [x for x in lines if not x.startswith(_R433_NOISE) and "-F log" not in x]
    return (f"rtl_433 stopped" + (f" with code {rc}" if rc not in (None, 0) else "")
            + (f": {useful[-1][:140]}" if useful else ""))


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_A_DET = re.compile(r"Detected (OOK|FSK) package")
_AT_S = re.compile(r"@(\d+(?:\.\d+)?)s")      # a position inside a recording ("@0.040000s"), file mode only
_A_TOT = re.compile(r"Total count:\s*(\d+),\s*width:\s*([\d.]+)\s*ms")
_A_LVL = re.compile(r"RSSI:\s*(-?[\d.]+)\s*dB\s+SNR:\s*(-?[\d.]+)\s*dB")
_A_OFF = re.compile(r"Frequency offsets \[F1, F2\]:[^(]*\(([+-]?[\d.]+)\s*kHz")
_A_GUESS = re.compile(r"Guessing modulation:\s*(.+)")
_A_DEMOD = re.compile(r"short_width:\s*(\d+),\s*long_width:\s*(\d+)")


def _pattern_hint(mod, guess, short_us, long_us, bits=None):
    """What kind of device usually sends a burst like this (a pattern-based guess, not an identification).
    EV1527 / PT2262 remotes send 24 data bits (25 with the sync pulse), so another packet length rules them out."""
    g = (guess or "").lower()
    if g in ("", "pattern unclear", "single pulse", "unclear") or (bits is not None and bits < 8):
        return None                         # no pattern to match (v4.8.6: 'pattern unclear' -> 'remotes')
    if mod == "OOK" and "pulse width" in g and short_us and long_us and 250 <= short_us <= 700 \
            and 2.3 <= long_us / short_us <= 3.7 and bits in (None, 24, 25):
        return ("fixed-code remotes (EV1527/PT2262 chips): doorbells, PIR motion sensors, door/window contacts, "
                "gate and garage remotes")
    if mod == "OOK" and "pulse width" in g:
        return "remotes and simple sensors"
    if mod == "OOK" and ("position" in g or "distance" in g):
        return "weather sensors and some remotes"
    if mod == "OOK" and "manchester" in g:
        return "weather sensors, tyre-pressure sensors and some alarm sensors"
    if mod == "OOK":
        return "remotes and simple sensors"
    if mod == "FSK":
        return "smart meters, tyre-pressure sensors, modern alarm/door sensors and telemetry modules"
    return None


_CODINGS = {"OOK_PWM": "pulse-width modulation (PWM)", "OOK_PPM": "pulse-position modulation (PPM)",
            "OOK_PCM": "pulse-code modulation (PCM)", "OOK_RZ": "pulse-code modulation (RZ)",
            "OOK_MC_ZEROBIT": "Manchester coding", "OOK_DMC": "differential Manchester coding",
            "OOK_PIWM_RAW": "pulse-interval-width modulation", "OOK_PIWM_DC": "pulse-interval-width modulation",
            "OOK_NRZS": "NRZ-S coding", "OOK_MC_OSV1": "Manchester coding (Oregon v1)",
            "FSK_PCM": "FSK pulse-code modulation (PCM)", "FSK_PWM": "FSK pulse-width modulation",
            "FSK_MC_ZEROBIT": "FSK Manchester coding"}


def _coding_text(c):
    return _CODINGS.get(str(c or ""), str(c or "").replace("_", " ").lower() or None)


class _R433Reader:
    """Reads rtl_433 output line by line, with its stderr merged into stdout so the order is kept. For each
    package rtl_433 prints "Detected OOK|FSK package", then the JSON of anything a decoder recognised, then its
    pulse analyzer's description (modulation, pulse widths, level, and how to demodulate it). So every package is
    either decoded (a JSON line inside it) or not, and an unknown device is told apart from known devices heard at
    the same time. Only lengths and timings are kept from the analyzer, never the bits it demodulated."""

    def __init__(self):
        self.cur = None
        self.last = 0.0

    def _close(self):
        b, self.cur = self.cur, None
        if not b:
            return []
        bits = b.pop("_bits", [])
        gaps = b.pop("_gaps", [])
        b.pop("_sec", None)
        if bits:                                     # rtl_433 versions that print the demodulated rows
            b["bits"] = Counter(bits).most_common(1)[0][0]
            b["rows"] = b.get("rows") or len(bits)
        elif b.get("coding") and b.get("pulses"):    # otherwise: packets = gaps longer than the row gap limit
            g = b.get("gap_limit") or 0
            rows = 1 + (sum(n for n, w in gaps if w > g) if g else 0)
            b["rows"] = rows
            if b["coding"] in ("OOK_PWM", "FSK_PWM"):  # one pulse per bit
                b["bits"] = int(round(b["pulses"] / float(rows)))
        b["demodulated"] = bool(b.get("coding"))
        return [("package", b)]

    def feed(self, raw):
        """One output line -> a list of events: ("packet", decoded JSON) and ("package", one finished package)."""
        self.last = time.time()
        s = _ANSI.sub("", raw).strip()
        if not s:
            return []
        if s.startswith("{") and s.endswith("}"):
            try:
                obj = json.loads(s)
            except ValueError:
                return []
            if not (isinstance(obj, dict) and obj.get("model")):
                return []
            if self.cur is not None:
                self.cur["decoded"] = True
                self.cur.setdefault("models", []).append(str(obj["model"]))
            return [("packet", obj)]
        m = _A_DET.search(s)
        if m:
            ev = self._close()
            self.cur = {"mod": m.group(1), "decoded": False}
            at = _AT_S.search(s)
            if at:
                self.cur["at"] = float(at.group(1))
            return ev
        if self.cur is not None and _analyzer_fields(self.cur, s):
            return []
        return [("text", s)]

    def flush(self):
        return self._close()


def _fp_key(b):
    """Packages that look alike (same modulation, coding and pulse widths) come from the same kind of device."""
    if b.get("mod") == "FSK":
        return ("FSK", b.get("coding"))
    return (b.get("mod"), b.get("coding") or b.get("guess"), round((b.get("short") or 0) / 50.0),
            round((b.get("long") or 0) / 50.0))


def _clear_pattern(fp):
    return (fp.get("demodulated") or str(fp.get("pattern")) not in ("pattern unclear", "single pulse", "unclear"))


def _fingerprints(packages):
    """Undecoded packages -> one fingerprint per kind of signal, the ones rtl_433 could demodulate first."""
    fps = {}
    for b in packages:
        if b.get("decoded"):
            continue
        fp = fps.setdefault(_fp_key(b), {
            "modulation": b["mod"], "pattern": b.get("guess") or "unclear", "coding": _coding_text(b.get("coding")),
            "demodulated": bool(b.get("coding")), "short_pulse_us": b.get("short"), "long_pulse_us": b.get("long"),
            "bursts": 0, "snr_db": None, "pulses_per_burst": b.get("pulses"), "burst_ms": b.get("ms"),
            "offset_khz": b.get("off"), "bits_per_packet": b.get("bits"), "packets_per_burst": b.get("rows")})
        fp["bursts"] += 1
        if b.get("snr") is not None and (fp["snr_db"] is None or b["snr"] > fp["snr_db"]):
            fp["snr_db"] = b["snr"]
    out = sorted(fps.values(), key=lambda f: (not f["demodulated"], not _clear_pattern(f), -f["bursts"],
                                              -(f["snr_db"] or 0)))[:4]
    for fp in out:
        fp["pattern_hint"] = _pattern_hint(fp["modulation"], fp["pattern"], fp["short_pulse_us"], fp["long_pulse_us"],
                                           fp.get("bits_per_packet"))
    return out


def _fp_line(f, hint=True) -> str:
    """One undecoded signal's fingerprint in plain words: 'OOK pulse-width modulation (PWM), 400/1200 us pulses,
    28-bit packets sent 4 times'. Lengths and timings only, never the content."""
    how = f.get("coding") or str(f.get("pattern") or "unclear pattern").lower()
    mod = str(f.get("modulation") or "")
    bits = [how if how.upper().startswith(mod.upper()) and mod else f"{mod} {how}".strip()]
    if f.get("short_pulse_us") and f.get("long_pulse_us") and f["long_pulse_us"] > 20:
        bits.append(f"{f['short_pulse_us']}/{f['long_pulse_us']} us pulses")
    if f.get("bits_per_packet"):
        n = f.get("packets_per_burst") or 1
        bits.append(f"{f['bits_per_packet']}-bit packets" + (f" sent {n} times" if n > 1 else ""))
    elif (f.get("packets_per_burst") or 0) > 1:
        bits.append(f"packet sent {f['packets_per_burst']} times")
    if f.get("snr_db") is not None:
        bits.append(f"SNR {float(f['snr_db']):.1f} dB")
    txt = ", ".join(bits)
    if hint and f.get("pattern_hint"):
        txt += f" (this pattern is used by {f['pattern_hint']}: a pattern match, not an identification)"
    return txt


NOISE_MIN_PULSES = 8          # a device's packet has more pulses (and bits) than this
NOISE_MIN_SNR_DB = 6.0        # rtl_433 reports its own trigger level (about 4.8 dB) for noise
NOISE_MIN_PULSE_US = 50.0     # no OOK device sends pulses this short: rtl_433's OOK view of an FSK burst or noise


def _noise_package(b) -> bool:
    """An undecoded rtl_433 package that is noise, not a device: a few pulses or bits, or a short package at
    rtl_433's trigger level. On the live station (v4.8.6) '40/60 us pulses, 2-bit packets, SNR 4.8 dB' made both
    dongles 'hear' 433.92 (the IQ look heard nothing, -2.8 dB) and was saved as an unknown device."""
    if b.get("decoded"):
        return False
    pulses, bits, snr = b.get("pulses"), b.get("bits"), b.get("snr")
    short = b.get("short")
    if short is not None and 0 < float(short) < NOISE_MIN_PULSE_US and str(b.get("mod") or "OOK") == "OOK":
        return True            # live (v4.8.8): "OOK PWM, 16/37 us pulses, 9-bit packets, 18 dB" next to a TPMS decode
    if pulses is not None and pulses < NOISE_MIN_PULSES:
        return True
    if bits is not None and bits < NOISE_MIN_PULSES:
        return True
    return snr is not None and snr < NOISE_MIN_SNR_DB and (pulses or 0) < 30


def _at_seconds(v):
    m = _AT_S.search(str(v or ""))
    return float(m.group(1)) if m else None


def _parse_rtl433_timed(text):
    """rtl_433 output -> [(position_s or None, "packet" | "package", obj)] in order, noise packages left out. The
    position is where in a recording the package sat (file mode: a packet's "time" "@0.04s", the analyzer's
    "Detected OOK package @0.04s"); None when rtl_433 printed none (live mode)."""
    rd, out = _R433Reader(), []
    for line in (text or "").splitlines():
        for kind, obj in rd.feed(line):
            if kind == "packet":
                tm = obj.get("time")
                out.append((_at_seconds(tm) if isinstance(tm, str) and tm.startswith("@") else None, kind, obj))
            elif kind == "package" and not _noise_package(obj):
                out.append((obj.get("at"), kind, obj))
    out += [(b.get("at"), "package", b) for _, b in rd.flush() if not _noise_package(b)]
    return out


def _parse_rtl433(text):
    """rtl_433 output -> (decoded packets, fingerprints of the packages no decoder recognised, package count).
    Noise packages (_noise_package) are left out of both. Needs stderr merged into stdout in order (see
    _R433Reader); with separate streams every package looks undecoded, which only matters for the fingerprints."""
    items = _parse_rtl433_timed(text)
    packets = [o for _, k, o in items if k == "packet"]
    real = [o for _, k, o in items if k == "package"]
    return packets, _fingerprints(real), len(real)


# Fields that carry (part of) a remote's code. Besides the generic ones, rtl_433's remote decoders report the KeeLoq
# hopping code as 'encrypted' (Microchip-HCS200), the Security+ fixed code as remote_id + button_id (Secplus-v1/v2),
# the switch code as id + unit + group (Nexa / Proove), and 'command', 'dipswitch', house / switch IDs elsewhere.
# Any '*_code' field counts too (_is_code_key). rtl_433 25.12 also reports the Security+ v1 fixed code as id0 /
# id1 with the keypad's pin and pad_id, the Interlogix sensor's raw bits as raw_message, the Cardin remote's button
# as rbutton, and some decoders' spare bits as extradata.
_CODE_KEYS = {"tristate", "code", "codes", "cmd", "button", "rows", "data", "raw", "raw_msg", "raw_message",
              "rolling", "fixed", "secret", "counter", "encrypted", "remote_id", "button_id", "button_code",
              "command", "unit", "group", "group_call", "rolling_code", "fixed_code", "hopping_code", "dipswitch",
              "switch_id", "house_id", "house_code", "unit_code", "system_code", "cmd_id", "key", "pin", "pad_id",
              "id0", "id1", "rbutton", "extradata"}
# Models whose ID is part of what a receiver learns (remotes, switches, fobs, alarm and door sensors): the ID is
# hashed even when the decoder reports no code field.
_REMOTE_MODEL_RX = re.compile(r"remote|hcs\d|keeloq|secplus|security|nexa|proove|intertechno|somfy|car[-_ ]?remote|"
                              r"key[-_ ]?fob|keyfob|garage|gate|doorbell|switch|kerui|chuango|smartwares|x10|elro|"
                              r"cardin|chamberlain|liftmaster|button", re.I)


def _is_code_key(k) -> bool:
    k = str(k)
    return k in _CODE_KEYS or k.lower().endswith("_code")


_LABEL_SECRET = {"key": None}


def _label_secret() -> bytes:
    """The key behind a remote's label (#3fa2c1): random, made once, kept in DATA_DIR/label_secret (mode 600) and
    never logged or shown. Clear logs and Clear data keep it, so a remote keeps its label. Without a key (v4.8.7
    and before: sha1 of 'model:id', the first 6 hex digits) a remote's 20-bit ID was found again from its label by
    trying every ID, in under a second: the label was the remote."""
    if _LABEL_SECRET["key"] is None:
        path = DATA_DIR / "label_secret"
        key = b""
        try:
            key = path.read_bytes().strip()
        except OSError:
            pass
        if len(key) < 32:
            key = binascii.hexlify(secrets.token_bytes(32))
            try:                                   # made whole and linked into place: another ARIS process never
                DATA_DIR.mkdir(parents=True, exist_ok=True)      # reads a half-written key
                fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), prefix=".label_secret.")
                try:
                    os.fchmod(fd, 0o600)
                    os.write(fd, key + b"\n")
                    os.close(fd)
                    try:
                        os.link(tmp, str(path))
                    except FileExistsError:
                        key = path.read_bytes().strip() or key
                finally:
                    os.unlink(tmp)
            except OSError as e:
                log.warning("label secret not saved (%s): remote labels will change at the next start", e)
        _LABEL_SECRET["key"] = bytes(key)
    return _LABEL_SECRET["key"]


def _safe_packet(p):
    """Decoded packet without remote-control codes (they could be used to copy a gate or garage remote). A remote's
    ID is part of its code too, so for code-carrying devices and remote-type models the ID becomes a short label
    (#3fa2c1, keyed with _label_secret): still enough to tell two remotes apart, not enough to copy one."""
    q = {k: v for k, v in p.items() if not _is_code_key(k)}
    if "id" in q and not str(q["id"]).startswith("#") and (any(_is_code_key(k) for k in p)
                                                         or _REMOTE_MODEL_RX.search(str(p.get("model") or ""))):
        q["id"] = "#" + hmac.new(_label_secret(), f"{p.get('model')}:{p.get('id')}".encode(),
                                 "sha256").hexdigest()[:6]
    return q


# What a decoded device is, from the fields it reports (more reliable than the model name alone).
_MEASURES = [
    (("temperature_C", "temperature_F", "temperature_1_C"), "temperature"), (("humidity",), "humidity"),
    (("pressure_hPa", "pressure_inHg"), "air pressure"), (("pressure_kPa", "pressure_PSI", "pressure_bar"), "tyre pressure"),
    (("wind_avg_km_h", "wind_avg_m_s", "wind_max_km_h", "wind_max_m_s", "wind_dir_deg"), "wind"),
    (("rain_mm", "rain_in", "rain_rate_mm_h"), "rain"), (("light_lux", "lux"), "light"), (("uv", "uvi"), "UV"),
    (("moisture",), "soil moisture"), (("power_W", "energy_kWh", "current_A", "voltage_V"), "electricity use"),
    (("consumption_data", "consumption"), "meter reading"), (("co2_ppm",), "CO2"),
    (("pm2_5_ug_m3", "pm10_ug_m3"), "air particles"), (("contact_open", "reed_open"), "door/window open or closed"),
    (("motion",), "motion"), (("alarm", "smoke"), "alarm"), (("button", "cmd", "code", "tristate"), "button presses"),
]


def _device_type(model, keys, p):
    m = str(model or "").lower()
    has = lambda *ks: any(k in keys for k in ks)
    if p.get("type") == "TPMS" or "tpms" in m or (has("pressure_kPa", "pressure_PSI", "pressure_bar")
                                                  and not has("humidity", "wind_avg_km_h")):
        return "tyre-pressure sensor (TPMS) on a vehicle"
    if has("consumption_data", "consumption", "meter_reading") or m.startswith(
            ("ert-", "scm", "idm", "neptune", "badger", "itron", "elster", "metermaster")):
        return "utility meter (electricity, gas or water)"
    if has("power_W", "energy_kWh", "current_A", "voltage_V") or "energy" in m or "power" in m:
        return "electricity / energy monitor"
    if "smoke" in m or "fire" in m or has("smoke"):
        return "smoke / fire alarm"
    if has("water", "leak") or "leak" in m:
        return "water-leak sensor"
    if has("contact_open", "reed_open", "door_open") or any(w in m for w in ("door", "window", "contact")):
        return "door / window contact sensor"
    if has("motion") or "pir" in m or "motion" in m:
        return "motion (PIR) sensor"
    if any(w in m for w in ("doorbell", "bell", "chime", "activlink", "byron")):
        return "wireless doorbell"
    if any(w in m for w in ("security", "interlogix", "dsc", "visonic", "alarm", "kerui", "chuango")):
        return "alarm-system sensor"
    if has("wind_avg_km_h", "wind_avg_m_s", "wind_max_km_h", "rain_mm", "rain_in", "uv", "light_lux"):
        return "weather station"
    if has("temperature_C", "temperature_F", "humidity", "moisture"):
        return "temperature / humidity sensor"
    if any(_is_code_key(k) for k in keys) or any(w in m for w in ("remote", "switch", "keyfob", "button")):
        return "remote control / wireless button (gate, garage, lights or alarm fob)"
    return "device decoded by rtl_433"


# Decoder model strings that get a plain name (the chat, the Decode card and the Devices tab all use it).
_DEVICE_NAMES = {"Generic-Remote": "Generic fixed-code remote (PT2262 / EV1527-type chip)"}


def _device_info(p) -> dict:
    """Plain description of a decoded packet's device: name, kind, what it reports. No codes."""
    model = str(p.get("model") or "?")
    keys = set(p)
    q = _safe_packet(p)
    name = _DEVICE_NAMES.get(model) or model.replace("_", " ").replace("-", " ")
    return {"model": model, "device": name, "type": _device_type(model, keys, p),
            "id_label": None if q.get("id") is None else str(q.get("id")), "channel": q.get("channel"),
            "reports": [w for ks, w in _MEASURES if any(k in keys for k in ks)], "protocol": p.get("protocol")}


def _devices_from_packets(packets, heard_by=None) -> list:
    """Every distinct device in a set of decoded packets (model + ID + channel), with packet counts."""
    out = {}
    for p in packets:
        info = _device_info(p)
        key = (info["model"], info["id_label"], str(info["channel"]))
        d = out.get(key)
        if d is None:
            d = out[key] = {**info, "packets": 0, "best_snr_db": None, "freq_mhz": None, "heard_by": []}
        d["packets"] += 1
        if isinstance(p.get("snr"), (int, float)):
            d["best_snr_db"] = round(max(d["best_snr_db"] if d["best_snr_db"] is not None else -99.0,
                                         float(p["snr"])), 1)
        if isinstance(p.get("freq"), (int, float)):
            d["freq_mhz"] = round(float(p["freq"]), 4)
        if heard_by and heard_by not in d["heard_by"]:
            d["heard_by"].append(heard_by)
    return sorted(out.values(), key=lambda d: (-d["packets"], -(d["best_snr_db"] or 0)))


def _device_line(d) -> str:
    bits = [f"{d['packets']} packet{'s' if d['packets'] != 1 else ''}"]
    if d.get("id_label"):
        bits.append(f"ID {d['id_label']}")
    if d.get("channel") not in (None, ""):
        bits.append(f"channel {d['channel']}")
    if d.get("reports"):
        bits.append("reports " + ", ".join(d["reports"]))
    return f"{d['device']}, a {d['type']} ({'; '.join(bits)})"


def _stage2_rtl433(freq_mhz, serial, seconds, gain_db, rate="250k"):
    """rtl_433 on one dongle (the caller holds its lock): every built-in decoder, plus the pulse analyzer.
    rate: 250k is rtl_433's default; some fast FSK devices (meters, some 868/915 MHz sensors) only decode at 1024k."""
    if not shutil.which("rtl_433") or not _have_tool("rtl_433", serial):
        return {"status": "rtl_433 not installed", "packets_decoded_count": 0}
    tune_hz = int(round((freq_mhz - 0.035) * 1e6))  # keep the signal off the 0 Hz DC spike
    cmd = ["rtl_433"] + _rtl433_device(serial) + ["-f", str(tune_hz), "-s", rate, "-g", str(gain_db)]
    cmd += _ppm_args(serial) + _rtl433_extra_flags()
    cmd += ["-A", "-T", str(int(seconds)), "-F", "json", "-F", "log", "-M", "level", "-M", "protocol"]
    try:          # stderr into stdout: the order shows which packages a decoder recognised
        proc = subprocess.run(_radio_cmd(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              timeout=int(seconds) + 15)
        text, rc = proc.stdout or "", proc.returncode
    except subprocess.TimeoutExpired as e:
        # it did not end after -T (subprocess.run killed it and waited for it): what it printed until then counts
        text = e.output.decode(errors="replace") if isinstance(e.output, bytes) else (e.output or "")
        rc = None
        if not _parse_rtl433(text)[0]:
            return {"status": "rtl_433 timed out", "packets_decoded_count": 0}
    except Exception as e:
        return {"status": f"rtl_433 error: {e}", "packets_decoded_count": 0}
    packets, fps, bursts = _parse_rtl433(text)
    now = time.time()
    hist = []                         # for the device history (Devices tab): _merge_rtl433 feeds each packet once
    for p in packets:
        t = _packet_epoch(p, now)
        hist.append({**_safe_packet(p), "t": round(t, 2), "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)),
                     "aris_serial": serial, "aris_freq_mhz": round(float(freq_mhz), 4)})
    snrs = [float(p["snr"]) for p in packets if isinstance(p.get("snr"), (int, float))]
    out = {"status": f"listened {int(seconds)} s at {rate}S/s", "packets_decoded_count": len(packets),
           "identified_models": sorted({str(p["model"]) for p in packets}),
           "devices": _devices_from_packets(packets),
           "decoded_packets": [_safe_packet(p) for p in packets[:4]], "history": hist,
           "best_packet_snr_db": round(max(snrs), 1) if snrs else None, "bursts_heard": bursts, "sample_rate": rate}
    fps = [f for f in fps if not packets or _clear_pattern(f)]      # next to decodes, only clear unknown patterns
    if fps:
        out["unknown_signal_fingerprints"] = fps
    if rc not in (0, None) and not packets and not bursts:     # it never got going
        out["error"] = _rtl433_problem(_rtl433_log_lines(text), rc)
        out["status"] = f"could not run: {out['error']}"
    return out


# ============================================================================
# STAGE 3: rtl_fm -> multimon-ng (validated) / atest (APRS) / CTCSS and tone detection
# ============================================================================
_MM_WANTED = ["POCSAG512", "POCSAG1200", "POCSAG2400", "FLEX", "FLEX_NEXT", "AFSK1200", "AFSK2400", "DTMF", "EAS"]
_MM_AVAIL = None


def _multimon_demods():
    """Only decoders with real error checking. UFSK1200, CLIPFSK, MORSE_CW, ZVEI, CCIR, EIA, EEA and
    FMSFSK print 'frames' from pure static, so they are never enabled."""
    global _MM_AVAIL
    if _MM_AVAIL is None:
        try:
            p = subprocess.run(["multimon-ng", "-h"], capture_output=True, text=True, timeout=8)
            txt = (p.stdout or "") + (p.stderr or "")
        except Exception:
            txt = ""
        parts = txt.split("Available demodulators:", 1)
        if len(parts) == 2:
            avail = set(re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", parts[1]))
            _MM_AVAIL = [d for d in _MM_WANTED if d in avail]
        else:
            _MM_AVAIL = ["POCSAG512", "POCSAG1200", "POCSAG2400", "AFSK1200", "DTMF"]
    return _MM_AVAIL


def _mm_validate(lines):
    """Keep only multimon-ng lines with a valid structure. Returns (valid, rejected_count)."""
    valid, dtmf, junk = [], [], 0
    for s in lines:
        if s.count("[0x") >= 2:
            junk += 1
            continue
        if re.match(r"^POCSAG\d+:\s+Address:\s*\d+\s+Function:\s*\d", s):
            body = s.split("Alpha:", 1)[1] if "Alpha:" in s else ""
            if body.strip():
                printable = sum(1 for ch in body if 32 <= ord(ch) < 127)
                if printable < 0.85 * len(body) or body.count("<") > 3:
                    junk += 1
                    continue
            valid.append(s)
        elif re.match(r"^FLEX(_NEXT)?[:|]", s):
            valid.append(s)
        elif re.match(r"^AFSK\d+:\s+fm\s+\S+\s+to\s+\S+", s):
            valid.append(s)
        elif s.startswith("EAS") and "ZCZC" in s:
            valid.append(s)
        elif re.match(r"^DTMF:\s*[0-9A-D*#]", s):
            dtmf.append(s.split(":", 1)[1].strip()[:1])
        else:
            junk += 1
    if len(dtmf) >= 3:
        valid.append("DTMF: " + "".join(dtmf))
    elif dtmf:
        junk += len(dtmf)
    return valid, junk


def _tone_peak(freqs, spec, lo, hi, edge_hz, win_hz, excl_hz):
    """Strongest spectral line in [lo, hi] and how far it stands above its own neighbourhood.
    Returns (freq, prominence) or None if the max sits on the window edge (that is noise slope)."""
    m = (freqs >= lo) & (freqs <= hi)
    if not m.any():
        return None
    k = int(np.argmax(spec[m]))
    f0, a0 = float(freqs[m][k]), float(spec[m][k])
    if f0 - lo < edge_hz or hi - f0 < edge_hz:
        return None
    neigh = (np.abs(freqs - f0) <= win_hz) & (np.abs(freqs - f0) > excl_hz)
    if not neigh.any():
        return None
    return f0, a0 / (float(np.median(spec[neigh])) + 1e-12)


def _mm_metadata(line):
    """METADATA ONLY: say what kind of transmission was decoded, never its message content.
    Pager text, pager addresses, DTMF digits and packet payloads are dropped here, before anything is
    shown, saved or passed to the AI."""
    proto = line.split(":", 1)[0].split("|", 1)[0].strip()
    if proto.startswith("POCSAG"):
        kind = "alpha" if "Alpha:" in line else ("numeric" if "Numeric:" in line else "tone-only")
        return f"{proto}: page detected ({kind}; content not recorded)"
    if proto.startswith("FLEX"):
        return f"{proto}: page detected (content not recorded)"
    if proto.startswith("AFSK"):
        m = re.match(r"^AFSK\d+:\s+fm\s+(\S+)\s+to\s+(\S+)", line)
        return (f"{proto}: amateur packet from {m.group(1)} to {m.group(2)} (payload not recorded)" if m
                else f"{proto}: packet detected (payload not recorded)")
    if proto.startswith("DTMF"):
        return "DTMF: tone digits detected (digits not recorded)"
    if proto.startswith("EAS"):
        return "EAS: emergency alert header detected (content not recorded)"
    return f"{proto}: transmission detected (content not recorded)"


def _run_for(cmd, seconds):
    """Run a streaming command for `seconds` (less if the job or capture is stopped), then stop it with Ctrl+C so it
    flushes and exits cleanly (SIGKILL when its stop hangs). Returns (rc, stderr_tail)."""
    out, err, rc = _run_capture(cmd, seconds, keep_stdout=False)
    return rc, err


def _run_capture(cmd, seconds, keep_stdout=True):
    """Like _run_for, but also returns what the command printed: (stdout, stderr_tail, rc)."""
    with tempfile.TemporaryFile("w+") as of, tempfile.TemporaryFile("w+") as ef:
        p = subprocess.Popen(cmd, stdout=of if keep_stdout else subprocess.DEVNULL, stderr=ef)
        try:
            end = time.time() + max(0.0, float(seconds))
            while not _stop_requested():
                left = end - time.time()
                if left <= 0:
                    break
                try:
                    p.wait(timeout=min(0.25, left))
                    break
                except subprocess.TimeoutExpired:
                    pass
        finally:
            _end_radio(p)                            # gone before the caller lets go of the dongle
        of.seek(0)
        ef.seek(0)
        return of.read(), ef.read()[-300:], p.returncode


def _atest_frames(text):
    """APRS packets in atest output. Newer direwolf colours its output, so escape codes are removed first."""
    out = []
    for ln in (text or "").splitlines():
        m = re.match(r"^\[\d+(?:\.\d+)?\]\s*([A-Z0-9]{1,6}(?:-\d{1,2})?)\*?>[A-Z0-9]", _ANSI.sub("", ln).strip())
        if m:
            out.append(f"APRS packet from {m.group(1)} (payload not recorded)")
    return out


def _mm_lines(text):
    return [ln.strip() for ln in (text or "").splitlines()
            if ln.strip() and not ln.startswith(("multimon-ng", "Enabled demod", "Available demod"))]


_AUDIO_TMP = "/dev/shm" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else None


_OWNED_TMP_RX = re.compile(r"^aris_(aud|band)_(\d+)_")
_TMP_STALE_S = 600.0


def _tmp_owned_prefix(kind: str) -> str:
    """'aris_aud_<pid>_': the name of a temporary audio file or burst-cut folder says which process owns it, so
    the start-up sweep removes only what a dead process left and never what another ARIS process (the GUI's
    Decode job while the chat console restarts) is using right now (v4.9.5 review)."""
    return f"aris_{kind}_{os.getpid()}_"


def _open_private(path: str):
    """A new file only this user can read (0600, never an existing one): the decoders' audio in /dev/shm would
    otherwise take the umask (review: the .wav was world-readable for the seconds it existed)."""
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


def _sweep_audio_leftovers() -> int:
    """Delete the demodulated-audio files and burst-cut folders of ARIS processes that are gone (killed before
    their own clean-up): metadata input and a remote's replayable IQ, never anything to keep (v4.9.4; the
    owner's pid in the name and the band folders since v4.9.5). Names without a pid (older versions) go when
    older than _TMP_STALE_S. Called at start (after every part is loaded: _pid_alive lives in captures)."""
    n = 0
    for d in {_AUDIO_TMP or tempfile.gettempdir(), tempfile.gettempdir()}:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if not name.startswith(("aris_aud_", "aris_band_")):
                continue
            p = os.path.join(d, name)
            m = _OWNED_TMP_RX.match(name)
            try:
                if m:
                    if _pid_alive(int(m.group(2))):
                        continue
                elif time.time() - os.stat(p).st_mtime < _TMP_STALE_S:
                    continue
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    os.unlink(p)
                n += 1
            except OSError:
                pass
    return n


def _stage3_demod_audio(freq_mhz, serial, seconds, gain_db, mode="fm"):
    """rtl_fm audio -> multimon-ng (pagers, AFSK, EAS, DTMF), atest (APRS) and CTCSS / tone detection.
    The caller holds the dongle lock. Audio is analysed and deleted; nothing is recorded or transcribed."""
    if not _have_tool("rtl_fm", serial):
        return {"status": _tool_missing("rtl_fm", serial)}
    sr = 22050
    # the decoders need the audio in a file for seconds: in memory (/dev/shm, never a disk) and gone as soon as
    # they have read it; any left behind by a killed process goes at the next start (_sweep_audio_leftovers)
    fd, raw_path = tempfile.mkstemp(prefix=_tmp_owned_prefix("aud"), suffix=".raw", dir=_AUDIO_TMP)
    os.close(fd)
    wav_path = raw_path[:-4] + ".wav"
    cmd = ["rtl_fm", "-d", str(serial), "-f", str(int(round(freq_mhz * 1e6))), "-M", mode, "-s", str(sr),
           "-g", str(gain_db), "-l", "0"] + _ppm_args(serial) + [raw_path]
    valid, junk, aprs, used = [], 0, [], []
    try:
        _run_for(_radio_cmd(cmd), int(seconds))
        if not os.path.exists(raw_path) or os.path.getsize(raw_path) < 1024:
            return {"status": "No demodulated audio output from rtl_fm"}
        with open(raw_path, "rb") as rf:
            pcm_bytes = rf.read()
        with wave.open(_open_private(wav_path), "wb") as wf:      # (0600, never an existing file)
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(pcm_bytes)
        demods = _multimon_demods() if shutil.which("multimon-ng") else []
        if demods:
            used.append("multimon-ng")
            mm_cmd = ["multimon-ng", "-t", "raw"] + [x for d in demods for x in ("-a", d)] + [raw_path]
            try:
                out = subprocess.run(mm_cmd, capture_output=True, text=True, timeout=20).stdout
            except subprocess.TimeoutExpired:
                out = ""
            valid, junk = _mm_validate(_mm_lines(out))
            valid = [_mm_metadata(v) for v in valid]
            del out                          # raw decoder text (may hold message content) is discarded
        if shutil.which("atest"):            # direwolf's file decoder (direwolf itself cannot read a WAV file)
            used.append("atest")
            try:
                p = subprocess.run(["atest", "-B", "1200", wav_path], capture_output=True, text=True, timeout=25)
                aprs = _atest_frames((p.stdout or "") + "\n" + (p.stderr or ""))
            except subprocess.TimeoutExpired:
                pass
        pcm = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        ctcss = tone = rms = None
        if len(pcm) > sr:
            pcm = pcm - float(np.mean(pcm))
            rms = round(float(np.sqrt(np.mean(pcm ** 2))), 4)
            freqs = np.fft.rfftfreq(len(pcm), d=1.0 / sr)
            spec = np.abs(np.fft.rfft(pcm * np.hanning(len(pcm))))
            pk = _tone_peak(freqs, spec, 60.0, 260.0, edge_hz=4.0, win_hz=40.0, excl_hz=1.5)
            if pk and pk[1] >= 8.0:
                snapped = min(CTCSS_TONES, key=lambda t: abs(t - pk[0]))
                if abs(snapped - pk[0]) <= 1.8:
                    ctcss = f"{snapped} Hz (measured {pk[0]:.1f} Hz)"
            pk = _tone_peak(freqs, spec, 300.0, 3800.0, edge_hz=60.0, win_hz=150.0, excl_hz=8.0)
            if pk and pk[1] >= 20.0:
                tone = f"{pk[0]:.1f} Hz"
        return {"status": f"listened {int(seconds)} s", "decoders_used": used, "validated_frames": valid[:10],
                "validated_count": len(valid), "rejected_noise_lines": junk, "aprs_frames": aprs[:5],
                "ctcss_tone": ctcss or "none", "audio_tone": tone or "none", "audio_rms_level": rms}
    except Exception as e:
        log.error("stage3 failed:\n%s", traceback.format_exc())
        return {"status": f"Demodulator stage error: {e}"}
    finally:
        for p in (raw_path, wav_path):
            try:
                os.remove(p)
            except OSError:
                pass


def _stage_adsb(serial, seconds, gain_db):
    """rtl_adsb (part of the rtl-sdr tools): counts aircraft transponder messages and distinct aircraft.
    The caller holds the dongle lock."""
    if not radios.is_rtl(serial):
        return {"status": f"ADS-B decoding needs an RTL-SDR dongle (rtl_adsb); this is a {radios.label_for(serial)}",
                "messages": 0}
    if not shutil.which("rtl_adsb"):
        return {"status": "rtl_adsb not installed (it comes with the rtl-sdr tools)", "messages": 0}
    out, err, _ = _run_capture(["rtl_adsb", "-d", str(serial), "-g", str(gain_db)] + _ppm_args(serial), seconds)
    from arislib import adsb
    r = adsb.parse(out)              # rtl_adsb checks no checksums: an aircraft counts only from a verified one
    return {"status": f"listened {int(seconds)} s", "messages": r["messages"], "valid_messages": r["valid"],
            "aircraft_heard": len(r["aircraft"]), "aircraft": r["aircraft"][:20]}


# ============================================================================
# DECODERS: what is installed, and a self-test with synthetic recordings
# ============================================================================
_DECODER_APPS = (
    ("rtl_433", "sensors, remotes and meters (315/433/868/915 MHz)"),
    ("multimon-ng", "pagers (POCSAG/FLEX), AFSK packets and alert tones (metadata only)"),
    ("atest", "APRS packet radio (part of direwolf)"),
    ("rtl_adsb", "aircraft ADS-B at 1090 MHz (part of the rtl-sdr tools)"),
    ("rtl_fm", "audio demodulator that feeds multimon-ng and atest"),
)
_OTHER_APPS = ("direwolf", "dump1090", "dump1090-fa", "readsb", "urh", "gqrx", "sdrpp")


def _decoder_inventory():
    return {"used_by_aris": {n: bool(shutil.which(n)) for n, _ in _DECODER_APPS},
            "what_each_decodes": {n: what for n, what in _DECODER_APPS},
            "installed_but_not_used": [n for n in _OTHER_APPS if shutil.which(n)]}


def _st_remote_seq():
    """A fixed-code remote (EV1527-type, which rtl_433's own Generic-Remote decoder knows): (on, us) steps."""
    T, code, seq = 465, 0xA5C3B1, [(0, 20000)]
    for _ in range(6):
        seq += [(1, T), (0, 31 * T)]
        for i in range(23, -1, -1):
            seq += [(1, 3 * T), (0, T)] if (code >> i) & 1 else [(1, T), (0, 3 * T)]
    return seq + [(1, T), (0, 31 * T), (0, 20000)]


def _st_unknown_seq():
    """A 28-bit PWM sensor that no decoder knows: (on, us) steps."""
    seq = [(0, 20000)]
    for _ in range(4):
        for b in "1011001110001111010100101100":
            seq += [(1, 400), (0, 1200)] if b == "1" else [(1, 1200), (0, 400)]
        seq.append((0, 10000))
    return seq + [(0, 20000)]


def _st_env(seq, fs, amp=0.5, noise=0.01, seed=7):
    """The (on, us) steps as complex64 IQ at fs, on 0 Hz, scaled like a band capture's cut (amp of full scale)."""
    rng = np.random.default_rng(seed)
    env = np.concatenate([np.full(int(us * fs / 1e6), float(on)) for on, us in seq])
    z = amp * env + noise * (rng.standard_normal(len(env)) + 1j * rng.standard_normal(len(env)))
    return z.astype(np.complex64)


def _st_mixed_cu8(path):
    """A fixed-code remote (EV1527-type, which rtl_433's own Generic-Remote decoder knows) followed by a 28-bit
    PWM sensor that no decoder knows, 35 kHz off centre, as a .cu8 recording at 250 kS/s."""
    fs, rng = 250000, np.random.default_rng(7)
    env = np.concatenate([np.full(int(us * fs / 1e6), float(on)) for on, us in _st_remote_seq() + _st_unknown_seq()])
    z = 60.0 * env * np.exp(2j * np.pi * 35e3 * np.arange(len(env)) / fs)
    z = z + 2.0 * (rng.standard_normal(len(env)) + 1j * rng.standard_normal(len(env)))
    iq = np.empty(2 * len(z), dtype=np.float32)
    iq[0::2], iq[1::2] = z.real, z.imag
    np.clip(np.round(iq + 127.5), 0, 255).astype(np.uint8).tofile(path)


def _bch_pocsag(d21):
    reg = d21 << 10
    for i in range(30, 9, -1):
        if reg & (1 << i):
            reg ^= 0b11101101001 << (i - 10)
    cw = (d21 << 10) | (reg & 0x3FF)
    return (cw << 1) | (bin(cw).count("1") & 1)


def _st_pocsag_raw(path, address=1234567, function=3, text="ARIS SELFTEST"):
    """A POCSAG-1200 page as FM-demodulated audio (22050 Hz, 16-bit raw), for multimon-ng."""
    sync, idle = 0x7CD215D8, 0x7A89C197
    slots = [idle] * (2 * (address & 7)) + [_bch_pocsag(((address >> 3) << 2) | function)]
    bits = "".join(format(ord(c) & 0x7F, "07b")[::-1] for c in text)
    bits += "0" * ((-len(bits)) % 20)
    slots += [_bch_pocsag((1 << 20) | int(bits[i:i + 20], 2)) for i in range(0, len(bits), 20)]
    slots += [idle] * ((-len(slots)) % 16 + 16)
    stream = "10" * 288 + "".join(format(sync, "032b") + "".join(format(w, "032b") for w in slots[b:b + 16])
                                  for b in range(0, len(slots), 16))
    spb = 22050 / 1200.0
    idx = np.minimum((np.arange(int(len(stream) * spb)) / spb).astype(int), len(stream) - 1)
    lv = -np.array([1.0 if c == "1" else -1.0 for c in stream])[idx]
    x = np.concatenate([np.zeros(4410), np.convolve(lv, np.ones(5) / 5.0, mode="same") * 0.5, np.zeros(4410)])
    (x * 32000).astype("<i2").tofile(path)


def _st_aprs(raw_path, wav_path):
    """An APRS (AX.25 UI) packet as 1200 baud AFSK audio, for atest and multimon-ng."""
    def addr(call, last):
        return bytes(ord(ch) << 1 for ch in (call + "      ")[:6]) + bytes([0x60 | (1 if last else 0)])
    body = addr("APRS", False) + addr("ARIS", True) + bytes([0x03, 0xF0]) + b">ARIS decoder self-test"
    crc = 0xFFFF
    for b in body:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    crc ^= 0xFFFF
    frame, bits, ones = body + bytes([crc & 0xFF, crc >> 8]), [], 0
    for byte in frame:
        for i in range(8):
            bit = (byte >> i) & 1
            bits.append(bit)
            ones = ones + 1 if bit else 0
            if ones == 5:
                bits.append(0)
                ones = 0
    flag = [0, 1, 1, 1, 1, 1, 1, 0]
    bits = flag * 40 + bits + flag * 4
    fs, tone, phase, acc, out = 22050, True, 0.0, 0.0, []
    for b in bits:
        if b == 0:
            tone = not tone
        acc += fs / 1200.0
        n = int(round(acc))
        acc -= n
        step = 2 * np.pi * (1200.0 if tone else 2200.0) / fs
        out.append(phase + step * np.arange(1, n + 1))
        phase = out[-1][-1]
    x = np.concatenate([np.zeros(4410), 0.5 * np.sin(np.concatenate(out)), np.zeros(4410)])
    pcm = (x * 32000).astype("<i2")
    pcm.tofile(raw_path)
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(fs)
        w.writeframes(pcm.tobytes())


def _decoder_selftest():
    """Feed each installed decoder a synthetic recording of a signal it should decode, through the same output
    parsing ARIS uses for real signals. Proves the programs run and that ARIS reads their output correctly."""
    res = {}
    with tempfile.TemporaryDirectory(prefix="aris_selftest_") as td:
        poc = os.path.join(td, "aris_selftest_pocsag.raw")
        apr, aprw = os.path.join(td, "aris_selftest_aprs.raw"), os.path.join(td, "aris_selftest_aprs.wav")
        _st_pocsag_raw(poc)
        _st_aprs(apr, aprw)

        def run(cmd):
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return (p.stdout or "") + "\n" + (p.stderr or "")

        if shutil.which("rtl_433"):
            try:
                mix = os.path.join(td, "aris_selftest_mix_433.92M_250k.cu8")
                _st_mixed_cu8(mix)
                p = subprocess.run(["rtl_433", "-r", mix, "-A", "-F", "json", "-M", "level"] + _rtl433_extra_flags(),
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30)
                pk, fps, _ = _parse_rtl433(p.stdout or "")
                known = any(x.get("model") == "Generic-Remote" for x in pk)
                unk = next((f for f in fps if f.get("demodulated")), None)
                apart = bool(unk and unk.get("bits_per_packet") == 28 and unk.get("short_pulse_us")
                             and abs(unk["short_pulse_us"] - 400) <= 40)
                clean = all(not any(_is_code_key(k) for k in _safe_packet(x)) for x in pk)
                ok = known and apart and clean
                res["rtl_433"] = {"ok": ok, "detail": (
                    "decoded the test remote with its own decoder and told the unknown test sensor apart "
                    "(28-bit PWM)" if ok else "; ".join(t for t, bad in (
                        ("did NOT decode the test remote", not known),
                        ("did not tell the unknown test sensor apart", not apart),
                        ("remote codes were not stripped", not clean)) if bad))}
            except Exception as e:
                res["rtl_433"] = {"ok": False, "detail": f"failed to run: {type(e).__name__}: {e}"}
        else:
            res["rtl_433"] = {"ok": None, "detail": "not installed (sudo apt install rtl-433)"}

        if shutil.which("multimon-ng"):
            try:
                v1, _ = _mm_validate(_mm_lines(run(["multimon-ng", "-t", "raw", "-a", "POCSAG1200", poc])))
                v2, _ = _mm_validate(_mm_lines(run(["multimon-ng", "-t", "raw", "-a", "AFSK1200", apr])))
                pocsag = any("Address: 1234567" in v and "Function: 3" in v for v in v1)
                afsk = any(v.startswith("AFSK1200") and "ARIS" in v for v in v2)
                parts = [("test pager message", pocsag), ("test AFSK packet", afsk)]
                res["multimon-ng"] = {"ok": pocsag and afsk, "detail": "; ".join(
                    ("decoded the " if ok else "did NOT decode the ") + what for what, ok in parts)}
            except Exception as e:
                res["multimon-ng"] = {"ok": False, "detail": f"failed to run: {type(e).__name__}: {e}"}
        else:
            res["multimon-ng"] = {"ok": None, "detail": "not installed (sudo apt install multimon-ng)"}

        if shutil.which("atest"):
            try:
                ok = any("ARIS" in f for f in _atest_frames(run(["atest", "-B", "1200", aprw])))
                res["atest"] = {"ok": ok, "detail": "decoded the test APRS packet" if ok
                                else "ran, but did not decode the test APRS packet"}
            except Exception as e:
                res["atest"] = {"ok": False, "detail": f"failed to run: {type(e).__name__}: {e}"}
        else:
            res["atest"] = {"ok": None, "detail": "not installed (sudo apt install direwolf)"}
    try:
        from arislib import rds as _rds
        res["RDS"] = _rds.selftest()
        if res["RDS"]["ok"]:
            res["RDS"]["detail"] = "decoded the test FM station's name (RDS)"
    except Exception as e:
        res["RDS"] = {"ok": False, "detail": f"failed: {type(e).__name__}: {e}"}
    for name in ("rtl_fm", "rtl_adsb", "rtl_sdr", "rtl_power", "rtl_test"):
        res[name] = {"ok": True if shutil.which(name) else None,
                     "detail": "installed (tested with the dongles during setup)" if shutil.which(name)
                     else "not installed (sudo apt install rtl-sdr)"}
    return res


def _selftest_line(res):
    bad = [f"{n} {r['detail']}" for n, r in res.items() if r.get("ok") is False]
    good = [n for n, r in res.items() if r.get("ok") and n in ("rtl_433", "multimon-ng", "atest", "RDS")]
    missing = [n for n, r in res.items() if r.get("ok") is None]
    txt = ("Decoders working: " + ", ".join(good) + "." if good else "No decoder passed its test.")
    if bad:
        txt += " PROBLEM: " + "; ".join(bad) + "."
    if missing:
        txt += " Not installed: " + ", ".join(missing) + "."
    return txt


@register("run_decoder_selftest",
          "Check every decoder program (rtl_433, multimon-ng, atest) by decoding synthetic test signals. No "
          "dongle needed, takes a few seconds. Same as /selftest.", {}, aliases=("decoder_selftest",))
def run_decoder_selftest():
    res = _decoder_selftest()
    return {"summary": _selftest_line(res), "results": res, "inventory": _decoder_inventory()}
