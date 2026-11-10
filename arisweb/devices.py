"""Devices tab API: every device a decoder named, one row per device, a vehicle's tyre sensors as one row
(the engine's device history, DATA_DIR/devices.json; the rules are in arislib.devices)."""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from .auth import api_admin_required, api_login_required
from .engine import engine as _engine

bp = Blueprint("devices", __name__)


@bp.route("/api/devices")
@api_login_required
def api_devices():
    """The device history, rows sorted by last seen, with the totals (devices, sets, packets) and the grouping
    rule in words. A row is one device (model + ID: title, seen_line, reading_words, frequencies, dongles) or a
    set ("Citroen car: 4 tyre sensors": sensors of one make heard within 10 s of each other on 2 or more
    occasions; its members are listed under it). ?have=<tag> from the last reply answers {"unchanged": true}
    when nothing was decoded or forgotten since."""
    try:
        eng = _engine()
        tag = eng.devices_tag()
        if request.args.get("have") == tag:
            return jsonify({"unchanged": True, "tag": tag})
        return jsonify(eng.devices_list())
    except Exception as e:
        return jsonify({"error": f"could not read the device history: {type(e).__name__}: {e}"}), 500


@bp.route("/api/devices/forget", methods=["POST"])
@api_admin_required
def api_devices_forget():
    """Forget all devices (one confirm on the page): the history starts empty; the logs are not touched."""
    try:
        return jsonify({"ok": True, **_engine().forget_devices()})
    except Exception as e:
        return jsonify({"error": f"could not forget the devices: {type(e).__name__}: {e}"}), 500


@bp.route("/api/devices/mark", methods=["POST"])
@api_admin_required
def api_devices_mark():
    """Mark a device or a set known (true), not known / watched (false) or back to the rule (null): {key, known}.
    A known device is folded into one line in capture results and in what the AI reads (arislib.devices.KNOWN_RULE)."""
    j = request.get_json(silent=True) or {}
    key = str(j.get("key") or "")
    known = j.get("known")
    if not key or not (known is None or isinstance(known, bool)):
        return jsonify({"error": "key and known (true, false or null) needed"}), 400
    try:
        r = _engine().mark_device_known(key, known)
        if r.get("status") != "ok":
            return jsonify({"error": r.get("error") or "could not mark the device"}), 404
        return jsonify({"ok": True, **r})
    except Exception as e:
        return jsonify({"error": f"could not mark the device: {type(e).__name__}: {e}"}), 500
