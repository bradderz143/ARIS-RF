#!/usr/bin/env python3
"""ARIS Bluetooth helper (v4.11.0), run by aris-bt.service. LISTENS ONLY: it starts `ubertooth-btle -n`, which prints the
advertisements it hears and sends nothing (the options of that tool that follow, interfere with or fake a connection are
never given; a test fails if they appear in this file). Every EVERY seconds it writes a SUMMARY to
$RUNTIME_DIRECTORY/scan.json for the ARIS web server: counts, the manufacturers and service types devices announce,
signal strength, and a keyed hash label per device. Addresses, device names and payloads are never written anywhere.
The label key is made once and kept in $STATE_DIRECTORY (mode 600), so a device keeps its label across restarts."""
import json
import os
import select
import secrets
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from arislib import bluetooth as bt   # noqa: E402

COMMAND = ["ubertooth-btle", "-n"]        # passive: advertisements only
EVERY = int(os.environ.get("ARIS_BT_EVERY", "60"))
STALL_S = 30                               # no output at all for this long: the tool is stuck (it normally prints hundreds of
                                           # advertisements a second here; it went silent about every half hour on the station), start it again


def _key() -> bytes:
    d = os.environ.get("STATE_DIRECTORY")
    if not d:
        return secrets.token_bytes(32)
    path = os.path.join(d.split(":")[0], "key")
    try:
        with open(path, "rb") as f:
            k = f.read()
        if len(k) >= 32:
            return k
    except OSError:
        pass
    k = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(k)
    return k


def _write(out: str, snap: dict):
    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(snap, f, separators=(",", ":"))
    os.replace(tmp, out)


def main() -> int:
    out = os.path.join(os.environ.get("RUNTIME_DIRECTORY", "/run/aris-bt").split(":")[0], "scan.json")
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))    # systemd's stop: leave through the finally below
    key = _key()
    p = subprocess.Popen(COMMAND, stdout=subprocess.PIPE, stderr=None, bufsize=0)
    parser, win = bt.AdvParser(), bt.Window(key)
    buf, t0, last = b"", time.time(), time.time()
    try:
        while True:
            ready, _, _ = select.select([p.stdout], [], [], 1.0)
            if ready:
                chunk = os.read(p.stdout.fileno(), 65536)
                if not chunk:
                    print("ubertooth-btle ended", file=sys.stderr)
                    return 1
                last = time.time()
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for ln in lines:
                    pkt = parser.feed(ln.decode("utf-8", "replace"))
                    if pkt:
                        win.add(pkt)
            now = time.time()
            if now - last > STALL_S:
                print("no output from ubertooth-btle for %d s: restarting" % STALL_S, file=sys.stderr)
                return 1
            if now - t0 >= EVERY:
                _write(out, win.snapshot(now, EVERY))
                win, t0 = bt.Window(key), now
    finally:
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()


if __name__ == "__main__":
    sys.exit(main())
