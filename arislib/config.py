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


def stat_key(path):
    """What tells one version of a file from the next without reading it: its time, size and inode (None when it
    is not there). For caches of parsed files: read the file again only when this changed."""
    try:
        s = os.stat(str(path))
    except OSError:
        return None
    return (s.st_mtime_ns, s.st_size, s.st_ino)


def write_json(path: Path, data, private: bool = False, compact: bool = False) -> None:
    """Atomic write (temp file + rename) so a crash never leaves half a file. private=True -> mode 600.
    compact=True: on one line, for large files only programs read (the same content; json.dumps in one piece
    is several times faster than json.dump with an indent, which builds the text in small Python steps)."""
    path = Path(path)
    text = json.dumps(data, separators=(",", ":")) if compact else None
    with _IO_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
        with os.fdopen(fd, "w") as f:
            if compact:
                f.write(text)
            else:
                json.dump(data, f, indent=1)
        os.replace(tmp, path)
        if private:
            os.chmod(path, 0o600)
