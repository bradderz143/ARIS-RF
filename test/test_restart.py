"""The web server starting itself again. Reset Scanner finds a dongle that a stuck task of the server's own process
holds; only a fresh process frees it, so the server re-executes itself. It must come back on the same port (its old
listening socket must not live on into the new program) and say what it did. A real server (serve.py) on a free
port.  python3 -m unittest test_restart"""
import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PW = "test-password-123"


class Restart(unittest.TestCase):
    def test_reset_scanner_with_a_stuck_dongle_restarts_the_server_on_the_same_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        base = f"http://127.0.0.1:{port}"
        out = tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False)
        env = dict(os.environ, ARIS_GUI_PORT=str(port), ARIS_TEST_PASSWORD=PW, SERVE_STUCK="1")
        srv = subprocess.Popen([sys.executable, os.path.join(HERE, "serve.py")], cwd=HERE, env=env, stdout=out,
                               stderr=subprocess.STDOUT)
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

        def up():
            try:
                return op.open(base + "/login", timeout=2).status == 200
            except OSError:
                return False

        def login():
            form = urllib.parse.urlencode({"username": "admin", "password": PW}).encode()
            op.open(urllib.request.Request(base + "/login", data=form), timeout=10)
        try:
            end = time.time() + 90
            while not up() and time.time() < end:
                time.sleep(0.3)
            self.assertTrue(up(), "the server did not start")
            login()
            req = urllib.request.Request(base + "/api/chat_reset", data=b"{}",
                                         headers={"X-ARIS": "1", "Content-Type": "application/json"})
            self.assertTrue(json.loads(op.open(req, timeout=60).read().decode()).get("ok"))
            down, back, end = False, False, time.time() + 90
            while time.time() < end:
                if not up():
                    down = True
                elif down:
                    back = True
                    break
                time.sleep(0.3)
            self.assertTrue(down, "the server never restarted")
            self.assertTrue(back, "the server did not come back after restarting itself")
            self.assertIsNone(srv.poll())                        # the same process slot, still running
            login()                                              # (it serves pages again, not only the port)
            st = json.loads(op.open(base + "/api/chat_state?seq=-1", timeout=10).read().decode())
            self.assertIn("messages", st)       # (the simulation's data folder is new after a restart, so the
            out.flush()                         # note the real server shows in the chat is not looked for here)
            with open(out.name) as f:
                self.assertNotIn("Address already in use", f.read())
        finally:
            srv.kill()
            srv.wait()
            out.close()
            os.unlink(out.name)


if __name__ == "__main__":
    unittest.main()
