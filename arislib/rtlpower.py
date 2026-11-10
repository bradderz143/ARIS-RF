"""rtl_power's own arithmetic (rtl-sdr src/rtl_power.c, frequency_range() and csv_dbm()), so ARIS reads its CSV
rows at the right frequencies.

What rtl_power prints for each hop: "Hz low, Hz high, Hz step, samples, dB, dB, ...". Two quirks matter:
  * Hz low = centre - bw2, with bw2 from a rounded bin count, while the first printed bin is really at
    centre + (i1 - len/2) * step. With a crop (ARIS uses 20%) the two differ by half a bin to one bin, so reading
    bin j as "Hz low + j * step" puts everything up to one bin too high (about 22 kHz in an 88-108 MHz view).
  * The last bin is printed twice.
The real bin frequencies are therefore centre + (j - n/2) * step for the n real values, with centre = (Hz low +
Hz high) / 2. `bin_freqs` does that; `plan` reproduces the hop layout (the simulated rtl_power in test/ uses it to
write rows exactly like the real one, and old baselines use it to correct their stored frequencies).
"""
from __future__ import annotations

import math

MAXIMUM_RATE = 2800000
MINIMUM_RATE = 1000000


def bin_freqs(hz_low, hz_high, hz_step, n_values):
    """Frequencies (Hz) of the real values in one CSV row, and how many values are real (the last one printed is a
    copy of the one before it). Returns (first_hz, step_hz, n_real)."""
    n = int(n_values)
    n_real = n - 1 if n >= 2 else n
    centre = (float(hz_low) + float(hz_high)) / 2.0
    if n_real <= 1:
        return centre, float(hz_step), n_real
    return centre - (n_real / 2.0) * float(hz_step), float(hz_step), n_real


def plan(lower_hz, upper_hz, max_bin_hz, crop=0.0, boxcar=True):
    """The hops rtl_power makes for "-f lower:upper:bin -c crop" (integer arithmetic as in the C code).
    Each hop: freq (tuned centre), rate, len (FFT size), step, i1, i2 (printed bins), bw2 (half the printed span)."""
    lower, upper, max_size = int(lower_hz), int(upper_hz), int(max_bin_hz)
    downsample = 1
    tune_count, bw_seen, bw_used = 1, upper - lower, upper - lower
    for i in range(1, 1500):
        bw_seen = int((upper - lower) / i)                    # C int division of positive ints
        bw_used = int(bw_seen / (1.0 - crop))
        if bw_used > MAXIMUM_RATE:
            continue
        tune_count = i
        break
    if bw_used < MINIMUM_RATE:
        tune_count = 1
        downsample = MAXIMUM_RATE // bw_used
        bw_used = bw_used * downsample
    if not boxcar and downsample > 1:
        passes = int(math.log2(downsample))
        downsample = 1 << passes
        bw_used = int(bw_seen * downsample / (1.0 - crop))
    bin_e = 1
    for i in range(1, 22):
        bin_e = i
        if bw_used / ((1 << i) * downsample) <= max_size:
            break
    if max_size >= MINIMUM_RATE:
        bw_seen = bw_used = max_size
        tune_count = (upper - lower) // bw_seen
        bin_e, crop = 0, 0.0
    hops = []
    for i in range(tune_count):
        length = 1 << bin_e
        freq = lower + i * bw_seen + bw_seen // 2
        bin_count = int(length * (1.0 - crop))
        bw2 = int((bw_used * bin_count) / (length * 2 * downsample))
        i1 = int(length * crop * 0.5)
        i2 = (length - 1) - int(length * crop * 0.5)
        hops.append({"freq": freq, "rate": bw_used, "len": length, "downsample": downsample,
                     "step": bw_used / (length * downsample), "i1": i1, "i2": i2, "bw2": bw2})
    return hops


def hop_bins(hop):
    """True frequencies (Hz) of a hop's printed bins i1..i2 (after rtl_power's FFT shift: bin i is at
    freq + (i - len/2) * step)."""
    return [hop["freq"] + (i - hop["len"] / 2.0) * hop["step"] for i in range(hop["i1"], hop["i2"] + 1)]


def old_bias_hz(lower_hz, upper_hz, max_bin_hz, crop=0.0):
    """How far ARIS before v4.7.2 placed this sweep's bins from their true frequencies (old minus true, Hz): it read
    bin j as Hz low + j * step. The same for every hop of one sweep."""
    hops = plan(lower_hz, upper_hz, max_bin_hz, crop)
    if not hops:
        return 0.0
    h = hops[0]
    true_first = h["freq"] + (h["i1"] - h["len"] / 2.0) * h["step"]
    return (h["freq"] - h["bw2"]) - true_first


FULL_SCALE = 127.0      # the largest amplitude of an 8-bit sample after rtl_power's "buf - 127"
WINDOW_SCALE = 256      # rtl_power keeps its window as (int)(256 * window_fn(i, length))


def window_coefs(name, length):
    """rtl_power's integer window coefficients, (int)(256 * window_fn(i, length)) as in rtl_power.c. A name
    rtl_power does not know leaves it with its default, the rectangle, so the same here (ARIS passes -w from the
    config, "hamming" by default). 'youssef' is, in rtl_power's words, "really a blackman-harris variant" and is
    treated as blackman-harris."""
    name = (name or "hamming").strip().lower()
    if name not in ("rectangle", "hann", "hanning", "blackman", "blackman-harris", "youssef", "bartlett", "hamming"):
        name = "rectangle"
    n = max(1, int(length))
    d = float(n - 1) if n > 1 else 1.0
    out = []
    for i in range(n):
        x = 2.0 * math.pi * i / d
        if name == "rectangle":
            w = 1.0
        elif name in ("hann", "hanning"):
            w = 0.5 - 0.5 * math.cos(x)
        elif name == "blackman":
            w = 0.42 - 0.5 * math.cos(x) + 0.08 * math.cos(2 * x)
        elif name in ("blackman-harris", "youssef"):
            w = 0.35875 - 0.48829 * math.cos(x) + 0.14128 * math.cos(2 * x) - 0.01168 * math.cos(3 * x)
        elif name == "bartlett":
            t = 2.0 * i / d
            w = t if t <= 1.0 else 2.0 - t
        else:                                                   # hamming
            w = 0.54 - 0.46 * math.cos(x)
        out.append(int(WINDOW_SCALE * w))
    return out


def fullscale_db(hop, window="hamming"):
    """The value rtl_power prints for a full-scale sine (amplitude 127 after its "- 127") in this hop. Subtract it
    from a row's values to get dBFS, 0 dB = that sine; every real level then sits at or below 0.

    From rtl_power.c: csv_dbm() prints 10 * log10(avg / rate / samples), where avg is the sum over the FFTs of
    |X[bin]|^2, the fixed-point fix_fft() halves at each of its log2(len) stages (so X = DFT / len), each sample was
    multiplied by the integer window (256 * w), boxcar downsampling sums ds samples into one, and `samples` grows
    by ds per FFT. A sine of amplitude A on one bin therefore gives, per FFT, (A * ds * mean(256 w))^2, and after
    the division (A * mean(256 w))^2 * ds / rate: the bin width, the crop and the number of FFTs cancel out, so
    the reference depends only on the hop's sample rate, its downsample factor and the window."""
    coefs = window_coefs(window, hop["len"])
    wbar = sum(coefs) / float(len(coefs))
    lin = (FULL_SCALE * wbar) ** 2 * float(hop.get("downsample", 1)) / float(hop["rate"])
    return 10.0 * math.log10(lin)


def sweep_fullscale_db(lower_hz, upper_hz, max_bin_hz, crop=0.0, window="hamming"):
    """fullscale_db for a whole sweep (every hop of one sweep shares its rate, FFT size and downsample)."""
    hops = plan(lower_hz, upper_hz, max_bin_hz, crop)
    return fullscale_db(hops[0], window) if hops else 0.0


def parse_crop(value, default=0.0):
    """'20%' -> 0.2, '0.2' -> 0.2 (rtl_power's own -c parsing)."""
    s = str(value if value is not None else "").strip()
    try:
        return float(s[:-1]) / 100.0 if s.endswith("%") else float(s)
    except ValueError:
        return default
