"""Bluetooth for the Devices tab (v4.11.0): what the Ubertooth heard, with a baseline.

The listening is done by a separate helper (deploy/aris-bt.sh installs it: a small unit that runs `ubertooth-btle -n`
and writes a SUMMARY to ARIS_BT_SCAN_FILE, default /run/aris-bt/scan.json). ARIS itself neither listens nor transmits:
this module reads that file when it is newer than the last one, keeps the devices with a stable address and the
baseline in DATA_DIR/bluetooth.json, and the rules are in arislib.bluetooth. No address, name or payload is in either
file: devices are hash labels, the rest is counts, manufacturers and signal strength."""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from flask import Blueprint, jsonify, request

from arislib import bluetooth as bt
from arislib.config import DATA_DIR, read_json, stat_key, write_json

from .auth import api_admin_required, api_login_required

bp = Blueprint("bluetooth", __name__)
_LOCK = threading.Lock()
STALE_S = 300      # a summary older than this: the helper stopped
_STATE = {"key": None, "st": None}     # bluetooth.json as last read or written, and the file's time, size and inode then
_SNAP = {"key": None, "snap": None}    # the helper's summary as last read: its text and what it parsed to


def _scan_file() -> Path:
    return Path(os.environ.get("ARIS_BT_SCAN_FILE", "/run/aris-bt/scan.json"))


def _state_file() -> Path:
    return DATA_DIR / "bluetooth.json"


def _load() -> dict:
    """The state (the caller holds _LOCK). The file is read again only when it changed (the page asks every few
    seconds). Callers change the state inside _changing and write it with _save, so the copy kept here is what the
    file holds: a change that fails half way drops the copy."""
    key = stat_key(_state_file())
    if key is not None and key == _STATE["key"]:
        return _STATE["st"]
    st = read_json(_state_file(), None)
    if not isinstance(st, dict) or st.get("version") != 1 or not isinstance(st.get("devices"), dict):
        st, key = bt.new_state(), None
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


def _read_snap(p: Path):
    """The helper's summary, parsed again only when its text changed. The text itself is the key: reading it is
    cheap and parsing it is not, and a file's time and size cannot tell apart two summaries of one size written
    within one tick of the file clock. Raises OSError / ValueError as reading it does."""
    text = p.read_text(errors="replace")
    if text != _SNAP["key"]:
        _SNAP.update(key=None, snap=None)
        snap = json.loads(text)
        _SNAP.update(key=text, snap=snap)
    return _SNAP["snap"]


def _refresh(st: dict, now: float) -> dict:
    """Fold the helper's newest summary into the state when it is newer than the last one; returns the helper's status."""
    p = _scan_file()
    try:
        mt = p.stat().st_mtime
        snap = _read_snap(p)
    except (OSError, ValueError):
        return {"helper": "missing", "scan_age_s": None}
    if not isinstance(snap, dict) or snap.get("v") != 1:
        return {"helper": "missing", "scan_age_s": None}
    t = float(snap.get("t") or mt)
    if t > float(st.get("scan_t") or 0):
        with _changing():
            bt.update(st, snap, t)
            _save(st)
    age = max(0.0, now - t)
    return {"helper": "stale" if age > STALE_S else "ok", "scan_age_s": int(age)}


@bp.route("/api/bluetooth")
@api_login_required
def api_bluetooth():
    """The Bluetooth devices with a stable address (status baseline / new / look / unrated, reasons in words), the count
    of devices with rotating addresses, the tracker-type advertisements, and the helper's state: ok, stale or missing."""
    now = time.time()
    try:
        with _LOCK:
            st = _load()
            helper = _refresh(st, now)
            # (the tag first: the page asks every few seconds, and the rows are only built when it has an older list)
            tag = f"{st.get('scan_t')}-{st.get('baseline_t')}-{helper['helper']}"
            if request.args.get("have") == tag:
                return jsonify({"unchanged": True, "tag": tag, **helper})
            out = bt.rows(st, now)
            out.update(helper)
            out["tag"] = tag
            return jsonify(out)      # (written out under the lock: the rows share lists with the state kept by _load)
    except Exception as e:
        return jsonify({"error": f"could not read the Bluetooth list: {type(e).__name__}: {e}"}), 500


@bp.route("/api/bluetooth/baseline", methods=["POST"])
@api_admin_required
def api_bluetooth_baseline():
    """Every device with a stable address heard so far is normal here (one confirm on the page)."""
    try:
        with _LOCK:
            st = _load()
            _refresh(st, time.time())
            if not st.get("devices"):
                return jsonify({"error": "No Bluetooth device has been heard yet, so there is nothing to record. Wait until the list has devices."}), 409
            with _changing():
                n = bt.record_baseline(st)
                _save(st)
        return jsonify({"ok": True, "baseline_n": n, "message": f"Bluetooth baseline recorded: {n} devices are normal here."})
    except Exception as e:
        return jsonify({"error": f"could not record the baseline: {type(e).__name__}: {e}"}), 500


@bp.route("/api/bluetooth/forget", methods=["POST"])
@api_admin_required
def api_bluetooth_forget():
    """Forget the Bluetooth list and its baseline."""
    try:
        with _LOCK:
            _save(bt.new_state())
        return jsonify({"ok": True, "message": "Bluetooth list and baseline forgotten."})
    except Exception as e:
        return jsonify({"error": f"could not forget the Bluetooth list: {type(e).__name__}: {e}"}), 500
