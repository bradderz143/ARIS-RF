"""ARIS engine, part "fm": Name all FM stations: the band read in slices, RDS names, the per-station states.

Executed into aris.py's namespace by aris._include (see aris.py): names from aris.py's imports and from the
parts before this one are available here without imports, and everything defined here becomes aris.<name>.
"""
from __future__ import annotations

FM_BLOCK_FS = 2048000           # Name all: one capture covers a 2 MHz slice of the band, several stations at once
FM_BLOCK_CHUNK = 1 << 22        # samples per FFT chunk (2.05 s): memory stays small for long looks
FM_BLOCK_HALF_MHZ = 0.6         # a station's +-100 kHz stays within +-0.7 MHz, where the dongle's filter is flat
                                # (v4.7.4 used 0.8: stations near the edge lost part of their signal on the real PC)
FM_BLOCK_SPAN_MHZ = 1.0         # stations per capture: within 1.0 MHz (leaves room to tune away from every one)
FM_NAME_LOOK_S = float(_env("FM_LOOK_S", "6"))      # first look at each slice: every station in it gets 6 s of RDS
FM_NAME_RETRY_S = float(_env("FM_RETRY_S", "12"))   # RDS there but no whole name: one longer look (a name needs 4
                                                    # parts in a row)
_NAME_RANK = {"named": 5, "rds_weak": 4, "no_name": 3, "off_channel": 3, "not_fm": 2, "hd_sideband": 2, "too_weak": 1,
              "not_heard": 1, "not_checked": 0}
_NAME_BAND = {"no_name": "Station channel: no RDS name read in the last check",
              "hd_sideband": "Digital (HD Radio) sideband of a station next to it, not a station of its own",
              "rds_weak": "Station channel, checked: RDS too weak to read its name",
              "not_fm": "Checked: not standard FM broadcast",
              "not_heard": "Not heard in the check (weak or on and off)",
              "too_weak": "Checked: too weak here to read (under 6 dB across its channel)",
              "narrow": "Checked: a narrow signal, not FM broadcast",
              "not_checked": "Not checked: the dongles could not capture it this time"}


def _fm_check_result(x, ser) -> dict:
    """One IQ look at an FM channel, summed up for the Name all list."""
    car, off = x.get("measured_carrier_mhz"), x.get("offset_from_channel_khz")
    r = {"snr_db": x.get("channel_snr_db"), "dongle": _short(ser), "serial": ser, "carrier_mhz": car,
         "offset_khz": off, "stereo": bool(x.get("stereo_pilot_19khz")), "pi": x.get("rds_pi_code")}
    if x.get("overloaded_pct"):
        r["overloaded_pct"] = x["overloaded_pct"]
    clearly_off = car is not None and (int(round(car * 10.0)) % 2 == 0 or abs(off or 0.0) >= FM_CHECK_OFF_KHZ)
    piece = bool(x.get("rds_station_name") and not x.get("rds_name_scrolls") and _fragment_name(x["rds_station_name"]))
    fixed = _known_fixed_name(car if car is not None else x.get("frequency_mhz")) if piece else None
    lone_piece = piece and not fixed
    if x.get("rds_station_name") and not clearly_off and not lone_piece:
        if piece:            # a piece of text ('Al Aire') read on a channel whose fixed name is known: that name
            r.update(state="named", name=fixed, names=[fixed], scrolls=False, text=x["rds_station_name"])
        else:
            r.update(state="named", name=x["rds_station_name"], names=x.get("rds_station_names"),
                     scrolls=bool(x.get("rds_name_scrolls")))
    elif lone_piece and not clearly_off:
        # a piece of text ('Al Aire', 'En Amor') with no station name read yet: RDS, but not a name (v4.9.3)
        r.update(state="rds_weak", text=x["rds_station_name"],
                 band=f"Station channel, checked: RDS read only a piece of text (\"{x['rds_station_name']}\"), no "
                      f"station name yet")
    elif not x.get("signal_present_during_capture"):
        s, pk = x.get("channel_snr_db"), x.get("peak_snr_db")
        if pk is not None and pk >= 10.0 and pk - (s or 0.0) >= 8.0:
            r.update(state="not_fm", band=_NAME_BAND["narrow"])         # a narrow carrier, not a station
        elif (s is not None and s >= 2.0) or (pk is not None and pk >= 6.0):
            r["state"] = "too_weak"                  # there (the sweep saw it), but too weak for a reading
        else:
            r["state"] = "not_heard"
    elif not x.get("wideband_fm"):
        r["state"] = "not_fm"
        if (x.get("occupied_bw_khz") or 0.0) < 40.0:
            r["band"] = _NAME_BAND["narrow"]          # a carrier tens of kHz wide at most: no FM station
    elif clearly_off:
        r["state"] = "off_channel"
        r["band"] = f"Not a station channel (checked: carrier at {car:.3f} MHz)"
    else:
        r["state"] = "rds_weak" if x.get("rds_57khz") else "no_name"
    r.setdefault("band", _NAME_BAND.get(r["state"]))
    return r


def _plan_fm_blocks(chans, span=FM_BLOCK_SPAN_MHZ, half=FM_BLOCK_HALF_MHZ) -> list:
    """FM channels -> [(tuning MHz, [channels])]: as few captures as possible, each tuned away from every station in
    it (up to 150 kHz: the receiver's DC spike never sits on a carrier)."""
    chans, out, i = sorted(chans), [], 0
    while i < len(chans):
        j = i
        while j + 1 < len(chans) and chans[j + 1] - chans[i] <= span + 1e-9:
            j += 1
        grp = chans[i:j + 1]
        lo, hi, mid = grp[-1] - half, grp[0] + half, (grp[0] + grp[-1]) / 2.0
        cands = [lo + k * 0.01 for k in range(int(round((hi - lo) / 0.01)) + 1)]
        tune = max(cands, key=lambda c: (round(min(min(abs(c - ch) for ch in grp), 0.15), 3), -abs(c - mid)))
        out.append((round(tune, 3), grp))
        i = j + 1
    return out


def _fm_block_samples(secs) -> int:
    return max(1, int(round(float(secs) * FM_BLOCK_FS / FM_BLOCK_CHUNK))) * FM_BLOCK_CHUNK


def _fm_block_analyse(raw, fs, tune_hz, chans, serial) -> dict:
    """One wide capture -> {channel: the same result as _stage1_wideband}. Each station is cut out of the capture's
    spectrum (+-128 kHz, flat to +-100 kHz) chunk by chunk, so every station gets the whole capture time. A chunk is
    a whole number of turns of each station's offset, so the pieces join without a phase step."""
    C = FM_BLOCK_CHUNK
    b = np.frombuffer(raw, dtype=np.uint8)
    nch = len(b) // (2 * C)
    if nch < 1:
        raise RFError("IQ capture too short for the FM check.")
    dc = b[: nch * 2 * C].reshape(-1, 2).mean(axis=0)            # the dongle's DC offset, I and Q
    # samples at the 8-bit converter's limits: strong stations overload it, and two of them then mix into copies on
    # other channels (2 x f1 - f2), RDS and all
    clipped = float(np.count_nonzero((b == 0) | (b == 255))) / max(1, len(b))
    out_fs, df, nb = 256000.0, fs / C, 1024
    m = int(round(C * out_fs / fs))                              # bins per station (2^19)
    fo = np.fft.fftfreq(m, 1.0 / out_fs)
    a = np.abs(fo)
    taper = np.where(a <= 100e3, 1.0, np.where(a >= 128e3, 0.0, 0.5 * (1.0 + np.cos(np.pi * (a - 100e3) / 28e3))))
    rel = np.round(fo / df).astype(np.int64)
    k0 = {ch: int(round((ch * 1e6 - tune_hz) / df)) for ch in chans}
    pieces, psd = {ch: [] for ch in chans}, np.zeros(nb)
    for c in range(nch):
        _check_stop()
        seg = b[2 * c * C:2 * (c + 1) * C].astype(np.float32)
        z = np.empty(C, dtype=np.complex64)
        z.real = (seg[0::2] - dc[0]) / 128.0
        z.imag = (seg[1::2] - dc[1]) / 128.0
        X = np.fft.fft(z)
        del z, seg
        psd += np.fft.fftshift(np.abs(X) ** 2).reshape(nb, C // nb).mean(axis=1)
        for ch in chans:
            pieces[ch].append(np.fft.ifft(X[(k0[ch] + rel) % C] * taper).astype(np.complex64))
        del X
    fp = (np.arange(nb) - nb / 2.0 + 0.5) * fs / nb               # centre of each coarse PSD bin (2 kHz)
    ref = (np.abs(fp) <= 0.95e6) & (np.abs(fp) > 6e3)
    floor = float(np.percentile(psd[ref], 20)) + 1e-30
    out = {}
    for ch in chans:
        o = k0[ch] * df
        win = np.abs(fp - o) <= 120e3
        snr_db = 10.0 * math.log10(float(np.mean(psd[win])) / floor) if win.any() else 0.0
        near = np.abs(fp - o) <= 100e3
        peak_db = 10.0 * math.log10(float(np.max(psd[near])) / floor) if near.any() else None
        band = np.abs(fp - o) <= 250e3
        ex = np.maximum(psd[band] - floor, 0.0)
        obw_khz = 0.0
        if ex.sum() > 0:
            cum, fb = np.cumsum(ex) / ex.sum(), fp[band]
            obw_khz = float(fb[int(np.searchsorted(cum, 0.995))] - fb[int(np.searchsorted(cum, 0.005))]) / 1e3
        try:
            out[ch] = _wbfm_analyse(np.concatenate(pieces[ch]), out_fs, (tune_hz + o) / 1e6, snr_db, obw_khz, serial,
                                    peak_snr_db=peak_db)
            if clipped > 0.001:
                out[ch]["overloaded_pct"] = round(100.0 * clipped, 2)
        except Exception as e:
            log.error("FM check of %.1f failed:\n%s", ch, traceback.format_exc())
            out[ch] = {"status": f"Wideband IQ analysis error: {e}", "signal_present_during_capture": False}
        pieces[ch] = None
    return out


def _mark_hd_sidebands(res) -> None:
    """A 'not standard FM' result one or two tenths from a station that is there (named, or on its channel) is that
    station's HD Radio digital sideband: on the live station 97.9 (carrier read at 97.8686 = 97.7 + 168.6 kHz, the
    middle of 97.7's upper block) and 91.4 next to 91.3 were reported as 'not standard FM broadcast' signals."""
    station = {ch: r for ch, r in res.items() if r.get("state") in ("named", "no_name", "rds_weak")}
    for ch, r in res.items():
        if r.get("state") != "not_fm" or r.get("band") == _NAME_BAND["narrow"]:
            continue
        nb = min((s for s in station if 0.05 < abs(s - ch) <= 0.25), key=lambda s: abs(s - ch), default=None)
        if nb is not None and (station[nb].get("snr_db") or 0.0) >= (r.get("snr_db") or 0.0):
            r.update(state="hd_sideband", sideband_of=nb,
                     band=f"Digital (HD Radio) sideband of {nb:.1f} MHz, not a station of its own")


def _names_report(chans, res, serials, errors=None) -> dict:
    """Name all's result: named stations, what was found on the rest, and a plain message."""
    named = {f"{ch:.1f}": {k: res[ch].get(k) for k in ("name", "names", "pi", "dongle", "scrolls")}
             for ch in chans if res.get(ch, {}).get("state") == "named"}
    gone = "not_checked" if errors else "not_heard"
    missing = [{"channel_mhz": ch, "state": gone, "band": _NAME_BAND[gone]} if ch not in res else
               {"channel_mhz": ch, **{k: v for k, v in res[ch].items() if k != "serial"}}
               for ch in chans if res.get(ch, {}).get("state") != "named"]
    by = lambda st: [x for x in missing if x["state"] == st]
    parts = [f"Named {len(named)} of {len(chans)} FM stations from their RDS data"]
    if by("no_name"):
        parts.append("1 more is on its station channel, but no RDS name was read from it this time"
                     if len(by("no_name")) == 1 else
                     f"{len(by('no_name'))} more are on their station channels, but no RDS name was read from them "
                     f"this time")
    if by("rds_weak"):
        parts.append(("1 sends" if len(by("rds_weak")) == 1 else f"{len(by('rds_weak'))} send")
                     + " RDS too weak to read a name here: " + ", ".join(f"{x['channel_mhz']:.1f}" for x in by("rds_weak")))
    if by("off_channel"):
        parts.append("NOT on a station channel: " + ", ".join(f"{x['carrier_mhz']:.3f} MHz" for x in by("off_channel"))
                     + " (no regular station; worth a closer look)")
    if by("not_fm"):
        parts.append("Not standard FM broadcast: " + ", ".join(f"{x['channel_mhz']:.1f}" for x in by("not_fm")))
    if by("hd_sideband"):
        parts.append("Digital (HD Radio) sidebands of the stations next to them, not stations of their own: "
                     + ", ".join(f"{x['channel_mhz']:.1f} (of {x['sideband_of']:.1f})" for x in by("hd_sideband")))
    if by("too_weak"):
        parts.append("Too weak here to read (under 6 dB across the channel): "
                     + ", ".join(f"{x['channel_mhz']:.1f}" for x in by("too_weak")))
    if by("not_heard"):
        parts.append("Not heard in the check (weak or on and off): "
                     + ", ".join(f"{x['channel_mhz']:.1f}" for x in by("not_heard")))
    if by("not_checked"):
        parts.append("Could not be checked this time (the dongles failed to capture): "
                     + ", ".join(f"{x['channel_mhz']:.1f}" for x in by("not_checked")))
    for ser, (kind, err) in (errors or {}).items():
        if kind == "wide":
            parts.append(f"{_short(ser)}'s wide capture failed ({err[:160].rstrip('.')}), so it read its stations "
                         f"one at a time instead")
        else:
            parts.append(f"{_short(ser)} had a problem ({err[:120].rstrip('.')})"
                         + ("; its stations were tried on the other dongle" if len(serials) > 1 else ""))
    over = sorted({r["dongle"] for r in res.values() if r.get("overloaded_pct")})
    if over:
        parts.append(f"{' and '.join(over)} {'was' if len(over) == 1 else 'were'} overloaded by strong stations in "
                     f"part of the band (a lower FM gain would help); readings there may include copies of other "
                     f"stations")
    # every station's carrier is measured: a dongle whose stations all sit a few kHz off needs recalibrating
    for ser in serials:
        offs = sorted(float(r["offset_khz"]) for r in res.values() if r.get("serial") == ser and r.get("offset_khz")
                      is not None and r["state"] in ("named", "no_name", "rds_weak"))
        if len(offs) >= 3 and abs(offs[len(offs) // 2]) > 3.0:
            parts.append(f"{_short(ser)} reads stations {offs[len(offs) // 2]:+.1f} kHz off their channels: its "
                         f"frequency correction is off (Run setup recalibrates it)")
    return {"named": named, "not_named": missing,
            "counts": {st: sum(1 for r in res.values() if r["state"] == st) for st in _NAME_RANK},
            "message": ". ".join(parts) + "."}


def name_fm_stations(freqs, max_n=40, best=None, on_result=None, on_plan=None) -> dict:
    """RDS station names for FM channels (the Spectrum list's). The band is read in 2 MHz slices: one 6 s capture
    hears every station in its slice at once, and the dongles work in parallel (each slice on the dongle that hears
    most of its stations best, `best` = {channel: dongle} from the sweep, as long as the work stays shared); each
    dongle captures its next slice while the last one is decoded. Stations that send RDS but gave no whole name get
    one 12 s look; weak ones that showed nothing get a look on another dongle. Every station checked is remembered
    (fm_stations.json): named, on its channel without a name, or off the channel, so the Spectrum list shows what
    was found. on_result(partial report) is called as results come in; on_plan(seconds) when the work still to
    do changes (the retries are planned, a dongle falls back to one station at a time), so the page's time left
    stays true (v4.8.6 said 75 s and took 172)."""
    import queue
    cfg = _load_config()
    chans = sorted({round(float(f), 1) for f in freqs if _in_fm_band(float(f))})[:int(max_n)]
    if not chans:
        raise RFError("No FM-band signals to name.")
    free = _working_serials(cfg)
    hears = lambda s: ((_dongle_cfg(s, cfg).get("reception") or {}).get("88-108") or {})
    fm = sorted([s for s in free if hears(s).get("signals")], key=lambda s: -(hears(s).get("strongest_snr_db") or 0))
    serials = fm or free
    if not serials:
        raise RFError("No dongle is free to listen (all busy with captures?).")
    pref = {}
    for ch_s, d in (best or {}).items():
        try:
            ser = _resolve_dongle_serial(d, fallback=None)
            if ser in serials:
                pref[round(float(ch_s), 1)] = ser
        except (TypeError, ValueError):
            continue
    t0, res, errors, lk = time.time(), {}, {}, threading.Lock()

    def plan(seconds):
        if on_plan:
            try:
                on_plan(float(seconds))
            except Exception:
                pass
    n_done, total, dead, slow = [0], [0], set(), set()   # slow: wide captures failed there, one station at a time

    def assign(groups, secs, among):
        """[(tune, chans)] -> {serial: [(tune, chans, secs)]}: preferred dongle first, the work kept shared."""
        among = [s for s in among if s not in dead] or [s for s in serials if s not in dead]
        tasks, load = {s: [] for s in among}, {s: 0 for s in among}
        if not among:
            return tasks
        cap = math.ceil(1.5 * len(groups) / len(among))
        score = lambda g, s: sum(1 for ch in g[1] if pref.get(ch) == s)
        for g in sorted(groups, key=lambda g: -max(score(g, s) for s in among)):
            ok = [s for s in among if load[s] < cap] or among
            s = max(ok, key=lambda s: (score(g, s), -load[s]))
            tasks[s].append((g[0], g[1], secs))
            load[s] += 1
        return tasks

    def merge(ser, r):
        with lk:
            for ch, x in r.items():
                new, old = _fm_check_result(x, ser), res.get(ch)
                if old is None or (_NAME_RANK[new["state"]], new.get("snr_db") or -99) > \
                        (_NAME_RANK[old["state"]], old.get("snr_db") or -99):
                    res[ch] = new
            snap = _names_report([c for c in chans if c in res], res, serials) if on_result else None
        if on_result:
            try:
                on_result(snap)
            except Exception:
                pass

    def run(tasks):
        def work(ser):
            todo = tasks.get(ser) or []
            gain = _gain_for(ser, 88.0, 108.0, cfg)
            q = queue.Queue(maxsize=1)               # capture the next slice while this one is decoded

            def analyse():
                while True:
                    item = q.get()
                    if item is None:
                        return
                    tune, grp, raw = item
                    try:
                        merge(ser, _fm_block_analyse(raw, FM_BLOCK_FS, tune * 1e6, grp, ser))
                    except Stopped:
                        continue                     # keep emptying the queue until the end marker
                    except Exception as e:
                        log.error("FM slice %.2f failed:\n%s", tune, traceback.format_exc())
                        merge(ser, {ch: {"status": f"analysis error: {e}", "signal_present_during_capture": False}
                                    for ch in grp})
            th = threading.Thread(target=_carry_ctx(analyse), name="fm-names-dsp", daemon=True)
            th.start()
            def one_by_one(grp, secs):
                """The v4.7.3 way, for a dongle whose wide captures fail: 1 MS/s, one station per look."""
                got, ok = {}, False
                for ch in grp:
                    _check_stop()
                    time.sleep(USB_SETTLE_S)
                    x = _stage1_wideband(ch, ser, gain, min(float(secs), 4.0))
                    if not str(x.get("status", "")).startswith("IQ capture failed"):
                        got[ch], ok = x, True
                if got:
                    merge(ser, got)
                return ok

            fails = 0
            try:
                with _use_dongle(ser, timeout=30, holder="naming FM stations"):
                    for i, (tune, grp, secs) in enumerate(todo):
                        _check_stop()
                        if i:
                            time.sleep(USB_SETTLE_S)  # the same dongle, opened again at once
                        with lk:
                            n_done[0] += 1
                            k = n_done[0]
                        _say(f"Listening to {', '.join(f'{c:.1f}' for c in grp)} MHz on {_short(ser)} "
                             f"({min(k, total[0])} of {total[0]})...")
                        if ser in slow:
                            ok = one_by_one(grp, secs)
                        else:
                            try:
                                raw = _capture_raw(ser, int(round(tune * 1e6)), FM_BLOCK_FS,
                                                   _fm_block_samples(secs), gain, timeout=int(secs) + 15,
                                                   min_bytes=2 * FM_BLOCK_CHUNK)
                                q.put((tune, grp, raw))
                                ok = True
                            except Stopped:
                                raise
                            except RFError as e:
                                log.warning("FM slice %.2f on %s: %s", tune, ser, e)
                                errors.setdefault(ser, ("wide", str(e)))
                                slow.add(ser)        # from now on one station at a time on this dongle
                                plan(sum(len(g2) for _t2, g2, _s2 in todo[i:]) * (min(float(secs), 4.0) + 1.5))
                                time.sleep(USB_SETTLE_S)
                                ok = one_by_one(grp, secs)
                        fails = 0 if ok else fails + 1
                        if fails >= 2:               # not even single looks work: leave its slices to another
                            dead.add(ser)
                            break
            finally:
                q.put(None)
                th.join()
        busy = [s for s in tasks if tasks[s]]
        with lk:
            total[0] += sum(len(tasks[s]) for s in busy)
        for ser, r in _parallel(work, busy).items():
            if isinstance(r, RFError) and not isinstance(r, Stopped):
                errors[ser] = ("other", str(r))
                dead.add(ser)                        # busy or broken: its slices go to another dongle
        _check_stop()

    run(assign(_plan_fm_blocks(chans), FM_NAME_LOOK_S, serials))
    # RDS is there but no whole name came: one long look on the dongle that heard it
    longer = {}
    for ch in chans:
        if res.get(ch, {}).get("state") == "rds_weak" and res[ch]["serial"] not in dead:
            longer.setdefault(res[ch]["serial"], []).append(ch)
    # weak and nothing readable (or never captured): another dongle's antenna may hear it better
    other = {}
    for ch in chans:
        r = res.get(ch)
        if r is None or (r["state"] in ("not_heard", "too_weak", "not_fm", "no_name") and (r.get("snr_db") or 0) < 20.0):
            tried = r["serial"] if r else None
            alt = [s for s in serials if s != tried and s not in dead]
            if alt:
                other.setdefault(alt[0], []).append(ch)
    tasks = {s: [] for s in serials}
    for ser, cs in longer.items():
        tasks[ser] += [(t, g, FM_NAME_RETRY_S) for t, g in _plan_fm_blocks(cs)]
    for ser, cs in other.items():
        tasks[ser] += [(t, g, FM_NAME_LOOK_S) for t, g in _plan_fm_blocks(cs)]
    if any(tasks.values()):
        plan(max(len(t) for t in tasks.values()) * (max(FM_NAME_RETRY_S, FM_NAME_LOOK_S) + 2.0) + 5.0)
        run(tasks)
    _mark_hd_sidebands(res)
    _remember_unheard(res)                       # not heard / too weak: remembered with the time (v4.9.3)
    out = _names_report(chans, res, serials, errors)
    out["took_seconds"] = round(time.time() - t0)
    return out
