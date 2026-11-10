"""The README's screenshots, from the simulator: phone-width Chat, Spectrum, Devices and Activity, and a desktop-width
Spectrum. Needs serve.py running with ARIS_SEED_DEVICES=1 (run_all.sh --shots starts it).
python3 shot_readme.py [port] [outdir]   (default outdir: docs/screenshots)"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright

PORT = sys.argv[1] if len(sys.argv) > 1 else "5097"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "docs", "screenshots")
B = f"http://127.0.0.1:{PORT}"
PW = os.environ.get("ARIS_TEST_PASSWORD", "test-password-123")


async def login(pg):
    await pg.goto(B + "/login")
    await pg.fill("input[name=username]", "admin")
    await pg.fill("input[name=password]", PW)
    await pg.click("button[type=submit]")
    await pg.wait_for_selector("#tab-spectrum")


async def idle(pg, timeout=300000):
    """Until the chat console is not busy (the status bar is hidden)."""
    await pg.wait_for_function("getComputedStyle(document.getElementById('status-bar')).display === 'none'",
                               timeout=timeout)


async def chat(pg, text, wait_for, timeout=240000):
    await pg.click("#tab-chat")
    await idle(pg, timeout)
    n = await pg.locator(".bubble.ai").count()
    await pg.fill("#chat-input", text)
    await pg.press("#chat-input", "Enter")
    await pg.wait_for_function(f"document.querySelectorAll('.bubble.ai').length > {n} && "
                               f"[...document.querySelectorAll('.bubble.ai')].slice({n}).some(b => "
                               f"b.textContent.includes({wait_for!r}))", timeout=timeout)
    await idle(pg, timeout)
    await pg.wait_for_timeout(600)


async def fm_sweep(pg):
    await pg.click("#tab-spectrum")
    await pg.wait_for_function("document.querySelectorAll('#sp-presets .chip').length > 3")
    await pg.click("#sp-presets .chip[data-id='fm']")
    await pg.click("text=Sweep once")
    await pg.wait_for_function("SP.data !== null && !SP.busy", timeout=90000)
    await pg.wait_for_timeout(800)


async def main():
    os.makedirs(OUT, exist_ok=True)
    async with async_playwright() as p:
        br = await p.chromium.launch()
        errors = []

        ph = await br.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
        ph.on("pageerror", lambda e: errors.append(str(e)))
        await login(ph)
        await fm_sweep(ph)                                   # every dongle free: the three traces together
        await ph.screenshot(path=f"{OUT}/phone-spectrum.png", full_page=False)
        await chat(ph, "investigate 433.92 for 30 seconds", "433.92", timeout=300000)
        await chat(ph, "status", "Plug-ins")
        await ph.screenshot(path=f"{OUT}/phone-chat.png", full_page=False)
        await chat(ph, "record the 433 band for 1 minute", "433 MHz band")
        await ph.click("#tab-devices")
        await ph.wait_for_selector(".act-row.dev-set", timeout=60000)
        await ph.wait_for_timeout(500)
        await ph.screenshot(path=f"{OUT}/phone-devices.png", full_page=False)
        await ph.click("#tab-activity")
        await ph.wait_for_selector(".lvl.RECORDED", timeout=180000)
        await ph.wait_for_timeout(800)
        await ph.screenshot(path=f"{OUT}/phone-activity.png", full_page=False)

        dt = await br.new_page(viewport={"width": 1280, "height": 760}, device_scale_factor=1)
        dt.on("pageerror", lambda e: errors.append(str(e)))
        await login(dt)
        await dt.wait_for_timeout(5000)                      # the band capture's dongle is free again
        await fm_sweep(dt)
        await dt.screenshot(path=f"{OUT}/desktop-spectrum.png", full_page=False)
        print("page errors:", errors or "none")
        await br.close()

asyncio.run(main())
