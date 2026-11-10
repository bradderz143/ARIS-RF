"""The AI chat: the aris.py console runs on a pseudo-terminal and this bridge relays each question and its reply.
Slash commands go straight to the console; questions about saved signals get a short list of recent ones as
context. Background results (a capture that finished) arrive as notices (DATA_DIR/notices.jsonl) and are added
to the chat as soon as they are written, whichever ARIS process wrote them."""
from __future__ import annotations

import json
import os
import re
import select
import signal
import sqlite3
import subprocess
import sys
import termios
import threading
import time
import pty
from datetime import datetime

from .engine import ARIS_SCRIPT, engine as _engine

PROMPT = "[You] >"
REPLY_MARK = "[ARIS] >"
ABOUT_SAVED = re.compile(r"\b(signals?|saved|found|detected|records?|devices?|bugs?|trackers?|transmitters?)\b",
                         re.I)
_NUM_TIME = r"\s+\d+(?:\.\d+)?\s*(?:mhz)?(?:\s+\d+(?:\.\d+)?\s*[a-z]*)?"
SLASH_COMMANDS = re.compile(r"^/(setup|status|help|selftest|check|nodes|plugins|clear-signals|cleanup(?: confirm)?|"
                            r"patrol(?: (?:start|stop|status))?|captures|capture(?: stop(?: all)?| status|"
                            + _NUM_TIME + r")?|investigate" + _NUM_TIME + r"|devices(?: .+)?)$", re.I)
MAX_USER_CHARS = 800


def not_a_chat_command(text) -> bool:
    """A console command the chat does not offer ('/run <tool> ...', '/raw', '/reset', 'exit'): never sent to the
    console (a logged-in page could run every engine tool with any arguments through it; v4.9.4)."""
    t = re.sub(r"[\x00-\x1f\x7f]", " ", str(text or "")).strip()
    return bool(t) and not SLASH_COMMANDS.match(t) and (t.startswith("/") or t.lower() in ("exit", "quit"))
MAX_FACT_CHARS = 1500       # the terminal line limit is 4096 chars; keep the whole prompt well under
MAX_MESSAGES = 200
MAX_TURN_S = 3600           # longest wait for one reply (a long band scan with long listens per signal)


def _lan_ips():
    """Non-loopback IPv4 addresses (Wi-Fi, Ethernet, USB tethering) so the phone knows where to connect."""
    out = []
    try:
        txt = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=3).stdout
        for m in re.finditer(r"^\d+:\s+(\S+)\s+inet\s+([\d.]+)/", txt, re.M):
            if not m.group(2).startswith("127."):
                out.append((m.group(1), m.group(2)))
    except Exception:
        pass
    if not out:
        try:
            txt = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3).stdout
            out = [("network", ip) for ip in txt.split() if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip)
                   and not ip.startswith("127.")]
        except Exception:
            pass
    return out


def _clean_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", s).replace("\r", "")


FACT_CHARS = 220            # one saved record's verdict for the model


def _fact_line(freq, snr, analysis, when=None) -> str:
    """One saved record for the model: its verdict without the band-plan guess and the advice, cut at a word
    (v4.8.6 cut the first line at 110 characters, '1) Renault, a tyre-pr', and the model invented the other two
    devices), plus the decoded models."""
    lines = (analysis or "").splitlines()
    head = re.sub(r"^\W*ARIS verdict \(([^)]*)\):\s*", r"(\1) ", lines[0]) if lines else ""
    head = re.split(r" \(band: |\. To identify it, capture it", head)[0]
    if len(head) > FACT_CHARS:
        head = head[:FACT_CHARS].rsplit(" ", 1)[0].rstrip(",;:") + " ..."
    models = next((ln.split(":", 1)[1].strip() for ln in lines if "rtl_433 confirmed decode:" in ln), "")
    return (f"{freq:.4f}MHz" + (f" saved {when[:16]}" if when else "") + f" snr={snr}dB [{head}]"
            + (f" decoded: {models}" if models else ""))


def _recent_facts(limit=5) -> str:
    """Compact saved-signal facts for the model (kept short on purpose)."""
    try:
        conn = sqlite3.connect(_engine().DB_PATH, timeout=5)
        try:
            rows = conn.execute("SELECT frequency_mhz, power_db, snr_db, urh_analysis, notes, timestamp FROM peaks "
                                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        finally:
            conn.close()
    except Exception:
        return ""
    parts = [_fact_line(freq, snr, analysis, when) for freq, _pwr, snr, analysis, _notes, when in rows]
    return " | ".join(parts)[:MAX_FACT_CHARS]


class ArisAIBridge:
    def __init__(self, extra_env=None):
        self.extra_env = dict(extra_env or {})   # how the console reaches this GUI process (captures run here)
        self.proc = None
        self.master_fd = None
        self.lock = threading.Lock()          # one question at a time
        self.state_lock = threading.Lock()
        self.gen = 0                          # bumped by reset so stale workers stay quiet
        self.seq = 1
        self.messages = [{
            "role": "ai",
            "text": ("Hey! I'm connected to your SDR scanner and your saved signals.\n\n"
                     "Ask in plain English, for example:\n"
                     "• \"Investigate 433.92 MHz for 2 minutes\" (listens on every dongle at once)\n"
                     "• \"Capture 433.92\" (keeps the best dongle on it until a packet decodes; the result "
                     "appears here)\n"
                     "• \"Scan the 433 MHz band and investigate everything\"\n"
                     "• \"Where is 433.92 coming from?\" (needs antennas placed on the Map tab)\n"
                     "• \"Check the system\" (dongles, reception, decoders)\n\n"
                     "On the Spectrum tab, tap a signal and press Decode."),
            "time": datetime.now().strftime("%H:%M"),
        }]
        self.busy = False
        self.status_line = "Starting..."
        self.notice_pos = None                # read position in the notices file (None = start at its end)
        self.notice_lock = threading.Lock()   # page polls arrive on several threads at once
        self.last_notice = None               # given to the AI with the next question, so follow-ups work
        self.pull_notices()
        self.start_process()

    def _add(self, role, text):
        self.messages.append({"role": role, "text": text, "time": datetime.now().strftime("%H:%M")})
        del self.messages[:-MAX_MESSAGES]
        self.seq += 1                         # the page only downloads the messages when this changes

    def pull_notices(self):
        """New lines of the notices file -> chat messages. Cheap when nothing changed (one stat call)."""
        if not self.notice_lock.acquire(blocking=False):
            return                            # another poll is reading them right now
        try:
            self._pull_notices()
        finally:
            self.notice_lock.release()

    def forget_notices(self):
        """Clear logs deleted the notices file: whatever is written next is new, read it from the start."""
        with self.notice_lock:
            self.notice_pos = 0

    def _pull_notices(self):
        try:
            path = _engine().NOTICES
            size = os.path.getsize(path)
        except Exception:
            self.notice_pos = 0                   # no file yet (or Clear logs removed it): the next one is new
            return
        if self.notice_pos is None or size < self.notice_pos:   # first look, or the file was trimmed:
            self.notice_pos = size                              # only what is written from now on
            return
        if size == self.notice_pos:
            return
        try:
            with open(path, "rb") as f:
                f.seek(self.notice_pos)
                data = f.read(size - self.notice_pos)
        except OSError:
            return
        end = data.rfind(b"\n")
        if end < 0:
            return                            # a line is still being written
        self.notice_pos += end + 1
        for line in data[:end].decode("utf-8", errors="replace").splitlines():
            try:
                n = json.loads(line)
            except ValueError:
                continue
            if isinstance(n, dict) and n.get("text"):
                with self.state_lock:
                    self._add("ai", str(n["text"]))
                self.last_notice = str(n["text"])[:500]

    def start_process(self):
        old = self.proc
        if old is not None:
            _end_console(old, 5)                 # with every radio program it started, before a new one starts
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
        master_fd, slave_fd = pty.openpty()
        try:
            attrs = termios.tcgetattr(slave_fd)
            attrs[3] &= ~termios.ECHO            # do not echo our own prompt back into the reply
            termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
        except termios.error:
            pass
        env = os.environ.copy()
        env["PATH"] = os.path.join(os.path.expanduser("~"), ".local", "bin") + os.pathsep + env.get("PATH", "")
        env["PYTHONUNBUFFERED"] = "1"
        env.update(self.extra_env)
        env["ARIS_GUI_CHILD"] = "1"           # background captures run in the GUI process (see aris._gui_call)
        self.proc = subprocess.Popen([sys.executable, "-u", ARIS_SCRIPT], stdin=slave_fd, stdout=slave_fd,
                                     stderr=slave_fd, close_fds=True, env=env, preexec_fn=os.setsid)
        os.close(slave_fd)
        self.master_fd = master_fd
        self._drain_until_prompt(timeout=30)
        self.status_line = "Ready"
        self.busy = False

    def _drain_until_prompt(self, timeout=15):
        buf, end = "", time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([self.master_fd], [], [], 0.25)
            if r:
                try:
                    chunk = os.read(self.master_fd, 4096).decode("utf-8", errors="ignore")
                except OSError:
                    break
                buf += _clean_ansi(chunk)
                if PROMPT in buf:
                    break
        return buf

    def ask_async(self, user_text: str, role: str = "admin") -> bool:
        with self.state_lock:
            if self.busy:
                self._add("ai", "I'm still working on your last request. Give me a moment, or press Stop.")
                return False
            self._add("user", user_text)
            if not_a_chat_command(user_text):
                # any other console command (/run <tool> ..., /raw, /reset, exit) stays out of the console: a
                # logged-in page could otherwise run every engine tool with any arguments (v4.9.4)
                self._add("ai", "That is not a chat command here. Type /help for the ones the chat knows.")
                return True
            self.busy = True
            self.status_line = "Thinking..."
            gen = self.gen
        threading.Thread(target=self._worker, args=(user_text, gen, role), daemon=True).start()
        return True

    def _worker(self, user_text: str, gen: int, role: str = "admin"):
        with self.lock:
            if gen != self.gen:
                return
            if not self.proc or self.proc.poll() is not None:
                self.start_process()

            # Throw away any stale console output so it cannot be mistaken for this reply.
            while select.select([self.master_fd], [], [], 0)[0]:
                try:
                    if not os.read(self.master_fd, 4096):
                        break
                except OSError:
                    break

            clean = re.sub(r"[\x00-\x1f\x7f]", " ", user_text)[:MAX_USER_CHARS].strip()
            clean = re.sub(r"\(Context[,:]", "(context", clean)       # (the GUI's own context marker, not the user's)
            note, self.last_notice = self.last_notice, None
            if SLASH_COMMANDS.match(clean):
                wrapped = clean.lower()          # console command: send as-is, no AI involved
            elif not_a_chat_command(clean):      # (ask_async refuses these before the console is touched)
                wrapped = "(nothing)"
            elif ABOUT_SAVED.search(clean):
                # Only questions about signals get the saved records; otherwise the AI pads every answer with them.
                facts = _recent_facts()
                wrapped = (f"{clean} (Context, only if relevant: saved records, newest first: {facts or 'none yet'}. "
                           f"Device IDs in them are unverified guesses unless rtl_433 decoded them. "
                           f"Never invent numbers.)")
            else:
                wrapped = clean
            if note and not SLASH_COMMANDS.match(clean) and len(wrapped) + len(note) < 3000:
                wrapped += f" (Context, only if relevant: a background capture reported: {note})"
            if role != "admin":                  # the console refuses the set-up tools and commands for this line
                wrapped = f"[role:{re.sub(r'[^a-z]', '', str(role).lower()) or 'viewer'}] " + wrapped
            try:
                os.write(self.master_fd, (wrapped + "\n").encode("utf-8"))
            except OSError:
                self.start_process()
                os.write(self.master_fd, (wrapped + "\n").encode("utf-8"))

            buf, start_t = "", time.time()
            while time.time() - start_t < MAX_TURN_S and gen == self.gen:
                r, _, _ = select.select([self.master_fd], [], [], 0.4)
                if not r:
                    continue
                try:
                    chunk = os.read(self.master_fd, 4096).decode("utf-8", errors="ignore")
                except OSError:
                    break
                text = _clean_ansi(chunk)
                buf += text
                for line in text.splitlines():
                    ls = line.strip()
                    if ls.startswith(("[RF Action]", "[HW", "[LISTEN", "[DECODE", "-> ", "[SETUP", "[CAL", "[CHECK",
                                      "[AI]")):
                        self.status_line = ls[:90]
                if PROMPT in buf:
                    break

            if gen != self.gen:      # reset happened while we were working
                return
            if REPLY_MARK in buf:
                reply = buf.rsplit(REPLY_MARK, 1)[1]
                if PROMPT in reply:
                    reply = reply.rsplit(PROMPT, 1)[0]
            else:
                lines = [ln for ln in buf.splitlines() if not ln.strip().startswith("[You]")]
                reply = "\n".join(lines[-15:])
            self._add("ai", reply.strip() or "I finished checking. What would you like to look at next?")
            self.status_line = "Ready"
            self.busy = False

    def reset(self, stop=None, clear=False):
        """Reset Scanner: stop(), which stops the GUI's own jobs and captures and returns what it stopped, then a
        fresh chat console (which also ends whatever the old one was doing)."""
        with self.state_lock:
            self.gen += 1
            self.busy = True
            self.status_line = "Resetting: stopping every task..."
        note = None
        if stop is not None:
            try:
                note = stop()
            except Exception as e:
                note = f"Scanner reset (a problem while stopping: {type(e).__name__}: {e})."
        self.status_line = "Resetting: restarting the AI..."
        self.start_process()
        with self.state_lock:
            if clear:                            # Clear data: the chat starts afresh (only the greeting stays)
                del self.messages[1:]
                self.last_notice = None
            self._add("ai", (note + " The AI is restarted and ready for your next question.") if note else
                      "Stopped. The AI is restarted and ready for your next question (captures keep running).")


def _end_console(proc, wait_s) -> None:
    """End the chat console and the radio programs it started (its own process group, from setsid): SIGTERM to all
    of them, then SIGKILL for whatever is still there, and wait until they have exited (arislib/radioproc.py). The
    rtl-sdr tools' stop can hang (librtlsdr's cancel; seen on the real PC): such a program used to outlive the
    console for ever, holding its dongle, while the dongle's lock (the console's) was already free."""
    try:
        from arislib import radioproc
        radioproc.end_group(proc, term_wait=wait_s)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass


def stop_child(bridge) -> None:
    """Stop the ARIS console (and any radio tools it started) when the GUI exits."""
    proc = getattr(bridge, "proc", None)
    if proc is not None:
        _end_console(proc, 8)
