"""Activity tab (patrol findings, background captures and the watchdog log, recent investigations) and Signals tab
APIs. Both are read-only views of what ARIS logged."""
from __future__ import annotations

import json
import sqlite3
import time

from flask import Blueprint, jsonify, request

from .auth import api_login_required
from .engine import engine as _engine

bp = Blueprint("activity", __name__)


def _jload(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


_WD_CACHE = {"key": None, "summary": None}


def _watchdog_summary(eng):
    """The watchdog log is re-read only when it, the list of running watchdogs or the captures changed."""
    key = []
    for p in (eng.WATCHDOG_LOG, eng.WATCHDOG_STATE, eng.CAPTURES):
        try:
            st = p.stat()
            key.append((st.st_mtime_ns, st.st_size))
        except OSError:
            key.append(None)
    key = tuple(key)
    if _WD_CACHE["key"] != key:
        _WD_CACHE.update(key=key, summary=eng._watchdog_summary(max_events=0))
    return _WD_CACHE["summary"]


@bp.route("/api/signals")
@api_login_required
def api_signals():
    """Saved signals, newest first. ?have=<tag> from the last reply answers {"unchanged": true} when nothing was
    added or removed since, so the page does not download the whole list after every chat reply."""
    try:
        eng = _engine()
        eng.init_db()
        conn = sqlite3.connect(eng.DB_PATH, timeout=5)
        try:
            n, top = conn.execute("SELECT COUNT(*), MAX(id) FROM peaks").fetchone()
            tag = f"{n}-{top}"
            if request.args.get("have") == tag:
                return jsonify({"unchanged": True, "tag": tag})
            rows = conn.execute("SELECT id, timestamp, frequency_mhz, power_db, snr_db, bw_khz, "
                                "urh_analysis, notes FROM peaks ORDER BY id DESC LIMIT 200").fetchall()
        finally:
            conn.close()
    except Exception:
        rows, tag = [], "0-None"
    keys = ("id", "timestamp", "frequency_mhz", "power_db", "snr_db", "bw_khz", "urh_analysis", "notes")
    # (no ook_hex / fsk_hex since v4.9.4: the demodulated bits of a burst are never kept or served)
    out = []
    for r in rows:
        d = dict(zip(keys, r))
        d["frequency_mhz"] = float(d["frequency_mhz"] or 0.0)
        out.append(d)
    return jsonify({"tag": tag, "count": n if rows else 0, "signals": out})


@bp.route("/api/disk")
@api_login_required
def api_disk():
    """The Activity tab's line: what the data folder holds (logs, signals, baselines, other) and the free space.
    The engine caches it for 30 s, so the tab can ask every time it is shown."""
    try:
        return jsonify(_engine().disk_usage())
    except Exception as e:
        return jsonify({"error": f"could not read the data folder: {type(e).__name__}: {e}"}), 500


@bp.route("/api/activity")
@api_login_required
def api_activity():
    """?brief=1 (the badge check every 15 s while another tab is open) returns only the flagged patrol rounds."""
    try:
        eng = _engine()
    except Exception as e:
        return jsonify({"error": f"ARIS engine not available: {type(e).__name__}: {e}"}), 500
    # today's threat levels and dongle names (rounds from older versions said CRITICAL / MODERATE)
    rounds = eng._patrol_rounds(20)
    caps = _jload(eng.CAPTURES, [])[-10:][::-1]
    if request.args.get("brief"):
        return jsonify({"now": time.time(), "flagged_rounds": [
            {"t": r["t"], "top": [{"threat_level": ev.get("threat_level")} for ev in r.get("top") or []]}
            for r in rounds[::-1]], "captures": [{"t": c.get("t"), "outcome": c.get("outcome")} for c in caps]})
    patrol = _jload(eng.PATROL_STATE, {"running": False})
    patrol.pop("recent_runs", None)
    try:
        wd = _watchdog_summary(eng)
    except Exception:
        wd = {}
    checks = _jload(eng.RECENT_CHECKS, [])[-10:][::-1]
    return jsonify({"now": time.time(), "patrol": patrol, "flagged_rounds": rounds[::-1], "watchdog": wd,
                    "captures": [{k: c.get(k) for k in ("t", "finished", "freq_mhz", "dongle", "serial", "outcome",
                                                       "verdict", "seconds", "decoder", "mode", "band",
                                                       "band_label")} for c in caps],
                    "checks": [dict({k: v for k, v in c.items() if k != "epoch"},
                                    evidence_text=eng._EVIDENCE_TEXT.get(c.get("evidence"), ""))
                               for c in checks]})
