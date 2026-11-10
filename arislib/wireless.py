"""Wi-Fi networks heard (v4.10.0): the rules of the Wireless tab, with no Flask and no radio calls.

What is read: the output of `iw dev <interface> scan dump` (one block per network: BSSID, frequency, signal,
SSID, security). Only what every access point announces in its beacons: the network's name and ID, channel,
signal strength and security. Nothing about the devices that connect to it (no client addresses) and no
payloads are read or kept. ARIS itself never transmits: the scan is made by a separate helper that listens
only (`iw ... scan passive`, see deploy/aris-wifi.sh); this module only reads text.

The baseline works like the baselines of the radio bands: the user records "what is here normally"; a network
that was not in it is new, and a new network that is strong (close to you), has a hidden name, or has the name
of a known network with weaker security is worth a look. Without a baseline nothing is rated.
"""
from __future__ import annotations

import re
import time

from . import oui

MAX_NETS = 600            # networks kept (the oldest not in the baseline go first)
STRONG_DBM = -60          # a signal this strong or stronger is most likely within the building
HEARD_S = 600             # "heard now": in a scan within this many seconds
TWIN_SPAN = 8             # v4.12.0: addresses equal but for the last byte, within this many, are one device's radios
LONG_UP_S = 14 * 86400    # a router up for two weeks or more is an old fixture: a strong signal alone is not news
JUST_UP_S = 3600          # up for less than an hour: it came up just now
MARKS = ("mine", "known", "no")
HIST_SLOT_S = 900         # v4.13.0: the signal history keeps each network's best signal per 15 minutes ...
HIST_SLOTS = 96           # ... over the last 24 hours (96 numbers at most per address: wifi.json stays small)
GONE_S = 2 * 3600         # a steady baseline network (or one marked yours) not heard for this long has gone: a notice
STEADY_FRAC = 0.8         # steady: heard in this share of the scanned quarter hours before it went ...
STEADY_MIN_SLOTS = 8      # ... over at least two hours of scans (weak networks at the edge come and go all day)
NOTICE_AGAIN_S = 86400    # one notice per network per day
NOTICE_GROUP = 3          # more new networks than this at once: one notice for all of them
SECURITY_ORDER = ("open", "WEP", "WPA", "WPA2", "WPA3")   # weakest first

_TSF_RX = re.compile(r"^(\d+)\s*usec")
_BSS_RX = re.compile(r"^BSS ([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")
_ESC_RX = re.compile(r"\\x([0-9A-Fa-f]{2})")


def channel_of(freq_mhz) -> int | None:
    """The Wi-Fi channel number of a centre frequency (2.4, 5 and 6 GHz), None when it is none."""
    try:
        f = int(round(float(freq_mhz)))
    except (TypeError, ValueError):
        return None
    if f == 2484:
        return 14
    if 2412 <= f <= 2472:
        return (f - 2407) // 5
    if 5160 <= f <= 5885:
        return (f - 5000) // 5
    if 5955 <= f <= 7115:
        return (f - 5950) // 5
    return None


def band_of(freq_mhz) -> str:
    try:
        f = float(freq_mhz)
    except (TypeError, ValueError):
        return ""
    return "2.4 GHz" if f < 3000 else "5 GHz" if f < 5925 else "6 GHz"


def _ssid_text(raw: str):
    """(name, hidden): iw prints bytes it cannot show as \\xNN; an empty name or one of only zero bytes is hidden."""
    raw = raw.strip()
    codes = _ESC_RX.findall(raw)
    if raw == "" or (codes and _ESC_RX.sub("", raw) == "" and all(c == "00" for c in codes)):
        return "", True
    name = "".join(ch if ch.isprintable() else "?" for ch in _ESC_RX.sub("?", raw)).strip()
    return name, not name


def parse_iw_dump(text: str) -> list:
    """`iw dev X scan dump` -> [{bssid, ssid, hidden, freq, chan, band, security, signal_dbm, associated}]."""
    nets, cur, section = [], None, ""

    def close():
        if cur and cur.get("freq") is not None:
            if "width" not in cur and cur.get("_ht_width"):
                cur["width"] = cur["_ht_width"]
            sec = ("WPA3" if cur.pop("_sae", False) else "WPA2" if cur.get("_rsn") else "WPA" if cur.get("_wpa")
                   else "WEP" if cur.get("_privacy") else "open")
            cur["security"] = sec
            for k in [k for k in cur if k.startswith("_")]:
                cur.pop(k, None)
            cur["chan"] = channel_of(cur["freq"])
            cur["band"] = band_of(cur["freq"])
            nets.append(cur)

    for line in str(text or "").splitlines():
        m = _BSS_RX.match(line)
        if m:
            close()
            cur = {"bssid": m.group(1).lower(), "ssid": "", "hidden": True, "freq": None, "signal_dbm": None,
                   "associated": "associated" in line}
            section = ""
            continue
        if cur is None or not line.startswith("\t"):
            continue
        body = line.strip()
        if not line.startswith("\t\t"):
            key, _, val = body.partition(":")
            key, val = key.strip(), val.strip()
            section = key
            if key == "freq":
                try:
                    cur["freq"] = float(val)
                except ValueError:
                    pass
            elif key == "signal":
                try:
                    cur["signal_dbm"] = float(val.split()[0])
                except (ValueError, IndexError):
                    pass
            elif key == "SSID":
                cur["ssid"], cur["hidden"] = _ssid_text(val)
            elif key == "TSF":
                m = _TSF_RX.match(val)
                if m:
                    cur["uptime_s"] = int(m.group(1)) // 1_000_000       # the access point's own clock: time since it came up
            elif key == "Country":
                cur["country"] = val.split()[0][:2].upper() if val.split() else ""
            elif key == "MESH ID":
                cur["mesh_id"], _h = _ssid_text(val)
            elif key == "capability":
                cur["_privacy"] = "Privacy" in val
            elif key == "RSN":
                cur["_rsn"] = True
            elif key == "WPA":
                cur["_wpa"] = True
        elif section == "RSN" and "Authentication suites" in body and "SAE" in body and "PSK" not in body:
            cur["_sae"] = True          # SAE alone is WPA3; PSK next to it (transition mode) is still reachable as WPA2
        elif section == "WPS":
            for tag, field in (("Manufacturer", "manufacturer"), ("Model", "model"), ("Device name", "device_name")):
                if body.startswith("* " + tag + ":"):
                    cur.setdefault("wps", {})[field] = body.split(":", 1)[1].strip()[:40]
        elif section in ("HT operation", "VHT operation"):
            m = re.search(r"channel width: (\d) \((\d+) MHz\)", body)
            if m and section == "VHT operation" and m.group(1) != "0":
                cur["width"] = int(m.group(2))                           # 80 or 160
            elif "STA channel width:" in body and "width" not in cur:
                cur["_ht_width"] = 40 if body.rstrip().endswith("any") else 20
    close()
    return nets


def new_state() -> dict:
    return {"version": 1, "nets": {}, "baseline": None, "baseline_t": None, "scan_t": None, "marks": {}}


def update(state: dict, nets: list, now=None) -> dict:
    """Fold one scan into the state (in place). Returns the state."""
    now = time.time() if now is None else now
    known = state.setdefault("nets", {})
    slot = int(now // HIST_SLOT_S)
    first = slot - HIST_SLOTS + 1
    scans = [x for x in (state.get("scan_slots") or []) if first <= x <= slot]
    if not scans or scans[-1] != slot:
        scans.append(slot)
    state["scan_slots"] = scans                  # the quarter hours with a scan: a gap there is "no data", not "not heard"
    for n in nets:
        key = n["bssid"]
        r = known.get(key)
        sig = n.get("signal_dbm")
        if r is None:
            r = known[key] = {"bssid": key, "first_t": now, "seen": 0, "max_dbm": sig}
        r.update(ssid=n.get("ssid", ""), hidden=bool(n.get("hidden")), freq=n.get("freq"), chan=n.get("chan"),
                 band=n.get("band"), security=n.get("security"), signal_dbm=sig, last_t=now)
        for k in ("uptime_s", "country", "mesh_id", "wps", "width"):
            if n.get(k) not in (None, "", {}):
                r[k] = n[k]
        r["seen"] = int(r.get("seen") or 0) + 1
        v = int(round(sig)) if sig is not None else -100
        h = [x for x in (r.get("hist") or []) if x[0] >= first]
        if h and h[-1][0] == slot:
            h[-1][1] = max(h[-1][1], v)
        else:
            h.append([slot, v])
        r["hist"] = h
        if sig is not None and (r.get("max_dbm") is None or sig > r["max_dbm"]):
            r["max_dbm"] = sig
    state["scan_t"] = now
    if len(known) > MAX_NETS:
        base = state.get("baseline") or {}
        order = sorted(known, key=lambda k: (k in base, float(known[k].get("last_t") or 0)))
        for k in order[:len(known) - MAX_NETS]:
            known.pop(k, None)
    return state


def record_baseline(state: dict, now=None) -> int:
    """Everything heard so far is "normal here" (the user's button). Returns how many networks."""
    now = time.time() if now is None else now
    state["baseline"] = {k: {"ssid": r.get("ssid", ""), "security": r.get("security"), "chan": r.get("chan")}
                         for k, r in (state.get("nets") or {}).items()}
    state["baseline_t"] = now
    return len(state["baseline"])


def _weaker(a, b) -> bool:
    """Security `a` is weaker than `b`."""
    try:
        return SECURITY_ORDER.index(a) < SECURITY_ORDER.index(b)
    except ValueError:
        return False


def _up_text(sec) -> str:
    sec = int(sec)
    if sec >= 2 * 86400:
        return f"up {sec // 86400} days"
    if sec >= 3600:
        return f"up {sec // 3600} h"
    return f"up {max(1, sec // 60)} min"


def _family(bssid: str):
    """(first five bytes, last byte): one device's radios (2.4 GHz, 5 GHz, guest or backhaul networks) differ in the last byte."""
    b = bssid.lower().split(":")
    try:
        return ":".join(b[:5]), int(b[5], 16)
    except (IndexError, ValueError):
        return bssid.lower(), 0


def _reasons(r: dict, base: dict, named_sibling: str = "") -> list:
    """Why a network that is not in the baseline is worth a look, in plain words (empty: just new)."""
    out = []
    up = r.get("uptime_s")
    mx = r.get("max_dbm")
    if mx is not None and mx >= STRONG_DBM and not (up is not None and up >= LONG_UP_S):
        out.append(f"strong signal ({mx:.0f} dBm): probably inside or right next to your space")
    if r.get("hidden") and not named_sibling:
        out.append("hidden name")
    if up is not None and up < JUST_UP_S:
        out.append(f"came up just now ({_up_text(up)}): a network that was not here a moment ago")
    name = r.get("ssid")
    if name:
        for b in base.values():
            if b.get("ssid") == name and _weaker(r.get("security"), b.get("security")):
                out.append(f"same name as a known network but weaker security ({r.get('security')} instead of {b.get('security')})")
                break
    return out


def _hist(members: list, nets: dict, scans: list, now: float):
    """The row's last 24 hours, oldest first, one entry per quarter hour: its best signal in dBm (any of its radios),
    0 = scanned but not heard, None = no scan then (the helper was not running). And how many quarter hours it was
    heard in, of how many scanned."""
    cur = int(now // HIST_SLOT_S)
    first = cur - HIST_SLOTS + 1
    best = {}
    for k in members:
        for s, v in nets[k].get("hist") or []:
            if s >= first and (s not in best or v > best[s]):
                best[s] = v
    scanned = {s for s in scans if s >= first}
    out = [best[s] if s in best else (0 if s in scanned else None) for s in range(first, cur + 1)]
    return out, len(set(best) & scanned), len(scanned)


def _groups(nets: dict) -> list:
    """Lists of BSSIDs that are one device's radios: addresses equal but for the last byte (within TWIN_SPAN) with the
    same name, or both hidden."""
    by = {}
    for key, r in nets.items():
        fam, last = _family(key)
        by.setdefault((fam, r.get("ssid") or "", bool(r.get("hidden"))), []).append((last, key))
    out = []
    for members in by.values():
        members.sort()
        cur = [members[0]]
        for m in members[1:]:
            if m[0] - cur[-1][0] <= TWIN_SPAN:
                cur.append(m)
            else:
                out.append([k for _, k in cur])
                cur = [m]
        out.append([k for _, k in cur])
    return out


def _row_of(members: list, nets: dict, now: float) -> dict:
    rs = sorted((dict(nets[k]) for k in members), key=lambda x: -(x.get("signal_dbm") if x.get("signal_dbm") is not None else -999))
    top = rs[0]
    row = dict(top)
    row.pop("hist", None)
    row["key"] = sorted(members)[0]
    row["bssids"] = [r["bssid"] for r in rs]
    row["members"] = [{"bssid": r["bssid"], "band": r.get("band"), "chan": r.get("chan"), "signal_dbm": r.get("signal_dbm"),
                       "security": r.get("security"), "width": r.get("width")} for r in rs]
    row["bands"] = sorted({r.get("band") for r in rs if r.get("band")}, key=lambda b: float(b.split()[0]))
    row["max_dbm"] = max((r["max_dbm"] for r in rs if r.get("max_dbm") is not None), default=None)
    row["last_t"] = max(float(r.get("last_t") or 0) for r in rs)
    row["heard_now"] = now - row["last_t"] <= HEARD_S
    ups = [r["uptime_s"] for r in rs if r.get("uptime_s") is not None]
    row["uptime_s"] = max(ups) if ups else None
    row["up_text"] = _up_text(row["uptime_s"]) if row["uptime_s"] is not None else ""
    row["wps"] = next((r["wps"] for r in rs if r.get("wps")), None)
    row["country"] = next((r["country"] for r in rs if r.get("country")), "")
    row["private_address"] = all(oui.locally_administered(b) for b in row["bssids"])
    row["maker"] = "" if row["private_address"] else next((oui.maker(b) for b in row["bssids"] if oui.maker(b)), "")
    sec = [r.get("security") for r in rs if r.get("security") in SECURITY_ORDER]
    row["security"] = min(sec, key=SECURITY_ORDER.index) if sec else top.get("security")   # the weakest of its radios
    return row


def rows(state: dict, now=None) -> dict:
    """The list for the page: one row per device (its radios on both bands are one row), with status; and the totals."""
    now = time.time() if now is None else now
    base = state.get("baseline")
    nets = state.get("nets") or {}
    marks = state.get("marks") or {}
    scans = state.get("scan_slots") or []
    named = {}
    for key, r in nets.items():
        if r.get("ssid") and not r.get("hidden"):
            named.setdefault(_family(key)[0], []).append((_family(key)[1], r["ssid"]))
    out = []
    for members in _groups(nets):
        row = _row_of(members, nets, now)
        row["hist"], row["heard_slots"], row["scanned_slots"] = _hist(members, nets, scans, now)
        mk = {marks.get(k) for k in members} & set(MARKS)
        mark = "no" if "no" in mk else "mine" if "mine" in mk else "known" if "known" in mk else None
        row["mark"] = mark
        sibling = ""
        if row.get("hidden"):
            fam, last = _family(row["key"])
            sibling = next((n for l, n in named.get(fam, []) if abs(l - last) <= TWIN_SPAN), "")
        row["hidden_sibling"] = sibling
        notes = []
        in_base = base is not None and any(k in base for k in members)
        all_in = base is not None and all(k in base for k in members)
        if mark in ("mine", "known"):
            row["status"], row["reasons"] = mark, []
        elif base is None:
            row["status"], row["reasons"] = "unrated", []
        elif all_in and mark != "no":
            row["status"], row["reasons"] = "baseline", []
        else:
            fresh = [nets[k] for k in members if k not in base or mark == "no"]
            mx = max((x["max_dbm"] for x in fresh if x.get("max_dbm") is not None), default=None)
            reasons = _reasons(dict(row, max_dbm=mx), base, sibling)
            row["status"], row["reasons"] = ("look" if reasons else "new"), reasons
            if in_base and mark != "no":
                notes.append("one of its radios is new; the other is in the baseline")
        if sibling:
            notes.append(f"hidden name, but the same device as the network '{sibling}' (its guest, backhaul or second radio)")
        row["notes"] = notes
        out.append(row)
    rank = {"look": 0, "new": 1, "unrated": 2, "baseline": 3, "known": 4, "mine": 5}
    out.sort(key=lambda x: (rank[x["status"]], -(x.get("signal_dbm") if x.get("signal_dbm") is not None else -999)))
    counts = {s: sum(1 for x in out if x["status"] == s) for s in rank}
    return {"rows": out, "networks": len(out), "bssids": len(nets), "counts": counts, "baseline_t": state.get("baseline_t"),
            "baseline_n": len(base) if base is not None else None, "scan_t": state.get("scan_t"),
            "hidden": sum(1 for x in out if x.get("hidden")), "hist_slot_s": HIST_SLOT_S}


def set_mark(state: dict, bssids, mark) -> int:
    """Mark every address of a row 'mine', 'known' or 'no' (not known: rated by the rule even if in the baseline), or None (back
    to the rule). Returns how many addresses were changed."""
    if mark not in MARKS and mark is not None:
        raise ValueError("mark must be mine, known, no or null")
    marks = state.setdefault("marks", {})
    n = 0
    for b in bssids or []:
        b = str(b).lower()
        if b not in (state.get("nets") or {}) and b not in marks:
            continue
        if mark is None:
            n += 1 if marks.pop(b, None) is not None else 0
        else:
            marks[b] = mark
            n += 1
    state["marks_t"] = time.time()
    return n


# ---- v4.13.0: words for the chat: notices and direct answers ----------------------------------------

_CTRL_RX = re.compile(r"[\x00-\x1f\x7f]")


def _name(r: dict) -> str:
    """A row's name for a sentence. The name is what a stranger chose to broadcast: control characters go, it is cut
    at 40 characters and always quoted (never read as words of ARIS's own)."""
    if r.get("hidden") or not r.get("ssid"):
        sib = r.get("hidden_sibling")
        return f"a hidden network of the device that sends '{_CTRL_RX.sub('?', sib)[:40]}'" if sib else "a hidden network"
    return "'" + _CTRL_RX.sub("?", str(r["ssid"]))[:40] + "'"


def _where(r: dict) -> str:
    """'2.4 + 5 GHz, channels 1 and 100'"""
    bands = " + ".join(b.split()[0] for b in r.get("bands") or []) + " GHz" if r.get("bands") else ""
    ch = sorted({m.get("chan") for m in r.get("members") or [] if m.get("chan") is not None})
    chs = (f"channel {ch[0]}" if len(ch) == 1 else "channels " + ", ".join(str(c) for c in ch[:-1]) + f" and {ch[-1]}") if ch else ""
    return ", ".join(x for x in (bands, chs) if x)


def _ago(sec) -> str:
    sec = max(0, int(sec))
    if sec >= 2 * 86400:
        return f"{sec // 86400} days"
    if sec >= 3600:
        h = sec / 3600
        return f"{h:.0f} h" if h >= 3 else f"{h:.1f} h".replace(".0 h", " h")
    return f"{max(1, sec // 60)} min"


def _when(t: float, now: float) -> str:
    lt = time.localtime(t)
    return time.strftime("%H:%M" if now - t < 20 * 3600 else "%d %b %H:%M", lt)


def describe(r: dict, now=None) -> str:
    """One row in words: "'HOMENET-7F2A' (ExampleMaker): 2.4 + 5 GHz, channels 1 and 100, WPA2, -44 dBm now,
    up 16 h; in the baseline"."""
    now = time.time() if now is None else now
    head = _name(r) + (f" ({r['maker']})" if r.get("maker") else " (private address, no maker)" if r.get("private_address") else "")
    parts = [_where(r)]
    if r.get("security"):
        parts.append(str(r["security"]))
    sig = r.get("signal_dbm")
    if r.get("heard_now"):
        parts.append(f"{sig:.0f} dBm now" if sig is not None else "heard now")
    else:
        parts.append(f"not heard since {_when(r['last_t'], now)}" + (f" (it was {sig:.0f} dBm)" if sig is not None else ""))
    if r.get("up_text"):
        parts.append(r["up_text"])
    wps = r.get("wps") or {}
    dev = " ".join(str(wps.get(k)) for k in ("manufacturer", "model") if wps.get(k))
    if dev:
        parts.append("announces itself as " + _CTRL_RX.sub("?", dev)[:60])
    status = {"look": "new, worth a look", "new": "new (not in the baseline)", "baseline": "in the baseline",
              "known": "marked known", "mine": "marked as yours", "unrated": "not rated (no baseline yet)"}.get(r.get("status"), "")
    text = head + ": " + ", ".join(p for p in parts if p)
    if status:
        text += "; " + status
    if r.get("reasons"):
        text += " (" + "; ".join(r["reasons"]) + ")"
    return text


def _steady(r: dict, now: float) -> bool:
    """Heard in most of the scanned quarter hours of the day before it went quiet (not a weak network at the edge)."""
    hist = r.get("hist") or []
    cur = int(now // HIST_SLOT_S)
    last = int(float(r.get("last_t") or 0) // HIST_SLOT_S)
    upto = hist[:len(hist) - (cur - last)] if cur > last else hist
    scanned = [v for v in upto if v is not None]
    return len(scanned) >= STEADY_MIN_SLOTS and sum(1 for v in scanned if v) >= STEADY_FRAC * len(scanned)


def events(state: dict, now=None) -> list:
    """Chat notices for the newest scan: a new network worth a look (strong, hidden, came up just now, a weaker
    copy of a known name), and a steady baseline network or one marked yours that has not been heard for GONE_S. Once
    per network per day (state["noticed"]); never for networks marked known; nothing while the helper is not scanning.
    The first call after an update only notes what is there already (no flood of old news). Changes the state."""
    now = time.time() if now is None else now
    noticed = state.setdefault("noticed", {})
    for k in [k for k, t in noticed.items() if now - float(t or 0) > 3 * 86400]:
        noticed.pop(k, None)
    if state.get("baseline") is None or now - float(state.get("scan_t") or 0) > 300:
        return []
    seed = "noticed_since" not in state          # (only once there is something to rate and the helper scans)
    if seed:
        state["noticed_since"] = now
    fresh, gone = [], []
    for r in rows(state, now)["rows"]:
        k = r["key"]
        if r["status"] == "look" and r.get("heard_now"):
            if now - float(noticed.get(k) or 0) >= NOTICE_AGAIN_S:
                noticed[k] = now
                fresh.append(r)
        elif r["status"] in ("baseline", "mine") and not r.get("heard_now") and now - r["last_t"] >= GONE_S:
            gk = "gone:" + k
            if now - float(noticed.get(gk) or 0) >= NOTICE_AGAIN_S and (r["status"] == "mine" or _steady(r, now)):
                noticed[gk] = now
                gone.append(r)
    if seed:
        return []
    out = []
    if len(fresh) > NOTICE_GROUP:
        out.append(f"Wi-Fi: {len(fresh)} new networks worth a look: "
                   + "; ".join(f"{_name(r)} ({r['signal_dbm']:.0f} dBm, {r['reasons'][0].split(':')[0].split(' (')[0]})"
                               if r.get("signal_dbm") is not None and r.get("reasons") else _name(r) for r in fresh[:8])
                   + (f"; and {len(fresh) - 8} more" if len(fresh) > 8 else "") + ". See the Devices tab, Wi-Fi.")
    else:
        out += [f"Wi-Fi: a new network worth a look: {describe(r, now)}." for r in fresh]
    for r in gone:
        whose = "yours" if r["status"] == "mine" else "in the baseline"
        sig = f" at {r['signal_dbm']:.0f} dBm" if r.get("signal_dbm") is not None else ""
        up = f"; it had been {r['up_text']}" if r.get("up_text") else ""
        out.append(f"Wi-Fi: {_name(r)} ({whose}) has not been heard for {_ago(now - r['last_t'])}: "
                   f"last heard {_when(r['last_t'], now)}{sig}{up}.")
    return out


_CHAN_RX = re.compile(r"\b(?:channel|chan|ch|canal)\s*(\d{1,3})\b")
_QUOTED_RX = re.compile(r"[\"'“”‘’]([^\"'“”‘’]{1,40})[\"'“”‘’]")
MAX_ANSWER_ROWS = 8


def answer(state: dict, question: str, now=None) -> str:
    """A question about the Wi-Fi networks answered from the data, in plain words: a channel ("the hidden
    network on channel 2"), hidden, the strongest, new today, new / worth a look, open, yours, a network by name,
    else an overview. The names quoted are what the networks broadcast."""
    now = time.time() if now is None else now
    q = str(question or "").lower()
    rep = rows(state, now)
    rs = rep["rows"]
    if not rs:
        return ("No Wi-Fi networks heard yet. The Wi-Fi helper listens with the USB adapter "
                "(installed with deploy/aris-wifi.sh); once it runs, networks appear within a minute.")
    age = now - float(state.get("scan_t") or 0)
    tail = (f"\n(The last Wi-Fi scan is {_ago(age)} old: the Wi-Fi helper may have stopped.)" if age > 600 else "")
    pick, what = None, ""
    m = _CHAN_RX.search(q)
    if m:
        ch = int(m.group(1))
        pick = [r for r in rs if any(x.get("chan") == ch for x in r.get("members") or [])]
        if "hidden" in q:
            pick = [r for r in pick if r.get("hidden")]
        what = f"{'Hidden networks' if 'hidden' in q else 'Networks'} on channel {ch}"
    elif re.search(r"\bstrong|\bclosest|\bnearest|\bloudest|\bbest signal", q):
        pick = sorted((r for r in rs if r.get("heard_now") and r.get("signal_dbm") is not None),
                      key=lambda r: -r["signal_dbm"])[:3]
        what = "The strongest networks heard now"
    elif "hidden" in q:
        pick, what = [r for r in rs if r.get("hidden")], "Hidden networks"
    elif re.search(r"\btoday\b|\bhoy\b", q):
        midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        pick = [r for r in rs if min(float((state["nets"].get(b) or {}).get("first_t") or now) for b in r["bssids"]) >= midnight]
        what = "Networks first heard today"
    elif re.search(r"\bopen\b|\bunsecured\b|\bno password\b|\bsin contrase", q):
        pick, what = [r for r in rs if r.get("security") == "open"], "Open networks (no password)"
    elif re.search(r"\bmine\b|\bmy (?:own )?(?:wi-?fi|network|router)", q):
        pick, what = [r for r in rs if r.get("status") == "mine"], "Networks marked as yours"
    elif re.search(r"\bnew\b|\bnuevas?\b|\bunknown\b|\bworth a look\b|\bsuspicious\b|\bstrange\b|\bodd\b", q):
        if rep.get("baseline_n") is None:
            return ("There is no Wi-Fi baseline yet, so nothing is new or old: record one on the Devices tab "
                    "(Wi-Fi, \"Record what is normal here\", admin)." + tail)
        pick = [r for r in rs if r["status"] in ("look", "new")]
        what = "New networks (not in the baseline), worth a look first"
    else:
        names = [x.strip().lower() for x in _QUOTED_RX.findall(str(question or ""))]
        hits = [r for r in rs if r.get("ssid") and (r["ssid"].lower() in names or
                                                     re.search(r"(?<!\w)" + re.escape(r["ssid"].lower()) + r"(?!\w)", q))]
        if hits:
            pick, what = hits, "That network" if len(hits) == 1 else "Those networks"
    if pick is None:
        c = rep["counts"]
        heard = sum(1 for r in rs if r.get("heard_now"))
        lines = [f"Wi-Fi: {rep['networks']} networks ({rep['bssids']} addresses), {heard} heard now, "
                 f"{rep['hidden']} with a hidden name."]
        if rep.get("baseline_n") is None:
            lines.append("No baseline yet, so nothing is rated.")
        else:
            lines.append(f"{c['baseline']} in the baseline, {c['look'] + c['new']} new ({c['look']} worth a look)"
                         + (f", {c['mine']} yours" if c.get("mine") else "") + (f", {c['known']} marked known" if c.get("known") else "") + ".")
        top = [r for r in rs if r["status"] == "look"][:3]
        if top:
            lines.append("Worth a look:")
            lines += [f"- {describe(r, now)}." for r in top]
        lines.append("Ask e.g. \"which Wi-Fi networks are new today?\", \"what's the strongest network?\" or "
                     "\"what is the hidden network on channel 6?\". The list is on the Devices tab, Wi-Fi.")
        return "\n".join(lines) + tail
    if not pick:
        return f"{what}: none." + tail
    lines = [f"{what}: {len(pick)}." if len(pick) > 1 else f"{what}:"]
    lines += [f"- {describe(r, now)}." for r in pick[:MAX_ANSWER_ROWS]]
    if len(pick) > MAX_ANSWER_ROWS:
        lines.append(f"... and {len(pick) - MAX_ANSWER_ROWS} more on the Devices tab, Wi-Fi.")
    return "\n".join(lines) + tail
