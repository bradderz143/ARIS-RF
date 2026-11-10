"""Band capture: hear every burst in a band, not just the ones on one frequency.

ISM-band devices (remotes, sensors, tyre-pressure sensors, meters) each transmit for a few milliseconds on their own
frequency and then go quiet, so a baseline says little about them. ARIS records the band's IQ continuously and this
detector finds each burst in time and frequency; the engine then cuts that burst out of the recording, centred on its
own frequency, and decodes it (rtl_433 on the recording). Nothing in the band is missed while the dongle is on it.

A band wider than one capture (902-928 MHz) is visited in slices (`slices`), longer where bursts were heard.
numpy only. Receive-only: the detector measures timing, frequency, width and level, never content."""
from __future__ import annotations

import math

import numpy as np

# name -> (low MHz, high MHz, label). The Americas (ITU Region 2): 902-928 is the ISM band; 433.05-434.79 is the
# low-power device band most remotes and sensors use.
BANDS = {
    "433": (433.05, 434.79, "433 MHz band (433.05-434.79)"),
    "915": (902.0, 928.0, "902-928 MHz ISM band"),
    "315": (314.0, 316.0, "315 MHz band (314-316)"),
}


def slices(lo_mhz, hi_mhz, usable_mhz):
    """Tunings that cover lo..hi with captures whose clean part is usable_mhz wide (evenly spread)."""
    span = float(hi_mhz) - float(lo_mhz)
    n = max(1, int(math.ceil(span / float(usable_mhz) - 1e-9)))
    step = span / n
    return [round(float(lo_mhz) + step * (i + 0.5), 4) for i in range(n)]


class BurstDetector:
    """Feed it IQ blocks (complex64 at fs) as they arrive; it returns the bursts that have ended: dicts with start
    and end (seconds since the stream began), offset from the tuning (Hz), width (Hz) and level over the noise (dB).

    How: short FFT frames (nfft bins, about 0.2 ms each), averaged over `avg` frames; a cell counts as active when it
    stands `thresh_db` over that bin's noise floor (the bin's median, followed slowly, so a steady carrier becomes part
    of the floor and is never a "burst"). Active cells next to each other in frequency, and less than `gap_s` apart in
    time, belong to one burst. Only the flat part of the capture (`edge` of the band edge) is used. The bins next to
    0 Hz (the receiver's own DC spike) are used with a threshold `dc_extra_db` higher: v4.8.6 left them out, a 14 kHz
    blind spot right where the 433 band's devices crowd (433.863-433.877 at its tuning of 433.87)."""

    DC_EXTRA_DB = 6.0
    JOIN_FRAMES = 15                     # FSK tones start and end together: within this many frames (3 ms)

    def __init__(self, fs, nfft=512, avg=4, thresh_db=9.0, gap_s=0.03, min_s=0.0008, max_s=2.0, edge=0.7,
                 dc_extra_db=None):
        self.fs, self.nfft, self.avg = float(fs), int(nfft), max(1, int(avg))
        self.k = 10.0 ** (thresh_db / 10.0)
        self.frame_s = self.nfft / self.fs
        self.gap = max(1, int(round(gap_s / self.frame_s)))
        self.min_frames = max(1, int(round(min_s / self.frame_s)))
        self.max_frames = max(2, int(round(max_s / self.frame_s)))
        f = np.fft.fftshift(np.fft.fftfreq(self.nfft, 1.0 / self.fs))
        self.freqs = f
        self.use = np.abs(f) <= edge * self.fs / 2.0
        dc = np.abs(f) <= 2.0 * self.fs / self.nfft                 # the receiver's own DC spike: a higher threshold
        extra = self.DC_EXTRA_DB if dc_extra_db is None else float(dc_extra_db)
        self.kbin = np.where(dc, self.k * 10.0 ** (extra / 10.0), self.k).astype(np.float32)
        self.win = np.hanning(self.nfft).astype(np.float32)
        self.floor = None
        self.frame0 = 0                                              # frames handed in so far
        self._tail = None                                            # last avg-1 frames, for the running mean
        self._rest = np.zeros(0, dtype=np.complex64)                 # samples short of a whole frame
        self.open = []
        self.recent = []                                             # bursts closed in the last second
        self.ghosts = 0                                              # side-bands / images dropped
        self._absorb = np.zeros(self.nfft, dtype=bool)               # bins of a carrier: join the floor next block
        self._mute = np.zeros(self.nfft, dtype=bool)                 # ... and are ignored for the rest of this one

    def _frames(self, z):
        z = np.concatenate([self._rest, np.asarray(z, dtype=np.complex64)])
        n = len(z) // self.nfft
        self._rest = z[n * self.nfft:]
        if n == 0:
            return np.zeros((0, self.nfft), dtype=np.float32)
        X = np.fft.fftshift(np.fft.fft(z[: n * self.nfft].reshape(n, self.nfft) * self.win, axis=1), axes=1)
        return (X.real ** 2 + X.imag ** 2).astype(np.float32)

    def feed(self, z) -> list:
        P = self._frames(z)
        if not len(P):
            return []
        a = self.avg
        prev = self._tail if self._tail is not None else np.repeat(P[:1], a - 1, axis=0)
        Q = np.concatenate([prev, P]) if a > 1 else P
        self._tail = Q[-(a - 1):] if a > 1 else None
        c = np.cumsum(Q, axis=0, dtype=np.float64)
        S = ((c[a - 1:] - np.concatenate([np.zeros((1, self.nfft)), c[:-a]])) / a).astype(np.float32)
        med = np.median(S, axis=0)
        if self.floor is None:
            self.floor = med
        if self._absorb.any():                   # a carrier closed in the last block: it is floor from now on
            self.floor = np.where(self._absorb, np.maximum(self.floor, med), self.floor)
            self._absorb[:] = False
        self._mute[:] = False
        act = (S > self.floor * self.kbin) & self.use
        # The floor follows slowly and never jumps up with a burst-filled block. Where a burst is on, it does not
        # rise at all: a remote held down fills its bins' median, and a floor that rose 3 dB per block cut it off
        # after 3 blocks (0.75 s, found on the real PC). A signal on for longer than max_s is a carrier: _close ends
        # it, mutes its bins for the rest of the block and has them join the floor at the next one (`_absorb`).
        # It falls at once: a burst can only raise a block's median, so a lower median is the noise. Falling 15% per
        # block left a channel deaf for seconds after a carrier or a held remote ended (v4.8.6: a remote pressed again
        # 0.3-2 s after a 3.5 s press was not heard, nor 5 s after a 40 dB carrier). Never below a quarter of the
        # band's own noise level in one block (a block of zeros must not make every bin a burst).
        # When the whole band falls at once (samples missing for a block, a gain step) it is not a burst ending:
        # then it falls slowly as before, or a few such blocks would leave the floor far under the noise and the
        # noise coming back would be one band-wide false burst.
        gmed = float(np.median(self.floor[self.use])) if self.use.any() else float(np.median(self.floor))
        bmed = float(np.median(med[self.use])) if self.use.any() else float(np.median(med))
        fall = (0.85 * self.floor + 0.15 * med if bmed < 0.5 * gmed else
                np.maximum(med, 0.25 * np.minimum(self.floor, gmed)))
        new = np.where(med < self.floor, fall, np.minimum(0.85 * self.floor + 0.15 * med, 2.0 * self.floor))
        hold = np.convolve(act.any(axis=0).astype(np.float32), np.ones(9, dtype=np.float32), "same") > 0.5
        for b in self.open:
            hold[max(0, b["lo"] - 4):b["hi"] + 5] = True
        self.floor = np.where(hold, np.minimum(self.floor, new), new)
        out = []
        rows = np.flatnonzero(act.any(axis=1))
        for i in rows:
            g = self.frame0 + int(i)
            cols = np.flatnonzero(act[i] & ~self._mute)
            if not len(cols):
                continue
            # runs of active bins (gaps of up to 2 bins joined): one signal each
            cuts = np.flatnonzero(np.diff(cols) > 3)
            for lo, hi in zip(np.r_[cols[0], cols[cuts + 1]], np.r_[cols[cuts], cols[-1]]):
                b = next((o for o in self.open if o["last"] >= g - self.gap and lo <= o["hi"] + 4
                          and hi >= o["lo"] - 4), None)
                if b is None:
                    b = {"first": g, "last": g, "lo": int(lo), "hi": int(hi), "e": np.zeros(self.nfft),
                         "peak": 0.0}
                    self.open.append(b)
                nlo, nhi = min(b["lo"], int(lo)), max(b["hi"], int(hi))
                if nhi - nlo <= 2 * (b["hi"] - b["lo"]) + 8:
                    b["lo"], b["hi"] = nlo, nhi
                # (else one row of a strong burst's edge splatter would stretch this burst over a neighbour's bins,
                # and the neighbour's next rows would all land in it: two devices as one wide burst)
                b["last"] = g
                seg = S[i, lo:hi + 1]
                b["e"][lo:hi + 1] += seg
                rel = float(np.max(seg / self.floor[lo:hi + 1]))
                b["peak"] = max(b["peak"], rel)
            out += self._close(g, force_long=True)        # (only carriers: ended bursts close at the block end)
        self.frame0 += len(P)
        return out + self._close(self.frame0)

    GHOST = 10.0 ** (20.0 / 10.0)        # 20 dB weaker than a burst at the very same moment: its side-band or image

    def _close(self, now, force_long=False, final=False) -> list:
        """Ends the bursts that are over. Called for each active row (force_long: only signals on for longer than
        max_s, carriers) and at the end of each block (every burst whose gap ran out). v4.8.6 also closed ended
        bursts row by row: with any other signal on in the band, each FSK tone closed at its own row and the two
        were never joined (one tyre-sensor burst logged as two, each cut to one tone)."""
        closing, keep = [], []
        for b in self.open:
            ended = (not force_long) and b["last"] < now - self.gap
            too_long = force_long and b["last"] - b["first"] >= self.max_frames
            if too_long and not ended:
                self._absorb[b["lo"]:b["hi"] + 1] = True
                self._mute[b["lo"]:b["hi"] + 1] = True
            (closing if ended or too_long else keep).append(b)
        if not final and not force_long:
            # a tone whose partner is still open (the block ended between their ends) waits for it
            wait = [b for b in closing if any(self._one_burst(b, o) for o in keep)]
            closing = [b for b in closing if b not in wait]
            keep += wait
        self.open = keep
        if not closing:
            return []
        # A strong on/off signal spreads side-bands far out (its sharp edges), and a dongle has images: they appear
        # at the very same moments as the real burst, much weaker. They are not other devices.
        pool = closing + keep + self.recent
        real = []
        for b in closing:
            if b["last"] - b["first"] + 1 < self.min_frames:
                continue
            span = b["last"] - b["first"] + 1
            if any(o is not b and o["peak"] >= b["peak"] * self.GHOST
                   and (min(o["last"], b["last"]) - max(o["first"], b["first"]) + 1) >= 0.6 * span for o in pool):
                self.ghosts += 1
                continue
            real.append(b)
        real = [p for b in self._splinters(real, keep) for p in self._split_far(b)]
        out = [self._report(b) for b in self._join_tones(real)]
        horizon = now - int(round(1.0 / self.frame_s))
        self.recent = [o for o in self.recent + closing if o["last"] >= horizon]
        return out

    def _split_far(self, b) -> list:
        """A burst whose energy sits in places further apart than an FSK burst's tones is two devices that a strong
        burst's edge splatter connected (a remote at 50 dB per bin bridges hundreds of kHz): one report each."""
        e = b["e"]
        sel = np.flatnonzero(e > 0.1 * max(float(e.max()), 1e-30))
        if len(sel) < 2:
            return [b]
        step = self.fs / self.nfft
        cuts = np.flatnonzero(np.diff(sel) > 3)
        groups = []
        for lo, hi in zip(np.r_[sel[0], sel[cuts + 1]], np.r_[sel[cuts], sel[-1]]):
            if groups and (hi - groups[-1][0]) * step <= self.TONES_HZ:
                groups[-1][1] = int(hi)
            else:
                groups.append([int(lo), int(hi)])
        if len(groups) == 1:
            return [b]
        out = []
        for lo, hi in groups:
            m = np.zeros_like(e)
            m[max(0, lo - 4):hi + 5] = 1.0
            out.append(dict(b, lo=lo, hi=hi, e=e * m))
        return out

    @staticmethod
    def _core(o):
        """The bins that carry a burst's energy, not the splatter of a strong burst's edges (which widens lo..hi)."""
        idx = np.flatnonzero(o["e"] > 0.1 * max(float(o["e"].max()), 1e-30))
        return (int(idx[0]) - 2, int(idx[-1]) + 2) if len(idx) else (o["lo"], o["hi"])

    def _splinters(self, real, keep) -> list:
        """A short piece of a burst that came apart at its start (a strong burst's first rows can open a second
        entry next to it) lies inside the other burst's time and frequency: it goes back into it. Two devices are
        never inside each other's frequency range."""
        core = self._core
        out = list(real)
        for b in sorted(real, key=lambda o: o["last"] - o["first"]):
            pk = int(np.argmax(b["e"]))
            host = next((o for o in out + keep if o is not b and o["last"] - o["first"] > b["last"] - b["first"]
                         and o["first"] - self.JOIN_FRAMES <= b["first"] and b["last"] <= o["last"] + self.JOIN_FRAMES
                         and core(o)[0] <= pk <= core(o)[1]), None)
            if host is not None:
                host.update(lo=min(host["lo"], b["lo"]), hi=max(host["hi"], b["hi"]), e=host["e"] + b["e"],
                            peak=max(host["peak"], b["peak"]))
                out.remove(b)
        return out

    TONES_HZ = 200e3                     # an FSK burst's tones lie within this span of each other

    def _one_burst(self, a, b) -> bool:
        """Two bursts that are one FSK transmission's tones: they start and end together (within JOIN_FRAMES) and
        lie within TONES_HZ. Two devices that only overlap in time are not (v4.8.6 joined any pair sharing half the
        shorter one's time: two remotes 120 kHz apart became one burst between them that neither decoder read)."""
        step = self.fs / self.nfft
        if a is b or abs(a["first"] - b["first"]) > self.JOIN_FRAMES or abs(a["last"] - b["last"]) > self.JOIN_FRAMES:
            return False
        (alo, ahi), (blo, bhi) = self._core(a), self._core(b)
        return (max(ahi, bhi) - min(alo, blo) - 4) * step <= self.TONES_HZ

    def _join_tones(self, closing) -> list:
        """An FSK burst shows up as one burst per tone (on the real PC: 433.888 and 433.955 MHz at the same moment,
        each 14 kHz wide). Bursts closing together that are one transmission (_one_burst) are joined, so it is
        decoded once and with both tones."""
        out = []
        for b in sorted(closing, key=lambda o: -o["peak"]):
            for o in out:
                if self._one_burst(o, b):
                    o.update(first=min(o["first"], b["first"]), last=max(o["last"], b["last"]),
                             lo=min(o["lo"], b["lo"]), hi=max(o["hi"], b["hi"]), e=o["e"] + b["e"],
                             peak=max(o["peak"], b["peak"]), tones=True)
                    break
            else:
                out.append(b)
        return out

    def _report(self, b) -> dict:
        e = b["e"]
        sel = e > 0.1 * e.max()
        w = e * sel
        f0 = float(np.sum(self.freqs * w) / max(np.sum(w), 1e-30))
        fs_bins = self.freqs[sel]
        if b.get("tones"):                       # FSK: the middle between its tones, not the stronger one's side
            f0 = float(fs_bins.min() + fs_bins.max()) / 2.0
        # the frames are a running mean of `avg`: the burst started avg-1 frames before its first active frame
        t0 = max(0, b["first"] - (self.avg - 1)) * self.frame_s
        t1 = (b["last"] + 1) * self.frame_s
        return {"t0": round(t0, 5), "t1": round(t1, 5), "offset_hz": round(f0, 1),
                "width_hz": round(float(fs_bins.max() - fs_bins.min()) + self.fs / self.nfft, 1),
                "snr_db": round(10.0 * math.log10(max(b["peak"], 1e-12)), 1)}

    def flush(self) -> list:
        """The bursts still open (the stream is ending)."""
        return self._close(self.frame0 + self.gap + 1, final=True)
