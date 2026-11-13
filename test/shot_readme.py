"""The README's screenshots, from the simulator: phone-width Chat, Spectrum, Devices and Activity, and a desktop-width
Spectrum. Needs serve.py running with ARIS_SEED_DEVICES=1 (run_all.sh --shots starts it).
python3 shot_readme.py [port] [outdir]   (default outdir: docs/screenshots)
The phone page is opened as a touch screen and the desktop page with a mouse: a row of chips is one line that scrolls
under a finger and wraps onto more lines for a mouse (app.css). Both are measured after the screenshots, and the
script ends with status 1 when a chip is cut off on the desktop page or the phone's row is no longer one line."""
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


async def chip_row(pg, sel="#sp-presets"):
    """Where a row of chips sits: how many lines, which chips reach outside the row's box, and the plot's place."""
    return await pg.evaluate("""sel => {
        const box = document.querySelector(sel), b = box.getBoundingClientRect(), kids = [...box.children];
        const r = kids.map(c => c.getBoundingClientRect());
        const plot = document.getElementById('spec-canvas').getBoundingClientRect();
        return {chips: kids.length, lines: new Set(r.map(x => Math.round(x.top))).size,
                cut: kids.filter((c, i) => r[i].left < b.left - 0.5 || r[i].right > b.right + 0.5).map(c => c.textContent),
                scrolls: box.scrollWidth > box.clientWidth + 1,
                plot_on_screen: plot.top >= 0 && plot.bottom <= window.innerHeight}; }""", sel)


async def main():
    os.makedirs(OUT, exist_ok=True)
    async with async_playwright() as p:
        br = await p.chromium.launch()
        errors = []

        ph = await br.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2, has_touch=True)
        ph.on("pageerror", lambda e: errors.append(str(e)))
        await login(ph)
        await fm_sweep(ph)                                   # every dongle free: the three traces together
        await ph.screenshot(path=f"{OUT}/phone-spectrum.png", full_page=False)
        phone = await chip_row(ph)
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

        # 800 high: the presets take a second line at this width, and the plot keeps its place in the picture
        dt = await br.new_page(viewport={"width": 1280, "height": 800}, device_scale_factor=1)
        dt.on("pageerror", lambda e: errors.append(str(e)))
        await login(dt)
        await dt.wait_for_timeout(5000)                      # the band capture's dongle is free again
        await fm_sweep(dt)
        await dt.screenshot(path=f"{OUT}/desktop-spectrum.png", full_page=False)
        desk = await chip_row(dt)
        print("page errors:", errors or "none")
        await br.close()
    print(f"preset chips: phone (touch) {phone['chips']} on {phone['lines']} line(s), "
          f"{'scrolls sideways' if phone['scrolls'] else 'does not scroll'}; desktop (mouse) {desk['chips']} on "
          f"{desk['lines']} line(s), cut off: {', '.join(desk['cut']) or 'none'}")
    wrong = []
    if phone["lines"] != 1 or not phone["scrolls"]:
        wrong.append("the phone's presets are no longer one line that scrolls sideways")
    if desk["cut"]:
        wrong.append("presets are cut off on the desktop page: " + ", ".join(desk["cut"]))
    if not desk["plot_on_screen"]:
        wrong.append("the plot does not fit on the desktop page")
    if wrong:
        sys.exit("PROBLEM: " + "; ".join(wrong))

asyncio.run(main())
