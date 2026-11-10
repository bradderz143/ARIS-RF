"""Mode S / ADS-B frames as rtl_adsb prints them ("*8D4840D6202CC371C32CE0576098;"). rtl_adsb does not check
the checksum, so noise also comes out as "frames". An aircraft counts only from an extended squitter (DF17 / DF18,
112 bits) whose CRC-24 (ICAO Annex 10, generator 0x1FFF409) checks out; short replies carry no readable address.
Receive-only: these are public position broadcasts."""
from __future__ import annotations

import re

GEN = 0x1FFF409                                    # x^24 + ... + 1 (the Mode S parity generator)
_FRAME = re.compile(r"^\*([0-9a-fA-F]{14}|[0-9a-fA-F]{28});", re.M)


def crc_remainder(hexframe: str) -> int:
    """The CRC-24 remainder of a whole frame (data and parity): 0 for an intact DF17/18 frame."""
    nbits = len(hexframe) * 4
    v = int(hexframe, 16)
    for i in range(nbits - 24):
        if v & (1 << (nbits - 1 - i)):
            v ^= GEN << (nbits - 25 - i)
    return v & 0xFFFFFF


def parse(text: str) -> dict:
    """rtl_adsb output -> {"messages": all frames, "valid": checksum-verified squitters, "aircraft": [ICAO]}."""
    frames = _FRAME.findall(text or "")
    good, icao = 0, []
    for f in frames:
        if len(f) == 28 and (int(f[:2], 16) >> 3) in (17, 18) and crc_remainder(f) == 0:
            good += 1
            a = f[2:8].upper()
            if a not in icao:
                icao.append(a)
    return {"messages": len(frames), "valid": good, "aircraft": icao}


def make_frame(icao: int, me: str = "58B9822C7B1F2E") -> str:
    """A DF17 frame with a correct checksum (for the self-test and the simulated radios; never transmitted)."""
    head = f"8D{icao:06X}{me}"
    return head + f"{crc_remainder(head + '000000'):06X}"
