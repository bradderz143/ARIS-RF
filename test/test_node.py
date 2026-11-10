"""Remote node agent (aris_node.py) on simulated radios: token check, measuring, and ARIS using it on the map."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

import harness
from werkzeug.security import generate_password_hash

from arisweb import app as webapp

PORT = 5061
NODE_DIR = tempfile.mkdtemp(prefix="aris_node_")
H = {"X-ARIS": "1"}


def _token():
    with open(os.path.join(NODE_DIR, "config.json")) as f:
        return json.load(f)["token"]


class Node(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = dict(os.environ, ARIS_NODE_DIR=NODE_DIR)
        node = os.path.join(harness.CODE_DIR, "aris_node.py")
        subprocess.run([sys.executable, node, "--init", "--name", "Test Pi", "--port", str(PORT), "--serial",
                        "00003003", "--bind", "127.0.0.1"], env=env, check=True, capture_output=True)
        cls.proc = subprocess.Popen([sys.executable, node], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1)
            except urllib.error.HTTPError:
                break                                     # 401 = it is up
            except OSError:
                time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)

    def test_token_required(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=5)
        self.assertEqual(cm.exception.code, 401)
        self.assertEqual(oct(os.stat(os.path.join(NODE_DIR, "config.json")).st_mode & 0o777), "0o600")

    def test_aris_uses_remote_node(self):
        appl = webapp.configure({"admin_user": "a", "admin_hash": generate_password_hash("pw-12345678"),
                                 "secret_key": "q" * 32})
        c = appl.test_client()
        c.post("/login", data={"username": "a", "password": "pw-12345678"})
        n = c.post("/api/map/remote", json={"name": "Test Pi", "url": f"http://127.0.0.1:{PORT}", "token": _token()},
                   headers=H).get_json()
        self.assertEqual(n["kind"], "remote")
        h = c.get(f"/api/map/node/{n['id']}/health").get_json()
        self.assertTrue(h["ok"], h)
        self.assertIn("00003003", h["dongles"])
        st = c.get("/api/map/state").get_json()
        for x in st["nodes"]:
            c.post(f"/api/map/node/{x['id']}", json={"enabled": x["id"] == n["id"]}, headers=H)
        c.post(f"/api/map/node/{n['id']}", json={"x": 9, "y": 1}, headers=H)
        m = c.post("/api/map/measure", json={"freq_mhz": 433.92, "dwell_s": 2, "new_session": True},
                   headers=H).get_json()
        rows = {r["node_id"]: r for r in m["this_round"]}
        self.assertIn(n["id"], rows, m)
        self.assertTrue(rows[n["id"]]["heard"], rows)
        for x in st["nodes"]:
            c.post(f"/api/map/node/{x['id']}", json={"enabled": True}, headers=H)
        self.assertEqual(c.delete(f"/api/map/node/{n['id']}", headers=H).status_code, 200)


if __name__ == "__main__":
    unittest.main()
