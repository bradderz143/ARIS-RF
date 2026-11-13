"""Wi-Fi networks for the Devices tab (v4.10.0): what a passive scan heard, with a baseline.

The scan is made by a separate helper (deploy/aris-wifi.sh installs it: a tiny unit that runs `iw ... scan passive`
every minute and writes the text to ARIS_WIFI_SCAN_FILE, default /run/aris-wifi/scan.txt). ARIS itself neither
scans nor transmits: this module reads that file when it is newer than the last one, keeps the networks and the
baseline in DATA_DIR/wifi.json, and the rules are in arislib.wireless."""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from flask import Blueprint, jsonify, request

from arislib import wireless
from arislib.config import DATA_DIR, read_json, stat_key, write_json

from .auth import api_admin_required, api_login_required

bp = Blueprint("wifi", __name__)
_LOCK = threading.Lock()
STALE_S = 300      # a scan file older than this: the helper stopped
_WATCH = {"thread": None}
_STATE = {"key": None, "st": None}     # wifi.json as last read or written, and the file's time, size and inode then


def _scan_file() -> Path:
    return Path(os.environ.get("ARIS_WIFI_SCAN_FILE", "/run/aris-wifi/scan.txt"))


def _state_file() -> Path:
    return DATA_DIR / "wifi.json"


def _load() -> dict:
    """The state (the caller holds _LOCK). The file is read again only when it changed: the page asks every few
    seconds, and a day of history for a few hundred networks is a megabyte of JSON. Callers change the state
    inside _changing and write it with _save, so the copy kept here is what the file holds: a change that fails
    half way drops the copy."""
    key = stat_key(_state_file())
    if key is not None and key == _STATE["key"]:
        return _STATE["st"]
    st = read_json(_state_file(), None)
    if not isinstance(st, dict) or st.get("version") != 1 or not isinstance(st.get("nets"), dict):
        st, key = wireless.new_state(), None
    _STATE.update(key=key, st=st)
    return st


def _save(st: dict) -> None:
    """Write the state (the caller holds _LOCK) and keep it as the copy _load hands out. If the write fails, the
    next _load reads the file again."""
    _STATE.update(key=None, st=None)
    write_json(_state_file(), st, private=True, compact=True)
    _STATE.update(key=stat_key(_state_file()), st=st)


@contextmanager
def _changing():
    """Round every change made to the kept state in place (the caller holds _LOCK). If the change fails half way,
    the kept copy is dropped: the next look reads what the file holds, and the half-made change is never applied
    a second time on top of itself."""
    try:
        yield
    except BaseException:
        _STATE.update(key=None, st=None)
        raise


def _refresh(st: dict, now: float) -> dict:
    """Fold the helper's newest scan into the state when it is newer than the last one; returns the helper's status."""
    p = _scan_file()
    try:
        mt = p.stat().st_mtime
        if not os.access(str(p), os.R_OK):
            raise OSError("the scan file cannot be read")
    except OSError:
        return {"helper": "missing", "scan_age_s": None}
    if mt > float(st.get("scan_t") or 0):              # (the scan's text is read only when it is a new scan)
        try:
            text = p.read_text(errors="replace")
        except OSError:
            return {"helper": "missing", "scan_age_s": None}
        nets = wireless.parse_iw_dump(text)
        with _changing():
            wireless.update(st, nets, mt)
            _save(st)
    age = max(0.0, now - mt)
    return {"helper": "stale" if age > STALE_S else "ok", "scan_age_s": int(age)}


def _post(text: str) -> None:
    """A Wi-Fi notice into the chat (the engine's notices file, which the chat bridge reads)."""
    try:
        from .engine import engine
        engine()._notice(text, kind="wifi")
    except Exception as e:
        print(f"wifi: could not post a notice ({type(e).__name__}: {e})", flush=True)


def tick(now=None) -> list:
    """One round of the watcher (v4.13.0): fold the newest scan in (signal history) and post the notices for it
    (arislib.wireless.events). Returns the notice texts."""
    now = time.time() if now is None else now
    with _LOCK:
        st = _load()
        if _refresh(st, now)["helper"] == "missing":
            return []
        before = (dict(st.get("noticed") or {}), st.get("noticed_since"))
        with _changing():
            texts = wireless.events(st, now)
            if (st.get("noticed"), st.get("noticed_since")) != before:
                _save(st)
    for t in texts:
        _post(t)
    return texts


def start_watch() -> None:
    """Fold each scan in every minute even when no page is open (the history must not have holes because nobody
    looked), and post the notices. ARIS_WIFI_WATCH_S sets the period (0: off)."""
    try:
        every = float(os.environ.get("ARIS_WIFI_WATCH_S", "60") or 0)
    except ValueError:
        every = 60.0
    if every <= 0 or _WATCH["thread"] is not None:
        return

    def loop():
        while True:
            time.sleep(every)
            try:
                tick()
            except Exception as e:          # (never let the watcher die: the next scan is another chance)
                print(f"wifi watch: {type(e).__name__}: {e}", flush=True)

    _WATCH["thread"] = threading.Thread(target=loop, name="wifi-watch", daemon=True)
    _WATCH["thread"].start()


@bp.route("/api/wifi")
@api_login_required
def api_wifi():
    """The Wi-Fi networks heard (rows: status baseline / new / look / unrated, reasons in words), the totals and the
    helper's state: ok, stale (no scan for 5 minutes) or missing (the helper is not installed or not running)."""
    now = time.time()
    try:
        with _LOCK:
            st = _load()
            helper = _refresh(st, now)
            # (the tag first: the page asks every few seconds, and the rows are only built when it has an older list)
            tag = f"{st.get('scan_t')}-{st.get('baseline_t')}-{st.get('marks_t')}-{helper['helper']}"
            if request.args.get("have") == tag:
                return jsonify({"unchanged": True, "tag": tag, **helper})
            out = wireless.rows(st, now)
            out.update(helper)
            out["tag"] = tag
            return jsonify(out)      # (written out under the lock: the rows share lists with the state kept by _load)
    except Exception as e:
        return jsonify({"error": f"could not read the Wi-Fi list: {type(e).__name__}: {e}"}), 500


@bp.route("/api/wifi/baseline", methods=["POST"])
@api_admin_required
def api_wifi_baseline():
    """Everything heard so far is normal here (one confirm on the page)."""
    try:
        with _LOCK:
            st = _load()
            _refresh(st, time.time())
            if not st.get("nets"):
                return jsonify({"error": "No Wi-Fi network has been heard yet, so there is nothing to record. Wait until the list has networks."}), 409
            with _changing():
                n = wireless.record_baseline(st)
                _save(st)
        return jsonify({"ok": True, "baseline_n": n, "message": f"Wi-Fi baseline recorded: {n} networks are normal here."})
    except Exception as e:
        return jsonify({"error": f"could not record the baseline: {type(e).__name__}: {e}"}), 500


@bp.route("/api/wifi/mark", methods=["POST"])
@api_admin_required
def api_wifi_mark():
    """Mark a row's networks 'mine', 'known' or 'no' (not known), or null (back to the rule): {bssids: [...], mark}."""
    body = request.get_json(silent=True) or {}
    bssids, mark = body.get("bssids"), body.get("mark")
    if not isinstance(bssids, list) or not bssids or len(bssids) > 20 or mark not in ("mine", "known", "no", None):
        return jsonify({"error": "send {bssids: [...], mark: 'mine' | 'known' | 'no' | null}"}), 400
    try:
        with _LOCK:
            st = _load()
            with _changing():
                n = wireless.set_mark(st, bssids, mark)
                _save(st)
        what = {"mine": "marked as yours", "known": "marked as known", "no": "marked as not known", None: "back to the rule"}[mark]
        return jsonify({"ok": True, "changed": n, "message": f"Wi-Fi network {what}." if n else "Nothing changed."})
    except Exception as e:
        return jsonify({"error": f"could not mark the network: {type(e).__name__}: {e}"}), 500


@bp.route("/api/wifi/forget", methods=["POST"])
@api_admin_required
def api_wifi_forget():
    """Forget the Wi-Fi list and its baseline."""
    try:
        with _LOCK:
            _save(wireless.new_state())
        return jsonify({"ok": True, "message": "Wi-Fi list and baseline forgotten."})
    except Exception as e:
        return jsonify({"error": f"could not forget the Wi-Fi list: {type(e).__name__}: {e}"}), 500
