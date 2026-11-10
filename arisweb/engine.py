"""Load the ARIS engine (aris.py) once inside the GUI process.

ARIS_SCRIPT picks the file. By default that is the aris.py next to this package (the checkout); ~/aris.py is used
only if there is none. Links are followed, so arislib/ is found next to the real file.
"""
from __future__ import annotations

import importlib.util
import os
import re
import sys
import threading
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent


def script_path() -> str:
    env = os.environ.get("ARIS_SCRIPT")
    if env:
        return str(Path(env).resolve())
    here = _REPO / "aris.py"
    return str(here if here.is_file() else (Path.home() / "aris.py").resolve())


ARIS_SCRIPT = script_path()
_ENGINE = None
_LOCK = threading.Lock()


_VERSION = {"key": None, "v": "?"}


def version() -> str:
    """VERSION from aris.py (re-read only when the file changes, e.g. after an update)."""
    try:
        st = os.stat(ARIS_SCRIPT)
        key = (st.st_mtime_ns, st.st_size)
        if _VERSION["key"] != key:
            with open(ARIS_SCRIPT) as f:
                for line in f:                      # VERSION is near the top: stop reading there
                    m = re.match(r'^VERSION = "([^"]+)"', line)
                    if m:
                        _VERSION["v"] = m.group(1)
                        break
            _VERSION["key"] = key
        return _VERSION["v"]
    except OSError:
        return "?"


def engine():
    """The engine module (loaded on first use). Raises if aris.py cannot be loaded."""
    global _ENGINE
    with _LOCK:
        if _ENGINE is None:
            d = str(Path(ARIS_SCRIPT).parent)
            if d not in sys.path:
                sys.path.insert(0, d)
            spec = importlib.util.spec_from_file_location("aris_engine", ARIS_SCRIPT)
            mod = importlib.util.module_from_spec(spec)
            sys.modules["aris_engine"] = mod          # tools that pass the engine around find it here
            try:
                spec.loader.exec_module(mod)
            except Exception:
                sys.modules.pop("aris_engine", None)
                raise
            _ENGINE = mod
            _attach_log(mod)
        return _ENGINE


def _attach_log(mod) -> None:
    """The engine's INFO lines ("band capture: rtl_433 decodes bursts in batches", resumed captures, ...) go to
    aris.log in this process too. Only the terminal console installed the file handler before (v4.9.5, found on
    a test station: a 433 band recording in the GUI wrote no such line anywhere; the httpx lines in aris.log came from the
    chat console's own process)."""
    import logging
    import logging.handlers
    lg = logging.getLogger("aris")
    if any(isinstance(h, logging.handlers.RotatingFileHandler) for h in lg.handlers):
        return
    try:
        h = mod._file_log_handler()
    except (OSError, AttributeError):
        return
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    lg.addHandler(h)
    if lg.level == logging.NOTSET or lg.level > logging.INFO:
        lg.setLevel(logging.INFO)
