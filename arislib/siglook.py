"""What one narrowband look shows about a signal, as measured and no further: whether it stayed on, its strongest
spectral line and how that line moved, how wide the signal is, how steady its level is, and a modulation look.

Input: the channel samples of one look (complex, centred on the signal's energy), their sample rate, the noise power
per sample in that channel and the channel filter's half width. Output: plain Python numbers and words.

Every block carries a "why" when it could not be measured, and a kind of modulation is named only when its test
passed with margin: "unknown" (modulated, kind not determined) and "untested" are answers too, and a wrong kind is
worse than either. The thresholds come from simulated signals with white noise and 8-bit samples; they are upper
bounds on what a real look can tell, not proof on real signals.

Nothing here keeps or returns symbols, bits or audio: the frequency track and the symbol histogram exist only inside
the call. numpy only. Receive-only: timing, frequency, width and level, never content."""
from __future__ import annotations

import math

import numpy as np

BLOCK_S = 0.01                  # on / off is judged in 10 ms blocks
ON_X, OFF_X = 2.0, 1.4          # on above 2.0 x noise, off again below 1.4 x
TRUST_DB = 8.0                  # level while on, over the noise: under this nothing is counted or classified
GOOD_DB = 12.0                  # from here: the 99 % width, AM, quality A
LEVELS_DB = 15.0                # a count of FSK levels needs this (at 12 to 14 dB four levels often counted as two) ...
LEVELS_MAX_RATE = 4800          # ... and a rate the look's samples resolve (9600 has 2.6 samples a symbol)
FOLD_HW = 0.4                   # a filter wider than this share of the sample rate lets through what the samples
                                # cannot hold: it folds back in at a frequency that is not its own
OFF_NEAR_DB = 1.5               # off blocks must sit this close to the noise (else something else is in the channel)
EDGE_BLOCKS = 2                 # a keyed edge is complete within two blocks; a fade is not
SHARP_EDGES = 0.8               # share of the edges that must be that sharp before on / off periods are counted
MIN_OFF_BLOCKS = 3              # "on the whole time" allows no gap this long (30 ms)
MAX_HOLES = 0.05                # more of the on time than this in single-block holes: keyed faster than blocks show
SOLID_DB = 5.0                  # line, width and modulation use only blocks this far over the noise ...
SOLID_X = 0.25                  # ... and within 6 dB of the usual level (a fade's weak part reads as modulation)
BURST_S = 0.1                   # every on period shorter than this: bursts
PIECE_S = 0.25                  # the line is followed in pieces this long (4 Hz apart before interpolation)
MAX_PIECES = 240
LINE_SHARE = 0.5                # a single clean line holds at least this share of the signal's power in each piece
LINE_HZ = 25.0                  # ... within this distance of its peak
OTHER_LINE_HZ = 500.0           # a second line counts from this far away
SLOT_S = 0.5                    # the on / off strip is never finer than this (finer would show the keying itself)
MAX_SLOTS = 60
CLIP_MAX = 0.001                # more than 0.1 % of the raw samples at full scale: overloaded
NARROWEST_HZ = 100.0            # a narrower reading is the resolution, not the source: "under 0.1 kHz"
RATES = (512, 1200, 1600, 2400, 3200, 3600, 4800, 9600)
CTCSS = (67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5, 94.8, 97.4, 100.0, 103.5, 107.2, 110.9, 114.8,
         118.8, 123.0, 127.3, 131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 159.8, 162.2, 165.5, 167.9, 171.3, 173.8,
         177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5, 203.5, 206.5, 210.7, 218.1, 225.7, 229.1, 233.6,
         241.8, 250.3, 254.1)
TONE_DEV_HZ = 150.0             # rms deviation a sub-audible tone needs (hum on a carrier read 40 Hz in simulation)
MAINS_HZ = (100.0, 120.0, 180.0, 200.0, 240.0, 250.0)     # mains hum lines of 50 and 60 Hz supplies
HUM_SHARE = 0.6                 # this much of the audio-band frequency power on multiples of 50 or 60 Hz: a hum
CLICK_KURT = 15.0               # a frequency track spikier than this is clicks (speech reads 7 to 8, clicks 25 and up)
WIDE_DB = 16.0                  # from here the noise's own share of the frequency track is known well enough to
                                # tell audio (nothing above 3.6 kHz) from clicks and wide data (as much above as below)
MAINS_DEV_HZ = 250.0            # ... and what a tone within 2 Hz of one of them needs
TONE_PROM = 100.0               # the tone's line over its neighbourhood (20 dB; noise alone reached 12 dB)
RATE_PROM = 10 ** 1.5           # the symbol-rate line over its neighbourhood (15 dB)
AM_INDEX = 0.12                 # rms amplitude modulation, noise removed, from which a signal is called AM
QUIET_AM = 0.04                 # ... and under which a carrier is called unmodulated (or 0.55 x the noise's own)
QUIET_FM, VOICE_FM = 1.5, 4.0   # frequency power over the noise's own: under / over these
FLAT_ENV = 0.25                 # FM and FSK keep their envelope flatter than this (rms, all rates, noise removed)


def clip_fraction(raw) -> float:
    """Share of 8-bit samples at full scale (0 or 255) in a raw capture (bytes or a uint8 array)."""
    try:
        a = np.frombuffer(raw, dtype=np.uint8) if isinstance(raw, (bytes, bytearray, memoryview)) \
            else np.asarray(raw, dtype=np.uint8).ravel()
        return float(np.count_nonzero((a == 0) | (a == 255))) / len(a) if len(a) else 0.0
    except Exception:
        return 0.0


def profile(y, fs, noise, hw_hz, mode="full", clip_frac=0.0) -> dict:
    """The look's profile (the keys are described at each block below). mode "light" leaves out width, level and
    modulation. line.hz and line.others_hz are offsets from the centre of y. Never raises: input that cannot be
    measured gives key.state "off" or "unclear" with a why."""
    full = mode != "light"
    try:
        with np.errstate(all="ignore"):
            out = _finite(_profile(y, float(fs), float(noise), float(hw_hz), full, float(clip_frac or 0.0)))
        if out["line"]["hz"] is None:
            out["line"]["ok"] = False
        return out
    except Exception:
        return _blank(0.0, "unclear", "too_short", full)


def _finite(v):
    """No NaN or infinity leaves this module (the result goes out as JSON): such a number reads None, not measured."""
    if isinstance(v, dict):
        return {k: _finite(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_finite(x) for x in v]
    return None if isinstance(v, float) and not math.isfinite(v) else v


def _blank(seconds, state, why, full):
    out = {"seconds": seconds, "snr_on_db": None, "quality": "C", "overloaded": False, "folded": False,
           "key": {"state": state, "on_pct": 0.0, "runs": 0, "longest_on_s": 0.0, "longest_off_s": seconds,
                   "shortest_on_ms": None, "period_s": None, "open_start": False, "open_end": False, "why": why},
           "line": {"ok": False, "hz": None, "share": None, "slope_hz_s": None, "span_s": None, "scatter_hz": None,
                    "pieces": 0, "others_hz": [], "why": why},
           "strip": [], "strip_slot_s": SLOT_S}
    if full:
        out["width"] = {"hz": None, "share": None, "limit": None, "why": why}
        out["level"] = {"range_db": None, "why": why}
        out["mod"] = {"kind": "untested", "tone_hz": None, "rate_baud": None, "levels": None, "am_index": None,
                      "dev_rms_hz": None, "hum_hz": None, "stretch_s": 0.0, "why": why}
    return out


def _profile(y, fs, noise, hw, full, clip):
    y = np.asarray(y).ravel()
    if not np.iscomplexobj(y):
        y = y.astype(np.complex64)
    blk = int(fs * BLOCK_S) if math.isfinite(fs) else 0
    nb = len(y) // blk if blk >= 8 else 0
    seconds = round(len(y) / fs, 2) if blk >= 8 else 0.0
    if nb < 2:
        return _blank(seconds, "unclear", "too_short", full)
    if not (math.isfinite(noise) and noise > 0 and math.isfinite(hw) and hw > 0):
        return _blank(seconds, "unclear", "too_weak", full)     # no noise reference: nothing can be told from noise
    pch = y.real.astype(np.float64) ** 2 + y.imag.astype(np.float64) ** 2
    if not math.isfinite(float(pch.sum())):
        y = np.where(np.isfinite(pch), y, 0).astype(y.dtype)
        pch = y.real.astype(np.float64) ** 2 + y.imag.astype(np.float64) ** 2
    bm = pch[: nb * blk].reshape(nb, blk).mean(axis=1)
    if float(bm.max()) > 1e12 * noise:          # 120 dB over the noise: not a receiver's samples
        return _blank(seconds, "unclear", "overloaded", full)
    on, holes = _on_blocks(bm, noise)
    runs = _runs(on)
    overloaded = clip > CLIP_MAX
    if not runs:
        out = _blank(seconds, "off", "not_on", full)
        out["overloaded"] = overloaded
        out["strip"], out["strip_slot_s"] = _strip(on)
        return out
    # the level while on: the upper quartile of the on blocks (blocks at a key edge are only partly on)
    usual = float(np.percentile(bm[on], 75))
    snr_on = 10.0 * math.log10(max(usual / noise - 1.0, 1e-3))
    on_s = float(on.sum()) * BLOCK_S
    key = _keying(bm, on, holes, runs, noise, snr_on, seconds)
    solid = _runs(on & (bm >= max((1.0 + 10 ** (SOLID_DB / 10.0)) * noise, SOLID_X * usual)))
    line, aux = _line(y, fs, solid, blk, noise, hw, snr_on)
    folded = hw > FOLD_HW * fs
    if folded:
        # one wide signal or several side by side: a line read here may be a folded copy of one further out, so no
        # line, no other lines and no "separate lines" are reported (the share stays: it is a share of what is here)
        line.update({"ok": False, "hz": None, "slope_hz_s": None, "span_s": None, "scatter_hz": None,
                     "others_hz": [], "why": "no_single_line"})
        aux = {"lines": 0, "lines_share": 0.0, "at_start": None}
    out = {"seconds": seconds, "snr_on_db": round(snr_on, 1), "folded": folded,
           "quality": ("A" if snr_on >= GOOD_DB and on_s >= 2.0 and not overloaded else
                       "B" if snr_on >= TRUST_DB and on_s >= 1.0 and not overloaded else "C"),
           "overloaded": overloaded, "key": key, "line": line}
    if full:
        yd, centre = y, line["hz"]
        if line["ok"] and line["slope_hz_s"] is not None and abs(line["slope_hz_s"]) * seconds >= 12.0:
            # follow the drifting line: width and modulation are the signal's own, not the distance it travelled
            t = np.arange(len(y), dtype=np.float64) / fs
            yd = y * np.exp(-1j * np.pi * line["slope_hz_s"] * t * t).astype(np.complex64)
            centre = aux["at_start"]
        width, spec = _width(yd, fs, solid, nb, blk, noise, hw, snr_on, overloaded)
        out["width"] = width
        out["level"] = _level(bm, on, runs, noise, snr_on, overloaded)
        out["mod"] = _modulation(yd, fs, solid, nb, blk, hw, snr_on, overloaded, line, aux, spec, centre)
    out["strip"], out["strip_slot_s"] = _strip(on)
    return out


# ---------------------------------------------------------------------------------------------------- on and off
def _runs(mask):
    d = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(zip(np.flatnonzero(d == 1).tolist(), np.flatnonzero(d == -1).tolist()))     # (start, end) blocks


def _on_blocks(bm, noise):
    """Which 10 ms blocks the signal is on in, and how many of them are one-block holes that were filled. A lone on
    block counts only when it is strong by itself (a short burst): a weak carrier at the threshold does not
    flicker. A one-block hole does not split a transmission (it may be the block grid, not the key); many of them
    mean keying faster than the blocks can show."""
    nb = len(bm)
    code = np.where(bm > ON_X * noise, 1, np.where(bm < OFF_X * noise, -1, 0))
    last = np.maximum.accumulate(np.where(code != 0, np.arange(nb), -1))
    on = np.where(last >= 0, code[np.maximum(last, 0)], -1) > 0
    strong = (1.0 + 10 ** (TRUST_DB / 10.0)) * noise
    for a, b in _runs(on):
        if b - a == 1 and bm[a] < strong:
            on[a] = False
    holes = 0
    for a, b in _runs(~on):
        if a > 0 and b < nb and b - a == 1:
            on[a] = True
            holes += 1
    return on, holes


def _period(marks):
    """The spacing of like edges, when three or more are evenly spaced (within 20 %)."""
    if len(marks) < 3:
        return None
    d = np.diff(np.asarray(marks, dtype=np.float64))
    return round(float(d.mean()) * BLOCK_S, 2) if d.max() - d.min() < 0.2 * d.mean() else None


def _keying(bm, on, holes, runs, noise, snr_on, seconds):
    """key: state on_whole (on in 99 % of the blocks, no gap of 30 ms or more) / keyed / bursts (every on period
    under 100 ms) / unclear / off; on_pct, runs, longest_on_s, longest_off_s, shortest_on_ms (complete on periods
    only), period_s, open_start / open_end (on at the first / last block: that period may be longer), why. With
    "unclear" the numbers are what the blocks gave and must not be worded as counts."""
    nb = len(bm)
    gaps = _runs(~on)
    edges = sharp = 0
    for a, b in runs:
        ref = float(np.median(bm[a:b]) if b - a >= 5 else bm[a:b].max()) - noise
        if a > 0:
            edges += 1
            sharp += float(bm[a:a + EDGE_BLOCKS].max()) - noise >= 0.5 * ref
        if b < nb:
            edges += 1
            sharp += float(bm[max(a, b - EDGE_BLOCKS):b].max()) - noise >= 0.5 * ref
    off_near = (not gaps) or float(np.median(bm[~on])) <= noise * 10 ** (OFF_NEAR_DB / 10.0)
    whole = float(on.sum() - holes) >= 0.99 * nb and all(b - a < MIN_OFF_BLOCKS for a, b in gaps)
    done = [b - a for a, b in runs if a > 0 and b < nb]
    why = None
    if snr_on < TRUST_DB:
        state, why = "unclear", "too_weak"
    elif whole:
        state = "on_whole"
    elif not off_near or sharp < SHARP_EDGES * edges or holes > MAX_HOLES * float(on.sum()):
        state, why = "unclear", "unsteady"
    elif done and all((b - a) * BLOCK_S < BURST_S for a, b in runs):
        state = "bursts"
    else:
        state = "keyed"
    starts, ends = [a for a, _b in runs if a > 0], [b for _a, b in runs if b < nb]
    return {"state": state, "on_pct": round(100.0 * float(on.mean()), 1), "runs": len(runs),
            "longest_on_s": seconds if runs == [(0, nb)] else round(max(b - a for a, b in runs) * BLOCK_S, 2),
            "longest_off_s": round(max([b - a for a, b in gaps] or [0]) * BLOCK_S, 2),
            "shortest_on_ms": int(round(min(done) * BLOCK_S * 1000)) if done and not whole else None,
            "period_s": None if whole else _period(starts if len(starts) >= len(ends) else ends),
            "open_start": bool(on[0]), "open_end": bool(on[-1]), "why": why}


def _strip(on):
    """One value per half second (longer slots for a long look): 1 on (nine tenths of it or more), 0 off (all of
    it), 0.5 partly on: a slot with one short burst in it is not "off"."""
    per = int(round(SLOT_S / BLOCK_S)) * max(1, int(math.ceil(len(on) / (SLOT_S / BLOCK_S) / MAX_SLOTS)))
    slots = max(1, int(round(len(on) / float(per))))
    out = []
    for k in range(slots):
        part = on[k * per:(k + 1) * per if k < slots - 1 else len(on)]
        share = float(part.mean()) if len(part) else 0.0
        out.append(1 if share >= 0.9 else 0.5 if share > 0 else 0)
    return out[:MAX_SLOTS], round(per * BLOCK_S, 2)


# ------------------------------------------------------------------------------------------- the line and its drift
def _theil_sen(t, f):
    i, j = np.triu_indices(len(t), 1)
    return float(np.median((f[j] - f[i]) / (t[j] - t[i])))


def _quantile_of_mean(k, z):
    """Where the z-sigma point of an average of k periodograms lies, as a share of its mean (Wilson-Hilferty)."""
    k = max(float(k), 1.0)
    return (1.0 - 1.0 / (9.0 * k) + z / (3.0 * math.sqrt(k))) ** 3


def _parabola(a, b, c):
    """Offset of the peak from the middle of three log-spaced points, in steps."""
    a, b, c = (np.log(np.maximum(v, 1e-30)) for v in (a, b, c))
    den = a - 2.0 * b + c
    return np.where(den < 0, 0.5 * (a - c) / np.where(den < 0, den, 1.0), 0.0)


def _line(y, fs, runs, blk, noise, hw, snr_on):
    """line: ok (one clean line: it holds half or more of the signal's power in the pieces, at 8 dB or more), hz
    (its offset, the median over the pieces: of a drifting line, where it was at mid-look), share, slope_hz_s
    (Theil-Sen over four or more pieces), span_s (first to last piece centre), scatter_hz (5 to 95 % of the pieces
    around the slope), pieces, others_hz (up to two other carriers 0.5 kHz or more away, to the nearest 4 Hz; listed
    only beside a clean line or when the look is made of such lines), why. With ok false hz is None.
    The pieces lie inside the on periods (a piece fades in and out, so a key edge at its end does not count). A
    drifting line is followed: its share is measured with the drift taken out, so a moving carrier is still one
    line."""
    out = {"ok": False, "hz": None, "share": None, "slope_hz_s": None, "span_s": None, "scatter_hz": None,
           "pieces": 0, "others_hz": [], "why": None}
    aux = {"lines": 0, "lines_share": 0.0, "at_start": None}
    per = max(1, int(round(PIECE_S / BLOCK_S)))
    dec = max(1, per * blk // 256)
    n = (per * blk // dec) * dec
    starts = []
    for a, b in runs:
        starts += [(a + k * per) * blk for k in range((b - a) // per)]
    if snr_on < TRUST_DB or not starts:
        out["why"] = "too_weak" if snr_on < TRUST_DB else "too_short"
        return out, aux
    if len(starts) > MAX_PIECES:
        starts = [starts[i] for i in np.unique(np.linspace(0, len(starts) - 1, MAX_PIECES).round().astype(int))]
    st = np.asarray(starts)
    k = len(st)
    tc = (st + n / 2.0) / fs
    w = np.hanning(n).astype(np.float32)
    w2 = float(np.sum(w.astype(np.float64) ** 2))
    y0 = y[st[:, None] + np.arange(n)[None, :]] * w
    fq = np.fft.fftfreq(n, 1.0 / fs)
    band = np.abs(fq) <= min(hw, 0.47 * fs)

    def spectra(v):
        p2 = np.abs(np.fft.fft(v, axis=1)) ** 2
        return p2, np.argmax(np.where(band, p2, 0.0), axis=1)

    yy = y0
    p2, pk = spectra(yy)
    chirp = 0.0
    for _ in range(2):                  # a line that moves a bin or more inside a piece: take that drift out
        if k < 4:
            break
        s = _theil_sen(tc, fq[pk])
        if abs(s - chirp) * PIECE_S < 2.0:
            break
        chirp = s
        tt = (np.arange(n) - n / 2.0) / fs
        yy = y0 * np.exp(-1j * np.pi * chirp * tt * tt).astype(np.complex64)
        p2, pk = spectra(yy)
    # each piece's peak to a small fraction of a bin: the piece mixed to its peak bin, thinned, and its spectrum
    # zero-padded 8 times or more (the same values a zero-padded transform of the whole piece gives there)
    m = np.arange(n, dtype=np.float64) / fs
    nz = 1 << int(math.ceil(math.log2(8 * (n // dec))))
    fz = np.fft.fftfreq(nz, dec / fs)
    near = np.abs(fz) <= 1.5 / PIECE_S
    f = np.empty(k)
    for i in range(0, k, 16):
        lo = np.exp(-2j * np.pi * fq[pk[i:i + 16]][:, None] * m[None, :]).astype(np.complex64)
        z = (yy[i:i + 16] * lo).reshape(-1, n // dec, dec).sum(axis=2)
        fz2 = np.abs(np.fft.fft(z, nz, axis=1)) ** 2
        j = np.argmax(np.where(near, fz2, 0.0), axis=1)
        r = np.arange(len(j))
        d = _parabola(fz2[r, j - 1], fz2[r, j], fz2[r, (j + 1) % nz])
        f[i:i + 16] = fq[pk[i:i + 16]] + fz[j] + d * fs / dec / nz
    # shares: the line's power within 25 Hz of its peak over the signal's power, noise taken off both
    avg = p2.mean(axis=0)
    flat = np.abs(fq) <= min(0.7 * hw, 0.45 * fs)
    # (the spectrum's own floor; the noise reference only bounds it, for a signal that fills the whole filter)
    floor = min(float(np.median(avg[flat])) / _quantile_of_mean(k, 0.0), 2.0 * noise / min(2.0 * hw, fs) * fs * w2)
    hb = max(1, int(round(LINE_HZ * n / fs)))
    cols = (pk[:, None] + np.arange(-hb, hb + 1)[None, :]) % n
    linep = np.take_along_axis(p2, cols, axis=1).sum(axis=1) - (2 * hb + 1) * floor
    sig = p2.sum(axis=1) - n * noise * w2
    share = np.where(sig > 0, np.clip(linep / np.maximum(sig, 1e-30), 0.0, 1.0), 0.0)
    out["pieces"] = int(k)
    out["share"] = round(float(np.median(share)), 2)
    # other carriers: narrow peaks well over the floor and over their own surroundings, away from the main line,
    # with nothing like them at the same distance on its other side (a pair either side is the line's own
    # modulation: an AM tone's sidebands, a data preamble)
    thr = (10.0 if k >= 3 else 20.0) * floor
    cand = np.flatnonzero((avg > np.roll(avg, 1)) & (avg >= np.roll(avg, -1)) & (avg >= thr) & band
                          & ((fq < f.min() - OTHER_LINE_HZ) | (fq > f.max() + OTHER_LINE_HZ)))
    cand = cand[np.argsort(avg[cand])[::-1]][:20]
    side = np.concatenate((np.arange(-int(300 * n / fs), -int(40 * n / fs)),
                           np.arange(int(40 * n / fs) + 1, int(300 * n / fs) + 1)))
    main = int(round(float(np.median(f)) * n / fs))
    others = []
    for c in cand:
        c = int(c)
        if avg[c] >= 10.0 * float(np.median(avg[(c + side) % n])) and \
                float(avg[(2 * main - c + np.arange(-2, 3)) % n].max()) < 0.25 * avg[c] and \
                all(abs(fq[c] - fq[o]) >= OTHER_LINE_HZ for o in others):
            others.append(c)
    aux["lines"] = 1 + len(others)
    part = sum(float(avg[(c + np.arange(-hb, hb + 1)) % n].sum()) - (2 * hb + 1) * floor for c in others[:2])
    aux["lines_share"] = (float(linep.mean()) + part) / max(float(sig.mean()), 1e-30)
    if len(others) <= 2 and (out["share"] >= LINE_SHARE or aux["lines_share"] >= 0.8):
        out["others_hz"] = [round(float(fq[c]), 1) for c in others]
    if out["share"] < LINE_SHARE:
        out["why"] = "no_single_line"
        return out, aux
    out["ok"] = True
    out["hz"] = round(float(np.median(f)), 1) + 0.0
    out["span_s"] = round(float(tc[-1] - tc[0]), 2)
    if k < 4:
        out["why"] = "too_short"        # the line is there; too little of it to see a drift
        return out, aux
    slope = _theil_sen(tc, f)
    res = f - (np.median(f - slope * tc) + slope * tc)
    out["slope_hz_s"] = round(slope, 2) + 0.0
    aux["at_start"] = float(np.median(f - slope * tc))
    out["scatter_hz"] = round(float(np.percentile(res, 95) - np.percentile(res, 5)), 1)
    return out, aux


# ------------------------------------------------------------------------------------------------- width and level
def _width(y, fs, runs, nb, blk, noise, hw, snr_on, overloaded, nfft=2048):
    """width: hz (the span that holds the middle `share` per cent of the signal's power: 99 from 12 dB, 90 from
    8 dB), limit "under" (hz is then 100: narrower than the look resolves) or "at_least" (it fills the look's
    filter), why. Welch spectrum over the on time; the floor is the spectrum's own lower quartile, and the noise's
    ups and downs cancel in the sum instead of adding to the width. Also returns the spectrum for the modulation
    look: (frequencies, signal power per bin, total, noise per Hz) or None."""
    out = {"hz": None, "share": None, "limit": None, "why": None}
    hop = nfft // 2
    starts = []
    for a, b in runs:
        a, b = (a + (1 if a > 0 else 0)) * blk, (b - (1 if b < nb else 0)) * blk
        starts += list(range(a, b - nfft + 1, hop))
    if len(starts) > 600:
        starts = starts[::int(math.ceil(len(starts) / 600.0))]
    k = len(starts)
    if k < 8:
        out["why"] = "too_weak" if snr_on < TRUST_DB else "too_short"
        return out, None
    w = np.hanning(nfft).astype(np.float32)
    w2 = float(np.sum(w.astype(np.float64) ** 2))
    seg = y[np.asarray(starts)[:, None] + np.arange(nfft)[None, :]] * w
    s = np.fft.fftshift(np.mean(np.abs(np.fft.fft(seg, axis=1)) ** 2, axis=0)).astype(np.float64)
    f = np.fft.fftshift(np.fft.fftfreq(nfft, 1.0 / fs))
    flat = np.abs(f) <= min(0.7 * hw, 0.45 * fs)
    reg = np.abs(f) <= min(0.9 * hw, 0.47 * fs)
    # (half-overlapped Hann segments average a little less than their count)
    floor = min(float(np.percentile(s[flat], 25)) / _quantile_of_mean(k / 1.06, -0.6745),
                2.0 * noise / min(2.0 * hw, fs) * fs * w2)
    fr, ex = f[reg], s[reg] - floor
    total = float(ex.sum())
    spec = (fr, ex, total, floor / (fs * w2)) if total > 0 else None
    if snr_on < TRUST_DB or overloaded or total <= 0:
        out["why"] = "overloaded" if overloaded and snr_on >= TRUST_DB else "too_weak"
        return out, spec
    share = 99 if snr_on >= GOOD_DB else 90
    span = _span(fr, ex, total, share / 100.0) + fs / nfft
    out["share"] = share
    if span <= NARROWEST_HZ:
        out["hz"], out["limit"] = NARROWEST_HZ, "under"
    else:
        out["hz"] = float(round(span))
        if span >= 0.8 * (fr[-1] - fr[0]):
            out["limit"], out["why"] = "at_least", "wider_than_filter"
    return out, spec


def _span(fr, ex, total, p):
    """The span holding the middle p of the power: each edge is where the sum from outside first reaches its tail."""
    tail = 0.5 * (1.0 - p) * total
    lo = int(np.argmax(np.cumsum(ex) >= tail))
    hi = len(ex) - 1 - int(np.argmax(np.cumsum(ex[::-1]) >= tail))
    return float(max(fr[hi] - fr[lo], 0.0))


def _level(bm, on, runs, noise, snr_on, overloaded, step=10):
    """level: range_db, the 5 to 95 % range of the power in 0.1 s steps while on (noise removed), why."""
    core = on.copy()
    for a, b in runs:                   # blocks at a key edge are only partly on
        if a > 0:
            core[a] = False
        if b < len(on):
            core[b - 1] = False
    k = len(bm) // step
    full = core[: k * step].reshape(k, step).all(axis=1) if k else np.zeros(0, dtype=bool)
    if snr_on < TRUST_DB or overloaded or int(full.sum()) < 10:
        return {"range_db": None, "why": "too_weak" if snr_on < TRUST_DB else
                "overloaded" if overloaded else "too_short"}
    v = np.maximum(bm[: k * step].reshape(k, step).mean(axis=1)[full] - noise, noise * 1e-3)
    db = 10.0 * np.log10(v)
    return {"range_db": round(float(np.percentile(db, 95) - np.percentile(db, 5)), 1), "why": None}


# ------------------------------------------------------------------------------------------------------ modulation
def _good_len(n):
    """The largest even length up to n whose transform is fast (factors 2, 3 and 5 only)."""
    n -= n % 2
    while n > 16:
        m = n
        for p in (2, 3, 5):
            while m % p == 0:
                m //= p
        if m == 1:
            return n
        n -= 2
    return max(n, 0)


def _welch(x, fs, nfft):
    """One-sided power density of a real track (each segment's mean removed)."""
    hop = nfft // 2
    k = (len(x) - nfft) // hop + 1
    if k < 2:
        return None, None
    w = np.hanning(nfft)
    seg = x[np.arange(nfft)[None, :] + hop * np.arange(k)[:, None]]
    s = np.mean(np.abs(np.fft.rfft((seg - seg.mean(axis=1, keepdims=True)) * w, axis=1)) ** 2, axis=0)
    return np.fft.rfftfreq(nfft, 1.0 / fs), s / (fs * float(np.sum(w ** 2))) * 2.0


def _peak(fq, s, lo, hi, win, excl):
    """The strongest point between lo and hi: (frequency, how far it stands over the median of its neighbourhood
    within win but outside excl, index), the frequency refined between the points."""
    sel = (fq >= lo) & (fq <= hi)
    if not sel.any():
        return None
    i = int(np.argmax(np.where(sel, s, 0.0)))
    nbh = (np.abs(fq - fq[i]) <= win) & (np.abs(fq - fq[i]) > excl)
    if not nbh.any() or i == 0 or i == len(s) - 1:
        return None
    d = float(_parabola(s[i - 1], s[i], s[i + 1]))
    return float(fq[i] + d * (fq[1] - fq[0])), float(s[i] / (float(np.median(s[nbh])) + 1e-30)), i


def _edge(f, cut, roll=0.25):
    """A low-pass response: 1 up to cut, a cosine edge down to 0 at (1 + roll) x cut."""
    return np.where(f <= cut, 1.0, np.where(f >= (1 + roll) * cut, 0.0,
                                            0.5 * (1.0 + np.cos(np.pi * (f - cut) / (roll * cut)))))


def _kurtosis(v):
    v = v - v.mean()
    return float(np.mean(v ** 4) / (float(np.mean(v ** 2)) ** 2 + 1e-30))


def _levels(spec, f, fsx, rate, n):
    """How many frequency levels the symbols take: the track low-passed to the rate, folded at the rate, read at
    the phase where the frequency moves least, and its histogram's clusters counted. Returns (count, evenly spaced).
    Only the count leaves this function."""
    v = np.fft.irfft(spec * _edge(f, min(0.65 * rate, 6000.0)), n)
    v = v[int(0.004 * fsx): n - int(0.004 * fsx)]
    which = np.minimum((((np.arange(len(v)) / fsx * rate) % 1.0) * 16).astype(int), 15)
    count = np.bincount(which[1:], minlength=16)
    move = np.bincount(which[1:], weights=np.abs(np.diff(v)), minlength=16) / np.maximum(count, 1)
    move = np.where(count > 0, move, np.inf)            # (a phase no sample falls on is not the quietest one)
    v = v[which == int(np.argmin(move))]
    if len(v) < 200:
        return 0, False
    lo, hi = np.percentile(v, 1), np.percentile(v, 99)
    hist, edges = np.histogram(v, bins=36, range=(lo - 0.08 * (hi - lo), hi + 0.08 * (hi - lo) + 1e-9))
    hs = np.convolve(hist, [0.25, 0.5, 0.25], mode="same")
    keep = []
    for p in range(1, 35):
        if not (hs[p] >= hs[p - 1] and hs[p] > hs[p + 1] and hs[p] >= 0.25 * hs.max()):
            continue
        if keep and hs[keep[-1]:p + 1].min() > 0.5 * min(hs[keep[-1]], hs[p]):
            if hs[p] > hs[keep[-1]]:        # no real valley between them: one cluster
                keep[-1] = p
        else:
            keep.append(p)
    gaps = np.diff([0.5 * (edges[i] + edges[i + 1]) for i in keep])
    return len(keep), bool(len(keep) == 4 and gaps.max() < 1.6 * gaps.min())


def _modulation(y, fs, runs, nb, blk, hw, snr_on, overloaded, line, aux, spec, centre):
    """mod: kind, tone_hz, rate_baud, levels, am_index (rms between 300 and 3400 Hz, noise removed), dev_rms_hz
    (frequency deviation between 300 and 3000 Hz, rms, noise removed), stretch_s, why. Measured on the longest
    unbroken stretch at a solid level. First match: lines (two or three separate carriers), none (no modulation
    during this look), tone (a standard sub-audible tone), fsk2 / fsk4 / data (a standard symbol rate; the level
    count only at 12 dB and 1 s), am, fm_voice, unknown (modulated, kind not determined). untested under 8 dB,
    under 0.3 s, or overloaded."""
    out = {"kind": "untested", "tone_hz": None, "rate_baud": None, "levels": None, "am_index": None,
           "dev_rms_hz": None, "hum_hz": None, "stretch_s": 0.0, "why": "too_weak" if snr_on < TRUST_DB else "too_short"}
    if not runs:
        return out
    a, b = max(runs, key=lambda r: r[1] - r[0])
    a, b = (a + (1 if a > 0 else 0)) * blk, (b - (1 if b < nb else 0)) * blk      # (blocks at a key edge)
    t_on = max(0, b - a) / fs
    out["stretch_s"] = round(t_on, 2)
    d = max(1, int(fs // 24000))
    m = _good_len(max(0, b - a) // d)
    if snr_on < TRUST_DB or overloaded or t_on < 0.3 or spec is None or m < 2048:
        out["why"] = "too_weak" if snr_on < TRUST_DB or (spec is None and t_on >= 0.3) else \
            "overloaded" if overloaded else "too_short"
        return out
    fr, ex, total, n0 = spec
    # a filter fitted to the signal, around its line (or the middle of what it occupies); the result thinned to
    # about 25 kS/s, which holds everything the filter lets through
    tail = 0.01 * total
    lo_f = float(fr[int(np.argmax(np.cumsum(ex) >= tail))])
    hi_f = float(fr[len(ex) - 1 - int(np.argmax(np.cumsum(ex[::-1]) >= tail))])
    if centre is None:
        centre = 0.5 * (lo_f + hi_f)
    fsx = fs / d
    cut = min(8000.0, max(2500.0, 0.6 * (hi_f - lo_f) + 800.0), 0.9 * hw, 0.36 * fsx)
    n = m * d
    sp = np.roll(np.fft.fft(y[a:a + n]), -int(round(centre * n / fs)))
    h = _edge(np.abs(np.fft.fftfreq(m, 1.0 / fsx)), cut)
    x = np.fft.ifft(np.concatenate((sp[:m // 2], sp[n - m // 2:])) * h) * (float(m) / n)
    x = x[int(0.004 * fsx): m - int(0.004 * fsx)]                    # (the filter rings at both ends)
    enb = float(np.sum(h * h)) * fs / n
    c = float(np.mean(np.abs(x) ** 2)) - n0 * enb
    if c <= 2.0 * n0 * enb:             # most of the signal lies outside the filter, or there is too little of it
        out["why"] = "too_weak"
        return out
    out["why"] = None
    cn0 = c / n0
    t_on = len(x) / fsx
    env = np.abs(x).astype(np.float64)
    nl = _good_len(len(x) - 1)
    fi = np.angle(x[1:nl + 1] * np.conj(x[:nl])).astype(np.float64) * fsx / (2.0 * np.pi)
    fi -= np.median(fi)
    share = line["share"] if line["share"] is not None else 0.0

    # amplitude: the envelope's power between 300 and 3400 Hz (voice), over all rates, and whether the power beside
    # the line is the same on both sides (AM is; a second carrier is not)
    nw = 2048 if len(env) >= 6 * 2048 else 512
    fe, se = _welch(env, fsx, nw)
    top = max(min(3400.0, cut), 300.0)
    sel = (fe >= 300.0) & (fe <= top)
    am_noise = n0 * (top - 300.0) / c
    am = math.sqrt(max(float(se[sel].sum()) * (fe[1] - fe[0]) / c - am_noise, 0.0))
    # speech ends near 3.4 kHz; clicks (power-line noise on a bare carrier) and wide data carry as much above it
    am_wide = False
    if cut >= 6000.0:
        top_a = min(7500.0, cut)
        hi = float(se[(fe >= 4000.0) & (fe <= top_a)].sum()) * (fe[1] - fe[0]) / c - n0 * (top_a - 4000.0) / c
        am_wide = hi > 0.25 * am * am
    am_all = math.sqrt(max((float(np.var(env)) - 0.5 * n0 * enb) / c, 0.0))
    up = float(ex[(fr > centre + 2.0 * LINE_HZ) & (fr <= centre + cut)].sum())
    down = float(ex[(fr < centre - 2.0 * LINE_HZ) & (fr >= centre - cut)].sum())
    two_sided = min(up, down) >= 0.5 * max(up, down) > 0
    # frequency: its power between 300 and 3000 Hz against what the noise alone gives at this level
    ff, sf = _welch(fi, fsx, nw)
    top = max(min(3000.0, cut), 300.0)
    sel = (ff >= 300.0) & (ff <= top)
    fm_noise = (top ** 3 - 300.0 ** 3) / (3.0 * cn0)
    fm_pow = float(sf[sel].sum()) * (ff[1] - ff[0])
    fm_ratio = fm_pow / fm_noise if fm_noise > 0 else 0.0
    fm_wide = False
    if cut >= 5000.0 and snr_on >= WIDE_DB:
        top_f = min(6000.0, cut)
        hi = float(sf[(ff >= 3600.0) & (ff <= top_f)].sum()) * (ff[1] - ff[0]) \
            - (top_f ** 3 - 3600.0 ** 3) / (3.0 * cn0)
        fm_wide = hi > 0.5 * max(fm_pow - fm_noise, 0.0)
    out["am_index"] = round(am, 3)
    out["dev_rms_hz"] = round(math.sqrt(max(fm_pow - fm_noise, 0.0)), 1)

    # the frequency track's spectrum, once: low-passed for the data tests, band-passed for the voice test
    spec_f = np.fft.rfft(fi)
    f_f = np.fft.rfftfreq(nl, 1.0 / fsx)
    fd = np.fft.irfft(spec_f * _edge(f_f, min(6000.0, cut)), nl)
    kurt_data = _kurtosis(fd)
    kurt_voice = _kurtosis(np.fft.irfft(spec_f * _edge(f_f, top) * (f_f >= 300.0), nl))
    # a hum: the audio-band frequency power sits on multiples of 50 or 60 Hz (mains ripple on a transmitter's
    # supply); speech moves its pitch, so its power does not stay on such a comb
    hum = None
    pw = np.abs(spec_f) ** 2
    vb = (f_f >= 300.0) & (f_f <= top)
    band_pow = float(pw[vb].sum())
    if band_pow > 0 and fm_ratio >= VOICE_FM and t_on >= 1.0:
        for base in (50.0, 60.0):
            near = np.abs(f_f - base * np.round(f_f / base)) <= max(3.0, 2.0 / t_on)
            flat_share = float(np.count_nonzero(vb & near)) / max(int(np.count_nonzero(vb)), 1)
            if float(pw[vb & near].sum()) / band_pow >= max(HUM_SHARE, 3.0 * flat_share):
                hum = base
                break

    # a symbol rate: a line in the spectrum of how fast the frequency changes, at a standard rate
    rate = None
    step = np.abs(np.diff(fd))
    nw1 = 4096 if len(step) >= 4 * 4096 else 1024
    f1, s1 = _welch(step, fsx, nw1)
    pk = _peak(f1, s1, 300.0, 0.5 * fsx - 100.0, 400.0, 20.0) if f1 is not None else None
    if pk and pk[1] >= RATE_PROM:
        near = min(RATES, key=lambda r: abs(r - pk[0]))
        if abs(near - pk[0]) <= max(0.004 * near, f1[1] - f1[0]):
            rate = near
    # one audio tone carrying most of the frequency power is tone signalling, not symbols (a tone at half a
    # standard rate gives the same rate line)
    one_tone = False
    fa, sa = _welch(fd, fsx, nw1)
    if fa is not None:
        sel = (fa >= 300.0) & (fa <= min(6000.0, cut))
        i = int(np.argmax(np.where(sel, sa, 0.0)))
        rest = float(sa[sel].sum()) - (min(6000.0, cut) ** 3 - 300.0 ** 3) / (3.0 * cn0) / (fa[1] - fa[0])
        one_tone = float(sa[max(i - 2, 0):i + 3].sum()) >= 0.5 * max(rest, 1e-30)

    # a sub-audible tone: a line between 60 and 260 Hz in the whole stretch's frequency track
    tone = None
    d2 = max(1, int(round(fsx / 2000.0)))
    low = np.convolve(fi, np.ones(2 * d2) / (2.0 * d2), mode="valid")[::d2]
    if t_on >= 1.0 and len(low) >= 64:
        wl = np.hanning(len(low))
        pad = 1 << int(math.ceil(math.log2(4 * len(low))))
        sl = np.abs(np.fft.rfft((low - low.mean()) * wl, pad)) ** 2
        pk2 = _peak(np.fft.rfftfreq(pad, d2 / fsx), sl, 62.0, 258.0, 30.0, 2.5 / t_on + 0.5)
        if pk2 and pk2[1] >= TONE_PROM:
            dev = 2.0 * math.sqrt(float(sl[pk2[2]])) / float(wl.sum()) / math.sqrt(2.0) \
                / abs(float(np.sinc(pk2[0] * 2 * d2 / fsx)))
            std = min(CTCSS, key=lambda v: abs(v - pk2[0]))
            need = MAINS_DEV_HZ if any(abs(std - v) <= 2.0 for v in MAINS_HZ) else TONE_DEV_HZ
            if abs(std - pk2[0]) <= max(0.005 * std, 1.0 / t_on) and dev >= need:
                tone = std
                # a standard tone that is also a multiple of the mains frequency: hum reads the same
                for base in (50.0, 60.0):
                    k = round(std / base)
                    if k >= 1 and abs(std - base * k) <= max(0.005 * std, 1.0 / t_on):
                        hum = base

    if aux["lines"] in (2, 3) and aux["lines_share"] >= 0.8:
        out["kind"] = "lines"
    elif share >= 0.8 and am < max(QUIET_AM, 0.55 * math.sqrt(am_noise)) and fm_ratio < QUIET_FM:
        out["kind"] = "none"
    elif tone and rate is None and am_all < FLAT_ENV:
        out["kind"], out["tone_hz"] = "tone", float(tone)
    elif rate and kurt_data < 2.4 and not one_tone and am_all < FLAT_ENV:
        out["kind"], out["rate_baud"] = "data", int(rate)
        if snr_on >= LEVELS_DB and t_on >= 1.0 and rate <= LEVELS_MAX_RATE:
            count, even = _levels(spec_f, f_f, fsx, _fine_rate(step, fsx, pk[0], f1[1] - f1[0]), nl)
            if count == 2 or (count == 4 and even):
                out["kind"], out["levels"] = "fsk%d" % count, count
    elif share >= 0.45 and am >= AM_INDEX and am >= 0.9 * math.sqrt(am_noise) and fm_ratio < 6.0 and two_sided \
            and snr_on >= GOOD_DB and not am_wide:
        out["kind"] = "am"
    elif fm_ratio >= VOICE_FM and am_all < FLAT_ENV and 3.5 < kurt_voice < CLICK_KURT and not fm_wide and hum is None:
        out["kind"] = "fm_voice"
    else:
        out["kind"] = "unknown"
    if out["kind"] in ("tone", "unknown"):
        out["hum_hz"] = hum
    return out


def _fine_rate(step, fsx, rate, width):
    """The symbol rate to a small fraction of 1 / (the stretch's length), which folding at the rate needs: the rate
    line mixed to 0 Hz, thinned, and its zero-padded spectrum's peak within `width` of where it was found."""
    n = len(step)
    dec = max(1, n // 512)
    k = n // dec
    t = np.arange(k * dec) / fsx
    z = (step[: k * dec] - step.mean()) * np.hanning(k * dec) * np.exp(-2j * np.pi * rate * t)
    z = z.reshape(k, dec).sum(axis=1)
    s = np.abs(np.fft.fft(z, 16384)) ** 2
    f = np.fft.fftfreq(16384, dec / fsx)
    i = int(np.argmax(np.where(np.abs(f) <= 2.0 * width, s, 0.0)))
    return rate + float(f[i]) + float(_parabola(s[i - 1], s[i], s[(i + 1) % 16384])) * fsx / dec / 16384
