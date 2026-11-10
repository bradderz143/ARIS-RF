"""Phone-width screenshots of the v4.11.0 Bluetooth view (Devices tab > Bluetooth). Needs serve.py running with
ARIS_SEED_DEVICES=1 and ARIS_BT_SCAN_FILE=<file> (this script writes the file).  python3 shot51.py [port] [outdir]"""
import asyncio
import json
import os
import sys
import time

from playwright.async_api import async_playwright

PORT = sys.argv[1] if len(sys.argv) > 1 else "5097"
OUT = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("ARIS_TEST_DIR", ".")
B = f"http://127.0.0.1:{PORT}"
PW = os.environ.get("ARIS_TEST_PASSWORD", "test-password-123")
SCAN = os.environ["ARIS_BT_SCAN_FILE"]


def snap(t, devs, trackers=None):
    return {"v": 1, "t": t, "window_s": 60, "packets": 2400,
            "stable": [{"label": "#" + d[0], "atype": d[1], "company": d[2], "uuids": [], "n": d[4], "rssi_max": d[3],
                        "rssi_last": d[3] - 2, "trackers": d[5] if len(d) > 5 else [], "oui": "54:13:79" if d[1] == "public" else ""}
                       for d in devs],
            "rotating": {"distinct": 271, "by_company": {"unknown": 160, "Apple, Inc.": 80, "Samsung Electronics Co. Ltd.": 20}, "rssi_max": -38},
            "trackers": trackers or {}, "dropped": 0}


BASE = [("e9a551", "public", "Apple, Inc.", -70, 120), ("41a7f3", "random", "not assigned", -72, 80),
        ("07c2aa", "public", "Sony Corporation", -80, 12), ("5b0e19", "public", "", -86, 4)]
LATER = BASE + [("a1b2c3", "public", "Acme Tags", -41, 30, ["tile"]), ("d4e5f6", "public", "", -84, 3)]


async def main():
    async with async_playwright() as p:
        br = await p.chromium.launch()
        pg = await br.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("dialog", lambda d: asyncio.ensure_future(d.accept()))
        await pg.goto(B + "/login")
        await pg.fill("input[name=username]", "admin")
        await pg.fill("input[name=password]", PW)
        await pg.click("button[type=submit]")
        await pg.wait_for_selector("#tab-devices")
        await pg.click("#tab-devices")
        await pg.wait_for_selector(".act-row.dev-set", timeout=30000)
        await pg.click("#dev-wifi-seg button[data-v=bt]")
        await pg.wait_for_timeout(600)
        await pg.screenshot(path=f"{OUT}/v4110_bt_missing.png")            # no helper yet
        open(SCAN, "w").write(json.dumps(snap(time.time() - 3, BASE)))
        await pg.evaluate("loadBt(true)")
        await pg.wait_for_timeout(600)
        await pg.screenshot(path=f"{OUT}/v4110_bt_unrated.png")            # devices, no baseline
        await pg.click("text=Record Bluetooth baseline")
        await pg.wait_for_timeout(800)
        open(SCAN, "w").write(json.dumps(snap(time.time() - 1, LATER, {"tile": {"distinct": 1, "rssi_max": -41}, "find_my": {"distinct": 14, "rssi_max": -70}})))
        await pg.evaluate("loadBt(true)")
        await pg.wait_for_timeout(800)
        await pg.screenshot(path=f"{OUT}/v4110_bt_look.png")               # worth a look first
        await pg.screenshot(path=f"{OUT}/v4110_bt_full.png", full_page=True)
        await pg.click("#dev-wifi-seg button[data-v=wifi]")
        await pg.wait_for_timeout(500)
        await pg.click("#dev-group button[data-v=all]")
        await pg.wait_for_timeout(400)
        await pg.screenshot(path=f"{OUT}/v4110_devices_back.png")          # the other views unchanged
        print("page errors:", errors or "none")
        await br.close()

asyncio.run(main())
