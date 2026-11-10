"""The ARIS web app: login, the single-page GUI, the chat API, and the tab APIs (blueprints)."""
from __future__ import annotations

import atexit
import re
import os
import signal
import sys
import threading
import time
from datetime import timedelta

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from . import activity, auth, bluetooth, devices, jobs, mapapi, spectrum, wifi
from .engine import engine as _engine, version

HOST = os.environ.get("ARIS_GUI_HOST", "0.0.0.0")
PORT = int(os.environ.get("ARIS_GUI_PORT", "5000"))
MAX_UPLOAD = 13 * 1024 * 1024

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True, MAX_CONTENT_LENGTH=MAX_UPLOAD,
                  PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
app.register_blueprint(spectrum.bp)
app.register_blueprint(activity.bp)
app.register_blueprint(mapapi.bp)
app.register_blueprint(jobs.bp)
app.register_blueprint(devices.bp)
app.register_blueprint(wifi.bp)
app.register_blueprint(bluetooth.bp)
SECRETS = {}
BRIDGE = {"ai": None}


@app.before_request
def _csrf():
    """Changes need the X-ARIS header, which other websites cannot add (the session cookie alone is not enough)."""
    if request.method in ("POST", "PUT", "DELETE") and request.path.startswith("/api/"):
        if request.headers.get("X-ARIS") != "1":
            return jsonify({"error": "missing X-ARIS header"}), 403


@app.after_request
def _headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    if auth.via_proxy() and request.headers.get("X-Forwarded-Proto") == "https":
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"      # the page came over HTTPS (tailscale serve)
    if request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "private, max-age=86400"      # file names carry ?v=<version>
    elif request.path.endswith("/image") and resp.status_code == 200:
        resp.headers["Cache-Control"] = "private, max-age=3600"
    else:
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        ip = auth.client_ip()
        if auth.locked_out(ip):
            return render_template("login.html", error="Too many failed attempts. Try again in 10 minutes."), 429
        who = auth.check_login(request.form.get("username"), request.form.get("password"), SECRETS)
        if who:
            session.clear()
            session["logged_in"] = True
            session["user"] = who
            session["role"] = "admin" if auth.is_admin(who, SECRETS) else "viewer"
            session.permanent = True
            return redirect(url_for("dashboard"))
        auth.record_fail(ip)
        error = "Invalid username or password"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
def dashboard():
    if not auth.authed():
        return redirect(url_for("login"))
    v = version()
    return render_template("index.html", version=v, asset_v=v)


@app.route("/api/chat_state")
@auth.api_login_required
def chat_state():
    ai = BRIDGE["ai"]
    if ai is None:
        return jsonify({"seq": 0, "messages": [], "busy": False, "status": "AI chat not started"})
    ai.pull_notices()                                    # a background capture may have finished
    out = {"seq": ai.seq, "busy": ai.busy, "status": ai.status_line}
    if request.args.get("seq") != str(ai.seq):          # unchanged: a few bytes instead of the whole chat
        out["messages"] = ai.messages
    return jsonify(out)


@app.route("/api/chat_send", methods=["POST"])
@auth.api_login_required
def chat_send():
    data = request.get_json(force=True, silent=True) or {}
    msg = str(data.get("message") or "").strip()
    who = auth.role()
    if who != "admin" and _ADMIN_CHAT_RX.search(msg):
        return jsonify({"error": "only the admin account can run that"}), 403
    if msg and BRIDGE["ai"] is not None:
        BRIDGE["ai"].ask_async(msg, role=who)          # (the console refuses the set-up tools for a viewer)
    return jsonify({"ok": True})


# Chat requests an extra account may not make: the ones that delete or change the station's set-up. A first line
# only; the console enforces the role on every tool and command (engine _ADMIN_TOOLS, _ADMIN_COMMANDS).
_ADMIN_CHAT_RX = re.compile(r"^/(?:cleanup|clear-signals|setup|patrol)\b|\bclear\s+(?:the\s+)?(?:data|logs|signals)\b|"
                            r"\b(?:run|do|start|redo|re-?run)\s+(?:the\s+|a\s+)?(?:full\s+|auto\s+)?set\s?-?up\b|"
                            r"\bset\s?-?up\s+(?:everything|it\s+all|the\s+(?:whole\s+)?(?:system|dongles|station))\b|"
                            r"\bset\s+(?:everything|it\s+all|the\s+(?:whole\s+)?(?:system|dongles|station))\s+up\b|"
                            r"\b(?:forget|remove|drop|delete)\b.*\bdongle|\bdongle\b.*\b(?:forget|remove|drop|delete)\b", re.I)


@app.route("/api/chat_reset", methods=["POST"])
@auth.api_login_required
def chat_reset():
    """Reset Scanner: stop everything that uses a dongle (Decode, Name all, captures, patrol, the chat's own work),
    restart the AI, and report in the chat what was stopped. Runs in the background; the chat shows the result.
    {"scope": "chat"} (the chat's own Stop button) only restarts the chat console: captures and jobs keep running."""
    data = request.get_json(force=True, silent=True) or {}
    if data.get("scope") == "chat":
        if BRIDGE["ai"] is not None:
            threading.Thread(target=BRIDGE["ai"].reset, name="chat-stop", daemon=True).start()
        return jsonify({"ok": True, "scope": "chat"})
    threading.Thread(target=_reset_worker, name="reset", daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/clear_data", methods=["POST"])
@auth.api_admin_required
def clear_data():
    """Clear data (a chat quick action, confirmed on the page): stops everything, then wipes every log and result
    (baselines too only when asked). The chat starts afresh with the summary."""
    data = request.get_json(force=True, silent=True) or {}
    base = bool(data.get("baselines"))
    bridge = BRIDGE["ai"]
    if bridge is None:
        jobs.LAST_RESET["message"] = jobs.clear_all(base)
        return jsonify({"ok": True, "message": jobs.LAST_RESET["message"]})
    threading.Thread(target=lambda: bridge.reset(stop=lambda: jobs.clear_all(base), clear=True), name="clear",
                     daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/clear_logs", methods=["POST"])
@auth.api_admin_required
def clear_logs():
    """Clear logs (the Activity tab's button, one confirm on the page): the event logs and result lists go, the
    Signals list, station names, device history, baselines and settings stay, captures keep running. Runs here, in
    the process that owns the files; the chat console only reads them."""
    r = jobs.clear_logs()
    if BRIDGE["ai"] is not None:
        BRIDGE["ai"].forget_notices()       # the notices file is gone: the next notice is read from its start
    return jsonify(r)


RESET_NOTE = "reset_note.txt"


def _reset_worker():
    bridge = BRIDGE["ai"]
    if bridge is None:                          # (no chat console: tests, or it failed to start)
        jobs.LAST_RESET["message"] = jobs.reset_all()
        return
    bridge.reset(stop=jobs.reset_all)
    if jobs.RESTART["needed"]:                  # a stuck thread still holds a dongle: only a fresh process frees it
        _restart_web_server(bridge.messages[-1]["text"] if bridge.messages else "")


def _restart_web_server(note: str) -> None:
    """Start this web server again in the same process slot (systemd and a terminal keep working), after ending the
    chat console. The next start shows `note` in the chat."""
    from .bridge import stop_child
    try:
        with open(os.path.join(str(_engine().DATA_DIR), RESET_NOTE), "w") as f:
            f.write(note[:600])
    except OSError:
        pass
    time.sleep(1.5)                             # let the page fetch the message first
    stop_child(BRIDGE["ai"])
    sys.stdout.flush()
    os.execv(sys.executable, [sys.executable] + sys.argv)


def _reset_note() -> str:
    path = os.path.join(str(_engine().DATA_DIR), RESET_NOTE)
    try:
        with open(path) as f:
            note = f.read().strip()
        os.unlink(path)
        return note
    except OSError:
        return ""


def configure(secrets_dict: dict) -> Flask:
    """Set the login secrets (tests call this too)."""
    SECRETS.clear()
    SECRETS.update(secrets_dict)
    app.secret_key = SECRETS["secret_key"]
    return app


def main() -> None:
    if "--set-password" in sys.argv:
        auth.cmd_set_password()
        return
    for flag, fn in (("--add-user", auth.cmd_add_user), ("--remove-user", auth.cmd_remove_user)):
        if flag in sys.argv:
            i = sys.argv.index(flag)
            fn(sys.argv[i + 1] if i + 1 < len(sys.argv) else "")
            return
    sec = auth.load_secrets()
    if not sec:
        print(f"No GUI login found. Create one first:  python3 {sys.argv[0]} --set-password")
        sys.exit(1)
    configure(sec)
    try:
        _engine().upgrade_signal_db()
    except Exception as e:
        print(f"Note: could not check the Saved Signals list ({type(e).__name__}: {e})")
    from .bridge import ArisAIBridge, _lan_ips, stop_child
    jobs.init_internal_token()
    BRIDGE["ai"] = ArisAIBridge(extra_env=jobs.env_for_console(
        f"http://{'127.0.0.1' if HOST in ('0.0.0.0', '', '::') else HOST}:{PORT}"))
    note = _reset_note()
    if note:                         # Reset Scanner restarted the web server to free a dongle
        BRIDGE["ai"]._add("ai", "Reset Scanner restarted the web server to free a dongle that a stuck task held. "
                                "The dongles are free again.")
    resumed = jobs.resume()          # captures run in this process from now on (after the chat, so it sees the notice)
    wifi.start_watch()               # v4.13.0: Wi-Fi history and notices also when no page is open
    atexit.register(lambda: stop_child(BRIDGE["ai"]))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print(f"ARIS v{version()} GUI ready. Open one of these in a browser:")
    if resumed:
        print("  Resumed background capture(s): " + ", ".join(resumed))
    if HOST in ("0.0.0.0", "127.0.0.1", "localhost"):
        print(f"  On this PC (no Wi-Fi needed): http://127.0.0.1:{PORT}")
    if HOST in ("127.0.0.1", "localhost"):
        print("  From the phone: only through the HTTPS front (tailscale serve; deploy/tailscale-https.sh): "
              "this server listens on this PC only.")
    if HOST == "0.0.0.0":
        for ifname, ip in _lan_ips():
            print(f"  From your phone via {ifname}: http://{ip}:{PORT}")
    sys.stdout.flush()
    app.run(host=HOST, port=PORT, threaded=True)
