"""Device history: every device a decoder named (tyre sensors, weather sensors, remotes ...), one record per
model + ID, kept across the event log's cap. The store is a plain dict; aris.py keeps it in DATA_DIR/devices.json
and feeds it every decoded packet after _safe_packet (no remote codes; a remote's ID is a hashed label "#3fa2c1").

The rules (the Devices tab and /api/devices state them too):
- A packet more than TRANSMISSION_GAP_S (2 s) after the device's last one starts a new transmission (also one
  that arrives with an earlier time: another dongle's copy of the same packet).
- Heard together (v4.9.10): tyre sensors of one make heard within TOGETHER_S (5 s) of each other, even once, are one row
  (`together_of`: 2 to TOGETHER_MAX linked sensors; a bigger group is a queue and stays single rows), shown as
  probably one car. Set: sensors of the same make heard within TOGETHER_S (5 s) of each other on SET_MIN_TIMES (2) or more occasions
  (occasions at least OCCASION_GAP_S = 120 s apart: a queue of traffic lasts longer than 30 s) are shown as one set
  ("Citroen car: 4 tyre sensors"): they always transmit together. Per device the IDs it was heard with and how
  often, capped at SEEN_WITH_MAX (8). A pair joins a set only when a third device was heard with both (a car's
  sensors are all heard with each other), or when it is the only pair either one has: one shared pair never joins
  two cars that stood in the same queue (sets_of).
- At most MAX_DEVICES (1500), pruned in this order (v4.9.9; before, the 500 ones heard last stayed, and on the
  station passing cars filled them within a day): a passing tyre sensor (one sensor, one day, in no set, not
  marked) goes after PASSING_KEEP_S (3 days) or when more than PASSING_MAX (1000) are kept (the oldest first);
  then, over the limit, other unmarked devices not seen longest, and known devices (the rule or marked) last, so
  a known device is not forgotten (it would be new again). DAYS_MAX dates per device, READING_MAX
  reading fields (the plain ones: pressure, temperature, battery ...). A remote or code-carrying device (its ID is
  a '#' label) keeps only the readings this module knows words for: rtl_433's remote decoders report the parts of
  the code under their own names (Secplus-v1: id0, id1, pin, pad_id; Cardin: rbutton; Interlogix: raw_message).
"""
from __future__ import annotations

import re
import time

TRANSMISSION_GAP_S = 2.0
TOGETHER_S = 5.0        # v4.9.10 (was 10): sensors of one make heard this close are one pass
TOGETHER_MAX = 5        # most sensors one 'heard together' group can hold (a car has 4, with a spare 5); more = a queue, left alone
OCCASION_GAP_S = 120.0
SET_MIN_TIMES = 2
SEEN_WITH_MAX = 8
MAX_DEVICES = 1500
PASSING_MAX = 1000       # passing tyre sensors kept (the rest of the budget is for devices that matter)
PASSING_KEEP_S = 3 * 86400   # a passing sensor not heard again for this long is gone (a car that comes back does within days)
DAYS_MAX = 30
READING_MAX = 8
VERSION = 2        # 2: remote labels keyed (aris._safe_packet), readings of remotes without code parts (scrub)
NOISE_AFTER_S = 900   # v4.9.11: a sensor heard once and not again for this long is "likely noise" (a real one repeats)
KNOWN_DAYS = 3     # a device heard on this many different days is "known" (its own sensors, the neighbours') ...
KNOWN_RULE = (f"Known: heard on {KNOWN_DAYS} or more different days (your own sensors, the neighbours' weather station), "
              f"or marked by you. Known devices are folded into one line in capture results and in what the AI reads, "
              f"so new and undecoded signals get the attention; a set is known when all its members are.")

RULE = ("Grouped because they transmit together: tyre sensors of one make heard within 5 s of each other are one row "
        "(probably one car); other sensors of the same make heard that close on 2 or more occasions are one set. A sensor "
        "heard on its own stays its own row.")

# Fields that describe the reception or the log entry, not the device's reading.
META_KEYS = {"time", "t", "type", "aris_serial", "aris_heard_by", "aris_freq_mhz", "aris_band", "mod", "freq",
             "freq1", "freq2", "rssi", "snr", "noise", "protocol", "mic", "model", "id", "also_read_as", "brand",
             "subtype"}
# Code-carrying fields never reach the store (aris._safe_packet strips them first; this is the second guard).
_CODE_RX = re.compile(r"^(?:tristate|code|codes|cmd|button|rows|data|raw|raw_msg|raw_message|rolling|fixed|secret|"
                      r"counter|encrypted|remote_id|button_id|command|unit|group|group_call|dipswitch|switch_id|"
                      r"house_id|cmd_id|key|pin|pad_id|id0|id1|rbutton|extradata)$|_code$", re.I)

# The reading in plain words, in this order. Only these fields are ever shown (reading_words): any other decoder
# field stays in the stored reading and out of the page and the chat (its name is rtl_433's, not plain words).
_WORDS = [
    ("pressure_kPa", lambda v: f"{float(v) / 100:.1f} bar"), ("pressure_bar", lambda v: f"{float(v):.1f} bar"),
    ("pressure_PSI", lambda v: f"{float(v):.0f} psi"), ("pressure_hPa", lambda v: f"{float(v):.0f} hPa"),
    ("pressure_inHg", lambda v: f"{float(v):.2f} inHg"),
    ("temperature_C", lambda v: f"{float(v):g} °C"), ("temperature_F", lambda v: f"{float(v):g} °F"),
    ("temperature_1_C", lambda v: f"{float(v):g} °C"), ("temperature_2_C", lambda v: f"{float(v):g} °C (2)"),
    ("temperature_3_C", lambda v: f"{float(v):g} °C (3)"),
    ("humidity", lambda v: f"{float(v):g}% humidity"), ("moisture", lambda v: f"{float(v):g}% moisture"),
    ("wind_avg_km_h", lambda v: f"wind {float(v):g} km/h"), ("wind_avg_m_s", lambda v: f"wind {float(v):g} m/s"),
    ("wind_avg_mi_h", lambda v: f"wind {float(v):g} mph"),
    ("wind_max_km_h", lambda v: f"gusts {float(v):g} km/h"), ("wind_max_m_s", lambda v: f"gusts {float(v):g} m/s"),
    ("wind_max_mi_h", lambda v: f"gusts {float(v):g} mph"),
    ("wind_dir_deg", lambda v: f"wind from {float(v):.0f}°"), ("rain_mm", lambda v: f"rain {float(v):g} mm"),
    ("rain_in", lambda v: f"rain {float(v):g} in"), ("rain_rate_mm_h", lambda v: f"rain {float(v):g} mm/h"),
    ("light_lux", lambda v: f"{float(v):g} lux"), ("lux", lambda v: f"{float(v):g} lux"),
    ("uv", lambda v: f"UV {float(v):g}"), ("uvi", lambda v: f"UV {float(v):g}"),
    ("strike_count", lambda v: f"{int(float(v))} lightning strikes"), ("depth_cm", lambda v: f"depth {float(v):g} cm"),
    ("pm2_5_ug_m3", lambda v: f"PM2.5 {float(v):g} µg/m³"), ("pm10_ug_m3", lambda v: f"PM10 {float(v):g} µg/m³"),
    ("power_W", lambda v: f"{float(v):g} W"), ("energy_kWh", lambda v: f"{float(v):g} kWh"),
    ("voltage_V", lambda v: f"{float(v):g} V"), ("current_A", lambda v: f"{float(v):g} A"),
    ("consumption", lambda v: f"meter {v}"), ("consumption_data", lambda v: f"meter {v}"),
    ("co2_ppm", lambda v: f"CO2 {float(v):g} ppm"),
    ("battery_ok", lambda v: "battery ok" if _truthy(v) else "battery low"),
    ("battery_low", lambda v: "battery low" if _truthy(v) else "battery ok"),
    ("battery", lambda v: ("battery ok" if str(v).lower() in ("ok", "1", "true", "good", "high", "full")
                           else "battery low" if str(v).lower() in ("low", "0", "false", "weak") else f"battery {v}")),
    ("battery_V", lambda v: f"battery {float(v):.2f} V"), ("battery_mV", lambda v: f"battery {float(v) / 1000:.2f} V"),
    ("contact_open", lambda v: "open" if _truthy(v) else "closed"),
    ("reed_open", lambda v: "open" if _truthy(v) else "closed"),
    ("motion", lambda v: "motion" if _truthy(v) else "no motion"),
    ("alarm", lambda v: "alarm" if _truthy(v) else "no alarm"), ("smoke", lambda v: "smoke" if _truthy(v) else "no smoke"),
    ("tamper", lambda v: "tamper" if _truthy(v) else "no tamper"),
    ("channel", lambda v: f"channel {v}"),
]
_WORD_KEYS = [k for k, _ in _WORDS]

# What a kind of device is called in a title: "Citroen tyre sensor 92cf949a", "Citroen car: 4 tyre sensors".
# (doorbell before door: 'wireless doorbell' is not a door sensor)
_SHORT = [("tyre", "tyre sensor"), ("meter", "meter"), ("energy", "energy monitor"), ("smoke", "smoke alarm"),
          ("leak", "leak sensor"), ("doorbell", "doorbell"), ("door", "door sensor"), ("motion", "motion sensor"),
          ("alarm-system", "alarm sensor"), ("weather", "weather station"), ("temperature", "temperature sensor"),
          ("remote", "remote")]


def _truthy(v) -> bool:
    return str(v).lower() in ("1", "true", "yes", "ok", "on", "open", "high") or v is True


def new_store(**extra) -> dict:
    return {"version": VERSION, "devices": {}, **extra}


def key_of(model, ident) -> str:
    return f"{model}|{'' if ident is None else ident}"


def is_remote(p: dict, kind: str = "") -> bool:
    """A remote or code-carrying device: its ID is a '#' label (aris._safe_packet), or its kind says so."""
    k = str(kind or "").lower()
    return str(p.get("id") or "").startswith("#") or "remote" in k or "button" in k


def reading_of(p: dict, plain_only: bool = False) -> dict:
    """The device's own reading: the plain fields, at most READING_MAX, known ones first. Never a code field.
    plain_only (remotes): only the fields with words, so no part of a code reaches the store under a decoder's
    own field name."""
    plain = {k: v for k, v in p.items() if k not in META_KEYS and not _CODE_RX.search(str(k))
             and isinstance(v, (int, float, str, bool))}
    out = {}
    for k in _WORD_KEYS:
        if k in plain and len(out) < READING_MAX:
            out[k] = plain[k]
    if plain_only:
        return out
    for k, v in plain.items():
        if k not in out and len(out) < READING_MAX:
            out[k] = v
    return out


def reading_words(reading: dict, limit: int = 6) -> str:
    """'2.3 bar, 21 °C, battery ok' from a stored reading: the fields with words only, never a field name."""
    out = []
    for k, fn in _WORDS:
        if k in reading and len(out) < limit:
            try:
                out.append(fn(reading[k]))
            except (TypeError, ValueError):
                pass
    return ", ".join(out)


def scrub(store: dict) -> int:
    """Bring a store from an earlier version up to VERSION: readings re-filtered (remotes keep only the fields
    with words), and remote rows with a label from before the labels were keyed dropped (they cannot be re-keyed;
    the next decode lists the remote again). Returns how many devices changed or went."""
    n = 0
    if int(store.get("version") or 0) >= VERSION:
        return 0
    devs = store.get("devices") or {}
    for k in list(devs):
        d = devs[k]
        if str(d.get("id") or "").startswith("#"):
            devs.pop(k)
            n += 1
            continue
        r = reading_of(d.get("reading") or {}, plain_only=is_remote(d, d.get("kind")))
        if r != (d.get("reading") or {}):
            d["reading"] = r
            n += 1
    store["version"] = VERSION
    return n


def short_kind(kind) -> str:
    k = str(kind or "").lower()
    return next((w for word, w in _SHORT if word in k), "device")


def _freq_of(p: dict):
    for k in ("aris_freq_mhz", "freq"):
        v = p.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            return round(float(v), 4)
    return None


def _day_of(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _note_pair(d: dict, other_id, t: float) -> None:
    """d was heard within TOGETHER_S of other_id: one more occasion, unless the last one was under OCCASION_GAP_S
    ago (a sensor repeating within the same pass). The list keeps the SEEN_WITH_MAX most frequent partners."""
    w = d.setdefault("with", {})
    e = w.get(other_id)
    if e is None:
        w[other_id] = {"n": 1, "t": round(t, 2)}
    elif t - float(e.get("t") or 0) >= OCCASION_GAP_S:
        e["n"] = int(e.get("n") or 0) + 1
        e["t"] = round(t, 2)
    if len(w) > SEEN_WITH_MAX:
        keep = sorted(w.items(), key=lambda kv: (-int(kv[1].get("n") or 0), -float(kv[1].get("t") or 0)))
        d["with"] = dict(keep[:SEEN_WITH_MAX])


def apply_packet(store: dict, p: dict, kind: str = "", dongle=None, name=None) -> bool:
    """One decoded packet (after _safe_packet) into the store. Returns False when it names no device. `dongle`:
    the dongle's name, or the names of every dongle that heard this packet. `name`: what the device is called in
    plain words when that differs from the decoder's model string ('Generic fixed-code remote ...')."""
    model = p.get("model")
    if model in (None, ""):
        return False
    try:
        t = round(float(p.get("t") or 0) or time.time(), 2)
    except (TypeError, ValueError):
        t = round(time.time(), 2)
    ident = p.get("id")
    ident = None if ident is None else str(ident)
    when = str(p.get("time") or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)))
    devs = store.setdefault("devices", {})
    key = key_of(model, ident)
    d = devs.get(key)
    # (an earlier time within the gap is the same transmission: another dongle's copy, a second earlier)
    new_tx = d is None or abs(t - float(d.get("last_t") or 0)) > TRANSMISSION_GAP_S
    if d is None:
        d = devs[key] = {"model": str(model), "id": ident, "kind": kind or "", "first_seen": when, "first_t": t,
                         "last_seen": when, "last_t": t, "packets": 0, "transmissions": 0, "freq_min": None,
                         "freq_max": None, "dongles": [], "reading": {}, "days": [], "with": {}}
    if kind and (not d.get("kind") or d["kind"] == "device decoded by rtl_433"):
        d["kind"] = kind
    if name and str(name) != str(model):
        d["name"] = str(name)
    d["packets"] = int(d.get("packets") or 0) + 1
    if new_tx:
        d["transmissions"] = int(d.get("transmissions") or 0) + 1
        for ok, o in devs.items():                    # the same make, heard just before: a set in the making
            if ok != key and o.get("model") == str(model) and o.get("id") not in (None, ident) \
                    and 0 <= t - float(o.get("last_t") or 0) <= TOGETHER_S:
                _note_pair(d, str(o["id"]), t)
                _note_pair(o, ident, t)
    if t >= float(d.get("last_t") or 0):
        d["last_t"], d["last_seen"] = t, when
        r = reading_of(p, plain_only=is_remote(p, d.get("kind")))
        if r:
            d["reading"] = r
        snr = p.get("snr")
        if isinstance(snr, (int, float)) and not isinstance(snr, bool):
            d["snr_db"] = round(float(snr), 1)
    if t < float(d.get("first_t") or t):
        d["first_t"], d["first_seen"] = t, when
    f = _freq_of(p)
    if f is not None:
        d["freq_min"] = f if d.get("freq_min") is None else min(float(d["freq_min"]), f)
        d["freq_max"] = f if d.get("freq_max") is None else max(float(d["freq_max"]), f)
    for x in (dongle if isinstance(dongle, (list, tuple)) else [dongle]):
        if x and x not in d.setdefault("dongles", []):
            d["dongles"].append(x)
    day = _day_of(t)
    days = d.setdefault("days", [])
    if day not in days:
        days.append(day)
        if len(days) > DAYS_MAX:
            d["days"] = sorted(days)[-DAYS_MAX:]
    return True


def is_known(d: dict) -> bool:
    """A known device: marked by the user ("known": "yes" / "no" wins), else heard on KNOWN_DAYS or more days."""
    m = str(d.get("known") or "").lower()
    if m in ("yes", "no"):
        return m == "yes"
    return len(d.get("days") or []) >= KNOWN_DAYS


def known_by(d: dict):
    """'you' (marked), 'days' (the rule), or None."""
    m = str(d.get("known") or "").lower()
    if m == "yes":
        return "you"
    if m == "no":
        return None
    return "days" if len(d.get("days") or []) >= KNOWN_DAYS else None


def mark_known(store: dict, key: str, known) -> int:
    """Mark a device (or a set: 'set:k1+k2') as known (True), not known / watch it (False) or back to the rule
    (None). Returns how many records changed."""
    devs = store.get("devices") or {}
    keys = key[4:].split("+") if str(key).startswith("set:") else [key]
    n = 0
    for k in keys:
        d = devs.get(k)
        if d is None:
            continue
        if known is None:
            d.pop("known", None)
        else:
            d["known"] = "yes" if known else "no"
        n += 1
    return n


def known_keys(store: dict) -> set:
    return {k for k, d in (store.get("devices") or {}).items() if is_known(d)}


def fold_known(store: dict, heard: list) -> tuple:
    """Fold the known devices out of what was just heard. `heard`: [{"model", "id", "kind", "packets",
    "transmissions"}] (one per device). Returns (line or None, the rest): the line is one sentence for all the
    known devices ("Known: Citroen car (4 tyre sensors), Nexus-TH temperature sensor; 40 transmissions"), the rest
    are the entries of devices that are not known, in the order given."""
    devs = store.get("devices") or {}
    known, rest = [], []
    for h in heard:
        d = devs.get(key_of(h.get("model"), h.get("id")))
        (known if d is not None and is_known(d) else rest).append(h)
    if not known:
        return None, rest
    # grouped by set (a car's sensors, by the set rule) or, for devices in no set, by model; a set is called a
    # car only when its members really form one (never two sensors of one make that were never heard together)
    set_of = {k: i for i, grp in enumerate(sets_of(devs)) for k in grp}
    groups = {}
    for h in known:
        key = key_of(h.get("model"), h.get("id"))
        d = devs.get(key) or {}
        gk = ("set", set_of[key]) if key in set_of else ("model", str(h.get("model")))
        e = groups.setdefault(gk, {"n": 0, "kind": h.get("kind") or d.get("kind"), "name": d.get("name"),
                                   "model": str(h.get("model")), "ids": [], "remote": is_remote(d, d.get("kind"))})
        e["n"] += 1
        e["ids"].append(h.get("id"))
    bits = []
    for gk, e in groups.items():
        short = short_kind(e["kind"])
        if e["n"] == 1:                              # one device: its title (a remote keeps its '#' label)
            bits.append(_title(e["model"], e["kind"], e["ids"][0] if e["remote"] else None, name=e["name"]))
        elif gk[0] == "set":
            bits.append(_title(e["model"], e["kind"], count=e["n"], name=e["name"]))
        else:
            bits.append(f"{e['n']} {e['name'] or e['model']} {short}s")
    tx = sum(int(h.get("transmissions") or 0) for h in known)
    pk = sum(int(h.get("packets") or 0) for h in known)
    tail = (f"{tx} transmission{'s' if tx != 1 else ''}" if tx else f"{pk} packet{'s' if pk != 1 else ''}")
    return f"Known: {', '.join(bits)}; {tail}", rest


def fold_passing(heard: list) -> tuple:
    """Tyre sensors not known here (passing cars have new IDs every time and are grouped by make, not learned one
    by one): when two or more makes of tyre sensor were heard, one line names them all, without claiming whose
    they are. Returns (line or None, the rest)."""
    tyre = [h for h in heard if short_kind(h.get("kind")) == "tyre sensor"]
    if len({str(h.get("model")) for h in tyre}) < 2:
        return None, heard
    makes = {}
    for h in tyre:
        makes[str(h.get("model"))] = makes.get(str(h.get("model")), 0) + 1
    tx = sum(int(h.get("transmissions") or 0) for h in tyre)
    line = (f"Tyre sensors not known here (probably passing cars): "
            + ", ".join(f"{m} ({n})" if n > 1 else m for m, n in sorted(makes.items(), key=lambda kv: -kv[1]))
            + (f"; {tx} transmission{'s' if tx != 1 else ''}" if tx else ""))
    return line, [h for h in heard if h not in tyre]


def _passing(d: dict, in_set: bool) -> bool:
    """A passing tyre sensor: one sensor, one day, in no set, not marked by the user: most likely a car that passed."""
    return (short_kind(d.get("kind")) == "tyre sensor" and not in_set and len(d.get("days") or []) < 2
            and str(d.get("known") or "").lower() not in ("yes", "no"))


def prune(store: dict, limit: int = MAX_DEVICES, now=None) -> int:
    """Forget devices, passing tyre sensors first (see the module's rules). Returns how many went."""
    devs = store.get("devices") or {}
    if not devs:
        return 0
    now = time.time() if now is None else float(now)
    before = len(devs)
    in_set = {k for g in sets_of(devs) for k in g}
    passing = sorted((k for k, d in devs.items() if _passing(d, k in in_set)),
                     key=lambda k: float(devs[k].get("last_t") or 0))
    gone = [k for k in passing if now - float(devs[k].get("last_t") or 0) > PASSING_KEEP_S]
    gone += [k for k in passing if k not in gone][:max(0, len(passing) - len(gone) - PASSING_MAX)]
    for k in gone:
        devs.pop(k, None)
    if len(devs) > limit:
        # (passing sensors first, then the unmarked ones, then known ones: the ones the user marked go last)
        def rank(k):
            d = devs[k]
            m = str(d.get("known") or "").lower()
            return (2 if m == "yes" else 1 if (m != "no" and is_known(d)) else 0, float(d.get("last_t") or 0))
        for k in sorted(devs, key=lambda k: ((0 if _passing(devs[k], k in in_set) else 1),) + rank(k))[:len(devs) - limit]:
            devs.pop(k, None)
    return before - len(devs)


def _links(devs: dict) -> dict:
    """key -> set of keys of the same model heard with it on SET_MIN_TIMES or more occasions (either way)."""
    out = {k: set() for k in devs}
    for k, d in devs.items():
        for oid, e in (d.get("with") or {}).items():
            ok = key_of(d.get("model"), oid)
            if ok not in devs or ok == k:
                continue
            n = max(int(e.get("n") or 0), int(((devs[ok].get("with") or {}).get(str(d.get("id"))) or {}).get("n") or 0))
            if n >= SET_MIN_TIMES:
                out[k].add(ok)
                out[ok].add(k)
    return out


def sets_of(devs: dict) -> list:
    """Sets of keys: devices of one model heard together (SET_MIN_TIMES occasions), joined up. A pair counts only
    when a third device is linked to both (a car's sensors are all heard with each other) or when it is the only
    link either one has (a two-sensor set): two cars that stood in the same queue twice share one pair, and that
    pair must not fold eight sensors into one 'car'."""
    parent = {k: k for k in devs}

    def root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    links = _links(devs)
    for k, others in links.items():
        for ok in others:
            if (links[k] & links[ok]) or (links[k] == {ok} and links[ok] == {k}):
                parent[root(k)] = root(ok)
    groups = {}
    for k in devs:
        groups.setdefault(root(k), []).append(k)
    return [sorted(g, key=lambda k: -float(devs[k].get("last_t") or 0)) for g in groups.values() if len(g) > 1]


def together_of(devs: dict, taken=()) -> list:
    """Groups of tyre sensors of one make that were heard within TOGETHER_S of each other (even on one occasion, the
    'with' notes), outside `taken` (the sets). 2 to TOGETHER_MAX sensors: more is a queue of cars, not one car, and
    stays single rows. Newest first."""
    ok = {k for k, d in devs.items() if k not in taken and short_kind(d.get("kind")) == "tyre sensor"}
    parent = {k: k for k in ok}

    def root(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for k in ok:
        for oid in (devs[k].get("with") or {}):
            o = key_of(devs[k].get("model"), oid)
            if o in ok and o != k:
                parent[root(k)] = root(o)
    groups = {}
    for k in ok:
        groups.setdefault(root(k), []).append(k)
    return [sorted(g, key=lambda k: -float(devs[k].get("last_t") or 0)) for g in groups.values()
            if 2 <= len(g) <= TOGETHER_MAX]


def when_words(t, now=None) -> str:
    """'18:40 (today)', '09:12 (yesterday)', '2 Nov 15:21', '2 Nov 2025 15:21'."""
    now = time.time() if now is None else now
    try:
        t = float(t)
    except (TypeError, ValueError):
        return "?"
    lt, ln = time.localtime(t), time.localtime(now)
    hm = time.strftime("%H:%M", lt)
    if lt[:3] == ln[:3]:
        return f"{hm} (today)"
    if lt[:3] == time.localtime(now - 86400)[:3]:
        return f"{hm} (yesterday)"
    if lt.tm_year == ln.tm_year:
        return f"{lt.tm_mday} {time.strftime('%b', lt)} {hm}"
    return f"{lt.tm_mday} {time.strftime('%b %Y', lt)} {hm}"


def _mhz(f) -> str:
    return ("%.3f" % float(f)).rstrip("0").rstrip(".")


def freq_text(fmin, fmax) -> str:
    """'433.92 MHz', '433.918-433.935 MHz' (a device's frequencies, as measured by band recordings)."""
    if fmin is None:
        return ""
    if fmax is None or abs(float(fmax) - float(fmin)) < 0.0015:
        return f"{_mhz(fmin)} MHz"
    return f"{_mhz(fmin)}-{_mhz(fmax)} MHz"


def _seen_line(n, first_t, last_t, fmin, fmax, dongles, now) -> str:
    """'Seen 14 times since 2 Nov 15:21, last 18:40 (today) | 433.92 MHz | Dongle 1'."""
    first, last = when_words(first_t, now), when_words(last_t, now)
    bits = [f"Seen once at {first}" if n == 1 else
            f"Seen {n} times since {first}" + (f", last {last}" if last != first else "")]
    ft = freq_text(fmin, fmax)
    if ft:
        bits.append(ft)
    if dongles:
        bits.append(", ".join(dongles))
    return " | ".join(bits)


def _title(model, kind, ident=None, count=None, name=None, together=False) -> str:
    """'Citroen tyre sensor 92cf949a', 'Citroen car: 4 tyre sensors', 'Generic fixed-code remote ... #3fa2c1' (the
    plain name the investigation reply uses, when the store has one)."""
    short = short_kind(kind)
    m = str(name or model)
    if count is not None and together:
        return f"{m}: {count} {short}s heard together"
    if count is not None:
        what = "car" if short == "tyre sensor" else "set"
        return f"{m} {what}: {count} {short}s"
    if short == "device" or short.split()[0] in m.lower():
        return f"{m} {ident}" if ident not in (None, "") else m
    return f"{m} {short} {ident}" if ident not in (None, "") else f"{m} {short}"


GROUPS = ("vehicle", "sensor", "remote")


def group_of(short: str, ident, kind: str = "") -> str:
    """Which tab of the Devices page a device belongs to (v4.9.7): 'vehicle' (tyre sensors: a car's set or one
    sensor of a car that passed), 'remote' (a '#' label or a remote kind: never shown with a code), 'sensor' (every
    other device a decoder names: weather, meters, alarms ...)."""
    if short == "tyre sensor":
        return "vehicle"
    if is_remote({"id": ident}, kind):
        return "remote"
    return "sensor"


def _likely_noise(r: dict, now: float) -> bool:
    """A sensor row (not a vehicle, not a remote, not a set) heard exactly once, a while ago, and not marked by
    the user: a real weather station or meter transmits every minute or so, so one packet that never came back is
    most likely a decoder reading noise or a burst of another device (v4.9.11: an 'FS20' at 433.9, a barbecue
    thermometer at -200 C, both seen once). Only a label: nothing is hidden or dropped."""
    if r.get("group") != "sensor" or r.get("kind") != "device" or r.get("mark") == "yes":
        return False
    return (int(r.get("transmissions") or 0) <= 1 and int(r.get("days") or 0) < 2
            and now - float(r.get("last_t") or now) >= NOISE_AFTER_S)


def _vehicle_kind(r: dict) -> str:
    """For a vehicle row: 'car' (a set: several sensors that always transmit together, so the same car came by
    more than once or stayed), 'again' (one sensor heard on 2 or more days: a car that comes back), 'passing' (one
    sensor, one day: most likely a car passing; the page never says whose)."""
    if r.get("kind") == "set":
        return "together" if r.get("once") else "car"
    return "again" if int(r.get("days") or 0) >= 2 else "passing"


def _row(key, d, now) -> dict:
    return {"kind": "device", "key": key, "model": d.get("model"), "id": d.get("id"), "type": d.get("kind") or "",
            "short": short_kind(d.get("kind")),
            "group": group_of(short_kind(d.get("kind")), d.get("id"), d.get("kind") or ""),
            "title": _title(d.get("model"), d.get("kind"), d.get("id"), name=d.get("name")),
            "packets": int(d.get("packets") or 0), "transmissions": int(d.get("transmissions") or 0),
            "first_seen": d.get("first_seen"), "first_t": d.get("first_t"), "last_seen": d.get("last_seen"),
            "last_t": d.get("last_t"), "freq_min": d.get("freq_min"), "freq_max": d.get("freq_max"),
            "freq_text": freq_text(d.get("freq_min"), d.get("freq_max")), "dongles": list(d.get("dongles") or []),
            "reading": dict(d.get("reading") or {}), "reading_words": reading_words(d.get("reading") or {}),
            "days": len(d.get("days") or []), "snr_db": d.get("snr_db"),
            "known": is_known(d), "known_by": known_by(d), "mark": d.get("known"),
            "seen_line": _seen_line(int(d.get("transmissions") or 0), d.get("first_t"), d.get("last_t"),
                                    d.get("freq_min"), d.get("freq_max"), d.get("dongles") or [], now)}


def _set_row(grp, devs, now, once=False) -> dict:
    """One row for a set of sensors (members under it); `once`: heard together on one occasion (v4.9.10)."""
    members = [_row(k, devs[k], now) for k in grp]
    first = devs[grp[0]]
    fmins = [m["freq_min"] for m in members if m["freq_min"] is not None]
    fmaxs = [m["freq_max"] for m in members if m["freq_max"] is not None]
    dongles = []
    for m in members:
        for x in m["dongles"]:
            if x not in dongles:
                dongles.append(x)
    n_tx = sum(m["transmissions"] for m in members)
    # the set's 'Seen N times' is its passes: each sensor transmits once per pass, so the member heard most
    # often counts them (the sum would read 'Seen 12 times' over four members each 'Seen 3 times')
    n_seen = max(m["transmissions"] for m in members)
    first_t = min(float(m["first_t"] or now) for m in members)
    last_t = max(float(m["last_t"] or 0) for m in members)
    fmin, fmax = (min(fmins) if fmins else None), (max(fmaxs) if fmaxs else None)
    return ({"kind": "set", "key": "set:" + "+".join(grp), "model": first.get("model"),
                "ids": [m["id"] for m in members], "type": first.get("kind") or "",
                "short": short_kind(first.get("kind")),
                "group": group_of(short_kind(first.get("kind")), first.get("id"), first.get("kind") or ""),
                "title": _title(first.get("model"), first.get("kind"), count=len(members), name=first.get("name"), together=once),
                "packets": sum(m["packets"] for m in members), "transmissions": n_tx, "seen": n_seen,
                "first_t": first_t, "last_t": last_t, "freq_min": fmin, "freq_max": fmax,
                "freq_text": freq_text(fmin, fmax), "dongles": dongles, "reading": members[0]["reading"],
                "reading_words": members[0]["reading_words"], "members": members,
                "once": once, "known": all(m["known"] for m in members),
                "known_by": ("you" if all(m["known_by"] == "you" for m in members) else
                             "days" if all(m["known"] for m in members) else None),
                "mark": members[0].get("mark") if all(m.get("mark") == members[0].get("mark") for m in members) else None,
                "seen_line": _seen_line(n_seen, first_t, last_t, fmin, fmax, dongles, now)})


def rows(store: dict, now=None) -> dict:
    """The list for the page and the chat: one row per device, a set's members under one row, newest last-seen
    first; and the totals."""
    now = time.time() if now is None else now
    devs = store.get("devices") or {}
    out, in_set = [], set()
    strict = sets_of(devs)
    for grp in strict:
        in_set.update(grp)
    loose = together_of(devs, in_set)
    for grp, once in [(g, False) for g in strict] + [(g, True) for g in loose]:
        in_set.update(grp)
        out.append(_set_row(grp, devs, now, once))
    for k, d in devs.items():
        if k not in in_set:
            out.append(_row(k, d, now))
    out.sort(key=lambda r: -float(r.get("last_t") or 0))
    for r in out:
        if r["group"] == "vehicle":
            r["vehicle"] = _vehicle_kind(r)
        r["noise"] = _likely_noise(r, now)
    groups = {g: sum(1 for r in out if r["group"] == g) for g in GROUPS}
    veh = {k: sum(1 for r in out if r.get("vehicle") == k) for k in ("car", "together", "again", "passing")}
    return {"rows": out, "devices": len(devs), "sets": sum(1 for r in out if r["kind"] == "set"),
            "groups": groups, "vehicles": veh, "noise": sum(1 for r in out if r.get("noise")),
            "known": sum(1 for r in out if r.get("known")),          # rows: a known car counts once
            "packets": sum(int(d.get("packets") or 0) for d in devs.values()), "rule": RULE, "known_rule": KNOWN_RULE}


def matches(row: dict, text) -> bool:
    """The page's filter box: 'Citroen', '433.9', '#3f', 'tpms', a dongle."""
    q = str(text or "").strip().lower()
    if not q:
        return True
    if q in ("vehicle", "vehicles", "car", "cars"):
        return row.get("group") == "vehicle"
    if q in ("remote", "remotes"):
        return row.get("group") == "remote"
    if q in ("sensor", "sensors"):
        return row.get("group") == "sensor"
    if q in ("known", "new", "not known", "unknown"):
        return bool(row.get("known")) == (q == "known")
    hay = " ".join(str(x) for x in (row.get("title"), row.get("model"), row.get("id"), row.get("type"),
                                   row.get("freq_text"), " ".join(row.get("dongles") or []),
                                   " ".join(str(i) for i in row.get("ids") or []), row.get("reading_words")))
    return q in hay.lower()
