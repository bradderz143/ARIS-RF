"""The room watch's page API (Activity tab): its state and timeline, start and stop (admin), a note in the timeline,
"seen" for the alerts, "this one is known", the optional alert address, and the report as text or as a file. The
watch itself runs in this process (jobs.py hosts it, like captures) and is described in engine/roomwatch.py."""
from __future__ import annotations

from flask import Blueprint, Response, jsonify, request

from .auth import api_admin_required, api_login_required
from .engine import engine as _engine
from .jobs import _tool

bp = Blueprint("roomwatch", __name__)


def _body() -> dict:
    """The request's JSON object ({} for anything else: a list or a string is not an error page)."""
    d = request.get_json(force=True, silent=True)
    return d if isinstance(d, dict) else {}

_KEEP = ("running", "phase", "phase_note", "started", "ended", "deadline", "hours", "interval_min", "trigger_db",
         "rounds", "failed_rounds", "alerts_total", "last_alert", "last_round", "round_s", "sweeps_last",
         "not_covered", "partly_covered", "errors", "unwatched", "stalled", "interrupted", "accepted", "deaf",
         "receivers", "gaps_s", "notes_total")
_ROW = ("t", "time", "kind", "text", "mhz", "what", "over_db")
_KINDS = ("start", "alert", "note", "mark", "stop", "skip", "ack", "health", "mine", "late")


def brief(eng) -> dict:
    """What every tab needs every few seconds: is a watch running, and the alerts nobody has marked as seen."""
    rows = eng._room_rows()
    unseen = eng._room_unseen(rows)
    last = unseen[-1] if unseen else None
    st = eng.room_watch_status_dict() if (unseen or rows) else {}
    return {"running": bool(st.get("running")), "stalled": bool(st.get("stalled")),
            "interrupted": bool(st.get("interrupted")), "unseen": len(unseen),
            "last": {k: last.get(k) for k in ("t", "time", "kind", "text", "mhz")} if last else None}


@bp.route("/api/roomwatch")
@api_login_required
def api_roomwatch():
    try:
        eng = _engine()
        st = eng.room_watch_status_dict()
        rows = [r for r in eng._room_rows() if r.get("kind") in _KINDS]
        out = {k: st.get(k) for k in _KEEP}
        out["line"] = eng._room_status_line(st)
        out["bands"] = [{"s": b.get("s"), "e": b.get("e"), "label": b.get("label"), "dongles": b.get("dongles")}
                        for b in (st.get("bands") or [])]
        out["rows"] = [{k: r.get(k) for k in _ROW} for r in rows[-14:]][::-1]
        out["unseen"] = len(eng._room_unseen(rows))
        # an earlier watch whose reference can be carried on (the page then offers both ways to start)
        out["previous"] = bool(not st.get("running") and st.get("tracker") is not None and st.get("watch_since"))
        inv = st.get("inventory") or {}
        out["inventory"] = {"total": inv.get("total", 0), "listed": len(inv.get("listed") or []) + int(inv.get("more") or 0)}
        out["late_refs"] = len(st.get("late_refs") or [])
        push = eng.room_alert_address()
        from . import auth
        if auth.role() != "admin":                      # (how the address is doing is the admin's business)
            push = {"set": push.get("set")}
        out["push"] = push
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": f"could not read the room watch: {type(e).__name__}"}), 500


@bp.route("/api/roomwatch/start", methods=["POST"])
@api_admin_required
def api_roomwatch_start():
    d = _body()
    r = _tool("start_room_watch", hours=d.get("hours"), interval_min=d.get("interval_min"),
              frequency_range=d.get("frequency_range"), new_reference=d.get("new_reference"),
              carry_on=d.get("carry_on"))
    return jsonify(r), (200 if r.get("status") == "ok" and not r.get("needs_choice") else 409)


@bp.route("/api/roomwatch/stop", methods=["POST"])
@api_admin_required
def api_roomwatch_stop():
    r = _tool("stop_room_watch")
    return jsonify(r), (200 if r.get("status") == "ok" else 409)


@bp.route("/api/roomwatch/mark", methods=["POST"])
@api_login_required
def api_roomwatch_mark():
    d = _body()
    r = _tool("mark_room_watch", label=d.get("label"))
    return jsonify(r), (200 if r.get("status") == "ok" else 409)


@bp.route("/api/roomwatch/ack", methods=["POST"])
@api_login_required
def api_roomwatch_ack():
    try:
        from . import auth
        return jsonify(_engine().ack_room_watch("the admin account" if auth.role() == "admin" else "a viewer account"))
    except Exception as e:
        return jsonify({"error": f"could not mark the alerts as seen: {type(e).__name__}"}), 500


@bp.route("/api/roomwatch/accept", methods=["POST"])
@api_admin_required
def api_roomwatch_accept():
    d = _body()
    r = _tool("accept_room_watch_signal", frequency_mhz=d.get("frequency_mhz"), label=d.get("label"))
    return jsonify(r), (200 if r.get("status") == "ok" else 409)


@bp.route("/api/roomwatch/push", methods=["POST"])
@api_admin_required
def api_roomwatch_push():
    """Set where alerts are also posted ("" clears it, no "url" or null leaves it). {"url": "...", "detail": false}"""
    d = _body()
    eng = _engine()
    try:
        return jsonify(eng.set_room_alert_address(d.get("url"), d.get("detail") if "detail" in d else None))
    except eng.RFError as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/api/roomwatch/push_test", methods=["POST"])
@api_admin_required
def api_roomwatch_push_test():
    eng = _engine()
    if not eng._room_push("ARIS: this is a test of the room watch alert.", test=True, wait=True):
        return jsonify({"error": "No alert address is set."}), 409
    st = eng.room_alert_address()
    if st.get("error"):
        return jsonify({"error": f"The address did not take the test ({st['error']}).", **st}), 502
    return jsonify({"message": "Test sent: the phone should ring now.", **st})


@bp.route("/api/roomwatch/report")
@api_login_required
def api_roomwatch_report():
    r = _tool("room_watch_report")
    text = r.get("text") or r.get("message") or ""
    if request.args.get("download"):
        return Response(text + "\n", mimetype="text/plain; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="aris-room-watch-report.txt"',
                                 "Cache-Control": "no-store"})
    return jsonify({"text": text})
