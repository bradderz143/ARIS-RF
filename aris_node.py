#!/usr/bin/env python3
"""
ARIS remote node: lets another computer with an RTL-SDR dongle (for example a Raspberry Pi in another room)
take signal-strength readings for the ARIS Map tab. Receive only: it measures how strong a frequency is when
ARIS asks, and nothing else. It never records audio or content.

  Set up:   python3 aris_node.py --init [--name "Kitchen Pi"] [--port 5055] [--serial 00000001] [--gain 30]
            (prints the address and the token to type into ARIS: Map tab > Place antennas > Add a remote node)
  Run:      python3 aris_node.py
  Needs:    python3, numpy, rtl-sdr (rtl_sdr, rtl_test) and the arislib/ folder next to this file
            (copy or git clone the whole ARIS folder).

Settings live in ~/.aris_node/config.json (mode 600). Every request must carry the token.
"""
from __future__ import annotations

import argparse
import fcntl
import hmac
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from arislib import measure  # noqa: E402

VERSION = "1.0"
CONF_DIR = Path(os.environ.get("ARIS_NODE_DIR", str(Path.home() / ".aris_node")))
CONF = CONF_DIR / "config.json"
MAX_BODY = 4096
_BUSY = threading.Lock()


def load_conf() -> dict:
    try:
        with open(CONF) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_conf(c: dict) -> None:
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONF_DIR, 0o700)
    fd = os.open(str(CONF), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(c, f, indent=1)


def dongles() -> list:
    """Serial numbers of the dongles rtl_test can see."""
    try:
        p = subprocess.run(["rtl_test", "-t"], capture_output=True, text=True, timeout=8)
        return re.findall(r"SN:\s*(\S+)", (p.stderr or "") + (p.stdout or ""))
    except (OSError, subprocess.TimeoutExpired):
        return []


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))          # no traffic is sent: this only picks the outgoing interface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


class _FileLock:
    """Same idea as ARIS's dongle lock: only one program uses the dongle at a time, and the dongle gets its USB
    settle time after the last program closed it (the time is kept in the lock file)."""

    def __init__(self, serial):
        self.path = f"/tmp/aris_node_{re.sub(r'[^A-Za-z0-9]', '_', str(serial))}.lock"
        self.fd = None

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            raise measure.MeasureError("the dongle is busy")
        try:
            last = float(os.pread(self.fd, 32, 0).decode().strip() or 0)
        except (OSError, ValueError):
            last = 0.0
        wait = min(measure.USB_SETTLE_S, measure.USB_SETTLE_S - (time.time() - last))
        if wait > 0:
            time.sleep(wait)
        return self

    def __exit__(self, *a):
        try:
            os.pwrite(self.fd, f"{time.time():.3f}".ljust(31).encode() + b"\n", 0)
        except OSError:
            pass
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def make_handler(conf):
    token = conf["token"]

    class H(BaseHTTPRequestHandler):
        server_version = "ARISNode/" + VERSION
        timeout = 15                             # a client that stops sending does not hold a thread for ever

        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authed(self):
            got = self.headers.get("X-ARIS-Token", "")
            if hmac.compare_digest(got.encode(), token.encode()):
                return True
            time.sleep(0.5)                       # slows down guessing
            self._send(401, {"error": "wrong token"})
            return False

        def do_GET(self):
            if self.path != "/health":
                return self._send(404, {"error": "not found"})
            if self._authed():
                self._send(200, {"ok": True, "node": conf.get("name"), "version": VERSION, "dongles": dongles(),
                                 "serial": conf.get("serial")})

        def do_POST(self):
            if self.path != "/measure":
                return self._send(404, {"error": "not found"})
            if not self._authed():
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, {"error": "bad request: Content-Length"})
            if n < 0 or n > MAX_BODY:                 # (a negative length would read until the client stops)
                return self._send(413, {"error": "request too large"})
            try:
                req = json.loads(self.rfile.read(n).decode() or "{}")
                f = float(req["freq_mhz"])
                bw = float(req.get("bw_khz") or 25.0)
                dwell = float(req.get("dwell_s") or 5.0)
                if not (24.0 <= f <= 1766.0 and 5.0 <= bw <= 400.0 and 0.5 <= dwell <= measure.MAX_DWELL_S):
                    raise ValueError("frequency, width or listen time out of range")
                gain = float(req.get("gain_db") if req.get("gain_db") is not None else conf.get("gain_db", 29.7))
            except (KeyError, ValueError, TypeError) as e:
                return self._send(400, {"error": f"bad request: {e}"})
            serial = conf.get("serial") or (dongles() or ["0"])[0]
            if not _BUSY.acquire(blocking=False):
                return self._send(409, {"error": "busy with another measurement"})
            try:
                with _FileLock(serial):
                    r = measure.measure_raw(serial, f, bw, dwell, gain, int(conf.get("ppm") or 0))
                r["node"] = conf.get("name")
                self._send(200, r)
            except measure.MeasureError as e:
                self._send(409, {"error": str(e)})
            except Exception as e:                    # (the detail stays in the node's own log, not on the wire)
                sys.stderr.write("%s measure failed: %s: %s\n" % (time.strftime("%H:%M:%S"), type(e).__name__, e))
                self._send(500, {"error": "the measurement failed on the node; see its log"})
            finally:
                _BUSY.release()

    return H


def main(argv=None):
    ap = argparse.ArgumentParser(description="ARIS remote node (signal strength readings for the Map tab)")
    ap.add_argument("--init", action="store_true", help="create settings and a new token")
    ap.add_argument("--name", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--serial", default=None, help="dongle serial (default: the first one found)")
    ap.add_argument("--gain", type=float, default=None, help="receiver gain in dB (default 29.7)")
    ap.add_argument("--bind", default=None, help="address to listen on (default 0.0.0.0)")
    a = ap.parse_args(argv)
    conf = load_conf()
    if a.init or not conf.get("token"):
        conf.setdefault("name", socket.gethostname())
        conf["token"] = secrets.token_urlsafe(24) if a.init or not conf.get("token") else conf["token"]
    for k, v in (("name", a.name), ("port", a.port), ("serial", a.serial), ("gain_db", a.gain), ("bind", a.bind)):
        if v is not None:
            conf[k] = v
    conf.setdefault("port", 5055)
    save_conf(conf)
    url = f"http://{lan_ip()}:{conf['port']}"
    if a.init:
        print(f"ARIS node '{conf['name']}' is set up. In ARIS: Map tab > Place antennas > Add a remote node:")
        print(f"  Address: {url}")
        print(f"  Token:   {conf['token']}")
        print("Then start it with:  python3 aris_node.py")
        return
    srv = ThreadingHTTPServer((conf.get("bind") or "0.0.0.0", int(conf["port"])), make_handler(conf))
    print(f"ARIS node '{conf['name']}' listening on {url} (dongles: {', '.join(dongles()) or 'none found'})")
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
