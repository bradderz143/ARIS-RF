"""engine part: room watch. A long, quiet-room watch of the bands where small transmitters are found: a reference is
recorded in each band on every receiver that tunes it, then rounds of audits against it run for hours or days, and
what appears is told as it happens ("worth a look", never a finding). The run is hosted by the GUI process (like
captures) so it survives the chat's Stop and Reset of the console, writes its state as it goes and is resumed after
a restart. The rules (bands, what makes an alert, the report) are arislib.roomwatch; the audit, the baselines and
the radio locks are the engine's own. Receive-only; nothing is recorded but levels and frequencies.

How it runs: every receiver has its own thread and walks its own list of sweeps at its own pace, each starting at
a different place in the list, so a band that several receivers tune is looked at more often than once a round. A
receiver that is slow, busy or stuck never holds the others back. Where one receiver cannot get through its whole
list in a round, the bands only it tunes come first and the bands it shares with others take turns. Each sweep is
judged against that receiver's own reference, and only a reference recorded for this watch counts: a file an
earlier watch left behind is never used. Events go to the tracker at once. A round is the watch's bookkeeping
step: counting, health checks, the state's copy.

What keeps it honest over days: a round in which no sweep worked is not counted as a round; a receiver that stops
working is named after two rounds; a band nobody could sweep for half an hour is said so (chat, the page, the
optional alert address); time in which the computer slept or the program was held up is written into the timeline
as a gap; an error in the watch's own thread starts it again; a stall is noticed by a guard thread; Reset Scanner
ends the sweeps in flight and the watch carries on with the next ones; a stop and a later "carry on" keeps the
reference and everything seen so far."""

ROOM_STATE = DATA_DIR / "roomwatch_state.json"
ROOM_LOG = DATA_DIR / "roomwatch.jsonl"
ROOM_STATE_BAK = DATA_DIR / "roomwatch_state.json.bak"
ROOM_LOG_MAX_LINES = 8000        # past 1.5 x this the oldest NOTES go; alerts, marks, gaps and checks always stay
ROOM_DEFAULT_HOURS = 72.0
ROOM_MAX_HOURS = 24.0 * 14
ROOM_INTERVAL_MIN = 5.0
ROOM_NOTICES_PER_HOUR = 6        # more alerts than this in an hour are logged and reported, not each sent to the chat
ROOM_CHECK_EVERY_S = 3600        # a new carrier gets one quick investigation per hour at most
ROOM_STALL_S = 900               # no progress for this long while it should be working: said loudly
ROOM_RX_STALL_S = 600            # one receiver that has not finished a sweep for this long: named; the others go on
ROOM_ALIVE_S = 300               # another process's watch counts as running only with a state this fresh
ROOM_UNWATCHED_S = 1800          # a band no receiver could sweep for this long is said so ...
ROOM_HEALTH_EVERY_S = 2 * 3600   # ... and again this much later if it is still so
ROOM_HOLD_S = 25                 # Reset Scanner: the watch leaves the radios alone this long, then carries on
ROOM_INVENTORY_MAX = 200         # carriers of the reference kept one by one (the report lists them all)
ROOM_LIFT_DB = 6.0               # a sweep this far over its reference everywhere ...
ROOM_LIFT_CAUSE_DB = 30.0        # ... with a signal this strong in it (or 6 flagged): a strong transmitter is close
ROOM_LOW_DB = -4.0               # a sweep this far UNDER its reference two rounds running: the reference was noisy
ROOM_NOISE_NOTE_S = 6 * 3600     # a band's raised or fallen noise is noted this often at most
ROOM_LOOKS_PER_ROUND = 2         # quick looks per receiver per round (many new carriers at once must not stall it)
ROOM_REF_TRIES = 3               # a reference that could not be recorded is tried again this often, a round apart,
ROOM_REF_AGAIN_S = 1800          # ... and after that once in this long, for as long as the watch runs
ROOM_LATE_SUM_S = 180.0          # hold-ups too short to be a gap each, adding up to this within an hour: a gap
ROOM_ERR_AGE_S = 2 * 3600        # the card shows errors this recent
ROOM_DEAF_DB = -15.0             # a receiver whose reference's strong signals are all this far down, two rounds
ROOM_DEAF_BINS = 20              # running and over at least this many frequencies, has lost its antenna
ROOM_PILOT_DB = 15.0             # a steady signal this strong in a reference shows that the receiver hears
ROOM_SUMMARY_HOURS = (8, 20)     # "all is well" at these hours (local time): its absence is the dead-man signal
ROOM_CARRY_DAYS = 14             # an earlier watch this recent is offered to be carried on
ROOM_GUARD_S = 10.0              # the guard thread's step; when it wakes ...
ROOM_GAP_S = 90.0                # ... this much late, the computer slept or the program was held up: a gap
ROOM_CLOCK_MHZ = 28.8            # an RTL-SDR dongle shows its own clock at multiples of this
ROOM_REPORTS = DATA_DIR / "reports"
ROOM_REPORTS_KEEP = 20
ROOM_PUSH_QUIET = "ARIS room watch: something needs a look. Open ARIS to read it."
ROOM_PUSH_OK = "ARIS room watch: running, nothing needs you."
ROOM_PUSH_OPEN = "ARIS room watch: running. Something in ARIS waits for you."
ROOM_PUSH_END = "ARIS room watch: finished."
ROOM_PUSH_RETRY_S = 60           # an alert the address did not take is sent again this often ...
ROOM_PUSH_KEEP_S = 1800          # ... for this long, then given up (the card says how many)

_ROOM = None
_ROOM_START_LOCK = threading.Lock()          # start / stop / resume: one at a time
_ROOM_ROWS_CACHE = {"key": None, "rows": []}
_ROOM_PUSH = {"error": None, "ok_t": None, "queue": deque(maxlen=50), "thread": None, "dropped": 0,
              "lock": threading.Lock()}
_ROOM_GUARD = {"thread": None}


# ---------------------------------------------------------------------------------------------- plan, log, state
def _room_ranges(serials) -> list:
    out = []
    for s in serials:
        try:
            lo, hi = radios.backend_for(s).freq_range_mhz
            out.append((float(lo), float(hi)))
        except Exception:
            continue
    return out


def _room_edges() -> list:
    """Where a receiver's gain changes (the FM band's edges and the region's sub-GHz gain band): a sweep never
    crosses one, or a whole band would be measured at the low gain the FM band needs."""
    try:
        return sorted(set(float(x) for x in tuple(_fm_band()) + tuple(_region().gain_band or ())))
    except Exception:
        return []


def _room_merged(spans) -> list:
    out = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1] + 1e-6:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _room_plan(serials, cfg, only=None) -> dict:
    """The sweeps of a watch: {chunks: [{s, e, step, label, serials}], not_covered, partly_covered}. `only` is a
    list of (lo, hi) bands in place of the standard ones. not_covered are the ranges no receiver here tunes."""
    bands = [(float(lo), float(hi), f"{float(lo):g}-{float(hi):g} MHz") for lo, hi in only] if only \
        else list(roomwatch.WATCH_BANDS)
    ranges = _room_ranges(serials)
    segs = [(a, b, c["label"]) for c in roomwatch.coverage(bands, ranges) for a, b in c["parts"] if b - a >= 0.5]
    starts, ends = {a for a, _, _ in segs}, {b for _, b, _ in segs}
    inner = starts & ends                         # where one band ends and the next begins
    cuts = starts | ends
    # where one receiver's range ends inside what another still tunes: a sweep of its own on each side, so the
    # sweeps of the receivers already there stay as they are when a wider receiver joins
    for x in sorted({v for r in ranges for v in r}):
        if any(a + 0.5 <= x <= b - 0.5 for a, b, _ in segs) and all(abs(x - y) >= 0.5 for y in cuts):
            cuts.add(x)
    for g in _room_edges():
        if not any(a < g < b for a, b, _ in segs):
            continue
        # a boundary between two bands within 2 MHz of a gain edge gives way to it (no sliver beside the edge); a
        # start or end of what is covered never moves, and the edge is no cut where it would leave under 0.5 MHz
        rest = {x for x in cuts if not (x in inner and 0 < abs(x - g) < 2.0)}
        if all(abs(g - x) >= 0.5 for x in rest):
            cuts = rest | {g}
    cuts = sorted(cuts)
    chunks = []
    base = float(cfg.get("step_khz", 10.0))
    for lo, hi in zip(cuts, cuts[1:]):
        mid = (lo + hi) / 2.0
        if hi - lo < 0.5 or not any(a - 2.0 <= lo and hi <= b + 2.0 and a <= mid <= b for a, b, _ in segs):
            continue                                                               # (a gap between two bands)
        label = next((lab for a, b, lab in bands if a <= mid <= b), f"{lo:g}-{hi:g} MHz")
        for s, e, step in roomwatch.chunks(lo, hi, base):
            have = _covering(serials, s, e)
            if have:
                chunks.append({"s": s, "e": e, "step": step, "label": label, "serials": have})
    none = [f"{a:g}-{b:g} MHz" for a, b in roomwatch.gaps(bands, ranges) if b - a >= 0.5]
    # anything the receivers tune that ended in no sweep is said, never dropped in silence
    swept = _room_merged([(c["s"], c["e"]) for c in chunks])
    for a, b in _room_merged([(a, b) for a, b, _ in segs]):
        at = a
        for s, e in swept + [(b, b)]:
            if e <= at or s >= b:
                continue
            if s - at >= 0.05:
                none.append(f"{at:g}-{min(s, b):g} MHz")
            at = max(at, e)
        if b - at >= 0.05:
            none.append(f"{at:g}-{b:g} MHz")
    return {"chunks": chunks, "not_covered": none, "partly_covered": []}


def _room_pairs(plan) -> set:
    return {(c["s"], c["e"], ser) for c in plan["chunks"] for ser in c["serials"]}


def _room_pair_key(p) -> str:
    return f"{p[0]:g}-{p[1]:g}@{p[2]}"


def _room_trim() -> None:
    """The timeline past its cap: the oldest notes go. Every alert, mark, gap, check of the watch itself, start
    and stop stays for as long as the file does: a long watch's first day must still be in its report."""
    with _LOG_LOCK:
        try:
            with open(ROOM_LOG, errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return
        notes = [i for i, ln in enumerate(lines) if '"kind": "note"' in ln]
        drop = set(notes[:max(0, len(lines) - ROOM_LOG_MAX_LINES)])
        kept = [ln for i, ln in enumerate(lines) if i not in drop]
        fd, tmp = tempfile.mkstemp(dir=str(ROOM_LOG.parent), prefix=ROOM_LOG.name + ".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.writelines(kept)
            os.replace(tmp, ROOM_LOG)
            _LINE_COUNTS[str(ROOM_LOG)] = len(kept)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass


def _room_log(kind, text, **extra) -> None:
    rec = {"t": round(time.time(), 2), "time": _now(), "kind": kind, "text": str(text), **extra}
    try:
        _jsonl_append(ROOM_LOG, rec, 10 ** 9)                 # (its own trim: see _room_trim)
        if _LINE_COUNTS.get(str(ROOM_LOG), 0) > ROOM_LOG_MAX_LINES * 1.5:
            _room_trim()
    except OSError:
        pass


def _room_rows() -> list:
    """The log's rows, oldest first. Parsed again only when the file changed (the pages ask every few seconds); the
    cache key is the file's time, size and inode. Callers must not change the rows."""
    try:
        st = os.stat(ROOM_LOG)
    except OSError:
        return []
    key = (st.st_mtime_ns, st.st_size, st.st_ino)
    if _ROOM_ROWS_CACHE["key"] == key:
        return _ROOM_ROWS_CACHE["rows"]
    rows = []
    try:
        with open(ROOM_LOG, errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and isinstance(obj.get("t"), (int, float)):
                    rows.append(obj)
    except OSError:
        return []
    _ROOM_ROWS_CACHE.update(key=key, rows=rows)
    return rows


def _room_unseen(rows=None) -> list:
    """The alerts nobody has acknowledged yet: those after the last "ack" row."""
    rows = _room_rows() if rows is None else rows
    last = max((r["t"] for r in rows if r.get("kind") == "ack"), default=0)
    return [r for r in rows if r.get("kind") in ("alert", "health") and r["t"] > last]


def _room_state_read() -> dict:
    """The watch's saved state. When the file is there but cannot be read (a power cut can leave it empty), the
    copy of the last finished round; no file at all means no watch (Clear data removed it)."""
    if not ROOM_STATE.exists():
        return {}
    for path in (ROOM_STATE, ROOM_STATE_BAK):
        try:
            with open(path) as f:
                st = json.load(f)
            if isinstance(st, dict) and st:
                if path is ROOM_STATE_BAK:
                    st["from_backup"] = True
                return st
        except (OSError, ValueError):
            continue
    return {}


def _room_state_write(obj) -> None:
    """The state through a temp file, flushed to the disk before the rename: after a power cut the file is the old
    state or the new one, never an empty one."""
    data = json.dumps(obj, separators=(",", ":"), default=str)
    fd, tmp = tempfile.mkstemp(dir=str(ROOM_STATE.parent), prefix=ROOM_STATE.name + ".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ROOM_STATE)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _room_plain(ex) -> str:
    """Why a sweep failed, in plain words (the raw text goes to aris.log, never to the card or the phone)."""
    msg = str(ex)
    low = msg.lower()
    if "is busy" in low or "busy with" in low:
        return "the receiver was busy with something else"
    if "no supported devices" in low or "not found" in low or "usb_open" in low or "unplugged" in low \
            or "not plugged" in low:
        return "the receiver was not found (unplugged?)"
    if "timed out" in low or "timeout" in low:
        return "the sweep took too long and was ended"
    if isinstance(ex, RFError):
        return "the radio program ended early"
    return f"an internal error ({type(ex).__name__})"


def _room_power_notes() -> list:
    """What about this computer could cut a long watch short, in plain sentences: it can still go to sleep (a
    sleeping computer watches nothing), or it runs on battery. Only what can be read for sure is said; where
    nothing can be read, nothing is claimed."""
    out = []
    try:
        if shutil.which("systemctl"):
            p = subprocess.run(["systemctl", "is-enabled", "suspend.target"], capture_output=True, text=True, timeout=4)
            state = (p.stdout or "").strip().splitlines()[-1:] or [""]
            if state[0] and state[0] != "masked":
                out.append("This computer can still go to sleep: a sleeping computer watches nothing (the watch "
                           "notices the gap only when it wakes). Switch sleep and the lid switch off for the "
                           "length of the watch; the guide says how.")
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        base = Path("/sys/class/power_supply")
        mains = [x for x in (base.iterdir() if base.is_dir() else [])
                 if (x / "type").is_file() and (x / "type").read_text().strip() == "Mains"]
        if mains and all((x / "online").read_text().strip() == "0" for x in mains if (x / "online").is_file()):
            out.append("This computer is running on its battery: plug it in before a long watch.")
    except (OSError, ValueError):
        pass
    return out


def _room_boot_clock() -> float:
    """A clock that keeps counting while the computer sleeps (Linux); the plain monotonic clock elsewhere."""
    try:
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    except (AttributeError, OSError):
        return time.monotonic()


def _room_gap(step, wall, boot, mono):
    """A step of the guard that should have taken `step` seconds took `wall` by the wall clock, `boot` by the clock
    that counts sleep, `mono` by the one that does not. Returns (seconds nothing ran, why) or (0, ''):
    asleep: the computer slept; held: the program did not run (paused, or the computer was overloaded); a wall
    clock that jumped while the other two did not is a clock that was set, not a gap."""
    late = boot - step
    if late < ROOM_GAP_S:
        return (0.0, "clock") if abs(wall - boot) >= ROOM_GAP_S else (0.0, "")
    return late, ("asleep" if mono - step < ROOM_GAP_S / 2.0 else "held")


# ------------------------------------------------------------------------------------- the optional alert address
def _room_push_target(cfg=None):
    """(the address alerts are also posted to, whether the alert's own text goes with it). Empty when not set."""
    cfg = cfg or _load_config()
    url = str(cfg.get("room_alert_url") or "").strip()
    return (url if re.match(r"^https?://[^\s]{4,300}$", url) else ""), bool(cfg.get("room_alert_detail"))


def _room_push_host(url) -> str:
    try:
        from urllib.parse import urlsplit
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def _room_push_why(ex) -> str:
    """Why a post failed, without one word of the server's own (an error page can repeat the address): the HTTP
    status as a number, or what this computer's own network code says."""
    code = getattr(ex, "code", None)
    if isinstance(code, int):
        return f"HTTP {code}"
    why = getattr(ex, "reason", ex)
    if isinstance(why, (socket.timeout, TimeoutError)) or isinstance(ex, (socket.timeout, TimeoutError)):
        return "no answer in time"
    if isinstance(why, ConnectionRefusedError):
        return "connection refused"
    if isinstance(why, socket.gaierror):
        return "the address's name was not found"
    if isinstance(why, OSError):
        return "no connection"
    return type(ex).__name__


def _room_push_send(url, body, title, prio) -> bool:
    import urllib.request
    try:
        req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST",
                                     headers={"Title": title, "Priority": prio,
                                              "Content-Type": "text/plain; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=8) as r:
            r.read(200)
        _ROOM_PUSH.update(error=None, ok_t=time.time())
        return True
    except Exception as ex:                               # (never the address: it is a secret of sorts)
        _ROOM_PUSH["error"] = _room_push_why(ex)
        return False


def _room_push_worker() -> None:
    """Send what waits, oldest first; what the address does not take is tried again every minute for half an hour."""
    q = _ROOM_PUSH["queue"]
    while True:
        with _ROOM_PUSH["lock"]:
            if not q:
                _ROOM_PUSH["thread"] = None
                return
            item = q[0]
        url, _detail = _room_push_target()
        ok = bool(url) and _room_push_send(url, item["body"], item["title"], item["prio"])
        with _ROOM_PUSH["lock"]:
            if q and q[0] is item and (ok or not url or time.time() - item["t"] > ROOM_PUSH_KEEP_S):
                q.popleft()
                if not ok and url:
                    _ROOM_PUSH["dropped"] += 1
                    log.warning("room watch: an alert could not be posted to the alert address (%s)",
                                _ROOM_PUSH["error"])
        if not ok and url:
            time.sleep(ROOM_PUSH_RETRY_S)


def _room_push(text, kind="alert", test=False, wait=False):
    """Post to the address the admin set (a push service such as an ntfy topic, so a phone in a pocket rings). Off
    unless set. Without `room_alert_detail` only a fixed sentence is sent: no frequency leaves the station. The
    sentence and the priority follow `kind` (alert, health, summary, open, end), so the twice-daily sign of life
    does not read like an alert. Never raises; the last error (no address in it) is kept for the page."""
    url, detail = _room_push_target()
    if not url:
        return False
    loud = kind in ("alert", "health") or test
    fixed = {"summary": ROOM_PUSH_OK, "open": ROOM_PUSH_OPEN, "end": ROOM_PUSH_END}.get(kind, ROOM_PUSH_QUIET)
    body = str(text) if (detail or test) else fixed
    title = "ARIS room watch" if loud else "ARIS room watch: daily check" if kind in ("summary", "open") \
        else "ARIS room watch: finished"
    prio = "high" if loud else "default"
    if test or wait:
        th = threading.Thread(target=_room_push_send, args=(url, body, title, prio), name="room-push", daemon=True)
        th.start()
        th.join(20)
        return True
    with _ROOM_PUSH["lock"]:
        _ROOM_PUSH["queue"].append({"body": body, "title": title, "prio": prio, "t": time.time()})
        th = _ROOM_PUSH["thread"]
        if th is None or not th.is_alive():
            th = _ROOM_PUSH["thread"] = threading.Thread(target=_room_push_worker, name="room-push", daemon=True)
            th.start()
    return True


def set_room_alert_address(url=None, detail=None) -> dict:
    """The admin's setting: where alerts are also posted ("" = nowhere, None = leave the address as it is), and
    whether their text goes along."""
    if url is not None:
        url = str(url).strip()
        if url and not re.match(r"^https?://[^\s]{4,300}$", url):
            raise RFError("The alert address must start with https:// or http:// and have no spaces.")
    with _CFG_LOCK:
        cfg = _load_config()
        if url is not None:
            cfg["room_alert_url"] = url
        if detail is not None:
            cfg["room_alert_detail"] = bool(detail)
        _save_config(cfg)
    with _ROOM_PUSH["lock"]:
        _ROOM_PUSH["queue"].clear()
    _ROOM_PUSH.update(error=None, ok_t=None, dropped=0)
    return room_alert_address()


def room_alert_address() -> dict:
    url, detail = _room_push_target()
    return {"set": bool(url), "host": _room_push_host(url), "detail": detail, "error": _ROOM_PUSH["error"],
            "last_ok": _ROOM_PUSH["ok_t"], "pending": len(_ROOM_PUSH["queue"]), "dropped": _ROOM_PUSH["dropped"]}


# ------------------------------------------------------------------------- what was on the air at the reference
def _room_is_rtl(ser) -> bool:
    try:
        return radios.backend_for(ser).name == "rtlsdr"
    except Exception:
        return False


def _room_inventory(plan, refs=None) -> dict:
    """The steady signals in the receivers' references: how many, how many of them are broadcast stations, TV or
    radio multiplexes, phone-band blocks or the dongles' own clock lines, and the rest one by one (carriers in the
    FM band off the station channels first, then the strongest). A transmitter that was on while the reference
    was recorded is in it and never alerts, so the report lists what nothing explains. Only what is wide enough
    to be a station, a multiplex or a phone block is counted as one: a narrow carrier inside such a band is listed."""
    items = []
    for c in plan["chunks"]:
        for ser in c["serials"]:
            if refs is not None and _room_pair_key((c["s"], c["e"], ser)) not in refs:
                continue                                 # (a file an earlier watch left is not this watch's)
            found = _find_baseline(c["s"], c["e"], ser)
            if not found:
                continue
            rtl = _room_is_rtl(ser)
            for p in (found[1].get("summary") or {}).get("steady") or []:
                try:
                    f, db = float(p["mhz"]), float(p["snr_db"])
                except (KeyError, TypeError, ValueError):
                    continue
                if c["s"] <= f <= c["e"]:
                    items.append({"mhz": f, "db": db, "dongle": _short(ser), "band": str(p.get("band") or ""),
                                  "bw": float(p.get("bw_khz") or 0.0), "rtl": rtl})
    items.sort(key=lambda x: x["mhz"])
    merged = []
    for it in items:
        m = merged[-1] if merged else None
        if m and it["mhz"] - m["mhz"] <= max(0.05, m["bw"] / 2000.0, it["bw"] / 2000.0):
            m["receivers"].add(it["dongle"])
            m["rtl"] = m["rtl"] and it["rtl"]
            if it["db"] > m["db"]:
                m.update(mhz=it["mhz"], db=it["db"], band=it["band"], bw=it["bw"])
        else:
            merged.append({**{k: it[k] for k in ("mhz", "db", "band", "bw", "rtl")}, "receivers": {it["dongle"]}})
    out = {"total": len(merged), "fm": 0, "tv": 0, "busy": 0, "own": 0, "listed": [], "more": 0}
    rest = []
    for m in merged:
        f, bw = m["mhz"], m["bw"]
        n = round(f / ROOM_CLOCK_MHZ)
        if _in_fm_band(f) and bw >= 100.0 and _on_fm_plan(f, 50.0):
            out["fm"] += 1
        elif bw >= 500.0 and re.search(r"\bTV\b|television|\bDAB\b", m["band"], re.I):
            out["tv"] += 1
        elif bw >= 150.0 and _in_busy_band(f):
            out["busy"] += 1
        elif m["rtl"] and n >= 1 and abs(f - n * ROOM_CLOCK_MHZ) <= 0.03 and bw < 100.0:
            out["own"] += 1                              # (only RTL-SDR dongles saw it, on their own clock's line)
        else:
            m["note"] = "FM band, not a broadcast station's signal" if _in_fm_band(f) else ""
            rest.append(m)
    rest.sort(key=lambda x: (not x["note"], -x["db"]))
    out["listed"] = [{"mhz": round(m["mhz"], 4), "db": round(m["db"], 1), "receivers": sorted(m["receivers"]),
                      "band": m["band"][:70], **({"note": m["note"]} if m["note"] else {})}
                     for m in rest[:ROOM_INVENTORY_MAX]]
    out["more"] = max(0, len(rest) - ROOM_INVENTORY_MAX)
    return out


def _room_tracker(state, trig):
    return roomwatch.Tracker.from_state(state, new_db=trig)




def _room_win() -> dict:
    return {"ok": 0, "busy": 0, "noref": 0, "failed": 0, "ref": 0}


# ------------------------------------------------------------------------------------------------------ the watch
class RoomWatch:
    """One watch. Phases: reference (recording the quiet reference), watching, ended."""

    def __init__(self, plan, hours, interval_min, trig, new_reference, state=None, only=None):
        st = dict(state or {})
        self.plan, self.only = plan, only
        self.interval_s = max(60.0, float(interval_min) * 60.0)
        self.trig = float(trig)
        self.new_reference = bool(st["new_reference"]) if "new_reference" in st else bool(new_reference)
        self.started_epoch = float(st.get("started_epoch") or time.time())
        self.started = st.get("started") or _now()
        self.deadline = float(st.get("deadline") or (self.started_epoch + float(hours) * 3600.0))
        self.tracker = _room_tracker(st.get("tracker"), self.trig)
        self.rounds = int(st.get("rounds") or 0)
        self.failed_rounds = int(st.get("failed_rounds") or 0)
        self.alerts_total = int(st.get("alerts_total") or 0)
        self.notes_total = int(st.get("notes_total") or 0)
        self.last_alert = st.get("last_alert")
        self.suppressed = int(st.get("suppressed") or 0)
        self.phase = "watching" if st.get("phase") == "watching" else "reference"
        self.watch_since = st.get("watch_since")
        self.inventory = st.get("inventory")
        self.round_s = st.get("round_s")
        self.sweeps_last = st.get("sweeps_last")
        # (band, receiver) -> when its reference was recorded FOR THIS WATCH. Only these count: a baseline file an
        # earlier watch or a rehearsal left behind is never taken for the reference.
        refs = st.get("refs")
        self.refs = dict(refs) if isinstance(refs, dict) else {k: None for k in (st.get("ref_done") or [])}
        self.late_refs = list(st.get("late_refs") or [])[-200:]
        self.gaps_s = float(st.get("gaps_s") or 0.0)
        self.resumed = bool(st)
        self.carried = bool(st.get("carried"))
        self.restart_note = "ARIS restarted; the room watch carried on from where it was."
        self.lock = threading.RLock()        # the tracker, the counters and the plan (receivers report in parallel)
        self.save_lock = threading.Lock()
        self.cv = threading.Condition()      # a new round: the receivers' threads start their next walk
        self.round_no = 0
        self.rx = {}                         # serial -> that receiver's own run (thread, walk, counters, health)
        self.shutting = False
        self.looks = {}                      # this round: serial -> quick looks made
        self.ref_tries = {}
        self.ref_last = {}                   # pair key -> when its reference was last tried
        self.dead_said = False               # "no sweep worked" was said: the all-clear is owed
        self.lates = []                      # (time, seconds) of the guard's steps that woke a little late
        self.lift_said = {}
        self.noise_said = {}
        self.low = {}                        # (band, serial) -> rounds in a row it read under its reference
        self.replaced_said = set()
        self.stop_evt = threading.Event()
        self.held_evt = threading.Event()    # Reset Scanner's hold: nothing new is launched while it is set
        self.thread = None
        self.errors = deque(maxlen=8)
        self.last_round = st.get("last_round")
        self.notice_times = deque(maxlen=2 * ROOM_NOTICES_PER_HOUR)
        self.many_said = 0.0
        self.checked = {}
        self.ended = None
        self.phase_note = ""
        self.beat = time.time()
        self.doing = ""
        self.hold_until = 0.0
        self.chunk_ok = {}                   # "s-e" -> when a sweep of it last worked, on any receiver
        self.born = time.time()              # (this object's own start: ages are never counted from before it)
        self.fail_streak = 0
        self.health_t = 0.0
        self.unwatched = []
        self.unwatched_said = set()
        self.busy_last = set()
        self.stall_said = 0.0
        self.hop_said = {}
        self.no_ref = set()                  # (s, e, serial) with no reference: said once
        self.pilots = {}                     # this round: serial -> [(bins, median dB against the reference)]
        self.deaf = {}                       # serial -> rounds in a row its pilots were down
        self.deaf_said = {}
        self.err_times = []
        self.last_summary = float(st.get("last_summary") or 0.0)

    @property
    def ref_done(self) -> set:
        return set(self.refs)

    # -- state ------------------------------------------------------------------------------------------------
    def _rx(self, ser) -> dict:
        r = self.rx.get(ser)
        if r is None:
            r = self.rx[ser] = {"thread": None, "lap_round": 0, "done": 0, "laps": 0, "win": _room_win(), "bad": 0,
                                "said": 0.0, "since": None, "doing": "", "sweep_s": None, "lap_s": None, "ptr": 0,
                                "state": "working", "note": "", "err": "", "stall_said": 0.0, "last_ok": None}
        return r

    def receivers(self) -> list:
        """Every receiver of the plan and how it is doing, for the card and the status."""
        with self.lock:
            want = sorted({x for c in self.plan["chunks"] for x in c["serials"]}, key=_short)
            return [{"name": _short(s), "state": self._rx(s)["state"], "note": self._rx(s)["note"]} for s in want]

    def snapshot(self) -> dict:
        with self.lock:
            types = {}
            for c in self.plan["chunks"]:
                for x in c["serials"]:
                    if _short(x) not in types:
                        try:
                            types[_short(x)] = "RTL-SDR" if _room_is_rtl(x) else str(radios.label_for(x))
                        except Exception:
                            types[_short(x)] = "receiver"
            return {"v": 3, "running": self.alive() and self.phase != "ended", "pid": os.getpid(),
                    "phase": self.phase, "started": self.started, "started_epoch": self.started_epoch,
                    "log_since": self.started_epoch,
                    "deadline": self.deadline, "hours": round((self.deadline - self.started_epoch) / 3600.0, 1),
                    "interval_min": round(self.interval_s / 60.0, 1), "trigger_db": self.trig,
                    "rounds": self.rounds, "failed_rounds": self.failed_rounds, "alerts_total": self.alerts_total,
                    "notes_total": self.notes_total,
                    "last_alert": self.last_alert, "suppressed": self.suppressed, "last_round": self.last_round,
                    "round_s": self.round_s, "sweeps_last": self.sweeps_last, "watch_since": self.watch_since,
                    "beat": self.beat, "doing": self.doing, "ended": self.ended,
                    "new_reference": self.new_reference, "refs": dict(self.refs), "ref_done": sorted(self.refs),
                    "late_refs": list(self.late_refs), "gaps_s": round(self.gaps_s),
                    "bands": [{"s": c["s"], "e": c["e"], "step": c["step"], "label": c["label"],
                               "dongles": [_short(x) for x in c["serials"]]} for c in self.plan["chunks"]],
                    "not_covered": self.plan["not_covered"], "partly_covered": self.plan["partly_covered"],
                    "only": self.only, "unwatched": list(self.unwatched),
                    "errors": [x for t, x in self.errors if time.time() - t < ROOM_ERR_AGE_S],
                    "tracker": self.tracker.state(), "accepted": self.tracker.accepted(),
                    "inventory": self.inventory, "phase_note": self.phase_note, "last_summary": self.last_summary,
                    "deaf": sorted(_short(k) for k, v in list(self.deaf.items()) if v >= 2),
                    "receivers": self.receivers(), "receiver_types": types}

    def _save(self) -> None:
        if self.shutting:                    # ARIS is going down: the state stays as the last sweep left it
            return
        if _ROOM is not None and _ROOM is not self:      # another watch has taken this one's place: its state
            return                                       # file is no longer this object's to write
        with self.save_lock:                 # (receivers save in parallel: the newest snapshot is written last)
            try:
                _room_state_write(self.snapshot())
            except OSError:
                pass

    def _backup(self) -> None:
        try:
            shutil.copyfile(ROOM_STATE, ROOM_STATE_BAK)
        except OSError:
            pass

    def _park(self) -> None:
        """ARIS is shutting down: do nothing more, change nothing (the restart carries the watch on)."""
        while True:
            time.sleep(3600)

    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="room-watch", daemon=True)
        self.thread.start()
        _room_guard_start()

    def stop(self) -> None:
        self.stop_evt.set()
        with self.cv:
            self.cv.notify_all()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=60)
        self.parked(45.0)                    # (a sweep in flight ends by itself: the dongles are free after this)
        with self.lock:
            if self.ended is None:
                self.ended = _now()
            if self.alive():                 # (its thread is still ending something: it is over all the same)
                self.phase = "ended"
        self._save()

    def hold(self, seconds=ROOM_HOLD_S) -> None:
        """Leave the radios alone for a while (Reset Scanner ends the sweeps in flight; nothing new is launched
        until the hold is over, and the sweeps it ended are made again)."""
        self.hold_until = time.time() + float(seconds)
        self.held_evt.set()

    def _holding(self) -> bool:
        return self.held_evt.is_set() or time.time() < self.hold_until + 2.0

    def parked(self, timeout=2.5) -> bool:
        """True once no receiver of the watch is inside a sweep (Reset Scanner waits for this before it looks at
        the dongles' locks)."""
        end = time.time() + float(timeout)
        while True:
            with self.lock:
                busy = [s for s, r in self.rx.items() if r["since"] is not None]
            if not busy:
                return True
            if time.time() >= end:
                return False
            time.sleep(0.1)

    def _err(self, text) -> None:
        with self.lock:
            self.errors.append((time.time(), f"{_now()[11:16]} {text}"[:180]))

    def _wait(self, until) -> None:
        """Sleep until `until` (or the deadline, or a stop) in short steps; the state is written about once a
        minute meanwhile, so another process and the guard see that the watch is alive. The time left is worked
        out once and counted down on the monotonic clock: a wall clock that is set back must not stop the rounds."""
        end = time.monotonic() + max(0.0, min(until, self.deadline) - time.time())
        saved = time.monotonic()
        while not self.stop_evt.is_set():
            left = end - time.monotonic()
            if left <= 0:
                return
            self.beat = time.time()
            if time.monotonic() - saved >= 60:
                saved = time.monotonic()
                self._save()
            self.stop_evt.wait(min(20.0, left))

    def gap(self, seconds, why, now=None) -> None:
        """Time in which nothing ran (the computer slept, or the program was held up): into the timeline, the
        chat and the alert address, and counted. A long watch must never look continuous when it was not."""
        now = time.time() if now is None else now
        with self.lock:
            self.gaps_s += float(seconds)
        clock = lambda t: time.strftime("%H:%M", time.localtime(t))
        text = (f"Nothing was watched for {_took(seconds)}, from {clock(now - seconds)} to {clock(now)}: "
                + ("the computer was asleep. Switch its sleep and its lid switch off for the length of the watch."
                   if why == "asleep" else "ARIS was not running (the program or the computer was restarted)."
                   if why == "down" else "ARIS was held up (the program was paused or the computer was too "
                   "busy)."))
        _room_log("health", text, what="gap", s=round(float(seconds)))
        _notice("Room watch: " + text, kind="roomwatch", level="alert")
        _room_push("Room watch: " + text, kind="health")
        self._save()

    # -- the run ----------------------------------------------------------------------------------------------
    def _run(self) -> None:
        self._save()
        said = 0.0
        while True:
            try:
                self._main()
                break
            except Exception as ex:                  # a watch must not die silently: start again, and say so
                if self.shutting:
                    self._park()
                log.error("room watch failed:\n%s", traceback.format_exc())
                self._err(f"an internal error ({type(ex).__name__})")
                if self.stop_evt.is_set():
                    break
                now = time.time()
                self.err_times = [t for t in self.err_times if now - t < 3600] + [now]
                n = len(self.err_times)
                if now - said >= 3600:               # (said once an hour; it never gives up by itself)
                    said = now
                    text = (f"Room watch: an internal error interrupted it ({type(ex).__name__}); it starts its "
                            f"loop again" + (f" (the {n}th time this hour)" if n > 1 else "") + ". If this keeps "
                            f"coming, stop the watch and carry it on.")
                    _room_log("health", text)
                    _notice(text, kind="roomwatch", level="alert")
                    _room_push(text, kind="health")
                self.restart_note = ""
                self._wait(time.time() + (30 if n <= 2 else 120 if n <= 5 else 600))
                if self.stop_evt.is_set():
                    break
        with self.lock:
            self.phase, self.ended = "ended", _now()
        with self.cv:
            self.cv.notify_all()
        self._save()
        _room_archive(self.snapshot())

    def _main(self) -> None:
        if self.carried:
            _room_log("start", f"Room watch carried on with the reference it had, for "
                      f"{(self.deadline - time.time()) / 3600.0:.1f} h more.")
        elif not self.resumed:
            n = len({x for c in self.plan["chunks"] for x in c["serials"]})
            _room_log("start", f"Room watch started for {(self.deadline - self.started_epoch) / 3600.0:g} h: "
                      f"{_plural(len(self.plan['chunks']), 'band')} on {_plural(n, 'receiver')}, a round about "
                      f"every {self.interval_s / 60.0:g} min.")
        elif self.restart_note:
            _room_log("note", self.restart_note)
        self.resumed, self.carried = True, False         # (an error that starts _main again is not a new start)
        if self.phase == "reference":
            self._reference({p for p in _room_pairs(self.plan) if _room_pair_key(p) not in self.refs})
            if self.stop_evt.is_set():
                _room_log("stop", "Room watch stopped while the reference was being recorded.")
                return
            with self.lock:
                self.phase, self.watch_since, self.new_reference = "watching", time.time(), False
            self._inventory()
            self._save()
            _room_log("note", "The quiet reference is recorded; the watch starts now."
                      + (f" {self.inventory['total']} steady signals were on the air: the report lists the ones "
                         f"no broadcast or phone band explains." if (self.inventory or {}).get("total") else ""))
            self._safely(self._heard_at_reference)
        elif self.inventory is None:
            self._inventory()
        if self.watch_since is None:
            self.watch_since = time.time()
        while not self.stop_evt.is_set() and time.time() < self.deadline:
            t0 = time.time()
            self._safely(self._replan)
            self._round()
            self._save()
            nxt = t0 + (min(60.0, self.interval_s) if self.fail_streak else self.interval_s)
            self._wait(max(nxt, time.time() + 5.0))
        done = time.time() >= self.deadline and not self.stop_evt.is_set()
        _room_log("stop", "Room watch finished: its time was up." if done else "Room watch stopped.")
        if done:
            text = ("The room watch finished its time. " + roomwatch.summary_line(self.snapshot())
                    + ' Say "room watch report" for the timeline.')
            _notice(text, kind="roomwatch", level="info")
            _room_push(text, kind="end")

    def _inventory(self) -> None:
        try:
            inv = _room_inventory(self.plan, self.refs)
        except Exception:
            log.error("room watch: inventory failed:\n%s", traceback.format_exc())
            inv = {"total": 0, "fm": 0, "tv": 0, "busy": 0, "own": 0, "listed": [], "more": 0}
        with self.lock:
            self.inventory = inv

    def _receivers(self, fn, queues, what) -> None:
        """Run fn(k, serial, items) for every receiver at the same time and wait for all of them (after a stop:
        not for long, a sweep that hangs ends by itself or with the program)."""
        threads = [threading.Thread(target=self._guarded, args=(fn, k, ser, queues[ser], what),
                                    name=f"room-{what}-{ser}", daemon=True)
                   for k, ser in enumerate(sorted(queues))]
        for t in threads:
            t.start()
        gone = None
        for t in threads:
            while t.is_alive():
                t.join(5.0)
                if self.stop_evt.is_set():
                    gone = gone or time.time()
                    if time.time() - gone > 20:
                        return

    def _guarded(self, fn, k, ser, items, what) -> None:
        _QUIET.stop = self.held_evt                    # (Reset Scanner's hold stops what this thread would launch)
        try:
            fn(k, ser, items)
        except Exception as ex:
            log.error("room watch %s on %s failed:\n%s", what, ser, traceback.format_exc())
            self._err(f"{what} on {_short(ser)}: an internal error ({type(ex).__name__})")

    def _held(self) -> None:
        while not self.stop_evt.is_set():
            left = self.hold_until - time.time()
            if left <= 0:
                self.held_evt.clear()
                return
            self.beat = time.time()
            self.stop_evt.wait(min(1.0, left))

    def _ref_made(self, c, ser) -> None:
        """A reference of this watch was recorded: remember when (its file's own time), so that nothing else is
        ever taken for it."""
        made = None
        try:
            found = _find_baseline(c["s"], c["e"], ser)
            made = float(found[1].get("created_epoch")) if found else None
        except (TypeError, ValueError):
            made = None
        with self.lock:
            self.refs[_room_pair_key((c["s"], c["e"], ser))] = made if made is not None else time.time()

    def _reference(self, pairs, late=False, quiet=False) -> int:
        """Record the reference of these (band, receiver) pairs: every receiver its own bands, all receivers at the
        same time. Returns how many could not be recorded (the rounds try those again and say when it worked)."""
        cfg = _load_config()
        dur = int(cfg.get("baseline_seconds", 30))
        work = set(_working_serials(cfg))
        queues, failed = {}, []
        for c in self.plan["chunks"]:
            for ser in c["serials"]:
                if (c["s"], c["e"], ser) in pairs:
                    queues.setdefault(ser, []).append(c)
        total = sum(len(v) for v in queues.values())
        if not total:
            return 0
        longest = max(len(v) for v in queues.values())
        if not late:
            _room_log("note", f"Recording the quiet reference: {_plural(total, 'sweep')} of about {dur} s on "
                      f"{_plural(len(queues), 'receiver')} (about {math.ceil(longest * (dur + 6) / 60.0)} min). Keep the room as "
                      f"quiet as it will be: phones off, no new equipment.")
        done = [0]

        def walk(k, ser, items):
            for c in items:
                for attempt in (0, 1):
                    if self.stop_evt.is_set() or self.shutting:
                        return
                    self._held()
                    self.beat, self.doing = time.time(), f"reference {c['s']:g}-{c['e']:g} MHz on {_short(ser)}"
                    rx = self._rx(ser)
                    rx["since"], rx["doing"] = time.time(), f"the reference of {c['s']:g}-{c['e']:g} MHz"
                    try:
                        if ser not in work:
                            raise RFError(f"{_short(ser)} is busy with a capture or not plugged in")
                        t_rec = time.time()
                        _record_one_baseline(ser, c["s"], c["e"], c["step"], dur, "room watch", cfg)
                        if self.hold_until > t_rec:      # Reset Scanner came meanwhile; a sweep program that
                            raise Stopped("cut short")   # ends cleanly on its signal leaves a short reference
                        self._ref_made(c, ser)
                        rx["since"] = None
                        break
                    except Exception as ex:
                        rx["since"] = None
                        if self.stop_evt.is_set() or self.shutting:
                            return
                        if attempt == 0 and (isinstance(ex, Stopped) or self._holding()):
                            continue                     # Reset Scanner ended it: once more after the hold
                        if not isinstance(ex, RFError):
                            log.error("room watch reference failed:\n%s", traceback.format_exc())
                        else:
                            log.warning("room watch reference %s-%s MHz on %s: %s", c["s"], c["e"], ser, ex)
                        failed.append(f"{c['s']:g}-{c['e']:g} MHz on {_short(ser)} ({_room_plain(ex)})")
                        break
                with self.lock:
                    done[0] += 1
                    self.phase_note = f"{done[0]} of {total} sweeps"
                self._save()

        self._receivers(walk, queues, "reference")
        if self.shutting:
            self._park()
        with self.lock:
            self.phase_note, self.doing = "", ""
        if failed and not self.stop_evt.is_set() and not quiet:
            for f in failed[:8]:
                self._err("reference " + f)
            text = (f"Room watch: {_plural(len(failed), 'reference sweep')} of {total} could not be recorded yet: "
                    + "; ".join(failed[:4]) + ("; ..." if len(failed) > 4 else "")
                    + ". They are tried again in the next rounds (after three tries, every half hour); until one "
                      "works, that band is not watched on that receiver.")
            _room_log("health", text)
            _notice(text, kind="roomwatch", level="alert")
            _room_push(text, kind="health")
        return len(failed)

    def _heard_at_reference(self) -> None:
        """Did every receiver hear at the reference? One whose reference shows no steady signal at all in the
        bands it shares with a receiver that shows strong ones has no antenna (or one for other bands), and would
        sweep those bands for days seeing nothing. A weaker antenna still shows the strong signals weakly, and is
        not meant. A receiver with no strong signal of its own in its reference is noted: nothing would show
        that its antenna came off later."""
        strong, seen, own = {}, {}, {}
        for c in self.plan["chunks"]:
            for ser in c["serials"]:
                if _room_pair_key((c["s"], c["e"], ser)) not in self.refs:
                    continue                             # (only this watch's own references say anything)
                found = _find_baseline(c["s"], c["e"], ser)
                rows = [p for p in ((found[1].get("summary") or {}).get("steady") or [])
                        if c["s"] <= float(p.get("mhz") or 0) <= c["e"]] if found else []
                n = sum(1 for p in rows if float(p.get("snr_db") or 0) >= ROOM_PILOT_DB)
                own[ser] = own.get(ser, 0) + n
                if len(c["serials"]) > 1:
                    seen[ser] = seen.get(ser, 0) + len(rows)
                    strong[ser] = strong.get(ser, 0) + n
        said = set()
        if strong:
            best_ser = max(strong, key=lambda s: strong[s])
            best = strong[best_ser]
            for ser, n in sorted(seen.items()):
                if n == 0 and best >= 2:
                    said.add(ser)
                    text = (f"Room watch: the reference of {_short(ser)} shows no steady signal at all in the bands "
                            f"it shares with {_short(best_ser)}, which shows {best} strong ones. If its antenna is "
                            f"made for other bands, this is as expected; if not, check its antenna and cable: as "
                            f"it is, its sweeps of those bands see next to nothing.")
                    _room_log("health", text, what="receiver")
                    _notice(text, kind="roomwatch", level="alert")
                    _room_push(text, kind="health")
        quiet = sorted(_short(s) for s, n in own.items() if n == 0 and s not in said)
        if quiet:
            _room_log("note", f"No strong steady signal is in the reference of {_and_list(quiet)}: an antenna that "
                      f"comes off {'it' if len(quiet) == 1 else 'one of them'} during the watch would not be "
                      f"noticed by the watch itself.")

    def _replan(self) -> None:
        """Before a round: a receiver added since the watch began (plugged in, then /check) joins it; its
        reference is recorded by its own thread in the next rounds, while the others keep sweeping, and said to
        be late. Receivers that went away stay in the plan (they are then reported as not working); a forgotten
        one leaves."""
        cfg = _load_config()
        known = set(DONGLE_NAMES)
        have = {x for c in self.plan["chunks"] for x in c["serials"]}
        gone = [s for s in have if _dongle_cfg(s, cfg).get("present") is False]
        if gone and self.round_no % 2 == 0:              # marked as unplugged: is it back? (the list opens none)
            back = {str(d.get("serial")) for d in _list_devices()} & set(gone)
            for s in back:
                _set_dongle(s, present=True)
            if back:
                cfg = _load_config()
        usable = set(_working_serials(cfg)) | (have & known)
        serials = [s for s in DONGLE_NAMES if s in usable]
        try:
            new = _room_plan(serials, cfg, self.only)
        except Exception:
            log.error("room watch: re-plan failed:\n%s", traceback.format_exc())
            return
        if not new["chunks"]:
            return
        old_pairs, new_pairs = _room_pairs(self.plan), _room_pairs(new)
        if new_pairs == old_pairs:
            return
        added = new_pairs - old_pairs
        with self.lock:
            self.plan = new
        if added:
            joined = sorted({_short(p[2]) for p in added if p[2] not in have})
            late = [p for p in added if _room_pair_key(p) not in self.refs]
            per = max([sum(1 for p in late if p[2] == s) for s in {p[2] for p in late}] or [0])
            mins = math.ceil(per * (int(cfg.get("baseline_seconds", 30)) + 6) / 60.0)
            text = ((f"{_and_list(joined)} joined the room watch" if joined else "The room watch's bands changed")
                    + f": {_plural(len(added), 'more sweep')}."
                    + (f" Their reference is recorded now, in the next rounds (about {mins} min), while the other "
                       f"receivers keep sweeping: what is on the air then is taken as normal there." if late else ""))
            _room_log("note", text)
            _notice("Room watch: " + text, kind="roomwatch", level="info")
        self._save()

    # -- the receivers' own threads -----------------------------------------------------------------------------
    def _walkers(self, late=False) -> list:
        """Every receiver of the plan has its own thread; one that ended (or never ran) is started. Returns the
        plan's receivers. `late`: the round has begun already, so a thread started now walks at once."""
        with self.lock:
            want = sorted({x for c in self.plan["chunks"] for x in c["serials"]})
            for ser in want:
                r = self._rx(ser)
                t = r["thread"]
                if t is None or not t.is_alive():
                    r["lap_round"] = r["done"] = self.round_no - (1 if late else 0)
                    r["since"] = None
                    t = r["thread"] = threading.Thread(target=self._walker, args=(ser,), name=f"room-sweep-{ser}",
                                                       daemon=True)
                    t.start()
        return want

    def _walker(self, ser) -> None:
        """One receiver's run: at every new round it walks its list once; a walk that takes longer than a round
        simply goes on, and the next one follows at once."""
        _QUIET.stop = self.held_evt                    # (Reset Scanner's hold stops what this thread would launch)
        r = self._rx(ser)
        idle = time.monotonic()
        while not self.stop_evt.is_set() and not self.shutting and self.phase != "ended":
            with self.cv:
                if self.round_no <= r["lap_round"]:
                    self.cv.wait(2.0)
                    if self.round_no <= r["lap_round"] and time.monotonic() - idle > max(900.0, 3.0 * self.interval_s):
                        return                         # (no round for a long time: the next round starts it again)
                    continue
                r["lap_round"] = self.round_no
            try:
                self._lap(ser, r)
            except Exception as ex:
                log.error("room watch sweeps on %s failed:\n%s", ser, traceback.format_exc())
                self._err(f"sweeps on {_short(ser)}: an internal error ({type(ex).__name__})")
                with self.lock:
                    r["win"]["failed"] += 1
                    r["err"] = f"an internal error ({type(ex).__name__})"
            with self.lock:
                r["done"], r["since"], r["doing"] = r["lap_round"], None, ""
                r["laps"] += 1
            idle = time.monotonic()

    def _lap_list(self, ser, r, chunks, secs) -> list:
        """The sweeps of one walk. Normally all of the receiver's bands, starting where the other receivers do
        not. A receiver that cannot get through all of them in a round (one that tunes far more than the others)
        takes the bands only it tunes every time and as many of the shared ones as fit, in turn."""
        mine = [c for c in chunks if ser in c["serials"]]
        own = [c for c in mine if len(c["serials"]) == 1]
        shared = [c for c in mine if len(c["serials"]) > 1]
        est = float(r.get("sweep_s") or (secs + 3.0))
        if own and shared and len(mine) * est > self.interval_s * 1.1:
            n = max(1, min(len(shared), int((self.interval_s - len(own) * est) / est)))
            p = int(r.get("ptr") or 0) % len(shared)
            r["ptr"] = (p + n) % len(shared)
            return own + (shared + shared)[p:p + n]
        order = sorted({x for c in chunks for x in c["serials"]})
        off = (len(mine) * (order.index(ser) if ser in order else 0)) // max(1, len(order))
        return mine[off:] + mine[:off]

    def _lap(self, ser, r) -> None:
        cfg = _load_config()
        secs = int(cfg.get("audit_seconds", 10))
        with self.lock:
            chunks = list(self.plan["chunks"])
        usable = ser in set(_working_serials(cfg))
        late, t0, whole = [], time.time(), True
        for c in self._lap_list(ser, r, chunks, secs):
            if self.stop_evt.is_set() or self.shutting or time.time() >= self.deadline:
                whole = False
                break
            self._held()
            label = f"{c['s']:g}-{c['e']:g} MHz"
            ts = time.time()
            with self.lock:
                r["since"], r["doing"] = ts, label
                self.doing = f"{label} on {_short(ser)}"
            outcome = self._sweep(c, ser, secs, usable)
            if outcome == "held":                       # Reset Scanner ended it: once more after the hold
                with self.lock:
                    r["since"] = None                   # (it waits: Reset Scanner must not wait for it)
                self._held()
                if self.stop_evt.is_set():
                    outcome = "failed"
                else:
                    with self.lock:
                        r["since"] = time.time()
                    outcome = self._sweep(c, ser, secs, usable)
                    outcome = "failed" if outcome == "held" else outcome
            if outcome == "noref" and usable:
                outcome = self._late_reference(c, ser, cfg, late)
            now = time.time()
            with self.lock:
                r["win"][outcome] += 1
                r["since"], r["doing"] = None, ""
                if outcome == "ok":
                    self.chunk_ok[f"{c['s']:g}-{c['e']:g}"] = now
                    r["last_ok"] = now
                    r["sweep_s"] = (now - ts) if not r.get("sweep_s") else 0.7 * r["sweep_s"] + 0.3 * (now - ts)
            if outcome == "failed":
                self._save()
        if whole:
            r["lap_s"] = round(time.time() - t0, 1)
        if late:
            self._late_said(ser, late)

    def _late_reference(self, c, ser, cfg, late) -> str:
        """A band of this receiver that has no reference of this watch (it joined later, or its reference could not
        be recorded at the start): record it now. A few tries a round apart, then one every half hour for as long
        as the watch runs: a receiver that works again must get its reference without anyone restarting the watch.
        Returns ref / noref / failed."""
        key = _room_pair_key((c["s"], c["e"], ser))
        now = time.time()
        tries = self.ref_tries.get(key, 0)
        if tries >= ROOM_REF_TRIES and now - self.ref_last.get(key, 0.0) < ROOM_REF_AGAIN_S:
            if key not in self.no_ref:                   # said once, loudly: this band is blind on this receiver
                self.no_ref.add(key)
                why = self._rx(ser)["err"] or "it could not be recorded"
                text = (f"Room watch: the reference for {c['s']:g}-{c['e']:g} MHz on {_short(ser)} could not be "
                        f"recorded in {tries} tries ({why}). That band is not watched on that receiver for now; "
                        f"it is tried again every half hour.")
                self._err(f"{c['s']:g}-{c['e']:g} MHz on {_short(ser)} has no reference and is not watched there")
                _room_log("health", text, what="receiver")
                _notice(text, kind="roomwatch", level="alert")
                _room_push(text, kind="health")
            return "noref"
        self.ref_tries[key] = tries + 1
        self.ref_last[key] = now
        with self.lock:
            self._rx(ser)["doing"] = f"the reference of {c['s']:g}-{c['e']:g} MHz"
        try:
            _record_one_baseline(ser, c["s"], c["e"], c["step"], int(cfg.get("baseline_seconds", 30)), "room watch",
                                 cfg)
            if self.hold_until > now:                    # Reset Scanner came meanwhile: a reference cut short is
                raise Stopped("cut short")               # none (the sweep program may end cleanly on its signal)
        except Exception as ex:
            if isinstance(ex, Stopped) or self._holding():
                self.ref_tries[key] = tries              # (Reset Scanner ended it: that was no try)
                self.ref_last.pop(key, None)
                return "noref"
            if not isinstance(ex, RFError):
                log.error("room watch late reference failed:\n%s", traceback.format_exc())
            else:
                log.warning("room watch late reference %s-%s MHz on %s: %s", c["s"], c["e"], ser, ex)
            with self.lock:
                self._rx(ser)["err"] = _room_plain(ex)
            return "failed"
        self._ref_made(c, ser)
        self.ref_tries.pop(key, None)
        self.no_ref.discard(key)
        late.append(c)
        return "ref"

    def _late_said(self, ser, late) -> None:
        """References recorded after the watch began: one row for a walk's worth, and kept for the report (what was
        on the air at that moment is normal there, which the reader must be able to see)."""
        at = _now()[:16]
        with self.lock:
            for c in late:
                self.late_refs.append({"s": c["s"], "e": c["e"], "dongle": _short(ser), "time": at})
            self.late_refs = self.late_refs[-200:]
        spans = _room_merged([(c["s"], c["e"]) for c in late])
        text = (f"The reference for {'; '.join(f'{a:g}-{b:g} MHz' for a, b in spans[:6])}"
                + ("; ..." if len(spans) > 6 else "") + f" on {_short(ser)} ({_plural(len(late), 'sweep')}) was "
                f"recorded late, at {at[11:]}: what was on the air then is taken as normal there.")
        _room_log("late", text, what="late")
        _notice("Room watch: " + text, kind="roomwatch", level="info")
        self._inventory()
        self._save()

    # -- a round: the bookkeeping step ---------------------------------------------------------------------------
    def _round(self) -> None:
        t0, m0 = time.time(), time.monotonic()
        with self.lock:
            self.pilots, self.looks = {}, {}
        self._walkers()
        with self.cv:
            self.round_no += 1
            no = self.round_no
            self.cv.notify_all()
        want = self._walkers(late=True)                # (one that left just before the round began walks too)
        saved = m0
        while not self.stop_evt.is_set() and not self.shutting:
            with self.lock:
                walking = [s for s in want if self.rx[s]["thread"].is_alive() and self.rx[s]["done"] < no]
            if not walking or time.monotonic() - m0 >= self.interval_s or time.time() >= self.deadline:
                break                                  # (a receiver still walking goes on; the round is counted)
            self.beat = time.time()
            if time.monotonic() - saved >= 30:
                saved = time.monotonic()
                self._save()
            self.stop_evt.wait(0.1 if time.monotonic() - m0 < 20 else 1.0)
        if self.shutting:
            self._park()
        if self.stop_evt.is_set():
            return
        now = time.time()
        stats = {"ok": 0, "total": 0, "busy": 0, "noref": 0, "failed": 0, "ref": 0}
        with self.lock:
            wins = {s: self.rx[s]["win"] for s in want}
            for s in want:                             # a new window at once: a sweep that ends between two
                self.rx[s]["win"] = _room_win()        # rounds is counted in the next one, never dropped
            for w in wins.values():
                for k in ("ok", "busy", "noref", "failed", "ref"):
                    stats[k] += w[k]
            stats["total"] = stats["ok"] + stats["busy"] + stats["noref"] + stats["failed"]
            self.doing = ""
            self.sweeps_last = [stats["ok"], stats["total"]]
            if stats["ok"] or (stats["ref"] and not stats["total"]):
                self.rounds += 1 if stats["ok"] else 0
                self.fail_streak = 0
                self.last_round = _now()
                self.round_s = round(now - t0, 1)
            elif stats["total"] or not self._holding():
                self.failed_rounds += 1
                self.fail_streak += 1
        busy_now = {s for s, w in wins.items() if w["busy"] and not w["ok"]}
        if stats["busy"] and stats["ok"] and busy_now != self.busy_last:
            _room_log("skip", f"{_plural(stats['busy'], 'sweep')} of {stats['total']} skipped this round: "
                      f"{_and_list(sorted(_short(s) for s in busy_now)) or 'a receiver'} was busy with something "
                      f"else or not plugged in.")
        self.busy_last = busy_now
        self._safely(self._rx_health, wins, now)
        self._safely(self._health, stats, now)
        self._safely(self._hearing, now)
        self._safely(self._summary, now)
        if stats["ok"]:
            self._save()
            self._backup()

    def _safely(self, fn, *args) -> None:
        """The watch's housekeeping (health, hearing, the summary, the plan) must never end the watch: an error
        there is logged and the sweeps go on."""
        try:
            fn(*args)
        except Exception as ex:
            log.error("room watch: %s failed:\n%s", getattr(fn, "__name__", "a step"), traceback.format_exc())
            self._err(f"{getattr(fn, '__name__', 'a step').strip('_')}: an internal error ({type(ex).__name__})")

    def _only(self, ser) -> list:
        """The ranges only this receiver tunes in the plan, merged."""
        return _room_merged([(c["s"], c["e"]) for c in self.plan["chunks"] if c["serials"] == [ser]])

    def _rx_health(self, wins, now) -> None:
        """Each receiver after a round: one with no working sweep two rounds running is named at once (chat, page,
        alert address), with what it means for the bands; one that works again is said too."""
        cfg = _load_config()
        some = any(w["ok"] or w["ref"] for w in wins.values())
        for ser, w in sorted(wins.items()):
            r = self._rx(ser)
            name = _short(ser)
            tried = w["ok"] + w["busy"] + w["noref"] + w["failed"] + w["ref"]
            d = _dongle_cfg(ser, cfg)
            absent = d.get("present") is False or d.get("usb_ok") is False
            if r["since"] is not None and now - r["since"] > ROOM_RX_STALL_S:
                state, note = "slow", (f"{name} has not finished its sweep of {r['doing']} for "
                                       f"{_took(now - r['since'])}.")
            elif not tried:
                continue                                  # (in the middle of a long sweep: nothing to judge yet)
            elif w["ok"]:
                state, note = "working", ""
            elif w["ref"]:
                state, note = "recording", f"{name} is recording its reference."
            elif w["busy"] == tried and not absent:
                state, note = "busy", f"{name} is busy with something else (a capture?)."
            else:
                why = ("it is not plugged in or was not detected" if absent and not w["failed"]
                       else "it has no reference for its bands" if w["noref"] == tried
                       else (r["err"] or "its sweeps fail"))
                state, note = "failing", f"{name} does not work: {why}."
            was = r["state"]
            with self.lock:
                r["state"], r["note"] = state, note
                r["bad"] = r["bad"] + 1 if state in ("failing", "slow") else 0
            if state in ("failing", "slow") and some \
                    and (r["bad"] == 2 or (r["bad"] > 2 and now - r["said"] >= ROOM_HEALTH_EVERY_S)):
                r["said"] = now
                only = self._only(ser)
                others = sorted({_short(x) for c in self.plan["chunks"] if ser in c["serials"]
                                 for x in c["serials"] if x != ser})
                text = (f"Room watch: {note[:-1]}, {r['bad']} rounds running. "
                        + (f"Only it tunes {'; '.join(f'{a:g}-{b:g} MHz' for a, b in only[:4])}"
                           f"{'; ...' if len(only) > 4 else ''}: nothing is watched there meanwhile. " if only else "")
                        + (f"The bands it shares are still checked by {_and_list(others)}. " if others else "")
                        + ("Press Reset Scanner to end the stuck sweep." if state == "slow"
                           else "Its reference is tried again every half hour." if w["noref"] == tried
                           else "Check its cable and plug; it is tried again every round."))
                _room_log("health", text, what="receiver")
                _notice(text, kind="roomwatch", level="alert")
                _room_push(text, kind="health")
            elif state == "working" and was in ("failing", "slow") and r["said"]:
                r["said"] = 0.0
                text = f"Room watch: {name} works again."
                _room_log("note", text)
                _notice(text, kind="roomwatch", level="info")

    def _hearing(self, now) -> None:
        """Can each receiver still hear? The strong signals of its reference are its pilots: when all of them are
        far down two rounds running, its antenna or cable is off, and its sweeps see nothing."""
        with self.lock:
            pilots, self.pilots = self.pilots, {}
        for ser, rows in pilots.items():
            bins = sum(n for n, _ in rows)
            if bins < ROOM_DEAF_BINS:
                continue
            down = sum(n * m for n, m in rows) / bins
            if down <= ROOM_DEAF_DB:
                self.deaf[ser] = self.deaf.get(ser, 0) + 1
                if self.deaf[ser] >= 2 and now - self.deaf_said.get(ser, 0.0) >= ROOM_HEALTH_EVERY_S:
                    self.deaf_said[ser] = now
                    text = (f"Room watch: {_short(ser)} no longer hears the strong signals it heard at the "
                            f"reference (they read {abs(down):.0f} dB lower). Check its antenna and cable: its "
                            f"sweeps are blind until then. If those signals have simply gone off the air, this "
                            f"passes by itself.")
                    _room_log("health", text, what="receiver")
                    _notice(text, kind="roomwatch", level="alert")
                    _room_push(text, kind="health")
            elif self.deaf.get(ser, 0) >= 2:
                self.deaf[ser] = 0
                self.deaf_said.pop(ser, None)
                text = f"Room watch: {_short(ser)} hears its reference's strong signals again."
                _room_log("note", text)
                _notice(text, kind="roomwatch", level="info")
            else:
                self.deaf[ser] = 0

    def _summary(self, now) -> None:
        """A short "all is well" at fixed hours: someone who is not looking cannot tell a quiet watch from a dead
        one, so the line arriving is the sign of life (and its absence the warning)."""
        lt = time.localtime(now)
        due = None
        for back in (0, 1):
            day = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday - back, 0, 0, 0, 0, 0, -1))
            for h in sorted(ROOM_SUMMARY_HOURS, reverse=True):
                t = day + h * 3600
                if t <= now and (due is None or t > due):
                    due = t
        if due is None or self.last_summary >= due or float(self.watch_since or now) > due or now - due > 6 * 3600:
            if due is not None and self.last_summary < due and now - due > 6 * 3600:
                self.last_summary = due
            return
        self.last_summary = due
        st = self.snapshot()
        good = [x["name"] for x in st["receivers"] if x["state"] in ("working", "recording")]
        bad = sorted(set(st.get("deaf") or []) | {x["name"] for x in st["receivers"]
                                                   if x["state"] in ("failing", "slow", "busy")})
        unseen = len(_room_unseen())
        text = (f"Room watch at {time.strftime('%H:%M', lt)}: running since {self.started[:16]}. "
                + roomwatch.summary_line(st)
                + (f" Not checked for a while: {'; '.join(st['unwatched'][:3])}." if st.get("unwatched")
                   else " Every band the receivers tune was checked in the last half hour.")
                + (f" Not covered by any receiver here: {'; '.join(st['not_covered'][:3])}." if st.get("not_covered")
                   else "")
                + (f" Working: {_and_list(good)}." if good else "")
                + (f" Not working or not hearing: {_and_list(bad)}." if bad else "")
                + (f" Not watched for {_took(self.gaps_s)} in total." if self.gaps_s >= 60 else "")
                + (f" {_plural(unseen, 'alert')} not yet marked as seen." if unseen else ""))
        _room_log("note", text, what="summary")
        _notice(text, kind="roomwatch", level="info")
        _room_push(text, kind="open" if (bad or unseen or st.get("unwatched")) else "summary")

    def _health(self, stats, now) -> None:
        """After a round: say it loudly when nothing worked twice in a row, or when a band has not been checked
        for a long time (again when more bands join them); say it once when all is well again."""
        since = max(float(self.watch_since or now), self.born)
        limit = max(ROOM_UNWATCHED_S, 3.0 * self.interval_s)
        names, spans = [], set()
        for c in self.plan["chunks"]:
            age = now - self.chunk_ok.get(f"{c['s']:g}-{c['e']:g}", since)
            if age > limit:
                names.append(f"{c['s']:g}-{c['e']:g} MHz ({_took(age)})")
                spans.add(f"{c['s']:g}-{c['e']:g}")
        was = bool(self.unwatched)
        with self.lock:
            self.unwatched = names
        due = now - self.health_t >= ROOM_HEALTH_EVERY_S
        text = None
        if stats["total"] and not stats["ok"] and (self.fail_streak == 2 or (self.fail_streak > 2 and due)):
            why = ("the receivers are busy with something else or not plugged in" if stats["busy"] >= stats["total"]
                   else "there is no reference for these bands" if stats["noref"] >= stats["total"]
                   else (self.errors[-1][1] if self.errors else "the sweeps failed"))
            text = (f"Room watch: no sweep worked in the last {self.fail_streak} rounds ({why}). Nothing is being "
                    f"watched right now; "
                    + ("the references are tried again every half hour." if stats["noref"] >= stats["total"]
                       else "it keeps trying."))
        elif names and stats["ok"] and (due or spans - self.unwatched_said):
            text = ("Room watch: not checked for a while: " + "; ".join(names[:6]) + ("; ..." if len(names) > 6 else "")
                    + ". A receiver is busy, unplugged or failing"
                    + (f" (last error: {self.errors[-1][1]})" if self.errors else "") + ".")
        if text:
            self.health_t = now
            self.unwatched_said = set(spans)
            self.dead_said = self.dead_said or not stats["ok"]
            _room_log("health", text)
            _notice(text, kind="roomwatch", level="alert")
            _room_push(text, kind="health")
        elif self.dead_said and stats["ok"]:              # whoever was woken must be able to see that it is over
            self.dead_said = False
            self.health_t = 0.0
            text = "Room watch: its sweeps work again" + (" and all bands are being checked again."
                                                           if not names else ".")
            _room_log("note", text)
            _notice(text, kind="roomwatch", level="info")
            _room_push(text, kind="summary")
        elif was and not names and stats["ok"]:
            self.health_t = 0.0
            self.unwatched_said = set()
            _room_log("note", "All bands are being checked again.")
            _notice("Room watch: all bands are being checked again.", kind="roomwatch", level="info")

    def _sweep(self, c, ser, secs, usable) -> str:
        """One sweep of one band on one receiver against its own reference of this watch. Returns ok / busy /
        noref / failed / held (Reset Scanner ended it)."""
        if not usable:
            return "busy"
        pair = (c["s"], c["e"], ser)
        key = _room_pair_key(pair)
        if key not in self.refs:
            return "noref"
        span = f"{c['s']:g}-{c['e']:g}"
        try:
            rep = _audit(c["s"], c["e"], ser, self.trig, secs, lock_timeout=3.0, take_lift=True)
        except RFError as ex:
            msg = str(ex)
            if isinstance(ex, Stopped) or self._holding():
                return "held"
            if "no baseline" in msg or "share no frequency bins" in msg:
                with self.lock:
                    self.refs.pop(key, None)            # (its file is gone: recorded again, and said to be late)
                return "noref"
            if "is busy" in msg:
                return "busy"
            log.warning("room watch sweep %s MHz on %s: %s", span, ser, msg)
            if not self.stop_evt.is_set():
                plain = _room_plain(ex)
                self._err(f"{span} MHz on {_short(ser)}: {plain}")
                with self.lock:
                    self._rx(ser)["err"] = plain
            return "failed"
        except Exception as ex:
            log.error("room watch sweep failed:\n%s", traceback.format_exc())
            self._err(f"{span} MHz on {_short(ser)}: an internal error ({type(ex).__name__})")
            with self.lock:
                self._rx(ser)["err"] = f"an internal error ({type(ex).__name__})"
            return "failed"
        if self.stop_evt.is_set():                       # stopped (or replaced) meanwhile: nothing more is told
            return "failed"
        now = time.time()
        # the reference this sweep was judged against must be the one this watch recorded
        made, want = rep.get("baseline_created_epoch"), self.refs.get(key)
        if made is not None:
            if want is None:
                with self.lock:
                    self.refs[key] = float(made)
            elif abs(float(made) - float(want)) > 2.0:
                with self.lock:
                    self.refs[key] = float(made)
                    self.late_refs.append({"s": c["s"], "e": c["e"], "dongle": _short(ser),
                                           "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(float(made)))})
                text = (f"Room watch: the reference for {span} MHz on {_short(ser)} was replaced at "
                        f"{time.strftime('%H:%M', time.localtime(float(made)))} by a baseline recorded outside the "
                        f"watch (a baseline recording or a setup run). What was on the air then counts as normal "
                        f"there from now on.")
                _room_log("health", text, what="late")
                _notice(text, kind="roomwatch", level="alert")
                _room_push(text, kind="health")
        pil = rep.get("pilots") or {}
        if pil.get("n") and pil.get("median_db") is not None:
            with self.lock:
                self.pilots.setdefault(ser, []).append((int(pil["n"]), float(pil["median_db"])))
        events = _merge_events({ser: rep})
        folded = _collapse_hopping(list(events), c["s"], c["e"])
        hop = next((e for e in folded if e.get("pattern") == "frequency hopping"), None)
        kept = {id(e) for e in folded}
        lift = float(rep.get("floor_lift_db") or 0.0)
        shift = float(rep.get("floor_shift_db") or 0.0)
        top = max((float(e.get("surge_delta_db") or 0.0) for e in events), default=0.0)
        # a lifted sweep is an overloaded receiver only with a cause in sight (a very strong signal, or many at
        # once); a noise floor that rose by itself (equipment switched on) is judged as it is, and noted
        overload = lift >= ROOM_LIFT_DB and (int(rep.get("flagged") or 0) >= 6 or top >= ROOM_LIFT_CAUSE_DB)
        for e in events:
            try:
                f = float(e["peak_mhz"])
                e["busy"], e["sensor"] = _in_busy_band(f), _in_sensor_band(f)
            except (KeyError, TypeError, ValueError):
                pass
            e["quiet"] = id(e) not in kept          # one of many bursts on many frequencies: recorded, not told
            e["overload"] = overload
        with self.lock:
            alerts = self.tracker.update(events)
            if hop and now - self.hop_said.get(span, 0.0) >= roomwatch.REPEAT_AFTER_S:
                self.hop_said[span] = now
                alerts.append({"kind": "hopping", "level": "note", "mhz": hop.get("peak_mhz"), "text":
                               f"Several short bursts on different frequencies in {span} MHz on {_short(ser)} "
                               f"({hop.get('channels', 'several')} of them): frequency hopping looks like this, "
                               f"and so do several unrelated devices."})
            if overload and now - self.lift_said.get((span, ser), 0.0) >= roomwatch.REPEAT_AFTER_S:
                self.lift_said[(span, ser)] = now
                alerts.append({"kind": "lift", "level": "alert", "mhz": None, "text":
                               f"The whole {span} MHz sweep on {_short(ser)} reads {lift:.0f} dB over its reference "
                               f"since {_now()[11:16]}: a very strong transmitter is close to its antenna (a phone "
                               f"or a radio within a metre or two), or the receiver is overloaded. Signals seen in "
                               f"this state may be the receiver's own. Worth a look."})
            elif lift >= ROOM_LIFT_DB and not overload \
                    and now - self.noise_said.get((span, ser), 0.0) >= ROOM_NOISE_NOTE_S:
                self.noise_said[(span, ser)] = now
                alerts.append({"kind": "noise", "level": "note", "mhz": None, "text":
                               f"The noise in {span} MHz on {_short(ser)} is {lift:.0f} dB higher than at the "
                               f"reference (equipment switched on nearby does this): the watch is that much less "
                               f"sensitive there for now."})
            low = self.low.get((span, ser), 0) + 1 if shift <= ROOM_LOW_DB else 0
            self.low[(span, ser)] = low
            if low == 2 and now - self.noise_said.get((span, ser, "low"), 0.0) >= 4 * ROOM_NOISE_NOTE_S:
                self.noise_said[(span, ser, "low")] = now
                alerts.append({"kind": "noise", "level": "note", "mhz": None, "text":
                               f"The noise in {span} MHz on {_short(ser)} is {abs(shift):.0f} dB lower than when "
                               f"the reference was recorded: the reference was noisy there, so a new signal needs "
                               f"to be that much stronger to count. A new watch started in the quiet room fixes "
                               f"it."})
        if alerts:
            told = [self._tell(al) for al in alerts]      # the rows first: a crash must never lose an alert
            self._save()
            for al, quiet in zip(alerts, told):
                self._look(al, events, ser, quiet)
        return "ok"

    def _tell(self, al) -> bool:
        """An alert or a note into the timeline, the chat and the alert address. Returns whether it was held back
        from the chat (too many this hour)."""
        text = al["text"]
        _room_log("alert" if al["level"] == "alert" else "note", text, mhz=al["mhz"], what=al["kind"],
                  **{k: al[k] for k in ("over_db", "by", "bw_khz", "pattern") if al.get(k) not in (None, {}, "")})
        if al["level"] != "alert":
            with self.lock:
                self.notes_total += 1
            return True
        now = time.time()
        strong = float(al.get("over_db") or 0.0) >= roomwatch.STRONG_DB
        with self.lock:
            self.alerts_total += 1
            self.last_alert = _now()[11:16]
            recent = [t for t in self.notice_times if now - t < 3600]
            # past the hourly number the rest are only logged; a strong one still goes out, up to twice the number
            quiet = len(recent) >= (2 * ROOM_NOTICES_PER_HOUR if strong else ROOM_NOTICES_PER_HOUR)
            first = False
            if quiet:
                self.suppressed += 1
                first = now - self.many_said >= 3600
                if first:
                    self.many_said = now
            else:
                self.notice_times.append(now)
        if quiet and first:
            many = ("Room watch: many alerts this hour. The rest are logged; open the Activity tab or say "
                    "\"room watch report\" to read them.")
            _notice(many, kind="roomwatch", level="alert")
            _room_push(many, kind="alert")
        elif not quiet:
            _notice("Room watch: " + text, kind="roomwatch", level="alert", mhz=al["mhz"])
            _room_push("Room watch: " + text, kind="alert")
        return quiet

    def _look(self, al, events, ser, quiet) -> None:
        """What the signal is, if a quick look can tell: a row of its own after the alert."""
        if al["level"] != "alert" or al["kind"] not in ("new", "surge", "stronger", "held", "recurring") \
                or al.get("mhz") is None:
            return
        look = self._check(al["mhz"], events, ser, al["kind"])
        if look:
            line = f"Quick look at {float(al['mhz']):g} MHz: {look}"
            _room_log("note", line, mhz=al["mhz"], what="look")
            if not quiet:
                _notice("Room watch: " + line, kind="roomwatch", level="info", mhz=al["mhz"])

    def _check(self, mhz, events, ser, kind="new") -> str:
        """One quick look at a new carrier (once an hour per frequency), on the receiver that heard it: what it is,
        if ARIS can tell."""
        key = round(float(mhz), 2)
        with self.lock:
            if time.time() - self.checked.get(key, 0.0) < ROOM_CHECK_EVERY_S or self.stop_evt.is_set() \
                    or self.shutting or self._holding() or self.looks.get(ser, 0) >= ROOM_LOOKS_PER_ROUND:
                return ""
            self.checked[key] = time.time()
            self.looks[ser] = self.looks.get(ser, 0) + 1
        ev = next((e for e in events if abs(float(e["peak_mhz"]) - float(mhz)) < 0.05), None) or {}
        peak = {"peak_id": 1, "frequency_mhz": mhz, "occupied_bw_khz": ev.get("bandwidth_khz", 25.0),
                "probable_allocation": ev.get("hint")}
        try:
            _QUIET.on = True
            res = _investigate(peak, [ser], listen_s=12, timed=False)
            verdict = str(res.get("verdict_core") or res["final_signal_verdict"])
            if res.get("evidence") in (None, "none") or verdict.startswith("Decoding was not possible"):
                # the look found nothing (the signal went off again, or this receiver's look is too coarse for
                # it): that must not read as if the sweep had been wrong
                return (f"the look on {_short(ser)} a moment later did not find it (it went off again, or it is "
                        f"too weak for a look). The sweep's reading stands.")
            tail = _sentence(_cut_at_word(verdict, 200))
            if kind == "new" and _in_fm_band(float(mhz)):    # (a look alone reads a new FM carrier as "a station")
                tail += (" It was not on the air when the reference was recorded, and a broadcast station rarely "
                         "starts in the middle of a watch: a simple FM transmitter nearby looks like this.")
            return tail
        except Exception as ex:                       # a receiver that cannot do the look (a wide IQ rate, busy)
            log.info("room watch: quick look at %s MHz not possible: %s", mhz, ex)
            if isinstance(ex, Stopped) or self._holding():    # Reset Scanner ended it: the look is still owed
                with self.lock:
                    self.checked.pop(key, None)
                    self.looks[ser] = max(0, self.looks.get(ser, 1) - 1)
            return ""
        finally:
            _QUIET.on = False

    def accept(self, mhz, label) -> dict:
        with self.lock:
            rec = self.tracker.accept(mhz, label)
        _room_log("mine", f"{float(rec['mhz']):g} MHz accepted as known: {rec['mine']}.", mhz=rec["mhz"])
        self._save()
        return rec


def _room_archive(st) -> str:
    """The finished watch's report as a text file in DATA_DIR/reports (only this user can read it), with its
    SHA-256 in the timeline: a record that can be shown later. Returns the file's name ('' when it failed)."""
    try:
        text = roomwatch.report(_room_rows(), st)["text"] + "\n"
        ROOM_REPORTS.mkdir(parents=True, exist_ok=True)
        stamp = re.sub(r"[^0-9]", "", str(st.get("started") or ""))[:12] + "_" + time.strftime("%Y%m%d%H%M")
        path = ROOM_REPORTS / f"roomwatch_{stamp}.txt"
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        _room_log("note", f"Report saved as {path.name} (SHA-256 {digest}).", what="archive")
        old = sorted(ROOM_REPORTS.glob("roomwatch_*.txt"), key=lambda p: p.stat().st_mtime)
        for p in old[:-ROOM_REPORTS_KEEP]:
            p.unlink()
        return path.name
    except Exception:
        log.error("room watch: the report could not be saved:\n%s", traceback.format_exc())
        return ""


# ----------------------------------------------------------------------------------------------- guard and tools
def _room_guard_start() -> None:
    """One small thread per process that watches the watch: a thread that died is started again from its state, a
    stall is said loudly (once an hour)."""
    g = _ROOM_GUARD.get("thread")
    if g is not None and g.is_alive():
        return

    def guard():
        global _ROOM
        while True:
            wall, boot, mono = time.time(), _room_boot_clock(), time.monotonic()
            time.sleep(ROOM_GUARD_S)
            try:
                w = _ROOM
                if w is None or w.stop_evt.is_set() or w.phase == "ended":
                    continue
                now = time.time()
                lost, why = _room_gap(ROOM_GUARD_S, now - wall, _room_boot_clock() - boot, time.monotonic() - mono)
                late = (_room_boot_clock() - boot) - ROOM_GUARD_S
                if not lost and late > 5.0:              # many short hold-ups: together they are a gap too
                    w.lates = [(t, x) for t, x in w.lates if now - t < 3600] + [(now, late)]
                    if sum(x for _, x in w.lates) >= ROOM_LATE_SUM_S:
                        lost, why = sum(x for _, x in w.lates), "held"
                        w.lates = []
                        with w.lock:
                            w.gaps_s += lost
                        text = (f"Room watch: ARIS was held up again and again in the last hour, {_took(lost)} in "
                                f"all (the computer is too busy or short of memory): that much was not watched.")
                        _room_log("health", text, what="gap", s=round(lost))
                        _notice(text, kind="roomwatch", level="alert")
                        _room_push(text, kind="health")
                        lost = 0.0
                if lost:
                    w.beat = now                        # (what looks like a stall after waking is the gap itself)
                    for r in list(w.rx.values()):
                        if r.get("since") is not None:
                            r["since"] = now
                    w.gap(lost, why, now)
                elif why == "clock":
                    _room_log("note", f"The computer's clock was set (it jumped by {_took(abs(now - wall - ROOM_GUARD_S))}): "
                              f"times in this timeline before and after do not line up.")
                if not w.alive():
                    text = "The room watch's own thread ended unexpectedly; it is being started again."
                    _room_log("health", text)
                    _notice(text, kind="roomwatch", level="alert")
                    _room_push(text, kind="health")
                    st = w.snapshot()
                    st["running"] = True                 # (it is carried on; the snapshot of a dead thread says no)
                    w.stop_evt.set()                     # the old object is done: its receivers' threads leave
                    with w.cv:                           # after the sweep in flight and it writes nothing more
                        w.cv.notify_all()
                    nw = RoomWatch(w.plan, 0, w.interval_s / 60.0, w.trig, False, state=st, only=w.only)
                    nw.restart_note = ""
                    _ROOM = nw
                    nw.start()
                elif now - w.beat > ROOM_STALL_S and now - w.stall_said > 3600:
                    w.stall_said = now
                    text = (f"The room watch has made no progress for {_took(now - w.beat)}"
                            + (f" (it was doing: {w.doing})" if w.doing else "")
                            + ". Press Reset Scanner: the watch carries on by itself afterwards.")
                    _room_log("health", text)
                    _notice(text, kind="roomwatch", level="alert")
                    _room_push(text, kind="health")
                else:
                    for ser, r in list(w.rx.items()):       # one receiver stuck in a sweep (the others go on)
                        since = r.get("since")
                        if since is not None and now - since > ROOM_RX_STALL_S and now - r["stall_said"] > 3600 \
                                and not w._holding():
                            r["stall_said"] = now
                            text = (f"Room watch: {_short(ser)} has not finished its sweep of {r.get('doing')} for "
                                    f"{_took(now - since)}. The other receivers carry on. Press Reset Scanner to "
                                    f"end the stuck sweep: the watch carries on by itself afterwards.")
                            _room_log("health", text, what="receiver")
                            _notice(text, kind="roomwatch", level="alert")
                            _room_push(text, kind="health")
            except Exception:
                log.error("room watch guard:\n%s", traceback.format_exc())

    t = threading.Thread(target=guard, name="room-guard", daemon=True)
    _ROOM_GUARD["thread"] = t
    t.start()


def _room_here():
    return _ROOM if _ROOM is not None and _ROOM.alive() else None


def _room_elsewhere(st=None):
    """The state of a watch run by another ARIS process: its owner is alive and wrote the state lately (a process
    id alone is not proof: after a reboot another program can have it)."""
    st = st if st is not None else _room_state_read()
    pid = st.get("pid")
    try:
        fresh = time.time() - float(st.get("beat") or 0) < ROOM_ALIVE_S
    except (TypeError, ValueError):
        fresh = False
    if st.get("running") and pid and pid != os.getpid() and fresh and _pid_alive(pid):
        return st
    return None


def _room_args(hours, interval_min, trigger_db, frequency_range, new_reference):
    cfg = _load_config()
    hrs = _num(hours, "hours", ROOM_DEFAULT_HOURS)
    if not 0.05 <= hrs <= ROOM_MAX_HOURS:
        raise RFError(f"hours must be between 0.05 and {ROOM_MAX_HOURS:g}.")
    mins = _num(interval_min, "interval_min", ROOM_INTERVAL_MIN)
    if not 1.0 <= mins <= 120.0:
        raise RFError("interval_min must be between 1 and 120.")
    trig = _num(trigger_db, "trigger_db", 10.0)
    if not 4.0 <= trig <= 30.0:
        raise RFError("trigger_db must be between 4 and 30.")
    only = None
    if frequency_range not in (None, "", []):
        only = [list(b) for b in _parse_band_list(frequency_range)]
        for lo, hi in only:
            if hi - lo < 0.5:
                raise RFError("Each band of a room watch must be at least 0.5 MHz wide.")
    return cfg, hrs, mins, trig, only, (True if new_reference in (None, "") else _truthy(new_reference))


def _truthy(v) -> bool:
    return str(v).strip().lower() not in ("0", "false", "no", "off", "none", "")


def _room_not_added() -> list:
    """Receivers that are plugged in but not yet in the station's list (they join after a system check)."""
    try:
        return sorted(str(d.get("serial")) for d in _list_devices()
                      if str(d.get("serial")) not in DONGLE_NAMES and not _generic_serial(str(d.get("serial"))))
    except Exception:
        return []


@register(
    "start_room_watch",
    "Start a room watch: record a quiet-room reference in every band where small transmitters are commonly found, "
    "on every receiver that tunes it, then watch for hours or days and tell what appears (new carriers, ones that "
    "grow). Say it covers only what the receivers here tune.",
    {"hours": {"type": "number", "description": "How long to watch (default 72)"},
     "interval_min": {"type": "number", "description": "Minutes between rounds (default 5)"},
     "trigger_db": {"type": "number", "description": "dB over the reference that counts (default 10)"},
     "frequency_range": {"type": "string", "description": "Only these bands, e.g. '88-108, 400-470' (default: all)"},
     "new_reference": {"type": "boolean", "description": "Start afresh with a new reference (the default)"},
     "carry_on": {"type": "boolean", "description": "Carry on the last watch with its reference and what it saw"}},
)
def start_room_watch(hours=None, interval_min=None, trigger_db=None, frequency_range=None, new_reference=None,
                     carry_on=None):
    global _ROOM
    cfg, hrs, mins, trig, only, newref = _room_args(hours, interval_min, trigger_db, frequency_range, new_reference)
    carry = carry_on not in (None, "") and _truthy(carry_on)
    if not carry and not newref:
        carry, newref = True, True       # "no new reference" can only mean: carry on the watch that has one
    r = _gui_call("/api/internal/watchdog", {"op": "room_start", "args": {
        "hours": hrs, "interval_min": mins, "trigger_db": trig, "frequency_range": frequency_range,
        "new_reference": newref, "carry_on": carry}})
    if r is not None:                                           # the GUI process runs it
        if r.get("status") == "error":
            raise RFError(r.get("error", "the GUI could not start the room watch"))
        r.pop("status", None)
        return r
    with _ROOM_START_LOCK:
        if _room_here() or _room_elsewhere():
            return {"message": "A room watch is already running. " + roomwatch.summary_line(room_watch_status_dict()),
                    "already": True}
        old = _room_state_read()
        has_old = old.get("tracker") is not None and bool(old.get("watch_since"))
        if carry and not has_old:
            raise RFError("There is no earlier room watch with a reference to carry on. Start a new one.")
        if carry:
            only = old.get("only")
        elif has_old and carry_on in (None, "") and new_reference in (None, "") \
                and time.time() - float(old.get("beat") or old.get("started_epoch") or 0) < ROOM_CARRY_DAYS * 86400:
            # an earlier watch: a new reference would take whatever transmits now as normal, so the user chooses
            n = int(old.get("alerts_total") or 0)
            return {"needs_choice": True, "message":
                    f"There is an earlier room watch (started {str(old.get('started'))[:16]}, {_plural(n, 'alert')}). "
                    f"Say \"carry on the room watch\" to go on with the same reference and everything it has "
                    f"seen, or \"start a new room watch\" to record a new reference: anything transmitting now "
                    f"then counts as normal and never alerts."}
        serials = _working_serials(cfg)
        plan = _room_plan(serials, cfg, only)
        if not plan["chunks"]:
            raise RFError("No receiver here tunes any of those bands: "
                          + "; ".join(plan["not_covered"] or ["no band"]) + ".")
        state = None
        if carry:
            now = time.time()
            state = {k: old.get(k) for k in ("tracker", "rounds", "failed_rounds", "alerts_total", "last_alert",
                                             "suppressed", "watch_since", "inventory", "round_s", "started",
                                             "started_epoch", "last_round", "refs", "ref_done", "late_refs",
                                             "gaps_s", "notes_total")}
            state.update(phase="watching", new_reference=False, deadline=now + hrs * 3600.0, carried=True)
            newref = False
        if not carry:
            left = len(_room_unseen())               # an earlier watch's alerts do not ring into the new one
            if left:
                _room_log("ack", f"{_plural(left, 'alert')} of the earlier watch had not been marked as seen "
                          f"when a new watch began.")
        _ROOM = RoomWatch(plan, hrs, mins, trig, newref, state=state, only=only)
        _ROOM.start()
    sweeps = len(plan["chunks"])
    used = {x for c in plan["chunks"] for x in c["serials"]}
    per = max(sum(1 for c in plan["chunks"] if ser in c["serials"]) for ser in used)
    extra = _room_not_added()
    power = _room_power_notes()
    for line in power:
        _room_log("note", line, what="computer")
    free = None
    try:
        free = shutil.disk_usage(str(DATA_DIR)).free
    except OSError:
        pass
    msg = ((f"Room watch carried on for {hrs:g} h more with the reference it had: " if carry
            else f"Room watch started for {hrs:g} h: ")
           + f"{_plural(sweeps, 'band')} on {_plural(len(used), 'receiver')}. "
           + (f"It records the quiet reference first (about "
              f"{math.ceil(per * (int(cfg.get('baseline_seconds', 30)) + 6) / 60.0)} min): "
              f"keep the room quiet, phones off. " if newref else "")
           + (f"Not covered by any receiver here: {'; '.join(plan['not_covered'])}. " if plan["not_covered"] else "")
           + (f"Plugged in but not yet added (say \"check the system\" and it joins the watch): "
              f"{', '.join(extra)}. " if extra else "")
           + (f"Only {free / 1e9:.1f} GB of disk is free. " if free is not None and free < 1e9 else "")
           + "".join(x + " " for x in power)
           + "Alerts arrive in the chat and on every tab.")
    return {"message": msg, "bands": sweeps, "not_covered": plan["not_covered"],
            "partly_covered": plan["partly_covered"], "hours": hrs, "not_added": extra}


@register("stop_room_watch", "Stop the room watch.", {})
def stop_room_watch():
    r = _gui_call("/api/internal/watchdog", {"op": "room_stop", "args": {}})
    if r is not None:
        if r.get("status") == "error":
            raise RFError(r.get("error", "the GUI could not stop the room watch"))
        r.pop("status", None)
        return r
    with _ROOM_START_LOCK:
        w = _room_here()
        if not w:
            st = _room_state_read()
            if st.get("running") and not _room_elsewhere(st):       # a watch a crash left "running": it is over
                st.update(running=False, ended=_now())
                try:
                    _atomic_write_json(ROOM_STATE, st, compact=True)
                except OSError:
                    pass
                return {"message": "The room watch was not running any more; it will not be resumed."}
            return {"message": "No room watch is running."}
        w.stop()
        return {"message": "Room watch stopped. " + roomwatch.summary_line(w.snapshot())
                           + " \"Carry on\" starts it again with the same reference."}


def room_watch_status_dict() -> dict:
    here = _room_here()
    st = here.snapshot() if here else _room_state_read()
    if not here and st.get("running") and not _room_elsewhere(st):
        st = {**st, "running": False, "interrupted": True}       # its owner is gone (a crash, a restart to come)
    if not st.get("running"):
        st = {**st, "receivers": []}
    if st.get("running"):
        try:
            st["stalled"] = time.time() - float(st.get("beat") or time.time()) > ROOM_STALL_S
        except (TypeError, ValueError):
            st["stalled"] = False
    return st


def _room_status_line(st) -> str:
    if not st.get("started"):
        return "No room watch has been started."
    if st.get("running"):
        left = max(0.0, float(st.get("deadline") or 0) - time.time())
        ph = ("Recording the quiet reference" + (f" ({st['phase_note']})" if st.get("phase_note") else "")
              if st.get("phase") == "reference" else "Watching")
        msg = f"{ph}; {_took(left)} left. " + roomwatch.summary_line(st)
        sw = st.get("sweeps_last")
        if sw and sw[1] and sw[0] < sw[1]:
            msg += f" In the last round {sw[0]} of {sw[1]} sweeps worked."
        if st.get("stalled"):
            msg += " It has made no progress for a while: press Reset Scanner (the watch carries on by itself)."
        if st.get("unwatched"):
            msg += " Not checked for a while: " + "; ".join(st["unwatched"][:4]) + "."
        for x in st.get("receivers") or []:
            if x.get("state") in ("failing", "slow", "busy") and x.get("note"):
                msg += " " + x["note"]
        if float(st.get("gaps_s") or 0) >= 60:
            msg += f" Not watched for {_took(st['gaps_s'])} in total (the report lists when)."
        try:
            msg += " It runs until " + time.strftime("%a %H:%M", time.localtime(float(st["deadline"]))) + "."
        except (KeyError, TypeError, ValueError):
            pass
    elif st.get("interrupted"):
        msg = ("The room watch was interrupted (ARIS stopped or restarted). It carries on when ARIS starts again. "
               + roomwatch.summary_line(st))
    else:
        msg = "The room watch is not running (" + (f"it ended {st['ended']}" if st.get("ended") else "stopped") + "). " \
              + roomwatch.summary_line(st)
    if st.get("not_covered"):
        msg += " Not covered by any receiver here: " + "; ".join(st["not_covered"]) + "."
    return msg


@register("room_watch_status", "Say whether a room watch is running and what it has found so far.", {})
def room_watch_status():
    st = room_watch_status_dict()
    return {"running": bool(st.get("running")), "message": _room_status_line(st), "rounds": st.get("rounds"),
            "alerts": st.get("alerts_total"), "bands": len(st.get("bands") or []), "errors": st.get("errors"),
            "unseen_alerts": len(_room_unseen())}


@register("room_watch_report", "The room watch as a timeline per day: what appeared, when, and what was not covered.",
          {})
def room_watch_report():
    st = room_watch_status_dict()
    if not st.get("started"):
        return {"message": "No room watch has been started."}
    rep = roomwatch.report(_room_rows(), st)
    return {"message": rep["short"], "days": len(rep["days"]), "text": rep["text"], "short": rep["short"]}


def _room_label(text, n) -> str:
    """A note or a label as the user typed it, made safe for the timeline, the chat and the report: one line, no
    control or text-direction characters, and no square brackets (a note that reads like the console's own prompt
    would cut the chat's view of the report)."""
    t = re.sub(r"[\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]+|\s+", " ", str(text or ""))
    return _cut_at_word(t.replace("[", "(").replace("]", ")").strip(), n)


@register("mark_room_watch", "Put a note into the room watch's timeline, e.g. 'break', 'door opened', 'people "
          "arrive'.", {"label": {"type": "string", "description": "The note"}}, required=("label",))
def mark_room_watch(label=None):
    text = _room_label(label, 80)
    if not text:
        raise RFError("Say what the mark is for, e.g. 'break'.")
    st = room_watch_status_dict()
    if not st.get("started"):
        raise RFError("No room watch has been started.")
    last = next((r for r in reversed(_room_rows()) if r.get("kind") == "mark"), None)
    if not (last and last.get("text") == text and time.time() - float(last.get("t") or 0) < 5):
        _room_log("mark", text)                              # (a double tap is one note)
    return {"message": f"Marked: {text}."}


@register("accept_room_watch_signal", "Tell the room watch that a signal is known (the user's own device): it stops "
          "alerting on it and lists it as accepted.",
          {"frequency_mhz": {"type": "number", "description": "The signal's frequency in MHz"},
           "label": {"type": "string", "description": "What it is, in the user's words"}}, required=("frequency_mhz",))
def accept_room_watch_signal(frequency_mhz=None, label=None):
    f = _to_mhz(frequency_mhz)
    text = _room_label(label, 60) or "known"
    r = _gui_call("/api/internal/watchdog", {"op": "room_accept", "args": {"frequency_mhz": f, "label": text}})
    if r is not None:
        if r.get("status") == "error":
            raise RFError(r.get("error", "the GUI could not do that"))
        r.pop("status", None)
        return r
    w = _room_here()
    if not w:
        raise RFError("No room watch is running here.")
    rec = w.accept(f, text)
    return {"message": f"{float(rec['mhz']):g} MHz is accepted as known ({rec['mine']}): no more alerts for it. "
                       f"The report lists it."}


def ack_room_watch(who="") -> dict:
    """The user has seen the alerts: they stop ringing on the page (the timeline keeps them, and who said so)."""
    n = len(_room_unseen())
    if n:
        _room_log("ack", f"{_plural(n, 'alert')} seen" + (f" by {who}" if who else "") + ".")
    return {"message": f"{_plural(n, 'alert')} marked as seen." if n else "Nothing new to mark.", "acknowledged": n}


def resume_room_watch() -> str:
    """GUI start-up: carry on a watch a restart or crash interrupted, if its time is not up. Returns what it did."""
    global _ROOM
    with _ROOM_START_LOCK:
        st = _room_state_read()
        if not st.get("running") or _room_here() or _room_elsewhere(st):
            return ""
        try:
            deadline, beat = float(st.get("deadline") or 0), float(st.get("beat") or time.time())
        except (TypeError, ValueError):
            return ""
        if deadline <= time.time() + 60:
            st.update(running=False, ended=_now())
            try:
                _atomic_write_json(ROOM_STATE, st, compact=True)
            except OSError:
                pass
            _room_log("stop", "Room watch: its time ran out while ARIS was not running.")
            return ""
        cfg = _load_config()
        only = st.get("only")
        try:
            plan = _room_plan(_working_serials(cfg), cfg, only)
            if not plan["chunks"]:
                return ""
            _ROOM = RoomWatch(plan, 0, float(st.get("interval_min") or ROOM_INTERVAL_MIN),
                              float(st.get("trigger_db") or 10.0), False, state=st, only=only)
        except Exception:
            log.error("room watch: resume failed:\n%s", traceback.format_exc())
            return ""
        _ROOM.start()
    for f in DATA_DIR.glob(ROOM_STATE.name + ".tmp*"):       # a write a crash interrupted
        try:
            f.unlink()
        except OSError:
            pass
    if st.get("from_backup"):
        _room_log("health", "The room watch's state could not be read after the restart; it carried on from the "
                  "copy of its last finished round.")
    gap = time.time() - beat
    if gap > 600:                                            # as loud as a sleep: hours may be missing
        _ROOM.gap(gap, "down")
    elif gap > 120:
        _room_log("skip", f"Nothing was watched for {_took(gap)}: ARIS was not running.", what="gap", s=round(gap))
        with _ROOM.lock:
            _ROOM.gaps_s += gap
    _notice("ARIS restarted, so the room watch carried on" + (f" after a gap of {_took(gap)}" if gap > 120 else "")
            + ". " + roomwatch.summary_line(st), kind="roomwatch", level="info")
    return "room watch"
