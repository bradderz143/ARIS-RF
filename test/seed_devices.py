"""Example devices for the device history (simulation only: screenshots and the API tests). Fed through the
engine's own path (aris._devices_note -> devices.json), as decoded packets would be, after _safe_packet."""
import time


def packets(now=None):
    """Decoded packets as the log holds them: a Ford car (4 tyre sensors heard together on 3 passes), a Porsche
    sensor heard once, a LaCrosse-TX141THBv2 temperature sensor every minute, and a remote (code fields, as rtl_433
    reports a Generic-Remote; the engine hashes its ID and drops the codes). Makes and IDs are made up."""
    now = time.time() if now is None else now
    clock = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
    out = []
    car = ("8a4c2f10", "8a4c3a27", "8a4c51b3", "8a4c66e9")
    for pass_t in (now - 52 * 3600, now - 27 * 3600, now - 540):      # three different days at any hour (25 h apart: 50 h failed 02:00-03:00 local)
        for i, ident in enumerate(car):
            t = pass_t + 1.3 * i
            for rep in range(2):                            # a TPMS sends its packet twice within the burst
                out.append({"type": "packet", "model": "Ford", "id": ident, "pressure_kPa": 230 + 5 * i,
                            "temperature_C": 21 + i, "flags": "a0", "mic": "CRC", "t": t + 0.4 * rep,
                            "time": clock(t), "aris_serial": "00017350", "aris_freq_mhz": 433.92,
                            "aris_band": "433 MHz band"})
    out.append({"type": "packet", "model": "Porsche", "id": "2e94b1c0", "pressure_kPa": 212, "temperature_C": 19,
                "flags": "0", "t": now - 2 * 3600, "time": clock(now - 2 * 3600), "aris_serial": "00017350",
                "aris_freq_mhz": 433.918, "aris_band": "433 MHz band"})
    for k in range(40):
        t = now - 3600 + 60 * k
        out.append({"type": "packet", "model": "LaCrosse-TX141THBv2", "id": 87, "channel": 1,
                    "temperature_C": 23.4 + 0.1 * (k % 3), "humidity": 44, "battery_ok": 1, "t": t, "time": clock(t),
                    "aris_serial": "00035720", "aris_freq_mhz": 433.92, "aris_band": "433 MHz band"})
    for k in range(3):
        t = now - 400 + 90 * k
        out.append({"type": "packet", "model": "Generic-Remote", "id": 1193046, "cmd": 12, "tristate": "0F10FF01F1F0",
                    "t": t, "time": clock(t), "aris_serial": "00017350", "aris_freq_mhz": 433.92})
    return out


def seed(eng, now=None):
    """Feed the packets through the engine's device history and write it out. Returns the store's rows."""
    for p in packets(now):
        eng._devices_note(eng._safe_packet(p))
    eng._devices_flush()
    return eng.devices_list()
