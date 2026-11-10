"""The update badge's API: is the code behind its git remote? The engine does the read-only check (update_state:
in the background, every few hours and after an update); the page shows a small "update" badge with the command to
run on the computer. The update itself is never run from the page (it needs root)."""
from __future__ import annotations

from flask import Blueprint, jsonify

from .auth import api_login_required
from .engine import engine as _engine

bp = Blueprint("update", __name__)
KEYS = ("enabled", "available", "state", "local", "remote", "branch", "checked_at", "command")


@bp.route("/api/update")
@api_login_required
def api_update():
    try:
        st = _engine().update_state(refresh=True)
    except Exception as e:                       # the badge is a convenience: never an error on the page
        return jsonify({"enabled": False, "available": False, "state": f"not available ({type(e).__name__})"})
    return jsonify({k: st.get(k) for k in KEYS})
