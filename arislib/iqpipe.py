#!/usr/bin/env python3
"""Run rtl_433 or narrow-band audio on a receiver that only gives IQ (arislib.radios.HelperBackend).

    iqpipe.py rtl433 -- <source argv: CU8 IQ on stdout> --- <rtl_433 argv reading "-r cu8:-">
        The source's IQ goes straight into rtl_433's stdin; rtl_433's output is this program's output (so the
        engine reads it exactly as from a live rtl_433). It ends when rtl_433 ends (its -T time) or on a signal.

    iqpipe.py fm --rate FS --mode fm|am --sr SR --out PATH|- -- <source argv: CU8 IQ at FS on stdout>
        Demodulates like rtl_fm (narrow-band FM, or AM) into 16-bit little-endian mono audio at SR samples a
        second, written to PATH (or stdout), until the source ends or a signal arrives.

Receive-only, and the audio only ever goes where the engine asked (its decoders read it and delete it): nothing is
recorded by this program. numpy for the demodulator only.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading


def _name(n: str) -> None:
    try:
        import ctypes
        ctypes.CDLL(None).prctl(15, n.encode()[:15], 0, 0, 0)          # PR_SET_NAME: Reset Scanner finds it
    except Exception:
        pass


def _split(argv, mark):
    if mark not in argv:
        return argv, []
    i = argv.index(mark)
    return argv[:i], argv[i + 1:]


def _stop(*procs):
    for p in procs:
        if p and p.poll() is None:
            try:
                p.send_signal(signal.SIGINT)
            except OSError:
                pass
    for p in procs:
        if not p:
            continue
        try:
            p.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            try:
                p.kill()
                p.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass


def run_rtl433(argv) -> int:
    src, sink = _split(argv, "---")
    if not src or not sink:
        print("iqpipe: usage: rtl433 -- <source> --- <rtl_433 ...>", file=sys.stderr)
        return 2
    s = subprocess.Popen(src, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    k = subprocess.Popen(sink, stdin=s.stdout)
    s.stdout.close()                                # rtl_433 owns the read end now
    done = threading.Event()

    def bye(*_):
        done.set()
        _stop(k, s)
        sys.exit(0)

    signal.signal(signal.SIGINT, bye)
    signal.signal(signal.SIGTERM, bye)
    rc = k.wait()
    _stop(s)
    return rc


def _demod(chunk, mode, state, np):
    """CU8 bytes -> complex baseband -> audio samples (float, before decimation to SR)."""
    a = np.frombuffer(chunk, dtype=np.uint8).astype(np.float32) - 127.5
    z = (a[0::2] + 1j * a[1::2]) / 127.5
    if mode == "am":
        m = np.abs(z)
        mu = float(m.mean()) if len(m) else 0.0
        state["dc"] = mu if "dc" not in state else 0.9 * state["dc"] + 0.1 * mu    # carrier level, followed slowly
        return (m - state["dc"]) * 4.0
    prev = state.get("prev")
    zz = np.concatenate([[prev], z]) if prev is not None else z
    state["prev"] = z[-1] if len(z) else prev
    if len(zz) < 2:
        return np.zeros(0, dtype=np.float32)
    return np.angle(zz[1:] * np.conj(zz[:-1])) / np.pi


def run_fm(argv) -> int:
    import numpy as np
    opts, src = _split(argv, "--")
    o = {opts[i]: opts[i + 1] for i in range(0, len(opts) - 1, 2)}
    fs = int(o.get("--rate", "240000"))
    sr = int(o.get("--sr", "24000"))
    mode = o.get("--mode", "fm").lower()
    out_path = o.get("--out", "-")
    dec = max(1, int(round(fs / sr)))
    s = subprocess.Popen(src, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    out = sys.stdout.buffer if out_path == "-" else open(out_path, "wb")
    state, carry = {}, np.zeros(0, dtype=np.float32)

    def bye(*_):
        _stop(s)
        try:
            out.flush()
        finally:
            if out is not sys.stdout.buffer:
                out.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, bye)
    signal.signal(signal.SIGTERM, bye)
    block = 2 * dec * 4096
    while True:
        chunk = s.stdout.read(block)
        if not chunk:
            break
        chunk = chunk[: len(chunk) - (len(chunk) % 2)]
        x = np.concatenate([carry, _demod(chunk, mode, state, np)])
        n = (len(x) // dec) * dec
        carry = x[n:]
        if n:
            y = x[:n].reshape(-1, dec).mean(axis=1)                  # boxcar filter + decimation, as rtl_fm's
            out.write((np.clip(y, -1.0, 1.0) * 32000).astype("<i2").tobytes())
    bye()
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    _name("aris-iqpipe")
    if cmd == "rtl433":
        return run_rtl433(rest[1:] if rest[:1] == ["--"] else rest)
    if cmd == "fm":
        return run_fm(rest)
    print(f"iqpipe: unknown mode {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
