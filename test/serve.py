"""Run the GUI against the simulated radios (for screenshots and manual poking).
python3 serve.py [port]   login: admin / $ARIS_TEST_PASSWORD (default 'test-password-123')"""
import json
import os
import sys

import harness
from werkzeug.security import generate_password_hash

# SERVE_FAKE_ANT: other simulated antennas for this run (e.g. every dongle hearing the FM band, for the README shots)
if os.environ.get("SERVE_FAKE_ANT"):
    os.environ["FAKE_ANT"] = os.environ["SERVE_FAKE_ANT"]

# The FM view gets every kind of station that "Name all FM stations" tells apart: mono with RDS, an RDS coder on its
# own clock, a stereo station that sends no RDS, and an FM transmitter on an even tenth (not a station channel).
os.environ["FAKE_IQ"] += ",92.1:wbfm_monords:30:NOTICIAS,99.3:wbfm_unlocked:28:LA K,93.7:wbfm_nords:28,94.8:wbfm_mono:22"
os.environ["FAKE_PEAKS"] += ",92.1:30:150,99.3:28:150,93.7:30:150,94.8:26:150"
# ... and one the sweep sees but the IQ look does not hear (99.7: "not heard in the last check", v4.9.3)
os.environ["FAKE_PEAKS"] += ",99.7:22:150"

pw = os.environ.get("ARIS_TEST_PASSWORD", "test-password-123")
with open(os.path.join(harness.DATA, "gui_secrets.json"), "w") as f:
    json.dump({"admin_user": "admin", "admin_hash": generate_password_hash(pw), "secret_key": "s" * 32}, f)
os.environ["ARIS_GUI_PORT"] = sys.argv[1] if len(sys.argv) > 1 else "5097"
os.environ["ARIS_GUI_HOST"] = "127.0.0.1"
print("data:", harness.DATA, flush=True)
from arisweb import app  # noqa: E402
app.PORT = int(os.environ["ARIS_GUI_PORT"])
app.HOST = "127.0.0.1"
if os.environ.get("ARIS_SEED_DEVICES"):          # the Devices tab with example devices (screenshots, poking)
    import seed_devices
    from arisweb.engine import engine
    seed_devices.seed(engine())
sys.argv = [sys.argv[0]]
app.main()
