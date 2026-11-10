"""Phone-width screenshots of the v4.10.0 Wi-Fi view (Devices tab > Wi-Fi). Needs serve.py running with
ARIS_SEED_DEVICES=1 and ARIS_WIFI_SCAN_FILE=<file> (this script writes the file).  python3 shot50.py [port] [outdir]"""
import asyncio
import os
import sys
import time

from playwright.async_api import async_playwright

PORT = sys.argv[1] if len(sys.argv) > 1 else "5097"
OUT = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("ARIS_TEST_DIR", ".")
B = f"http://127.0.0.1:{PORT}"
PW = os.environ.get("ARIS_TEST_PASSWORD", "test-password-123")
SCAN = os.environ["ARIS_WIFI_SCAN_FILE"]


def bss(n, freq, dbm, ssid, sec="WPA2"):
    cap = "ESS (0x0401)" if sec == "open" else "ESS Privacy (0x0411)"
    rsn = "" if sec == "open" else "\tRSN:\t * Version: 1\n\t\t * Authentication suites: PSK\n"
    return (f"BSS aa:bb:cc:00:00:{n:02x}(on wlx)\n\tfreq: {freq}\n\tcapability: {cap}\n\tsignal: {dbm:.2f} dBm\n"
            f"\tSSID: {ssid}\n{rsn}")


def rich(addr, freq, dbm, ssid, tsf, extra=""):
    sec = "\tRSN:\t * Version: 1\n\t\t * Authentication suites: PSK\n"
    return (f"BSS {addr}(on wlx)\n\tTSF: {tsf * 1000000} usec (x)\n\tfreq: {freq}\n\tcapability: ESS Privacy (0x0411)\n"
            f"\tsignal: {dbm:.2f} dBm\n\tSSID: {ssid}\n\tCountry: US\tEnvironment: Indoor/Outdoor\n{sec}{extra}")


WPS = "\tWPS:\t * Version: 1.0\n\t\t * Manufacturer: TP-Link\n\t\t * Model: Deco M5\n\t\t * Device name: Whole-home Wi-Fi\n"
TWINS = (rich("e4:c3:2a:11:22:30", 2432, -62, "Casa Vecino", 1546000, WPS) + rich("e4:c3:2a:11:22:31", 5745, -58, "Casa Vecino", 1546000)
         + rich("e4:c3:2a:11:22:32", 5745, -64, "", 1546000).replace("SSID: ", "SSID: \\x00\\x00")
         + rich("4e:c3:2a:aa:bb:01", 2412, -66, "Telefono de Ana", 900))
BASE = (bss(1, 5745, -48, "Totalplay-5G-0398") + bss(2, 2437, -49, "Totalplay-2.4G-0398") + bss(3, 2462, -70, "Izzi Deco")
        + bss(4, 2412, -80, "HOMENET-4402") + bss(5, 5180, -75, "CafeGuest") + bss(6, 2437, -85, "IZZI-6956"))
LATER = BASE + TWINS + (bss(9, 2462, -88, "TOTALPLAY_FAS550") + bss(10, 2412, -42, "", "open").replace("SSID: ", "SSID: \\x00\\x00")
                + bss(11, 2437, -55, "Totalplay-2.4G-0398", "open"))


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
        await pg.click("#dev-wifi-seg button")
        await pg.wait_for_timeout(600)
        await pg.screenshot(path=f"{OUT}/v4100_wifi_missing.png")           # no helper yet
        open(SCAN, "w").write(BASE)
        await pg.evaluate("loadWifi(true)")
        await pg.wait_for_timeout(600)
        await pg.screenshot(path=f"{OUT}/v4100_wifi_unrated.png")           # networks, no baseline
        await pg.click("text=Record Wi-Fi baseline")
        await pg.wait_for_timeout(800)
        open(SCAN, "w").write(LATER)
        t = time.time() + 5
        os.utime(SCAN, (t, t))
        await pg.evaluate("loadWifi(true)")
        await pg.wait_for_timeout(800)
        await pg.screenshot(path=f"{OUT}/v4100_wifi_look.png")              # worth a look first
        await pg.screenshot(path=f"{OUT}/v4100_wifi_full.png", full_page=True)
        await pg.click("#dev-group button[data-v=all]")
        await pg.wait_for_timeout(400)
        await pg.screenshot(path=f"{OUT}/v4100_devices_back.png")           # the other views unchanged
        print("page errors:", errors or "none")
        await br.close()

asyncio.run(main())
