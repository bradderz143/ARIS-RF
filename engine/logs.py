"""ARIS engine, part "logs": The capture log (ism_events.jsonl) with its caps and rotation, and the device history (the
Devices tab).

Executed into aris.py's namespace by aris._include (see aris.py): names from aris.py's imports and from the
parts before this one are available here without imports, and everything defined here becomes aris.<name>.
"""
from __future__ import annotations

# ============================================================================
# BACKGROUND ISM WATCHDOG (rtl_433) + LOG ROTATION
# ============================================================================
_LOG_LOCK = threading.Lock()
_log_line_count = None


def _trim_jsonl(path: Path, max_lines: int) -> dict:
    if not path.exists():
        return {"file": str(path), "status": "absent"}
    total, tail = 0, deque(maxlen=max_lines)
    with open(path, "r", errors="replace") as f:
        for line in f:
            total += 1
            tail.append(line)
    if total <= max_lines:
        return {"file": str(path), "status": "under limit", "lines": total}
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".trim_")
    with os.fdopen(fd, "w") as f:
        f.writelines(tail)
    os.replace(tmp, path)
    return {"file": str(path), "trimmed_from": total, "retained": len(tail)}


def _log_append(obj: dict) -> None:
    global _log_line_count
    with _LOG_LOCK:
        if _log_line_count is None:
            try:
                with open(WATCHDOG_LOG, "rb") as f:
                    _log_line_count = sum(1 for _ in f)
            except FileNotFoundError:
                _log_line_count = 0
        with open(WATCHDOG_LOG, "a") as f:
            f.write(json.dumps(obj, separators=(",", ":")) + "\n")
        _log_line_count += 1
        if _log_line_count > LOG_MAX_LINES * 1.5:
            # (a count that another process's Clear logs made stale is put right by the trim's own count)
            info = _trim_jsonl(WATCHDOG_LOG, LOG_MAX_LINES)
            _log_line_count = info.get("retained", info.get("lines", _log_line_count))
    if obj.get("type") == "packet":       # every decoded packet (captures, band recordings) joins the device history
        _devices_note(obj, logged=True)


_LINE_COUNTS = {}     # lines in each capped .jsonl file (patrol, audit): counted once, kept in step by _jsonl_append


def _jsonl_append(path: Path, obj, max_lines: int) -> None:
    """Append one record to a capped .jsonl file: past 1.5 x max_lines the oldest lines go (the watchdog log's rule),
    so no log grows for ever. A line count another process's Clear logs made stale costs one extra trim scan."""
    key = str(path)
    with _LOG_LOCK:
        n = _LINE_COUNTS.get(key)
        if n is None:
            try:
                with open(path, "rb") as f:
                    n = sum(1 for _ in f)
            except OSError:
                n = 0
        with open(path, "a") as f:
            f.write(json.dumps(obj, default=str) + "\n")
        n += 1
        if n > max_lines * 1.5:
            info = _trim_jsonl(path, max_lines)
            n = info.get("retained", info.get("lines", n))
        _LINE_COUNTS[key] = n


# ============================================================================
# DEVICE HISTORY (the Devices tab): every decoded device, one record per model + ID, kept past the log's cap.
# The records live in DEVICES_FILE (arislib.devices holds the rules: transmissions, sets, caps). Decoded packets
# reach it through _log_append (captures, band recordings) and _stage2_rtl433 (investigations, the Decode button),
# always after _safe_packet. Writes are batched (DEVICES_WRITE_S), atomic, and read-modify-write under a file lock,
# because the GUI and the chat console both decode.
# ============================================================================
_DEV_LOCK = threading.Lock()
_DEV_PENDING: list = []
_DEV_TIMER = {"t": None, "last": 0.0}
_DEV_CACHE = {"tag": None, "rows": None}


@contextmanager
def _devices_file_lock():
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_DIR / "devices.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _packet_epoch(p, default):
    """rtl_433's clock time in a live packet ('2026-11-02 15:21:03') as an epoch; `default` for '@0.04s' (-r files)
    or none."""
    try:
        return time.mktime(time.strptime(str(p.get("time")), "%Y-%m-%d %H:%M:%S"))
    except (TypeError, ValueError):
        return default


def _devices_apply(store, p) -> None:
    from arislib import devices
    q = _safe_packet(p)          # (already safe where it comes from; this costs nothing and makes sure)
    info = _device_info(q)
    kind = info["type"]
    if str(q.get("id", "")).startswith("#") and kind == "device decoded by rtl_433":
        kind = "remote control / wireless button (gate, garage, lights or alarm fob)"
    dongle = list(q.get("aris_heard_by") or []) or (_short(q["aris_serial"]) if q.get("aris_serial") else None)
    devices.apply_packet(store, q, kind=kind, dongle=dongle, name=_DEVICE_NAMES.get(str(q.get("model"))))


def _devices_read() -> dict:
    """devices.json as it is, or None when it is missing or unreadable."""
    from arislib import devices
    try:
        with open(DEVICES_FILE) as f:
            store = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("devices.json unreadable (%s): starting it afresh", e)
        return None
    if isinstance(store, dict) and isinstance(store.get("devices"), dict):
        if devices.scrub(store):            # an earlier version's file: remote readings and labels brought up to date
            try:
                _atomic_write_json(DEVICES_FILE, store, compact=True)
            except OSError:
                pass
        return store
    return devices.new_store()


def _devices_load() -> tuple:
    """(store, rebuilt): the store (the caller holds the file lock). On first use with no devices.json it is built
    once from ism_events.jsonl, so the history already logged shows at once."""
    from arislib import devices
    store = _devices_read()
    if store is not None:
        return store, False
    store = devices.new_store()
    for p in _watchdog_events():
        if p.get("type", "packet") == "packet" and p.get("model"):
            _devices_apply(store, p)
    devices.prune(store)
    store["built_from_log"] = _now()
    _atomic_write_json(DEVICES_FILE, store, compact=True)
    return store, True


def _devices_flush() -> None:
    """Write the pending packets into devices.json (and make the file exist). Called by the write timer and before
    every read, so a read never misses what was decoded a moment ago."""
    from arislib import devices
    with _DEV_LOCK:
        tm = _DEV_TIMER["t"]
        if tm is not None:
            tm.cancel()
            _DEV_TIMER["t"] = None
        batch, _DEV_PENDING[:] = list(_DEV_PENDING), []
        try:
            with _devices_file_lock():
                if batch or not DEVICES_FILE.exists():
                    store, rebuilt = _devices_load()
                    if rebuilt:               # the log's packets are in already: only the others are pending
                        batch = [(lg, p) for lg, p in batch if not lg]
                    if batch:
                        for _lg, p in sorted(batch, key=lambda x: float(x[1].get("t") or 0)):
                            _devices_apply(store, p)
                        devices.prune(store)
                        store["updated"] = _now()
                        _atomic_write_json(DEVICES_FILE, store, compact=True)
        except Exception as e:                    # noqa: BLE001 - the history must never stop a capture
            log.warning("device history not written (%s): %s", type(e).__name__, e)
        _DEV_TIMER["last"] = time.time()


def _devices_note(p: dict, logged: bool = False) -> None:
    """A decoded packet (after _safe_packet, with t / time / aris_serial / aris_freq_mhz) for the device history.
    Batched: written out within DEVICES_WRITE_S of the first pending packet. `logged`: it is in ism_events.jsonl
    too (a first-use rebuild from the log then already holds it)."""
    if not p.get("model"):
        return
    with _DEV_LOCK:
        _DEV_PENDING.append((bool(logged), dict(p)))
        if _DEV_TIMER["t"] is None:
            due = max(0.0, DEVICES_WRITE_S - (time.time() - _DEV_TIMER["last"]))
            tm = threading.Timer(due, _devices_flush)
            tm.daemon = True
            tm.start()
            _DEV_TIMER["t"] = tm


def devices_tag() -> str:
    """Changes whenever the history changes (the page's ?have=): the file's time and size, pending packets written."""
    _devices_flush()
    day = time.strftime("%Y%m%d")      # the rows say '(today)' / '(yesterday)': a new day is a new list
    try:
        st = DEVICES_FILE.stat()
        return f"{st.st_mtime_ns}-{st.st_size}-{day}"
    except OSError:
        return f"0-0-{day}"


def devices_list(limit=None, text=None) -> dict:
    """The device history as the page and the chat show it: rows newest first (a set of sensors that always
    transmit together is one row with its members), the totals and the grouping rule. `text` filters rows
    (make, ID, frequency, kind, dongle); `limit` cuts the list (the totals stay whole)."""
    from arislib import devices
    tag = devices_tag()
    with _DEV_LOCK:
        if _DEV_CACHE["tag"] != tag or _DEV_CACHE["rows"] is None:
            with _devices_file_lock():
                store = _devices_read() or devices.new_store()
            _DEV_CACHE.update(tag=tag, rows=devices.rows(store))
        rep = json.loads(json.dumps(_DEV_CACHE["rows"]))       # (the cache stays untouched by callers)
    rep["tag"] = tag
    if text:
        rep["rows"] = [r for r in rep["rows"] if devices.matches(r, text)]
        rep["filter"] = str(text)
    rep["shown"] = len(rep["rows"])
    if limit:
        rep["rows"] = rep["rows"][:int(limit)]
    return rep


def forget_devices() -> dict:
    """Forget all devices (the tab's button): the history starts empty. The logs stay as they are; the history is
    only rebuilt from them when its file is missing, so an emptied file keeps the forgotten devices away."""
    from arislib import devices
    with _DEV_LOCK:
        tm = _DEV_TIMER["t"]
        if tm is not None:
            tm.cancel()
            _DEV_TIMER["t"] = None
        _DEV_PENDING[:] = []
        with _devices_file_lock():
            store = _devices_read()
            n = len((store or {}).get("devices") or {})
            _atomic_write_json(DEVICES_FILE, devices.new_store(forgotten=_now()), compact=True)
        _DEV_CACHE.update(tag=None, rows=None)
    return {"message": (f"Forgotten: {_plural(n, 'device')}. " if n else "There were no devices to forget. ")
                       + "New decodes start the list again.", "forgotten": n}


def mark_device_known(key, known) -> dict:
    """The Devices tab's Known toggle: mark a device (or a set) known (True), not known / watch it (False) or back
    to the rule (None)."""
    from arislib import devices
    _devices_flush()
    with _DEV_LOCK:
        with _devices_file_lock():
            store = _devices_read() or devices.new_store()
            n = devices.mark_known(store, str(key), known)
            if n:
                _atomic_write_json(DEVICES_FILE, store, compact=True)
        _DEV_CACHE.update(tag=None, rows=None)
    if not n:
        return {"status": "error", "error": "no such device"}
    what = "known" if known else "not known (watched)" if known is False else "back to the rule"
    return {"status": "ok", "changed": n, "message": f"Marked {what}."}


def _known_fold(heard: list, passing: bool = True) -> tuple:
    """Known devices (the history's rule or the user's mark, arislib.devices) folded out of a list of devices just
    heard, then the tyre sensors of two or more makes that are not known folded by make (fold_passing):
    (known_line, passing_line, rest)."""
    from arislib import devices
    try:
        _devices_flush()
        with _devices_file_lock():
            store = _devices_read() or devices.new_store()
    except Exception as e:                       # noqa: BLE001 - never let the history break a result
        log.warning("known devices not read: %s", e)
        store = devices.new_store()
    known_line, rest = devices.fold_known(store, heard)
    passing_line = None
    if passing:
        passing_line, rest = devices.fold_passing(rest)
    return known_line, passing_line, rest


def _identified_text(devs, decoder="rtl_433", limit=5) -> str:
    """'Identified: ...' for the devices a decoder named (from _devices_from_packets), the known ones folded into
    one clause: 'Identified: Citroen tyre sensor 92cf949a, a tyre-pressure sensor (3 packets; ID 92cf949a).
    Also known devices: Nexus-TH temperature sensor; 4 packets. Decoded by rtl_433'."""
    heard = [{"model": d.get("model"), "id": d.get("id_label"), "kind": d.get("type"), "packets": d.get("packets"),
              "_d": d} for d in devs]
    known_line, _none, rest = _known_fold(heard, passing=False)   # (one frequency: every new device with its ID)
    new = [h["_d"] for h in rest]
    parts = []
    if len(new) == 1:
        parts.append(f"Identified: {_device_line(new[0])}")
    elif new:
        parts.append(f"Identified {len(new)} different devices (decoded by {decoder}): "
                     + "; ".join(f"{i}) {_device_line(d)}" for i, d in enumerate(new[:limit], 1)))
    if known_line:
        body = known_line[len("Known: "):]             # (no bracket around it: a remote's name has its own)
        parts.append(f"Also known devices: {body}" if parts else f"Identified: only known devices: {body}")
    text = ". ".join(parts) if parts else "Identified: nothing"
    if "decoded by" not in text:
        text += f". Decoded by {decoder}"
    return text


WATCHDOG_STATE = DATA_DIR / "watchdogs.json"           # running watchdogs (each ARIS process adds its own)
WATCHDOG_RESUME = DATA_DIR / "watchdogs_resume.json"   # captures a restart interrupted (the GUI resumes them)
CAPTURES = DATA_DIR / "captures.json"                  # finished captures, newest last
NOTICES = DATA_DIR / "notices.jsonl"                   # messages for the chat (capture results)
STOP_DIR = DATA_DIR / "stop"                           # a file named after a dongle's serial stops its watchdog
CAPTURE_DEFAULT_MIN = 60
CAPTURE_MAX_MIN = 24 * 60
WATCHDOG_STOP_S = 8.0   # how long a stop waits for a capture's thread (it ends its radio program within ~2 s)
GUI_HOST = False        # set by the GUI process: it hosts background captures, so they survive a chat reset
_A_LINES = ("Analyzing pulses", "Total count", "Pulse width distribution", "Gap width distribution", "Pulse+gap",
            "Gap+pulse", "Timing distribution", "Level estimates", "RSSI:", "Frequency offsets", "Guessing modulation",
            "view at", "Attempting demodulation", "Use a flex decoder", "[", "pulse_slicer", "pulse_demod", "codes",
            "bitbuffer")


_A_FLEX = re.compile(r"-X\s+'n=[^,']*,m=([A-Z0-9_]+)(?:,s=(\d+))?(?:,l=(\d+))?(?:,r=(\d+))?(?:,g=(\d+))?")
_A_DIST = re.compile(r"^\[\s*\d+\]\s*count:\s*(\d+),\s*width:\s*(\d+)\s*us")
_A_ROWS = re.compile(r"Number of rows:\s*(\d+)")
_A_BITS = re.compile(r"^\[\s*\d+\]\s*\{(\d+)\}")


def _analyzer_fields(cur, s) -> bool:
    """Read one line of rtl_433's pulse-analyzer block into `cur`. True if the line belongs to the block.
    The demodulated bits themselves are never kept: only how many there are."""
    m = _A_FLEX.search(s)
    if m:
        cur["coding"] = m.group(1)
        if m.group(5):
            cur["gap_limit"] = int(m.group(5))
        return True
    if s.endswith("distribution:"):
        cur["_sec"] = s.split()[0].lower()           # "gap", "pulse", "timing", ...
        return True
    m = _A_DIST.search(s)
    if m:
        if cur.get("_sec") == "gap":
            cur.setdefault("_gaps", []).append((int(m.group(1)), int(m.group(2))))
        return True
    m = _A_BITS.search(s)
    if m:
        cur.setdefault("_bits", []).append(int(m.group(1)))
        return True
    for rx, keys in ((_A_TOT, ("pulses", "ms")), (_A_LVL, ("rssi", "snr")), (_A_OFF, ("off",)),
                     (_A_DEMOD, ("short", "long")), (_A_ROWS, ("rows",))):
        m = rx.search(s)
        if m:
            for k, v in zip(keys, m.groups()):
                cur[k] = float(v) if "." in v else int(v)
            return True
    m = _A_GUESS.search(s)
    if m:
        cur["guess"] = _norm_guess(m.group(1))
        return True
    return s.startswith(_A_LINES)


def _norm_guess(g):
    g = str(g or "").strip()
    return ("pattern unclear" if g.lower().startswith("no clue") else
            "single pulse" if g.lower().startswith("single pulse") else g[:80])
