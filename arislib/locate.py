"""Locate a transmitter on a floorplan: measure on the placed nodes, keep the readings in a session, and turn
them into a heat map. Used by both the web GUI (Map tab) and the AI tool locate_signal."""
from __future__ import annotations

import time

from . import floorplan, localize, measure, nodes, store

DEFAULT_DWELL_S = 5.0
CAL_DWELL_S = 10.0
SESSION_REUSE_S = 3600


def default_bw_khz(freq_mhz: float) -> float:
    """Channel width to measure: FM broadcast-band signals are about 200 kHz wide, almost everything else ARIS
    hunts (remotes, sensors, bugs on VHF/UHF) fits in 25 kHz."""
    return 200.0 if 87.5 <= float(freq_mhz) <= 108.0 else 25.0


def _plan(plan_id=None):
    p = floorplan.get(plan_id) if plan_id else floorplan.active()
    if p is None:
        raise ValueError("No floorplan. Open the Map tab to add one (a blank plan works too).")
    return p


def run_measure(eng, freq_mhz, bw_khz=None, dwell_s=None, session_id=None, plan_id=None, kind="fixed",
                node_id=None, at=None, new_session=False) -> dict:
    """kind='fixed': every enabled node placed on the plan, at its position.
    kind='survey': one node (node_id), measured at `at` = (x, y) where its antenna is right now.
    Readings go into session_id, else into a recent session for the same frequency (unless new_session)."""
    freq_mhz = float(freq_mhz)
    bw_khz = float(bw_khz or default_bw_khz(freq_mhz))
    dwell_s = float(dwell_s or DEFAULT_DWELL_S)
    plan = _plan(plan_id)
    if kind == "survey":
        n = nodes.get(node_id) if node_id else None
        if n is None:
            raise ValueError("Pick the node whose antenna you are carrying.")
        if at is None:
            raise ValueError("Tap where the antenna is now.")
        use = [dict(n, x=float(at[0]), y=float(at[1]))]
    else:
        use = nodes.placed_on(plan["id"])
        if not use:
            raise ValueError("No antennas are placed on this floorplan yet. In the Map tab, pick a node and tap "
                             "where its antenna is.")
    sess = store.get_session(session_id) if session_id else None
    if sess is not None and (abs(sess["freq_mhz"] - freq_mhz) > 0.01 or sess["floorplan"] != plan["id"]):
        sess = None                                    # never mix frequencies or plans in one session
    if sess is None and not new_session:
        sess = store.find_session(freq_mhz, plan["id"], SESSION_REUSE_S)
    if sess is None:
        sess = store.new_session(freq_mhz, bw_khz, plan["id"])
    res = measure.measure_nodes(eng, use, freq_mhz, bw_khz, dwell_s)
    rows = []
    for n in use:
        r = res.get(n["id"]) or {"error": "no result"}
        off = nodes.offset_for(n, freq_mhz)
        row = {"node_id": n["id"], "kind": kind, "x": n["x"], "y": n["y"], "t": r.get("t", time.time())}
        if r.get("error"):
            row["error"] = r["error"]
        else:
            row.update(level_db=round(r["level_db"] + off, 2), raw_level_db=r["level_db"],
                       floor_db=round(r["floor_db"] + off, 2), snr_db=r["snr_db"], heard=r["heard"],
                       active_fraction=r["active_fraction"], gain_db=r.get("gain_db"),
                       clipped=r.get("clipped_fraction"))
        rows.append(row)
    store.add_readings(sess["id"], rows)
    out = analyse(sess["id"])
    out["this_round"] = [_round_line(n, res.get(n["id"]) or {}) for n in use]
    return out


def _round_line(n, r):
    if r.get("error"):
        return {"node": n["name"], "node_id": n["id"], "error": r["error"]}
    return {"node": n["name"], "node_id": n["id"], "heard": r["heard"], "snr_db": r["snr_db"],
            "level_db": r["level_db"], "active_fraction": r["active_fraction"], "overloaded": r.get("overloaded"),
            "note": r.get("note")}


def analyse(session_id: int, mode: str = "source") -> dict:
    sess = store.get_session(session_id)
    if sess is None:
        raise KeyError("No such locate session")
    plan = floorplan.get(sess["floorplan"]) or _plan()
    all_nodes = {n["id"]: n for n in nodes.list_nodes()}
    pts = store.points(session_id, {k: n["name"] for k, n in all_nodes.items()})
    bounds = localize.bounds_for(plan["width_m"], plan["height_m"], pts)
    if mode == "strength":
        heat = localize.strength_map(pts, bounds)
        text = ("Signal strength between the readings: the hottest area is where it was strongest."
                if heat.get("ok") else heat.get("reason"))
    else:
        heat = localize.source_map(pts, bounds, plan_size=(plan["width_m"], plan["height_m"]))
        text = localize.describe(heat, plan["width_m"], plan["height_m"],
                                 [{"label": p["label"], "x": p["x"], "y": p["y"]} for p in pts if p["kind"] == "fixed"])
    band = nodes.band_key(sess["freq_mhz"])
    uncal = sorted({p["label"] for p in pts if p["kind"] == "fixed" and
                    not ((all_nodes.get(p["node_id"]) or {}).get("calibrated") or {}).get(band)})
    notes = list(heat.get("notes") or [])
    if len(uncal) > 1 and mode == "source":
        notes.append("Antennas are not calibrated for this band yet, so their different gains blur the map. "
                     "Calibrate once: put the antennas side by side and use Calibrate.")
    return {"session": sess, "plan": plan, "mode": mode, "points": pts, "heat": heat, "summary": text,
            "notes": notes}


def calibrate(eng, freq_mhz, bw_khz=None, dwell_s=None, node_ids=None) -> dict:
    """Measure one transmitter with every antenna side by side (same distance), then store per-band offsets that
    make all nodes read the same. The offsets only apply to this band."""
    freq_mhz = float(freq_mhz)
    bw_khz = float(bw_khz or default_bw_khz(freq_mhz))
    use = [n for n in nodes.list_nodes() if n.get("enabled", True) and (not node_ids or n["id"] in node_ids)]
    if len(use) < 2:
        raise ValueError("Calibration needs at least two nodes.")
    res = measure.measure_nodes(eng, use, freq_mhz, bw_khz, float(dwell_s or CAL_DWELL_S))
    heard = {n["id"]: res[n["id"]] for n in use if not res[n["id"]].get("error") and res[n["id"]].get("heard")}
    if len(heard) < 2:
        missing = [n["name"] for n in use if n["id"] not in heard]
        raise ValueError("Fewer than two antennas heard the signal (" + ", ".join(missing) + " did not). Keep the "
                         "transmitter going during the whole measurement, or move it closer.")
    mean = sum(r["level_db"] for r in heard.values()) / len(heard)
    offsets = {nid: round(mean - r["level_db"], 2) for nid, r in heard.items()}
    nodes.set_offsets(freq_mhz, offsets)
    names = {n["id"]: n["name"] for n in use}
    warn = []
    spread = max(offsets.values()) - min(offsets.values())
    if spread > 25:
        warn.append(f"The antennas differ by {spread:.0f} dB in this band: the weakest probably does not suit it. "
                    "Its readings will be noisy.")
    skipped = [names[n["id"]] for n in use if n["id"] not in heard]
    return {"band": nodes.band_key(freq_mhz), "freq_mhz": freq_mhz,
            "offsets_db": {names[k]: v for k, v in offsets.items()}, "not_calibrated": skipped, "warnings": warn}


def for_ai(result: dict) -> dict:
    """Small summary for the AI: no grid."""
    heat = result.get("heat") or {}
    return {"frequency_mhz": result["session"]["freq_mhz"], "session_id": result["session"]["id"],
            "floorplan": result["plan"]["name"], "plan_size_m": [result["plan"]["width_m"], result["plan"]["height_m"]],
            "located": bool(heat.get("ok")), "estimate": result.get("summary"),
            "best_spot_m": heat.get("best"), "uncertainty_m": heat.get("spread_m"), "quality": heat.get("quality"),
            "receivers_that_heard_it": heat.get("heard_points"),
            "this_round": result.get("this_round"), "notes": result.get("notes"),
            "see": "The Map tab shows the heat map."}
