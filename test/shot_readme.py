"""The README's screenshots, from the simulator: phone-width Chat, Spectrum, Devices and Activity, and a desktop-width
Spectrum. Needs serve.py running with the example devices, every dongle hearing the FM band and the clock in UTC (the
pages are opened in UTC too, so the pictures show no local time). From the test folder:
  export TZ=UTC FAKE_WD_PERIOD=1 ARIS_SEED_DEVICES=1
  export SERVE_FAKE_ANT="00017350:fm=0,433=0,915=-25;00026410:fm=-3,433=-6,915=-10;00035720:fm=-6,433=3,915=0"
  python3 serve.py 5097 &
  python3 shot_readme.py [port] [outdir]   (default outdir: docs/screenshots), then stop serve.py
The phone page is opened as a touch screen and the desktop page with a mouse: a row of chips is one line that scrolls
under a finger and wraps onto more lines for a mouse (app.css). Both are measured after the screenshots, and the
script ends with status 1 when a chip is cut off on the desktop page or the phone's row is no longer one line.
The pages' own clock is set to PAGE_CLOCK (it runs on from there: timers and polls work as usual), so what the page
works out from its clock shows no real moment. The times the server writes itself (chat messages, Activity, the
Devices list, a sweep's time) follow the computer's clock, so just before each picture the dates and clock times in
the page's text are rewritten to PAGE_CLOCK's (SAME_TIME): the pictures never show when they were made."""
import asyncio
import datetime
import os
import re
import sys

from playwright.async_api import async_playwright

PORT = sys.argv[1] if len(sys.argv) > 1 else "5097"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "docs", "screenshots")
B = f"http://127.0.0.1:{PORT}"
PW = os.environ.get("ARIS_TEST_PASSWORD", "test-password-123")
PAGE_CLOCK = datetime.datetime(2026, 3, 14, 12, 0, 0, tzinfo=datetime.timezone.utc)
# every date and clock time in the page's text -> PAGE_CLOCK's (the server's own stamps follow the real clock)
SAME_TIME = r"""() => {
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n; n = w.nextNode()){
    const t = n.nodeValue
      .replace(/\b20\d\d-\d\d-\d\d(?:[ T]\d\d:\d\d(?::\d\d)?)?/g, m => '2026-03-14' + (m.length > 16 ? ' 12:00:00' : m.length > 10 ? ' 12:00' : ''))
      .replace(/\b\d{1,2} (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b/g, '14 Mar')
      .replace(/\b\d\d:\d\d(?::\d\d)?\b/g, m => m.length > 5 ? '12:00:00' : '12:00');
    if (t !== n.nodeValue) n.nodeValue = t;
  }
}"""


async def same_time_api(pg):
    """The server stamps each sweep with its own clock ("time": "HH:MM:SS"); the waterfall draws it on the canvas,
    where SAME_TIME cannot reach: the answers reach the page with PAGE_CLOCK's time instead."""
    async def fix(route):
        r = await route.fetch()
        body = re.sub(r'"time": ?"\d\d:\d\d:\d\d"', '"time": "12:00:00"', await r.text())
        await route.fulfill(response=r, body=body)
    await pg.route(re.compile(r"/api/spectrum(\?|$)"), fix)


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

        ph = await br.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2, has_touch=True,
                                timezone_id="UTC")
        ph.on("pageerror", lambda e: errors.append(str(e)))
        await ph.clock.install(time=PAGE_CLOCK)
        await same_time_api(ph)
        await login(ph)
        await fm_sweep(ph)                                   # every dongle free: the three traces together
        await ph.evaluate(SAME_TIME)
        await ph.screenshot(path=f"{OUT}/phone-spectrum.png", full_page=False)
        phone = await chip_row(ph)
        await chat(ph, "investigate 433.92 for 30 seconds", "433.92", timeout=300000)
        await chat(ph, "status", "Plug-ins")
        await ph.evaluate(SAME_TIME)
        await ph.screenshot(path=f"{OUT}/phone-chat.png", full_page=False)
        await chat(ph, "record the 433 band for 1 minute", "433 MHz band")
        await ph.click("#tab-devices")
        await ph.wait_for_selector(".act-row.dev-set", timeout=60000)
        await ph.wait_for_timeout(500)
        await ph.evaluate(SAME_TIME)
        await ph.screenshot(path=f"{OUT}/phone-devices.png", full_page=False)
        await ph.click("#tab-activity")
        await ph.wait_for_selector(".lvl.RECORDED", timeout=180000)
        await ph.wait_for_timeout(800)
        await ph.evaluate(SAME_TIME)
        await ph.screenshot(path=f"{OUT}/phone-activity.png", full_page=False)

        # 800 high: the presets take a second line at this width, and the plot keeps its place in the picture
        dt = await br.new_page(viewport={"width": 1280, "height": 800}, device_scale_factor=1, timezone_id="UTC")
        dt.on("pageerror", lambda e: errors.append(str(e)))
        await dt.clock.install(time=PAGE_CLOCK)
        await same_time_api(dt)
        await login(dt)
        await dt.wait_for_timeout(5000)                      # the band capture's dongle is free again
        await fm_sweep(dt)
        await dt.evaluate(SAME_TIME)
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
