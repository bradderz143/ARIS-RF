"""Channel power from an IQ capture. numpy only, so the same maths runs on this PC and on a remote node.

The signal sits `offset_hz` away from the tuning point (never on the receiver's own DC spike). The capture is cut
into short blocks (about 2 ms); for each block we add up the power inside the channel. The noise floor comes from
bins just beside the channel. Bursty devices (remotes, sensors) are only on for a few milliseconds, so the level we
report is the power while the signal is on (90th percentile of those blocks, noise removed), not the average
over the whole capture.

Long captures are processed in chunks so a 10 s capture never needs hundreds of MB of RAM.
"""
from __future__ import annotations

import numpy as np

ON_THRESHOLD_DB = 6.0          # a block counts as "signal on" this far above the floor


def iq_from_u8(raw: np.ndarray):
    """rtl_sdr output (interleaved uint8 I/Q) -> (complex64 samples with DC removed, fraction of clipped samples)."""
    raw = raw[: (len(raw) // 2) * 2]
    clipped = float(np.count_nonzero((raw <= 1) | (raw >= 254))) / len(raw) if len(raw) else 0.0
    z = ((raw.astype(np.float32) - 127.4) / 128.0).view(np.complex64)   # interleaved I/Q = complex64 in memory
    z -= z.mean()
    return z, clipped


def plan_capture(bw_khz: float):
    """Sample rate and tuning offset for a channel this wide. Returns (fs_hz, offset_hz)."""
    bw = float(bw_khz) * 1e3
    if bw <= 50e3:
        return 1_024_000, 200_000
    return 2_048_000, 450_000


def _to_db(x):
    return 10.0 * np.log10(np.maximum(x, 1e-20))


class _Acc:
    """Collects per-block channel power across chunks."""

    def __init__(self, fs, offset_hz, bw_hz, block_ms):
        self.fs, self.offset, self.bw = float(fs), float(offset_hz), float(bw_hz)
        self.nfft = 1 << int(round(np.log2(max(256.0, self.fs * block_ms / 1000.0))))
        self.w = np.hanning(self.nfft).astype(np.float32)
        self.wnorm = self.nfft * float(np.sum(self.w * self.w))
        f = np.fft.fftshift(np.fft.fftfreq(self.nfft, 1.0 / self.fs))
        half = self.bw / 2.0
        d = np.abs(f - self.offset)
        gap = max(half * 0.5, 2.5 * self.fs / self.nfft)
        self.f = f
        self.chan = d <= half
        self.ref = (d > half + gap) & (d <= half + gap + max(self.bw, 10e3)) & (np.abs(f) > 5e3) & \
                   (np.abs(f) < 0.45 * self.fs)
        if not self.chan.any():
            raise ValueError("channel outside the capture")
        self.n_chan = int(self.chan.sum())
        self.fc = self.f[self.chan]
        # Per block: channel power, and the power-weighted frequency sum (for the carrier offset). Two numbers per
        # block instead of the whole channel spectrum keeps a 15 s capture at a few hundred kB.
        self.chan_p, self.chan_fp, self.ref_med = [], [], []
        self.clip_sum, self.clip_n = 0.0, 0

    def add(self, z, clipped=0.0):
        nb = len(z) // self.nfft
        if nb < 1:
            return
        frames = z[: nb * self.nfft].reshape(nb, self.nfft) * self.w
        F = np.fft.fftshift(np.fft.fft(frames, axis=1), axes=1)
        P = (F.real * F.real + F.imag * F.imag) / self.wnorm
        pc = P[:, self.chan]
        self.chan_p.append(pc.sum(axis=1))
        self.chan_fp.append(pc @ self.fc)
        if self.ref.any():
            self.ref_med.append(float(np.median(P[:, self.ref])))
        self.clip_sum += clipped * nb
        self.clip_n += nb

    def result(self):
        if not self.chan_p:
            raise ValueError("IQ capture too short to measure")
        chan_p = np.concatenate(self.chan_p)
        nb = len(chan_p)
        if nb < 8:
            raise ValueError("IQ capture too short to measure")
        # Noise power per bin is exponentially distributed: its median is ln 2 times its mean.
        floor_lin = (float(np.median(self.ref_med)) / np.log(2.0) * self.n_chan if self.ref_med
                     else float(np.percentile(chan_p, 20)))
        chan_db = _to_db(chan_p)
        floor_db = float(_to_db(floor_lin))
        k = min(5, nb)                                             # about 10 ms smoothing for the peak
        smooth = np.convolve(chan_p, np.ones(k) / k, mode="valid")
        peak_db = float(_to_db(smooth.max()))
        on = chan_db > floor_db + ON_THRESHOLD_DB
        active = float(on.mean())
        # Level while on: the 90th percentile of the "on" blocks with the noise taken off. A median would read
        # weak bursts too high (the threshold drops their partly-on blocks but keeps them for strong ones), which
        # squeezes the differences between receivers that localisation depends on.
        level_db = (float(_to_db(max(float(np.percentile(chan_p[on], 90)) - floor_lin, floor_lin * 1e-3)))
                    if on.any() else peak_db)
        fp = np.concatenate(self.chan_fp)
        sel = on if on.any() else np.ones(nb, dtype=bool)
        offset_meas = float(fp[sel].sum() / max(float(chan_p[sel].sum()), 1e-20))   # centroid of the "on" spectrum
        return {
            "level_db": round(level_db, 2),         # power while the signal is on (what localisation uses)
            "peak_db": round(peak_db, 2),
            "mean_db": round(float(_to_db(chan_p.mean())), 2),
            "floor_db": round(floor_db, 2),
            "snr_db": round(level_db - floor_db, 2),
            "active_fraction": round(active, 4),    # 1.0 = continuous carrier, small = bursts
            "heard": bool(on.any() and level_db - floor_db >= ON_THRESHOLD_DB),
            "carrier_offset_hz": round(offset_meas - self.offset, 1),
            "clipped_fraction": round(self.clip_sum / max(self.clip_n, 1), 5),
            "blocks": int(nb),
            "block_ms": round(self.nfft / self.fs * 1000.0, 3),
        }


def channel_power(z: np.ndarray, fs: float, offset_hz: float, bw_hz: float, block_ms: float = 2.0,
                  clip_fraction: float = 0.0) -> dict:
    """Power in [offset - bw/2, offset + bw/2] of the complex baseband capture z, in dB relative to the ADC's
    full scale (dBFS). Subtract the receiver gain to compare receivers that used different gains."""
    acc = _Acc(fs, offset_hz, bw_hz, block_ms)
    z = np.asarray(z, dtype=np.complex64)
    step = acc.nfft * 256
    for i in range(0, len(z), step):
        acc.add(z[i:i + step], clip_fraction)
    return acc.result()


def channel_power_u8(raw: np.ndarray, fs: float, offset_hz: float, bw_hz: float, block_ms: float = 2.0,
                     chunk_s: float = 0.5) -> dict:
    """Same as channel_power, straight from rtl_sdr's uint8 output, in chunks (low memory)."""
    acc = _Acc(fs, offset_hz, bw_hz, block_ms)
    step = max(acc.nfft, int(fs * chunk_s) // acc.nfft * acc.nfft) * 2       # bytes: 2 per complex sample
    raw = np.asarray(raw, dtype=np.uint8)
    for i in range(0, len(raw), step):
        z, clip = iq_from_u8(raw[i:i + step])
        acc.add(z, clip)
    return acc.result()
