"""The band plan's region: which region's labels, FM channel plan and sensor bands ARIS uses (arislib.regions).

GET /api/region says which region is in force, whether it was guessed from the computer's time zone, and the
choices; POST /api/region {"id": "<region>", "country": "<code>" | null} sets it (the admin account only; a country
picks a country's own layout inside the region, null = the region-wide labels only). A change applies at once:
labels, the FM plan, sensor bands and the Spectrum presets all read the config's region when they run.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request

from arislib import regions as _regions

from .auth import api_admin_required, api_login_required
from .engine import engine as _engine

bp = Blueprint("region", __name__)


def _body(eng):
    st = eng._region_state()
    return {"id": st["id"], "name": st["name"], "short": st["short"], "country": st["country"],
            "country_name": st["country_name"], "guessed": bool(st["guessed"]), "source": st["source"],
            "line": _regions.describe(st), "choices": _regions.choices(), "countries": _regions.countries()}


@bp.route("/api/region")
@api_login_required
def api_region():
    try:
        return jsonify(_body(_engine()))
    except Exception as e:
        return jsonify({"error": f"ARIS engine not available: {type(e).__name__}: {e}"}), 500


@bp.route("/api/region", methods=["POST"])
@api_admin_required
def api_region_set():
    data = request.get_json(silent=True) or {}
    rid = str(data.get("id") or "").strip().lower()
    country = data.get("country")
    if country is not None and not isinstance(country, str):
        return jsonify({"error": "The country must be a country code such as US, or null."}), 400
    if rid not in _regions.REGION_IDS:
        return jsonify({"error": f"There is no region '{rid}'. Choose one of: {', '.join(_regions.REGION_IDS)}."}), 400
    try:
        eng = _engine()
        eng._set_region(rid, country)
    except _regions.RegionError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    return jsonify(_body(eng))
