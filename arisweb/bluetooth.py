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
from pathlib import Path

from flask import Blueprint, jsonify, request

from arislib import bluetooth as bt
from arislib.config import DATA_DIR, read_json, write_json

from .auth import api_admin_required, api_login_required

bp = Blueprint("bluetooth", __name__)
_LOCK = threading.Lock()
STALE_S = 300      # a summary older than this: the helper stopped


def _scan_file() -> Path:
    return Path(os.environ.get("ARIS_BT_SCAN_FILE", "/run/aris-bt/scan.json"))


def _state_file() -> Path:
    return DATA_DIR / "bluetooth.json"


def _load() -> dict:
    st = read_json(_state_file(), None)
    if not isinstance(st, dict) or st.get("version") != 1 or not isinstance(st.get("devices"), dict):
        st = bt.new_state()
    return st


def _refresh(st: dict, now: float) -> dict:
    """Fold the helper's newest summary into the state when it is newer than the last one; returns the helper's status."""
    p = _scan_file()
    try:
        mt = p.stat().st_mtime
        snap = json.loads(p.read_text(errors="replace"))
    except (OSError, ValueError):
        return {"helper": "missing", "scan_age_s": None}
    if not isinstance(snap, dict) or snap.get("v") != 1:
        return {"helper": "missing", "scan_age_s": None}
    t = float(snap.get("t") or mt)
    if t > float(st.get("scan_t") or 0):
        bt.update(st, snap, t)
        write_json(_state_file(), st, private=True)
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
            out = bt.rows(st, now)
        out.update(helper)
        tag = f"{out.get('scan_t')}-{out.get('baseline_t')}-{helper['helper']}"
        if request.args.get("have") == tag:
            return jsonify({"unchanged": True, "tag": tag, **helper})
        out["tag"] = tag
        return jsonify(out)
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
            n = bt.record_baseline(st)
            write_json(_state_file(), st, private=True)
        return jsonify({"ok": True, "baseline_n": n, "message": f"Bluetooth baseline recorded: {n} devices are normal here."})
    except Exception as e:
        return jsonify({"error": f"could not record the baseline: {type(e).__name__}: {e}"}), 500


@bp.route("/api/bluetooth/forget", methods=["POST"])
@api_admin_required
def api_bluetooth_forget():
    """Forget the Bluetooth list and its baseline."""
    try:
        with _LOCK:
            write_json(_state_file(), bt.new_state(), private=True)
        return jsonify({"ok": True, "message": "Bluetooth list and baseline forgotten."})
    except Exception as e:
        return jsonify({"error": f"could not forget the Bluetooth list: {type(e).__name__}: {e}"}), 500
