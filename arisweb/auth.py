"""GUI login. The username, password hash and session key live in DATA_DIR/gui_secrets.json (mode 600), never in
the source. Five failed logins from one address lock it out for 10 minutes.

Extra accounts (e.g. a 'viewer' login for someone who may look and measure but not change settings) are only password hashes under
"extra_users" in the same file: `aris_gui.py --add-user NAME` reads a hash on stdin (made where the password is
kept, so the password itself never reaches this file), `--remove-user NAME` takes one away. Logins read the
file each time, so adding or removing one needs no restart."""
from __future__ import annotations

import getpass
import json
import os
import secrets
import sys
import threading
import time
from collections import deque
from functools import wraps
from pathlib import Path

DATA_DIR = Path(os.environ.get("ARIS_DATA_DIR") or (Path.home() / "aris_data"))
SECRETS_PATH = DATA_DIR / "gui_secrets.json"

_FAILS = {}
_FAILS_LOCK = threading.Lock()


def _write_secrets(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(SECRETS_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.chmod(SECRETS_PATH, 0o600)


def load_secrets():
    try:
        with open(SECRETS_PATH) as f:
            d = json.load(f)
        if d.get("secret_key") and d.get("admin_hash") and d.get("admin_user"):
            return d
    except (OSError, ValueError):
        pass
    return None


def cmd_set_password() -> None:
    from werkzeug.security import generate_password_hash
    old = load_secrets() or {}
    user = input(f"Username [{old.get('admin_user', 'admin')}]: ").strip() or old.get("admin_user", "admin")
    pw = getpass.getpass("New password (min 8 chars): ")
    if len(pw) < 8 or pw != getpass.getpass("Repeat password: "):
        print("Passwords too short or do not match. Nothing changed.")
        sys.exit(1)
    _write_secrets(dict(old, admin_user=user, admin_hash=generate_password_hash(pw),
                        secret_key=old.get("secret_key") or secrets.token_hex(32)))
    print(f"Saved login for '{user}' to {SECRETS_PATH}")


_HASH_PREFIXES = ("scrypt:", "pbkdf2:")


def check_login(user, password, fallback=None) -> str:
    """The account name when user/password match the admin or an extra account, else ''. Reads the secrets file
    each time (an added or removed account counts at once); `fallback` when it cannot be read."""
    from werkzeug.security import check_password_hash
    sec = load_secrets() or fallback or {}
    user, password = str(user or ""), str(password or "")
    if not user or not password:
        return ""
    if user == sec.get("admin_user"):
        return user if check_password_hash(sec.get("admin_hash", ""), password) else ""
    h = (sec.get("extra_users") or {}).get(user)
    if h:
        return user if check_password_hash(h, password) else ""
    check_password_hash(_DUMMY_HASH, password)     # an unknown name costs the same time as a wrong password
    return ""


def is_admin(user, fallback=None) -> bool:
    """Only the admin account changes or deletes things; the extra accounts (`--add-user`) look and measure."""
    sec = load_secrets() or fallback or {}
    return bool(user) and user == sec.get("admin_user")


# A real werkzeug hash (of a random string, never a password) so a login with an unknown name takes as long as
# one with a wrong password (the review: an unknown name returned at once, which told names apart by timing).
_DUMMY_HASH = ("scrypt:32768:8:1$9h3d5lI8QhE5Vb2b$3c0b4c6cb0a07ad4b5af5a8f4a2c7a1a2d9ef0bcf7b1a8e2c1b4d7f6e9a0c3b5d8e1f2a4b6c8"
               "d0e2f4a6b8c0d2e4f6a8b0c2d4e6f8a0b2c4d6e8f0a2b4c6d8e0f2a4b6c8d0e2f4a6b8c0d2e4f6a8b0c2")


def cmd_add_user(name: str, stream=None) -> None:
    """Add or replace an extra account from a password hash on stdin (werkzeug format)."""
    name = str(name or "").strip()
    h = (stream or sys.stdin).readline().strip()
    old = load_secrets()
    if not old:
        print("No GUI login yet: create the main one first with --set-password. Nothing changed.")
        sys.exit(1)
    if not name or not name.replace("-", "").replace("_", "").isalnum() or name == old.get("admin_user"):
        print("Give a plain account name (letters, digits, - or _) other than the main login. Nothing changed.")
        sys.exit(1)
    if not h.startswith(_HASH_PREFIXES) or len(h) < 40 or " " in h:
        print("That is not a password hash (expected one line starting with scrypt: or pbkdf2:). Nothing changed.")
        sys.exit(1)
    extra = dict(old.get("extra_users") or {})
    extra[name] = h
    _write_secrets(dict(old, extra_users=extra))
    print(f"Added login '{name}' (the main login is unchanged).")


def cmd_remove_user(name: str) -> None:
    old = load_secrets() or {}
    extra = dict(old.get("extra_users") or {})
    if extra.pop(str(name or ""), None) is None:
        print(f"No extra login called '{name}'. Nothing changed.")
        sys.exit(1)
    _write_secrets(dict(old, extra_users=extra))
    print(f"Removed login '{name}'.")


LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def via_proxy() -> bool:
    """True when the request came through a reverse proxy on this machine (`tailscale serve` connects from 127.0.0.1
    and adds X-Forwarded-For): then 127.0.0.1 is the proxy, not a program on this PC."""
    from flask import request
    return (request.remote_addr or "") in LOOPBACK and bool(request.headers.get("X-Forwarded-For"))


def client_ip() -> str:
    """The address to count wrong passwords against. Behind the proxy every request is from 127.0.0.1, so one
    attacker would lock everybody out: use the address the proxy appended (its last entry) instead."""
    from flask import request
    ip = request.remote_addr or "?"
    if via_proxy():
        last = request.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
        if last and len(last) <= 45:
            return last
    return ip


def locked_out(ip: str) -> bool:
    with _FAILS_LOCK:
        q = _FAILS.setdefault(ip, deque())
        while q and time.time() - q[0] > 600:
            q.popleft()
        return len(q) >= 5


def record_fail(ip: str) -> None:
    with _FAILS_LOCK:
        _FAILS.setdefault(ip, deque()).append(time.time())


def authed() -> bool:
    from flask import session
    return bool(session.get("logged_in"))


def role() -> str:
    """'admin' or 'viewer' for the logged-in session. Sessions from before v4.9.4 carry no role (the cookie
    outlives an update): it is worked out from the user name then (v4.9.5 review)."""
    from flask import session
    r = session.get("role")
    if r not in ("admin", "viewer"):
        r = "admin" if is_admin(session.get("user")) else "viewer"
        session["role"] = r
    return r


def api_login_required(fn):
    """JSON APIs answer 401 instead of redirecting, so the page can send the user back to the login."""
    @wraps(fn)
    def wrapper(*a, **kw):
        from flask import jsonify
        if not authed():
            return jsonify({"error": "unauthorized"}), 401
        return fn(*a, **kw)
    return wrapper


def api_admin_required(fn):
    """Changing or deleting (Clear data, Clear logs, Forget devices, floorplans and nodes): the admin account only.
    An extra account gets 403 with a plain message (the review: extra accounts had every admin power)."""
    @wraps(fn)
    def wrapper(*a, **kw):
        from flask import jsonify, session
        if not authed():
            return jsonify({"error": "unauthorized"}), 401
        if role() != "admin":
            return jsonify({"error": "only the admin account can do that"}), 403
        return fn(*a, **kw)
    return wrapper
