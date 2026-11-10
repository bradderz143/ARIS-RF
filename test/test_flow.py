"""The whole chat path on simulated radios, through a real GUI server: the GUI starts the chat console (scripted
AI); a capture asked for in the chat runs in the GUI process (so a chat reset does not end it); its result appears
in the chat by itself; the slash commands work through the bridge.   python3 -m unittest test_flow  (about a minute)"""
import http.cookiejar
import json
import os
import subprocess
import sys
import time
import unittest
import urllib.parse
import urllib.request

import harness

PORT = int(os.environ.get("ARIS_FLOW_PORT", "5098"))
BASE = f"http://127.0.0.1:{PORT}"
PW = os.environ.get("ARIS_TEST_PASSWORD", "test-password-123")


class Flow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = dict(os.environ, FAKE_WD_PERIOD="1")
        for k in ("FAKE_SRC_POS", "FAKE_NODE_POS"):
            env.pop(k, None)
        cls.log = open(os.path.join(harness.DATA, "flow_server.log"), "w")
        cls.proc = subprocess.Popen([sys.executable, os.path.join(harness.TEST_DIR, "serve.py"), str(PORT)],
                                    env=env, stdout=cls.log, stderr=subprocess.STDOUT, cwd=harness.TEST_DIR)
        jar = http.cookiejar.CookieJar()
        cls.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), urllib.request.ProxyHandler({}))
        t0 = time.time()
        while time.time() - t0 < 90:
            try:
                cls.op.open(BASE + "/login", timeout=3).read()
                break
            except OSError:
                time.sleep(0.5)
        form = urllib.parse.urlencode({"username": "admin", "password": PW}).encode()
        cls.op.open(BASE + "/login", data=form, timeout=10).read()

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.log.close()

    def get(self, path):
        with self.op.open(BASE + path, timeout=30) as r:
            return json.loads(r.read().decode())

    def post(self, path, body=None):
        req = urllib.request.Request(BASE + path, data=json.dumps(body or {}).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-ARIS": "1"})
        with self.op.open(req, timeout=120) as r:
            return json.loads(r.read().decode())

    def chat(self, text, until, limit=120):
        """Send a chat message; wait until a reply after it contains `until`. Returns the replies."""
        n0 = len(self.get("/api/chat_state?seq=-1")["messages"])
        self.post("/api/chat_send", {"message": text})
        t0 = time.time()
        while time.time() - t0 < limit:
            msgs = self.get("/api/chat_state?seq=-1")["messages"][n0:]
            ai = [m["text"] for m in msgs if m["role"] == "ai"]
            if any(until in t for t in ai):
                return ai
            time.sleep(0.5)
        self.fail(f"no reply containing {until!r} to {text!r}; got {ai}")

    def test_1_capture_reports_by_itself(self):
        ai = self.chat("/capture 96.9", "Capturing 96.9 MHz")
        ai = self.chat("/captures", "96.9", limit=5) if not any("finished" in t for t in ai) else ai
        t0 = time.time()
        while time.time() - t0 < 60:                       # the result arrives as its own chat message
            msgs = [m["text"] for m in self.get("/api/chat_state?seq=-1")["messages"] if m["role"] == "ai"]
            done = [t for t in msgs if t.startswith("Capture on 96.9 MHz")]
            if done:
                break
            time.sleep(0.5)
        self.assertTrue(done, msgs[-3:])
        self.assertIn('"HITS FM"', done[-1])

    def test_2_capture_survives_chat_reset(self):
        ai = self.chat("keep logging 433.92", "Logging 433.92 MHz continuously")
        self.post("/api/chat_reset", {"scope": "chat"})    # the chat's Stop: restarts only the chat console
        time.sleep(3)
        run = self.get("/api/activity")["watchdog"]["watchdogs_running"]
        self.assertTrue(any(abs(w["freq_mhz"] - 433.92) < 0.01 for w in run), (ai, run))
        self.assertTrue(self.post("/api/capture/stop", {"freq_mhz": 433.92})["stopped"])
        self.assertFalse(self.get("/api/activity")["watchdog"]["watchdogs_running"])

    def test_4_reset_scanner_stops_everything(self):
        self.chat("keep logging 433.92", "Logging 433.92 MHz continuously")
        n0 = len(self.get("/api/chat_state?seq=-1")["messages"])
        self.post("/api/chat_reset")                       # Reset Scanner
        t0, said = time.time(), []
        while time.time() - t0 < 60 and not said:
            said = [m["text"] for m in self.get("/api/chat_state?seq=-1")["messages"][n0:]
                    if m["role"] == "ai" and "Scanner reset" in m["text"]]
            time.sleep(0.5)
        self.assertTrue(said)
        self.assertIn("Stopped the capture on 433.92 MHz", said[-1])
        self.assertIn("are free", said[-1])
        self.assertFalse(self.get("/api/activity")["watchdog"]["watchdogs_running"])
        self.chat("/check", "System check")                # the chat works again

    def test_3_slash_commands(self):
        self.chat("/check", "System check")
        ai = self.chat("/investigate 433.92 5s", "433.92 MHz")
        self.assertIn("listened", ai[-1])

    def test_5_investigation_is_answered_without_the_ai(self):
        # a routed investigation in the chat: the reply is built from the result (no AI round, so the busy line never
        # says '[AI] Writing the answer...'), and it is the reply the Decode card and /investigate give
        n0 = len(self.get("/api/chat_state?seq=-1")["messages"])
        self.post("/api/chat_send", {"message": "investigate 433.92 for 4 seconds"})
        t0, seen, ai = time.time(), set(), []
        while time.time() - t0 < 120:
            st = self.get("/api/chat_state?seq=-1")
            seen.add(str(st.get("status") or ""))
            ai = [m["text"] for m in st["messages"][n0:] if m["role"] == "ai"]
            if ai:
                break
            time.sleep(0.3)
        self.assertTrue(ai, seen)
        self.assertIn("433.92 MHz, listened", ai[-1])
        self.assertIn("Band plan: 433.92 MHz ISM Band", ai[-1])
        self.assertIn("Dongle 1:", ai[-1])
        self.assertNotIn("[fake AI]", ai[-1])                              # the scripted Ollama was never asked
        self.assertFalse([s for s in seen if s.startswith("[AI]")], seen)
        # the radio steps showed on the busy line meanwhile
        self.assertTrue(any(s.startswith(("[RF Action]", "[HW", "[LISTEN", "[DECODE")) for s in seen), seen)


if __name__ == "__main__":
    unittest.main()
