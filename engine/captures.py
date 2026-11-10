"""ARIS engine, part "captures": Captures (the background watchdog): state shared between ARIS processes, start / stop / resume,
results and notices.

Executed into aris.py's namespace by aris._include (see aris.py): names from aris.py's imports and from the
parts before this one are available here without imports, and everything defined here becomes aris.<name>.
"""
from __future__ import annotations

# ----------------------------------------------------------------------------
# Watchdog state shared between ARIS processes (the GUI hosts captures; the chat console and a terminal ARIS
# see them through this file)
# ----------------------------------------------------------------------------
_WD_STATE_LOCK = threading.Lock()


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False


def _state_entries() -> list:
    try:
        with open(WATCHDOG_STATE) as f:
            items = json.load(f).get("watchdogs", [])
        return [w for w in items if isinstance(w, dict)]
    except (OSError, ValueError, AttributeError):
        return []


def _write_watchdog_state() -> None:
    with _WD_STATE_LOCK:
        me = os.getpid()
        keep = [w for w in _state_entries() if _live_elsewhere(w, me)]
        mine = [w.summary() for w in list(_WATCHDOGS.values()) if w.alive()]
        try:
            _atomic_write_json(WATCHDOG_STATE, {"updated": _now(), "watchdogs": keep + mine})
        except OSError:
            pass


WATCHDOG_BEAT_S = 120.0     # a capture writes its state at least every 15 s; one silent this long has ended


def _live_elsewhere(w, me) -> bool:
    """An entry of another ARIS process that is really running: the process lives and the entry is fresh."""
    if w.get("pid") in (None, me) or not _pid_alive(w.get("pid")):
        return False
    beat = w.get("beat")
    return beat is None or time.time() - float(beat) < WATCHDOG_BEAT_S


def _running_watchdogs() -> list:
    """Every running watchdog / capture: this process's own, and those of other ARIS processes (GUI or terminal)."""
    me = os.getpid()
    mine = [w.summary() for w in list(_WATCHDOGS.values()) if w.alive()]
    return mine + [w for w in _state_entries() if _live_elsewhere(w, me)]


def _watchdog_elsewhere(serial):
    """The watchdog another ARIS process is running on this dongle (its summary), or None."""
    serial, me = str(serial), os.getpid()
    for w in _state_entries():
        if str(w.get("serial")) == serial and _live_elsewhere(w, me):
            return w
    return None


def _notice(text, **extra) -> None:
    """A message for the chat (the GUI shows it as soon as it arrives) and for a terminal user."""
    rec = {"t": round(time.time(), 2), "time": _now(), "text": str(text), **extra}
    try:
        with open(NOTICES, "a") as f:
            f.write(json.dumps(rec, default=_json_default) + "\n")
        if NOTICES.stat().st_size > 400_000:
            _trim_jsonl(NOTICES, 300)
    except OSError:
        pass
    if not os.environ.get("ARIS_GUI_CHILD") and not GUI_HOST and sys.stdin and sys.stdin.isatty():
        print(f"\n[Notice] {text}")


def _recent_captures(n=10) -> list:
    try:
        with open(CAPTURES) as f:
            items = json.load(f)
        return [c for c in items if isinstance(c, dict)][-n:][::-1]
    except (OSError, ValueError):
        return []


def _took(seconds) -> str:
    s = int(round(seconds))
    return f"{s} s" if s < 90 else f"{s // 60} min {s % 60} s" if s < 3600 else f"{s // 3600} h {s % 3600 // 60} min"


def _plural(n, word) -> str:
    """'1 burst', '96 bursts' (never 'burst(s)')."""
    return f"{n} {word}{'' if n == 1 else 's'}"


def _sentence(text) -> str:
    """A verdict as a sentence: ends with one full stop."""
    t = str(text or "").strip()
    return t + ("" if t.endswith((".", "!", "?")) else ".") if t else ""


def _lc_first(text) -> str:
    """'No bursts ...' -> 'no bursts ...' after a colon; 'RDS ...', 'HITS FM' and numbers stay as they are."""
    t = str(text or "")
    return t[0].lower() + t[1:] if len(t) > 1 and t[0].isupper() and t[1].islower() else t


def _band_capture_name(label) -> str:
    """A band capture in a sentence: '433 MHz band (433.05-434.79)' -> '433 MHz band capture (433.05-434.79)',
    '902-928 MHz ISM band' -> '902-928 MHz ISM band capture', 'band 433-436.4 MHz' -> '433-436.4 MHz band capture'."""
    label = str(label or "band").strip()
    if label.startswith("band "):
        label = label[5:] + " band"
    head, sep, rest = label.partition(" (")
    if not head.endswith("band"):
        head += " band"
    return f"{head} capture" + (f" ({rest}" if sep else "")


def _with_decoder(label) -> str:
    """A decoder label as it reads after 'with': a program by its name ('with rtl_433 at 250 kS/s'), anything else
    with 'the' ('with the ADS-B decoder', 'with the FM / RDS decoder')."""
    label = str(label or "its decoder")
    return label if label.startswith(("rtl_", "its ", "the ")) else "the " + label


def _capture_name(w) -> str:
    """A running or finished capture (its summary or result) in a sentence, with its dongle."""
    d = w.get("dongle") or _short(w.get("serial"))
    if w.get("mode") == "band" or w.get("band_label"):
        return f"the {_band_capture_name(w.get('band_label'))} on {d}"
    return f"the capture on {float(w.get('freq_mhz') or 0):g} MHz ({d})"


def _capture_doing(w) -> str:
    """What a running capture keeps its dongle doing, in words ('recording the 433 MHz band')."""
    if w.get("mode") == "band" or w.get("band_label"):
        return f"recording the {_band_capture_name(w.get('band_label'))}"
    what = "logging" if w.get("mode") == "continuous" else "capturing"
    return f"{what} {float(w.get('freq_mhz') or 0):g} MHz" + (" continuously" if w.get("mode") == "continuous" else "")


def _save_capture(res) -> bool:
    """A finished capture: kept for the Activity tab, and saved to the Signals list when it got something."""
    with _WD_STATE_LOCK:
        try:
            with open(CAPTURES) as f:
                items = json.load(f)
        except (OSError, ValueError):
            items = []
        try:
            _atomic_write_json(CAPTURES, ([c for c in items if isinstance(c, dict)] + [res])[-CAPTURES_KEEP:])
        except OSError:
            pass
    if res.get("outcome") not in ("identified", "demodulated") or res.get("public_broadcast"):
        return False
    f = float(res["freq_mhz"])
    peak = {"frequency_mhz": f, "probable_allocation": _classify_frequency_allocation(f, 25.0)}
    s2 = {}
    if res.get("devices"):
        s2["identified_models"] = sorted({d["model"] for d in res["devices"]})
    if res.get("signal"):
        s2["unknown_signal_fingerprints"] = [res["signal"]]
    out = {"frequency_mhz": f, "final_signal_verdict": res.get("verdict"), "snr_db": res.get("snr_db"),
           "evidence": "confirmed_decode" if res["outcome"] == "identified" else "signal_present",
           "dongle_comparison": [{"dongle": res.get("dongle"), "heard": True}], "strongest_dongle": res.get("dongle")}
    if s2:
        out["stage2_rtl433_telemetry"] = s2
    return bool(_log_signal(peak, out, res.get("serial")).get("saved_to_signal_list"))


class Watchdog:
    """Keeps one dongle on one frequency in the background; it holds the dongle the whole time.
    capture (default): runs the decoder that suits the band until it gets a packet it can DECODE (the device is
      then identified) or at least DEMODULATE (an unknown device, described by its signal), then reports in the
      chat, saves it to Signals and frees the dongle. Gives up after max_min minutes.
      Sensor bands: rtl_433 (an FSK packet no decoder knows at 250 kS/s gets another try at 1 MS/s). FM band: RDS
      station name. Pagers / VHF / UHF: pager, APRS and tone decoders. Airband: AM activity. 1090 MHz: ADS-B.
    continuous: rtl_433 logs every decoded packet and every undecoded burst until stopped (or max_min)."""
    MAX_RESTARTS = 10

    def __init__(self, serial, freq_mhz, lock, mode="capture", max_min=CAPTURE_DEFAULT_MIN, rate="250k",
                 deadline=None, started=None, started_epoch=None, resumed=False):
        self.serial, self.freq_mhz = str(serial), float(freq_mhz)
        self.lock = lock                     # held for the watchdog's whole life
        self.mode = "continuous" if mode == "continuous" else "capture"
        self.kind = _band_kind(self.freq_mhz)
        self.engine = "rtl_433" if self.kind in ("sensor", "other") or self.mode == "continuous" else self.kind
        self.rate = rate if rate in ("250k", "1024k") else "250k"
        self.started = started or _now()
        self.started_t = float(started_epoch or time.time())
        self.max_min = float(max_min or 0)
        self.deadline = deadline if deadline else (self.started_t + self.max_min * 60.0 if self.max_min else None)
        self.stop_evt = threading.Event()
        self.stop_reason = None
        self.proc = None
        self.thread = None
        self.packets = self.bursts = self.restarts = self.looks = 0
        self.tail = deque(maxlen=15)
        self.last_error = None
        self.status = "starting"
        self.result = None                   # the outcome once the capture finished
        self.resumed = resumed
        self._pk = []                        # decoded packets (in memory only, for the device list)
        self._unknown = {}                   # demodulated but undecoded signals: key -> {"n", "total", "fp"}
        self._decoded_at = None
        self._fast_tried = self.rate != "250k"
        self._switch = None
        self._state_t = 0.0
        self._quick = 0                      # rtl_433 starts in a row that failed at once
        self._ended = False                  # set as the thread finishes (alive() is False from then on)

    @property
    def events(self) -> int:
        return self.packets + self.bursts

    def alive(self) -> bool:
        # (its thread is still alive while it writes the state on the way out: `_ended` keeps it off the list)
        return self.thread is not None and self.thread.is_alive() and not self._ended

    def decoder(self) -> str:
        """The decoder as a label (the Activity tab, /captures): no parentheses, as it often stands in some."""
        return {"rtl_433": f"rtl_433 at {self.rate[:-1]} kS/s", "wide": "FM / RDS decoder",
                "air": "airband AM activity check", "adsb": "ADS-B decoder",
                "audio": "pager / APRS / tone decoders"}.get(self.engine, self.engine)

    def decoder_phrase(self) -> str:
        """The decoder as it reads after 'with' (v4.8.0 said "with the rtl_adsb")."""
        return _with_decoder(self.decoder())

    def until_phrase(self) -> str:
        """What a capture waits for, in a sentence after 'It keeps listening'."""
        return {"wide": "until it reads the station's RDS name, or has heard the signal in three looks",
                "air": "until it hears a transmission (activity only: nothing is recorded)",
                "adsb": "until it hears a checksum-verified aircraft message",
                "audio": "until a pager, APRS or tone decoder recognises a transmission, or it has heard three"
                }.get(self.engine, "until it captures a packet it can decode (then the device is identified) or "
                                   "at least demodulate")

    def summary(self) -> dict:
        return {"dongle": _short(self.serial), "serial": self.serial, "freq_mhz": self.freq_mhz, "mode": self.mode,
                "decoder": self.decoder(), "since": self.started, "started_epoch": round(self.started_t, 1),
                "minutes": round((time.time() - self.started_t) / 60.0, 1), "deadline": self.deadline,
                "running": self.alive(), "status": self.status, "decoded_packets": self.packets,
                "bursts_heard": self.bursts, "restarts": self.restarts, "sample_rate": self.rate,
                "last_error": self.last_error, "pid": os.getpid(), "beat": round(time.time(), 1)}

    def start(self) -> None:
        try:
            (STOP_DIR / self.serial).unlink()         # a stale stop request must not end this watchdog
        except OSError:
            pass
        self.thread = threading.Thread(target=self._run, name=f"watchdog-{self.serial}", daemon=True)
        self.thread.start()

    def stop(self, reason="stopped", wait=True) -> None:
        """Stop it. Its own thread ends its radio program within half a second (the look in progress too: the stop
        event is its _stop_requested), SIGKILLs it when its stop hangs, and frees the dongle once that program has
        exited. wait=False only asks: stop_background_watchdog asks every capture first and then waits for them
        all, so several stop together instead of one after another."""
        self.stop_reason = self.stop_reason or reason
        self.stop_evt.set()
        if wait:
            self.wait_stopped()

    def wait_stopped(self, timeout=WATCHDOG_STOP_S) -> None:
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=timeout)
            if self.thread.is_alive() and self.proc is not None:
                self._kill(sig=None)                  # its thread did not get to it in time: SIGKILL now
                self.thread.join(timeout=2)
        _write_watchdog_state()

    # -- helpers ---------------------------------------------------------------------------------------------
    def _kill(self, sig=signal.SIGTERM) -> None:
        """End its radio program and wait until it has exited (SIGTERM, then SIGKILL: _end_radio)."""
        p = self.proc
        if p is not None:
            _end_radio(p, sig=sig)

    def _done(self) -> bool:
        if self.stop_evt.is_set() or self.result is not None:
            return True
        try:
            if (STOP_DIR / self.serial).exists():     # stop request from another ARIS process
                (STOP_DIR / self.serial).unlink()
                self.stop_reason = self.stop_reason or "stopped"
                self.stop_evt.set()
                return True
        except OSError:
            pass
        if self.deadline and time.time() >= self.deadline:
            self.stop_reason = self.stop_reason or "timeout"
            self.stop_evt.set()
            return True
        return False

    def _tick_state(self, force=False) -> None:
        if force or time.time() - self._state_t > 15.0:  # counts and status for the other processes, now and then
            self._state_t = time.time()
            _write_watchdog_state()

    def _set_status(self, text) -> None:
        if text != self.status:
            self.status = text
            self._tick_state(force=True)

    # -- rtl_433 events ---------------------------------------------------------------------------------------
    def _burst(self, b) -> None:
        _log_append({"type": "burst", "t": round(time.time(), 2), "time": _now(), "modulation": b["mod"],
                     "pattern": b.get("guess") or "unclear", "coding": _coding_text(b.get("coding")),
                     "demodulated": bool(b.get("coding")), "short_pulse_us": b.get("short"),
                     "long_pulse_us": b.get("long"), "bits_per_packet": b.get("bits"),
                     "packets_per_burst": b.get("rows"), "snr_db": b.get("snr"), "offset_khz": b.get("off"),
                     "pulses": b.get("pulses"), "burst_ms": b.get("ms"), "aris_serial": self.serial,
                     "aris_freq_mhz": self.freq_mhz})
        self.bursts += 1

    def _ingest(self, obj) -> None:
        """A decoded packet: logged without remote-control codes (see _safe_packet)."""
        _log_append({**_safe_packet(obj), "type": "packet", "t": round(time.time(), 2), "aris_serial": self.serial,
                     "aris_freq_mhz": self.freq_mhz})
        self.packets += 1
        if len(self._pk) < 300:
            self._pk.append(obj)
        if self.mode == "capture" and self._decoded_at is None:
            self._decoded_at = time.time()
            self._set_status("decoded a packet: collecting its repeats")

    def _event(self, ev) -> None:
        kind, obj = ev
        if kind == "packet":
            self._ingest(obj)
        elif kind == "package":
            if obj.get("decoded") or _noise_package(obj):
                return
            self._burst(obj)
            if obj.get("demodulated") and self.mode == "capture":
                fp = _fingerprints([obj])[0]
                u = self._unknown.setdefault(_fp_key(obj), {"n": 0, "total": 0, "fp": fp})
                u["n"] += 1
                u["total"] += 1
                if (fp.get("snr_db") or -99) >= (u["fp"].get("snr_db") or -99):
                    u["fp"] = fp
        else:
            self.tail.append(obj)

    def _check_capture(self) -> None:
        if self.mode != "capture" or self.result is not None:
            return
        now = time.time()
        if self._decoded_at is not None:
            if now - self._decoded_at >= 3.0:          # devices repeat a packet several times: wait for them
                self._finish("identified")
            return
        for u in self._unknown.values():
            fp = u["fp"]
            if not (u["n"] >= 2 or (u["n"] >= 1 and (fp.get("packets_per_burst") or 0) >= 2)):
                continue                                # one lone package could be noise: wait for a repeat
            if fp["modulation"] == "FSK" and not self._fast_tried:
                self._fast_tried, self._switch = True, "1024k"
                u["n"], u["fast_since"] = 0, now
                self._set_status("an FSK device no decoder knows at 250 kS/s: listening again at 1 MS/s")
                return
            self._finish("demodulated", fp=u)
            return
        for u in self._unknown.values():                # tried 1 MS/s for 10 min without hearing it again
            if u.get("fast_since") and now - u["fast_since"] > 600:
                self._finish("demodulated", fp=u)
                return

    # -- outcome -----------------------------------------------------------------------------------------------
    def _finish(self, outcome, fp=None, verdict=None, **extra) -> None:
        if self.result is not None:
            return
        f, d = self.freq_mhz, _short(self.serial)
        dur = time.time() - self.started_t
        res = {"outcome": outcome, "freq_mhz": f, "dongle": d, "serial": self.serial, "mode": self.mode,
               "decoder": self.decoder(), "started": self.started, "finished": _now(), "t": round(time.time(), 1),
               "seconds": round(dur), "decoded_packets": self.packets, "bursts_heard": self.bursts, **extra}
        if outcome == "identified" and verdict is None:
            devs = _devices_from_packets(self._pk, heard_by=d)
            res["devices"] = devs
            res["snr_db"] = max((x["best_snr_db"] for x in devs if x.get("best_snr_db") is not None), default=None)
            verdict = _identified_text(devs, limit=4)      # known devices folded into one clause
        elif outcome == "demodulated" and verdict is None and fp:
            sig = dict(fp["fp"], bursts=fp.get("total", 1))
            res["signal"], res["snr_db"] = sig, sig.get("snr_db")
            verdict = ("Captured, not identified: rtl_433 demodulated its packets but none of its decoders knows "
                       "this device. Its signal: " + _fp_line(sig)
                       + (f" (also tried at 1 MS/s)" if self._fast_tried and self.rate == "1024k" else ""))
        elif outcome == "timeout" and verdict is None:
            # (no duration in a verdict: the notice, the Activity tab and /captures each say how long it ran)
            heard = (f"; it heard {_plural(self.bursts, 'burst')} too weak or unclear to demodulate"
                     if self.bursts else "")
            verdict = (f"Nothing came in that it could decode or demodulate{heard}. The device may be quiet, or this "
                       f"antenna may not suit it: 'investigate {f:g}' compares the dongles"
                       if self.mode == "capture" else self._logged_line())
        elif outcome == "stopped" and verdict is None:
            verdict = "Stopped" + (f": {self._logged_line()}" if self.engine == "rtl_433" else "")
        elif outcome == "error" and verdict is None:
            verdict = (f"It could not run: {self.last_error or 'the decoder stopped working'}. Run /check to test "
                       f"the dongles")
        res["verdict"] = verdict
        self.result = res
        self.status = "finished"
        saved = _save_capture(res)
        if outcome == "stopped":
            return                       # whoever stopped it says what it got (stop_background_watchdog)
        _notice(self._notice_text(outcome, dur, verdict, saved), kind="capture", outcome=outcome, freq_mhz=f,
                dongle=d)

    def _logged_line(self) -> str:
        return f"{_plural(self.packets, 'decoded packet')} and {_plural(self.bursts, 'undecoded burst')} logged"

    def _notice_text(self, outcome, dur, verdict, saved) -> str:
        """The chat message when it ends by itself: what, where, how long once; then the result."""
        f, d = self.freq_mhz, _short(self.serial)
        what = "Capture" if self.mode == "capture" else "Continuous logging"
        how = "stopped" if outcome == "error" else "finished"
        return (f"{what} on {f:g} MHz ({d}) {how} after {_took(dur)}. {_sentence(verdict)}"
                + (" Saved to the Signals list." if saved else "") + f" {d} is free again.")

    # -- main loops --------------------------------------------------------------------------------------------
    def _run(self) -> None:
        _QUIET.stop = self.stop_evt          # a stop also ends the look in progress (_capture_raw, _run_capture)
        try:
            self._set_status("listening" + (" (resumed after a restart)" if self.resumed else ""))
            if self.engine == "rtl_433":
                self._run_rtl433()
            elif self.engine == "band":
                self._run_band()
            else:
                self._run_looks()
        except Exception as e:
            log.error("watchdog %s crashed:\n%s", self.serial, traceback.format_exc())
            self.last_error = f"{type(e).__name__}: {e}"
        finally:
            self._kill()
            if self.result is None:
                if self.stop_reason == "shutdown":
                    pass                                 # resumed when ARIS starts again
                elif self.stop_reason in ("stopped", "timeout"):
                    self._finish(self.stop_reason)
                else:
                    self._finish("error")
            self.status = "finished" if self.stop_reason != "shutdown" else "interrupted"
            self._ended = True               # before the state is written: other processes must not see it running
            self.lock.release()              # (its radio program has exited by now: _kill waited for it)
            _QUIET.stop = None
            _write_watchdog_state()

    @staticmethod
    def _pump(proc, q) -> None:
        try:
            for line in proc.stdout:
                q.put(line)
        except (OSError, ValueError):
            pass
        q.put(None)

    def _run_rtl433(self) -> None:
        import queue
        backoff = 2
        while not self._done():
            tune = int(round((self.freq_mhz - 0.035) * 1e6))   # signal off the 0 Hz DC spike, as in investigations
            cmd = ["rtl_433"] + _rtl433_device(self.serial) + ["-f", str(tune), "-s", self.rate,
                   "-g", str(_gain_for(self.serial, self.freq_mhz, self.freq_mhz))] + _ppm_args(self.serial)
            cmd += _rtl433_extra_flags() + ["-A", "-F", "json", "-F", "log", "-M", "time:iso", "-M", "level",
                                            "-M", "protocol"]
            t0, events0 = time.time(), self.events
            self.tail.clear()
            try:            # stderr into stdout: the order tells decoded packages from undecoded ones
                self.proc = subprocess.Popen(_radio_cmd(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                             bufsize=1, errors="replace")
            except (OSError, RFError) as e:
                self.last_error = f"Cannot start rtl_433: {e}"
                return
            q = queue.Queue()
            threading.Thread(target=self._pump, args=(self.proc, q), daemon=True).start()
            rd = _R433Reader()
            while True:
                try:
                    line = q.get(timeout=0.4)
                except queue.Empty:
                    line = ""
                if line is None:                          # rtl_433 ended
                    for ev in rd.flush():
                        self._event(ev)
                    break
                if line:
                    for ev in rd.feed(line):
                        self._event(ev)
                elif rd.cur is not None and time.time() - rd.last > 1.0:
                    for ev in rd.flush():                 # the package's description is complete
                        self._event(ev)
                self._check_capture()
                self._tick_state()
                if self._done() or self._switch:
                    self._kill()
            rc = self.proc.wait()
            try:
                self.proc.stdout.close()
            except OSError:
                pass
            if self._done():
                return
            if self._switch:
                self.rate, self._switch = self._switch, None
                continue
            self.restarts += 1
            self.last_error = _rtl433_problem(list(self.tail), rc)
            log.warning("watchdog %s: %s | %s", self.serial, self.last_error, " | ".join(list(self.tail)[-3:]))
            # rtl_433 that dies at once without hearing anything will not get better by itself: stop early
            quick = time.time() - t0 < 15 and self.events == events0
            self._quick = self._quick + 1 if quick else 0
            if self.restarts > self.MAX_RESTARTS or self._quick >= (2 if "could not" in self.last_error else 3):
                return
            if time.time() - t0 > 60:
                backoff = 2
            self._set_status(f"rtl_433 stopped (restart {self.restarts}); starting it again")
            self.stop_evt.wait(backoff)
            backoff = min(backoff * 2, 60)

    def _run_looks(self) -> None:
        """Bands without rtl_433: one short capture after another until something decodes or demodulates."""
        f, ser = self.freq_mhz, self.serial
        alloc = _classify_frequency_allocation(f, 25.0)
        present = errors = 0
        while not self._done():
            if self.looks:
                time.sleep(USB_SETTLE_S)              # the dongle is opened again at once (the watchdog holds it)
            self.looks += 1
            self._tick_state()                        # the heartbeat other processes go by
            gain = _gain_for(ser, f, f)
            if self.kind == "adsb":
                r = _stage_adsb(ser, 10, gain)
                self._adsb_msgs = getattr(self, "_adsb_msgs", 0) + int(r.get("messages") or 0)
                if r.get("aircraft_heard"):
                    return self._finish("identified", verdict=(
                        f"Identified: aircraft ADS-B transponders, {r['aircraft_heard']} aircraft "
                        f"({r['valid_messages']} checksum-verified position or identity messages, public "
                        f"broadcast)"), public_broadcast=True, aircraft=r.get("aircraft"))
                if r.get("messages"):        # Mode S replies or noise: no verified aircraft message yet
                    self._set_status(f"heard {self._adsb_msgs} Mode S messages, no checksum-verified aircraft "
                                     f"message yet")
                if not str(r.get("status", "")).startswith("listened"):
                    errors += 1
                    self.last_error = str(r.get("status"))[:160]
                    if errors >= 5:
                        return
                continue
            r = (_stage1_wideband(f, ser, gain, 3.0) if self.kind == "wide"
                 else _stage1_iq_analysis(f, ser, gain, 25.0, 3.0))
            if _stage_failed(r):
                errors += 1
                self.last_error = str(r.get("status"))[:160]
                if errors >= 5:
                    return
                continue
            errors = 0
            if not r.get("signal_present_during_capture"):
                continue
            present += 1
            snr = r.get("channel_snr_db") if self.kind == "wide" else (
                r.get("peak_snr_db") if r.get("peak_snr_db") is not None else r.get("channel_avg_snr_db"))
            if self.kind == "wide":
                v, ev, _note, station = _wide_verdict(r, f, {"probable_allocation": alloc})
                if r.get("rds_station_name") or present >= 3:
                    return self._finish("identified" if ev == "confirmed_decode" else "demodulated", verdict=v,
                                        snr_db=snr, public_broadcast=bool(station),
                                        rds_station_name=r.get("rds_station_name"))
                self._set_status(f"FM signal heard ({present} of 3 looks), waiting for its RDS name")
                continue
            if self.kind == "air":
                return self._finish("demodulated", snr_db=snr, verdict=(
                    f"Aviation-band transmission heard ({r.get('physical_waveform_classification')}); AM voice or "
                    f"data, content not recorded"))
            time.sleep(USB_SETTLE_S)                  # audio channels: the data decoders listen for a while
            self._set_status("transmission heard: running the pager / APRS / tone decoders")
            a = _stage3_demod_audio(f, ser, 20, gain)
            frames, aprs = a.get("validated_frames") or [], a.get("aprs_frames") or []
            if frames:
                return self._finish("identified", verdict=f"Identified: {frames[0]}", snr_db=snr)
            if aprs:
                return self._finish("identified", verdict=f"Identified: APRS / AX.25 packet radio ({aprs[0]})",
                                    snr_db=snr)
            if a.get("ctcss_tone") not in (None, "none"):
                return self._finish("demodulated", snr_db=snr, verdict=(
                    f"Analog FM voice channel with CTCSS tone {a['ctcss_tone']} (band: {alloc}); content not "
                    f"recorded"))
            if present >= 3:
                return self._finish("demodulated", snr_db=snr, verdict=(
                    f"Transmissions heard 3 times ({r.get('physical_waveform_classification')}, band: {alloc}); no "
                    f"pager, APRS or tone decoder recognised them, content not recorded"))
            self._set_status(f"heard a transmission ({present} of 3), nothing decoded yet")


def _strongest_recently(freq, within_mhz=0.1, hours=2.0):
    """The dongle that heard this frequency best in a recent investigation, if any."""
    names = {_short(x): x for x in DONGLE_NAMES}
    for c in _earlier_checks(freq, within_mhz, hours):
        if c.get("strongest_dongle") in names:
            return names[c["strongest_dongle"]]
    return None


def _quick_compare(f, serials, cfg):
    """3 s look on every free dongle at once: the one that hears f best, and why (None if none heard it)."""
    if len(serials) < 2:
        return None, None
    kind = _band_kind(f)
    _say(f"[HW] Comparing {', '.join(_short(s) for s in serials)} on {f:g} MHz for 3 s to pick the best one...")

    def look(ser):
        gain = _gain_for(ser, f, f, cfg)
        with _use_dongle(ser, timeout=10, holder="choosing a dongle"):
            return (_stage1_wideband(f, ser, gain, 3.0) if kind == "wide"
                    else _stage1_iq_analysis(f, ser, gain, 25.0, 3.0))
    snr = {}
    for ser, r in _parallel(look, serials).items():
        if isinstance(r, dict) and r.get("signal_present_during_capture"):
            v = r.get("channel_snr_db") if kind == "wide" else (
                r.get("peak_snr_db") if r.get("peak_snr_db") is not None else r.get("channel_avg_snr_db"))
            if v is not None:
                snr[ser] = float(v)
    if not snr:
        return None, None
    best = max(snr, key=snr.get)
    return best, f"it heard {f:g} MHz best in a 3 s comparison, {snr[best]:.0f} dB above noise"


def _claim_best_dongle(f, dongle=None):
    """Lock the free dongle that hears f best for a background job: the one named, else the strongest in a recent
    investigation, else the winner of a quick 3 s comparison, else the band's best from /setup.
    Returns (serial, lock, why): why is in plain words, without parentheses (the messages put it in some)."""
    cfg = _load_config()
    busy = {w["serial"] for w in _running_watchdogs()}
    if dongle not in (None, ""):
        ser = _resolve_dongle_serial(dongle, "")
        if not ser:
            raise RFError(f"Unknown dongle '{dongle}'. Dongles: {', '.join(_short(s) for s in DONGLE_NAMES)}.")
        if ser in busy:
            raise RFError(f"{_short(ser)} is already busy with a background capture; stop it first.")
        order, why = [ser], "the dongle you asked for"
    else:
        free = [s for s in _working_serials(cfg) if s not in busy]
        if not free:
            raise RFError("Every dongle is busy with a background capture. Stop one first ('stop the capture').")
        free = _covering(free, f) or free     # receivers that tune it (an RTL-SDR stops at 1766 MHz)
        recent = _strongest_recently(f)
        if recent in free:
            best, why = recent, "it heard this frequency best in the last investigation"
        elif len(free) == 1:
            best, why = free[0], "the only free dongle"
        else:
            best, why = _quick_compare(f, free, cfg)
            if best is None:
                best, why = _best_for_band_why(f, f, cfg, among=free)
        order = [best] + [s for s in free if s != best]
    for i, ser in enumerate(order):
        lock = _device_lock(ser)
        if lock.acquire(20.0 if i == 0 else 2.0, "background capture"):
            return ser, lock, (why if i == 0 else f"{_short(order[0])} was busy, so the next free one")
    raise RFError(f"{', '.join(_short(s) for s in order)} {'is' if len(order) == 1 else 'are all'} busy right now "
                  f"({_device_lock(order[0]).holder or 'another ARIS window'}); try again in a moment.")


def _gui_call(path, body, timeout=90.0):
    """Ask the GUI process to do something (it hosts background captures, so they survive a chat reset).
    None when there is no GUI to ask (terminal ARIS) or it cannot be reached."""
    tok, url = os.environ.get("ARIS_GUI_TOKEN"), os.environ.get("ARIS_GUI_URL")
    if GUI_HOST or not tok or not url:
        return None
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url.rstrip("/") + path, data=json.dumps(body, default=_json_default).encode(),
                                 method="POST", headers={"Content-Type": "application/json", "X-ARIS": "1",
                                                         "X-ARIS-Internal": tok})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))     # never through a proxy
    try:
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except Exception:
            return {"status": "error", "error": f"the GUI answered HTTP {e.code}"}
    except (OSError, ValueError) as e:
        log.warning("GUI call %s failed (%s); doing it here", path, e)
        return None
