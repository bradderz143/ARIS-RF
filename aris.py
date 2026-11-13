#!/usr/bin/env python3
"""
ARIS v4.17.0 - passive (receive-only) multi-receiver RF monitoring station.

Run:      python3 aris.py                     (first time: type /check, then /setup)
Tidy up:  python3 aris.py --cleanup           (lists old files from earlier versions, asks before deleting)
Needs:    pip install ollama numpy, and the tools for your receivers: the rtl-sdr tools for RTL-SDR dongles,
          SoapySDR and rx_tools for other SDRs, or a device plug-in (plugins/). rtl_433 decodes sensors; decoders
          are used automatically when installed: multimon-ng (pagers), direwolf's atest (APRS), rtl_adsb (aircraft).
Offline:  everything runs on this computer. The AI is your local Ollama at http://127.0.0.1:11434. The one thing
          that reaches out: a read-only check of this code's own git remote every few hours, for the page's
          update badge (config "update_check": false or ARIS_UPDATE_CHECK=0 switches it off).
Env:      ARIS_MODEL, ARIS_NUM_CTX, ARIS_DATA_DIR, ARIS_DB, ARIS_OLLAMA_HOST, ARIS_PLUGIN_DIRS, ARIS_UPDATE_CHECK
"""
from __future__ import annotations

import atexit
import binascii
import copy
import fcntl
import functools
import hmac
import inspect
import json
import logging
import logging.handlers
import math
import os
import re
import secrets
import shlex
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import wave
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import ollama

os.environ["PATH"] = str(Path.home() / ".local" / "bin") + os.pathsep + os.environ.get("PATH", "")
_HERE = str(Path(__file__).resolve().parent)      # arislib/ lives next to this file (also when run via a link)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from arislib import radioproc      # noqa: E402  (stopping radio programs: see _end_radio)
from arislib import radios         # noqa: E402  (receivers of every kind: see _radio_cmd)

VERSION = "4.17.0"


# ---------------------------------------------------------------------------------------------------------------
# The engine is one program in one namespace, kept in parts under engine/ (v4.9.1: aris.py had 10,000 lines).
# Each part is plain Python executed into this module's namespace, in order, so every function sees the same
# globals, the same locks and the same tool registry as before, and the GUI (arisweb/engine.py), the tests and
# the console still reach everything as aris.<name> (and can still patch it there). Tracebacks name the part file.
# A part never redefines a name another part defined (test_engine checks).
# ---------------------------------------------------------------------------------------------------------------
ENGINE_DIR = os.path.join(_HERE, "engine")
ENGINE_PARTS = ("core", "sweep", "decode", "investigate", "fm", "autosetup", "audit", "logs", "captures", "band", "status", "chat", "plugins")


def _include(part):
    """Run engine/<part>.py in this module's namespace."""
    path = os.path.join(ENGINE_DIR, part + ".py")
    with open(path, encoding="utf-8") as f:
        code = compile(f.read(), path, "exec")
    doc = globals().get("__doc__")
    exec(code, globals())           # noqa: S102 - our own source files, next to this one
    globals()["__doc__"] = doc      # a part's docstring must not replace this file's


_include("core")

_include("sweep")

_include("decode")

_include("investigate")

_include("fm")

_include("autosetup")

_include("audit")

_include("logs")

_include("captures")

_include("band")

_include("status")

_include("chat")

_include("plugins")

try:
    _load_config()                  # dongle numbering from the saved config, before anything lists the dongles
    _sweep_audio_leftovers()        # demodulated audio a killed process left in memory / tmp: never kept
    _load_plugins()                 # receiver types, tools and bands from plugins/ (a broken one is skipped)
    _first_start_devices()          # a fresh data folder: find the receivers plugged in (no serials built in)
except Exception as _e:            # noqa: BLE001 - never stop ARIS from starting
    log.warning("config not loaded at start: %s", _e)

if __name__ == "__main__":
    main()
