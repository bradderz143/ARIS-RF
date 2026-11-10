"""Decode jobs (the Spectrum tab's Decode button) and background captures, run inside the GUI process.

Decode: POST /api/decode {freq_mhz, seconds} investigates one frequency on every dongle for that long (the same
investigation as the chat) in a background thread; GET /api/decode/<job> reports progress and, at the end, a
plain result card. One decode at a time.

Captures: POST /api/capture/start {freq_mhz, minutes} and /api/capture/stop {serial | freq_mhz}. Captures live
in this process (the GUI runs as a service), so pressing Stop in the chat, which restarts the chat console, does
not end them. The chat console asks for captures through /api/internal/watchdog with a per-start token that only
it knows (it gets it in its environment).
"""
from __future__ import annotations

import hmac
import math
import secrets
import threading
import time
import uuid

from flask import Blueprint, jsonify, request

from .auth import api_login_required
from .engine import engine as _engine

bp = Blueprint("jobs", __name__)
INTERNAL = {"token": None}
JOBS = {}
_LOCK = threading.Lock()
MAX_JOBS = 20


def init_internal_token() -> str:
    """A fresh token for the chat console (passed in its environment, never stored)."""
    INTERNAL["token"] = secrets.token_urlsafe(24)
    return INTERNAL["token"]


def _running():
    return next((j for j in JOBS.values() if j["state"] == "running"), None)


def _public(job):
    out = {k: job[k] for k in ("id", "state", "freq_mhz", "seconds", "started", "progress", "error")}
    out["elapsed"] = round(time.time() - job["t0"], 1)
    out["expected"] = job["expected"]
    if job.get("card"):
        out["card"] = job["card"]
    if job.get("names"):
        out["names"] = job["names"]
    elif job.get("partial"):
        out["partial"] = job["partial"]
    return out


def _decode_worker(job):
    eng = _engine()
    eng._QUIET.on = True                                  # progress goes to the job, not to the GUI's log

    def sink(line):
        if line:
            job["progress"] = line.replace("[LISTEN]", "").replace("[DECODE]", "").strip()[:160]
    eng._QUIET.sink = sink
    try:
        with eng.job_scope():                             # Reset Scanner can stop it
            res = eng.investigate_single_frequency(frequency_mhz=job["freq_mhz"], listen_seconds=job["seconds"],
                                                   dongle=job.get("dongle"))
        job["card"] = eng._investigation_card(res)
        job["state"] = "done"
    except eng.RFError as e:
        job["error"], job["state"] = str(e), "error"
    except Exception as e:                                # never leave a job "running" forever
        job["error"], job["state"] = f"{type(e).__name__}: {e}", "error"
    finally:
        eng._QUIET.on, eng._QUIET.sink = False, None


@bp.route("/api/decode", methods=["POST"])
@api_login_required
def api_decode():
    data = request.get_json(force=True, silent=True) or {}
    try:
        f = float(data.get("freq_mhz"))
        secs = int(float(data.get("seconds") or 30))
    except (TypeError, ValueError):
        return jsonify({"error": "freq_mhz and seconds must be numbers"}), 400
    eng = _engine()
    if not eng.MIN_MHZ <= f <= eng.MAX_MHZ:
        return jsonify({"error": f"{f:g} MHz is outside the tuner range"}), 400
    secs = max(3, min(eng.MAX_LISTEN_S, secs))
    with _LOCK:
        cur = _running()
        if cur:
            return jsonify({"error": f"Already decoding {cur['freq_mhz']:g} MHz", "job": cur["id"]}), 409
        job = {"id": uuid.uuid4().hex[:12], "state": "running", "freq_mhz": round(f, 5), "seconds": secs,
               "dongle": data.get("dongle") or None, "started": time.strftime("%H:%M:%S"), "t0": time.time(),
               "progress": "Starting...", "error": None,
               "expected": secs + (3 if eng._band_kind(f) in ("sensor", "other", "audio") else 0) + 6}
        JOBS[job["id"]] = job
        for k in sorted(JOBS, key=lambda k: JOBS[k]["t0"])[:-MAX_JOBS]:
            JOBS.pop(k, None)
    threading.Thread(target=_decode_worker, args=(job,), name="decode-" + job["id"], daemon=True).start()
    return jsonify({"job": job["id"], **_public(job)})


def _names_worker(job):
    eng = _engine()
    eng._QUIET.on = True

    def sink(line):
        if line:
            job["progress"] = line.strip()[:160]
    eng._QUIET.sink = sink

    def partial(rep):                                     # stations named so far: the list fills in as they come
        job["partial"] = {"named": rep["named"], "not_named": rep["not_named"]}
    try:
        with eng.job_scope():                             # Reset Scanner can stop it
            job["names"] = eng.name_fm_stations(job["freqs"], best=job.get("best"), on_result=partial,
                                                on_plan=lambda more: job.update(
                                                    expected=int(time.time() - job["t0"] + more)))
        job["state"] = "done"
    except eng.RFError as e:
        job["error"], job["state"] = str(e), "error"
    except Exception as e:
        job["error"], job["state"] = f"{type(e).__name__}: {e}", "error"
    finally:
        eng._QUIET.on, eng._QUIET.sink = False, None


@bp.route("/api/fm_names", methods=["POST"])
@api_login_required
def api_fm_names():
    """Read the RDS name of every FM station in the list (the Spectrum tab's "Name all FM stations")."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        freqs = [float(f) for f in (data.get("freqs") or [])][:40]
    except (TypeError, ValueError):
        return jsonify({"error": "freqs must be numbers"}), 400
    best = data.get("best") if isinstance(data.get("best"), dict) else {}     # {channel: dongle that hears it best}
    best = {str(k)[:12]: str(v)[:24] for k, v in list(best.items())[:40]}
    if not freqs:
        return jsonify({"error": "no FM stations in the list"}), 400
    eng = _engine()
    chans = sorted({round(f, 1) for f in freqs if eng._in_fm_band(f)})
    n_dongles = max(1, min(2, len(eng._working_serials())))
    slices = len(eng._plan_fm_blocks(chans)) if chans else 1
    with _LOCK:
        cur = _running()
        if cur:
            what = "naming FM stations" if cur.get("kind") == "fm_names" else f"decoding {cur['freq_mhz']:g} MHz"
            return jsonify({"error": f"Busy {what} (Reset Scanner stops it)", "job": cur["id"]}), 409
        job = {"id": uuid.uuid4().hex[:12], "state": "running", "freq_mhz": round(freqs[0], 1),
               "seconds": int(eng.FM_NAME_LOOK_S), "freqs": freqs, "best": best, "kind": "fm_names",
               "started": time.strftime("%H:%M:%S"), "t0": time.time(), "progress": "Starting...", "error": None,
               # one capture per 1 MHz slice, the dongles in parallel; the retries add to it once they are planned
               "expected": int(math.ceil(slices / n_dongles) * (eng.FM_NAME_LOOK_S + 1.5) + 15)}
        JOBS[job["id"]] = job
    threading.Thread(target=_names_worker, args=(job,), name="fmnames-" + job["id"], daemon=True).start()
    return jsonify({"job": job["id"], **_public(job)})


@bp.route("/api/decode/<job_id>")
@api_login_required
def api_decode_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "no such job (the GUI may have restarted)"}), 404
    return jsonify(_public(job))


def _tool(name, **kw):
    """Run an engine tool here (in the GUI process) and return its JSON reply as a dict."""
    import json
    return json.loads(_engine().TOOLS[name](**kw))


@bp.route("/api/capture/start", methods=["POST"])
@api_login_required
def api_capture_start():
    data = request.get_json(force=True, silent=True) or {}
    r = _tool("start_background_watchdog", freq_mhz=data.get("freq_mhz"), dongle=data.get("dongle"),
              mode=data.get("mode"), max_minutes=data.get("minutes"), band=data.get("band"))
    return jsonify(r), (200 if r.get("status") == "ok" else 409)


@bp.route("/api/capture/stop", methods=["POST"])
@api_login_required
def api_capture_stop():
    data = request.get_json(force=True, silent=True) or {}
    r = _tool("stop_background_watchdog", dongle=data.get("serial") or data.get("dongle"),
              freq_mhz=data.get("freq_mhz"))
    return jsonify(r), (200 if r.get("status") == "ok" else 409)


@bp.route("/api/internal/watchdog", methods=["POST"])
def api_internal_watchdog():
    """The chat console asks the GUI to run (or stop) a capture here. Token only, from this machine."""
    if request.remote_addr not in ("127.0.0.1", "::1", "::ffff:127.0.0.1") or request.headers.get("X-Forwarded-For"):
        #                                                                         this machine only, and not through the
        #                                                                         HTTPS proxy (it connects from 127.0.0.1
        #                                                                         for the whole network); (the console
        #                                                                         talks to 127.0.0.1; '::' binds see it
        #                                                                         IPv4-mapped)
        return jsonify({"status": "error", "error": "forbidden"}), 403
    tok = INTERNAL.get("token")
    got = request.headers.get("X-ARIS-Internal", "")
    if not tok or not hmac.compare_digest(got.encode(), tok.encode()):
        return jsonify({"status": "error", "error": "forbidden"}), 403
    data = request.get_json(force=True, silent=True) or {}
    args = {k: v for k, v in (data.get("args") or {}).items()
            if k in ("freq_mhz", "dongle", "mode", "max_minutes", "band")}
    name = {"start": "start_background_watchdog", "stop": "stop_background_watchdog"}.get(data.get("op"))
    if not name:
        return jsonify({"status": "error", "error": "unknown op"}), 400
    return jsonify(_tool(name, **args))


def reset_all() -> str:
    """Reset Scanner (GUI side): stop the Decode / Name all job, every capture and patrol, end the radio programs,
    and say in plain words what was stopped and which dongles are free. Returns the message; sets RESTART when a
    stuck thread still holds a dongle (only restarting the web server frees it)."""
    eng = _engine()
    running = [j for j in list(JOBS.values()) if j["state"] == "running"]
    r = eng.stop_everything()
    for j in running:                                     # never leave a job looking "running"
        if j["state"] == "running":
            j["error"], j["state"] = "Stopped by Reset Scanner.", "error"
    what = [("Name all FM stations" if j.get("kind") == "fm_names" else f"the Decode on {j['freq_mhz']:g} MHz")
            for j in running] + r["stopped"]
    ended = r["radio_programs_ended"]
    msg = ("Scanner reset. Stopped " + _join(what) + "." if what else "Scanner reset: nothing was running.")
    if ended:
        msg += f" Ended {ended} radio program{'s' if ended != 1 else ''}."
    if r["free"]:
        msg += f" {_join(r['free'])} {'is' if len(r['free']) == 1 else 'are'} free."
    if r.get("missing"):
        msg += f" {_join(r['missing'])} {'is' if len(r['missing']) == 1 else 'are'} not plugged in."
    RESTART["needed"] = bool(r["stuck_here"])
    LAST_RESET["message"] = None
    for b in r["busy"]:
        if b["in_this_process"]:
            msg += (f" {b['dongle']} was still held by a stuck task ({b['held_by']}), so ARIS is restarting its web "
                    f"server to free it: the page reconnects in a few seconds.")
        else:
            msg += (f" {b['dongle']} is held by another program ({b['held_by']}), for example a terminal ARIS: close "
                    f"it to free the dongle.")
    return msg


def clear_all(baselines: bool = False) -> str:
    """Clear data: Reset Scanner first (nothing may write while the files go), then every log and result."""
    eng = _engine()
    stopped = reset_all()
    with _LOCK:
        for k in [k for k, j in JOBS.items() if j["state"] != "running"]:
            JOBS.pop(k, None)
    try:
        r = eng.clear_data(baselines=bool(baselines))
    except eng.RFError as e:
        return f"Could not clear the data: {e}"
    gone = [f"{r['signals']} saved signal{'s' if r['signals'] != 1 else ''}",
            f"{r['log_entries']} log entr{'ies' if r['log_entries'] != 1 else 'y'} (captures, patrol, audits)",
            f"{r['captures']} capture result{'s' if r['captures'] != 1 else ''}",
            f"{r['fm_stations']} FM station name{'s' if r['fm_stations'] != 1 else ''}"]
    if r["locate_readings"]:
        gone.append(f"{r['locate_readings']} locate readings")
    if r["baselines"]:
        gone.append(f"{r['baselines']} baselines")
    kept = "settings, logins, the Map's antennas and floorplans" + ("" if r["baselines"] else " and the baselines")
    first = stopped.split(". ", 1)[1] if stopped.startswith("Scanner reset. ") else ""
    return ("All data cleared: " + _join(gone) + ", the investigation reports and this chat. Kept: " + kept + "."
            + (f" (First: {first.rstrip('.')}.)" if first and not first.startswith("nothing") else ""))


def clear_logs() -> dict:
    """Clear logs: the engine deletes its logs and lists (nothing is stopped), and the Activity tab's cache is
    dropped so the next view reads the fresh files."""
    eng = _engine()
    r = eng.clear_logs()
    from . import activity
    activity._WD_CACHE.update(key=None, summary=None)
    return {"ok": True, **r}


RESTART = {"needed": False}
LAST_RESET = {"message": None}


def _join(items) -> str:
    items = [str(x) for x in items]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


def env_for_console(url: str) -> dict:
    """Environment the chat console needs to reach this GUI process."""
    return {"ARIS_GUI_TOKEN": INTERNAL["token"] or init_internal_token(), "ARIS_GUI_URL": url}


def resume():
    """GUI start-up: this process hosts captures from now on; resume the ones a restart interrupted."""
    eng = _engine()
    eng.GUI_HOST = True
    try:
        return eng.resume_captures()
    except Exception as e:                                # a resume problem must not stop the GUI starting
        print(f"Note: could not resume background captures ({type(e).__name__}: {e})")
        return []

