"""ARIS engine, part "status": Locate (nodes, floorplans, heat maps through arislib), maintenance and status tools, the cleanup
of files from earlier versions, shutdown.

Executed into aris.py's namespace by aris._include (see aris.py): names from aris.py's imports and from the
parts before this one are available here without imports, and everything defined here becomes aris.<name>.
"""
from __future__ import annotations

# ============================================================================
# LOCATE: nodes on a floorplan and a heat map of where a transmitter probably is (arislib/)
# ============================================================================
def _local_node_list():
    """(dongles [(serial, name)], absent serials) for nodes.ensure_local: the dongles as ARIS numbers them now, and
    those the last check did not find."""
    cfg = _load_config()
    return ([(ser, _short(ser)) for ser in DONGLE_NAMES],
            [ser for ser in DONGLE_NAMES if _dongle_cfg(ser, cfg).get("present") is False])


def _nodes_mod():
    from arislib import nodes as _n
    _n.ensure_local(*_local_node_list())
    return _n


@register(
    "locate_signal",
    "Estimate WHERE a transmitter is: measures its strength on every antenna placed on the floorplan (Map tab) at "
    "the same time and works out the most likely area. Use for 'where is it', 'locate', 'which room', 'find "
    "where 433.92 is coming from'. Needs antennas placed on the Map tab first.",
    {"frequency_mhz": {"type": "number", "description": "Frequency in MHz, e.g. 433.92"},
     "listen_seconds": {"type": "integer", "description": "Seconds to listen on each antenna (default 5; use 15-30 "
                        "for a device that transmits rarely)"}},
    required=("frequency_mhz",),
    aliases=("locate", "find_location", "where_is_signal"),
)
def locate_signal(frequency_mhz=None, listen_seconds=None, bandwidth_khz=None):
    from arislib import locate as _loc
    if frequency_mhz in (None, ""):
        raise RFError("Give the frequency to locate, e.g. 433.92.")
    f = float(frequency_mhz)
    if not (MIN_MHZ <= f <= MAX_MHZ):
        raise RFError(f"{f} MHz is outside the dongles' range ({MIN_MHZ:g}-{MAX_MHZ:g} MHz).")
    _nodes_mod()
    secs = _int(listen_seconds, "listen_seconds", 5, 2, 30)
    _say(f"[HW LOCATE] Measuring {f:g} MHz on every placed antenna for {secs} s...")
    try:
        res = _loc.run_measure(sys.modules[__name__], f, bandwidth_khz, secs)
    except ValueError as e:
        raise RFError(str(e))
    return _loc.for_ai(res)


@register("list_nodes", "List the receivers (dongles and remote nodes), where each antenna sits on the floorplan, and "
          "which bands they are calibrated for.", {})
def list_nodes():
    from arislib import floorplan as _fp
    n = _nodes_mod()
    plan = _fp.active()
    rows = [{"name": x["name"], "kind": x["kind"], "enabled": x.get("enabled", True),
             "placed_on_active_plan": x["placed"] and x.get("floorplan") == (plan or {}).get("id"),
             "position_m": [x["x"], x["y"]] if x["placed"] else None, "antenna": x.get("antenna") or None,
             "calibrated_bands": sorted((x.get("calibrated") or {}).keys())} for x in n.list_nodes()]
    return {"floorplan": plan["name"] if plan else None,
            "plan_size_m": [plan["width_m"], plan["height_m"]] if plan else None, "nodes": rows}


# ============================================================================
# TOOLS: MAINTENANCE + STATUS
# ============================================================================
def _trim_text_log(path: Path, keep_lines: int) -> None:
    try:
        if path.exists() and path.stat().st_size > 2_000_000:
            with open(path, errors="replace") as f:
                tail = deque(f, maxlen=keep_lines)
            with open(path, "w") as f:
                f.writelines(tail)
    except OSError:
        pass


@register("perform_system_maintenance", "Trim background logs and delete stale temp files (never deletes data).",
          {"max_lines": {"type": "integer", "description": "Lines to retain in the ISM log (default 1000)"}})
def perform_system_maintenance(max_lines=None):
    global _log_line_count
    keep = _int(max_lines, "max_lines", LOG_MAX_LINES, 100, 100000)
    with _LOG_LOCK:
        info = _trim_jsonl(WATCHDOG_LOG, keep)
        _log_line_count = info.get("retained", info.get("lines", _log_line_count))
    managed = [info, _trim_jsonl(AUDIT_HISTORY, 500), _trim_jsonl(PATROL_LOG, 500)]
    _trim_text_log(LOG_FILE, 5000)
    removed = 0
    for p in Path(tempfile.gettempdir()).glob("aris_*"):
        try:
            if p.is_file() and time.time() - p.stat().st_mtime > 3600:
                p.unlink()
                removed += 1
        except OSError:
            pass
    managed.append({"stale_temp_files_removed": removed})
    return {"message": "System maintenance complete", "managed": managed,
            "old_files_from_earlier_versions": len(_cleanup_candidates())}


# ----------------------------------------------------------------------------
# Cleanup of files left by earlier versions (only these known patterns are ever touched)
# ----------------------------------------------------------------------------
def _db_rows(path) -> int:
    try:
        conn = sqlite3.connect(str(path), timeout=5)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM peaks").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error:
        return -1


def _size(p: Path) -> int:
    try:
        if p.is_dir():
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink())
        return p.stat().st_size
    except OSError:
        return 0


def _cleanup_candidates():
    """Leftovers from Argon and earlier ARIS upgrades: backups, patch files, downloads, caches, old logs."""
    me = Path(os.path.abspath(__file__))
    keep = {me.name, "aris_gui.py", "aris_node.py"}
    # ARIS may run from its folder (a checkout) through a link in the home folder: look in both places.
    bases = []
    for b in (me.parent, me.resolve().parent, Path.home()):
        if b not in bases:
            bases.append(b)
    found, seen = [], set()

    def add(p, why):
        try:
            if p.is_symlink() or not p.exists() or p.name in keep or p.resolve() in seen:
                return
            seen.add(p.resolve())
        except OSError:
            return
        found.append((p, why))
    for pat, why in (("aris_urh.py", "merged into aris.py in v4.4"), ("aris_urh.py.*", "old backup"),
                     ("argon*.py", "old Argon program (replaced by ARIS)"), ("argon_data", "old Argon data folder"),
                     ("*.pre4*", "backup from an earlier ARIS upgrade"),
                     ("aris*.py.pre2*", "backup made by deploy/install.sh"), ("aris*.py.bak*", "old backup"),
                     ("aris*.py.orig", "patch leftover"), ("aris*.py.rej", "patch leftover"),
                     ("aris*.patch", "old patch file"), ("aris_v4*.tar.gz", "old download"),
                     ("aris (*", "renamed duplicate download"), ("aris_gui (*", "renamed duplicate download"),
                     ("aris_urh (*", "renamed duplicate download"), ("__pycache__", "Python cache (rebuilt automatically)"),
                     ("aris_sigint.db.bak-*", "old Saved Signals backup")):
        for base in bases:
            for p in sorted(base.glob(pat)):
                add(p, why)
    base = Path.home()
    for op in [Path(x) for x in OLD_DB_PATHS] + [base / "aris_sigint.db", base / "argon_sigint.db"]:
        try:                                         # (an unreadable folder: Python 3.8's Path.exists raises
            there = op.exists()                      # PermissionError there, 3.9+ answers False)
        except OSError:
            there = False
        if there and Path(DB_PATH).exists() and op.resolve() != Path(DB_PATH).resolve() \
                and _db_rows(DB_PATH) >= _db_rows(op):
            add(op, "old Saved Signals file (already copied to aris_data/signals.db)")
    tmp = Path(tempfile.gettempdir())
    for pat, why in (("aris4*", "old download folder"), ("share", "old file-transfer folder"),
                     ("argon_rf_config.json", "old Argon setting"),
                     ("ism_background.jsonl", "old watchdog log (now kept in aris_data)"),
                     ("aris_*", "leftover temporary capture")):
        for p in sorted(tmp.glob(pat)):
            try:
                if hasattr(os, "getuid") and p.lstat().st_uid != os.getuid():
                    continue                 # only what this user made: /tmp is world-writable (v4.9.6 review)
                if pat == "aris_*" and (p.is_dir() or time.time() - p.stat().st_mtime < 3600):
                    continue
            except OSError:
                continue
            add(p, why)
    bdir = DATA_DIR / "backups"
    for p in (sorted(bdir.glob("before-setup-*"))[:-2] if bdir.exists() else []):
        add(p, "older setup backup (the newest 2 are kept)")
    if BASELINE_DIR.exists():
        for p in sorted(BASELINE_DIR.glob("*.bak")):
            add(p, "old baseline backup")
        for p in sorted(BASELINE_DIR.glob("*.json")):
            if "_" not in p.stem:
                add(p, "old-format baseline not tied to a dongle (/setup records new ones)")
    dbp = Path(DB_PATH)
    for p in sorted(dbp.parent.glob(dbp.name + ".bak-*"))[:-1]:
        add(p, "older Saved Signals backup (the newest is kept)")
    return found


def _cleanup(apply=False) -> dict:
    items = _cleanup_candidates()
    rows = [{"path": str(p), "kb": round(_size(p) / 1024.0), "why": why} for p, why in items]
    total_mb = round(sum(r["kb"] for r in rows) / 1024.0, 1)
    if not apply:
        return {"old_files": rows, "count": len(rows), "total_mb": total_mb}
    removed, failed = 0, []
    for p, _ in items:
        try:
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            else:
                p.unlink()
            removed += 1
        except OSError as e:
            failed.append(f"{p}: {e}")
    perform_system_maintenance()
    return {"removed": removed, "freed_mb": total_mb, "failed": failed, "items": rows}


def _cleanup_text(res) -> str:
    if "removed" in res:
        txt = f"Removed {res['removed']} old file(s), freeing {res['freed_mb']} MB. Logs trimmed."
        return txt + (f" Could not remove: {'; '.join(res['failed'])}" if res["failed"] else "")
    if not res["count"]:
        return "Nothing to clean up: no files from earlier versions found."
    lines = [f"Found {res['count']} old file(s) ({res['total_mb']} MB) from earlier versions:"]
    lines += [f"  {r['path']}  ({r['kb']} KB) - {r['why']}" for r in res["old_files"][:40]]
    if res["count"] > 40:
        lines.append(f"  ... and {res['count'] - 40} more")
    lines.append("Type /cleanup confirm to delete them. Your settings, baselines, login and Saved Signals are kept.")
    return "\n".join(lines)


@register("list_old_files", "List leftover files from Argon and earlier ARIS versions that /cleanup can remove "
          "(it never touches settings, baselines, the login or Saved Signals). Listing only; the user deletes them "
          "with /cleanup confirm.", {})
def list_old_files():
    return _cleanup(apply=False)


def _list_devices() -> list:
    """`rtl_test -d 99` lists dongles without opening any of them: librtlsdr prints the list first, 99 is no
    device index, and none of the station's serials starts or ends with 99 (its prefix and suffix matches would
    open such a dongle: keep that in mind when choosing a serial)."""
    if shutil.which("rtl_test") is None:
        return _other_receivers()
    try:
        p = subprocess.run(["rtl_test", "-d", "99"], capture_output=True, text=True, timeout=8)
        text = (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        text = " ".join(x.decode(errors="ignore") if isinstance(x, bytes) else (x or "")
                        for x in (e.stdout, e.stderr))
    out = []
    for i, n, sn in re.findall(r"^\s*(\d+):\s+(.+?),\s+SN:\s*(\S+)", text, re.M):
        if not _SERIAL_OK.fullmatch(sn):          # a serial is a file name, a lock name and an argv word here
            log.warning("dongle %s: unusable serial %r (letters, digits, '.', '_' and '-' only); ignored", i, sn[:40])
            continue
        out.append({"index": int(i), "name": n.strip()[:60], "serial": sn})
    return out + _other_receivers()


def _other_receivers() -> list:
    """Receivers that are no RTL-SDR dongle (SoapySDR, plug-in types, ones named in the config), listed by their
    own tools without opening any (arislib.radios)."""
    try:
        return [{"index": None, "name": d.get("name", ""), "serial": d["serial"], "backend": d.get("backend")}
                for d in radios.list_devices(include_rtl=False) if _SERIAL_OK.fullmatch(str(d.get("serial", "")))]
    except Exception as e:          # noqa: BLE001 - a broken helper must not hide the dongles
        log.warning("listing other receivers failed: %s", e)
        return []


_SERIAL_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")   # (a crafted EEPROM serial must not become a path)


_OLLAMA = None


def _ollama():
    global _OLLAMA
    if _OLLAMA is None:
        _OLLAMA = ollama.Client(host=OLLAMA_HOST)
    return _OLLAMA


def _ollama_check() -> dict:
    try:
        resp = _ollama().list()
    except Exception as e:
        return {"reachable": False, "host": OLLAMA_HOST, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    models = getattr(resp, "models", None)
    if models is None and isinstance(resp, dict):
        models = resp.get("models", [])
    names = []
    for m in models or []:
        n = getattr(m, "model", None) or (m.get("model") or m.get("name") if isinstance(m, dict) else None)
        if n:
            names.append(str(n).lower())
    want = MODEL.lower()
    installed = want in names or (":" not in want and f"{want}:latest" in names)
    return {"reachable": True, "host": OLLAMA_HOST, "model": MODEL, "model_installed": installed}


@register(
    "list_saved_signals",
    "List the signals in the Saved Signals list, newest first: time, frequency, ARIS verdict and whether a "
    "decoder confirmed it. Use for questions about signals found earlier.",
    {"max_items": {"type": "integer", "description": "How many to list (default 10)"}},
)
def list_saved_signals(max_items=None):
    n = _int(max_items, "max_items", 10, 1, 50)
    init_db()
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        try:
            total = conn.execute("SELECT COUNT(*) FROM peaks").fetchone()[0]
            rows = conn.execute("SELECT timestamp, frequency_mhz, snr_db, urh_analysis, notes FROM peaks "
                                "ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise RFError(f"Could not read the saved signals: {e}")
    items = []
    for ts, freq, snr, analysis, notes in rows:
        lines = (analysis or "").splitlines()
        head = next((ln for ln in lines if ln.startswith("• ARIS verdict")), "")
        verdict = head.split(":", 1)[1].strip() if ":" in head else str(notes or "")
        confirmed = any(ln.startswith("• rtl_433 confirmed") for ln in lines) or "(confirmed decode)" in head
        items.append({"time": ts, "frequency_mhz": round(float(freq or 0.0), 4), "snr_db": snr,
                      "verdict": verdict[:240], "decoder_confirmed": confirmed,
                      "heard_on": next((ln.split(":", 1)[1].strip() for ln in lines if ln.startswith("• Heard on")),
                                       None)})
    return {"count_total": total, "shown": len(items), "signals": items}


@register("system_status", "Report dongles (gains, frequency correction, what each hears per band), the best "
          "dongle per band, decoders, patrol, watchdogs, AI model and logs.", {})
def system_status():
    found = _list_devices()
    serials = {d["serial"] for d in found}
    cfg = _load_config()
    wds = _running_watchdogs()
    try:
        with open(WATCHDOG_LOG, "rb") as f:
            log_lines = sum(1 for _ in f)
    except OSError:
        log_lines = 0
    dongles = {}
    for ser in DONGLE_NAMES:
        d = _dongle_cfg(ser, cfg)
        cap = next((w for w in wds if str(w.get("serial")) == ser), None)
        dongles[_short(ser)] = {
            "serial": ser, "tuner": d.get("tuner"), "usb_ok": d.get("usb_ok"),
            "present": (ser in serials) if found else (False if d.get("present") is False else None),
            "doing": (_capture_doing(cap) if cap else "busy with a job" if _dongle_in_use(ser) else "free"),
            "gains_db": {"433_and_other": _gain_for(ser, cfg=cfg), "915": _gain_for(ser, 902.0, 928.0, cfg),
                         "fm": _gain_for(ser, 88.0, 108.0, cfg)},
            "ppm": _ppm_of(ser, cfg), "hears_per_band": d.get("reception") or "not measured yet (run /setup)",
            "antenna_note": d.get("antenna_note"), "busy": _device_lock(ser).locked()}
    return {
        "version": VERSION,
        "setup_done": cfg.get("setup_done"),
        "dongles_found": [d["serial"] for d in found],
        "dongles_missing": [_short(s) for s in DONGLE_NAMES if found and s not in serials],
        "dongles": dongles,
        "best_dongle_per_band": {b: _short(s) for b, s in cfg["band_dongles"].items()},
        "default_dongle": _short(_default_serial(cfg)),
        "decoders": _decoder_inventory(),
        "watchdogs": wds, "patrol": _PATROL.summary() if _PATROL else {"running": False},
        "baselines": len(list(BASELINE_DIR.glob("*.json"))) if BASELINE_DIR.exists() else 0,
        "ism_log_lines": log_lines,
        "tmp_free_gb": round(shutil.disk_usage(tempfile.gettempdir()).free / 1e9, 2),
        "disk": disk_usage(),
        "ai_model": _ollama_check(),
        "content_capture": "metadata only (no message text, no voice recording or transcription)",
        "plugins": [{"name": p["name"], "ok": p["ok"], "about": p["about"], "error": p["error"]}
                    for p in globals().get("PLUGINS", [])],
    }


def _clear_signals() -> str:
    init_db()
    bak = f"{DB_PATH}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(DB_PATH, bak)
    with _DB_LOCK:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        try:
            n = conn.execute("SELECT COUNT(*) FROM peaks").fetchone()[0]
            conn.execute("DELETE FROM peaks")
            conn.commit()
        finally:
            conn.close()
    return f"Cleared {n} saved signal(s). A backup copy is at {bak}"


def _shutdown() -> None:
    if _PATROL:
        try:
            _PATROL.stop_evt.set()
        except Exception:
            pass
    live = [w for w in list(_WATCHDOGS.values()) if w.alive()]
    if GUI_HOST and live:            # the GUI resumes these when it starts again (resume_captures)
        try:
            _atomic_write_json(WATCHDOG_RESUME, [w.summary() for w in live])
        except OSError:
            pass
    for wd in live:                  # asked together, then waited for: each ends its own radio program
        try:
            wd.stop("shutdown" if GUI_HOST else "stopped", wait=False)
        except Exception:
            pass
    for wd in live:
        try:
            wd.wait_stopped()
        except Exception:
            pass


atexit.register(_shutdown)
