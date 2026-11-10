"""Phone-width screenshots of the Spectrum and Map tabs (needs serve.py running). python3 shot46.py [port] [outdir]"""
import asyncio
import os
import sys

from playwright.async_api import async_playwright

PORT = sys.argv[1] if len(sys.argv) > 1 else "5097"
OUT = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("ARIS_TEST_DIR", ".")
B = f"http://127.0.0.1:{PORT}"
PW = os.environ.get("ARIS_TEST_PASSWORD", "test-password-123")
POS = {"00001001": (1, 1), "00003003": (9, 1), "00004004": (5, 7)}


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
        await pg.wait_for_selector("#tab-spectrum")
        await pg.click("#tab-spectrum")
        await pg.wait_for_function("document.querySelectorAll('#sp-presets .chip').length > 3")
        await pg.fill("#sp-start", "30"); await pg.fill("#sp-stop", "200"); await pg.dispatch_event("#sp-stop", "change")
        await pg.click("#sp-hold")
        await pg.click("text=Sweep once")
        await pg.wait_for_function("SP.data !== null", timeout=60000)
        await pg.click("text=Sweep once"); await pg.wait_for_timeout(300)
        await pg.wait_for_function("!SP.busy", timeout=60000)
        await pg.screenshot(path=f"{OUT}/v46_spectrum_wide.png", full_page=True)
        await pg.click("#sp-scale button[data-v=rel]")
        await pg.screenshot(path=f"{OUT}/v46_spectrum_wide_rel.png", full_page=True)
        await pg.click("#sp-presets .chip[data-id=fm]")
        await pg.click("text=Sweep once")
        await pg.wait_for_function("SP.data !== null && !SP.busy", timeout=60000)
        await pg.click(".pk >> nth=0"); await pg.click(".pk >> nth=1")
        await pg.screenshot(path=f"{OUT}/v46_spectrum_fm.png", full_page=True)
        # Map: place the antennas through the API, then measure from the page
        await pg.click("#tab-map")
        await pg.wait_for_function("MAP.st !== null")
        await pg.evaluate("""async (pos) => {
            await post('/api/map/plan', {name: 'Apartment', width_m: 12});
            const st = await apiGet('/api/map/state');
            await post('/api/map/plan/' + st.active, {height_m: 9});
            for (const n of st.nodes) await post('/api/map/node/' + n.id, {x: pos[n.serial][0], y: pos[n.serial][1]});
            await mapLoad(); mapMode('place');
        }""", POS)
        await pg.wait_for_timeout(300)
        await pg.screenshot(path=f"{OUT}/v46_map_place.png", full_page=True)
        await pg.click("#map-modes .chip[data-m=locate]")
        await pg.fill("#mp-freq", "433.92")
        await pg.select_option("#mp-dwell", "3")
        await pg.click("#map-panel button.btn:text-is('Measure')")
        await pg.wait_for_function("MAP.result !== null && !MAP.busy", timeout=90000)
        await pg.wait_for_timeout(300)
        await pg.screenshot(path=f"{OUT}/v46_map_locate.png", full_page=True)
        print("page errors:", errors or "none")
        await br.close()

asyncio.run(main())
