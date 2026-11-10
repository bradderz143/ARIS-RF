"""Who made a network card or radio, from the first three bytes of its address (v4.12.0).

An offline copy of the IEEE's public list of 24-bit prefixes (MA-L, about 36,000 makers) lives in the repository
(`arislib/data/oui.tsv.gz`, made from Debian's `ieee-data` package by `python3 -m arislib.oui SOURCE.csv`), so the
station never looks anything up over the network (the field kit may have none). An address whose second hex digit is
2, 6, A or E has the "locally administered" bit set: the device made the address up itself (a router's extra network, a
phone hotspot, a phone's random Wi-Fi or Bluetooth address) and the prefix says nothing about the maker.
"""
from __future__ import annotations

import csv
import gzip
import re
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data" / "oui.tsv.gz"
_TABLE = None
_ADDR_RX = re.compile(r"^([0-9A-Fa-f]{2})[:\-]?([0-9A-Fa-f]{2})[:\-]?([0-9A-Fa-f]{2})")
_SUFFIX_RX = re.compile(r"[,.]?\s+(?:co\.?,?\s*ltd\.?|corporation|corp\.?|incorporated|inc\.?|ltd\.?|limited|llc|gmbh|s\.?a\.?|b\.?v\.?|"
                        r"company|technologies|technology|electronics)\s*$", re.I)


def _short(name: str) -> str:
    """'TP-LINK TECHNOLOGIES CO.,LTD.' -> 'TP-LINK'; a long name in capitals is written in words ('XIAOMI COMMUNICATIONS' -> 'Xiaomi Communications')."""
    n = " ".join(str(name).split())
    for _ in range(3):
        m = _SUFFIX_RX.sub("", n)
        if m == n or not m:
            break
        n = m
    n = n.strip(" ,.")
    if n.isupper() and (" " in n or len(n) > 8):
        n = n.title()                      # a single short word in capitals (TP-LINK, HUAWEI) is most likely the brand's own spelling
    return n or str(name).strip()


def _load() -> dict:
    global _TABLE
    if _TABLE is None:
        t = {}
        try:
            with gzip.open(DATA, "rt", encoding="utf-8") as f:
                for line in f:
                    k, _, v = line.rstrip("\n").partition("\t")
                    if len(k) == 6:
                        t[k] = v
        except OSError:
            pass
        _TABLE = t
    return _TABLE


def locally_administered(addr: str) -> bool:
    """The address's second hex digit is 2, 6, A or E: made up by the device, not assigned to a maker."""
    m = _ADDR_RX.match(str(addr or ""))
    return bool(m) and int(m.group(1), 16) & 0x02 == 0x02


def maker(addr: str) -> str:
    """The maker's name for an address ('' when the prefix is not on the list or the address is locally administered)."""
    m = _ADDR_RX.match(str(addr or ""))
    if not m or locally_administered(addr):
        return ""
    return _load().get("".join(m.groups()).upper(), "")


def build(src: str, dst: str = str(DATA)) -> int:
    """Write the compact list from the IEEE's oui.csv; returns how many prefixes."""
    rows = {}
    with open(src, newline="", encoding="utf-8", errors="replace") as f:
        for r in csv.DictReader(f):
            if r.get("Registry") == "MA-L" and len(r.get("Assignment", "")) == 6:
                rows[r["Assignment"].upper()] = _short(r["Organization Name"])
    with gzip.open(dst, "wt", encoding="utf-8", compresslevel=9) as f:
        for k in sorted(rows):
            f.write(f"{k}\t{rows[k]}\n")
    return len(rows)


if __name__ == "__main__":
    print(build(sys.argv[1]) if len(sys.argv) > 1 else "usage: python3 -m arislib.oui /usr/share/ieee-data/oui.csv")
