"""Phone-width screenshots of the v4.8.8 Devices tab: the grouped list (a car's tyre sensors as one row) and the
filter box at work. Needs serve.py running with ARIS_SEED_DEVICES=1.  python3 shot49.py [port] [outdir]"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright

PORT = sys.argv[1] if len(sys.argv) > 1 else "5097"
OUT = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("ARIS_TEST_DIR", ".")
B = f"http://127.0.0.1:{PORT}"
PW = os.environ.get("ARIS_TEST_PASSWORD", "test-password-123")


async def main():
    async with async_playwright() as p:
        br = await p.chromium.launch()
        pg = await br.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        await pg.goto(B + "/login")
        await pg.fill("input[name=username]", "admin")
        await pg.fill("input[name=password]", PW)
        await pg.click("button[type=submit]")
        await pg.wait_for_selector("#tab-devices")
        await pg.click("#tab-devices")
        await pg.wait_for_selector(".act-row.dev-set", timeout=30000)
        await pg.wait_for_timeout(300)
        await pg.screenshot(path=f"{OUT}/v488_devices.png", full_page=False)
        await pg.fill("#dev-filter", "citroen")
        await pg.wait_for_timeout(300)
        await pg.screenshot(path=f"{OUT}/v488_devices_filter.png", full_page=False)
        await pg.fill("#dev-filter", "")
        print("page errors:", errors or "none")
        await br.close()

asyncio.run(main())
