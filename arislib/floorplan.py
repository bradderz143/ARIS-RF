"""Floorplans: a picture of the space (PNG, JPEG or WebP) and how big it really is.

Positions are metres from the plan's top-left corner: x to the right, y down. The real size comes from the
width in metres, or from two points tapped on the plan with the distance between them. Without any picture a
blank plan (a plain grid) works too.
"""
from __future__ import annotations

import re
import struct
import time
import uuid

from . import config, nodes

MAX_IMAGE_BYTES = 12 * 1024 * 1024
DEFAULT_PLAN = {"id": "default", "name": "Blank plan", "image": None, "width_m": 12.0, "height_m": 9.0,
                "img_w": None, "img_h": None}
_INDEX = config.FLOORPLAN_DIR / "index.json"


def image_size(data: bytes):
    """(kind, width_px, height_px) for PNG / JPEG / WebP, without needing Pillow. Raises ValueError otherwise."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return "png", int(w), int(h)
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return "jpg", int(w), int(h)
            i += 2 + seg
        raise ValueError("Could not read the JPEG size")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b"VP8X":
            w = 1 + int.from_bytes(data[24:27], "little")
            h = 1 + int.from_bytes(data[27:30], "little")
            return "webp", w, h
        if chunk == b"VP8L":
            b = int.from_bytes(data[21:25], "little")
            return "webp", (b & 0x3FFF) + 1, ((b >> 14) & 0x3FFF) + 1
        if chunk == b"VP8 ":
            w, h = struct.unpack("<HH", data[26:30])
            return "webp", w & 0x3FFF, h & 0x3FFF
    raise ValueError("The floorplan must be a PNG, JPEG or WebP picture.")


def _load():
    d = config.read_json(_INDEX, None)
    if not isinstance(d, dict) or not isinstance(d.get("plans"), list):
        d = {"plans": [dict(DEFAULT_PLAN, created=time.strftime("%Y-%m-%d %H:%M:%S"))], "active": "default"}
        config.write_json(_INDEX, d)
    if not any(p["id"] == d.get("active") for p in d["plans"]):
        d["active"] = d["plans"][0]["id"] if d["plans"] else None
    return d


def _save(d) -> None:
    config.write_json(_INDEX, d)


def list_plans():
    d = _load()
    return d["plans"], d.get("active")


def get(plan_id=None):
    d = _load()
    pid = plan_id or d.get("active")
    return next((p for p in d["plans"] if p["id"] == pid), None)


def active():
    return get(None)


def set_active(plan_id: str) -> dict:
    d = _load()
    if not any(p["id"] == plan_id for p in d["plans"]):
        raise KeyError("No such floorplan")
    d["active"] = plan_id
    _save(d)
    return get(plan_id)


def _clean_name(name, fallback="Floorplan"):
    return re.sub(r"[\x00-\x1f<>]", "", str(name or "")).strip()[:60] or fallback


def add(name: str, data: bytes = None, width_m: float = 10.0) -> dict:
    """New plan from picture bytes (or a blank one if data is None). Height follows the picture's shape."""
    width_m = float(width_m)
    if not (0.5 <= width_m <= 2000):
        raise ValueError("Width must be between 0.5 and 2000 metres")
    pid = uuid.uuid4().hex[:10]
    plan = {"id": pid, "name": _clean_name(name), "image": None, "width_m": round(width_m, 3),
            "height_m": round(width_m * 0.75, 3), "img_w": None, "img_h": None,
            "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    if data is not None:
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("Picture too large (12 MB max). Export it smaller.")
        kind, w, h = image_size(data)
        if not (16 <= w <= 20000 and 16 <= h <= 20000):
            raise ValueError("Picture size looks wrong")
        config.FLOORPLAN_DIR.mkdir(parents=True, exist_ok=True)
        fn = f"{pid}.{kind}"
        with open(config.FLOORPLAN_DIR / fn, "wb") as f:
            f.write(data)
        plan.update(image=fn, img_w=w, img_h=h, height_m=round(width_m * h / float(w), 3))
    d = _load()
    d["plans"].append(plan)
    d["active"] = pid
    _save(d)
    return plan


def update(plan_id: str, name=None, width_m=None, height_m=None) -> dict:
    d = _load()
    for p in d["plans"]:
        if p["id"] != plan_id:
            continue
        old_w = float(p["width_m"])
        if name is not None:
            p["name"] = _clean_name(name, p["name"])
        if width_m is not None:
            w = float(width_m)
            if not (0.5 <= w <= 2000):
                raise ValueError("Width must be between 0.5 and 2000 metres")
            p["width_m"] = round(w, 3)
            if p.get("img_w"):
                p["height_m"] = round(w * p["img_h"] / float(p["img_w"]), 3)
            elif height_m is None:
                p["height_m"] = round(float(p["height_m"]) * w / old_w, 3)
        if height_m is not None and not p.get("img_w"):
            h = float(height_m)
            if not (0.5 <= h <= 2000):
                raise ValueError("Height must be between 0.5 and 2000 metres")
            p["height_m"] = round(h, 3)
        _save(d)
        if width_m is not None and abs(float(p["width_m"]) - old_w) > 1e-9:
            _rescale_nodes(plan_id, float(p["width_m"]) / old_w)
        return p
    raise KeyError("No such floorplan")


def _rescale_nodes(plan_id, factor):
    """Node positions are in metres; when the scale changes they keep their spot on the picture."""
    for n in nodes.placed_on(plan_id, enabled_only=False):
        nodes.place(n["id"], plan_id, n["x"] * factor, n["y"] * factor)


def scale_from_points(plan_id: str, ax, ay, bx, by, distance_m: float) -> dict:
    """Two points tapped on the plan (in plan metres at the current scale) are distance_m apart in reality."""
    p = get(plan_id)
    if p is None:
        raise KeyError("No such floorplan")
    cur = ((float(bx) - float(ax)) ** 2 + (float(by) - float(ay)) ** 2) ** 0.5
    if cur < 1e-6:
        raise ValueError("Tap two different points")
    if not (0.05 <= float(distance_m) <= 2000):
        raise ValueError("Distance must be between 0.05 and 2000 metres")
    return update(plan_id, width_m=float(p["width_m"]) * float(distance_m) / cur)


def delete(plan_id: str) -> None:
    d = _load()
    p = next((x for x in d["plans"] if x["id"] == plan_id), None)
    if p is None:
        raise KeyError("No such floorplan")
    if p.get("image"):
        try:
            (config.FLOORPLAN_DIR / p["image"]).unlink()
        except OSError:
            pass
    d["plans"] = [x for x in d["plans"] if x["id"] != plan_id]
    if not d["plans"]:
        d["plans"] = [dict(DEFAULT_PLAN, created=time.strftime("%Y-%m-%d %H:%M:%S"))]
    if d.get("active") == plan_id:
        d["active"] = d["plans"][0]["id"]
    _save(d)
    nodes.unplace_all_on(plan_id)


def image_path(plan_id: str):
    p = get(plan_id)
    if not p or not p.get("image"):
        return None
    path = config.FLOORPLAN_DIR / p["image"]
    return path if path.is_file() else None
