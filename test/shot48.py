"""Phone-width screenshots of the v4.8.8 Activity tab: the "On disk" line with the Clear logs button, and the tab
after Clear logs while a capture keeps running. Needs serve.py running.  python3 shot48.py [port] [outdir]"""
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
        pg.on("dialog", lambda d: asyncio.ensure_future(d.accept()))      # the one confirm of Clear logs
        await pg.goto(B + "/login")
        await pg.fill("input[name=username]", "admin")
        await pg.fill("input[name=password]", PW)
        await pg.click("button[type=submit]")
        await pg.wait_for_selector("#tab-activity")
        # a capture running, so the tab shows that Clear logs leaves it alone
        await pg.evaluate("post('/api/capture/start', {freq_mhz: 433.92, mode: 'continuous'})")
        await pg.wait_for_timeout(2500)
        await pg.click("#tab-activity")
        await pg.wait_for_selector("#act-clear-logs", timeout=30000)
        await pg.wait_for_selector(".act-row:has-text('RUN')", timeout=30000)
        await pg.wait_for_timeout(300)
        await pg.screenshot(path=f"{OUT}/v488_activity_disk.png", full_page=False)
        await pg.click("#act-clear-logs")
        await pg.wait_for_selector(".infobox:has-text('Logs cleared')", timeout=30000)
        await pg.wait_for_selector(".act-row:has-text('RUN')", timeout=30000)
        await pg.wait_for_timeout(300)
        await pg.screenshot(path=f"{OUT}/v488_activity_cleared.png", full_page=False)
        await pg.evaluate("apiGet('/api/activity').then(a => Promise.all((a.watchdog.watchdogs_running || [])"
                          ".map(x => post('/api/capture/stop', {serial: x.serial}))))")
        print("page errors:", errors or "none")
        await br.close()

asyncio.run(main())
