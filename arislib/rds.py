"""RDS (Radio Data System) decoder for FM broadcast stations: the station name (PS) and PI code.

Input: the FM-demodulated multiplex (MPX) signal, for example the instantaneous frequency of a wideband FM
capture. Public broadcast metadata only (station name and identifier, no programme content).

How it works (IEC 62106):
  1. RDS sits on a 57 kHz subcarrier as BPSK. A stereo station's 19 kHz pilot gives an exact copy of its clock,
     so the subcarrier is mixed down with 3 x the pilot phase. A mono station has no pilot: then the nominal
     57 kHz is used.
  2. Squaring the BPSK removes the data and leaves a tone at twice the subcarrier's remaining offset. That finds
     RDS encoders that are not locked to the pilot (or a mono station's own clock), and its phase is followed
     through the capture, so a slowly turning carrier does not wipe the data out.
  3. Bits run at subcarrier / 48 (1187.5 bit/s), biphase coded: a matched filter finds the bit timing, then
     differential decoding gives the data bits.
  4. Data comes in 26-bit blocks (16 data bits + a 10-bit checkword with an offset word A/B/C/C'/D); four blocks
     make a group. Two exactly right blocks in a row fix the block positions; from there a block with one wrong
     bit, or two wrong bits side by side, is corrected (the checkword allows it). Corrected blocks count for less
     than exact ones when a name is confirmed, so a noisy capture gives no name rather than a wrong one.
  5. Group 0A/0B carries the 8-character station name, 2 characters at a time. Many stations rotate the name
     ("NOVA" then "ECHO"): a name is only taken from its 4 parts received in order, so two names are never
     stitched together, and every name seen is reported, most frequent first.
"""
from __future__ import annotations

import math
from collections import Counter

import numpy as np

POLY = 0x5B9                    # x^10 + x^8 + x^7 + x^5 + x^4 + x^3 + 1
OFFSETS = {"A": 0x0FC, "B": 0x198, "C": 0x168, "Cp": 0x350, "D": 0x1B4}
PILOT_HZ = 19000.0
RDS_HZ = 57000.0                # 3 x pilot; bit rate = subcarrier / 48
MAX_OFFSET_HZ = 30.0            # how far an unlocked RDS subcarrier may sit from 57 kHz (or 3 x pilot)
MIN_SUBCARRIER_DB = 16.0        # squared-signal tone this far above its surroundings: an RDS subcarrier (noise
                                # alone gives 9-13 dB, a readable station 25 dB and more)


def crc10(info16: int) -> int:
    reg = int(info16) << 10
    for i in range(25, 9, -1):
        if reg & (1 << i):
            reg ^= POLY << (i - 10)
    return reg & 0x3FF


# The checkword is linear in the data bits: two 256-entry tables give it for any 16-bit word.
_T_HI = np.array([crc10(b << 8) for b in range(256)], dtype=np.int64)
_T_LO = np.array([crc10(b) for b in range(256)], dtype=np.int64)


def _syndrome(word26: int) -> int:
    return crc10((word26 >> 10) & 0xFFFF) ^ (word26 & 0x3FF)


def _error_table() -> dict:
    """Syndrome -> error pattern for one wrong bit or two wrong bits side by side (all distinct for this code).
    Patterns whose syndrome equals the difference of two offset words are left out: correcting them could turn a
    block of one kind into another kind."""
    offs = set(OFFSETS.values())
    cross = {a ^ b for a in offs for b in offs if a != b}
    table = {}
    for e in [1 << k for k in range(26)] + [3 << k for k in range(25)]:
        s = _syndrome(e)
        if s and s not in cross:
            table[s] = e
    return table


_ERRORS = _error_table()
_KIND_OFFSETS = ((OFFSETS["A"],), (OFFSETS["B"],), (OFFSETS["C"], OFFSETS["Cp"]), (OFFSETS["D"],))


def _band(X, freqs, lo, hi, taper, shift_bins=0, m=None):
    """Keep only lo..hi Hz of a spectrum (raised-cosine edges): the inverse FFT is the analytic signal of that band.
    With shift_bins and m (= len(X) / D), the band is moved down by shift_bins FFT bins and only m points are
    transformed back: the same signal at every D-th sample, at a fraction of the cost."""
    sel = np.flatnonzero((freqs >= lo - taper) & (freqs <= hi + taper))
    fb = freqs[sel]
    w = np.ones(len(sel))
    for edge, sign in ((lo, -1), (hi, 1)):
        d = (fb - edge) * sign
        ramp = d > 0
        w[ramp] = 0.5 * (1 + np.cos(np.pi * np.minimum(d[ramp], taper) / taper))
    m = len(X) if m is None else int(m)
    Y = np.zeros(m, dtype=np.complex128)
    Y[(sel - int(shift_bins)) % m] = X[sel] * w
    return np.fft.ifft(Y)


def _to_char(b: int) -> str:
    return chr(b) if 0x20 <= b <= 0x7E else "?"


def _pilot(X, f, fs_d, m, c19, edge):
    """The stereo pilot's phase against c19 FFT bins (unwrapped, at the reduced rate fs_d), or None for a mono
    station."""
    p = _band(X, f, PILOT_HZ - 120.0, PILOT_HZ + 120.0, 60.0, c19, m)
    ref = _band(X, f, PILOT_HZ + 300.0, PILOT_HZ + 700.0, 60.0, c19, m)
    if float(np.median(np.abs(p))) < 4.0 * float(np.median(np.abs(ref))) * np.sqrt(240.0 / 400.0) + 1e-12:
        return None
    ph = np.unwrap(np.angle(p))
    t = np.arange(m) / fs_d
    return ph, float(np.polyfit(t[edge:-edge:8], ph[edge:-edge:8], 1)[0] / (2 * np.pi))


def _carrier_offset(bb, fs, edge):
    """Squaring BPSK removes the data: a tone remains at twice the subcarrier's offset from the reference.
    Returns (offset Hz, how far the tone stands above the squared signal's noise, dB)."""
    q = bb[edge:len(bb) - edge] ** 2
    d = max(1, int(fs // 2000))                              # average down to about 2 kHz (the tone sits near 0 Hz)
    m = len(q) // d
    if m < 64:
        return 0.0, 0.0
    qd = q[: m * d].reshape(m, d).mean(axis=1)
    fsq = fs / d
    nfft = 1 << int(math.ceil(math.log2(m * 8)))
    Q = np.abs(np.fft.fft(qd, nfft))
    fq = np.fft.fftfreq(nfft, 1.0 / fsq)
    win = np.abs(fq) <= 2 * MAX_OFFSET_HZ
    k = int(np.argmax(np.where(win, Q, 0.0)))
    a, b, c = Q[k - 1], Q[k], Q[(k + 1) % nfft]
    den = a - 2 * b + c
    shift = 0.5 * (a - c) / den if den < 0 else 0.0           # parabola through the peak and its neighbours
    nu = (fq[k] + shift * fsq / nfft) / 2.0
    ref = (np.abs(fq - fq[k]) > 25.0) & (np.abs(fq) <= 400.0)
    prom = 20.0 * math.log10(float(b) / (float(np.median(Q[ref])) + 1e-30) + 1e-30)
    return float(nu), prom


def _follow_phase(bb, fs, edge, window_s=0.25):
    """The BPSK carrier phase through the capture (from the squared signal in short windows, unwrapped), so a
    subcarrier that turns slowly against the reference does not fade the data out. Up to a sign flip, which the
    differential decoding ignores."""
    w = int(window_s * fs)
    m = (len(bb) - 2 * edge) // w
    if m < 2:
        return np.full(len(bb), 0.5 * float(np.angle(np.sum(bb[edge:len(bb) - edge] ** 2))))
    c = np.sum(bb[edge:edge + m * w].reshape(m, w) ** 2, axis=1)
    centres = edge + (np.arange(m) + 0.5) * w
    return 0.5 * np.interp(np.arange(len(bb)), centres, np.unwrap(np.angle(c)))


def _biphase_shaped(X, f, centre):
    """RDS is biphase coded: its spectrum has a hole at the subcarrier itself and humps about 1 kHz either side.
    A plain 57 kHz tone (a stereo coder's leak) is the opposite."""
    p = np.abs(X) ** 2
    mid = p[np.abs(f - centre) <= 80.0]
    side = p[(np.abs(f - centre) >= 600.0) & (np.abs(f - centre) <= 1600.0)]
    return bool(len(mid) and len(side) and float(np.mean(side)) > 1.5 * float(np.mean(mid)))


def _block(word, syn, kind, exact_kind):
    """The data word of a block expected to be of this kind: exact, corrected (one bit or two adjacent bits), or
    None. A block that is exact for another kind is never 'corrected' into this one."""
    for o in _KIND_OFFSETS[kind]:
        if syn == o:
            return int(word) >> 10, True
    if exact_kind >= 0:
        return None
    for o in _KIND_OFFSETS[kind]:
        e = _ERRORS.get(syn ^ o)
        if e is not None:
            return (int(word) ^ e) >> 10, False
    return None


def _groups(bits):
    """Blocks -> groups. Two exact blocks in a row fix the block positions; from there each group's four blocks
    are read (exact or corrected) until two whole groups in a row are unreadable, then the next exact pair is
    looked for. Returns ([(group index, [A, B, C, D]), ...], exact blocks, corrected blocks), each block
    (data, exact) or None."""
    L = len(bits) - 25
    if L <= 104:
        return [], 0, 0
    w = np.zeros(L, dtype=np.int64)
    for j in range(26):
        w = (w << 1) | bits[j:j + L]
    syn = (_T_HI[w >> 18] ^ _T_LO[(w >> 10) & 0xFF]) ^ (w & 0x3FF)
    kind = np.full(L, -1, dtype=np.int64)
    for k, offs in enumerate(_KIND_OFFSETS):
        for o in offs:
            kind[syn == o] = k
    exact = int((kind >= 0).sum())
    pair = np.zeros(L, dtype=bool)
    pair[:L - 26] = (kind[:L - 26] >= 0) & (kind[26:] == (kind[:L - 26] + 1) % 4)
    anchors = np.flatnonzero(pair)
    groups, corrected, pos, ai = [], 0, 0, 0
    while ai < len(anchors):
        a = int(anchors[ai])
        if a < pos:
            ai += 1
            continue
        g = a - 26 * int(kind[a])
        while g < 0:
            g += 104
        miss, lost = 0, None
        while g + 78 < L:
            blk = [_block(w[g + 26 * b], int(syn[g + 26 * b]), b, int(kind[g + 26 * b])) for b in range(4)]
            if all(x is None for x in blk):
                miss += 1
                lost = g if lost is None else lost
                if miss >= 2:
                    break
            else:
                miss, lost = 0, None
                corrected += sum(1 for x in blk if x is not None and not x[1])
                groups.append((g // 104, blk))
            g += 104
        # always move past this anchor: a pair near the end of the capture whose group cannot be read whole would
        # otherwise send the search back to the same anchor for ever (v4.7.3 hung on real, noisy stations)
        pos = max((lost if lost is not None else g) + 1, a + 1)
        ai = int(np.searchsorted(anchors, pos))
    return groups, exact, corrected


def decode(mpx, fs: float, min_groups: int = 2) -> dict:
    """Decode RDS from an MPX signal sampled at fs (at least 128 kHz). Returns
    {"ok": bool, "ps": station name or None, "pi": "0x1234" or None, "groups": readable groups,
     "rds_present": an RDS subcarrier is there (even when no name could be read), ...}."""
    x = np.asarray(mpx, dtype=np.float64)
    if fs < 120e3 or len(x) < int(fs * 0.5):
        return {"ok": False, "rds_present": False, "reason": "capture too short or sample rate too low for RDS"}
    d = max(1, int(fs // 32000))                            # the work runs at about 32 kHz (RDS is +-2.4 kHz)
    m = len(x) // d
    x = x[: m * d] - x[: m * d].mean()
    n = len(x)
    X = np.fft.fft(x)
    f = np.fft.fftfreq(n, 1.0 / fs)
    fs_d = fs / d
    t = np.arange(m) / fs_d
    edge = int(0.02 * fs_d)                                 # ignore filter ringing at the ends
    out = {"ok": False, "groups": 0, "valid_blocks": 0, "corrected_blocks": 0, "ps": None, "pi": None,
           "rds_present": False, "stereo_pilot": False}
    # 1. reference: 3 x the pilot phase for a stereo station, the nominal 57 kHz for a mono one
    c19 = int(round(PILOT_HZ * n / fs))                     # both bands are shifted down by whole FFT bins
    f19 = c19 * fs / n
    pil = _pilot(X, f, fs_d, m, c19, edge)
    bb = _band(X, f, RDS_HZ - 2600.0, RDS_HZ + 2600.0, 400.0, 3 * c19, m)
    if pil is not None:
        ph, slope = pil
        out.update(stereo_pilot=True, pilot_hz=round(f19 + slope, 2))
        bb, f_ref = bb * np.exp(-3j * ph), 3.0 * (f19 + slope)
        if abs(slope) > 30.0:                               # not a broadcast pilot: treat the station as mono
            bb, f_ref = _band(X, f, RDS_HZ - 2600.0, RDS_HZ + 2600.0, 400.0, 3 * c19, m), 3.0 * f19
            out.update(stereo_pilot=False, pilot_hz=None)
    else:
        f_ref = 3.0 * f19
    # 2. the subcarrier: its offset from the reference, then its phase through the capture
    df, prom = _carrier_offset(bb, fs_d, edge)
    out["subcarrier_db"] = round(prom, 1)
    if prom < MIN_SUBCARRIER_DB:
        out["reason"] = "no RDS subcarrier" + ("" if out["stereo_pilot"] else " (mono station, no stereo pilot)")
        return out
    out["rds_carrier_hz"] = round(f_ref + df, 2)
    out["locked_to_pilot"] = bool(out["stereo_pilot"] and abs(df) < 0.05)
    bb = bb * np.exp(-2j * np.pi * df * t)
    s = np.real(bb * np.exp(-1j * _follow_phase(bb, fs_d, edge)))
    # 3. 16 samples per bit at the subcarrier's own bit rate, biphase matched filter, best timing
    rate = (f_ref + df) / 48.0
    ts = np.arange(t[edge], t[-edge], 1.0 / (16.0 * rate))
    s16 = np.interp(ts, t, s)
    tmpl = np.concatenate([np.ones(8), -np.ones(8)])
    c = np.correlate(s16, tmpl, mode="valid")
    best = max(range(16), key=lambda o: float(np.mean(np.abs(c[o::16]))))
    e = (c[best::16] > 0).astype(np.int64)
    bits = e[1:] ^ e[:-1]                                   # differential decoding (polarity does not matter)
    if len(bits) < 208:
        out["reason"] = "too few bits"
        return out
    # 4. blocks and groups
    groups, exact, corrected = _groups(bits)
    out.update(valid_blocks=exact, corrected_blocks=corrected)
    # random bits give an exact block by chance at 5 of 1024 positions: RDS shows clearly more (or whole groups,
    # which random bits never give); the spectrum's biphase shape alone can come from audio around a pilot harmonic
    chance = len(bits) * sum(len(o) for o in _KIND_OFFSETS) / 1024.0
    clearly = chance + max(0.8 * chance, 5.0 * math.sqrt(chance))     # 5 standard deviations above chance at least
    out["rds_present"] = bool(groups) or (exact >= clearly and _biphase_shaped(X, f, f_ref + df))
    full = [(i, b) for i, b in groups if b[0] is not None and b[1] is not None and b[3] is not None]
    out["groups"] = len(full)
    if len(full) < min_groups:
        out["reason"] = "no complete RDS groups (weak signal or no RDS)"
        return out
    pis = Counter(b[0][0] for _i, b in full if b[0][1]) or Counter(b[0][0] for _i, b in full)
    pi, cnt = pis.most_common(1)[0]
    if not any(b[0][1] for _i, b in full) and cnt < 2:
        out["reason"] = "PI code not confirmed"
        return out
    out["pi"] = f"0x{pi:04X}"
    # 5. station name: group 0A / 0B, segment address in block B, two characters in block D
    segs = {k: Counter() for k in range(4)}
    names, cur, last, last_i = Counter(), {}, -1, -99
    full, order = set(), []                     # names that filled all 8 characters (no padding); the order seen
    for i, (a, b, _c, d) in groups:
        if b is None or d is None or (a is not None and a[0] != pi) or (b[0] >> 12) != 0:
            continue
        k, chars = b[0] & 3, (d[0] >> 8, d[0] & 0xFF)
        sure = bool(a is not None and a[1] and b[1] and d[1])
        segs[k][chars] += 1.0 if sure else 0.5
        if k == 0:
            cur = {0: (chars, sure)}
        elif cur and k == last + 1 and i - last_i <= 16:
            cur[k] = (chars, sure)
        else:
            cur = {}                                        # a part was lost: this cycle is not used
        last, last_i = k, i
        if k == 3 and len(cur) == 4:
            raw = "".join(_to_char(ch[0]) + _to_char(ch[1]) for ch, _s in (cur[0], cur[1], cur[2], cur[3]))
            nm = raw.strip()
            if nm:
                names[nm] += 1.0 if all(s for _c2, s in cur.values()) else 0.5
                if len(raw.rstrip()) == 8:
                    full.add(nm)
                if not order or order[-1] != nm:
                    order.append(nm)
            cur = {}
    top = max(names.values()) if names else 0.0
    ranked = [nm for nm, wgt in names.most_common() if wgt >= 2.0 or (wgt >= 1.0 and wgt == top)]
    clear = all(segs[k] and segs[k].most_common(1)[0][1] >= max(1.0, 0.67 * sum(segs[k].values()))
                for k in range(4))
    came_back = len(order) > len(set(order))                # A B A: a name came back after another one
    pieces = len(names) >= 3 or (len(ranked) >= 2 and not came_back
                                 and not all(names[nm] >= 2.0 for nm in ranked[:2]))
    if pieces:
        # three or more different names in one look: the station scrolls text through its name (a slogan, the song,
        # a word at a time). One piece of it is not the station's name: keep the text it showed instead, and a fixed
        # name only when one clearly comes back between the pieces. Two names count as a station rotating its name
        # only when each comes back (v4.8.6 stored 'RUSH' / 'HOUR' and 'Grandes' / 'Yo Y La', each seen once, as
        # names).
        cnt = sorted(names.values(), reverse=True) + [0.0]
        fixed = names.most_common(1)[0][0] if cnt[0] >= 2.0 and cnt[0] >= 2.0 * cnt[1] else None
        out["ps_scrolling"] = scrolling_text([nm for nm in names if nm != fixed], full=full)
        ranked, clear = ([fixed] if fixed else []), False
    if ranked:
        out["ps"], out["ps_all"] = ranked[0], ranked[:4]
    elif clear:                                             # no complete cycle, but a steady name in every part
        name = "".join(_to_char(a) + _to_char(b) for k in range(4) for (a, b), _ in segs[k].most_common(1))
        out["ps"] = name.strip() or None
        out["ps_all"] = [out["ps"]] if out["ps"] else []
    if out["ps"]:
        out["ps_segments_seen"] = {k: round(sum(segs[k].values()), 1) for k in range(4)}
    else:
        out["reason"] = "station name not complete in this capture"
        out["ps_partial"] = "".join((_to_char(a) + _to_char(b)) if segs[k] else "__"
                                    for k in range(4) for (a, b), _ in (segs[k].most_common(1) or [((0, 0), 0)]))
    out["ok"] = True
    return out


def scrolling_text(parts, limit=40, full=()) -> str:
    """The pieces a scrolling name showed, in order, joined back into text: a piece that overlaps the end of the
    text so far (3 characters or more) continues it, otherwise it is the next word. After a piece that filled all
    8 characters (in `full`) the text goes straight on when the next piece starts with a small letter: coders that
    cut the text into 8-character blocks split words there ('Alejandr' + 'o Sanz', shown as 'Alejandr o Sanz'
    before). A capital starts a new word ('Iglesias' + 'Y Santos' + 'Bravos' is three words: the live station
    showed 'IglesiasY SantosBravos' and 'Nada conLuis Miguel' for a while)."""
    text, prev = "", None
    for p in (x.strip() for x in parts):
        if not p or p in text:
            continue
        k = max((n for n in range(min(len(text), len(p)), 2, -1) if text.endswith(p[:n])), default=0)
        if k:
            text = text + p[k:]
        elif prev in full and p[:1].isalpha() and p[:1].islower() and text:
            text = text + p
        else:
            text = (text + " " + p).strip()
        prev = p
    return text[:limit]


# ----------------------------------------------------------------------------------------------------------------
# Encoder: used by the decoder self-test and the simulated radios in test/ (never transmitted anywhere)
# ----------------------------------------------------------------------------------------------------------------
def group_0a(pi: int, ps: str, seg: int, pty: int = 0, tp: int = 0):
    ps = (str(ps) + " " * 8)[:8]
    b2 = (0 << 12) | (0 << 11) | (tp << 10) | ((pty & 31) << 5) | (1 << 3) | (seg & 3)
    return [pi & 0xFFFF, b2, 0xE0CD, (ord(ps[2 * seg]) << 8) | ord(ps[2 * seg + 1])]


def encode_bits(pi: int, ps, n_groups: int, switch_every=0, ps2=""):
    """Group 0A after group 0A. With ps2, the name changes to ps2 (and back) every `switch_every` full names. ps
    as a list: scrolling text, the next piece every full name."""
    bits = []
    for g in range(n_groups):
        if isinstance(ps, (list, tuple)):
            name = ps[(g // 4) % len(ps)]
        else:
            name = ps2 if ps2 and switch_every and (g // 4 // switch_every) % 2 else ps
        for info, off in zip(group_0a(pi, name, g % 4), ("A", "B", "C", "D")):
            word = (info << 10) | (crc10(info) ^ OFFSETS[off])
            bits += [(word >> (25 - k)) & 1 for k in range(26)]
    return bits


def rds_waveform(t, pi: int, ps: str, pilot_phase=0.0, rds_phase=0.0, ps2="", switch_every=0, carrier_hz=None):
    """Baseband RDS (biphase BPSK on 57 kHz) at the sample times t, amplitude 1. Locked to the pilot (3 x 19 kHz)
    unless carrier_hz is given: an RDS encoder running on its own clock, or a mono station's."""
    fc = 3.0 * PILOT_HZ if carrier_hz is None else float(carrier_hz)
    rate = fc / 48.0
    n_bits = int((t[-1] - t[0]) * rate) + 3
    bits = encode_bits(pi, ps, n_bits // 104 + 2, switch_every, ps2)[:n_bits]
    e, prev = [], 0
    for d in bits:
        prev ^= d
        e.append(prev)
    e = np.array(e) * 2 - 1
    k = ((t - t[0]) * rate).astype(int)
    half = (((t - t[0]) * rate) % 1.0) < 0.5
    sym = np.where(half, e[k], -e[k])
    if carrier_hz is None:
        return sym * np.cos(3 * (2 * np.pi * PILOT_HZ * t + pilot_phase) + rds_phase)
    return sym * np.cos(2 * np.pi * fc * t + rds_phase)


def selftest() -> dict:
    """Decode a synthetic stereo station and a mono one (a check of the decoder itself, not of the radio)."""
    fs = 256000.0
    t = np.arange(int(fs * 2.0)) / fs
    rng = np.random.default_rng(5)
    music = 0.4 * np.sin(2 * np.pi * 1000 * t) + 0.02 * rng.standard_normal(len(t))
    stereo = decode(music + 0.1 * np.sin(2 * np.pi * PILOT_HZ * t)
                    + 0.04 * rds_waveform(t, 0x1A2B, "ARIS RDS", -np.pi / 2, 0.7), fs)
    mono = decode(music + 0.04 * rds_waveform(t, 0x2B3C, "ARIS MON", rds_phase=1.9, carrier_hz=RDS_HZ + 2.5), fs)
    ok = (stereo.get("ps"), stereo.get("pi"), mono.get("ps")) == ("ARIS RDS", "0x1A2B", "ARIS MON")
    return {"ok": ok, "detail": "decoded the test station names (stereo and mono)" if ok else
            f"did not decode the test names (stereo: {stereo.get('ps') or stereo.get('reason')}, "
            f"mono: {mono.get('ps') or mono.get('reason')})"}
