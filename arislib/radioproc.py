"""Ending radio programs (rtl_sdr, rtl_power, rtl_fm, rtl_433, rtl_adsb, rtl_test) so that their dongle really is
free afterwards. Standard library only, no ARIS imports.

The rtl-sdr tools end on Ctrl+C (SIGINT) or SIGTERM: their handler cancels librtlsdr's streaming, and the program
then closes the dongle. That cancel can hang. On the real PC (2026-11-02, both dongles, rtl-sdr from Ubuntu 26.04)
`timeout -s INT -k 3 5 rtl_sdr ...` stopped writing at the signal but was still running 3 s later, and only
SIGKILL ended it (exit 137). A program that hangs like that never ends by itself, and while it lives it keeps its
dongle: whoever opens that dongle next fails. So a stop is always: the signal, a short grace, SIGKILL, and a wait
until the process has exited, all before the dongle's lock is released (the lock's USB settle time then counts
from that moment).
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time

STOP_GRACE_S = 1.5      # a clean exit after Ctrl+C takes well under a second; a hung one never comes
KILL_WAIT_S = 5.0       # SIGKILL ends a process at once (its USB device is let go as it exits), unless the kernel
                        # itself is stuck in the USB driver


def _drain(p):
    """Read and drop what p still writes to its stdout pipe until it closes (a thread), so p is never blocked
    writing while it handles its stop."""
    f = getattr(p, "stdout", None)
    if f is None:
        return None
    try:
        fd = f.fileno()
    except (OSError, ValueError):
        return None

    def run():
        while True:
            try:
                if not os.read(fd, 1 << 20):
                    return
            except (OSError, ValueError):
                return
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th


def stop(p, sig=signal.SIGINT, grace=STOP_GRACE_S, kill_wait=KILL_WAIT_S, drain=False) -> str:
    """End the program p (a subprocess.Popen) and reap it: `sig` (None: straight to SIGKILL), up to `grace` seconds
    for it to exit, then SIGKILL and up to `kill_wait` seconds. drain=True reads and drops its stdout meanwhile (only
    when nobody else reads it): on the real PC (2026-11-02) rtl_sdr writing into a pipe nobody read never ended on
    Ctrl+C (blocked in its write, its cancel never ran), and ended 0.06 s after Ctrl+C when the pipe was read.
    Safe to call from two threads at once. Returns how
    it ended: "exited" (it had already), "signal" (it exited after the signal), "killed" (it needed SIGKILL) or
    "stuck" (still not gone after SIGKILL: its dongle may stay busy until it is replugged)."""
    if p.poll() is not None:
        return "exited"
    th = _drain(p) if drain else None
    try:
        if sig is not None:
            try:
                p.send_signal(sig)
            except OSError:
                pass
            try:
                p.wait(timeout=max(0.0, grace))
                return "signal"
            except subprocess.TimeoutExpired:
                pass
        try:
            p.kill()
        except OSError:
            pass
        try:
            p.wait(timeout=max(0.0, kill_wait))
            return "killed"
        except subprocess.TimeoutExpired:
            return "stuck"
    finally:
        if th is not None:
            th.join(0.5)                 # the pipe closes as the program exits: the reader ends with it


def _stat(pid):
    """(state, process group) of a process from /proc, or None when there is no such process."""
    try:
        with open(f"/proc/{int(pid)}/stat") as f:
            st = f.read()
    except (OSError, ValueError):
        return None
    rest = st[st.rfind(")") + 2:].split()
    try:
        return rest[0], int(rest[2])
    except (IndexError, ValueError):
        return None


def gone(pid) -> bool:
    """True once the process has exited. A zombie (exited, not yet reaped by its parent) counts as gone: it has
    already let go of its files and its USB device."""
    if os.path.isdir("/proc/self"):
        st = _stat(pid)
        return st is None or st[0] in ("Z", "X", "x")
    try:
        os.kill(int(pid), 0)
        return False
    except ProcessLookupError:
        return True
    except (OSError, ValueError):
        return False


def group_members(pgid) -> list:
    """The processes of process group pgid that have not exited yet."""
    pgid = int(pgid)
    if not os.path.isdir("/proc/self"):
        try:
            os.killpg(pgid, 0)
            return [pgid]
        except OSError:
            return []
    out = []
    for d in os.listdir("/proc"):
        if d.isdigit():
            st = _stat(d)
            if st and st[1] == pgid and st[0] not in ("Z", "X", "x"):
                out.append(int(d))
    return out


def wait_gone(pids, timeout) -> list:
    """Wait up to timeout seconds for these processes to exit. Returns the ones still running."""
    end = time.time() + max(0.0, timeout)
    left = [p for p in pids if not gone(p)]
    while left and time.time() < end:
        time.sleep(0.05)
        left = [p for p in left if not gone(p)]
    return left


def end_group(proc, term_wait=5.0, grace=STOP_GRACE_S, kill_wait=KILL_WAIT_S) -> list:
    """End a program that leads its own process group (the chat console, started with setsid) and every program in
    that group (the radio programs it started): SIGTERM to all of them, up to term_wait seconds for the leader and
    `grace` for the rest, SIGKILL to whatever is still there, then wait until it has exited. Without the SIGKILL a
    radio program whose stop hangs outlives the console for ever and keeps its dongle. Also ends what a console
    that has already died left behind. Returns the pids that needed SIGKILL."""
    pgid = proc.pid                      # a group leader's group has its own number

    def ours():
        mem = group_members(pgid)
        if proc.poll() is not None and pgid in mem:
            return []                    # the number went to a new program: nothing of this group is left
        return mem

    t0 = time.time()
    if proc.poll() is None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:                  # not a group leader after all: the program alone
            try:
                proc.terminate()
            except OSError:
                pass
        try:
            proc.wait(timeout=max(0.0, term_wait))
        except subprocess.TimeoutExpired:
            pass
        left = ours()
    else:
        left = ours()
        for pid in left:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    left = wait_gone(left, max(0.0, t0 + grace - time.time()))
    for pid in left:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=max(0.0, kill_wait))
        except subprocess.TimeoutExpired:
            pass
    wait_gone(left, kill_wait)
    return left
