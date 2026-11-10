"""Locate sessions and their readings (SQLite, DATA_DIR/locate.db).

A session is one hunt for one frequency. Every "Measure" adds a reading per node at the node's position
("fixed"); a survey reading is one node measured at a spot you tapped while carrying its antenna ("survey").
"""
from __future__ import annotations

import sqlite3
import threading
import time
from collections import defaultdict

from . import config

_LOCK = threading.Lock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, freq_mhz REAL NOT NULL, bw_khz REAL NOT NULL,
  floorplan TEXT, label TEXT);
CREATE TABLE IF NOT EXISTS readings (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL, t REAL NOT NULL, node_id TEXT NOT NULL,
  kind TEXT NOT NULL, x REAL, y REAL, level_db REAL, raw_level_db REAL, floor_db REAL, snr_db REAL,
  heard INTEGER, active_fraction REAL, gain_db REAL, clipped REAL, error TEXT);
CREATE INDEX IF NOT EXISTS readings_session ON readings(session_id);
"""
READING_KEYS = ("id", "session_id", "t", "node_id", "kind", "x", "y", "level_db", "raw_level_db", "floor_db",
                "snr_db", "heard", "active_fraction", "gain_db", "clipped", "error")


_READY: set = set()


def _conn():
    path = str(config.LOCATE_DB)
    c = sqlite3.connect(path, timeout=10)
    if path not in _READY:                  # create the tables once per process, not on every query
        c.executescript(_SCHEMA)
        _READY.add(path)
    return c


def _session_row(r):
    return {"id": r[0], "created": r[1], "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[1])),
            "freq_mhz": r[2], "bw_khz": r[3], "floorplan": r[4], "label": r[5]}


def new_session(freq_mhz: float, bw_khz: float, floorplan, label: str = "") -> dict:
    with _LOCK:
        c = _conn()
        try:
            cur = c.execute("INSERT INTO sessions(created, freq_mhz, bw_khz, floorplan, label) VALUES (?,?,?,?,?)",
                            (time.time(), float(freq_mhz), float(bw_khz), floorplan, str(label)[:80]))
            c.commit()
            return get_session(cur.lastrowid, c)
        finally:
            c.close()


def get_session(session_id: int, c=None):
    own = c is None
    c = c or _conn()
    try:
        r = c.execute("SELECT id, created, freq_mhz, bw_khz, floorplan, label FROM sessions WHERE id=?",
                      (int(session_id),)).fetchone()
        return _session_row(r) if r else None
    finally:
        if own:
            c.close()


def find_session(freq_mhz: float, floorplan, max_age_s: float = 3600, tol_mhz: float = 0.01):
    """The most recent session for this frequency on this plan, if it is recent enough to keep adding to."""
    c = _conn()
    try:
        r = c.execute("SELECT id, created, freq_mhz, bw_khz, floorplan, label FROM sessions WHERE "
                      "abs(freq_mhz - ?) <= ? AND floorplan IS ? AND created >= ? ORDER BY id DESC LIMIT 1",
                      (float(freq_mhz), tol_mhz, floorplan, time.time() - max_age_s)).fetchone()
        return _session_row(r) if r else None
    finally:
        c.close()


def list_sessions(limit: int = 20) -> list:
    c = _conn()
    try:
        rows = c.execute("SELECT s.id, s.created, s.freq_mhz, s.bw_khz, s.floorplan, s.label, COUNT(r.id) "
                         "FROM sessions s LEFT JOIN readings r ON r.session_id = s.id GROUP BY s.id "
                         "ORDER BY s.id DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(_session_row(r[:6]), readings=r[6]) for r in rows]
    finally:
        c.close()


def delete_session(session_id: int) -> None:
    with _LOCK:
        c = _conn()
        try:
            c.execute("DELETE FROM readings WHERE session_id=?", (int(session_id),))
            c.execute("DELETE FROM sessions WHERE id=?", (int(session_id),))
            c.commit()
        finally:
            c.close()


def add_readings(session_id: int, rows) -> None:
    """rows: dicts with the READING_KEYS fields (id and session_id are filled in)."""
    with _LOCK:
        c = _conn()
        try:
            for r in rows:
                c.execute("INSERT INTO readings(session_id, t, node_id, kind, x, y, level_db, raw_level_db, "
                          "floor_db, snr_db, heard, active_fraction, gain_db, clipped, error) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (int(session_id), float(r.get("t") or time.time()), str(r["node_id"]),
                           str(r.get("kind") or "fixed"), r.get("x"), r.get("y"), r.get("level_db"),
                           r.get("raw_level_db"), r.get("floor_db"), r.get("snr_db"),
                           None if r.get("heard") is None else int(bool(r.get("heard"))),
                           r.get("active_fraction"), r.get("gain_db"), r.get("clipped"), r.get("error")))
            c.commit()
        finally:
            c.close()


def readings(session_id: int) -> list:
    c = _conn()
    try:
        rows = c.execute("SELECT " + ", ".join(READING_KEYS) + " FROM readings WHERE session_id=? ORDER BY id",
                         (int(session_id),)).fetchall()
        out = []
        for r in rows:
            d = dict(zip(READING_KEYS, r))
            d["heard"] = None if d["heard"] is None else bool(d["heard"])
            out.append(d)
        return out
    finally:
        c.close()


def delete_readings(session_id: int, kind=None) -> None:
    with _LOCK:
        c = _conn()
        try:
            if kind:
                c.execute("DELETE FROM readings WHERE session_id=? AND kind=?", (int(session_id), str(kind)))
            else:
                c.execute("DELETE FROM readings WHERE session_id=?", (int(session_id),))
            c.commit()
        finally:
            c.close()


def points(session_id: int, labels=None) -> list:
    """Readings combined per node and spot (0.1 m), ready for localize. A spot counts as heard if any reading
    there heard the signal; its level is the mean of the readings that did."""
    labels = labels or {}
    groups = defaultdict(list)
    for r in readings(session_id):
        if r["error"] or r["x"] is None or r["y"] is None:
            continue
        groups[(r["node_id"], round(r["x"], 1), round(r["y"], 1))].append(r)
    out = []
    for (node_id, x, y), rs in groups.items():
        heard = [r for r in rs if r["heard"]]
        floors = [r["floor_db"] for r in rs if r["floor_db"] is not None]
        out.append({"node_id": node_id, "label": labels.get(node_id, node_id), "x": x, "y": y,
                    "heard": bool(heard),
                    "level_db": (sum(r["level_db"] for r in heard) / len(heard)) if heard else
                                (max(r["level_db"] for r in rs if r["level_db"] is not None)
                                 if any(r["level_db"] is not None for r in rs) else None),
                    "floor_db": (sum(floors) / len(floors)) if floors else None,
                    "n": len(rs), "kind": rs[-1]["kind"], "weight": min(2.0, 1.0 + 0.25 * (len(heard) - 1))
                    if heard else 1.0})
    return out
