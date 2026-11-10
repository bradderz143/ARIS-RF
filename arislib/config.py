"""Where ARIS keeps its data. Same rules as aris.py: ARIS_DATA_DIR, default ~/aris_data, and a temp folder
if that cannot be created."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path


def _data_dir() -> Path:
    p = Path(os.environ.get("ARIS_DATA_DIR") or (Path.home() / "aris_data"))
    try:
        p.mkdir(parents=True, exist_ok=True)
        return p
    except OSError:
        p = Path(tempfile.gettempdir()) / "aris_data"
        p.mkdir(parents=True, exist_ok=True)
        return p


DATA_DIR = _data_dir()
NODES_PATH = DATA_DIR / "nodes.json"
NODE_SECRETS_PATH = DATA_DIR / "node_secrets.json"      # tokens for remote nodes (mode 600, never shown)
FLOORPLAN_DIR = DATA_DIR / "floorplans"
LOCATE_DB = DATA_DIR / "locate.db"

_IO_LOCK = threading.Lock()


def read_json(path: Path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path: Path, data, private: bool = False) -> None:
    """Atomic write (temp file + rename) so a crash never leaves half a file. private=True -> mode 600."""
    path = Path(path)
    with _IO_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)
        if private:
            os.chmod(path, 0o600)
