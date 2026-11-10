"""Map tab API: floorplans, where each node's antenna sits, locate sessions (heat maps) and antenna calibration.
All the logic lives in arislib/; this file only checks input and turns errors into short messages."""
from __future__ import annotations

from flask import Blueprint, jsonify, request, send_file

from .auth import api_admin_required, api_login_required
from .engine import engine as _engine

bp = Blueprint("map", __name__)

_MIME = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}


def _eng():
    eng = _engine()
    from arislib import nodes
    nodes.ensure_local(*eng._local_node_list())
    return eng


def _body():
    return request.get_json(force=True, silent=True) or {}


def _num(d, key, default=None, lo=None, hi=None):
    v = d.get(key, default)
    if v in (None, ""):
        return default
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number")
    if (lo is not None and x < lo) or (hi is not None and x > hi):
        raise ValueError(f"{key} must be between {lo:g} and {hi:g}")
    return x


def _err(e, code=400):
    return jsonify({"error": str(e)}), code


def _guard(fn):
    """Expected problems (bad input, nothing placed, dongle busy) come back as a message the page can show."""
    from functools import wraps

    @wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except (ValueError, KeyError) as e:
            return _err(e.args[0] if e.args else e)
        except Exception as e:
            eng = None
            try:
                eng = _engine()
            except Exception:
                pass
            if eng is not None and isinstance(e, eng.RFError):
                return _err(e, 409)
            return _err(f"{type(e).__name__}: {e}", 500)
    return wrapper


@bp.route("/api/map/state")
@api_login_required
@_guard
def state():
    _eng()
    from arislib import floorplan, nodes, store
    plans, active = floorplan.list_plans()
    return jsonify({"plans": plans, "active": active, "plan": floorplan.get(active), "nodes": nodes.list_nodes(),
                    "sessions": store.list_sessions(12)})


@bp.route("/api/map/plan", methods=["POST"])
@api_admin_required
@_guard
def plan_new():
    from arislib import floorplan
    if request.files.get("image"):
        f = request.files["image"]
        data = f.read(floorplan.MAX_IMAGE_BYTES + 1)
        width = _num(request.form, "width_m", 10.0, 0.5, 2000)
        return jsonify(floorplan.add(request.form.get("name") or f.filename or "Floorplan", data, width))
    d = _body()
    return jsonify(floorplan.add(d.get("name") or "Blank plan", None, _num(d, "width_m", 12.0, 0.5, 2000)))


@bp.route("/api/map/plan/<pid>", methods=["POST", "DELETE"])
@api_admin_required
@_guard
def plan_edit(pid):
    from arislib import floorplan
    if request.method == "DELETE":
        floorplan.delete(pid)
        return jsonify({"ok": True})
    d = _body()
    if d.get("activate"):
        return jsonify(floorplan.set_active(pid))
    return jsonify(floorplan.update(pid, name=d.get("name"), width_m=_num(d, "width_m", None, 0.5, 2000),
                                    height_m=_num(d, "height_m", None, 0.5, 2000)))


@bp.route("/api/map/plan/<pid>/scale", methods=["POST"])
@api_admin_required
@_guard
def plan_scale(pid):
    from arislib import floorplan
    d = _body()
    return jsonify(floorplan.scale_from_points(pid, _num(d, "ax"), _num(d, "ay"), _num(d, "bx"), _num(d, "by"),
                                               _num(d, "distance_m", None, 0.05, 2000)))


@bp.route("/api/map/plan/<pid>/image")
@api_login_required
def plan_image(pid):
    from arislib import floorplan
    path = floorplan.image_path(pid)
    if path is None:
        return _err("no picture", 404)
    return send_file(str(path), mimetype=_MIME.get(path.suffix.lstrip("."), "application/octet-stream"),
                     max_age=3600)


@bp.route("/api/map/node/<nid>", methods=["POST", "DELETE"])
@api_admin_required
@_guard
def node_edit(nid):
    _eng()
    from arislib import floorplan, nodes
    if request.method == "DELETE":
        nodes.remove(nid)
        return jsonify({"ok": True})
    d = _body()
    if d.get("unplace"):
        return jsonify(nodes.unplace(nid))
    if "x" in d and "y" in d:
        plan = floorplan.get(d.get("floorplan")) or floorplan.active()
        nodes.place(nid, plan["id"], _num(d, "x"), _num(d, "y"))
    fields = {k: d[k] for k in ("name", "antenna", "enabled", "url") if k in d}
    return jsonify(nodes.update(nid, **fields) if fields else nodes.get(nid))


@bp.route("/api/map/node/<nid>/health")
@api_login_required
@_guard
def node_health(nid):
    from arislib import measure, nodes
    n = nodes.get(nid)
    if n is None:
        raise KeyError("No such node")
    if n["kind"] != "remote":
        _eng()                                     # numbering and presence as ARIS sees them now
        n = nodes.get(nid) or n
        if n.get("missing"):                       # v4.8.6 said 'Plugged into this PC' for the removed dongle
            return jsonify({"ok": False, "note": "Not plugged in: this dongle was forgotten, or the last check did "
                                                 "not find it. The node stays off until it is back."})
        return jsonify({"ok": True, "note": "Plugged into this PC"})
    return jsonify(measure.remote_health(n))


@bp.route("/api/map/remote", methods=["POST"])
@api_admin_required
@_guard
def node_add_remote():
    from arislib import nodes
    d = _body()
    return jsonify(nodes.add_remote(d.get("name"), d.get("url"), d.get("token")))


def _measure_args(d):
    return {"freq_mhz": _num(d, "freq_mhz", None, 24, 1766), "bw_khz": _num(d, "bw_khz", None, 5, 400),
            "dwell_s": _num(d, "dwell_s", None, 1, 30)}


@bp.route("/api/map/measure", methods=["POST"])
@api_login_required
@_guard
def map_measure():
    eng = _eng()
    from arislib import locate
    d = _body()
    a = _measure_args(d)
    if a["freq_mhz"] is None:
        raise ValueError("Enter a frequency")
    sid = int(d["session_id"]) if d.get("session_id") else None
    fresh = bool(d.get("new_session"))
    if d.get("survey"):
        out = locate.run_measure(eng, a["freq_mhz"], a["bw_khz"], a["dwell_s"], sid, kind="survey",
                                 node_id=d.get("node_id"), at=(_num(d, "x"), _num(d, "y")), new_session=fresh)
    else:
        out = locate.run_measure(eng, a["freq_mhz"], a["bw_khz"], a["dwell_s"], sid, new_session=fresh)
    if d.get("mode") == "strength":
        out.update({k: v for k, v in locate.analyse(out["session"]["id"], "strength").items()
                    if k in ("heat", "summary", "mode")})
    return jsonify(out)


@bp.route("/api/map/session/<int:sid>", methods=["GET", "DELETE"])
@api_login_required
@_guard
def session_view(sid):
    from arislib import locate, store
    if request.method == "DELETE":
        from .auth import role as _role
        if _role() != "admin":
            return jsonify({"error": "only the admin account can do that"}), 403
        store.delete_session(sid)
        return jsonify({"ok": True})
    return jsonify(locate.analyse(sid, request.args.get("mode", "source")))


@bp.route("/api/map/session/<int:sid>/clear", methods=["POST"])
@api_admin_required
@_guard
def session_clear(sid):
    from arislib import locate, store
    kind = _body().get("kind")
    store.delete_readings(sid, kind if kind in ("fixed", "survey") else None)
    return jsonify(locate.analyse(sid))


@bp.route("/api/map/calibrate", methods=["POST"])
@api_admin_required
@_guard
def calibrate():
    eng = _eng()
    from arislib import locate, nodes
    d = _body()
    if d.get("clear"):
        nodes.clear_offsets(_num(d, "freq_mhz", None, 24, 1766))
        return jsonify({"ok": True, "nodes": nodes.list_nodes()})
    a = _measure_args(d)
    if a["freq_mhz"] is None:
        raise ValueError("Enter the frequency of the transmitter you are using")
    out = locate.calibrate(eng, a["freq_mhz"], a["bw_khz"], a["dwell_s"])
    out["nodes"] = nodes.list_nodes()
    return jsonify(out)
