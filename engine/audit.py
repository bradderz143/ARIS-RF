"""ARIS engine, part "audit": Baselines, the automated threat audit and the patrol.

Executed into aris.py's namespace by aris._include (see aris.py): names from aris.py's imports and from the
parts before this one are available here without imports, and everything defined here becomes aris.<name>.
"""
from __future__ import annotations

# ============================================================================
# TOOLS: BASELINES + AUTOMATED THREAT AUDIT
# ============================================================================
def _collapse(acc: dict, grid_hz: float) -> dict:
    out: dict = {}
    for hz, vals in acc.items():
        out.setdefault(int(round(hz / grid_hz)), []).extend(vals)
    res = {}
    for i, v in out.items():
        n = len(v)
        m = sum(v) / n
        res[i] = {"mean": m, "max": max(v),
                  "std": math.sqrt(sum((x - m) * (x - m) for x in v) / n) if n > 1 else 0.0}   # population std
    return res


_BASELINE_CACHE: dict = {}          # path -> ((mtime_ns, size), parsed file); patrol reads baselines every round


def _read_baseline(p: Path):
    try:
        st = p.stat()
    except OSError:
        _BASELINE_CACHE.pop(str(p), None)
        return None
    key = (st.st_mtime_ns, st.st_size)
    hit = _BASELINE_CACHE.get(str(p))
    if hit and hit[0] == key:
        return hit[1]
    try:
        with open(p) as f:
            d = json.load(f)
    except (OSError, ValueError):
        log.warning("Unreadable baseline skipped: %s", p)
        return None
    if not isinstance(d, dict):
        return None
    if len(_BASELINE_CACHE) > 40:
        _BASELINE_CACHE.clear()
    _BASELINE_CACHE[str(p)] = (key, d)
    return d


def _baseline_bins(base: dict) -> dict:
    """{grid index: [mean, max, std]} of a baseline (converted once per file, then cached with it). Baselines
    recorded before v4.7.2 stored rtl_power's bins up to one bin too high: they are moved to the true frequencies
    here, so audits do not see every strong signal's edges as new."""
    bm = base.get("_bmap")
    if bm is None:
        shift = 0
        if int(base.get("freq_map") or 1) < 2:
            try:
                from arislib import rtlpower as _rp
                b = base.get("band") or {}
                bias = _rp.old_bias_hz(float(b["start_mhz"]) * 1e6, float(b["stop_mhz"]) * 1e6,
                                       float(base.get("step_khz") or float(base["grid_hz"]) / 1000.0) * 1000.0,
                                       _rp.parse_crop(_load_config().get("crop_percent", "20%"), 0.2))
                shift = int(round(bias / float(base["grid_hz"])))
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                shift = 0
        bm = base["_bmap"] = {int(k) - shift: v for k, v in base["bins"].items()}
        base["_shifted_bins"] = shift
    return bm


def _baseline_files(pattern="*.json"):
    out = []
    if BASELINE_DIR.exists():
        for p in sorted(BASELINE_DIR.glob(pattern)):
            d = _read_baseline(p)
            if d is not None:
                out.append((p, d))
    return out


def _find_baseline(start, stop, serial):
    """Baselines are per dongle: each antenna hears a different 'normal'. Files are named <band>MHz_<serial>.json,
    so only that dongle's files are opened."""
    best = None
    for p, d in _baseline_files(f"*_{serial}.json"):
        if str(d.get("serial")) != str(serial):
            continue
        b = d.get("band", {})
        if b.get("start_mhz", 1e9) <= start + 1e-9 and b.get("stop_mhz", -1) >= stop - 1e-9:
            span = b["stop_mhz"] - b["start_mhz"]
            if best is None or span < best[2]:
                best = (p, d, span)
    return (best[0], best[1]) if best else None


def _baseline_summary(grid, step_khz, hops_mhz, cfg):
    """What was on the air while the baseline was recorded (strongest level per frequency)."""
    gh = step_khz * 1000.0
    guard = _guard_mhz(step_khz)
    held = {round(i * gh / 1e6, 4): v["max"] for i, v in grid.items()}
    clean = {f: v for f, v in held.items() if not _near_hop(f, hops_mhz, guard)} or held
    floor = _robust_floor([v["mean"] for v in grid.values()])
    _, peaks = _extract_peak_clusters(clean, 6.0, cfg["cluster_gap_khz"], step_khz,
                                      floor=_robust_floor(list(clean.values())))
    peaks = _group_broadcast(peaks, step_khz)
    top = sorted(peaks, key=lambda p: p["snr_db"], reverse=True)[:10]
    return ({"signals_present": len(peaks), "noise_floor_db": floor,
             "strongest": [{"mhz": p.get("channel_mhz") or p["frequency_mhz"], "snr_db": p["snr_db"],
                            "band": p["probable_allocation"]} for p in top]}, floor)


def _record_one_baseline(serial, s, e, step, dur, label, cfg):
    gain = _gain_for(serial, s, e, cfg)
    acc, hop_hz = _rtl_power(s, e, step, gain, serial, duration_s=dur, integration_s=1, lock_timeout=60.0 + dur,
                             holder="baseline")
    grid = _collapse(acc, step * 1000.0)
    summary, floor = _baseline_summary(grid, step, [h / 1e6 for h in hop_hz], cfg)
    path = BASELINE_DIR / f"{s:g}-{e:g}MHz_{serial}.json"
    _atomic_write_json(path, {
        "version": 7, "freq_map": 2, "created": _now(), "created_epoch": time.time(), "label": label,
        "band": {"start_mhz": s, "stop_mhz": e}, "step_khz": step, "grid_hz": step * 1000.0,
        "gain_db": gain, "ppm": _ppm_of(serial, cfg), "duration_s": dur, "serial": serial, "summary": summary,
        "bins": {str(i): [round(v["mean"], 2), round(v["max"], 2), round(v["std"], 2)] for i, v in grid.items()},
    })
    return {"file": path.name, "gain_db": gain, "bins_stored": len(grid), "duration_s": dur,
            "noise_floor_db": floor, "summary": summary}


def _record_baselines(s, e, serials, dur, label, step=None):
    cfg = _load_config()
    return _parallel(_record_one_baseline, list(serials), s, e, float(step or cfg["step_khz"]), dur, label, cfg)


@register(
    "record_baseline",
    "Record a spectral baseline for a band on every dongle at once (each antenna gets its own baseline): watches "
    "the band for duration_s seconds and stores the average and strongest level of every frequency.",
    {**_BAND_PROPS, **_DONGLE_PROP,
     "duration_s": {"type": "integer", "description": "How long to watch, in seconds (default 30)"},
     "label": {"type": "string", "description": "Optional note, e.g. 'quiet night'"}},
)
def record_baseline(frequency_range=None, start_mhz=None, stop_mhz=None, dongle=None,
                    step_khz=None, duration_s=None, label=None):
    cfg = _load_config()
    s, e = _resolve_band(start_mhz, stop_mhz, frequency_range)
    step = _num(step_khz, "step_khz", cfg["step_khz"])
    dur = _int(duration_s, "duration_s", int(cfg.get("baseline_seconds", 30)), 2, 600)
    serials = _pick_serials(dongle, cfg)
    print(f"[HW] recording a {dur} s baseline of {s:g}-{e:g} MHz on {', '.join(_short(x) for x in serials)}"
          + (" at the same time..." if len(serials) > 1 else "..."))
    per, errors = {}, []
    for ser, r in _record_baselines(s, e, serials, dur, label, step).items():
        if isinstance(r, Exception):
            errors.append(f"{_short(ser)}: {r}")
            continue
        per[_short(ser)] = {"gain_db": r["gain_db"], "noise_floor_db": r["noise_floor_db"],
                            "signals_on_air": r["summary"]["signals_present"],
                            "strongest": r["summary"]["strongest"][:5]}
    if not per:
        raise RFError("Baseline failed on every dongle: " + "; ".join(errors))
    out = {"message": "Baseline recorded", "band_mhz": [s, e], "duration_s": dur, "per_dongle": per}
    if errors:
        out["dongle_errors"] = errors
    return out


@register("list_baselines", "List stored baselines (one per band per dongle) and what was on the air when each was "
          "recorded. Use for 'what did the baseline find'.", {})
def list_baselines():
    items = []
    for p, d in _baseline_files():
        items.append({"band_mhz": [d["band"]["start_mhz"], d["band"]["stop_mhz"]], "dongle": _short(d.get("serial")),
                      "created": d.get("created"), "label": d.get("label"), "seconds": d.get("duration_s"),
                      "gain_db": d.get("gain_db"),
                      "what_was_on_air": d.get("summary") or "not recorded (older baseline)"})
    return {"count": len(items), "baselines": items}


def _audit(s, e, serial, trig, dur, lock_timeout=60.0):
    """Watch a band on one dongle and compare it with that dongle's own baseline."""
    found = _find_baseline(s, e, serial)
    if not found:
        raise RFError(f"{_short(serial)} has no baseline covering {s:g}-{e:g} MHz. Run record_baseline first.")
    path, base = found
    grid_hz = float(base["grid_hz"])
    step_khz = grid_hz / 1000.0
    gain = base["gain_db"] if base.get("gain_db") is not None else _gain_for(serial, s, e)
    live, hop_hz = _rtl_power(s, e, step_khz, gain, serial, duration_s=dur, integration_s=1,
                              lock_timeout=lock_timeout, holder="threat audit")
    hops = [h / 1e6 for h in hop_hz]
    live_g = _collapse(live, grid_hz)
    bmap = _baseline_bins(base)
    base_floor = base.get("_floor")
    if base_floor is None:
        base_floor = base["_floor"] = statistics.median(v[0] for v in bmap.values())

    hits, compared = [], 0
    for idx in sorted(live_g):
        b = bmap.get(idx)
        if b is None:
            continue
        compared += 1
        delta = live_g[idx]["mean"] - b[0]
        burst = live_g[idx]["max"] - b[1]       # strongest live reading vs strongest during the baseline
        if delta >= max(trig, SIGMA_K * b[2]):
            hits.append((idx, live_g[idx]["mean"], b[0], delta, "steady"))
        elif burst >= trig:
            hits.append((idx, live_g[idx]["max"], b[1], burst, "intermittent"))
    if compared == 0:
        raise RFError("Live sweep and baseline share no frequency bins; re-record the baseline for this band.")

    clusters, cur = [], []
    for h in hits:
        if cur and h[0] - cur[-1][0] > 1:
            clusters.append(cur)
            cur = []
        cur.append(h)
    if cur:
        clusters.append(cur)

    g = _guard_mhz(step_khz)
    events, rejected = [], 0
    for c in clusters:
        peak = max(c, key=lambda h: h[3])
        peak_mhz = peak[0] * grid_hz / 1e6
        ev = {"peak_mhz": round(peak_mhz, 4), "baseline_db": round(peak[2], 2), "detected_db": round(peak[1], 2),
              "surge_delta_db": round(peak[3], 2), "bandwidth_khz": round(len(c) * grid_hz / 1e3, 1),
              "pattern": peak[4], "new_signal": peak[2] < base_floor + 3.0,
              "hint": _classify_frequency_allocation(peak_mhz, len(c) * grid_hz / 1e3)}
        if len(c) <= 6 and _near_hop(peak_mhz, hops, g):
            try:
                snr2 = _confirm_off_center(peak_mhz, step_khz, gain, serial, lock_timeout,
                                           min(int(dur), 10) if peak[4] == "intermittent" else 0)
            except RFError:
                snr2 = None
            if snr2 is None or snr2 < 6.0:
                rejected += 1
                continue
            ev["rechecked_off_centre_snr_db"] = snr2
        events.append(ev)
    events.sort(key=lambda x: x["surge_delta_db"], reverse=True)
    age_h = round((time.time() - base.get("created_epoch", time.time())) / 3600, 1)
    rep = {"dongle": _short(serial), "baseline_recorded": base.get("created"), "baseline_age_hours": age_h,
           "gain_db": gain, "bins_compared": compared, "flagged": len(events),
           "hop_centre_artifacts_rejected": rejected, "events": events[:40]}
    if age_h > 72:
        rep["warning"] = "Baseline is older than 72 h; consider re-recording."
    ppm_now = _ppm_of(serial)
    if base.get("ppm", ppm_now) != ppm_now:
        rep["warning"] = "Frequency correction changed since this baseline; re-record it."
    return rep


def _merge_events(per):
    """Audit events from several dongles -> one entry per frequency, noting which dongles flagged it."""
    merged = []
    for ser, rep in per.items():
        for ev in rep["events"]:
            tol = max(0.03, ev["bandwidth_khz"] / 2000.0)
            m = next((x for x in merged if abs(x["peak_mhz"] - ev["peak_mhz"]) <= tol), None)
            if m is None:
                m = {**ev, "flagged_by": {}}
                merged.append(m)
            elif ev["surge_delta_db"] > m["surge_delta_db"]:
                m.update({k: v for k, v in ev.items()})
            m["flagged_by"][_short(ser)] = ev["surge_delta_db"]
    merged.sort(key=lambda x: x["surge_delta_db"], reverse=True)
    return merged


PATROL_SEEN = DATA_DIR / "patrol_seen.json"
_SEEN_LOCK = threading.Lock()


def _seen_update(band_key, events):
    """Remember where audits flagged something (50 kHz cells, forgotten after 7 days). A device that keeps showing
    up at the same frequency is a regular, such as a neighbour's sensor; a first appearance stands out."""
    with _SEEN_LOCK:
        try:
            with open(PATROL_SEEN) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        band, now = data.setdefault(band_key, {}), time.time()
        for ev in events:
            k = f"{round(ev['peak_mhz'] * 20) / 20:.2f}"
            rec = band.get(k)
            ev["seen_in_earlier_audits"] = rec["n"] if rec else 0
            if rec:
                ev["first_seen"] = rec["first"]
            band[k] = {"n": (rec["n"] if rec else 0) + 1, "first": rec["first"] if rec else _now(), "t": now}
        for k in [k for k, v in band.items() if now - float(v.get("t", now)) > 7 * 86400]:
            del band[k]
        try:
            _atomic_write_json(PATROL_SEEN, data)
        except OSError:
            pass


def _score_event(ev):
    """Threat score with reasons. Short bursts are normal in the sensor bands and regulars are expected; a new,
    continuous transmitter (or an FM-band one off the station plan) is what a hidden transmitter looks like."""
    d, f = float(ev["surge_delta_db"]), float(ev["peak_mhz"])
    steady = ev["pattern"] == "steady"
    score, why = d * (1.5 if steady else 1.0), ["continuous transmission" if steady else "short bursts"]
    if ev.get("new_signal"):
        score += 4
        why.append("not present in the baseline")
    if _in_sensor_band(f) and not steady:
        score *= 0.6
        why.append("in a sensor band, where short bursts are normal")
    if _in_fm_band(f) and steady and ev.get("new_signal") and not _on_fm_plan(f, 30.0):
        score += 6
        why.append("FM-band transmitter off the station channel plan")
    n = int(ev.get("seen_in_earlier_audits") or 0)
    if n >= 3 and not steady:
        score *= 0.6
        why.append(f"flagged at this frequency in {n} earlier audits (a regular)")
    elif n >= 1 and steady:
        why.append(f"still there: flagged in {n} earlier audits (re-record the baseline once you know what it is)")
    elif n == 0:
        why.append("first time flagged at this frequency")
    ev["risk_score"], ev["threat_level"], ev["why"] = round(score, 1), _threat_level(score), why


def _collapse_hopping(events, s, e):
    """Many short bursts on many different frequencies in one audit is frequency hopping (smart meters, LoRa and
    other ISM devices do it all the time): report it once instead of as a dozen separate threats."""
    hop = [x for x in events if x["pattern"] == "intermittent" and x["bandwidth_khz"] <= 500]
    fs = [x["peak_mhz"] for x in hop]
    if len(hop) < 5 or max(fs) - min(fs) < max(1.0, 0.3 * (e - s)):
        return events
    top = sorted(hop, key=lambda x: -x["surge_delta_db"])[:3]
    by = {}
    for x in hop:
        for k, v in x.get("flagged_by", {}).items():
            by[k] = max(by.get(k, 0.0), v)
    summary = {"peak_mhz": top[0]["peak_mhz"], "pattern": "frequency hopping", "channels": len(hop),
               "span_mhz": [round(min(fs), 3), round(max(fs), 3)], "strongest_mhz": [t["peak_mhz"] for t in top],
               "surge_delta_db": top[0]["surge_delta_db"], "bandwidth_khz": top[0]["bandwidth_khz"], "flagged_by": by,
               "hint": ("short bursts hopping across many frequencies: typical of smart electricity or water meters, "
                        "LoRa and other frequency-hopping devices"),
               "risk_score": round(min(14.0, top[0]["surge_delta_db"] * 0.5), 1), "threat_level": "LOW",
               "why": [f"{len(hop)} short bursts on different frequencies across "
                       f"{round(max(fs) - min(fs), 1)} MHz", "frequency hopping is normal in this band"]}
    return [x for x in events if x not in hop] + [summary]


def _audit_all(s, e, serials, trig, dur, lock_timeout):
    with_base = [x for x in serials if _find_baseline(s, e, x)]
    if not with_base:
        raise RFError(f"No baseline covers {s:g}-{e:g} MHz on {', '.join(_short(x) for x in serials)}. "
                      f"Run record_baseline (or /setup) for this band first.")
    res = _parallel(lambda ser: _audit(s, e, ser, trig, dur, lock_timeout), with_base)
    per = {ser: r for ser, r in res.items() if not isinstance(r, Exception)}
    errors = [f"{_short(ser)}: {r}" for ser, r in res.items() if isinstance(r, Exception)]
    errors += [f"{_short(x)}: no baseline for this band" for x in serials if x not in with_base]
    events = _collapse_hopping(_merge_events(per), s, e)
    fixed = [x for x in events if x["pattern"] != "frequency hopping"]
    _seen_update(_band_key(s, e), fixed)
    for ev in fixed:
        _score_event(ev)
    events.sort(key=lambda x: x["risk_score"], reverse=True)
    return per, events, errors


@register(
    "run_automated_threat_audit",
    "Watch a band on every dongle at once and compare each with its own stored baseline; flags steady surges and "
    "new or intermittent transmitters.",
    {**_BAND_PROPS, **_DONGLE_PROP,
     "trigger_delta_db": {"type": "number", "description": "Minimum dB surge vs baseline (default 10)"},
     "duration_s": {"type": "integer", "description": "How long to watch, in seconds (default 10)"}},
    aliases=("detect_anomalies",),
)
def run_automated_threat_audit(frequency_range=None, start_mhz=None, stop_mhz=None, dongle=None,
                               trigger_delta_db=None, duration_s=None):
    cfg = _load_config()
    s, e = _resolve_band(start_mhz, stop_mhz, frequency_range)
    dur = _int(duration_s, "duration_s", int(cfg.get("audit_seconds", 10)), 2, 600)
    serials = _pick_serials(dongle, cfg)
    print(f"[HW] watching {s:g}-{e:g} MHz for {dur}s on {', '.join(_short(x) for x in serials)} and comparing "
          f"each with its own baseline...")
    per, events, errors = _audit_all(s, e, serials, _num(trigger_delta_db, "trigger_delta_db", 10.0), dur, 60.0 + dur)
    report = {"timestamp": _now(), "band_audited": f"{s:g}-{e:g} MHz", "watched_seconds": dur,
              "dongles": {_short(k): {x: v[x] for x in ("flagged", "baseline_age_hours",
                                                       "hop_centre_artifacts_rejected", "warning") if x in v}
                          for k, v in per.items()},
              "total_threats_flagged": len(events), "threat_manifest": events[:10]}
    if errors:
        report["dongle_notes"] = errors
    _atomic_write_json(AUDIT_REPORT, report)
    try:
        _jsonl_append(AUDIT_HISTORY, {k: report[k] for k in ("timestamp", "band_audited", "total_threats_flagged")},
                      AUDIT_LOG_MAX_LINES)
    except OSError:
        pass
    return report


# ============================================================================
# PATROL: periodic threat audits in the background (no console output; logs to file)
# ============================================================================
PATROL_STATE = DATA_DIR / "patrol_state.json"


class Patrol:
    def __init__(self, bands, interval_min, serials):
        self.bands = list(bands)
        self.interval_s = max(30.0, float(interval_min) * 60.0)
        self.serials = list(serials)
        self.stop_evt = threading.Event()
        self.thread = None
        self.runs = deque(maxlen=40)
        self.flagged_total = 0
        self.started = _now()
        self.checked = {}                    # frequency -> time it was last auto-investigated

    def _save_state(self) -> None:
        try:
            _atomic_write_json(PATROL_STATE, {**self.summary(), "updated": _now()})
        except OSError:
            pass

    def _auto_check(self, ev, rec) -> None:
        """A HIGH event gets a quick investigation on the dongle that flagged it most strongly (once an hour per
        frequency), so the log says what it was, not just that something appeared."""
        key = round(float(ev["peak_mhz"]), 2)
        if time.time() - self.checked.get(key, 0.0) < 3600 or self.stop_evt.is_set():
            return
        self.checked[key] = time.time()
        names = {_short(x): x for x in DONGLE_NAMES}
        by = ev.get("flagged_by") or {}
        ser = names.get(max(by, key=by.get)) if by else self.serials[0]
        peak = {"peak_id": 1, "frequency_mhz": ev["peak_mhz"], "occupied_bw_khz": ev.get("bandwidth_khz", 25.0),
                "probable_allocation": ev.get("hint")}
        try:
            _QUIET.on = True
            res = _investigate(peak, [ser], listen_s=12, timed=False)     # the automatic check, not a timed one
            rec["checked"] = {"mhz": ev["peak_mhz"], "dongle": _short(ser), "evidence": res["evidence"],
                              "verdict": _cut_at_word(res.get("verdict_core") or res["final_signal_verdict"], 220)}
        except RFError as ex:
            rec["checked"] = {"mhz": ev["peak_mhz"], "error": str(ex)[:120]}
        finally:
            _QUIET.on = False

    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="patrol", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_evt.set()
        if self.thread:
            self.thread.join(timeout=60)
        self._save_state()

    def run_once(self) -> None:
        for s, e in self.bands:
            if self.stop_evt.is_set():
                return
            rec = {"time": _now(), "band": f"{s:g}-{e:g} MHz"}
            try:
                secs = int(_load_config().get("audit_seconds", 10))
                per, events, errors = _audit_all(s, e, self.serials, 10.0, secs, lock_timeout=3.0)
                rec["flagged"] = len(events)
                rec["levels"] = dict(Counter(x["threat_level"] for x in events))
                rec["top"] = events[:3]
                rec["dongles_checked"] = [_short(x) for x in per]
                rec["serials"] = {_short(x): x for x in self.serials}     # names change when a dongle is forgotten
                if errors:
                    rec["skipped"] = [x[:120] for x in errors]
                self.flagged_total += rec["flagged"]
                high = [x for x in events if x["threat_level"] == "HIGH" and x["pattern"] != "frequency hopping"]
                if high:
                    self._auto_check(high[0], rec)
                if rec["flagged"]:
                    _jsonl_append(PATROL_LOG, rec, PATROL_LOG_MAX_LINES)
            except RFError as ex:
                rec["skipped"] = str(ex)[:160]
            except Exception as ex:
                rec["error"] = f"{type(ex).__name__}: {ex}"[:160]
                log.error("patrol run failed:\n%s", traceback.format_exc())
            self.runs.append(rec)
        self._save_state()

    def _run(self) -> None:
        self._save_state()
        while not self.stop_evt.is_set():
            self.run_once()
            self.stop_evt.wait(self.interval_s)
        self._save_state()

    def summary(self) -> dict:
        return {"running": self.alive(), "started": self.started, "dongles": [_short(x) for x in self.serials],
                "bands": [f"{s:g}-{e:g} MHz" for s, e in self.bands],
                "interval_min": round(self.interval_s / 60.0, 1), "audits_done": len(self.runs),
                "events_flagged_total": self.flagged_total, "recent_runs": list(self.runs)[-6:]}


_PATROL = None


def _parse_band_list(text):
    items = [str(x) for x in text] if isinstance(text, (list, tuple)) else re.split(r"[;,]|\band\b", str(text))
    return [_resolve_band(frequency_range=it.strip()) for it in items if it.strip()]


@register(
    "start_patrol",
    "Start background patrol: every few minutes, audit the bands on every dongle against its own baseline and "
    "log new transmitters. Best tool for spotting a transmitter that appears later (e.g. a bug switched on).",
    {"frequency_range": {"type": "string", "description": "Bands, e.g. '433-435MHz, 902-928MHz' (default: saved)"},
     "interval_min": {"type": "number", "description": "Minutes between rounds (default 15, min 1)"},
     **_DONGLE_PROP},
)
def start_patrol(frequency_range=None, interval_min=None, dongle=None):
    global _PATROL
    cfg = _load_config()
    if _PATROL and _PATROL.alive():
        return {"message": "Patrol already running", **_PATROL.summary()}
    bands = _parse_band_list(frequency_range or cfg["patrol_bands"])
    serials = _pick_serials(dongle, cfg)
    missing = [f"{s:g}-{e:g}" for s, e in bands if not any(_find_baseline(s, e, x) for x in serials)]
    if missing:
        raise RFError(f"No baseline for {missing}. Run record_baseline (or /setup) for these bands first.")
    interval = max(1.0, min(240.0, _num(interval_min, "interval_min", cfg["patrol_interval_min"])))
    with _CFG_LOCK:
        cfg = _load_config()
        cfg["patrol_bands"] = [f"{s:g}-{e:g}" for s, e in bands]
        cfg["patrol_interval_min"] = interval
        _save_config(cfg)
    _PATROL = Patrol(bands, interval, serials)
    _PATROL.start()
    return {"message": "Patrol started. A dongle that is busy with your requests is skipped for that round.",
            **_PATROL.summary()}


@register("stop_patrol", "Stop the background patrol.", {})
def stop_patrol():
    if not _PATROL or not _PATROL.alive():
        return {"message": "Patrol is not running"}
    _PATROL.stop()
    return {"message": "Patrol stopped", **_PATROL.summary()}


_OLD_LEVELS = {"CRITICAL": "HIGH", "SEVERE": "HIGH", "MODERATE": "MEDIUM", "ELEVATED": "MEDIUM", "MINOR": "LOW",
               "INFO": "LOW"}


def _patrol_rounds(maxlen=20) -> list:
    """The last flagged patrol rounds from the log, oldest first, in today's terms. Rounds from versions before git
    (the live station, 2026-10-31) say CRITICAL / MODERATE and used other score limits: every level is worked out
    again from its score (HIGH 25+, MEDIUM 15+), else the old word is mapped. Their dongle names follow the
    numbering of that day ('Dongle 2' was the removed 00002002): newer rounds carry the serials and get today's
    names, older ones are marked 'names_then'. Each round gets 't' (epoch seconds)."""
    out = []
    try:
        with open(PATROL_LOG, errors="replace") as f:
            lines = list(deque(f, maxlen=maxlen))
    except OSError:
        return out
    for line in lines:
        try:
            rd = json.loads(line)
            rd["t"] = time.mktime(time.strptime(rd["time"], "%Y-%m-%d %H:%M:%S"))
        except (ValueError, KeyError, TypeError):
            continue
        sers = rd.get("serials") if isinstance(rd.get("serials"), dict) else None
        now_name = (lambda n: _short(sers[n]) if n in sers else n) if sers else (lambda n: n)
        for ev in rd.get("top") or []:
            try:
                ev["threat_level"] = _threat_level(float(ev["risk_score"]))
            except (KeyError, TypeError, ValueError):
                lv = str(ev.get("threat_level") or "").upper()
                ev["threat_level"] = _OLD_LEVELS.get(lv, lv if lv in ("HIGH", "MEDIUM", "LOW") else "LOW")
            if isinstance(ev.get("flagged_by"), dict):
                ev["flagged_by"] = {now_name(k): v for k, v in ev["flagged_by"].items()}
        if isinstance(rd.get("levels"), dict):
            lv = Counter()
            for k, n in rd["levels"].items():
                lv[_OLD_LEVELS.get(str(k).upper(), str(k).upper())] += int(n or 0)
            rd["levels"] = dict(lv)
        if rd.get("dongles_checked"):
            rd["dongles_checked"] = [now_name(n) for n in rd["dongles_checked"]]
        if not sers:
            rd["names_then"] = True
        out.append(rd)
    return out


def _ago(t) -> str:
    """'5 min ago', '3 h ago', '2 days ago'."""
    s = max(0.0, time.time() - float(t))
    return (f"{int(s // 60)} min ago" if s < 5400 else f"{int(round(s / 3600))} h ago" if s < 36 * 3600
            else f"{int(round(s / 86400))} days ago")


@register("patrol_status", "Show the background patrol state and recently flagged transmitters.", {})
def patrol_status():
    out = _PATROL.summary() if _PATROL else {"running": False}
    out.pop("recent_runs", None)
    rounds = _patrol_rounds(20)
    day = [r for r in rounds if time.time() - r["t"] <= 86400]
    out["recent_flagged_rounds"] = [{k: v for k, v in r.items() if k not in ("t", "serials")}
                                    for r in day[-5:]]
    for r, src in zip(out["recent_flagged_rounds"], day[-5:]):
        r["age"] = _ago(src["t"])
    older = [r for r in rounds if time.time() - r["t"] > 86400]
    if older:
        out["older_flagged_rounds"] = {"count": len(older), "last": older[-1]["time"], "age": _ago(older[-1]["t"]),
                                       "note": "older than a day: not listed"}
    return out


def _patrol_reply(rep) -> str:
    """'patrol status' answered without the AI round. The live station's AI (v4.8.6) pasted two-day-old rounds
    with raw fields and old levels for 5 minutes, was cut off mid-word, and never said patrol was off."""
    if rep.get("running"):
        lines = [f"Patrol is running on {', '.join(rep.get('bands') or [])} with {', '.join(rep.get('dongles') or [])}"
                 f" every {rep.get('interval_min')} min: {rep.get('audits_done', 0)} band checks done, "
                 f"{rep.get('events_flagged_total', 0)} event(s) flagged."]
    else:
        lines = ["Patrol is off (start it with /patrol start)."]
    rounds = rep.get("recent_flagged_rounds") or []
    if rounds:
        lines.append("Flagged in the last day:")
    for r in rounds[::-1]:
        evs = r.get("top") or []
        what = "; ".join(
            (f"frequency hopping around {float(ev.get('peak_mhz') or 0):.3f} MHz" if ev.get("pattern") ==
             "frequency hopping" else f"{float(ev.get('peak_mhz') or 0):.3f} MHz")
            + f" {ev.get('threat_level', 'LOW')}" + (f" ({', '.join((ev.get('why') or [])[:2])})" if ev.get("why")
                                                     else "") for ev in evs[:3])
        lines.append(f"- {r.get('time')} ({r.get('age')}), {r.get('band')}: {what or 'nothing listed'}.")
    old = rep.get("older_flagged_rounds")
    if old:
        lines.append(f"{'Older' if rounds else 'Nothing flagged in the last day. Older'} rounds: {old['count']}, the "
                     f"last on {old['last']} ({old['age']}); they are on the Activity tab.")
    elif not rounds:
        lines.append("Nothing flagged yet.")
    return "\n".join(lines)
