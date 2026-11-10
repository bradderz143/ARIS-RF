"""Where is a transmitter? Turn signal levels measured at known positions into a heat map.

Model: indoor signal strength falls off roughly as  level = A - 10 * n * log10(distance),  with A (the
transmitter's strength) unknown and n between about 2 (open room) and 3.5 (through walls). For every spot on the
map we ask: if the transmitter were here, how well would the best A explain what each receiver measured? Spots
that explain the readings well get a high score. Receivers that heard nothing count too: the transmitter cannot
be so close to them that they should have heard it.

This is a rough-area tool, not a precise fix: walls, furniture, people and antenna direction move readings by
5-10 dB. More receivers, or more survey points (carrying one antenna around), sharpen the picture.
"""
from __future__ import annotations

import math

import numpy as np

EXPONENTS = (2.0, 2.5, 3.0, 3.5)
SIGMA_DB = 5.0          # typical indoor shadowing spread
NEAR_M = 0.5            # closer than this is treated as this distance (antenna near field, model breaks down)
MISS_MARGIN_DB = 3.0    # a receiver that heard nothing may still have been this close to hearing it


def grid_for(bounds, max_cells: int = 60, cell_m: float | None = None):
    x0, y0, x1, y1 = (float(v) for v in bounds)
    w, h = max(x1 - x0, 0.5), max(y1 - y0, 0.5)
    cell = float(cell_m) if cell_m else max(w, h) / float(max_cells)
    cell = max(cell, 0.05)
    nx, ny = max(2, int(math.ceil(w / cell))), max(2, int(math.ceil(h / cell)))
    xs = x0 + (np.arange(nx) + 0.5) * cell
    ys = y0 + (np.arange(ny) + 0.5) * cell
    return xs, ys, cell


def _points(points):
    out = []
    for p in points:
        try:
            x, y = float(p["x"]), float(p["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(x) and math.isfinite(y)):
            continue
        out.append({"x": x, "y": y, "level_db": p.get("level_db"), "floor_db": p.get("floor_db"),
                    "heard": bool(p.get("heard")), "weight": float(p.get("weight") or 1.0),
                    "label": p.get("label") or ""})
    return out


OUTSIDE_PRIOR = 0.5     # a spot outside the plan is half as likely a priori: you map the space you are searching


def _modes(like, gx, gy, cell, min_sep_m=1.5, min_val=0.5, keep=3):
    """Separate likely areas: local maxima at least min_sep_m apart, strongest first."""
    ny, nx = like.shape
    pad = np.pad(like, 1, mode="constant", constant_values=-1.0)
    peak = np.ones(like.shape, dtype=bool)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy or dx:
                peak &= like >= pad[1 + dy:1 + dy + ny, 1 + dx:1 + dx + nx]
    idx = np.argwhere(peak & (like >= min_val))
    cand = sorted(((float(like[i, j]), int(i), int(j)) for i, j in idx), reverse=True)
    out = []
    sep = max(min_sep_m, 2.5 * cell)

    def valley(a, b):
        """Lowest value on the straight line between two cells: a real second area has a dip in between."""
        n = max(abs(a[0] - b[0]), abs(a[1] - b[1]), 1)
        return min(float(like[int(round(a[0] + (b[0] - a[0]) * t / n)), int(round(a[1] + (b[1] - a[1]) * t / n))])
                   for t in range(n + 1))

    for v, i, j in cand:
        x, y = float(gx[i, j]), float(gy[i, j])
        ok = True
        for ov, oi, oj in out:
            if math.hypot(x - float(gx[oi, oj]), y - float(gy[oi, oj])) < sep or \
                    valley((i, j), (oi, oj)) >= 0.5 * min(v, ov):
                ok = False
                break
        if ok:
            out.append((v, i, j))
        if len(out) >= keep:
            break
    return [(v, float(gx[i, j]), float(gy[i, j])) for v, i, j in out]


def source_map(points, bounds, sigma_db: float = SIGMA_DB, exponents=EXPONENTS, max_cells: int = 60,
               plan_size=None) -> dict:
    """Likelihood (0..1) of the transmitter being in each cell. points: [{x, y, level_db, floor_db, heard}]
    in metres and dB, with receiver differences already corrected (node offsets). plan_size=(w, h) makes spots
    outside the plan a little less likely (OUTSIDE_PRIOR).

    Signal strength alone often allows two areas (a mirror image across the receivers), above all with only
    three receivers. Both are reported: 'best' and 'alternatives'."""
    pts = _points(points)
    heard = [p for p in pts if p["heard"] and p["level_db"] is not None]
    missed = [p for p in pts if not p["heard"] and p["floor_db"] is not None]
    xs, ys, cell = grid_for(bounds, max_cells)
    gx, gy = np.meshgrid(xs, ys)                                   # (ny, nx)
    notes = []
    if not heard:
        return {"ok": False, "reason": "No receiver heard the signal, so there is nothing to locate.",
                "heard_points": 0, "points": len(pts)}

    def logd(p):
        return np.log10(np.maximum(np.hypot(gx - p["x"], gy - p["y"]), NEAR_M))

    LH = np.stack([logd(p) for p in heard])                        # (k, ny, nx)
    L = np.array([p["level_db"] for p in heard], dtype=float)[:, None, None]
    W = np.array([p["weight"] for p in heard], dtype=float)[:, None, None]
    LM = np.stack([logd(p) for p in missed]) if missed else None
    FM = (np.array([p["floor_db"] for p in missed], dtype=float)[:, None, None] if missed else None)
    WM = (np.array([p["weight"] for p in missed], dtype=float)[:, None, None] if missed else None)

    like = np.zeros(gx.shape)
    best_n = []
    for n in exponents:
        A = np.sum(W * (L + 10.0 * n * LH), axis=0) / np.sum(W, axis=0)      # best transmitter strength per cell
        chi2 = np.sum(W * (L + 10.0 * n * LH - A) ** 2, axis=0) / sigma_db ** 2
        if missed:
            pred = A - 10.0 * n * LM
            excess = np.maximum(0.0, pred - (FM + MISS_MARGIN_DB))
            chi2 = chi2 + np.sum(WM * excess ** 2, axis=0) / sigma_db ** 2
        lk = np.exp(-0.5 * chi2)
        best_n.append(float(lk.max()))
        like += lk
    if len(heard) == 1 and not missed:
        # One receiver and no misses: the model cannot tell distance. Show "near this receiver" instead.
        p = heard[0]
        d = np.hypot(gx - p["x"], gy - p["y"])
        like = np.exp(-d / max(1.5, 0.15 * max(xs[-1] - xs[0], ys[-1] - ys[0])))
        notes.append(f"Only {p['label'] or 'one receiver'} heard it, so this only shows 'near that receiver'. "
                     "Place more antennas or add survey points to narrow it down.")
    if plan_size:
        w, h = (float(v) for v in plan_size)
        out_d = np.hypot(np.maximum(0.0, np.maximum(-gx, gx - w)), np.maximum(0.0, np.maximum(-gy, gy - h)))
        like = like * np.maximum(OUTSIDE_PRIOR, 1.0 - 0.3 * out_d)     # tapers off outside, no hard edge
    mx = float(like.max())
    like = like / mx if mx > 0 else like
    modes = _modes(like, gx, gy, cell) or [(1.0, float(gx.flat[int(np.argmax(like))]),
                                            float(gy.flat[int(np.argmax(like))]))]
    _, bx, by = modes[0]
    # Spread of the best area only: likely cells that are closer to the best spot than to any other area.
    sel = like >= 0.5
    if len(modes) > 1:
        d0 = np.hypot(gx - bx, gy - by)
        for _, ox, oy in modes[1:]:
            sel &= d0 <= np.hypot(gx - ox, gy - oy)
    wts = like[sel]
    cx, cy = float(np.sum(gx[sel] * wts) / wts.sum()), float(np.sum(gy[sel] * wts) / wts.sum())
    spread = float(math.sqrt(np.sum(wts * ((gx[sel] - cx) ** 2 + (gy[sel] - cy) ** 2)) / wts.sum()))
    spread = max(spread, cell)
    alts = [{"x": round(x, 2), "y": round(y, 2), "score": round(v, 2)} for v, x, y in modes[1:] if v >= 0.75]
    k = len(heard)
    if k >= 3 and spread <= 1.5:
        quality = "good"
    elif k >= 3 and spread <= 3.0:
        quality = "rough"
    elif k >= 2:
        quality = "very rough"
    else:
        quality = "nearest receiver only"
    if alts and k >= 2:
        quality = "two possible areas" if len(alts) == 1 else "several possible areas"
        notes.append("Signal strength fits more than one area. Measure again with one antenna moved, or add a "
                     "survey point near each area: the right one gets stronger.")
    if k == 2:
        notes.append("Two receivers heard it: the likely area is a curve between them. A third receiver, or a few "
                     "survey points, turns that into a spot.")
    return {
        "ok": True, "kind": "source",
        "grid": {"x0": float(xs[0] - cell / 2), "y0": float(ys[0] - cell / 2), "cell_m": round(cell, 4),
                 "nx": int(len(xs)), "ny": int(len(ys)),
                 "values": np.round(like, 3).tolist()},
        "best": {"x": round(bx, 2), "y": round(by, 2)},
        "alternatives": alts,
        "spread_m": round(spread, 2),
        "quality": quality,
        "heard_points": k,
        "points": len(pts),
        "notes": notes,
    }


def strength_map(points, bounds, power: float = 2.0, max_cells: int = 60) -> dict:
    """Interpolated signal level between measured points (inverse-distance weighting). Good for survey walks:
    the hot spot is where the signal was strongest."""
    pts = [p for p in _points(points) if p["level_db"] is not None]
    if len(pts) < 2:
        return {"ok": False, "reason": "A strength map needs at least two readings.", "points": len(pts)}
    xs, ys, cell = grid_for(bounds, max_cells)
    gx, gy = np.meshgrid(xs, ys)
    num = np.zeros(gx.shape)
    den = np.zeros(gx.shape)
    for p in pts:
        lvl = p["level_db"] if p["heard"] else min(p["level_db"], p["floor_db"] if p["floor_db"] is not None
                                                   else p["level_db"])
        wgt = p["weight"] / np.maximum(np.hypot(gx - p["x"], gy - p["y"]), 0.3) ** power
        num += wgt * lvl
        den += wgt
    val = num / den
    lo, hi = float(val.min()), float(val.max())
    norm = (val - lo) / (hi - lo) if hi > lo else np.zeros(val.shape)
    iy, ix = np.unravel_index(int(np.argmax(val)), val.shape)
    return {"ok": True, "kind": "strength",
            "grid": {"x0": float(xs[0] - cell / 2), "y0": float(ys[0] - cell / 2), "cell_m": round(cell, 4),
                     "nx": int(len(xs)), "ny": int(len(ys)), "values": np.round(norm, 3).tolist()},
            "level_range_db": [round(lo, 1), round(hi, 1)],
            "best": {"x": round(float(gx[iy, ix]), 2), "y": round(float(gy[iy, ix]), 2)},
            "points": len(pts)}


def bounds_for(width_m: float, height_m: float, points=(), margin: float = 0.15):
    """Map area plus a margin (the transmitter may be just outside the plan, e.g. next door), widened to cover
    every point."""
    xs = [0.0, float(width_m)] + [float(p["x"]) for p in points if p.get("x") is not None]
    ys = [0.0, float(height_m)] + [float(p["y"]) for p in points if p.get("y") is not None]
    w, h = max(xs) - min(xs), max(ys) - min(ys)
    mx, my = margin * max(w, 1.0), margin * max(h, 1.0)
    return (min(xs) - mx, min(ys) - my, max(xs) + mx, max(ys) + my)


def describe(result: dict, width_m: float, height_m: float, receivers=()) -> str:
    """One or two plain sentences about a source_map result. receivers: [{label, x, y}]."""
    if not result.get("ok"):
        return result.get("reason", "Could not locate it.")
    b = result["best"]
    inside = 0 <= b["x"] <= width_m and 0 <= b["y"] <= height_m
    near = sorted(((math.hypot(r["x"] - b["x"], r["y"] - b["y"]), r["label"]) for r in receivers
                   if r.get("x") is not None), key=lambda t: t[0])
    where = f"about {b['x']:.1f} m across and {b['y']:.1f} m down the plan"
    txt = f"Most likely spot: {where}"
    if near:
        txt += f", {near[0][0]:.1f} m from {near[0][1]}"
    txt += f" (within about {result['spread_m']:.1f} m; {result['quality']})."
    if not inside:
        txt += " That is outside the floorplan, so it may be next door or outside."
    for a in result.get("alternatives") or []:
        out = not (0 <= a["x"] <= width_m and 0 <= a["y"] <= height_m)
        txt += (f" Another possible area: about {a['x']:.1f} m across, {a['y']:.1f} m down"
                + (" (outside the plan)." if out else "."))
    return txt
