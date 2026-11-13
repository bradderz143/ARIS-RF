"""ARIS engine, part "investigate": Saved signals (SQLite), the investigation flow (every dongle at once, decode on the best),
stopping (job_scope, Reset Scanner), and the sweep / investigate / band-scan tools.

Executed into aris.py's namespace by aris._include (see aris.py): names from aris.py's imports and from the
parts before this one are available here without imports, and everything defined here becomes aris.<name>.
"""
from __future__ import annotations

# ============================================================================
# SAVED SIGNALS (SQLite): heuristic fingerprint + log of every investigated signal
# (formerly aris_urh.py). The "candidate ID" fields are guesses made by slicing the first bytes of a
# captured frame; random noise produces them too. Only trust them when a real decoder confirmed it.
# ============================================================================
_DB_LOCK = threading.Lock()
PROTOCOL_SIGNATURES = [
    (433.0, 434.79, "FSK", "433 MHz FSK telemetry sensor (weather station / tyre pressure / smart home)"),
    (433.0, 434.79, "OOK", "433 MHz OOK remote / door-window contact / keyfob"),
    (868.0, 870.0, "FSK", "868 MHz Wireless M-Bus / smart meter / security sensor"),
    (870.0, 895.0, "ANY", "Cellular base station downlink / trunked radio carrier"),
    (902.0, 928.0, "FSK", "915 MHz ISM smart utility meter (ERT/AMR) or LoRa/industrial sensor"),
    (902.0, 928.0, "OOK", "915 MHz OOK remote or simple sensor"),
    (928.0, 932.0, "ANY", "929-931 MHz commercial pager downlink (FLEX / POCSAG)"),
]
KNOWN_SYNC_HEADERS = {
    "2DD4": "HopeRF RFM69 / SX1231 / CC1101 transceiver (0x2DD4)",
    "D391": "TI CC1101 / CC2500 sub-GHz transceiver (0xD391)",
    "F68D": "Wireless M-Bus utility meter frame (0xF68D)",
}


_DB_READY: set = set()


def init_db():
    if DB_PATH in _DB_READY and os.path.exists(DB_PATH):
        return
    if not os.path.exists(DB_PATH):
        for old in OLD_DB_PATHS:
            if os.path.exists(old):
                try:
                    shutil.copy2(old, DB_PATH)            # carry the old Saved Signals list over once
                except OSError:
                    pass
                break
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS peaks (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, "
                     "frequency_mhz REAL, power_db REAL, snr_db REAL, bw_khz REAL, ook_hex TEXT, fsk_hex TEXT, "
                     "multimon_frames TEXT, urh_analysis TEXT, notes TEXT)")
        # rows from before v4.9.4 carried the demodulated bursts as hex (a remote's code): wiped once, for good
        conn.execute("UPDATE peaks SET ook_hex = NULL, fsk_hex = NULL WHERE ook_hex IS NOT NULL OR fsk_hex IS NOT NULL")
        for rid, card in conn.execute("SELECT id, urh_analysis FROM peaks WHERE urh_analysis LIKE '%Candidate ID%'").fetchall():
            kept = [ln for ln in str(card or "").split("\n")
                    if not ln.startswith(("• Candidate ID", "• Frame checksum"))]
            conn.execute("UPDATE peaks SET urh_analysis = ? WHERE id = ?", ("\n".join(kept), rid))
        conn.commit()
    finally:
        conn.close()
    _DB_READY.add(DB_PATH)


def _fingerprint(freq, mod_type, sync_word=None, allocation=""):
    """Band-plan device guess plus, in the sensor bands only, a radio-chip sync-word match. The demodulated bits
    never reach this (v4.9.4): before, the card showed a 'Candidate ID' from the first bytes of the burst, which
    for a fixed-code remote is the code."""
    mod_type = mod_type or "CARRIER"
    if not allocation and _in_fm_band(freq):
        allocation = "FM broadcast band (88-108 MHz)"
    device_class = allocation or f"Unclassified {mod_type} transmitter at {freq:.4f} MHz"
    for f_low, f_high, m_req, desc in PROTOCOL_SIGNATURES:
        if f_low <= freq <= f_high and (m_req == "ANY" or m_req == mod_type):
            device_class = desc
            break
    lines = [f"• Probable device type (band-plan guess): {device_class}"]
    if _in_sensor_band(freq) and sync_word:
        lines.append(f"• Radio chip fingerprint (sync-word match): {sync_word}")
    return "\n".join(lines)


def _frame_summary(out):
    """(modulation guess, sync-word match) from the stage-1 bit slicer: lengths and a chip match, never the bits."""
    s1 = out.get("stage1_iq_physical_and_bitstream") or {}
    nb = s1.get("native_bitstream_extractor") or {}
    return nb.get("modulation_guess"), nb.get("sync_word_match")


def _log_signal(peak_info, out, serial):
    """Plain-English card for the Saved Signals list; saves it unless nothing transmitted or it is a confirmed
    public FM broadcast station."""
    freq = float(out.get("frequency_mhz") or peak_info.get("frequency_mhz") or 0.0)
    mod_type, sync_word = _frame_summary(out)
    verdict, evidence = out.get("final_signal_verdict"), out.get("evidence")
    text = _fingerprint(freq, mod_type, sync_word, str(peak_info.get("probable_allocation") or ""))
    kind = "confirmed decode" if evidence == "confirmed_decode" else "heuristic"
    text = f"• ARIS verdict ({kind}): {verdict}\n" + text
    s2 = out.get("stage2_rtl433_telemetry") or {}
    if s2.get("identified_models"):
        text += "\n• rtl_433 confirmed decode: " + ", ".join(s2["identified_models"])
    for fp in (s2.get("unknown_signal_fingerprints") or [])[:1]:
        if fp.get("pattern_hint"):
            text += f"\n• Burst pattern ({fp['modulation']}, {fp['pattern']}): usually {fp['pattern_hint']}"
    if out.get("dongle_comparison"):
        heard = [r["dongle"] for r in out["dongle_comparison"] if r.get("heard")]
        if heard:
            text += f"\n• Heard on: {', '.join(heard)} (strongest: {out.get('strongest_dongle')})"
    res = {"device_identification_and_decode": text}
    if evidence == "none":
        res["note"] = ("Not saved to the signal list (no checksum-verified aircraft message)."
                       if out.get("band_type") == "adsb" and (out.get("adsb") or {}).get("messages")
                       else "Not saved to the signal list (nothing transmitted during the check).")
        return res
    if out.get("save_to_signal_list") is False:
        res["note"] = "Not saved to the signal list (confirmed public FM broadcast station)."
        return res
    s3 = out.get("stage3_multimon_direwolf_audio") or {}
    mm = (s3.get("validated_frames") or []) + (s3.get("aprs_frames") or [])
    try:
        with _DB_LOCK:
            init_db()
            conn = sqlite3.connect(DB_PATH, timeout=10)
            try:
                conn.execute("INSERT INTO peaks (timestamp, frequency_mhz, power_db, snr_db, bw_khz, ook_hex, fsk_hex, "
                             "multimon_frames, urh_analysis, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (_now(), freq, float(peak_info.get("peak_db") or 0.0), float(out.get("snr_db") or 0.0),
                              float(peak_info.get("occupied_bw_khz") or 0.0), None, None,     # (no bits, v4.9.4)
                              json.dumps(mm)[:3000], text, f"{_short(serial)} | {verdict}"))
                # the one file in the data folder that grew for ever (patrol investigates HIGH findings every hour)
                conn.execute("DELETE FROM peaks WHERE id NOT IN (SELECT id FROM peaks ORDER BY id DESC LIMIT ?)",
                             (SIGNALS_KEEP,))
                conn.commit()
            finally:
                conn.close()
        res["saved_to_signal_list"] = True
    except Exception as e:                       # logging must never break an investigation
        log.warning("Signals database not written: %s: %s", type(e).__name__, e)
        res["db_error"] = f"{type(e).__name__}: {e}"
    return res


_BITS_KEYS = ("native_bitstream_extractor", "ook_ask_binary_prefix", "ook_ask_hex_payload", "fsk_hex_payload")


def _scrub_last_report() -> bool:
    """last_investigation.json written before v4.9.4 holds the demodulated bursts as hex (a remote's code) until
    the next investigation overwrites it: dropped at start (v4.9.5 review). True when it was rewritten."""
    try:
        text = LAST_REPORT.read_text()
    except OSError:
        return False
    if not any(k in text for k in _BITS_KEYS):
        return False

    def scrub(o):
        if isinstance(o, dict):
            return {k: scrub(v) for k, v in o.items() if k not in _BITS_KEYS}
        if isinstance(o, list):
            return [scrub(x) for x in o]
        return o
    try:
        _atomic_write_json(LAST_REPORT, scrub(json.loads(text)))
    except (ValueError, OSError):
        return False
    return True


def upgrade_signal_db():
    """Rows saved by versions before 4.2.3 called FM-band signals sensors or remotes; relabel those."""
    init_db()
    _scrub_last_report()
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        rows = conn.execute("SELECT id, frequency_mhz, urh_analysis, notes FROM peaks").fetchall()
        for row_id, freq, old, notes in rows:
            f = float(freq or 0.0)
            head = [ln for ln in (old or "").splitlines() if ln.startswith("• ARIS verdict")]
            if _in_fm_band(f) and not any(w in " ".join(head) for w in ("FM broadcast", "FM transmit", "FM band",
                                                                          "relabelled")):
                label = (f"signal in the FM broadcast band near {f:.1f} MHz, most likely a radio station. Earlier "
                         f"versions wrongly called it a sensor; scan again to confirm")
                conn.execute("UPDATE peaks SET urh_analysis=?, notes=? WHERE id=?",
                             (f"• ARIS verdict (relabelled): {label}\n• Probable device type (band-plan guess): "
                              f"FM broadcast band (88-108 MHz)",
                              str(notes or "").split(" | ", 1)[0] + f" | FM band signal near {f:.1f} MHz (relabelled)",
                              row_id))
        conn.commit()
    finally:
        conn.close()
    _relabel_named_signals(_known_stations())


# ============================================================================
# INVESTIGATION: listen on every dongle at once, decode on the one that hears it best
# ============================================================================
_RECENT_LOCK = threading.Lock()
_QUIET = threading.local()          # background (patrol) investigations print nothing


def _say(msg) -> None:
    """Progress line: printed on the console, unless this is a quiet background job; a GUI job collects it."""
    sink = getattr(_QUIET, "sink", None)
    if sink is not None:
        try:
            sink(str(msg).strip())
        except Exception:
            pass
    if not getattr(_QUIET, "on", False):
        print(msg)


def _carry_ctx(fn):
    """Worker threads report progress like the thread that started them (a quiet patrol job stays quiet, a GUI
    job's progress goes to its own sink)."""
    on, sink, stop = getattr(_QUIET, "on", False), getattr(_QUIET, "sink", None), getattr(_QUIET, "stop", None)

    def run(*a, **k):
        _QUIET.on, _QUIET.sink, _QUIET.stop = on, sink, stop
        try:
            return fn(*a, **k)
        finally:
            _QUIET.on, _QUIET.sink, _QUIET.stop = False, None, None
    return run


# ----------------------------------------------------------------------------------------------------------------
# STOPPING: Reset Scanner ends every job in this process. A job runs in job_scope(); it and the worker threads it
# starts (_carry_ctx) check the flag at every dongle use and between steps, and the radio programs it started are
# ended, so it stops within a few seconds and its dongles are released.
# ----------------------------------------------------------------------------------------------------------------
_STOPS: set = set()
_STOPS_LOCK = threading.Lock()
RADIO_TOOLS = ("rtl_sdr", "rtl_power", "rtl_fm", "rtl_433", "rtl_test", "rtl_adsb", "rtl_tcp", "rx_sdr", "rx_power",
               "rx_fm", "aris-iqpipe", "aris-bb60", "multimon-ng",
               "direwolf", "atest")


class Stopped(RFError):
    """Reset Scanner stopped this job."""


def _stop_requested() -> bool:
    ev = getattr(_QUIET, "stop", None)
    return bool(ev is not None and ev.is_set())


def _check_stop() -> None:
    ev = getattr(_QUIET, "stop", None)
    if ev is not None and ev.is_set():
        raise Stopped("Stopped by Reset Scanner.")


@contextmanager
def job_scope():
    """Run a job (a GUI Decode, Name all) so that Reset Scanner can stop it."""
    ev, prev = threading.Event(), getattr(_QUIET, "stop", None)
    _QUIET.stop = ev
    with _STOPS_LOCK:
        _STOPS.add(ev)
    try:
        yield ev
    finally:
        with _STOPS_LOCK:
            _STOPS.discard(ev)
        _QUIET.stop = prev


def _radio_children(root=None) -> list:
    """Radio programs (rtl_*, decoders) started by this process or its children (the chat console): [(pid, name)]."""
    root = os.getpid() if root is None else int(root)
    parent, name = {}, {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as f:
                st = f.read()
        except OSError:
            continue
        r = st.rfind(")")
        if r < 0:
            continue
        name[int(d)] = st[st.find("(") + 1:r]
        try:
            parent[int(d)] = int(st[r + 2:].split()[1])
        except (IndexError, ValueError):
            continue
    out = []
    for pid in parent:
        p, seen = parent.get(pid), 0
        while p and p != root and seen < 64:
            p, seen = parent.get(p), seen + 1
        if p == root and name.get(pid) in RADIO_TOOLS:
            out.append((pid, name[pid]))
    return out


def _end_radio_children() -> int:
    """End the radio programs this process started (SIGTERM, then SIGKILL after a second) and wait until they have
    exited: one whose stop hangs (librtlsdr's cancel) keeps its dongle for as long as it lives. Returns how many."""
    kids = _radio_children()
    for pid, _n in kids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    left = radioproc.wait_gone([pid for pid, _n in kids], 1.0)
    for pid in left:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    stuck = radioproc.wait_gone(left, radioproc.KILL_WAIT_S)
    if left:
        log.info("reset: %d radio program(s) did not exit on SIGTERM: killed", len(left))
    if stuck:
        log.warning("reset: radio program(s) %s still running after SIGKILL", stuck)
    return len(kids)


def clear_data(baselines: bool = False) -> dict:
    """Clear data (the chat's quick action, after Reset Scanner has stopped everything): every log and result ARIS
    has kept. Settings (gains, frequency corrections, dongle order), logins, the Map's antennas and floorplans, the
    setup report and backups stay; the baselines only go when asked. Returns what was cleared."""
    from arislib import config as _acfg
    if _running_watchdogs():
        raise RFError("A capture is still running: press Reset Scanner first.")
    out = {}

    def wipe_db(path):
        if not os.path.exists(path):
            return 0
        n, conn = 0, sqlite3.connect(path, timeout=15)
        try:
            for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
                n += conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                conn.execute(f'DELETE FROM "{t}"')
            conn.commit()
            conn.execute("VACUUM")
        finally:
            conn.close()
        return n

    with _DB_LOCK:
        out["signals"] = wipe_db(DB_PATH)
    out["locate_readings"] = wipe_db(str(_acfg.LOCATE_DB))
    lines = 0
    for f in (WATCHDOG_LOG, NOTICES, PATROL_LOG, AUDIT_HISTORY):
        try:
            with open(f, "rb") as fh:
                lines += sum(1 for _ in fh)
        except OSError:
            pass
    out["log_entries"] = lines
    try:
        with open(CAPTURES) as fh:
            out["captures"] = len(json.load(fh))
    except (OSError, ValueError, TypeError):
        out["captures"] = 0
    out["fm_stations"] = len(_known_stations())
    with _DEV_LOCK:                   # the device history goes too (Clear logs keeps it; see KEPT_BY_CLEAR_LOGS)
        _DEV_PENDING[:] = []
        _DEV_CACHE.update(tag=None, rows=None)
        store = _devices_read()
        out["devices"] = len((store or {}).get("devices") or {})
    for f in (WATCHDOG_LOG, NOTICES, PATROL_LOG, PATROL_SEEN, PATROL_STATE, AUDIT_HISTORY, AUDIT_REPORT, LAST_REPORT,
              RECENT_CHECKS, CAPTURES, FM_STATIONS, WATCHDOG_RESUME, DEVICES_FILE, DATA_DIR / "check_report.json"):
        try:
            Path(f).unlink()
        except OSError:
            pass
    try:
        open(LOG_FILE, "w").close()
    except OSError:
        pass
    _STATIONS_CACHE.update(key=None, data={})
    out["baselines"] = 0
    if baselines and BASELINE_DIR.exists():
        for f in BASELINE_DIR.glob("*.json"):
            try:
                f.unlink()
                out["baselines"] += 1
            except OSError:
                pass
        _BASELINE_CACHE.clear()
    _DISK_CACHE["t"] = 0.0
    return out


# Clear logs (the Activity tab's button) never touches these: the keep-list, written out so nobody has to guess.
KEPT_BY_CLEAR_LOGS = ("signals.db", "locate.db", "fm_stations.json", "devices.json", "config.json", "baselines/",
                      "gui_secrets.json", "node_secrets.json", "label_secret", "nodes.json", "floorplans/",
                      "setup_report.json",
                      "backups/", "last_investigation.json", "watchdogs.json", "watchdogs_resume.json",
                      "patrol_state.json", "patrol_seen.json", "update_check.json")


def _log_files() -> list:
    """Every file Clear logs removes (those that exist): the event logs with any rotated copies, the result lists, the
    chat notices, the audit and check reports and the text log with its rotations. Nothing else."""
    out = []
    for p in (WATCHDOG_LOG, PATROL_LOG, AUDIT_HISTORY, AUDIT_REPORT, CAPTURES, NOTICES, RECENT_CHECKS,
              DATA_DIR / "check_report.json", LOG_FILE):
        out.append(p)
        out.extend(sorted(p.parent.glob(p.name + ".*")))      # aris.log.1, ism_events.jsonl.bak, captures.json.tmp
    out.extend(sorted(DATA_DIR.glob(".trim_*")))              # a trim that was interrupted
    seen, uniq = set(), []
    for p in out:
        if str(p) not in seen and p.is_file():
            seen.add(str(p))
            uniq.append(p)
    return uniq


def clear_logs() -> dict:
    """Clear logs (the Activity tab's button): the event logs and result lists go, so the data folder never fills the
    disk. Kept: the Signals list, the FM station names, the device history, settings, baselines, logins, the Map's
    antennas and floorplans, the setup report and backups (KEPT_BY_CLEAR_LOGS). A running capture keeps running and
    writes to a fresh log; its entry in watchdogs.json stays. aris.log is emptied, not deleted: the logging handler
    (this process's or the chat console's) keeps writing to it."""
    global _log_line_count
    deleted, freed = [], 0
    with _LOG_LOCK, _WD_STATE_LOCK:                   # (a capture ending right now saves itself after, not during)
        for p in _log_files():
            try:
                size = p.stat().st_size
                if p == LOG_FILE:
                    open(p, "w").close()                      # truncated: an open handler appends after it
                else:
                    p.unlink()
                deleted.append(p.name)
                freed += size
            except OSError:
                pass
        _log_line_count = 0
        _LINE_COUNTS.clear()
    _DISK_CACHE["t"] = 0.0
    running = _running_watchdogs()
    freed_mb = round(freed / 1e6, 2)
    msg = ("Logs cleared" + (f": {len(deleted)} file{'s' if len(deleted) != 1 else ''}, {_size_text(freed)} freed."
                            if deleted else ": there was nothing to clear.")
           + " Kept: the Signals list, FM station names, device history, baselines, settings, logins and the Map.")
    if running:
        msg += (f" {len(running)} capture{'s' if len(running) != 1 else ''} still running: "
                + ", ".join(_capture_doing(w) for w in running)
                + f"; what {'they find' if len(running) != 1 else 'it finds'} from now on goes into the fresh logs.")
    return {"message": msg, "deleted": deleted, "freed_mb": freed_mb, "captures_running": len(running)}


_DISK_CACHE = {"t": 0.0, "data": None}


def _size_text(n: float) -> str:
    """Bytes in plain words: '12 kB', '3.4 MB', '1.2 GB'."""
    if n >= 1e9:
        return f"{n / 1e9:.1f} GB"
    if n >= 1e6:
        return f"{n / 1e6:.1f} MB"
    return f"{n / 1e3:.0f} kB"


def disk_usage(fresh: bool = False) -> dict:
    """What the data folder holds, by kind, and the free space on its disk (the Activity tab's line): logs = what
    Clear logs removes, signals = signals.db and locate.db, baselines, other = everything else (settings, FM station
    names, device history, floorplans, backups). Cached DISK_CACHE_S: the page polls, and only the few files in
    DATA_DIR are read (a stat each, no big trees)."""
    now = time.time()
    if not fresh and _DISK_CACHE["data"] and now - _DISK_CACHE["t"] < DISK_CACHE_S:
        return dict(_DISK_CACHE["data"])
    from arislib import config as _acfg
    logs = {str(p) for p in _log_files()}
    dbs = set()
    for base in (DB_PATH, str(_acfg.LOCATE_DB)):
        for suffix in ("", "-wal", "-shm", "-journal"):
            dbs.add(base + suffix)
    sizes = {"logs": 0, "signals": 0, "baselines": 0, "other": 0}
    counted = set()

    def add(path: Path):
        s = str(path)
        if s in counted:
            return
        try:
            n = path.stat().st_size
        except OSError:
            return
        counted.add(s)
        kind = ("logs" if s in logs else "signals" if s in dbs
                else "baselines" if BASELINE_DIR in path.parents else "other")
        sizes[kind] += n

    def walk(folder: Path, depth: int):
        try:
            entries = list(os.scandir(str(folder)))
        except OSError:
            return
        for e in entries:
            try:
                if e.is_file(follow_symlinks=False):
                    add(Path(e.path))
                elif e.is_dir(follow_symlinks=False) and depth < 3:
                    walk(Path(e.path), depth + 1)
            except OSError:
                pass
    walk(DATA_DIR, 0)
    for s in list(logs) + list(dbs):                          # files kept outside the data folder (env settings)
        add(Path(s))
    try:
        free = shutil.disk_usage(str(DATA_DIR)).free
    except OSError:
        free = 0
    mb = {k: round(v / 1e6, 2) for k, v in sizes.items()}
    data = {"logs_mb": mb["logs"], "signals_mb": mb["signals"], "baselines_mb": mb["baselines"],
            "other_mb": mb["other"], "total_mb": round(sum(sizes.values()) / 1e6, 2),
            "free_gb": round(free / 1e9, 2), "data_dir": str(DATA_DIR),
            "line": (f"On disk: logs {_size_text(sizes['logs'])}, signals {_size_text(sizes['signals'])}, baselines "
                     f"{_size_text(sizes['baselines'])}, other {_size_text(sizes['other'])}; {_size_text(free)} free")}
    _DISK_CACHE.update(t=now, data=data)
    return dict(data)


def stop_everything(wait_s: float = 10.0) -> dict:
    """Reset Scanner: stop every job in this process (Decode, Name all), every capture (in any ARIS process) and
    patrol, end the radio programs this process started, then check which dongles are free. A dongle that a thread
    of this process still holds after wait_s can only be freed by restarting the process (`stuck_here`)."""
    with _STOPS_LOCK:
        evs = list(_STOPS)
    for ev in evs:
        ev.set()
    stopped = []
    caps = _running_watchdogs()
    if caps:
        try:
            stop_background_watchdog()
        except RFError as e:
            log.warning("reset: stopping captures: %s", e)
        stopped += [_capture_name(w) for w in caps]
    if _PATROL and _PATROL.alive():
        _PATROL.stop()
        stopped.append("patrol")
    ended = _end_radio_children()
    _write_watchdog_state()                          # nothing stale left for the chat console to trip over
    end = time.time() + max(0.0, wait_s)
    while time.time() < end:
        with _STOPS_LOCK:
            if not any(ev in _STOPS for ev in evs):
                break
        time.sleep(0.2)
    for f in STOP_DIR.glob("*") if STOP_DIR.exists() else []:
        if not _watchdog_elsewhere(f.name):          # a stop request nobody is left to read
            try:
                f.unlink()
            except OSError:
                pass
    cfg = _load_config()
    free, busy, stuck, missing = [], [], False, []
    plugged = {str(d.get("serial")) for d in _list_devices()}    # lists them without opening any (rtl_test -d 99);
    for s in list(DONGLE_NAMES):                                  # v4.8.5 called an unplugged dongle free
        d = _dongle_cfg(s, cfg)
        if d.get("present") is False or (plugged and s not in plugged):
            missing.append(_short(s))
            continue
        lk = _device_lock(s)
        if lk.acquire(1.5, "Reset Scanner check"):
            lk.release()
            free.append(_short(s))
        else:
            here = lk.locked()                       # a thread of this process holds it
            stuck = stuck or here
            busy.append({"dongle": _short(s), "serial": s, "held_by": lk.holder or "another ARIS window",
                         "in_this_process": here})
    with _STOPS_LOCK:
        still = sum(1 for ev in evs if ev in _STOPS)
    return {"jobs_stopped": len(evs), "jobs_still_running": still, "stopped": stopped, "radio_programs_ended": ended,
            "free": free, "busy": busy, "stuck_here": stuck, "missing": missing}


def _earlier_checks(freq, within_mhz=0.06, hours=6.0):
    try:
        with open(RECENT_CHECKS) as f:
            items = json.load(f)
    except (OSError, ValueError):
        return []
    now = time.time()
    near = [{k: v for k, v in x.items() if k != "epoch"} for x in items
            if abs(float(x.get("frequency_mhz") or 0) - freq) <= within_mhz
            and now - float(x.get("epoch") or 0) <= hours * 3600]
    return near[-3:][::-1]


def _cut_at_word(text, n) -> str:
    """text cut to about n characters at a word, marked with '...' when cut (v4.8.6 cut mid-word: '2-bi')."""
    t = str(text or "")
    if len(t) <= n:
        return t
    head = t[:n] if t[n] in " ,;:." else t[:n].rsplit(" ", 1)[0]      # (a whole last word stays)
    return head.rstrip(",;:( ") + " ..."


def _remember_check(out):
    rec = {"time": _now(), "epoch": time.time(), "frequency_mhz": out.get("frequency_mhz"),
           "evidence": out.get("evidence"),
           "verdict": _cut_at_word(out.get("verdict_core") or out.get("final_signal_verdict"), 300),
           "strongest_dongle": out.get("strongest_dongle"), "measured_carrier_mhz": out.get("measured_carrier_mhz")}
    with _RECENT_LOCK:
        try:
            with open(RECENT_CHECKS) as f:
                items = json.load(f)
        except (OSError, ValueError):
            items = []
        try:
            _atomic_write_json(RECENT_CHECKS, (items + [rec])[-60:])
        except OSError:
            pass


def _wide_verdict(s1, target, peak_info):
    """FM broadcast band / wide signals. Returns (verdict, evidence, note, station_confirmed)."""
    fm = _in_fm_band(target)
    present = bool(s1.get("signal_present_during_capture"))
    wbfm, marks = bool(s1.get("wideband_fm")), bool(s1.get("stereo_pilot_19khz") or s1.get("rds_57khz"))
    on_plan = bool(s1.get("on_standard_fm_channel"))
    meas = s1.get("measured_carrier_mhz") or target
    name, pi = s1.get("rds_station_name"), s1.get("rds_pi_code")
    others = [n for n in (s1.get("rds_station_names") or []) if n != name][:2]
    if fm and wbfm and name:
        what = "RDS scrolling text" if s1.get("rds_name_scrolls") else "RDS station name"
        return (f"Identified: FM broadcast station on {round(meas, 1):.1f} MHz, {what} \"{name}\" "
                + (f"(it also shows {', '.join(chr(34) + n + chr(34) for n in others)}) " if others else "")
                + f"(PI code {pi}). It sends its own name, so this is a public radio station"
                + ("" if on_plan else f", although {s1.get('offset_from_channel_khz')} kHz off its channel"),
                "confirmed_decode", "station name decoded from its RDS data (checksum-verified)", True)
    if fm and wbfm and on_plan and marks:
        found = " and ".join(x for x, ok in (("stereo pilot", s1.get("stereo_pilot_19khz")),
                                              ("RDS", s1.get("rds_57khz"))) if ok)
        return (f"FM broadcast radio station on {round(meas, 1):.1f} MHz ({found} present; public broadcast)",
                "signal_present", "standard channel plus broadcast fingerprints (stereo pilot / RDS)", True)
    if fm and wbfm and on_plan:
        return (f"FM transmission on station channel {round(meas, 1):.1f} MHz but mono, with no stereo pilot or RDS: "
                f"probably a mono station; check it against your FM baseline", "signal_present",
                "looks like broadcast FM but lacks the usual broadcast fingerprints", False)
    if fm and wbfm:
        return (f"FM transmitter at {meas:.3f} MHz, OFF the broadcast channel plan ({_between_channels(meas)}). "
                f"Not a normal radio station: could be a car FM transmitter, wireless microphone, baby monitor or "
                f"an FM bug. Worth locating.", "signal_present", "not on a standard FM channel", False)
    snr = s1.get("channel_snr_db")
    if fm and present and on_plan and snr is not None and snr < 15.0:
        return (f"Weak signal (+{snr:.0f} dB) on station channel {round(meas, 1):.1f} MHz: too weak to check its "
                f"waveform or read an RDS name, so not identified. Most likely a distant FM station; Decode for longer "
                f"or on the dongle with the better FM antenna to be sure", "signal_present",
                "too weak to check", False)
    if fm and present:
        return (f"Signal in the FM band at {meas:.3f} MHz that is not standard broadcast FM; worth a closer look",
                "signal_present", "unusual waveform for this band", False)
    if present:
        return (f"Wideband signal about {s1.get('occupied_bw_khz')} kHz wide "
                f"(band: {peak_info.get('probable_allocation', 'unclassified')})", "signal_present",
                "heuristic guess, not a confirmed identification", False)
    return "Nothing transmitting during the check", "none", "heuristic guess, not a confirmed identification", False


# A packet's reception fields: what differs between two dongles' copies of the same transmission.
_RECEPTION_KEYS = {"t", "time", "aris_serial", "aris_heard_by", "snr", "rssi", "noise", "freq", "freq1", "freq2",
                   "mod"}


def _history_once(per_dongle: dict) -> list:
    """The packets an investigation decoded, once each, for the device history. The dongles ran at the same time,
    so (v4.8.8 before this) every transmission reached the history once per dongle: 2-3 x the packets, and the
    second dongle's copy, a second earlier by rtl_433's clock, counted as a new transmission. Copies with the same
    content from several dongles are one: the dongle that heard most of them gives the count, and the packet says
    which dongles heard it (aris_heard_by)."""
    groups = {}
    for ser, packets in per_dongle.items():
        for p in packets:
            key = json.dumps({k: v for k, v in p.items() if k not in _RECEPTION_KEYS}, sort_keys=True, default=str)
            groups.setdefault(key, {}).setdefault(ser, []).append(p)
    out = []
    for g in groups.values():
        most = max(g.values(), key=lambda ps: (len(ps), max(float(p.get("snr") or -99) for p in ps)))
        heard = [_short(s) for s in g]
        out.extend({**p, "aris_heard_by": heard} for p in most)
    return sorted(out, key=lambda p: float(p.get("t") or 0))


def _merge_rtl433(results, table):
    """rtl_433 ran on several dongles at once: combine decodes and fingerprints, note who heard what, and feed the
    device history once per packet."""
    m = {"packets_decoded_count": 0, "identified_models": set(), "decoded_packets": [], "bursts_heard": 0,
         "unknown_signal_fingerprints": [], "status": []}
    devs, fpm, failed, hist = {}, {}, {}, {}
    best, best_snr = None, None
    for ser, r in results.items():
        row = table.setdefault(ser, {"dongle": _short(ser)})
        if isinstance(r, Exception):
            row["rtl_433"] = f"not run: {str(r)[:100]}"
            failed[_short(ser)] = str(r)[:120]
            continue
        if r.get("history"):
            hist[ser] = r["history"]
        if r.get("error"):
            row["rtl_433"] = f"could not run: {r['error']}"
            failed[_short(ser)] = r["error"]
        n, fps = r.get("packets_decoded_count", 0), r.get("unknown_signal_fingerprints") or []
        snr = r.get("best_packet_snr_db") if n else max((f["snr_db"] for f in fps if f.get("snr_db") is not None),
                                                         default=None)
        row["rtl_433_packets"], row["rtl_433_bursts"] = n, r.get("bursts_heard", 0)
        if snr is not None:
            row["rtl_433_snr_db"] = snr
        if n or r.get("bursts_heard"):
            if not row.get("heard"):
                row["heard_by_rtl433"] = True     # the short look missed it: rtl_433's level goes with it
            row["heard"] = True
            if snr is not None and (best_snr is None or snr > best_snr):
                best, best_snr = ser, snr
            elif best is None:
                best = ser
        m["packets_decoded_count"] += n
        m["identified_models"] |= set(r.get("identified_models") or [])
        for d in r.get("devices") or []:
            key = (d["model"], d["id_label"], str(d["channel"]))
            g = devs.get(key)
            if g is None:
                g = devs[key] = {**d, "packets": 0, "heard_by": []}
            g["packets"] += d["packets"]
            if d.get("best_snr_db") is not None:
                g["best_snr_db"] = max(g.get("best_snr_db") or -99.0, d["best_snr_db"])
            g["heard_by"].append(_short(ser))
        m["decoded_packets"] += [{**p, "heard_by": _short(ser)} for p in r.get("decoded_packets") or []]
        for f in fps:
            k = (f["modulation"], f.get("coding") or f.get("pattern"), round((f.get("short_pulse_us") or 0) / 50.0),
                 round((f.get("long_pulse_us") or 0) / 50.0))
            g = fpm.get(k)
            if g is None:
                g = fpm[k] = {**f, "heard_by": []}
            else:
                g["bursts"] += f.get("bursts", 0)
                if (f.get("snr_db") or -99) > (g.get("snr_db") or -99):
                    g.update({x: f.get(x) for x in ("snr_db", "offset_khz", "bits_per_packet", "packets_per_burst")})
            g["heard_by"].append(_short(ser))
        m["bursts_heard"] += r.get("bursts_heard", 0)
        m["status"].append(f"{_short(ser)}: {r.get('status')}")
    m["identified_models"] = sorted(m["identified_models"])
    m["devices"] = sorted(devs.values(), key=lambda d: (-d["packets"], -(d.get("best_snr_db") or 0)))
    m["decoded_packets"] = sorted(m["decoded_packets"], key=lambda p: -float(p.get("snr") or 0))[:4]
    m["unknown_signal_fingerprints"] = sorted(fpm.values(), key=lambda f: (not f.get("demodulated"),
                                                                            not _clear_pattern(f),
                                                                            -(f.get("snr_db") or 0)))[:3]
    m["status"] = "; ".join(m["status"])
    if not m["unknown_signal_fingerprints"]:
        m.pop("unknown_signal_fingerprints")
    if failed:
        m["failed_on"] = failed
        m["all_failed"] = len(failed) == len(results)
    for p in _history_once(hist):
        _devices_note(p)
    return m, best


def _antenna_note(serials, heard_on, table):
    if len(serials) < 2:
        return None
    names = lambda ss: _and_list(_short(s) for s in ss)
    every = "both" if len(serials) == 2 else "all"
    if not heard_on:
        return f"No dongle heard it ({names(serials)} {every} listened)."
    if len(heard_on) == len(serials):
        note = "Heard on both dongles." if len(serials) == 2 else f"Heard on all {len(serials)} dongles."
    else:
        miss = [s for s in serials if s not in heard_on]
        antennas = "the antennas on" if len(heard_on) > 1 else "the antenna on"
        note = (f"Heard on {names(heard_on)}, not on {names(miss)}. The dongles have different antennas, so this "
                f"usually means {antennas} {names(heard_on)} suit{'' if len(heard_on) > 1 else 's'} this band "
                f"better; it does not mean the signal is internal.")
    carriers = [table[s].get("carrier_mhz") for s in heard_on if table[s].get("carrier_mhz")]
    if len(carriers) >= 2 and (max(carriers) - min(carriers)) * 1000.0 <= 3.0:
        note += " They measured the same frequency, so it is a real over-the-air signal."
    return note


_STAGE_FAIL = ("IQ capture failed", "IQ analysis error", "Wideband IQ analysis error")


def _stage_failed(r) -> bool:
    """A stage-1 look that did not work (the dongle could not capture, or the analysis broke)."""
    return not isinstance(r, dict) or str(r.get("status", "")).startswith(_STAGE_FAIL)


def _best_look(runs, kind, span):
    """Several captures of one frequency (a long listen on a band with no decoder stage): keep the clearest one and
    record how often the signal was there."""
    if len(runs) == 1:
        return runs[0]
    ok = [r for r in runs if not _stage_failed(r)] or runs[:1]
    present = [r for r in ok if r.get("signal_present_during_capture")]
    snr = lambda r: r.get("channel_snr_db") if kind == "wide" else (
        r.get("peak_snr_db") if r.get("peak_snr_db") is not None else r.get("channel_avg_snr_db"))
    named = [r for r in present if r.get("rds_station_name")]
    best = dict(max(named or present or ok, key=lambda r: snr(r) if snr(r) is not None else -99.0))
    best.update(looks=len(ok), looks_with_signal=len(present), watched_seconds=round(span),
                present_percent=round(100.0 * len(present) / len(ok)))
    if present and len(present) < len(ok):
        best["continuous"] = False
        best["physical_waveform_classification"] = (f"{best.get('physical_waveform_classification')} (on in "
                                                    f"{len(present)} of {len(ok)} captures over {round(span)} s)")
    return best


def _investigate(peak_info, serials, listen_s=None, timed=None):
    """Investigate one frequency.
    1. Listen on every given dongle at the same time and compare how strongly each one hears it.
    2. Run the decoders that suit the band: rtl_433 on every dongle at once (sensor bands), the pager / APRS /
       tone decoders on the strongest dongle, rtl_adsb at 1090 MHz.
    3. Verdict from the strongest dongle plus anything a decoder confirmed on any dongle.
    listen_s: decoder listening time. timed (default: listen_s was given) means the user asked for that time, so
    it is honoured whatever the band: FM and airband get watched that long, steady carriers still get rtl_433."""
    cfg = _load_config()
    freq = float(peak_info["frequency_mhz"])
    bw = float(peak_info.get("occupied_bw_khz") or 25.0)
    kind = _band_kind(freq, bw)
    target = float(peak_info.get("channel_mhz") or peak_info.get("center_mhz") or freq) if kind == "wide" else freq
    serials = [str(s) for s in serials]
    earlier = _earlier_checks(target, max(0.06, bw / 2000.0))
    names = ", ".join(_short(s) for s in serials)

    listen = int(listen_s or 0)          # decoder listening time (0 = automatic)
    timed = bool(listen) if timed is None else bool(timed and listen)
    looks, span = {}, 0.0
    if kind != "adsb":
        look_s = 3.0
        # FM broadcast and airband have no decoder stage after the look, so when the user asked for a time the look
        # itself lasts that long (one 3 s capture after another) instead of stopping after 3 s.
        watch = timed and kind in ("wide", "air") and listen > look_s
        span = float(listen) if watch else look_s
        after = {"sensor": "rtl_433 decodes", "other": "rtl_433 decodes", "audio": "pager / APRS / tone decoders listen"}
        _say(f"     [LISTEN] {names}: " + (f"watching {target:g} MHz for {span:g} s (a 3 s capture after another)"
                                           if watch else f"{look_s:g} s look at {target:g} MHz")
             + (" on all of them at once to compare the antennas" if len(serials) > 1 else "")
             + (f"; then {after[kind]} for {listen or 'about 12-25'} s" if kind in after else "") + "...")

        def _look(ser):
            gain = _gain_for(ser, target, target, cfg)
            with _use_dongle(ser, timeout=30, holder="investigation"):
                once = lambda: (_stage1_wideband(target, ser, gain, look_s) if kind == "wide"
                                else _stage1_iq_analysis(freq, ser, gain, bw, look_s))
                runs = [once()]
                end = time.time() + span - look_s
                while watch and time.time() < end:
                    time.sleep(USB_SETTLE_S)         # the same dongle is opened again straight away
                    runs.append(once())
            r = _best_look(runs, kind, span)
            r["gain_db"] = gain
            return r
        looks = _parallel(_look, serials)
    table = {}
    for ser in serials:
        r, row = looks.get(ser), {"dongle": _short(ser)}
        if isinstance(r, Exception):
            row["error"] = str(r)[:140]
        elif r is not None:
            if _stage_failed(r):
                row["error"] = str(r["status"])[:140]
            else:
                cont = kind == "wide" or r.get("continuous")
                pk = r.get("peak_snr_db")
                row.update(heard=bool(r.get("signal_present_during_capture")) and (cont or pk is None or pk >= 3.0),
                           snr_db=(r.get("channel_snr_db") if kind == "wide" else
                                   r.get("channel_avg_snr_db") if cont else r.get("peak_snr_db")),
                           gain_db=r.get("gain_db"), waveform=r.get("physical_waveform_classification"),
                           carrier_mhz=r.get("measured_carrier_mhz"))
                if row["heard"] and kind != "wide":
                    row["pattern"] = "continuous" if r.get("continuous") else "bursts"
        table[ser] = row
    usable = [s for s in serials if "error" not in table[s]]
    if not usable and kind != "adsb":
        raise RFError("No dongle could listen: " + "; ".join(f"{t['dongle']}: {t['error']}" for t in table.values()))
    usable = usable or serials
    snr_of = lambda s: table[s].get("snr_db") if table[s].get("snr_db") is not None else -99.0
    heard = sorted([s for s in usable if table[s].get("heard")], key=lambda s: -snr_of(s))
    best = heard[0] if heard else _best_for_band(target, target, cfg, among=usable)

    s2 = s3 = adsb = None
    used, listened = ["IQ analysis"] if kind != "adsb" else [], 0
    steady = bool(heard) and all(table[s].get("pattern") == "continuous" for s in heard)
    # rtl_433 decodes bursts. A steady carrier carries no packets, so it gets the audio decoders instead, unless the
    # user asked for a listening time: then both run, so a burst device next to the carrier is not missed.
    run433 = kind == "sensor" or (kind == "other" and (not steady or timed))
    runaud = kind == "audio" or (kind == "other" and steady)
    secs433, secs_aud = int(listen or (12 if heard else 25)), int(listen or 8)
    on433, aud_on = list(usable), best
    together = False
    if run433 and runaud:
        if len(usable) > 1:          # audio decoders on the strongest dongle, rtl_433 on the others, at once
            on433, together = [x for x in usable if x != best], True
        else:                        # one dongle: share the time
            secs_aud = max(4, secs433 // 2)
            secs433 = max(4, secs433 - secs_aud)
    rates = {x: "250k" for x in on433}
    order = [x for x in heard if x in on433] + [x for x in on433 if x not in heard]
    if run433 and len(order) >= 2:
        rates[order[1]] = "1024k"    # some fast FSK devices only decode at 1 MS/s: cover both rates

    def job433():
        _say(f"     [DECODE] rtl_433 on {', '.join(_short(x) for x in on433)}" + (" at once" if len(on433) > 1 else "")
             + f" ({secs433} s" + (f"; {_short(order[1])} at 1 MS/s for devices that need it" if len(order) >= 2
                                   else "") + ")...")

        def _r433(ser):
            with _use_dongle(ser, timeout=30, holder="rtl_433 decode"):
                return _stage2_rtl433(freq, ser, secs433, _gain_for(ser, freq, freq, cfg), rates[ser])
        return _merge_rtl433(_parallel(_r433, on433), table)

    def jobaud():
        _say(f"     [DECODE] {_short(aud_on)}: rtl_fm -> pager / APRS / tone decoders ({secs_aud} s)...")
        try:
            with _use_dongle(aud_on, timeout=30, holder="audio decode"):
                return _stage3_demod_audio(freq, aud_on, secs_aud, _gain_for(aud_on, freq, freq, cfg))
        except RFError as e:
            return {"status": f"skipped: {e}"}

    r433_best = None
    if together:
        with ThreadPoolExecutor(max_workers=2) as pool:
            f433, faud = pool.submit(_carry_ctx(job433)), pool.submit(_carry_ctx(jobaud))
            (s2, r433_best), s3 = f433.result(), faud.result()
    else:
        if run433:
            s2, r433_best = job433()
        if runaud:
            s3 = jobaud()
    t433 = secs433 if s2 is not None and not s2.get("all_failed") else 0     # time actually spent decoding
    taud = secs_aud if s3 is not None and str(s3.get("status", "")).startswith("listened") else 0
    listened = max(t433, taud) if together else t433 + taud
    if s2 is not None:
        used.append("rtl_433")
        if not heard and r433_best:            # rtl_433 caught an intermittent signal the short look missed
            best = r433_best
    if s3 is not None:
        used += s3.get("decoders_used", [])
    if kind == "adsb":
        secs = int(listen or 10)
        listened = secs
        _say(f"     [DECODE] rtl_adsb on {names}" + (" at once" if len(serials) > 1 else "") + f" ({secs} s)...")

        def _ads(ser):
            with _use_dongle(ser, timeout=30, holder="ADS-B decode"):
                return _stage_adsb(ser, secs, _gain_for(ser, freq, freq, cfg))
        ra = _parallel(_ads, serials)
        for ser, r in ra.items():
            if isinstance(r, Exception):
                table[ser]["error"] = str(r)[:140]
            else:
                # rtl_adsb checks no checksums: only verified squitters show an aircraft is there (noise and short
                # replies also come out as "messages", the real PC: "0 aircraft, 4 messages" saved as a decode)
                table[ser].update(heard=bool(r.get("valid_messages")), adsb_messages=r.get("messages", 0),
                                  adsb_verified=r.get("valid_messages", 0), aircraft=r.get("aircraft_heard", 0))
        ok = [s for s in serials if table[s].get("adsb_verified")]
        best = max(ok, key=lambda s: (table[s]["adsb_verified"], table[s]["aircraft"])) if ok else best
        adsb = ra.get(best) if isinstance(ra.get(best), dict) else None
        used.append("rtl_adsb")

    s1 = looks.get(best) if isinstance(looks.get(best), dict) else {}
    heard_on = [s for s in serials if table[s].get("heard")]
    station, core = False, None
    if kind == "wide":
        verdict, evidence, note, station = _wide_verdict(s1, target, peak_info)
        if evidence == "none" and len(serials) > 1:
            verdict = "Nothing transmitting during the check on any dongle"
    elif kind == "adsb":
        raw = max((int(table[s].get("adsb_messages") or 0) for s in serials), default=0)
        if adsb and adsb.get("aircraft_heard"):
            n_ac, n_ok = adsb["aircraft_heard"], adsb.get("valid_messages", 0)
            verdict = (f"Identified: aircraft ADS-B transponders, {n_ac} aircraft ({n_ok} checksum-verified "
                       f"position or identity message{'s' if n_ok != 1 else ''}, public broadcast)")
            evidence = "confirmed_decode"
        elif raw:
            verdict, evidence = (f"Not identified: {raw} Mode S replies or noise, but no checksum-verified aircraft "
                                 f"message (the ADS-B decoder prints damaged frames and noise too)"), "none"
            core = verdict
        else:
            verdict, evidence = ("No ADS-B messages heard (no aircraft in range, or the antennas do not suit "
                                 "1090 MHz)"), "none"
        note = "confirmed by a decoder" if evidence == "confirmed_decode" else "nothing decoded"
    else:
        s2d, s3d = s2 or {}, s3 or {}
        valid, aprs = s3d.get("validated_frames") or [], s3d.get("aprs_frames") or []
        fps = s2d.get("unknown_signal_fingerprints") or []
        ctcss = s3d.get("ctcss_tone", "none")
        devs = s2d.get("devices") or []
        if devs:
            evidence = "confirmed_decode"
            verdict = _identified_text(devs)          # known devices folded into one clause
            unk = [f for f in fps if f.get("demodulated")]
            if unk:
                verdict += (f". Also heard {len(unk)} other signal{'s' if len(unk) > 1 else ''} that no decoder "
                            f"recognised: " + "; ".join(_fp_line(f, hint=False) for f in unk[:2]))
        elif valid:
            verdict, evidence = f"Identified: {valid[0]}", "confirmed_decode"
        elif aprs:
            verdict, evidence = f"Identified: APRS / AX.25 packet radio ({aprs[0]})", "confirmed_decode"
        elif heard_on:
            evidence = "signal_present"
            wave_txt = s1.get("physical_waveform_classification") if s1.get("signal_present_during_capture") else None
            if kind == "air":
                verdict = "Aviation-band transmission (AM voice or data; content not recorded)"
            elif ctcss != "none":
                verdict = f"Analog FM voice channel with CTCSS tone {ctcss} (content not recorded)"
            elif fps and fps[0].get("demodulated"):
                verdict = ("Not identified: rtl_433 demodulated its packets, but none of its decoders knows this "
                           "device")
            elif wave_txt:
                verdict = f"Not identified: no decoder recognised it. Measured: {wave_txt}"
            elif fps:
                verdict = f"Not identified: {fps[0]['modulation']} bursts that no rtl_433 decoder recognised"
            else:
                verdict = "Not identified: a signal was heard but nothing decoded it"
            if fps:
                verdict += ". Its signal: " + _fp_line(fps[0])
            core = verdict
            verdict += f" (band: {peak_info.get('probable_allocation') or _classify_frequency_allocation(freq, bw)})"
            if kind in ("sensor", "other", "audio"):
                verdict += (f". To identify it, capture it: 'capture {freq:g}' keeps the dongle that hears it best "
                            f"listening until a packet decodes")
        else:
            evidence = "none"
            total = round(span + listened)
            verdict = f"Nothing transmitted during this {total} s check" + (" on any dongle" if len(serials) > 1 else "")
            if any(e.get("evidence") in ("signal_present", "confirmed_decode") for e in earlier):
                verdict += ("; an earlier check DID hear a signal here, so it is probably intermittent and was quiet "
                            "this time")
        note = "identified by a decoder" if evidence == "confirmed_decode" else "not identified by any decoder"
        if s2d.get("all_failed"):                      # never let a broken decoder look like "nothing decoded"
            why = next(iter(s2d["failed_on"].values()))
            verdict = f"Decoding was not possible: {why}. {verdict}"
            core = f"Decoding was not possible: {why}. {core or ''}".strip()
            note = "the decoder could not run"

    out = {k: v for k, v in peak_info.items() if not k.startswith("_")}
    if core:
        out["verdict_core"] = core
    out.update({"frequency_mhz": round(target, 4), "band_type": kind, "listened_with": [_short(s) for s in serials],
                "dongle_comparison": [table[s] for s in serials],
                "strongest_dongle": _short(best) if heard_on else None,
                "investigated_by_dongle": DONGLE_NAMES.get(best, best),
                "final_signal_verdict": verdict, "evidence": evidence, "verdict_note": note,
                "listened_seconds": round(span + listened),
                "measured_carrier_mhz": s1.get("measured_carrier_mhz"), "decoders_used": used,
                "stage1_iq_physical_and_bitstream": s1})
    an = _antenna_note(serials, heard_on, table)
    if an:
        out["antenna_note"] = an
    if str(s1.get("physical_waveform_classification", "")).startswith("Steady unmodulated carrier"):
        ch = _clock_harmonic(s1.get("measured_carrier_mhz") or freq, best, cfg)
        if ch and len(heard_on) <= 1:
            out["possible_clock_harmonic"] = {**ch, "meaning": (
                f"this steady carrier sits on a multiple of the dongle's own {REF_CLOCK_MHZ} MHz clock, so it may come "
                f"from the receiver itself"), "how_to_check": "unplug that dongle's antenna: if the carrier stays at "
                "the same level, it comes from the receiver"}
    if s2:
        out["stage2_rtl433_telemetry"] = s2
        if s2.get("devices"):
            out["identified_devices"] = s2["devices"]
    if s3:
        out["stage3_multimon_direwolf_audio"] = s3
    if adsb:
        out["adsb"] = adsb
    if earlier:
        out["earlier_checks_of_this_frequency"] = earlier
    if out.get("snr_db") is None:
        out["snr_db"] = table[best].get("snr_db") if table[best].get("heard") else None
        if out["snr_db"] is None:
            out["snr_db"] = table[best].get("rtl_433_snr_db")
    if kind == "wide":
        out["broadcast_station_confirmed"] = station
        out["save_to_signal_list"] = evidence != "none" and not station
    out["stage4_plain_english"] = _log_signal(peak_info, out, best)
    _remember_check(out)
    return out


# ============================================================================
# TOOLS: SWEEP / INVESTIGATE / ROUTING (all dongles sweep at the same time by default)
# ============================================================================
def _merge_peaks(per):
    """Peaks from several dongles' sweeps of the same band -> one entry per signal, with each dongle's SNR."""
    groups = []
    for ser, peaks in per.items():
        for p in peaks:
            f = float(p.get("channel_mhz") or p["frequency_mhz"])
            tol = 0.12 if p.get("channel_mhz") else max(0.03, min(0.1, float(p.get("occupied_bw_khz") or 20) / 2000.0
                                                              + 0.01))
            g = next((g for g in groups if abs(g["f"] - f) <= max(tol, g["tol"]) and ser not in g["by"]), None)
            if g is None:
                g = {"f": f, "tol": tol, "by": {}}
                groups.append(g)
            g["by"][ser] = p
    out, known = [], None
    for g in groups:
        best = max(g["by"], key=lambda s: g["by"][s]["snr_db"])
        p = dict(g["by"][best])
        p["strongest_dongle"] = _short(best)
        # Each dongle's sweep saw an FM station at other moments, so its programme wander differs: the mean of
        # the centres of the dongles that hear it well is steadier than any one of them.
        cents = [float(q["centroid_mhz"]) for q in g["by"].values() if q.get("channel_mhz") and q.get("centroid_mhz")
                 and q["snr_db"] >= max(10.0, p["snr_db"] - 10.0)]
        if p.get("channel_mhz") and len(cents) > 1:
            known = _known_stations() if known is None else known
            p["centroid_mhz"] = round(sum(cents) / len(cents), 4)
            _fm_channel_fields(p, p["centroid_mhz"], bool(p.get("_fm_coarse")), known)
        if len(per) > 1:
            p["heard_by"] = {_short(s): g["by"][s]["snr_db"] for s in sorted(g["by"])}
            miss = [_short(s) for s in per if s not in g["by"]]
            if miss:
                p["not_heard_by"] = miss
        p["_best"] = best
        out.append(p)
    out.sort(key=lambda x: -x["snr_db"])
    for i, p in enumerate(out, start=1):
        p["peak_id"] = i
    return out


def _multi_sweep(s, e, serials, secs, thresh, cfg):
    step = float(cfg["step_khz"])
    how = f"watching for {secs} s, peak hold" if secs else "quick pass"
    print(f"\n[HW SWEEP] {', '.join(_short(x) for x in serials)} sweeping {s:g}-{e:g} MHz"
          + (" at the same time" if len(serials) > 1 else "") + f" ({how}, threshold +{thresh} dB)...")

    def one(ser):
        gain = _gain_for(ser, s, e, cfg)
        bins, floor, peaks, artifacts, info = _sweep_peaks(s, e, step, gain, ser, thresh, cfg["cluster_gap_khz"],
                                                           lock_timeout=60.0 + secs, duration_s=secs)
        return {"gain_db": gain, "noise_floor_db": floor, "peaks": _group_broadcast(peaks, step), "artifacts": artifacts,
                "info": info, "bins": len(bins)}
    res = _parallel(one, serials)
    per, errors, artifacts, info = {}, [], [], {}
    for ser in serials:
        r = res[ser]
        if isinstance(r, Exception):
            errors.append(f"{_short(ser)}: {r}")
            continue
        per[ser] = r
        artifacts += [{**a, "dongle": _short(ser)} for a in r["artifacts"]]
        info = info or r["info"]
        print(f"[HW SWEEP] {_short(ser)}: gain {r['gain_db']} dB, noise floor {r['noise_floor_db']} dB, "
              f"{len(r['peaks'])} signal(s), {len(r['artifacts'])} receiver artifact(s) filtered")
    if not per:
        raise RFError("No dongle could sweep: " + "; ".join(errors))
    peaks = _merge_peaks({k: v["peaks"] for k, v in per.items()})
    return {"peaks": peaks, "artifacts": artifacts, "errors": errors, "info": info,
            "per_dongle": {_short(k): {"gain_db": v["gain_db"], "noise_floor_db": v["noise_floor_db"],
                                       "signals": len(v["peaks"])} for k, v in per.items()}}


def _public(p):
    return {k: v for k, v in p.items() if not k.startswith("_")}


@register(
    "check_and_investigate_band",
    "Sweep a band with every dongle at once, find real signals (receiver artifacts are filtered out), then "
    "investigate the strongest ones, each on the dongle that hears it best, with the decoders that suit the band.",
    {**_BAND_PROPS, **_DONGLE_PROP,
     "sweep_seconds": {"type": "integer", "description": "How long to watch the band, in seconds. Set this "
                       "when the user asks for an N-second (or N-minute) sweep. 0 = quick 2 s pass (default)."},
     "threshold_db": {"type": "number", "description": "dB above noise floor to count as a peak (default 6)"},
     "dwell_seconds": {"type": "integer", "description": "Listening time per signal AFTER the sweep (default 12, "
                       "up to 600). Set it when the user says how long to listen to each signal. Not the sweep length."},
     "max_peaks_to_investigate": {"type": "integer", "description": "How many signals to investigate (default 4, "
                                  "up to 12). Use 12 when the user asks to investigate everything / all signals."}},
    aliases=("investigate_band", "scan_band"),
)
def check_and_investigate_band(frequency_range=None, start_mhz=None, stop_mhz=None, dongle=None,
                               sweep_seconds=None, threshold_db=None, dwell_seconds=None,
                               max_peaks_to_investigate=None):
    cfg = _load_config()
    s_mhz, e_mhz = _resolve_band(start_mhz, stop_mhz, frequency_range)
    secs = _sweep_seconds_arg(sweep_seconds, cfg["default_sweep_seconds"])
    thresh = _sanitize_threshold(threshold_db, cfg["threshold_db"])
    asked = dwell_seconds not in (None, "", 0, "0")
    dwell = _int(dwell_seconds, "dwell_seconds", int(cfg.get("dwell_seconds", 12)), 5, MAX_LISTEN_S)
    max_peaks = _int(max_peaks_to_investigate, "max_peaks_to_investigate", 4, 1, 12)
    serials = _pick_serials(dongle, cfg, band=(s_mhz, e_mhz))
    sw = _multi_sweep(s_mhz, e_mhz, serials, secs, thresh, cfg)
    peaks = sw["peaks"]
    # Investigate the unusual first: FM-band signals off the channel plan, then everything outside the FM band,
    # then ordinary-looking FM stations (strong stations must not crowd out a weak bug).
    rank = lambda p: (0 if p.get("channel_mhz") and p.get("on_standard_fm_channel") is False else
                      2 if p.get("channel_mhz") else 1, -p["snr_db"])
    targets = sorted(peaks, key=rank)[:max_peaks]
    queues = {}
    for p in targets:
        queues.setdefault(p["_best"], []).append(p)

    def work(ser):
        done = []
        for p in queues[ser]:
            print(f"\n  -> {p.get('channel_mhz') or p['frequency_mhz']} MHz (+{p['snr_db']} dB, heard best on "
                  f"{_short(ser)})")
            try:
                done.append(_investigate(p, [ser], listen_s=dwell, timed=asked))
            except RFError as ex:
                done.append({**_public(p), "investigation_error": str(ex)})
        return done
    results = []
    if targets:
        print(f"[HW DECODE] Investigating {len(targets)} signal(s), each on the dongle that hears it best...")
        for ser, r in _parallel(work, list(queues)).items():
            results += ([{**_public(p), "investigation_error": str(r)} for p in queues[ser]]
                        if isinstance(r, Exception) else r)
        pos = {p["peak_id"]: i for i, p in enumerate(targets)}      # unusual signals first, as investigated
        results.sort(key=lambda x: pos.get(x.get("peak_id"), 99))
    report = {"status": "Band sweep and investigation complete", "timestamp": _now(),
              "band_swept_mhz": f"{s_mhz}-{e_mhz} MHz", **sw["info"], **_fm_summary(peaks),
              "dongles_used": sw["per_dongle"], "total_real_peaks_found": len(peaks),
              "peaks_investigated_count": len(results), "investigated_peaks": results,
              "rejected_artifacts": sw["artifacts"],
              "limits": "Only shows what was transmitting during this check; it cannot prove nothing else is there."}
    if sw["errors"]:
        report["dongle_errors"] = sw["errors"]
    if len(peaks) > len(targets):
        done = {id(p) for p in targets}
        report["peaks_not_investigated"] = [
            {"frequency_mhz": p.get("channel_mhz") or p["frequency_mhz"], "snr_db": p["snr_db"],
             "strongest_dongle": p["strongest_dongle"]} for p in peaks if id(p) not in done][:10]
    try:
        _atomic_write_json(LAST_REPORT, report)
    except OSError:
        pass
    return report


@register(
    "investigate_single_frequency",
    "Investigate one frequency: listens on every dongle at the same time, compares how strongly each hears it, "
    "then decodes on the strongest with the decoders that suit the band (rtl_433, pager/APRS decoders, rtl_adsb).",
    {"frequency_mhz": {"type": "number", "description": "Frequency in MHz, e.g. 433.92"},
     **_DONGLE_PROP,
     "listen_seconds": {"type": "integer", "description": "How long to listen, in seconds (up to 600). Set it "
                        "whenever the user gives a time ('for 2 minutes' = 120); it is honoured on every band. "
                        "Default: automatic (12-25 s). Use 60 when the user wants to know exactly what a signal is."}},
    required=("frequency_mhz",),
    aliases=("investigate_frequency", "identify_signal"),
)
def investigate_single_frequency(frequency_mhz=None, listen_seconds=None, dongle=None, dwell_seconds=None):
    cfg = _load_config()
    freq = round(_to_mhz(frequency_mhz), 5) if frequency_mhz not in (None, "") else 433.92
    if not MIN_MHZ <= freq <= MAX_MHZ:
        raise RFError(f"{freq:g} MHz is outside the tuner range {MIN_MHZ:g}-{MAX_MHZ:g} MHz.")
    asked = _int(listen_seconds or dwell_seconds, "listen_seconds", 0, 3, 10 ** 6)
    if asked > MAX_LISTEN_S:              # never shorten a time the user asked for: say what to use instead
        raise RFError(f"An investigation listens for up to {MAX_LISTEN_S // 60} minutes, and {asked // 60} min was "
                      f"asked. For longer, use a capture: it keeps the best dongle on {freq:g} MHz in the background "
                      f"('capture {freq:g} for {max(1, round(asked / 60))} minutes'), or 'keep logging {freq:g}' to "
                      f"log every packet.")
    listen = asked
    serials = _pick_serials(dongle, cfg, band=freq)
    _say(f"\n[HW DEEP DIVE] {freq} MHz with {', '.join(_short(s) for s in serials)}...")
    peak = {"peak_id": 1, "frequency_mhz": freq, "center_mhz": freq, "occupied_bw_khz": 25.0,
            "probable_allocation": _classify_frequency_allocation(freq, 25.0)}
    res = _investigate(peak, serials, listen_s=listen or None)
    try:
        _atomic_write_json(LAST_REPORT, res)
    except OSError:
        pass
    return res
