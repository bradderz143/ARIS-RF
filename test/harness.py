"""Shared set-up for the automated tests: fake radio tools on PATH, a scripted Ollama, a fresh data folder.
Import this before anything from ARIS. The simulated room has a transmitter at 433.92 MHz at x=7, y=2 m."""
import os
import sys
import tempfile

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
CODE_DIR = os.path.dirname(TEST_DIR)
DATA = tempfile.mkdtemp(prefix="aris_test_")

os.environ.update({
    "ARIS_DATA_DIR": DATA, "ARIS_DB": os.path.join(DATA, "sigint.db"), "ARIS_WATCHDOG_LOG": os.path.join(DATA, "wd.jsonl"),
    "ARIS_FM_LOOK_S": "2", "ARIS_FM_RETRY_S": "4",       # Name all: short looks (the fake radio is slow to simulate)
    "ARIS_SDR_START_S": "60",                             # the fake rtl_sdr computes a whole capture before sending
    "ARIS_BAND_DWELL_S": "2",                             # band captures: short visits to each slice of a wide band
    "PATH": os.path.join(TEST_DIR, "fake", "bin") + os.pathsep + os.environ.get("PATH", ""),
    "FAKE_ANT": "00001001:fm=0,433=0,915=-25,1090=-20;00003003:fm=-18,433=-6,915=-10,1090=-8;"
                "00004004:fm=x,433=3,915=0,1090=0",
    "FAKE_PEAKS": "88.9:30:150,96.9:32:150,101.7:28:150,105.3:30:150,921.6:15:5",
    "FAKE_BURST": "433.92:18:0.5,914.842:16:0.4",
    "FAKE_IQ": "88.9:wbfm:30:ECHO 88,96.9:wbfm:32:HITS FM,101.7:wbfm:28:BEAT 101,105.3:wbfm:30:WAVE,433.92:ook:18,914.842:ook:16,921.6:cw:15",
    "FAKE_433": "433.92:ev1527:22,914.842:unk28:20", "FAKE_ADSB": "3", "FAKE_PPM_TRUE": "0",
    "FAKE_SRC_POS": "433.92:7:2", "FAKE_NODE_POS": "00001001:1:1;00003003:9:1;00004004:5:7",
})
os.environ.pop("FAKE_ANTENNA", None)
# The simulated station: three RTL-SDR dongles (serials 00001001, 00003003, 00004004), already found by /check and
# numbered Dongle 1-3, as config.json records them. Nothing is built into ARIS itself (DEFAULT_DONGLES is empty).
SIM_DONGLES = ["00001001", "00003003", "00004004"]
import json as _json
with open(os.path.join(DATA, "config.json"), "w") as _f:
    _json.dump({"dongle_order": SIM_DONGLES}, _f)
# The fake rtl_433 / multimon-ng / atest hand recordings to the real decoders (built from source) to decode.
REAL_BIN = next((d for d in (os.environ.get("REAL_BIN"), os.path.expanduser("~/realbin"),
                             "/usr/local/bin", "/usr/bin")
                 if d and os.path.isfile(os.path.join(d, "rtl_433"))), None)
if REAL_BIN:
    os.environ["REAL_BIN"] = REAL_BIN
os.environ["PYTHONPATH"] = os.pathsep.join([os.path.join(TEST_DIR, "fake", "mods"), CODE_DIR])
os.environ.setdefault("ARIS_UPDATE_CHECK", "0")      # tests never ask GitHub; test_engine.UpdateCheck turns it on
for p in (CODE_DIR, os.path.join(TEST_DIR, "fake", "mods")):
    if p not in sys.path:
        sys.path.insert(0, p)
NODE_POS = {"00001001": (1, 1), "00003003": (9, 1), "00004004": (5, 7)}
