"""Bluetooth Low Energy advertisements (v4.11.0): the rules of the Bluetooth view, with no Flask and no radio calls.

What is read: the text `ubertooth-btle -n` prints (passive: -n only prints advertisements; the options that follow or
interfere with connections are never used). Of each advertisement only this is kept: the kind of address, the
manufacturer and service UUIDs the device announces, the signal strength, and whether it advertises like a tracker
(Apple Find My, Tile, Samsung SmartTag). NOT kept, anywhere: the address itself (replaced by a keyed hash label, like a
remote's label), device names (they are often the owner's name), payloads, and the devices that scan or connect (their
packets are dropped before anything is read from them).

Phones and watches use random addresses that change every few minutes (resolvable / non-resolvable private
addresses): listing them one by one would only be noise, so they are counted. Public and static random addresses stay
the same, so those are the devices that are listed, and the baseline ("normal here") works on them like the radio
bands' and the Wi-Fi view's.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import time

ADV_PDUS = ("ADV_IND", "ADV_NONCONN_IND", "ADV_SCAN_IND", "ADV_DIRECT_IND", "SCAN_RSP")   # the advertiser's own packets
MAX_DEVICES = 500          # stable devices kept (the oldest not in the baseline go first)
MAX_WINDOW = 800           # stable devices in one snapshot
MAX_PRIVATE = 5000         # rotating addresses counted in one snapshot
STRONG_DBM = -50           # this strong or stronger: within a few metres of the antenna
TRACKER_LOOK_DBM = -65     # a tracker-type advertisement is worth a look only this strong or stronger (weaker: only new)
HEARD_S = 300              # "heard now": within this many seconds
TRACKERS = {"find_my": "Apple Find My network", "tile": "Tile", "smarttag": "Samsung SmartTag"}
_TILE_UUIDS = ("feed", "fd84")
_SMARTTAG_UUIDS = ("fd5a",)

_HEAD_RX = re.compile(r"^systime=\d+\s.*?rssi=(-?\d+)")
_PDU_RX = re.compile(r"^\s+Type:\s+(\S+)")
_ADVA_RX = re.compile(r"^\s+AdvA:\s+((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\s+\((public|random)\)")
_COMPANY_RX = re.compile(r"^\s+Company:\s+(.*\S)")
_DATA_RX = re.compile(r"^\s+Data:\s+([0-9A-Fa-f]{2})\b")
_UUID_HEAD_RX = re.compile(r"^\s+Type (?:02|03) \(16-bit Service UUIDs")
_UUID_BARE_RX = re.compile(r"^\s+([0-9A-Fa-f]{4})\s*$")
_UUID_SVC_RX = re.compile(r"^\s+UUID:\s+([0-9A-Fa-f]{4})\b")
_TYPE_LINE_RX = re.compile(r"^\s+Type [0-9A-Fa-f]{2}\b")


class AdvParser:
    """Feeds on the lines of `ubertooth-btle -n`; `feed(line)` returns the packet that the line just closed (or None).
    A packet is {rssi, pdu, addr, atype, company, uuids, find_my}. Only the advertiser's own packets come out: scan
    requests and connection requests (sent by other devices) are dropped without their addresses being read."""

    def __init__(self):
        self.cur = None
        self._after_company = False
        self._uuid_list = False

    def feed(self, line: str):
        m = _HEAD_RX.match(line)
        if m:
            done = self.finish()
            self.cur = {"rssi": int(m.group(1)), "pdu": "", "addr": "", "atype": "", "company": "", "uuids": [],
                        "find_my": False}
            self._after_company = self._uuid_list = False
            return done
        c = self.cur
        if c is None:
            return None
        m = _PDU_RX.match(line)
        if m and not c["pdu"]:
            c["pdu"] = m.group(1)
            return None
        if c["pdu"] not in ADV_PDUS:
            return None                     # a scan or connection request: nothing else of it is read
        m = _ADVA_RX.match(line)
        if m and not c["addr"]:
            c["addr"], c["atype"] = m.group(1).lower(), m.group(2)
            return None
        was_company, was_list = self._after_company, self._uuid_list
        self._after_company = self._uuid_list = False
        m = _COMPANY_RX.match(line)
        if m:
            if not c["company"]:
                c["company"] = m.group(1)
            self._after_company = True
            return None
        if was_company:
            m = _DATA_RX.match(line)
            if m and c["company"].startswith("Apple") and m.group(1).lower() == "12":
                c["find_my"] = True        # Apple's offline-finding advertisement (only its first byte is looked at)
            return None
        if _UUID_HEAD_RX.match(line):
            self._uuid_list = True
            return None
        if was_list:
            m = _UUID_BARE_RX.match(line)
            if m:
                c["uuids"].append(m.group(1).lower())
                self._uuid_list = True
            return None
        m = _UUID_SVC_RX.match(line)
        if m:
            c["uuids"].append(m.group(1).lower())
        return None

    def finish(self):
        """The packet being built (call at the end of the stream); None unless it is an advertiser's packet."""
        c, self.cur = self.cur, None
        if c and c["pdu"] in ADV_PDUS and c["addr"]:
            return c
        return None


def addr_class(addr: str, atype: str) -> str:
    """'public' and 'static' addresses stay the same; 'private' ones (resolvable or not) change every few minutes."""
    if atype == "public":
        return "public"
    try:
        top = int(addr[:2], 16) >> 6
    except ValueError:
        return "private"
    return "static" if top == 3 else "private"


def label(key: bytes, addr: str) -> str:
    """A device's label: '#' + HMAC-SHA256(key, address)[:6], the way a remote's label is made. The key lives with the
    helper only, so the address cannot be worked back from the label (there are only 2^48 of them)."""
    return "#" + hmac.new(key, addr.encode(), hashlib.sha256).hexdigest()[:6]


def tracker_kinds(p: dict) -> list:
    out = []
    if p.get("find_my"):
        out.append("find_my")
    u = p.get("uuids") or []
    if any(x in _TILE_UUIDS for x in u):
        out.append("tile")
    if any(x in _SMARTTAG_UUIDS for x in u):
        out.append("smarttag")
    return out


class Window:
    """What was heard in one stretch of time, kept in the helper's memory and written out as a summary."""

    def __init__(self, key: bytes):
        self.key = key
        self.packets = 0
        self.stable = {}
        self.private = {}                  # label -> company (counted, never listed)
        self.priv_rssi = None
        self.trackers = {}                 # kind -> {"labels": set, "rssi_max": dBm}
        self.dropped = 0

    def add(self, p: dict):
        self.packets += 1
        cls = addr_class(p["addr"], p["atype"])
        lab = label(self.key, p["addr"])
        rssi = p.get("rssi")
        if cls == "private":
            if lab in self.private or len(self.private) < MAX_PRIVATE:
                self.private[lab] = self.private.get(lab) or p.get("company") or ""
            if rssi is not None and (self.priv_rssi is None or rssi > self.priv_rssi):
                self.priv_rssi = rssi
        else:
            d = self.stable.get(lab)
            if d is None:
                if len(self.stable) >= MAX_WINDOW:
                    self.dropped += 1
                    d = None
                else:
                    d = self.stable[lab] = {"label": lab, "atype": cls, "company": "", "uuids": [], "n": 0,
                                            "rssi_max": rssi, "rssi_last": rssi, "trackers": [],
                                            "oui": p["addr"][:8] if cls == "public" else ""}
            if d is not None:
                d["n"] += 1
                d["rssi_last"] = rssi
                if rssi is not None and (d["rssi_max"] is None or rssi > d["rssi_max"]):
                    d["rssi_max"] = rssi
                if p.get("company") and not d["company"]:
                    d["company"] = p["company"]
                for u in p.get("uuids") or []:
                    if u not in d["uuids"] and len(d["uuids"]) < 6:
                        d["uuids"].append(u)
        for k in tracker_kinds(p):
            tr = self.trackers.setdefault(k, {"labels": set(), "rssi_max": None})
            tr["labels"].add(lab)
            if rssi is not None and (tr["rssi_max"] is None or rssi > tr["rssi_max"]):
                tr["rssi_max"] = rssi
            if cls != "private":
                d = self.stable.get(lab)
                if d is not None and k not in d["trackers"]:
                    d["trackers"].append(k)

    def snapshot(self, now=None, window_s=60) -> dict:
        by = {}
        for comp in self.private.values():
            by[comp or "unknown"] = by.get(comp or "unknown", 0) + 1
        top = dict(sorted(by.items(), key=lambda kv: -kv[1])[:8])
        return {"v": 1, "t": time.time() if now is None else now, "window_s": window_s, "packets": self.packets,
                "stable": sorted(self.stable.values(), key=lambda d: -d["n"]),
                "rotating": {"distinct": len(self.private), "by_company": top, "rssi_max": self.priv_rssi},
                "trackers": {k: {"distinct": len(v["labels"]), "rssi_max": v["rssi_max"]} for k, v in self.trackers.items()},
                "dropped": self.dropped}


def new_state() -> dict:
    return {"version": 1, "devices": {}, "baseline": None, "baseline_t": None, "scan_t": None,
            "rotating": {}, "trackers": {}}


def update(state: dict, snap: dict, now=None) -> dict:
    """Fold one summary from the helper into the state (in place). Returns the state."""
    now = time.time() if now is None else now
    t = float(snap.get("t") or now)
    known = state.setdefault("devices", {})
    for d in snap.get("stable") or []:
        key = str(d.get("label") or "")
        if not key:
            continue
        r = known.get(key)
        if r is None:
            r = known[key] = {"label": key, "first_t": t, "seen": 0, "max_dbm": d.get("rssi_max"), "company": "",
                              "uuids": [], "trackers": []}
        r.update(atype=d.get("atype"), oui=d.get("oui") or r.get("oui", ""), last_t=t, last_dbm=d.get("rssi_last"))
        if d.get("company"):
            r["company"] = d["company"]
        r["seen"] = int(r.get("seen") or 0) + int(d.get("n") or 0)
        mx = d.get("rssi_max")
        if mx is not None and (r.get("max_dbm") is None or mx > r["max_dbm"]):
            r["max_dbm"] = mx
        for u in d.get("uuids") or []:
            if u not in r["uuids"] and len(r["uuids"]) < 6:
                r["uuids"].append(u)
        for k in d.get("trackers") or []:
            if k not in r["trackers"]:
                r["trackers"].append(k)
    rot = snap.get("rotating") or {}
    prev = state.get("rotating") or {}
    state["rotating"] = {"t": t, "distinct": int(rot.get("distinct") or 0), "by_company": rot.get("by_company") or {},
                         "rssi_max": rot.get("rssi_max"),
                         "peak": max(int(prev.get("peak") or 0), int(rot.get("distinct") or 0))}
    state["trackers"] = {"t": t, "now": snap.get("trackers") or {}}
    state["scan_t"] = t
    if len(known) > MAX_DEVICES:
        base = state.get("baseline") or {}
        order = sorted(known, key=lambda k: (k in base, float(known[k].get("last_t") or 0)))
        for k in order[:len(known) - MAX_DEVICES]:
            known.pop(k, None)
    return state


def record_baseline(state: dict, now=None) -> int:
    """Every stable device heard so far is "normal here". Returns how many."""
    now = time.time() if now is None else now
    state["baseline"] = {k: {"company": r.get("company", "")} for k in (state.get("devices") or {})
                         for r in [state["devices"][k]]}
    state["baseline_t"] = now
    return len(state["baseline"])


def _reasons(r: dict) -> list:
    out = []
    mx = r.get("max_dbm")
    if mx is not None and mx >= STRONG_DBM:
        out.append(f"strong signal ({mx:.0f} dBm): probably within a few metres of the antenna")
    if mx is not None and mx >= TRACKER_LOOK_DBM:
        for k in r.get("trackers") or []:
            out.append(f"advertises like a {TRACKERS.get(k, k)} tracker (other devices of that network do too: not proof of a tag)")
    return out


def rows(state: dict, now=None) -> dict:
    """The list for the page: one row per device with a stable address, the counts of rotating ones, the trackers."""
    now = time.time() if now is None else now
    base = state.get("baseline")
    out = []
    for key, r in (state.get("devices") or {}).items():
        row = dict(r)
        row["heard_now"] = now - float(r.get("last_t") or 0) <= HEARD_S
        if base is None:
            row["status"], row["reasons"] = "unrated", []
        elif key in base:
            row["status"], row["reasons"] = "baseline", []
        else:
            reasons = _reasons(r)
            row["status"], row["reasons"] = ("look" if reasons else "new"), reasons
        out.append(row)
    rank = {"look": 0, "new": 1, "unrated": 2, "baseline": 3}
    out.sort(key=lambda x: (rank[x["status"]], -(x.get("max_dbm") if x.get("max_dbm") is not None else -999)))
    counts = {s: sum(1 for x in out if x["status"] == s) for s in rank}
    rot = state.get("rotating") or {}
    return {"rows": out, "devices": len(out), "counts": counts, "baseline_t": state.get("baseline_t"),
            "baseline_n": len(base) if base is not None else None, "scan_t": state.get("scan_t"),
            "rotating": {"distinct": rot.get("distinct", 0), "by_company": rot.get("by_company", {}),
                         "peak": rot.get("peak", 0), "rssi_max": rot.get("rssi_max")},
            "trackers": (state.get("trackers") or {}).get("now", {})}
