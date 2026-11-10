"""Receivers ARIS can measure with ("nodes").

  local   a dongle plugged into this PC (its antenna can sit anywhere a USB extension or coax reaches)
  remote  another computer running aris_node.py with its own dongle (for example a Raspberry Pi in another room)

Each node can be placed on a floorplan (x, y in metres from the plan's top-left corner). Antennas and cables
differ, so each node keeps a correction per band ("offsets_db") that makes its readings comparable with the
others; calibrate them by measuring one transmitter with every antenna side by side.

nodes.json holds everything except remote-node tokens, which live in node_secrets.json (mode 600).
"""
from __future__ import annotations

import re
import secrets as _secrets
import threading
import time

from . import config

_LOCK = threading.RLock()

# Antenna gain changes a lot between bands, so offsets are kept per band.
BANDS = [
    ("fm", 87.5, 108.0), ("vhf", 108.0, 300.0), ("uhf_low", 300.0, 400.0), ("433", 400.0, 470.0),
    ("uhf", 470.0, 860.0), ("900", 860.0, 960.0), ("l_band", 960.0, 2000.0),
]


def band_key(freq_mhz: float) -> str:
    f = float(freq_mhz)
    for key, lo, hi in BANDS:
        if lo <= f < hi:
            return key
    return "other"


def _empty():
    return {"version": 1, "nodes": []}


def _load():
    d = config.read_json(config.NODES_PATH, None)
    if not isinstance(d, dict) or not isinstance(d.get("nodes"), list):
        d = _empty()
    return d


def _save(d) -> None:
    config.write_json(config.NODES_PATH, d)


def _public(n: dict) -> dict:
    out = dict(n)
    out.pop("token", None)
    out["placed"] = n.get("x") is not None and n.get("y") is not None and bool(n.get("floorplan"))
    return out


_AUTO_NAME = re.compile(r"^(?:Dongle \d+|Removed dongle \S+)$")


def _auto_named(n) -> bool:
    """The node still has the name ARIS gave it (not one the user typed)."""
    return n.get("auto_name", bool(_AUTO_NAME.match(str(n.get("name") or ""))))


def ensure_local(dongles, absent=()) -> list:
    """Make sure every dongle of this PC has a node, named as ARIS numbers it now. dongles: [(serial, name)] of
    the dongles ARIS knows; absent: serials among them that are not plugged in. A local node whose dongle was
    forgotten or is absent is switched off ('missing') and keeps its place and calibration, so it comes back as it
    was; v4.8.6 kept the removed 00002002 as an enabled 'Dongle 2' and called the real Dongle 2 'Dongle 3'.
    A name the user gave a node is kept. Returns all nodes."""
    with _LOCK:
        d = _load()
        names = {str(s): str(nm) for s, nm in dongles}
        gone = {str(s) for s in absent}
        have = {n.get("serial") for n in d["nodes"] if n.get("kind") == "local"}
        changed = False
        for n in d["nodes"]:
            if n.get("kind") != "local":
                continue
            ser = str(n.get("serial"))
            here = ser in names and ser not in gone
            want = (names[ser] if ser in names else f"Removed dongle {ser}") if _auto_named(n) else n.get("name")
            if n.get("name") != want:
                n["name"], changed = want, True
            if not here and not n.get("missing"):
                n.update(missing=True, enabled_before=n.get("enabled", True), enabled=False)
                changed = True
            elif here and n.get("missing"):
                n["enabled"] = n.pop("enabled_before", True)
                n.pop("missing", None)
                changed = True
        for serial, name in dongles:
            if str(serial) in have:
                continue
            d["nodes"].append({"id": f"dongle-{serial}", "name": str(name), "kind": "local", "serial": str(serial),
                               "floorplan": None, "x": None, "y": None, "antenna": "", "offsets_db": {},
                               "enabled": str(serial) not in gone, "auto_name": True,
                               "created": time.strftime("%Y-%m-%d %H:%M:%S")})
            if str(serial) in gone:
                d["nodes"][-1].update(missing=True, enabled_before=True)
            changed = True
        if changed:
            _save(d)
        return [_public(n) for n in d["nodes"]]


def list_nodes() -> list:
    with _LOCK:
        return [_public(n) for n in _load()["nodes"]]


def get(node_id: str):
    with _LOCK:
        for n in _load()["nodes"]:
            if n["id"] == node_id:
                return _public(n)
    return None


def update(node_id: str, **fields) -> dict:
    """Change name, antenna note, enabled, or position (floorplan, x, y). Unknown fields are ignored."""
    allowed = {"name", "antenna", "enabled", "floorplan", "x", "y", "url"}
    with _LOCK:
        d = _load()
        for n in d["nodes"]:
            if n["id"] != node_id:
                continue
            for k, v in fields.items():
                if k not in allowed:
                    continue
                if k in ("x", "y"):
                    v = None if v is None else round(float(v), 3)
                elif k == "name":
                    v = str(v).strip()[:40] or n["name"]
                    if v != n.get("name"):
                        n["auto_name"] = False          # the user's own name: ARIS no longer renumbers it
                elif k == "antenna":
                    v = str(v).strip()[:80]
                elif k == "enabled":
                    v = bool(v)
                    if n.get("missing"):              # its dongle is not plugged in: it stays off until it is back
                        n["enabled_before"] = v
                        continue
                elif k == "url":
                    if n.get("kind") != "remote":
                        continue
                    v = _check_url(v)
                n[k] = v
            _save(d)
            return _public(n)
    raise KeyError(f"No node called {node_id}")


def place(node_id: str, floorplan_id, x, y) -> dict:
    return update(node_id, floorplan=floorplan_id, x=x, y=y)


def unplace(node_id: str) -> dict:
    return update(node_id, floorplan=None, x=None, y=None)


def unplace_all_on(floorplan_id: str) -> None:
    with _LOCK:
        d = _load()
        for n in d["nodes"]:
            if n.get("floorplan") == floorplan_id:
                n["floorplan"], n["x"], n["y"] = None, None, None
        _save(d)


def _check_url(url) -> str:
    url = str(url or "").strip().rstrip("/")
    if not re.match(r"^https?://[A-Za-z0-9.\-\[\]:]+(:\d+)?$", url):
        raise ValueError("Node address must look like http://192.168.1.50:5055")
    return url


def add_remote(name: str, url: str, token: str) -> dict:
    url = _check_url(url)
    token = str(token or "").strip()
    if len(token) < 16:
        raise ValueError("The node token is the long code aris_node.py printed when it was set up.")
    base = re.sub(r"[^a-z0-9]+", "-", str(name or "node").lower()).strip("-")[:24] or "node"
    with _LOCK:
        d = _load()
        ids = {n["id"] for n in d["nodes"]}
        nid, k = f"remote-{base}", 2
        while nid in ids:
            nid, k = f"remote-{base}-{k}", k + 1
        n = {"id": nid, "name": str(name or nid).strip()[:40], "kind": "remote", "url": url, "floorplan": None,
             "x": None, "y": None, "antenna": "", "offsets_db": {}, "enabled": True,
             "created": time.strftime("%Y-%m-%d %H:%M:%S")}
        d["nodes"].append(n)
        _save(d)
        sec = config.read_json(config.NODE_SECRETS_PATH, {})
        sec[nid] = token
        config.write_json(config.NODE_SECRETS_PATH, sec, private=True)
        return _public(n)


def remove(node_id: str) -> None:
    """Remote nodes are deleted; local dongles cannot be (disable them instead)."""
    with _LOCK:
        d = _load()
        n = next((x for x in d["nodes"] if x["id"] == node_id), None)
        if n is None:
            raise KeyError(f"No node called {node_id}")
        if n.get("kind") == "local":
            raise ValueError("Dongles plugged into this PC cannot be removed; switch them off instead.")
        d["nodes"] = [x for x in d["nodes"] if x["id"] != node_id]
        _save(d)
        sec = config.read_json(config.NODE_SECRETS_PATH, {})
        if sec.pop(node_id, None) is not None:
            config.write_json(config.NODE_SECRETS_PATH, sec, private=True)


def token_for(node_id: str):
    return config.read_json(config.NODE_SECRETS_PATH, {}).get(node_id)


def offset_for(node: dict, freq_mhz: float) -> float:
    return float((node.get("offsets_db") or {}).get(band_key(freq_mhz), 0.0))


def set_offsets(freq_mhz: float, offsets: dict) -> None:
    """offsets: {node_id: dB to add to that node's readings in this band}."""
    key = band_key(freq_mhz)
    with _LOCK:
        d = _load()
        for n in d["nodes"]:
            if n["id"] in offsets:
                n.setdefault("offsets_db", {})[key] = round(float(offsets[n["id"]]), 2)
                n.setdefault("calibrated", {})[key] = {"freq_mhz": float(freq_mhz),
                                                       "time": time.strftime("%Y-%m-%d %H:%M:%S")}
        _save(d)


def clear_offsets(freq_mhz=None) -> None:
    with _LOCK:
        d = _load()
        for n in d["nodes"]:
            if freq_mhz is None:
                n["offsets_db"], n["calibrated"] = {}, {}
            else:
                (n.get("offsets_db") or {}).pop(band_key(freq_mhz), None)
                (n.get("calibrated") or {}).pop(band_key(freq_mhz), None)
        _save(d)


def placed_on(floorplan_id: str, enabled_only: bool = True) -> list:
    return [n for n in list_nodes() if n.get("floorplan") == floorplan_id and n["placed"]
            and (n.get("enabled", True) or not enabled_only)]


def new_token() -> str:
    return _secrets.token_urlsafe(24)
