"""ARIS engine, part "band": Band capture: every burst in a band, recorded as it arrives and decoded at its own frequency
(BandWatch on arislib.bandwatch).

Executed into aris.py's namespace by aris._include (see aris.py): names from aris.py's imports and from the
parts before this one are available here without imports, and everything defined here becomes aris.<name>.
"""
from __future__ import annotations

# ----------------------------------------------------------------------------------------------------------------
# BAND CAPTURE: every burst in a band, recorded as it arrives and decoded at its own frequency
# ----------------------------------------------------------------------------------------------------------------
BAND_FS = 2400000            # 2.4 MS/s: about 1.8 MHz clean, the whole 433 MHz band in one capture
BAND_FS_SAFE = 2048000       # after the dongle stalls twice at 2.4 MS/s (some USB links cannot keep up)
BAND_EDGE = 0.75             # part of the capture used (the dongle's filter is flat to about there)
BAND_USABLE_MHZ = 1.6        # slice width for bands wider than one capture (902-928: 17 slices)
BAND_DWELL_S = float(_env("BAND_DWELL_S", "8"))   # a slice of a wide band per visit; 3 x where bursts were heard
BAND_BLOCK_S = 0.25          # IQ handed to the detector at a time
BAND_RING_S = 4.0            # IQ kept to cut bursts out of
BAND_PAD_S = (0.04, 0.03)    # noise kept before and after each burst (rtl_433 measures its level from it)
BAND_QUEUE = 200             # bursts waiting for rtl_433 at most; beyond that a burst is logged undecoded
BAND_BATCH = 16              # bursts decoded by one rtl_433 run (v4.9.2): the cuts waiting in the queue are
                             # joined into one recording, so a busy band costs one process per batch, not per burst
BAND_BATCH_BUF = 65536       # rtl_433 reads a recording in blocks (262144 bytes: 32768 .cf32 samples, or 65536 if
                             # it reads that many floats): every cut is padded with quiet noise to whole blocks of
                             # 65536 samples (a multiple of both), so a package's reported position ("@1.23s", at
                             # worst the block's end) always falls inside its own cut
BAND_BATCH_GAP_S = 0.05      # at least this much quiet between two cuts (rtl_433 closes a package in a gap)
# Measured on a test station (Dongle 1, 433.87, 2026-11-02, three 3-minute runs through the real rtl_433): no burst under
# 12 dB per FFT bin was ever decoded or even fingerprinted (0 of about 4,900), nor anything wider than 150 kHz
# (0 of 1,800: LoRa-like wideband digital signals, 50-350 kHz wide, 40-200 ms long); at the dongle's 33.8 dB gain
# the band gave 36 such bursts a second (6,506 in 3 min, the decode queue overflowed and the log filled), at 19.7 dB
# 3 a second, with the same tyre-sensor decodes (they sit at 28-39 dB per bin). The 0 Hz bins showed no DC spike.
BAND_DECODE_MIN_DB = 12.0    # a burst under this per FFT bin is counted as faint, not sent to rtl_433
BAND_WIDE_HZ = 150e3         # wider than this: a wideband digital signal, counted, not sent to rtl_433
BAND_CELL_TRY = 10           # unread bursts in a row at one frequency: after that only one in BAND_CELL_SKIP is tried
BAND_CELL_SKIP = 10
BAND_MAX_GAIN_DB = 25.0      # a band capture never uses more gain than this
BAND_FAINT_LOG_S = 60.0      # faint and wideband bursts are logged per 10 kHz cell, one line per this many seconds
BAND_HOT_MHZ = (433.92, 433.42, 434.42, 315.0, 868.3, 915.0)   # where devices crowd: never at a capture's 0 Hz


def _band_cutoff_hz(width_hz):
    """Low-pass cutoff for decoding a recorded burst: half its measured width plus room for drift (6 kHz, 15% for
    wide FSK bursts), 8-120 kHz. Measured offline with the real rtl_433 on an EV1527 remote: the whole +-120 kHz
    needs about 28 dB in one FFT bin, +-16.7 kHz about 20 dB, +-10 kHz 18 dB."""
    w = float(width_hz or 0.0)
    return min(120e3, max(8e3, w / 2.0 + max(6e3, 0.15 * w)))


def _band_args(band):
    """'433' / 'ism' / '915' / '902-928' / (lo, hi) -> (lo MHz, hi MHz, label). A known band keeps its name also when
    it comes as a range (the chat console hands the GUI the range)."""
    from arislib import bandwatch
    if isinstance(band, (list, tuple)) and len(band) == 2:
        band = f"{float(band[0])}-{float(band[1])}"
    s = str(band or "").lower()
    m = re.search(r"(\d{3}(?:\.\d+)?)\s*(?:-|to|\.\.)\s*(\d{3,4}(?:\.\d+)?)", s)
    if m:
        lo, hi = sorted((float(m.group(1)), float(m.group(2))))
        label = next((lb for a, b, lb in bandwatch.BANDS.values() if abs(a - lo) < 0.01 and abs(b - hi) < 0.01),
                     f"band {lo:g}-{hi:g} MHz")
    else:
        key = (s if s in bandwatch.BANDS else _plugin_band_key(s) or
               ("915" if re.search(r"9(?:15|02|28|00)", s) else "315" if "315" in s else
                "433" if re.search(r"43[34]|ism", s) else None))
        if key is None:                       # never the 433 band by default ('1090' became one before v4.8.2)
            raise RFError(f"I don't know which band '{band}' is. A band capture covers the 433, 315 or 915 MHz band, "
                          "or a range such as 902-928.")
        lo, hi, label = bandwatch.BANDS[key]
    if not (MIN_MHZ <= lo < hi <= MAX_MHZ):
        raise RFError(f"{lo:g}-{hi:g} MHz is outside the tuner range {MIN_MHZ:g}-{MAX_MHZ:g} MHz.")
    if hi - lo > 60.0:
        raise RFError("A band capture covers up to 60 MHz (it visits it in 1.6 MHz slices).")
    return lo, hi, label


class BandWatch(Watchdog):
    """Band capture ('capture the ISM band'): records the band's IQ continuously, finds every burst in time and
    frequency (arislib.bandwatch.BurstDetector), cuts each one out around its own frequency and decodes it with
    rtl_433 from the recording. Each burst is logged at its own frequency (a decoded packet, codes stripped, or its
    signal's description), so 'check the logs' and the Activity tab list what turned up. Runs until stopped (or
    max_min). A band wider than one capture is visited slice by slice, three times longer where bursts were heard.
    A steady carrier is part of the noise floor here: patrol and investigations are for those."""

    def __init__(self, serial, lo, hi, lock, label=None, max_min=0, **kw):
        import queue
        from arislib import bandwatch
        super().__init__(serial, (float(lo) + float(hi)) / 2.0, lock, mode="continuous", max_min=max_min, **kw)
        self.mode, self.engine = "band", "band"
        self.lo, self.hi = float(lo), float(hi)
        self.label = label or f"{self.lo:g}-{self.hi:g} MHz"
        self.fs = BAND_FS
        self.tunes = self._tunings(self.fs)
        self.dwell = {t: BAND_DWELL_S for t in self.tunes}
        self.dwell_peak = {}
        self.floors = {}
        self.skipped = self.stalls = self.dec_bursts = 0
        self.faint = self.wide = self.sampled = 0      # bursts counted but not sent to rtl_433
        self._q = queue.Queue(maxsize=BAND_QUEUE)
        self._groups = {}                    # (what, first freq) -> {"n", "decoded", "line", "f", "fmin", "fmax"}
        self._devs = {}                      # decoded model -> {"ids", "packets", "times", "fmin", "fmax"}
        self._faint = {}                     # (kind, 10 kHz cell) -> {"n", "t", "b", "snr", "fmin", "fmax"}
        self._cells = {}                     # 10 kHz cell -> [unread in a row, bursts skipped since]
        self._flock = threading.Lock()

    def _tunings(self, fs) -> list:
        """The tunings that cover the band at sample rate fs (one capture, or slices). Worked out again when the
        rate drops to BAND_FS_SAFE: v4.8.6 kept the 2.4 MS/s tuning and no longer heard 433.05-433.10 and
        434.64-434.79 MHz."""
        from arislib import bandwatch
        one = BAND_EDGE * fs / 1e6
        usable = min(BAND_USABLE_MHZ, one - 0.1)      # room for the move away from a crowded frequency
        tunes = ([(self.lo + self.hi) / 2.0] if self.hi - self.lo <= one else
                 bandwatch.slices(self.lo, self.hi, usable))
        # never tune onto a frequency where devices crowd (433.92 is the 433 band's own centre): the bins around
        # the receiver's 0 Hz need a stronger signal (its DC spike)
        return [round(t - 0.05 if any(abs(t - h) < 0.04 for h in BAND_HOT_MHZ) else t, 4) for t in tunes]

    def decoder(self) -> str:
        return "rtl_433 on every recorded burst"

    def summary(self) -> dict:
        s = super().summary()
        s.update(band=[self.lo, self.hi], band_label=self.label, slices=len(self.tunes), sample_rate=self.fs,
                 bursts_not_decoded_queue_full=self.skipped)
        st = str(s.get("status") or "")
        if st.startswith(self.label + ": "):       # shown under the band's own name: not twice
            s["status"] = st[len(self.label) + 2:]
        return s

    # -- recording ------------------------------------------------------------------------------------------------
    def _run_band(self) -> None:
        from arislib.bandwatch import BurstDetector
        dec = threading.Thread(target=_carry_ctx(self._decode_loop), name=f"band-decode-{self.serial}", daemon=True)
        dec.start()
        try:
            first = True
            while not self._done():
                for tune in list(self.tunes):
                    if self._done():
                        break
                    if not first:
                        time.sleep(USB_SETTLE_S)          # the same dongle, opened again (the capture holds it)
                    first = False
                    det = BurstDetector(self.fs, edge=BAND_EDGE)
                    det.floor = self.floors.get((tune, self.fs))
                    heard, ok = self._stream(tune, det, None if len(self.tunes) == 1 else self.dwell[tune])
                    if det.floor is not None:
                        self.floors[(tune, self.fs)] = det.floor
                    if len(self.tunes) > 1:          # 3x after bursts, easing back over quiet visits
                        self.dwell[tune] = (BAND_DWELL_S * 3.0 if heard else
                                            max(BAND_DWELL_S, self.dwell[tune] * 0.67))
                        self.dwell_peak[tune] = max(self.dwell_peak.get(tune, 0.0), self.dwell[tune])
                    if ok:
                        self.stalls = 0                       # stalls count in a row, not over hours
                    else:
                        self.restarts += 1
                        if self.stalls >= 2 and self.fs != BAND_FS_SAFE:
                            self.fs = BAND_FS_SAFE                # this USB link cannot keep up with 2.4 MS/s
                            self.tunes = self._tunings(self.fs)   # the band needs other tunings at this rate
                            self.dwell = {t: BAND_DWELL_S for t in self.tunes}
                            self._set_status("the dongle stalled at 2.4 MS/s: recording at 2.048 MS/s instead"
                                             + (f", in {len(self.tunes)} slices" if len(self.tunes) > 1 else ""))
                        if self.restarts > self.MAX_RESTARTS:
                            return
                        time.sleep(1.0)
                        if tune not in self.tunes:            # the tunings changed: start over with the new ones
                            break
                self._tick_state()
        finally:
            try:
                self._q.put(None, timeout=5)      # (never block for ever on a full queue with no reader)
            except Exception:                      # noqa: BLE001
                pass
            dec.join(60)

    def _stream(self, tune, det, secs):
        """One rtl_sdr run on `tune` (for secs, or until stopped): IQ to the detector, bursts to the decoder.
        Returns (bursts heard, ended normally)."""
        import select
        try:
            p = subprocess.Popen(_radio_cmd(self._rtl_sdr_cmd(tune)), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as e:
            self.last_error = f"Cannot start rtl_sdr: {e}"
            return 0, False
        self.proc = p
        err = []
        th = threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True)
        th.start()
        blk = 2 * int(self.fs * BAND_BLOCK_S)
        ring, pos, buf = deque(), 0, bytearray()
        t0 = last = time.time()
        heard, ok, fd = 0, True, p.stdout.fileno()
        try:
            while not self._done():
                now = time.time()
                if secs and now - t0 >= secs:
                    break
                r, _w, _x = select.select([fd], [], [], 0.5)
                if r:
                    chunk = os.read(fd, 1 << 20)
                    if not chunk:
                        ok = False
                        self.last_error = "rtl_sdr ended: " + (_rtl_err_tail(err[0].decode(errors="replace"))
                                                               if err and err[0] else "no message")
                        break
                    buf += chunk
                    last = now
                elif now - last > (5.0 if pos or buf else SDR_START_S):   # (it sent some, then stopped)
                    ok = False
                    self.stalls = self.stalls + 1 if now - t0 < 60.0 else 1    # after a long good run: the first
                    self.last_error = f"the dongle stopped sending samples at {self.fs / 1e6:g} MS/s"
                    break
                while len(buf) >= blk:
                    raw = bytes(buf[:blk])
                    del buf[:blk]
                    ring.append((pos, raw))
                    pos += blk // 2
                    while ring and ring[0][0] + len(ring[0][1]) // 2 < pos - BAND_RING_S * self.fs:
                        ring.popleft()
                    for b in det.feed(_raw_to_iq(raw)):
                        heard += 1
                        self._take(tune, b, ring)
                self._tick_state()
        finally:
            _end_radio(p, drain=True)             # Ctrl+C while its pipe is read; gone before the next tuning
            th.join(2)
            _close_pipes(p, th)
            self.proc = None
        return heard + self._end_visit(tune, det, buf, ring, pos), ok

    def _end_visit(self, tune, det, buf, ring, pos) -> int:
        """The end of a visit: the whole frames still waiting go to the detector, then the bursts still open go to
        the decoder (v4.8.6 dropped both: a burst in the last 30 ms of each visit of a sliced band was never logged).
        Returns how many bursts that gave."""
        n, whole = 0, len(buf) - len(buf) % (2 * det.nfft)
        if whole:
            raw = bytes(buf[:whole])
            ring.append((pos, raw))
            for b in det.feed(_raw_to_iq(raw)):
                n += 1
                self._take(tune, b, ring)
        if ring:
            for b in det.flush():
                n += 1
                self._take(tune, b, ring)
        return n

    def _rtl_sdr_cmd(self, tune) -> list:
        """rtl_sdr for one tuning. The gain is the dongle's own for the band, capped at BAND_MAX_GAIN_DB: at the
        33.8 dB /setup left Dongle 1 with, the 433 band is a flood of faint wideband signals (36 bursts a second
        on a test station, nothing decodable among them) and the real devices decode just as well at 20-25 dB."""
        gain = min(float(_gain_for(self.serial, self.lo, self.hi)), BAND_MAX_GAIN_DB)
        return (["rtl_sdr", "-d", self.serial, "-f", str(int(round(tune * 1e6))), "-s", str(self.fs), "-g",
                 f"{gain:g}"] + _ppm_args(self.serial) + ["-"])

    def _take(self, tune, b, ring) -> None:
        """A burst that just ended: its stretch of IQ (with a little noise either side) goes to the decoder. Not
        every burst: a faint one (under BAND_DECODE_MIN_DB per bin) or a wideband one (over BAND_WIDE_HZ) is
        counted and logged in bulk instead (rtl_433 never read one on a test station), and a frequency where burst after
        burst goes unread gets only one in BAND_CELL_SKIP tried until one reads."""
        f = round(tune + b["offset_hz"] / 1e6, 3)
        if b["width_hz"] > BAND_WIDE_HZ:
            self.wide += 1
            return self._faint_burst(f, b, "wide")
        if b["snr_db"] < BAND_DECODE_MIN_DB:
            self.faint += 1
            return self._faint_burst(f, b, "faint")
        with self._flock:
            cell = self._cells.setdefault(round(f, 2), [0, 0])
            skip = cell[0] >= BAND_CELL_TRY
            if skip:
                cell[1] += 1
                skip = bool(cell[1] % BAND_CELL_SKIP)
        if skip:
            self.sampled += 1
            return self._faint_burst(f, b, "sampled")
        s0 = max(int((b["t0"] - BAND_PAD_S[0]) * self.fs), ring[0][0])
        s1 = int((b["t1"] + BAND_PAD_S[1]) * self.fs)
        parts = []
        for start, raw in ring:
            n = len(raw) // 2
            a, z = max(s0, start), min(s1, start + n)
            if a < z:
                parts.append(raw[2 * (a - start):2 * (z - start)])
        item = (tune, b, b"".join(parts), self.fs)
        try:
            self._q.put_nowait(item)
        except Exception:                            # rtl_433 cannot keep up: keep the burst's measurement
            self.skipped += 1
            self._log_unread(tune, b, "not decoded (too many bursts at once)")

    # -- decoding ---------------------------------------------------------------------------------------------------
    def _decode_loop(self) -> None:
        """Takes the bursts waiting in the queue in batches of up to BAND_BATCH (one rtl_433 run each; v4.8.8 ran
        one per burst: 37 bursts a second on the live station, 622 skipped with the queue full)."""
        import queue
        td = tempfile.mkdtemp(prefix=_tmp_owned_prefix("band"), dir=_AUDIO_TMP)   # (memory; swept if we die)
        try:
            while True:
                item = self._q.get()
                if item is None:
                    return
                batch, done = [item], False
                while len(batch) < BAND_BATCH:
                    try:
                        nxt = self._q.get_nowait()
                    except queue.Empty:
                        break
                    if nxt is None:
                        done = True
                        break
                    if nxt[3] != batch[0][3]:          # a cut at another sample rate starts the next batch
                        self._safe_batch(td, batch)
                        batch = []
                    batch.append(nxt)
                self._safe_batch(td, batch)
                if done:
                    return
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def _safe_batch(self, td, batch) -> None:
        """One batch; an exception never ends the decode thread (the capture would then fill its queue and log
        every later burst as undecoded)."""
        try:
            self._decode_batch(td, batch)
        except Exception:
            log.error("band capture: decoding %d bursts failed:\n%s", len(batch), traceback.format_exc())

    def _prepare(self, tune, b, raw, fs):
        """A burst's IQ -> (f, y, rate): mixed to 0 Hz, decimated 8x, filtered to its own width and scaled to half
        of full scale. (f, None, why) when it cannot be decoded."""
        f = round(tune + b["offset_hz"] / 1e6, 3)
        if len(raw) < 2 * int(0.002 * fs):
            return f, None, "too short to decode"
        pre = int((b["t0"] - max(0.0, b["t0"] - BAND_PAD_S[0])) * fs * 0.8)     # the noise before the burst
        z = _raw_to_iq(raw, dc_from=pre if pre >= 2000 else None)
        y = _mix_filter_decimate(z, fs, b["offset_hz"], 120e3, 8, taps=129)
        rate = int(round(fs / 8))
        # then down to the burst's own width: a 9 kHz remote in the whole 240 kHz kept only 2-3 dB over the noise
        # and rtl_433 found no pulses (real PC, a remote at 17 dB in one FFT bin); FSK tones are joined by then
        cut = _band_cutoff_hz(b["width_hz"])
        if cut < 120e3:
            y = _mix_filter_decimate(y, rate, 0.0, cut, 1, taps=129)
        # filtered to its own channel the burst is far above the noise but small in absolute terms: scaled to half
        # of full scale and written as floats (.cf32), so rtl_433's level detection sees it as it is
        y = y * (0.5 / max(float(np.max(np.abs(y))), 1e-6))
        return f, y, rate

    def _decode_batch(self, td, batch) -> None:
        """One rtl_433 run over the cuts of `batch` (all at one sample rate), each result back to its own burst."""
        cuts = []
        for tune, b, raw, fs in batch:
            f, y, rate = self._prepare(tune, b, raw, fs)
            if y is None:
                self._log_unread(tune, b, rate)
            else:
                cuts.append((tune, b, f, y, rate))
        if not cuts:
            return
        if len(cuts) > 1 and not _batch_decoding_works():
            for c in cuts:                          # this rtl_433 does not report positions: one run per burst
                self._decode_cuts(td, [c])
            return
        self._decode_cuts(td, cuts)

    def _decode_cuts(self, td, cuts) -> None:
        rate, f0 = cuts[0][4], cuts[0][2]
        path = os.path.join(td, f"aris_burst_{f0:.3f}M_{rate // 1000}k.cf32")
        try:
            results, err = _rtl433_cuts(path, [c[3] for c in cuts], rate)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        if err:
            self.last_error = f"rtl_433 on a recorded burst: {err}"
        for (tune, b, f, _y, _r), (pk, fps) in zip(cuts, results):
            try:
                self._result(tune, b, f, pk, fps)
            except Exception:
                log.error("band capture: a burst's result failed:\n%s", traceback.format_exc())

    def _result(self, tune, b, f, pk, fps) -> None:
        """What rtl_433 made of one burst: packets (codes stripped), a fingerprint, or nothing."""
        now = time.time()
        pk, twins = _one_reading(pk)
        self._cell_result(f, bool(pk or fps))
        if pk:
            self.dec_bursts += 1
            for x in pk:
                # rtl_433 -r gives the time inside the file ("@0.04s"): the clock time is ours
                x = dict(x, freq=f, time=_now())
                _log_append({**_safe_packet(x), "type": "packet", "t": round(now, 2), "aris_serial": self.serial,
                             "aris_freq_mhz": f, "aris_band": self.label,
                             **({"also_read_as": twins[str(x.get("model"))]} if twins.get(str(x.get("model"))) else {})})
                self.packets += 1
                if len(self._pk) < 2000:
                    self._pk.append(x)
            for m in sorted({str(x.get("model")) for x in pk}):      # this burst, its repeats counted as packets
                mine = [_safe_packet(x) for x in pk if str(x.get("model")) == m]
                self._device(m, f, now, [x.get("id") for x in mine], len(mine), _device_info(mine[0])["type"])
        elif fps:
            fp = fps[0]
            _log_append({"type": "burst", "t": round(now, 2), "time": _now(), "modulation": fp["modulation"],
                         "pattern": fp.get("pattern"), "coding": fp.get("coding"),
                         "demodulated": fp.get("demodulated"), "short_pulse_us": fp.get("short_pulse_us"),
                         "long_pulse_us": fp.get("long_pulse_us"), "bits_per_packet": fp.get("bits_per_packet"),
                         "packets_per_burst": fp.get("packets_per_burst"), "snr_db": fp.get("snr_db") or b["snr_db"],
                         "burst_ms": round((b["t1"] - b["t0"]) * 1000.0, 1), "width_khz": round(b["width_hz"] / 1e3, 1),
                         "aris_serial": self.serial, "aris_freq_mhz": f, "aris_band": self.label})
            self.bursts += 1
            key = ("burst", fp["modulation"], fp.get("coding"), round((fp.get("short_pulse_us") or 0) / 50.0),
                   round((fp.get("long_pulse_us") or 0) / 50.0), fp.get("bits_per_packet"))
            self._group(key, f, False, _fp_line(dict(fp, snr_db=None)))
        else:
            self._log_unread(tune, b, "not read by rtl_433 (no clean on-off or frequency-shift pulses, or too weak)")
        self.status = (f"{self.label}: {self.dec_bursts + self.bursts} bursts recorded, {self.packets} decoded "
                       f"packets" + (f", now on {tune:g} MHz" if len(self.tunes) > 1 else ""))
        self._tick_state()

    FAINT_WHY = {"faint": "faint (under 12 dB): too weak for rtl_433, counted only",
                 "wide": "wideband digital signal (LoRa-like), not something rtl_433 can read: counted only",
                 "sampled": "not read by rtl_433 again and again at this frequency: only one in 10 is tried now"}

    def _faint_burst(self, f, b, kind) -> None:
        """A burst counted without decoding: gathered per 10 kHz cell and logged as one line with its count every
        BAND_FAINT_LOG_S (v4.8.7 logged each one: 36 lines a second at the dongle's full gain)."""
        key = (kind, round(f, 2))
        now = time.time()
        with self._flock:
            g = self._faint.get(key)
            if g is not None and now - g["t"] >= BAND_FAINT_LOG_S:
                self._faint.pop(key)
                self._write_faint(key, g)
                g = None
            if g is None:
                g = self._faint[key] = {"n": 0, "t": now, "b": b, "snr": b["snr_db"], "fmin": f, "fmax": f}
            g["n"] += 1
            if b["snr_db"] > g["snr"]:
                g["snr"], g["b"] = b["snr_db"], b
            g["fmin"], g["fmax"] = min(g["fmin"], f), max(g["fmax"], f)

    def _write_faint(self, key, g) -> None:
        kind, _cell = key
        b = dict(g["b"], snr_db=g["snr"])
        self._log_unread(None, b, self.FAINT_WHY[kind], count=g["n"], f=round((g["fmin"] + g["fmax"]) / 2.0, 3),
                         kind=kind)

    def _flush_faint(self) -> None:
        """The faint, wideband and sampled bursts gathered so far go to the log (the capture is ending)."""
        with self._flock:
            items, self._faint = list(self._faint.items()), {}
        for key, g in items:
            self._write_faint(key, g)

    def _cell_result(self, f, read) -> None:
        """rtl_433's result for a burst at f: a frequency where bursts go unread in a row is sampled (BAND_CELL_TRY),
        and one that reads again is tried every time again."""
        with self._flock:
            cell = self._cells.setdefault(round(f, 2), [0, 0])
            if read:
                cell[0] = cell[1] = 0
            else:
                cell[0] += 1

    def _log_unread(self, tune, b, why, count=1, f=None, kind=None) -> None:
        f = round(tune + b["offset_hz"] / 1e6, 3) if f is None else f
        _log_append({"type": "burst", "t": round(time.time(), 2), "time": _now(), "modulation": "signal",
                     "pattern": why, "demodulated": False, "snr_db": b["snr_db"],
                     "burst_ms": round((b["t1"] - b["t0"]) * 1000.0, 1), "width_khz": round(b["width_hz"] / 1e3, 1),
                     "aris_serial": self.serial, "aris_freq_mhz": f, "aris_band": self.label,
                     **({"count": int(count)} if count != 1 else {}), **({"kind": kind} if kind else {})})
        self.bursts += int(count)
        self._group(("unread", why), f, False, why, n=int(count), kind=kind)

    def _group(self, what, f, decoded, line, packets=0, n=1, kind=None) -> None:
        """An undecoded burst: with the others of the same kind within 15 kHz of the group's first one (v4.8.6 used
        fixed 10 kHz cells: one signal jittering over a cell edge was two)."""
        g = next((g for (w, _f0), g in self._groups.items() if w == what and abs(g["f"] - f) <= 0.015), None)
        if g is None:
            g = self._groups[(what, round(f, 3))] = {"n": 0, "packets": 0, "decoded": decoded, "line": line,
                                                     "f": round(f, 3), "fmin": f, "fmax": f, "kind": kind}
        g["n"] += n
        g["packets"] += packets
        g["fmin"], g["fmax"] = min(g["fmin"], f), max(g["fmax"], f)

    def _device(self, model, f, t, ids, packets, kind=None) -> None:
        """A decoded burst of `model`: its IDs (codes already stripped), packets, time and frequency. The result
        counts transmissions from the burst times and tells sensors apart by ID (v4.8.6 called 4 Renault tyre
        sensors one 'Renault, 6 transmissions', each repeat counted as a transmission)."""
        d = self._devs.setdefault(model, {"ids": Counter(), "packets": 0, "times": [], "fmin": f, "fmax": f,
                                          "kind": kind, "id_times": {}})
        d["kind"] = d.get("kind") or kind
        for i in ids:
            if i not in (None, ""):
                d["ids"][str(i)] += 1
                d["id_times"].setdefault(str(i), []).append(t)
        d["packets"] += packets
        d["times"].append(t)
        d["fmin"], d["fmax"] = min(d["fmin"], f), max(d["fmax"], f)

    def _finish(self, outcome, fp=None, verdict=None, **extra) -> None:
        self._flush_faint()
        if self.result is None and verdict is None:
            verdict = self._band_verdict()
            if outcome == "error":
                verdict = f"The recording failed: {self.last_error or 'the dongle stopped working'}. " + verdict
        super()._finish(outcome, fp=fp, verdict=verdict, band=[self.lo, self.hi], band_label=self.label,
                        devices=_devices_from_packets(self._pk, heard_by=_short(self.serial))[:12], **extra)

    def _band_verdict(self) -> str:
        """What the band capture got, in words: bursts (not packets), decoded models with their devices, packets and
        transmissions, and the undecoded ones with each reason once."""
        unk = [g for g in self._groups.values() if not g["decoded"] and not g.get("kind")]
        unk.sort(key=lambda g: -g["n"])
        n = self.dec_bursts + self.bursts           # bursts, not packets: one press is one burst of 6 repeats
        frange = lambda a, b: f"{a:.3f}" if b - a < 0.0015 else f"{a:.3f}-{b:.3f}"
        counted = {}                                # faint / wide / sampled: counted, not decoded
        for g in self._groups.values():
            if g.get("kind"):
                c = counted.setdefault(g["kind"], [0, g["fmin"], g["fmax"]])
                c[0] += g["n"]
                c[1], c[2] = min(c[1], g["fmin"]), max(c[2], g["fmax"])
        # (neither the band nor the time: the notice, the stop reply and the Activity tab say those once)
        parts = [f"{_plural(n, 'burst')} recorded" if n else "Nothing transmitted in the band while it listened"]
        devs = sorted(self._devs.items(), key=lambda kv: -kv[1]["packets"])
        if devs:
            # known devices (the history's rule or the user's mark) fold into one clause, and tyre sensors of
            # passing cars into one by make; the rest are listed one model at a time as before
            heard = [{"model": m, "id": i, "kind": d.get("kind"), "packets": d["ids"][i],
                      "transmissions": len(_transmission_starts(d["id_times"].get(i) or d["times"]))}
                     for m, d in devs for i in (list(d["ids"]) or [None])]
            known_line, passing_line, rest = _known_fold(heard)
            left = {}
            for h in rest:
                left.setdefault(h["model"], set()).add(h["id"])
            lines = []
            for m, d in devs:
                if m not in left:
                    continue
                ids = [i for i in d["ids"] if i in left[m]] or list(d["ids"])
                k = len(ids)
                who = (f"ID {ids[0]}; " if k == 1 else f"{k} different devices; " if k > 1 else "")
                tx = len(_transmission_starts(sum((d["id_times"].get(i) or [] for i in ids), []) or d["times"]))
                pk_n = sum(d["ids"][i] for i in ids) or d["packets"]
                lines.append(f"{m} at {frange(d['fmin'], d['fmax'])} MHz ({who}{_plural(pk_n, 'packet')} "
                             f"in {_plural(tx, 'transmission')})")
            if lines:
                parts.append("Decoded: " + "; ".join(lines[:5]) + (f"; and {len(lines) - 5} more" if len(lines) > 5 else ""))
            if passing_line:
                parts.append(passing_line)
            if known_line:
                parts.append(known_line)
        if unk:
            why, hints = {}, []                     # each reason once, with the frequencies that gave it
            for g in unk:
                # fingerprints of one kind differ only in their bit count and repeat the pattern note: one entry,
                # the bit counts as a range, the note once at the end (live, v4.8.8: three near-identical lines)
                line, bits = g["line"], re.findall(r"(\d+)-bit packets", g["line"])
                hint = re.search(r" \((this pattern is used by[^)]*)\)", line)
                if hint and hint.group(1) not in hints:
                    hints.append(hint.group(1))
                plain = re.sub(r" \(this pattern is used by[^)]*\)", "", line)
                key = re.sub(r"\d+-bit packets", "N-bit packets", plain)
                e = why.setdefault(key, {"gs": [], "bits": set(), "line": plain})
                e["gs"].append(g)
                e["bits"].update(int(b) for b in bits)
            txt = []
            for e in list(why.values())[:4]:
                gs, b = e["gs"], sorted(e["bits"])
                at = ", ".join(frange(g["fmin"], g["fmax"]) for g in gs[:3]) + " MHz" + (
                    f" (and {len(gs) - 3} more frequencies)" if len(gs) > 3 else "")
                line = (re.sub(r"\d+-bit packets", f"{b[0]}-{b[-1]}-bit packets", e["line"]) if len(b) > 1
                        else e["line"])
                txt.append(f"{at}, {line} ({_plural(sum(g['n'] for g in gs), 'burst')})")
            parts.append("Not decoded: " + "; ".join(txt)
                         + (f"; and {_plural(len(why) - 4, 'more kind')}" if len(why) > 4 else "")
                         + ("".join(f". {h[0].upper() + h[1:]}" for h in hints) if hints else ""))
        words = {"faint": "faint (under 12 dB per bin, too weak for rtl_433)",
                 "wide": "wideband digital signals (LoRa-like, which rtl_433 cannot read)",
                 "sampled": "at frequencies that went unread again and again (one in 10 tried)"}
        cnt = [f"{c[0]} {words[k]} at {frange(c[1], c[2])} MHz" for k, c in counted.items()]
        if cnt:
            parts.append("Counted but not decoded: " + "; ".join(cnt))
        if self.skipped:
            k = self.skipped
            parts.append(f"{k} of them came too fast to decode and {'was' if k == 1 else 'were'} logged with "
                         f"{'its' if k == 1 else 'their'} measurements only")
        return ". ".join(parts)

    def _notice_text(self, outcome, dur, verdict, saved) -> str:
        """'The 433 MHz band capture (433.05-434.79) on Dongle 1 finished after 2 min 2 s: 96 bursts recorded. ...'
        (v4.8.0: "Watchdog on 433.92 MHz (Dongle 1) finished after 2 min 2 s. Recorded 96 bursts in the 433 MHz band
        (433.05-434.79) over 2 min 2 s.")"""
        d = _short(self.serial)
        head = f"The {_band_capture_name(self.label)} on {d}"
        if outcome == "error":
            return f"{head} stopped after {_took(dur)}. {_sentence(verdict)} {d} is free again."
        return f"{head} finished after {_took(dur)}: {_sentence(_lc_first(verdict))} {d} is free again."


def _cut_layout(ys, rate):
    """Where each cut sits in the joined recording: [(start_s, end_s)], every cut padded to whole rtl_433 blocks
    and at least BAND_BATCH_GAP_S apart (see BAND_BATCH_BUF)."""
    out, pos = [], 0
    for y in ys:
        n = len(y) + int(BAND_BATCH_GAP_S * rate)
        n = -(-n // BAND_BATCH_BUF) * BAND_BATCH_BUF
        out.append((pos / rate, (pos + n) / rate))
        pos += n
    return out


def _rtl433_cuts(path, ys, rate):
    """rtl_433 once over the cuts `ys` (complex64 at `rate`) written to `path` as one .cf32 recording, each cut
    padded to whole buffers. Returns ([(packets, fingerprints)] per cut, error text or None): every package is
    given to the cut its reported position falls in (a decoded packet's "@1.23s" time, the analyzer's "Detected
    OOK package" position), and one without a position to the cut of the package before it."""
    layout = _cut_layout(ys, rate)
    total = int(round(layout[-1][1] * rate))
    iq = np.zeros(2 * total, dtype=np.float32)
    rng = np.random.default_rng(1)
    for y, (s0, e0) in zip(ys, layout):
        i, j = int(round(s0 * rate)), int(round(e0 * rate))
        iq[2 * i:2 * (i + len(y)):2], iq[2 * i + 1:2 * (i + len(y)):2] = y.real, y.imag
        # the padding after the cut is noise at the level of the cut's own noise before the burst (its first
        # samples), not digital silence: rtl_433's level estimate then sees one continuous recording
        k, n = i + len(y), j - (i + len(y))
        if n > 0:
            head = y[:min(len(y), 2000)]
            sd = float(np.sqrt(np.mean(np.abs(head) ** 2) / 2.0)) if len(head) else 0.0
            sd = min(max(sd, 1e-4), 0.05)
            iq[2 * k:2 * j:2] = (sd * rng.standard_normal(n)).astype(np.float32)
            iq[2 * k + 1:2 * j:2] = (sd * rng.standard_normal(n)).astype(np.float32)
    iq.tofile(path)
    err = None
    try:
        p = subprocess.run(["rtl_433", "-r", path, "-A", "-F", "json", "-M", "level", "-M", "protocol"]
                           + _rtl433_extra_flags(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, timeout=30 + 5 * len(ys))
        out = p.stdout or ""
    except (OSError, subprocess.TimeoutExpired) as e:
        out, err = "", str(e)
    per = _assign_cuts(_parse_rtl433_timed(out), layout)
    return [(pk, _fingerprints(pkgs)) for pk, pkgs in per], err


def _assign_cuts(items, layout) -> list:
    """[(position_s, kind, obj)] in order -> [(packets, packages)] per cut: a position goes to the cut whose span
    holds it (a position reported at a buffer's end is that cut's own last buffer end, so the span is
    (start, end]); a package without a position follows the one before it."""
    per = [([], []) for _ in layout]
    cur = 0
    for t, kind, obj in items:
        if t is not None:
            cur = next((i for i, (s0, e0) in enumerate(layout) if s0 < t <= e0),
                       0 if t <= layout[0][0] else len(layout) - 1)
        per[cur][0 if kind == "packet" else 1].append(obj)
    return per


_BATCH_OK = {"ok": None}


def _batch_decoding_works() -> bool:
    """Once per process: does this rtl_433 report where in a recording each package sat? Four synthetic cuts (the
    self-test's remote, its unknown sensor, the remote again placed so that it ends in its cut's last block, the
    sensor again) go through _rtl433_cuts; batches are used only if the remote comes back in cuts 1 and 3 only
    and the unknown sensor in cuts 2 and 4 only. Otherwise every burst gets its own run, as before v4.9.2."""
    if _BATCH_OK["ok"] is None:
        ok = False
        try:
            fs = 300000
            remote, unknown = _st_env(_st_remote_seq(), fs), _st_env(_st_unknown_seq(), fs)
            late = _st_env([(0, 120000)] + _st_remote_seq(), fs)      # the packets sit in the cut's last block
            ys = [remote, unknown, late, unknown]
            with tempfile.TemporaryDirectory(prefix="aris_batchprobe_") as td:
                res, err = _rtl433_cuts(os.path.join(td, "aris_burst_433.920M_300k.cf32"), ys, fs)
            rem = [any(x.get("model") == "Generic-Remote" for x in pk) for pk, _ in res]
            unk = [any(f.get("bits_per_packet") == 28 for f in fp) for _, fp in res]
            ok = err is None and rem == [True, False, True, False] and unk == [False, True, False, True]
        except Exception as e:                     # noqa: BLE001
            log.warning("batch decoding probe failed: %s", e)
        _BATCH_OK["ok"] = ok
        log.info("band capture: rtl_433 %s", "decodes bursts in batches" if ok
                 else "runs once per burst (it did not report package positions in a joined recording)")
    return _BATCH_OK["ok"]


_CONTINUOUS_RX = re.compile(r"\b(continuous\w*|keep\s+(?:logging|recording|watching)|log\s+every\w*|until\s+i\s+stop|"
                            r"all\s+(?:day|night)|24/7|non-?stop|(?:don'?t|do\s+not|never)\s+stop"
                            r"(?!\s+(?:until|when|once|after|if|before)\b))\b")


def _one_reading(pk):
    """The packets rtl_433 decoded from one recorded burst, with a second decoder's reading of the same bits
    dropped: two models whose IDs are the same digits shifted by one ('Citroen 5b2c7e14' and 'Truck 75b2c7e1' on the
    PC) are one device read twice, so the first is kept. Returns (packets, {kept model: the other model})."""
    models = list(dict.fromkeys(str(x.get("model")) for x in pk))
    if len(models) < 2:
        return pk, {}
    ids = {m: {str(x.get("id")).lower() for x in pk if str(x.get("model")) == m and x.get("id") is not None}
           for m in models}
    shifted = lambda a, b: len(a) == len(b) >= 6 and (a[1:] == b[:-1] or b[1:] == a[:-1])
    drop, twins = set(), {}
    for i, m in enumerate(models):
        for o in models[i + 1:]:
            if m not in drop and o not in drop and any(shifted(a, b) for a in ids[m] for b in ids[o]):
                drop.add(o)
                twins[m] = o
    return [x for x in pk if str(x.get("model")) not in drop], twins


def _band_of_freq(freq_mhz):
    """The band capture's band ('433' / '315' / '915') a frequency lies in, else None. mode='band' with a frequency
    outside them (the model copying 'mode: band' to a 1090 capture) is an ordinary capture."""
    from arislib import bandwatch
    try:
        f = _to_mhz(freq_mhz)
    except Exception:
        return None
    return next((k for k, (lo, hi, _lb) in bandwatch.BANDS.items() if lo <= f <= hi), None)


@register(
    "start_background_watchdog",
    "Capture a signal in the background: locks the dongle that hears it best and runs the band's decoder until a "
    "packet is decoded (device identified) or demodulated (unknown device, described); then reports in the chat, "
    "saves it to Signals and frees the dongle. For 'capture / monitor / watch / listen until'. mode='continuous' "
    "logs every packet until stopped ('keep logging').",
    {"freq_mhz": {"type": "number", "description": "Frequency in MHz (e.g. 433.92 or 908.74)"},
     "dongle": {"type": "string", "description": "A dongle number, only if the user names one"},
     "mode": {"type": "string", "description": "'capture' (default) or 'continuous'"},
     "max_minutes": {"type": "integer", "description": "Give up after this many minutes (default 60)"},
     "band": {"type": "string", "description": "A whole band instead of one frequency ('capture the ISM band'): "
              "'433' (433.05-434.79), '915' (902-928), '315', or 'low-high' in MHz. It records the band and "
              "decodes every burst as it arrives, until stopped"}},
    aliases=("capture_signal", "start_capture", "monitor_frequency"),
)
def start_background_watchdog(freq_mhz=None, dongle=None, mode=None, max_minutes=None, band=None):
    if band not in (None, "") or (str(mode or "").lower() == "band" and freq_mhz not in (None, "")
                                   and _band_of_freq(freq_mhz)):
        return _start_band_watch(band if band not in (None, "") else _band_of_freq(freq_mhz), dongle, max_minutes)
    f = round(_to_mhz(freq_mhz), 5) if freq_mhz not in (None, "") else 433.92
    if not MIN_MHZ <= f <= MAX_MHZ:
        raise RFError(f"{f:g} MHz is outside the tuner range {MIN_MHZ:g}-{MAX_MHZ:g} MHz.")
    mode = "continuous" if str(mode or "").lower().startswith(("cont", "log", "keep", "all")) else "capture"
    mins = _int(max_minutes, "max_minutes", CAPTURE_DEFAULT_MIN if mode == "capture" else 0, 0, CAPTURE_MAX_MIN)
    if mode == "capture" and not mins:
        mins = CAPTURE_DEFAULT_MIN
    kind = _band_kind(f)
    if mode == "continuous" and kind not in ("sensor", "other"):
        mode, mins = "capture", mins or CAPTURE_DEFAULT_MIN       # continuous logging is an rtl_433 feature
    if (kind in ("sensor", "other") or mode == "continuous") and shutil.which("rtl_433") is None:
        raise RFError("rtl_433 not found in PATH.")
    r = _gui_call("/api/internal/watchdog", {"op": "start", "args": {"freq_mhz": f, "dongle": dongle, "mode": mode,
                                                                    "max_minutes": mins}})
    if r is not None:                                    # the GUI process runs it
        if r.get("status") == "error":
            raise RFError(r.get("error", "the GUI could not start the capture"))
        r.pop("status", None)
        return r
    for w in _running_watchdogs():
        if abs(float(w.get("freq_mhz") or 0) - f) < 0.005:
            what = (f"Already recording {f:g} MHz with the rest of the {w.get('band_label')} on {w['dongle']}"
                    if w.get("mode") == "band" else
                    f"Already logging {f:g} MHz continuously on {w['dongle']}" if w.get("mode") == "continuous" else
                    f"Already capturing {f:g} MHz on {w['dongle']}")
            return {"message": what + (f": {w['status']}" if w.get("status") else ""),
                    **{k: w.get(k) for k in ("dongle", "freq_mhz", "mode", "since")}}
    serial, lock, why = _claim_best_dongle(f, dongle)
    wd = Watchdog(serial, f, lock, mode=mode, max_min=mins)
    _WATCHDOGS[serial] = wd
    wd.start()
    wd.thread.join(1.5)      # catch instant failures (busy / wrong serial) without waiting longer than needed
    if not wd.alive() and wd.result and wd.result.get("outcome") == "error":
        raise RFError(f"The capture could not start on {_short(serial)}: {wd.last_error}")
    _write_watchdog_state()
    d = _short(serial)
    if wd.result:
        return {"message": f"Done within seconds on {d}. {wd.result['verdict']}.", "dongle": d, "freq_mhz": f,
                "result": wd.result}
    others = _others_working(serial)
    if mode == "capture":
        saves = wd.engine in ("rtl_433", "audio", "air")           # broadcasts (FM, ADS-B) are not saved to Signals
        msg = (f"Capturing {f:g} MHz in the background on {d} ({why}), with {wd.decoder_phrase()}. It keeps "
               f"listening {wd.until_phrase()}, for up to {int(mins)} min; the result will appear here in the chat "
               f"and in the Activity tab" + (", and is saved to Signals" if saves else "") + f". {d} is locked for "
               f"this" + (f"; {others}." if others else "."))
    else:
        msg = (f"Logging {f:g} MHz continuously on {d} ({why}): every packet rtl_433 decodes and every burst it "
               f"cannot, " + (f"for {int(mins)} min" if mins else "until you stop it") + ". 'check the logs' shows "
               f"what it heard. {d} stays busy" + (f"; {others}." if others else "."))
    return {"message": msg, "dongle": d, "freq_mhz": f, "mode": mode, "max_minutes": mins, "decoder": wd.decoder(),
            "why_this_dongle": why}


def _others_working(serial) -> str:
    """'the other dongle keeps working' / 'the other dongles keep working' by how many others there are ('' for
    none). v4.8.0 always said 'the other dongles' for a capture and 'the other dongle' for a band capture."""
    cfg = _load_config()
    n = sum(1 for s in DONGLE_NAMES if s != str(serial) and _dongle_cfg(s, cfg).get("present") is not False
            and _dongle_cfg(s, cfg).get("usb_ok") is not False)
    return "" if not n else "the other dongle keeps working" if n == 1 else "the other dongles keep working"


def _start_band_watch(band, dongle=None, max_minutes=None):
    lo, hi, label = _band_args(band)
    mins = _int(max_minutes, "max_minutes", 0, 0, CAPTURE_MAX_MIN)
    if shutil.which("rtl_433") is None:
        raise RFError("rtl_433 not found in PATH (it decodes the bursts).")
    r = _gui_call("/api/internal/watchdog", {"op": "start", "args": {"band": f"{lo:g}-{hi:g}", "dongle": dongle,
                                                                    "max_minutes": mins}})
    if r is not None:                                    # the GUI process runs it
        if r.get("status") == "error":
            raise RFError(r.get("error", "the GUI could not start the band capture"))
        r.pop("status", None)
        return r
    for w in _running_watchdogs():
        b = w.get("band")
        if w.get("mode") == "band" and b and b[0] < hi and b[1] > lo:
            return {"message": f"Already recording the {w.get('band_label')} on {w['dongle']}"
                               + (f": {w['status']}" if w.get("status") else ""),
                    "dongle": w["dongle"], "band": b}
    serial, lock, why = _claim_best_dongle((lo + hi) / 2.0, dongle)
    wd = BandWatch(serial, lo, hi, lock, label=label, max_min=mins)
    _WATCHDOGS[serial] = wd
    wd.start()
    wd.thread.join(1.5)
    if not wd.alive() and wd.result and wd.result.get("outcome") == "error":
        raise RFError(f"The band capture could not start on {_short(serial)}: {wd.last_error}")
    _write_watchdog_state()
    d = _short(serial)
    how = ("the whole band in one go" if len(wd.tunes) == 1 else
           f"{len(wd.tunes)} slices of {BAND_USABLE_MHZ:g} MHz in turn, staying longer where it hears bursts")
    others = _others_working(serial)
    return {"message": f"Recording the {wd.label} on {d} ({why}): {how}. Every burst is recorded as it arrives "
                       f"and decoded at its own frequency with rtl_433, "
                       + (f"for {int(mins)} min" if mins else "until you stop it")
                       + ". 'check the logs' lists what turned up and the Activity tab shows it as it happens. "
                       f"{d} stays busy" + (f"; {others}." if others else "."),
            "dongle": d, "band": [lo, hi], "band_label": wd.label, "mode": "band", "max_minutes": mins}


@register("stop_background_watchdog", "Stop background captures / watchdogs (all of them, or the one on a dongle or "
          "frequency).",
          {"dongle": {"type": "string", "description": "A dongle number (default: every running one)"},
           "freq_mhz": {"type": "number", "description": "Only the one on this frequency"},
           "band": {"type": "string", "description": "Only the band capture of this band ('433', '915', 'ism', "
                    "'902-928'), or 'any' for every band capture"}},
          aliases=("stop_capture", "stop_watchdog"))
def stop_background_watchdog(dongle=None, freq_mhz=None, band=None):
    running = _running_watchdogs()
    if band not in (None, "") and dongle in (None, "") and freq_mhz in (None, ""):
        # 'stop the 433 band capture': that band's recording; with none running, a capture inside the band
        if str(band).lower() in ("any", "all", "band", "true"):
            lo, hi = -1.0, 1e9
        else:
            lo, hi, _lb = _band_args(band)
        rec = [w for w in running if w.get("mode") == "band" and isinstance(w.get("band"), (list, tuple))
               and float(w["band"][0]) <= hi and float(w["band"][1]) >= lo]
        inside = [w for w in running if w.get("mode") != "band" and lo - 0.01 <= float(w.get("freq_mhz") or 0)
                  <= hi + 0.01]
        running = rec or inside or (running if hi > 1e8 else [])
        if not running:
            return {"message": "No band capture is running" + ("" if hi > 1e8 else f" on {lo:g}-{hi:g} MHz")}
    elif dongle not in (None, ""):
        ser = _resolve_dongle_serial(dongle, "")
        running = [w for w in running if w.get("serial") == ser]
    elif freq_mhz not in (None, ""):
        f = _to_mhz(freq_mhz)

        def _hits(w):                     # a band capture is stopped by any frequency in its band ('stop 434')
            b = w.get("band")
            if isinstance(b, (list, tuple)) and len(b) == 2:
                try:
                    return float(b[0]) - 0.01 <= f <= float(b[1]) + 0.01
                except (TypeError, ValueError):
                    pass
            return abs(float(w.get("freq_mhz") or 0) - f) <= 0.01
        running = [w for w in running if _hits(w)]
    if not running:
        return {"message": "No background capture or watchdog is running" + (" there" if dongle or freq_mhz else "")}
    stopped, asked, here, t_ask = [], [], [], time.time()
    for w in running:
        wd = _WATCHDOGS.get(w["serial"])
        if wd and wd.alive():
            wd.stop("stopped", wait=False)               # all of them asked first, then waited for together
            here.append((w, wd))
        else:                                  # it runs in another ARIS process: ask it through a stop file
            try:
                STOP_DIR.mkdir(parents=True, exist_ok=True)
                (STOP_DIR / str(w["serial"])).write_text(_now())
                asked.append(w)
            except OSError as e:
                raise RFError(f"Could not ask the other ARIS process to stop it: {e}")
    for w, wd in here:
        wd.wait_stopped()
        stopped.append(wd.summary())
        if wd.result:                                    # what it got, in the answer itself
            w["result"] = wd.result
    end = time.time() + 8.0
    while asked and time.time() < end:
        time.sleep(0.3)
        live = {x.get("serial") for x in _running_watchdogs()}
        asked = [w for w in asked if w["serial"] in live] if live else []
    gone = [w for w in running if w not in asked]
    if asked:
        return {"message": f"Asked {', '.join(w['dongle'] for w in asked)} to stop; "
                           f"{'it is' if len(asked) == 1 else 'they are'} still stopping",
                "stopped": [w["dongle"] for w in gone]}
    done = {}                  # what the ones another process stopped got: in captures.json by the time they are gone
    for c in _recent_captures(30)[::-1]:
        if float(c.get("t") or 0) >= t_ask - 2.0:
            done[str(c.get("serial"))] = c
    return {"message": _stopped_text([dict(w, result=w.get("result") or done.get(str(w["serial"]))) for w in gone]),
            "stopped": [w["dongle"] for w in gone]}


def _stopped_text(gone) -> str:
    """The reply to 'stop the capture': what was stopped, after how long, what it got, and the dongles freed.
    v4.8.0 said 'Those dongles are free again.' after one band capture."""
    items = []
    for w in gone:
        res = w.get("result") or {}
        secs = res.get("seconds")
        if secs is None and w.get("started_epoch"):
            secs = time.time() - float(w["started_epoch"])
        got = str(res.get("verdict") or "")
        got = got[len("Stopped: "):] if got.startswith("Stopped: ") else "" if got == "Stopped" else got
        items.append((_capture_name(w) + (f" after {_took(secs)}" if secs is not None else ""), got))
    n = len({str(w.get("serial")) for w in gone})
    free = "That dongle is free again." if n == 1 else "Those dongles are free again."
    said = [name + (f": {_sentence(_lc_first(got))}" if got else ".") for name, got in items]
    if len(said) == 1:
        return f"Stopped {said[0]} {free}"
    return f"Stopped {len(said)} captures. " + " ".join(x[0].upper() + x[1:] for x in said) + f" {free}"


def resume_captures() -> list:
    """GUI start-up: restart the captures a restart or crash interrupted, if their time is not up yet."""
    cands = []
    try:
        with open(WATCHDOG_RESUME) as f:
            cands += [w for w in json.load(f) if isinstance(w, dict)]
        WATCHDOG_RESUME.unlink()
    except (OSError, ValueError, TypeError):
        pass
    cands += [w for w in _state_entries() if w.get("pid") != os.getpid() and not _pid_alive(w.get("pid"))]
    done, out = set(), []
    for w in cands:
        ser, dl = str(w.get("serial") or ""), w.get("deadline")
        if not ser or ser in done or ser not in DONGLE_NAMES or (dl and float(dl) <= time.time() + 30):
            continue
        done.add(ser)
        lock = _device_lock(ser)
        if not lock.acquire(5.0, "background capture"):
            continue
        if w.get("mode") == "band" and w.get("band"):
            wd = BandWatch(ser, w["band"][0], w["band"][1], lock, label=w.get("band_label"), deadline=dl,
                           started=w.get("since"), started_epoch=w.get("started_epoch"), resumed=True)
        else:
            wd = Watchdog(ser, float(w["freq_mhz"]), lock, mode=w.get("mode", "capture"), rate=w.get("sample_rate"),
                          deadline=dl, started=w.get("since"), started_epoch=w.get("started_epoch"), resumed=True)
        _WATCHDOGS[ser] = wd
        wd.start()
        out.append((f"the {wd.label}" if wd.mode == "band" else f"{wd.freq_mhz:g} MHz") + f" on {_short(ser)}")
    _write_watchdog_state()
    if out:
        _notice("ARIS restarted, so I resumed the background capture" + ("s" if len(out) > 1 else "") + ": "
                + ", ".join(out) + ".", kind="capture", outcome="resumed")
    return out


def _watchdog_events():
    events = []
    if WATCHDOG_LOG.exists():
        with _LOG_LOCK:
            with open(WATCHDOG_LOG, "r", errors="replace") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(obj, dict):
                        events.append(obj)
    return events


def _transmission_starts(times, gap=2.0):
    """Burst times -> the start of each transmission (devices repeat a packet several times within a second)."""
    starts, last = [], None
    for t in sorted(times):
        if last is None or t - last > gap:
            starts.append(t)
        last = t
    return starts


def _repeat_every(starts):
    if len(starts) < 3:
        return None
    return round(statistics.median(b - a for a, b in zip(starts, starts[1:])), 1)


def _watchdog_summary(freq_mhz=None, model_filter=None, max_events=8) -> dict:
    """What the watchdog log holds: decoded devices, and undecoded signals grouped by their pulse pattern, with
    how often each one transmits."""
    events = _watchdog_events()
    if freq_mhz is not None:
        events = [x for x in events if x.get("aris_freq_mhz") is None or abs(float(x["aris_freq_mhz"]) - freq_mhz) <= 0.15]
    packets = [x for x in events if x.get("type", "packet") == "packet" and x.get("model")]
    if model_filter:
        packets = [x for x in packets if str(model_filter).lower() in str(x.get("model", "")).lower()]
    bursts = [x for x in events if x.get("type") == "burst"]
    ptimes = sorted(float(p["t"]) for p in packets if p.get("t"))

    def decoded_near(t):                      # the analyzer also reports bursts that a decoder then decoded
        import bisect
        i = bisect.bisect_left(ptimes, t - 1.5)
        return i < len(ptimes) and ptimes[i] <= t + 1.5
    undecoded = [b for b in bursts if not decoded_near(float(b.get("t") or 0))]

    devices = {}
    for p in packets:
        d = devices.setdefault((p.get("model"), p.get("id"), p.get("channel")), {
            "model": p.get("model"), "id": p.get("id"), "packets": 0, "first_seen": p.get("time"), "_t": [],
            "best_snr_db": None, "heard_on": set()})
        d["packets"] += 1
        d["last_seen"] = p.get("time")
        if p.get("t"):
            d["_t"].append(float(p["t"]))
        if isinstance(p.get("snr"), (int, float)):
            d["best_snr_db"] = max(d["best_snr_db"] or -99.0, round(float(p["snr"]), 1))
        d["heard_on"].add(_short(p.get("aris_serial", "?")))
        d["last_packet"] = None           # (filled in below, once per device and not once per packet)
        d["_last"] = p
    for d in devices.values():            # each device's last packet, codes stripped as always
        d["last_packet"] = {k: v for k, v in _safe_packet(d.pop("_last")).items()
                            if k not in ("time", "t", "type", "aris_serial", "aris_freq_mhz", "mod", "freq", "rssi",
                                         "snr", "noise", "protocol")}
    sigs, near = {}, {}
    for b in undecoded:
        f = b.get("aris_freq_mhz")
        shape = (("FSK",) if b.get("modulation") == "FSK" else
                 (b.get("modulation"), b.get("pattern"), round((b.get("short_pulse_us") or 0) / 50.0),
                  round((b.get("long_pulse_us") or 0) / 50.0)))
        if b.get("aris_band") and isinstance(f, (int, float)):
            # A band capture logs each burst at its own measured frequency (1 kHz steps, a few kHz of jitter):
            # bursts of the same shape within 15 kHz of a group's first one are that group (v4.8.6 listed one
            # undecoded signal as six rows at 433.917-433.933 MHz, which filled the list)
            cells = near.setdefault(shape, [])
            f = next((g0 for g0 in cells if abs(float(f) - g0) <= 0.015), float(f))
            if f not in cells:
                cells.append(f)
        key = (f,) + shape
        g = sigs.setdefault(key, {"freq_mhz": f, "modulation": b.get("modulation"),
                                  "pattern": b.get("pattern"), "coding": b.get("coding"),
                                  "bits_per_packet": b.get("bits_per_packet"),
                                  "packets_per_burst": b.get("packets_per_burst"),
                                  "short_pulse_us": b.get("short_pulse_us"),
                                  "long_pulse_us": b.get("long_pulse_us"), "bursts": 0, "first_seen": b.get("time"),
                                  "_t": [], "best_snr_db": None, "heard_on": set()})
        g["bursts"] += int(b.get("count") or 1)
        g["last_seen"] = b.get("time")
        g["_t"].append(float(b.get("t") or 0))
        if isinstance(b.get("aris_freq_mhz"), (int, float)):
            bf = float(b["aris_freq_mhz"])
            g["freq_range_mhz"] = [min(bf, (g.get("freq_range_mhz") or [bf, bf])[0]),
                                   max(bf, (g.get("freq_range_mhz") or [bf, bf])[1])]
        g.setdefault("_p", Counter())[_norm_guess(b.get("pattern"))] += 1
        if isinstance(b.get("snr_db"), (int, float)):
            g["best_snr_db"] = max(g["best_snr_db"] or -99.0, float(b["snr_db"]))
        g["heard_on"].add(_short(b.get("aris_serial", "?")))
    for coll in (devices, sigs):
        for g in coll.values():
            starts = _transmission_starts(g.pop("_t"))
            g["transmissions"] = len(starts)
            g["repeats_every_s"] = _repeat_every(starts)
            g["heard_on"] = sorted(g["heard_on"])
    for g in sigs.values():
        common = g.pop("_p").most_common()
        g["pattern"] = next((p for p, _ in common if p not in ("pattern unclear", "single pulse")), common[0][0])
        g["pattern_hint"] = _pattern_hint(g["modulation"], g["pattern"], g["short_pulse_us"], g["long_pulse_us"],
                                          g.get("bits_per_packet"))
    running = _running_watchdogs()
    out = {"watchdogs_running": running, "recent_captures": [
               {k: c.get(k) for k in ("finished", "freq_mhz", "dongle", "outcome", "verdict", "seconds")}
               for c in _recent_captures(5)],
           "decoded_devices": sorted(devices.values(), key=lambda d: -d["packets"])[:10],
           "decoded_devices_total": len(devices),
           "undecoded_signals": sorted(sigs.values(), key=lambda g: -g["bursts"])[:6],
           "undecoded_signals_total": len(sigs),
           "total_packets": len(packets), "total_undecoded_bursts": len(undecoded),
           "recent_events": [_safe_packet(x) for x in events[-max_events:]] if max_events else []}
    for g in sigs.values():
        g["signal"] = _fp_line(g, hint=False)
    if not packets and not undecoded:
        mins = max((w.get("minutes") or 0 for w in running), default=0)
        out["note"] = ("Nothing logged yet" + (f" after {mins} minutes" if mins else "") + ". The device may transmit "
                       "rarely, or this dongle's antenna may not suit it: investigate the frequency on all dongles to "
                       "see which one hears it best.")
    return out


@register(
    "read_background_watchdog",
    "Read what the background watchdog heard: decoded devices AND undecoded bursts grouped by pulse pattern, with "
    "how often each transmits. Use for 'check the logs' or 'what did the watchdog hear'. NOT for investigating a "
    "frequency (use investigate_single_frequency for that).",
    {"freq_mhz": {"type": "number", "description": "Only events from the watchdog on this frequency"},
     "max_events": {"type": "integer", "description": "Recent raw events to include (default 8)"},
     "model_filter": {"type": "string", "description": "Only decoded packets whose 'model' contains this text"}},
)
def read_background_watchdog(max_events=None, model_filter=None, freq_mhz=None):
    f = _to_mhz(freq_mhz) if freq_mhz not in (None, "") else None
    n = _int(max_events, "max_events", 8, 1, 50)
    mf = str(model_filter).strip() if model_filter not in (None, "") else None
    out = _watchdog_summary(f, mf, n)
    if mf and not out["decoded_devices"]:          # nothing by that name: say so, and show everything
        out = _watchdog_summary(f, None, n)
        out["filter_not_found"] = mf
    elif mf:
        out["filter"] = mf
    return out


def _plain_time(t) -> str:
    """A logged time for a reply: rtl_433 writes '2026-11-01T18:19:04', ARIS '2026-11-02 15:23:29'; shown alike."""
    return str(t or "?").replace("T", " ", 1)[:19]


def _logs_reply(rep) -> str:
    """'check the logs (for Truck)' answered without the AI round. On the live station (v4.8.6) the 7B model took
    about 4 minutes, pasted raw packet fields, was cut off, and called Schrader and Citroen tyre sensors 'the Truck
    device' (the route had dropped the filter)."""
    lines = []
    for w in rep.get("watchdogs_running") or []:
        lines.append(f"Running now: {_capture_doing(w)} on {w.get('dongle')}"
                     + (f" ({w['status']})" if w.get("status") else "") + ".")
    if rep.get("filter_not_found"):
        lines.append(f"No decoded device in the logs is called \"{rep['filter_not_found']}\". Everything logged:")
    devs = rep.get("decoded_devices") or []
    plural = lambda n, w: f"{n} {w}{'' if n == 1 else 's'}"
    if devs:
        lines.append((f"Decoded devices matching \"{rep['filter']}\":" if rep.get("filter") else "Decoded devices:"))
        for d in devs[:8]:
            ident = f" {d['id']}" if d.get("id") not in (None, "") else ""
            every = f", about every {d['repeats_every_s']:g} s" if d.get("repeats_every_s") else ""
            lines.append(f"- {d.get('model')}{ident}: {plural(int(d.get('packets') or 0), 'packet')} in "
                         f"{plural(int(d.get('transmissions') or 0), 'transmission')}{every}, last seen "
                         f"{_plain_time(d.get('last_seen'))} on {', '.join(d.get('heard_on') or []) or '?'}.")
        more = int(rep.get("decoded_devices_total") or len(devs)) - min(8, len(devs))
        if more > 0:
            lines.append(f"... and {more} more decoded device{'s' if more > 1 else ''} (Activity tab).")
    sigs = [] if rep.get("filter") else (rep.get("undecoded_signals") or [])
    if sigs:
        by_sig = {}
        for g in sigs:                      # the same reason once, with every frequency that gave it
            by_sig.setdefault(g.get("signal") or g.get("pattern") or "unknown signal", []).append(g)
        lines.append("Not decoded:")
        for sig, gs in by_sig.items():
            fs = ", ".join(f"{float(g['freq_mhz']):.3f}" for g in gs if g.get("freq_mhz") is not None)
            nb = sum(int(g.get("bursts") or 0) for g in gs)
            nt = sum(int(g.get("transmissions") or 0) for g in gs)
            lines.append(f"- {fs} MHz: {sig} ({plural(nb, 'burst')} in {plural(nt, 'transmission')}, last seen "
                         f"{_plain_time(max(str(g.get('last_seen') or '') for g in gs))}).")
        more = int(rep.get("undecoded_signals_total") or len(sigs)) - len(sigs)
        if more > 0:
            lines.append(f"... and {more} more undecoded signal{'s' if more > 1 else ''} (Activity tab).")
    if not devs and not sigs:
        lines.append(rep.get("note") or "Nothing logged yet.")
    return "\n".join(lines)


@register(
    "list_devices",
    "List the devices decoded so far (the Devices tab): tyre sensors grouped per vehicle, weather and door sensors, "
    "remotes, meters; newest first, with how often and where each was heard. Use for 'what devices have you "
    "seen', 'list devices', 'which cars'. NOT for undecoded signals (read_background_watchdog).",
    {"filter": {"type": "string", "description": "Only devices whose make, ID, kind or frequency contains this "
                "text (e.g. 'Citroen', 'tpms', '433.9')"},
     "limit": {"type": "integer", "description": "Rows to list (default 10)"}},
)
def list_devices(filter=None, limit=None):          # (the dongle list is _list_devices: a different thing)
    n = _int(limit, "limit", 10, 1, 100)
    text = str(filter).strip() if filter not in (None, "") else None
    rep = devices_list(limit=n, text=text)
    if text and not rep["rows"]:                     # nothing by that name: say so, and show everything
        rep = devices_list(limit=n)
        rep["filter_not_found"] = text
    return rep


_FILTER_WORDS = {"tpms": "cars (tyre sensors)"}     # a filter the router chose, in the user's own words


def _devices_reply(rep) -> str:
    """'what devices have you seen' answered without the AI round: the top rows in words."""
    rows = rep.get("rows") or []
    if not rows:                     # (also 'which cars' with nothing decoded: never a dangling 'Everything seen:')
        return ("No devices decoded yet. Captures and band recordings (say \"record the 433 band\") and "
                "investigations add every device a decoder names to the Devices tab.")
    sets_n, n = int(rep.get("sets") or 0), int(rep.get("devices") or 0)
    kn = int(rep.get("known") or 0)
    head = (f"Devices decoded so far: {n}"
            + (f" ({sets_n} vehicle{'s' if sets_n != 1 else ''} or set{'s' if sets_n != 1 else ''} among them)"
               if sets_n else "") + (f", {kn} known" if kn else "")
            + f", {_plural(int(rep.get('packets') or 0), 'packet')}.")
    lines = [head]
    mf = rep.get("filter_not_found")
    if mf:
        what = _FILTER_WORDS.get(str(mf).lower())     # 'which cars' asked for cars, not for "tpms"
        lines.append((f"No {what} decoded yet." if what else f"No device matches \"{mf}\".") + " Everything seen:")
    elif rep.get("filter"):
        lines.append(f"Matching \"{rep['filter']}\":")
    for r in rows:
        seen = r["seen_line"].replace(" | ", ", ")           # (the page's line; the chat reads better with commas)
        line = f"- {r['title']}{' (known)' if r.get('known') else ''}: {seen[0].lower() + seen[1:]}"
        if r.get("reading_words"):
            line += f"; last reading {r['reading_words']}"
        if r.get("kind") == "set":
            line += f" (IDs {', '.join(str(i) for i in r.get('ids') or [])})"
        lines.append(line + ".")
    shown = int(rep.get("shown") or len(rows))
    if shown > len(rows):
        lines.append(f"... and {shown - len(rows)} more on the Devices tab.")
    return "\n".join(lines)


@register(
    "wifi_networks",
    "Answer a question about the Wi-Fi networks a passive scan heard (Devices tab, Wi-Fi): new or worth a look, "
    "hidden, the strongest, a channel, a network by name, new today. Pass the user's question as it was asked.",
    {"question": {"type": "string", "description": "The user's question, in their words"}},
)
def wifi_networks(question=None):
    """v4.13.0: read the GUI's Wi-Fi list (DATA_DIR/wifi.json; the GUI folds the helper's scans in every
    minute) and answer from it in plain words (arislib.wireless.answer). Reads only: ARIS never scans Wi-Fi itself."""
    from arislib import wireless
    try:
        st = json.loads((DATA_DIR / "wifi.json").read_text())
    except (OSError, ValueError):
        st = None
    if not isinstance(st, dict) or not isinstance(st.get("nets"), dict):
        st = wireless.new_state()
    return {"status": "ok", "answer": wireless.answer(st, str(question or ""))}
