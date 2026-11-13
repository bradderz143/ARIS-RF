"""Phone-width screenshots of the v4.7 parts: the Spectrum tab's Decode card (identified and not identified), a
capture's result in the Activity tab and in the chat. Needs serve.py running.  python3 shot47.py [port] [outdir]"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright

PORT = sys.argv[1] if len(sys.argv) > 1 else "5097"
OUT = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("ARIS_TEST_DIR", ".")
B = f"http://127.0.0.1:{PORT}"
PW = os.environ.get("ARIS_TEST_PASSWORD", "test-password-123")


async def decode(pg, mhz, secs_button):
    await pg.evaluate(f"specSelect({mhz})")
    await pg.click(f"#sp-dec-t button[data-s='{secs_button}']")
    await pg.click("#sp-dec-go")
    await pg.wait_for_selector("#sp-result .dec-card .tag", timeout=120000)
    await pg.wait_for_timeout(300)
    await pg.evaluate("document.getElementById('sp-sel').scrollIntoView({block: 'start'})")


async def main():
    async with async_playwright() as p:
        br = await p.chromium.launch()
        pg = await br.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2, has_touch=True)
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        await pg.goto(B + "/login")
        await pg.fill("input[name=username]", "admin")
        await pg.fill("input[name=password]", PW)
        await pg.click("button[type=submit]")
        await pg.wait_for_selector("#tab-spectrum")
        await pg.click("#tab-spectrum")
        await pg.wait_for_function("document.querySelectorAll('#sp-presets .chip').length > 3")
        await pg.click("#sp-presets .chip[data-id='fm']")
        await pg.click("text=Sweep once")
        await pg.wait_for_function("SP.data !== null && !SP.busy", timeout=60000)
        # name every FM station from its RDS data: named ones turn green, only unnamed off-plan ones stay orange
        await pg.click(".name-row button")
        # v4.7.4: slices of the band, names filling in as they are read
        await pg.wait_for_selector("#sp-names .infobox:has-text('Listening to')", timeout=60000)
        await pg.wait_for_function("/\\(\\d+ named\\)/.test(document.getElementById('sp-peaks').textContent)",
                                   timeout=90000)
        await pg.click(".pk:has-text('FM band')")              # open the FM group: the named rows are green
        await pg.wait_for_timeout(300)
        await pg.evaluate("document.getElementById('sp-peaks').scrollIntoView({block: 'start'})")
        await pg.screenshot(path=f"{OUT}/v474_naming_progress.png", full_page=False)
        await pg.click(".pk:has-text('FM band')")              # (closed again for the shots below)
        await pg.wait_for_selector("#sp-names .infobox:has-text('Named')", timeout=180000)
        await pg.evaluate("document.getElementById('sp-peaks').scrollIntoView({block: 'start'})")
        await pg.click(".pk:has-text('FM band')")
        await pg.wait_for_timeout(300)
        await pg.screenshot(path=f"{OUT}/v473_fm_checked.png", full_page=False)
        # v4.9.3: the station the check did not hear says so on its row, with the date, and is not offered
        # to Name all again at once
        await pg.evaluate("""() => { const r = [...document.querySelectorAll('#sp-peaks .pk')].find(x => /not heard in the last check/.test(x.textContent)); if (r) r.scrollIntoView({block: 'center'}); }""")
        await pg.screenshot(path=f"{OUT}/v493_fm_not_heard.png", full_page=False)
        await pg.evaluate("document.querySelector('.pk.flag').scrollIntoView({block: 'center'})")
        await pg.screenshot(path=f"{OUT}/v473_fm_list.png", full_page=False)
        await pg.evaluate("document.getElementById('view-spectrum').scrollTop = 0")
        await pg.screenshot(path=f"{OUT}/v473_fm_plot.png", full_page=False)
        await pg.evaluate("specSelect(96.9)")
        await pg.wait_for_timeout(200)
        await pg.screenshot(path=f"{OUT}/v47_spectrum_select.png", full_page=False)
        await decode(pg, 96.9, 10)
        await pg.screenshot(path=f"{OUT}/v47_decode_identified.png", full_page=False)
        await decode(pg, 914.842, 10)
        await pg.screenshot(path=f"{OUT}/v47_decode_not_identified.png", full_page=False)
        # a capture from the card's button, then its result in the Activity tab and the chat
        await pg.click("#sp-result button:text-is('Capture until decoded')")
        await pg.wait_for_function("(() => { const b = document.querySelector('#sp-result .infobox, #sp-result "
                                   ".warnbox'); return b && !b.textContent.startsWith('Starting'); })()",
                                   timeout=60000)
        await pg.screenshot(path=f"{OUT}/v47_capture_started.png", full_page=False)
        await pg.click("#tab-activity")
        await pg.wait_for_selector(".act-row:has-text('CAPTURED')", timeout=90000)
        await pg.screenshot(path=f"{OUT}/v47_activity_captures.png", full_page=False)
        await pg.click("#tab-chat")
        await pg.wait_for_selector(".bubble.ai:has-text('Capture on 914.842')", timeout=30000)
        await pg.screenshot(path=f"{OUT}/v47_chat_notice.png", full_page=False)
        # Reset Scanner during a capture: everything stops, the chat says what and that the dongles are free
        await pg.evaluate("post('/api/capture/start', {freq_mhz: 433.92, mode: 'continuous'})")
        await pg.wait_for_timeout(2500)
        await pg.click("text=Reset Scanner")
        await pg.wait_for_selector(".bubble.ai:has-text('Scanner reset')", timeout=60000)
        await pg.wait_for_timeout(500)
        await pg.screenshot(path=f"{OUT}/v474_reset.png", full_page=False)
        print("page errors:", errors or "none")
        await br.close()

asyncio.run(main())
