import json, os, sys
REAL = os.environ.get("REAL_BIN", "/usr/local/bin")


def _own_name():
    """Show up under the tool's own name, as the real program does: "#!/usr/bin/env python3" makes every fake a
    "python3" in /proc, where ARIS finds the radio programs it has to end (_radio_children: Reset Scanner)."""
    try:
        import ctypes
        ctypes.CDLL(None).prctl(15, os.path.basename(sys.argv[0]).encode()[:15], 0, 0, 0)     # PR_SET_NAME
    except Exception:
        pass


_own_name()
def logargs(name):
    p = os.environ.get("FAKE_ARGLOG")
    if p:
        with open(p, "a") as f:
            f.write(json.dumps([name] + sys.argv[1:]) + "\n")
def opt(args, flag, default=None):
    return args[args.index(flag) + 1] if flag in args else default
def listing():
    """The dongles rtl_test lists: the simulated station's three, plus FAKE_EXTRA, minus FAKE_MISSING, plus
    FAKE_DUP (a second dongle with a serial already listed)."""
    devs = ["00001001", "00003003", "00004004"] + [x for x in os.environ.get("FAKE_EXTRA", "").split(",") if x]
    miss = [x for x in os.environ.get("FAKE_MISSING", "").split(",") if x]
    devs = [d for d in devs if d not in miss]
    return devs + [x for x in os.environ.get("FAKE_DUP", "").split(",") if x]
def resolve_dev(dev, devs=None):
    """librtlsdr's verbose_device_search (convenience.c), which every rtl_* tool but rtl_433 uses for -d: the
    string is first read with strtol(s, 0) (a leading 0 means octal: '00001001' is 513, '00000001' is 1) and, when
    all of it parsed and the number is below the device count, it is a device INDEX; else the exact serial, then
    a serial starting with it, then one ending with it. Returns (index, serial) or (None, None)."""
    devs = listing() if devs is None else devs
    s = str(dev or "")
    import re
    m = re.fullmatch(r"\s*[+-]?(0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)", s)
    if m:
        body = m.group(1)
        n = int(body, 16) if body[:2].lower() == "0x" else int(body, 8) if body.startswith("0") else int(body)
        n = -n if s.strip().startswith("-") else n
        if 0 <= n < len(devs):
            return n, devs[n]
    for test in (lambda d: d == s, lambda d: d.startswith(s), lambda d: d.endswith(s)):
        for i, d in enumerate(devs):
            if test(d):
                return i, d
    return None, None
def soapy_as_rtl(args):
    """rx_tools arguments -> the simulated rtl_* tool's: "-d driver=X,serial=Y" names the receiver FAKE_SOAPY lists
    as X:Y, which hears like the dongle FAKE_SOAPY_AS (default 00004004); rx_sdr's "-F CU8" is dropped (CU8 is what
    rtl_sdr writes). An unknown receiver exits like rx_sdr does."""
    out, i = [], 0
    listed = [x.split(":", 1) for x in os.environ.get("FAKE_SOAPY", "").split(",") if ":" in x]
    while i < len(args):
        a = args[i]
        if a == "-d" and i + 1 < len(args):
            kv = dict(p.split("=", 1) for p in args[i + 1].split(",") if "=" in p)
            if not any(kv.get("driver") == d and kv.get("serial", s) == s for d, s in listed):
                print(f"SoapySDR::Device::make() no match for {args[i + 1]}", file=sys.stderr)
                sys.exit(1)
            out += ["-d", os.environ.get("FAKE_SOAPY_AS", "00004004")]
            i += 2
            continue
        if a == "-F" and i + 1 < len(args) and args[i + 1].upper() == "CU8":
            i += 2
            continue
        out.append(a)
        i += 1
    return out
def specs(var):
    return [x.strip().split(":") for x in os.environ.get(var, "").split(",") if x.strip()]
def band_of(f_hz):
    return "fm" if f_hz < 300e6 else ("433" if f_hz < 700e6 else ("915" if f_hz < 1000e6 else "1090"))
def _distance_db(dev, f_hz):
    """Room simulation for locate tests: FAKE_SRC_POS='433.92:7:2' (transmitter at x=7 m, y=2 m) and
    FAKE_NODE_POS='00001001:1:1;00003003:9:1' (antenna positions). Path loss exponent 2.7, 0 dB at 1 m."""
    import math
    src = [s.split(":") for s in os.environ.get("FAKE_SRC_POS", "").split(",") if s.strip()]
    pos = dict((p.split(":")[0], p.split(":")[1:]) for p in os.environ.get("FAKE_NODE_POS", "").split(";")
               if p.strip())
    for f, x, y in src:
        if abs(float(f) * 1e6 - f_hz) <= 0.5e6 and dev in pos:
            nx, ny = (float(v) for v in pos[dev])
            d = max(0.5, math.hypot(nx - float(x), ny - float(y)))
            return -27.0 * math.log10(d)
    return 0.0


def atten(dev, f_hz):
    """dB added to a signal's strength on this dongle (its antenna), or None if it cannot hear it at all.
    FAKE_ANT='00001001:fm=0,433=-10;00003003:433=0,915=-5'. Legacy FAKE_ANTENNA='00001001,00003003'."""
    a = _atten_ant(dev, f_hz)
    return None if a is None else a + _distance_db(dev, f_hz)


def _atten_ant(dev, f_hz):
    spec = os.environ.get("FAKE_ANT", "")
    if spec:
        for part in spec.split(";"):
            if not part.strip():
                continue
            d, _, rest = part.partition(":")
            if d.strip() == dev:
                vals = dict(kv.split("=") for kv in rest.split(",") if "=" in kv)
                v = vals.get(band_of(f_hz), "0")
                return None if v == "x" else float(v)
        return None
    legacy = [x for x in os.environ.get("FAKE_ANTENNA", "").split(",") if x]
    if legacy and dev not in legacy:
        return None
    return 0.0


def sigint_hang(before=None, sigs=("SIGINT", "SIGTERM")):
    """FAKE_SIGINT_HANG=<seconds>: a stop that hangs, like librtlsdr's async cancel on the real PC (2026-11-02:
    `timeout -s INT -k 3 5 rtl_sdr ...` stopped writing at Ctrl+C but was still running 3 s later; SIGKILL, exit
    137). On SIGINT or SIGTERM (the rtl-sdr tools handle both the same way) the tool stops sending anything, then
    exits only after that many seconds; 'never' (or 'inf'): only SIGKILL ends it. before() runs first (what the
    tool has printed by then). Unset: nothing changes. Returns True when the hang is armed."""
    v = os.environ.get("FAKE_SIGINT_HANG", "").strip().lower()
    if not v:
        return False
    import signal, time
    secs = float("inf") if v in ("never", "inf", "forever") else float(v)

    def hang(*_a):
        for s in sigs:
            signal.signal(getattr(signal, s), signal.SIG_IGN)     # a second Ctrl+C changes nothing
        try:
            sys.stderr.write("Signal caught, exiting!\n"); sys.stderr.flush()
            if before:
                before()
        except Exception:
            pass
        end = time.time() + secs
        while time.time() < end:
            time.sleep(min(0.5, max(0.01, end - time.time())))
        os._exit(0)
    for s in sigs:
        signal.signal(getattr(signal, s), hang)
    return True
