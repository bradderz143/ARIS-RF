"""Phone-width screenshots and checks of the Spectrum tab's touch zoom, the zoomed view swept by itself, the gain
edges on the plot, a Decode's own Stop and the band labels' region. Real touches: two touch points through the
browser's own input (CDP Input.dispatchTouchEvent), at 390 x 844 with touch, then one check at 1280 px. The pinch,
the sweep after it and the Decode Stop run against the simulated radios of serve.py; the gain pieces and the region
come from answers this script gives in the server's place (page.route), so they are tested whatever the server
offers yet. Needs serve.py running.  python3 shot53.py [port] [outdir]
Ends with status 1 when a pinch does not zoom about the fingers' midpoint, takes more than one view change or one
Back entry, scrolls or zooms the page, or one finger stops marking or scrolling (also with a thumb resting off the
plot); when a zoomed view is not swept by itself once it settles, is swept while zooming out, with the setting off
or during a Decode, or more than once after two quick pinches; when the gain edges or their labels are missing,
wrong, drawn without the server's pieces, too close to tell apart or over a trace, or the legend's gains are not in
frequency order; when Stop does not end the Decode as stopped (or the progress card is rebuilt under the finger), a
failed Stop does not say why, a Decode the server no longer knows does not end, or a view change hides a running
Decode; when the region line, its selects, a refusal or a viewer's text are wrong, or a chip stays on for a range
the view does not show after a region change; when something pressed is under 39 px; or when the page scrolls
sideways."""
import asyncio
import json
import os
import re
import sys
import time

from playwright.async_api import async_playwright

PORT = sys.argv[1] if len(sys.argv) > 1 else "5097"
OUT = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("ARIS_TEST_DIR", ".")
B = f"http://127.0.0.1:{PORT}"
PW = os.environ.get("ARIS_TEST_PASSWORD", "test-password-123")
MIN_TAP_PX = 39
BAD = []
ERRORS = []
SWEEPS = []                     # (time, url) of every /api/spectrum?... the page asked for

# the band labels' region as this script answers for the server (GET and POST /api/region)
CHOICES = [{"id": "americas", "name": "Americas"}, {"id": "europe", "name": "Europe"},
           {"id": "asia_pacific", "name": "Asia-Pacific"}, {"id": "japan", "name": "Japan"},
           {"id": "australia", "name": "Australia and New Zealand"}, {"id": "world", "name": "World"}]
COUNTRIES = [{"id": "US", "name": "United States", "region": "americas"}, {"id": "CA", "name": "Canada", "region": "americas"},
             {"id": "BR", "name": "Brazil", "region": "americas"}, {"id": "AU", "name": "Australia", "region": "australia"},
             {"id": "NZ", "name": "New Zealand", "region": "australia"}]
REGION = {"id": "americas", "country": None, "guessed": True, "source": "time zone", "mode": "ok", "posts": [], "meta": 0}
EXTRA_PRESET = {"id": "srd868", "name": "SRD 868", "start": 863.0, "stop": 870.0, "tuned": True}


def bad(what):
    BAD.append(what)
    print("FAIL:", what)


def region_body():
    r = REGION
    name = next(c["name"] for c in CHOICES if c["id"] == r["id"])
    cn = next((c["name"] for c in COUNTRIES if c["id"] == r["country"]), None)
    return {"id": r["id"], "name": name, "country": r["country"], "country_name": cn, "guessed": r["guessed"],
            "source": r["source"], "choices": CHOICES, "countries": COUNTRIES}


async def region_route(route):
    req = route.request
    if REGION["mode"] == "absent":                       # a server without the region API
        return await route.fulfill(status=404, content_type="application/json", body=json.dumps({"error": "not found"}))
    if req.method == "POST":
        ask = json.loads(req.post_data or "{}")
        REGION["posts"].append(ask)
        if REGION["mode"] == "forbid":
            return await route.fulfill(status=403, content_type="application/json",
                                       body=json.dumps({"error": "only the admin account can do that"}))
        if ask.get("id") not in [c["id"] for c in CHOICES]:
            return await route.fulfill(status=400, content_type="application/json",
                                       body=json.dumps({"error": "There is no region called that."}))
        REGION.update(id=ask["id"], country=ask.get("country"), guessed=False, source="config")
    await route.fulfill(status=200, content_type="application/json", body=json.dumps(region_body()))


async def meta_route(route):
    REGION["meta"] += 1
    resp = await route.fetch()
    body = await resp.json()
    if REGION["id"] == "europe":                         # presets follow the region
        body["presets"] = body["presets"] + [EXTRA_PRESET]
    if REGION["id"] == "japan":                          # a preset of the same name over another range
        body["presets"] = [dict(p, start=76.0, stop=99.0, name="FM 76-99") if p["id"] == "fm" else p for p in body["presets"]]
    await route.fulfill(response=resp, body=json.dumps(body), content_type="application/json")


# ---- stand-in sweeps with gain pieces: Auto gain cut at 880 and 960 MHz ----
GAINS = {"00017350": (37.2, 29.7, 8.7), "00026410": (33.8, 28.0, 10.0)}     # base, 880-960 MHz, FM band
EDGES = (87.5, 108.0, 880.0, 960.0)


def piece_gain(serial, f):
    a, b, c = GAINS[serial]
    return b if 880.0 <= f < 960.0 else (c if 87.5 <= f < 108.0 else a)


def standin_sweep(url, pieces=True, dongles=("00017350",), spike=None):
    q = dict(x.split("=", 1) for x in url.split("?", 1)[1].split("&"))
    s, e = float(q["start"]), float(q["stop"])
    n = 301
    freqs = [round(s + (e - s) * i / (n - 1), 4) for i in range(n)]
    traces = []
    for k, ser in enumerate(dongles):
        cuts = sorted({s, e} | {x for x in EDGES if s < x < e})
        gp = [[a, b, piece_gain(ser, (a + b) / 2)] for a, b in zip(cuts, cuts[1:])]
        db = [round((-52.0 if piece_gain(ser, f) == GAINS[ser][0] else -46.0) - 3 * k + (12.0 if abs(f - 915.0) < 0.6 else 0.0), 1)
              if not (spike and abs(f - spike[0]) < 0.3) else spike[1] for f in freqs]
        t = {"dongle": ser, "name": f"Dongle {k + 1}", "antenna": "", "freqs": freqs, "db": db, "floor_db": -50.0 - 3 * k,
             "unit": "dBFS", "gain_db": " to ".join(str(g[2]) for g in gp), "range_mhz": [s, e], "peaks": 0, "health": "ok"}
        if pieces:
            t["gain_pieces"] = gp
        traces.append(t)
    return {"mode": "sweep", "unit": "dBFS", "start_mhz": s, "stop_mhz": e, "step_khz": float(q.get("step_khz", 100)),
            "took_ms": 40, "time": "12:00:00", "traces": traces, "peaks": [], "warnings": [], "fm_plan_checked": False,
            "spikes_removed": 0, "gain_removed": True, "region": {"id": REGION["id"], "name": region_body()["name"]}}


async def geom(pg):
    return await pg.evaluate("() => { const c = document.getElementById('spec-canvas'); c.scrollIntoView({block: 'center'});"
                             " const r = c.getBoundingClientRect(); return {l: r.left, t: r.top, w: r.width, h: r.height,"
                             " L: SP.geom.L, pw: SP.geom.pw, s: SP.view.s, e: SP.view.e}; }")


async def pinch(pg, cdp, frac, d0, d1, steps=6):
    """Two fingers on the plot, their midpoint at `frac` of its width, from d0 to d1 px apart."""
    g = await geom(pg)
    mid, y = g["l"] + g["L"] + frac * g["pw"], g["t"] + g["h"] / 2

    def pts(d):
        return [{"x": mid - d / 2, "y": y, "id": 1}, {"x": mid + d / 2, "y": y, "id": 2}]
    await cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": pts(d0)})
    for i in range(1, steps + 1):
        await cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": pts(d0 + (d1 - d0) * i / steps)})
        await pg.wait_for_timeout(20)
    await cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    return g


async def sweep_once(pg):
    await pg.evaluate("specOnce()")
    await pg.wait_for_function("SP.data !== null && !SP.busy", timeout=60000)


async def touch_zoom(pg, cdp):
    """Two fingers zoom about their midpoint in one view change; one finger still marks and scrolls."""
    await pg.evaluate("document.getElementById('sp-autozoom').checked = false; specAutoZoom()")
    await pg.click("#sp-presets .chip[data-id='fm']")
    await sweep_once(pg)
    await pg.wait_for_timeout(800)                       # (a change within 0.7 s of the last joins its Back entry)
    await pg.evaluate("window.__vc = 0; window.__vcOrig = specViewChanged;"
                      " window.specViewChanged = function(){ window.__vc++; return window.__vcOrig.apply(this, arguments); }; 0")
    await geom(pg)                                       # (the plot in view first: that scroll is this script's own)
    before = await pg.evaluate("() => ({hist: SP.hist.length, top: document.getElementById('view-spectrum').scrollTop})")
    g = await pinch(pg, cdp, 0.6, 80, 240)
    await pg.wait_for_timeout(300)
    got = await pg.evaluate("() => ({s: SP.view.s, e: SP.view.e, vc: window.__vc, hist: SP.hist.length, last: SP.hist[SP.hist.length - 1],"
                            " top: document.getElementById('view-spectrum').scrollTop, scale: visualViewport.scale, pinch: SP.pinch,"
                            " field: document.getElementById('sp-start').value, status: document.getElementById('sp-status').textContent})")
    span0 = g["e"] - g["s"]
    f0 = g["s"] + 0.6 * span0
    want = span0 * 80 / 240
    if abs((got["e"] - got["s"]) - want) > 0.002 * span0 or abs(got["s"] - (f0 - 0.6 * want)) > 0.01 * span0:
        bad(f"a pinch from 80 to 240 px about 60% of the plot: view {got['s']}-{got['e']}, wanted a span of {want:.4f} about {f0:.4f}")
    if got["vc"] != 1 or got["hist"] != before["hist"] + 1 or got["last"] != [g["s"], g["e"]]:
        bad(f"a pinch is not one view change with one Back entry: {got['vc']} changes, Back list {before['hist']} -> {got['hist']} ({got['last']})")
    if abs(got["top"] - before["top"]) > 1 or got["scale"] != 1 or got["pinch"] is not None:
        bad(f"a pinch moved or zoomed the page: scroll {before['top']} -> {got['top']}, scale {got['scale']}")
    if abs(float(got["field"]) - got["s"]) > 1e-3 or not got["status"].startswith("View changed"):
        bad(f"after a pinch the fields or the status are off: {got}")
    await pg.screenshot(path=f"{OUT}/v53_pinched.png")
    # Back returns to the view before the pinch
    await pg.evaluate("specBack()")
    v = await pg.evaluate("[SP.view.s, SP.view.e]")
    if v != [g["s"], g["e"]]:
        bad(f"Back after a pinch went to {v}, not {[g['s'], g['e']]}")
    # one finger: a tap marks (300 ms after the pinch has ended), a vertical swipe scrolls the page and not the view
    await pg.wait_for_timeout(400)
    g = await geom(pg)
    x, y = g["l"] + g["L"] + 0.25 * g["pw"], g["t"] + g["h"] / 2
    await cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y, "id": 3}]})
    await cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    await pg.wait_for_timeout(400)
    sel = await pg.evaluate("SP.sel")
    if sel is None or abs(sel - (g["s"] + 0.25 * (g["e"] - g["s"]))) > 0.02 * (g["e"] - g["s"]):
        bad(f"one finger's tap did not mark the frequency under it: {sel}")
    top0 = await pg.evaluate("document.getElementById('view-spectrum').scrollTop")
    await cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y + 60, "id": 4}]})
    for i in range(1, 9):
        await cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y + 60 - 20 * i, "id": 4}]})
        await pg.wait_for_timeout(16)
    await cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    await pg.wait_for_timeout(500)
    after = await pg.evaluate("() => ({top: document.getElementById('view-spectrum').scrollTop, s: SP.view.s, e: SP.view.e})")
    if after["top"] <= top0 + 20 or [after["s"], after["e"]] != [g["s"], g["e"]]:
        bad(f"one finger's swipe up did not scroll the page ({top0} -> {after['top']}) or moved the view ({after})")
    await resting_thumb(pg, cdp)
    await pg.evaluate("window.specViewChanged = window.__vcOrig; document.getElementById('sp-autozoom').checked = true; specAutoZoom()")


async def resting_thumb(pg, cdp):
    """A thumb resting on the screen off the plot (holding the phone) and one finger sliding sideways on the plot:
    only fingers on the plot count, so that is no pinch and no zoom."""
    await pg.wait_for_timeout(400)
    g = await geom(pg)
    thumb = {"x": g["l"] + 30, "y": g["t"] + g["h"] + 30, "id": 7}          # below the plot
    x, y = g["l"] + g["L"] + 0.3 * g["pw"], g["t"] + g["h"] / 2
    v0 = await pg.evaluate("() => { window.__vc = 0; return [SP.view.s, SP.view.e]; }")
    await cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [thumb]})
    await cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [thumb, {"x": x, "y": y, "id": 8}]})
    for i in range(1, 7):
        await cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [thumb, {"x": x + 15 * i, "y": y, "id": 8}]})
        await pg.wait_for_timeout(20)
    await cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": [thumb]})
    await cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    await pg.wait_for_timeout(300)
    got = await pg.evaluate("() => ({v: [SP.view.s, SP.view.e], vc: window.__vc, pinch: SP.pinch})")
    if got["v"] != v0 or got["vc"] or got["pinch"] is not None:
        bad(f"a resting thumb and one finger sliding on the plot changed the view: {v0} -> {got}")


def sweeps_since(t):
    return [u for (w, u) in SWEEPS if w >= t]


async def wait_status(pg, rx, ms=4000):
    t0 = time.time()
    while time.time() - t0 < ms / 1000:
        s = await pg.evaluate("document.getElementById('sp-status').textContent")
        if re.search(rx, s):
            return s
        await pg.wait_for_timeout(60)
    return None


async def zoom_sweep(pg, cdp):
    """A zoomed view is swept once by itself after it settles; never when zooming out, with the setting off or while
    a Decode runs; two quick pinches give one sweep, of the last view."""
    await pg.click("#sp-presets .chip[data-id='fm']")
    await sweep_once(pg)
    step0 = await pg.evaluate("SP.data.step_khz")
    await pg.wait_for_timeout(900)
    await pinch(pg, cdp, 0.5, 60, 240)
    t_end = time.time()
    st = await wait_status(pg, r"^Zoomed in: ")
    first = sweeps_since(t_end)
    t_first = next((w for (w, u) in SWEEPS if w >= t_end), None)
    await pg.wait_for_function("!SP.busy", timeout=30000)
    await pg.wait_for_timeout(200)
    got = await pg.evaluate("() => ({s: SP.view.s, e: SP.view.e, ds: SP.data.start_mhz, de: SP.data.stop_mhz, step: SP.data.step_khz,"
                            " stale: SP.stale, status: document.getElementById('sp-status').textContent})")
    if not st or not re.match(r"^Zoomed in: sweeping [\d.]+-[\d.]+ MHz at [\d.]+ kHz steps$", st):
        bad(f"a zoomed view did not say it is sweeping by itself: {st!r}")
    if not first or t_first is None or t_first - t_end < 0.6:
        bad(f"the zoomed view was swept {None if t_first is None else round(t_first - t_end, 2)} s after the pinch (about 0.8 s wanted)")
    if not (got["step"] < step0) or got["stale"] or abs(got["ds"] - got["s"]) > 1e-6 or "swept by itself after the zoom" not in got["status"]:
        bad(f"after the zoom sweep: step {step0} -> {got['step']}, {got}")
    print("zoom sweep:", st, "|", got["status"])
    await pg.screenshot(path=f"{OUT}/v53_zoom_swept.png")
    # two pinches 0.3 s apart: one sweep, of the view the second one left
    t0 = time.time()
    await pinch(pg, cdp, 0.4, 80, 160)
    await pg.wait_for_timeout(300)
    await pinch(pg, cdp, 0.6, 80, 160)
    await pg.wait_for_timeout(2500)
    await pg.wait_for_function("!SP.busy", timeout=30000)
    v = await pg.evaluate("[SP.view.s, SP.view.e]")
    got = sweeps_since(t0)
    if len(got) != 1 or f"start={v[0]}&stop={v[1]}" not in got[0]:
        bad(f"two quick pinches: the page asked for {got}, the view is {v}")
    # zooming out: nothing by itself
    t0 = time.time()
    await pinch(pg, cdp, 0.5, 240, 60)
    await pg.wait_for_timeout(1700)
    st = await pg.evaluate("document.getElementById('sp-status').textContent")
    if sweeps_since(t0) or not st.startswith("View changed"):
        bad(f"zooming out swept by itself: {sweeps_since(t0)}, status {st!r}")
    await sweep_once(pg)
    await pg.wait_for_timeout(900)
    # a Decode is running (the page's own mark of one): nothing by itself
    t0 = time.time()
    await pg.evaluate("SP.job = 'stand-in'")
    await pinch(pg, cdp, 0.5, 60, 180)
    await pg.wait_for_timeout(1700)
    await pg.evaluate("SP.job = null")
    if sweeps_since(t0):
        bad(f"a zoomed view was swept during a Decode: {sweeps_since(t0)}")
    # the setting off: nothing by itself, and it stays off after a reload
    await pg.evaluate("specBack()")
    await sweep_once(pg)
    await pg.wait_for_timeout(900)
    await pg.tap("label.sp-check")
    t0 = time.time()
    await pinch(pg, cdp, 0.5, 60, 180)
    await pg.wait_for_timeout(1700)
    if sweeps_since(t0) or await pg.evaluate("SP.autoZoom"):
        bad(f"with the setting off a zoomed view was swept by itself: {sweeps_since(t0)}")
    h = await pg.evaluate("Math.round(document.querySelector('label.sp-check').getBoundingClientRect().height)")
    if h < MIN_TAP_PX:
        bad(f"the setting's line is {h} px high")
    await pg.reload()
    await pg.wait_for_selector("#tab-spectrum")
    await pg.click("#tab-spectrum")
    await pg.wait_for_function("document.querySelectorAll('#sp-presets .chip').length > 3")
    if await pg.evaluate("document.getElementById('sp-autozoom').checked || SP.autoZoom"):
        bad("the setting 'Sweep a zoomed view by itself' did not stay off after a reload")
    await pg.tap("label.sp-check")
    if not await pg.evaluate("SP.autoZoom"):
        bad("the setting did not come back on")


async def decode_stop(pg):
    """Stop in a running Decode's progress card ends that Decode as stopped: nothing saved, buttons back; the card
    is updated in place while it runs (a press is never lost to a rebuild)."""
    await pg.click("#sp-presets .chip[data-id='fm']")
    await sweep_once(pg)
    await pg.evaluate("specSelect(97.1); specDecodeTime(30); document.getElementById('sp-sel').scrollIntoView({block: 'start'})")
    hs = await pg.evaluate("[...document.querySelectorAll('#sp-dec-t button')].map(b => Math.round(b.getBoundingClientRect().height))")
    if min(hs) < MIN_TAP_PX:
        bad(f"the 'Decode for' buttons are {hs} px high")
    sig0 = await pg.evaluate("fetch('/api/signals').then(r => r.json()).then(j => j.tag)")
    await pg.tap("#sp-dec-go")
    await pg.wait_for_selector("#sp-result .dec-stop", timeout=10000)
    await pg.evaluate("window.__card = document.querySelector('#sp-result .dec-prog')")
    await pg.wait_for_function("SP.job && document.querySelector('#sp-result .dec-pmsg').textContent !== 'Starting...'", timeout=20000)
    await pg.wait_for_timeout(2500)                      # a few polls: the same card, updated
    same = await pg.evaluate("window.__card === document.querySelector('#sp-result .dec-prog') && window.__card.isConnected")
    m = await pg.evaluate("() => { const b = document.querySelector('#sp-result .dec-stop'), r = b.getBoundingClientRect(),"
                          " h = document.querySelector('#sp-result .dec-h').getBoundingClientRect();"
                          " return {h: Math.round(r.height), on: !b.disabled, first_line: r.top < h.bottom,"
                          " panel_off: document.querySelectorAll('#sp-sel .btns button:disabled').length}; }")
    await pg.screenshot(path=f"{OUT}/v53_decode_running.png")
    if not same:
        bad("the progress card was rebuilt while the Decode ran")
    if m["h"] < MIN_TAP_PX or not m["on"] or not m["first_line"] or m["panel_off"] != 4:
        bad(f"the Stop button: {m}")
    # the view moves off the frequency while it runs (an arrow, a pinch): the panel with the progress and Stop stays
    await pg.evaluate("specSetView(100, 104); document.getElementById('sp-sel').scrollIntoView({block: 'start'})")
    v = await pg.evaluate("() => { const b = document.querySelector('#sp-result .dec-stop'); return {sel: SP.sel,"
                          " shown: getComputedStyle(document.getElementById('sp-sel')).display !== 'none',"
                          " h: b ? Math.round(b.getBoundingClientRect().height) : 0}; }")
    if v["sel"] != 97.1 or not v["shown"] or v["h"] < MIN_TAP_PX:
        bad(f"a view change during a Decode hid its progress and Stop: {v}")
    t0 = time.time()
    await pg.tap("#sp-result .dec-stop")
    await pg.wait_for_timeout(150)
    mid = await pg.evaluate("() => { const b = document.querySelector('#sp-result .dec-stop'); return b ? [b.disabled, b.textContent] : null; }")
    await pg.wait_for_function("SP.job === null", timeout=30000)
    took = time.time() - t0
    await pg.wait_for_timeout(200)
    end = await pg.evaluate("() => ({msg: [...document.querySelectorAll('#sp-result .infobox, #sp-result .warnbox')].map(e => e.textContent),"
                            " card: !!document.querySelector('#sp-result .dec-card'), off: document.querySelectorAll('#sp-sel button:disabled').length,"
                            " held: document.getElementById('sp-result').style.minHeight})")
    await pg.screenshot(path=f"{OUT}/v53_decode_stopped.png")
    sig1 = await pg.evaluate("fetch('/api/signals').then(r => r.json()).then(j => j.tag)")
    if mid not in ([True, "Stopping..."], None):
        bad(f"right after Stop the button reads {mid}")
    if end["msg"] != ["Decode of 97.1000 MHz stopped. Nothing was saved, and the dongles are free."] or end["card"] \
            or end["off"] or end["held"]:
        bad(f"after Stop: {end}")
    if took > 20 or sig1 != sig0:
        bad(f"Stop took {took:.1f} s, signals {sig0} -> {sig1}")
    print(f"decode stopped in {took:.1f} s")
    # after the Decode the old rule is back: a view off the marker closes the panel
    await pg.evaluate("specSetView(104, 108)")
    if await pg.evaluate("SP.sel !== null || getComputedStyle(document.getElementById('sp-sel')).display !== 'none'"):
        bad("with no Decode running, a view off the marker kept its panel")


def is_poll(url):
    return re.search(r"/api/decode/[0-9a-f]+$", url) is not None


async def decode_stop_errors(pg):
    """A Stop the server did not take says why, and that stays readable through the polls; a Decode the server no
    longer knows (404: the GUI restarted) ends its card at once instead of waiting for ever."""
    await pg.evaluate("specSetView(88, 108); specSelect(97.1); specDecodeTime(10); document.getElementById('sp-sel').scrollIntoView({block: 'start'})")
    await pg.tap("#sp-dec-go")
    await pg.wait_for_function("SP.job && document.querySelector('#sp-result .dec-stop')", timeout=10000)
    job = await pg.evaluate("SP.job")
    hits = {"stop": 0}

    async def fail(route):
        hits["stop"] += 1
        await route.fulfill(status=500, content_type="application/json", body=json.dumps({"error": "stand-in failure"}))
    await pg.route("**/api/decode/*/stop", fail)
    await pg.tap("#sp-result .dec-stop")
    await pg.wait_for_timeout(2600)                      # two or three polls
    st = await pg.evaluate("() => { const b = document.querySelector('#sp-result .dec-stop');"
                           " return {msg: document.querySelector('#sp-result .dec-pmsg').textContent, on: !!b && !b.disabled,"
                           " text: b && b.textContent, job: SP.job}; }")
    await pg.unroute("**/api/decode/*/stop")
    if hits["stop"] != 1 or st["msg"] != "Could not stop it: stand-in failure" or not st["on"] or st["text"] != "Stop" or st["job"] != job:
        bad(f"a Stop that failed: {hits['stop']} request(s), {st}")

    async def gone(route):
        await route.fulfill(status=404, content_type="application/json",
                            body=json.dumps({"error": "no such job (the GUI may have restarted)"}))
    await pg.route("**/api/decode/*/stop", gone)
    await pg.route(is_poll, gone)
    t0 = time.time()
    await pg.tap("#sp-result .dec-stop")
    try:
        await pg.wait_for_function("SP.job === null", timeout=5000)
    except Exception:
        pass
    took = time.time() - t0
    end = await pg.evaluate("() => ({job: SP.job, msg: [...document.querySelectorAll('#sp-result .warnbox')].map(e => e.textContent),"
                            " off: document.querySelectorAll('#sp-sel button:disabled').length, held: document.getElementById('sp-result').style.minHeight})")
    await pg.unroute("**/api/decode/*/stop")
    await pg.unroute(is_poll)
    await pg.screenshot(path=f"{OUT}/v53_decode_gone.png")
    if end["job"] is not None or took > 3 or end["off"] or end["held"] or len(end["msg"]) != 1 \
            or not end["msg"][0].startswith("The Decode of 97.1000 MHz is no longer running"):
        bad(f"a Decode the server no longer knows (after {took:.1f} s): {end}")
    # the real job on the simulated server is still running: stop it there and wait until its dongles are free
    await pg.request.post(f"{B}/api/decode/{job}/stop", headers={"X-ARIS": "1", "Content-Type": "application/json"}, data="{}")
    for _ in range(150):
        r = await pg.request.get(f"{B}/api/decode/{job}")
        if (await r.json()).get("state") != "running":
            break
        await pg.wait_for_timeout(200)
    else:
        bad("the real Decode behind the stand-in answers did not stop")


async def regions(pg):
    """The region line in the tab's notes: the admin's selects (a change posts, then the presets reload and a quick
    view is swept for the new labels), a refusal turns them into text, a viewer reads text, and a server without
    the region API shows no line."""
    async def follow(route):                             # the sweeps report the stand-in's region, as the server would
        resp = await route.fetch()
        body = await resp.json()
        body["region"] = {"id": REGION["id"], "name": region_body()["name"]}
        await route.fulfill(response=resp, body=json.dumps(body), content_type="application/json")
    await pg.route(is_sweep, follow)
    try:
        await regions_routed(pg)
    finally:
        await pg.unroute(is_sweep)


async def regions_routed(pg):
    note = "document.querySelector('#view-spectrum details.note')"
    await pg.evaluate(f"{note}.open = true; document.getElementById('sp-region').scrollIntoView({{block: 'start'}})")
    st = await pg.evaluate("() => { const p = document.getElementById('sp-region'), s = document.getElementById('sp-region-sel'),"
                           " c = document.getElementById('sp-country-sel');"
                           " return {hidden: p.hidden, text: p.textContent, sel: s ? s.value : null, opts: s ? s.options.length : 0,"
                           " cs: c ? [...c.options].map(o => o.textContent) : null, h: s ? Math.round(s.getBoundingClientRect().height) : 0}; }")
    await pg.screenshot(path=f"{OUT}/v53_region.png")
    if st["hidden"] or not st["text"].startswith("Band labels:") or "(guessed from the time zone)" not in st["text"] \
            or st["sel"] != "americas" or st["opts"] != len(CHOICES) or st["cs"] != ["Whole region", "United States", "Canada", "Brazil"]:
        bad(f"the region line: {st}")
    if st["h"] < MIN_TAP_PX:
        bad(f"the region select is {st['h']} px high")
    # the admin picks Europe: one POST, the presets reload (its own chip), a quick view is swept again
    REGION["meta"], t0 = 0, time.time()
    await pg.select_option("#sp-region-sel", "europe")
    await pg.wait_for_function("SP.region && SP.region.id === 'europe' && document.querySelector(\"#sp-presets .chip[data-id='srd868']\") !== null", timeout=10000)
    await pg.wait_for_timeout(300)
    await pg.wait_for_function("!SP.busy", timeout=30000)
    st = await pg.evaluate("() => ({text: document.getElementById('sp-region').textContent, cs: !!document.getElementById('sp-country-sel'),"
                           " status: document.getElementById('sp-status').textContent})")
    if REGION["posts"][-1:] != [{"id": "europe", "country": None}] or not REGION["meta"] or "(guessed" in st["text"] or st["cs"] \
            or "Band labels now follow Europe." not in st["text"]:
        bad(f"after choosing Europe: posts {REGION['posts']}, meta asked {REGION['meta']}x, {st}")
    if not sweeps_since(t0):
        bad("the list was not swept again for the new labels")
    await pg.screenshot(path=f"{OUT}/v53_region_changed.png")
    # back to the Americas with a country
    await pg.select_option("#sp-region-sel", "americas")
    await pg.wait_for_function("SP.region && SP.region.id === 'americas' && !!document.getElementById('sp-country-sel')", timeout=10000)
    await pg.wait_for_function("!SP.busy", timeout=30000)
    await pg.select_option("#sp-country-sel", "CA")
    await pg.wait_for_function("SP.region && SP.region.country === 'CA'", timeout=10000)
    await pg.wait_for_function("!SP.busy", timeout=30000)
    if REGION["posts"][-1] != {"id": "americas", "country": "CA"} or "Americas, Canada" not in await pg.evaluate("document.getElementById('sp-region').textContent"):
        bad(f"choosing a country: {REGION['posts'][-1]}")
    if await pg.evaluate("!!document.querySelector(\"#sp-presets .chip[data-id='srd868']\")"):
        bad("the Europe-only preset stayed after going back to the Americas")
    # changed elsewhere (another page, the chat): the next sweep reports it and the page follows
    REGION.update(id="europe", country=None)
    await sweep_once(pg)
    try:
        await pg.wait_for_function("SP.region && SP.region.id === 'europe' && document.getElementById('sp-region-sel').value === 'europe'"
                                   " && document.querySelector(\"#sp-presets .chip[data-id='srd868']\") !== null", timeout=10000)
    except Exception:
        bad(f"a region changed elsewhere was not followed: {await pg.evaluate('SP.region && SP.region.id')}")
    await same_name_preset(pg)
    await pg.select_option("#sp-region-sel", "americas")
    await pg.wait_for_function("SP.region && SP.region.id === 'americas' && !!document.getElementById('sp-country-sel')", timeout=10000)
    await pg.wait_for_function("!SP.busy", timeout=30000)
    await pg.select_option("#sp-country-sel", "CA")
    await pg.wait_for_function("SP.region && SP.region.country === 'CA'", timeout=10000)
    await pg.wait_for_function("!SP.busy", timeout=30000)
    # the server refuses (an extra account): the selects become text
    REGION["mode"] = "forbid"
    await pg.select_option("#sp-region-sel", "japan")
    await pg.wait_for_function("SP.regionRO === true", timeout=10000)
    st = await pg.evaluate("() => ({text: document.getElementById('sp-region').textContent, sel: !!document.getElementById('sp-region-sel')})")
    if st["sel"] or st["text"].count("Only the admin account can change") != 1 or "Americas, Canada" not in st["text"]:
        bad(f"a refused change (the reason once): {st}")
    REGION["mode"] = "ok"
    # a viewer reads it, from the page's start
    await pg.evaluate("SP.regionRO = false; document.body.dataset.role = 'viewer'; spRegionDraw()")
    st = await pg.evaluate("() => ({text: document.getElementById('sp-region').textContent, sel: document.querySelectorAll('#sp-region select').length})")
    await pg.screenshot(path=f"{OUT}/v53_region_viewer.png")
    if st["sel"] or not st["text"].startswith("Band labels: Americas, Canada") or "Only the admin account can change it." not in st["text"]:
        bad(f"a viewer's region line: {st}")
    await pg.evaluate("document.body.dataset.role = 'admin'; spRegionDraw()")
    # names from the server are text, never markup
    await pg.evaluate("SP.region = Object.assign({}, SP.region, {name: '<img src=x onerror=\"window.shot53_ran=1\">', country_name: null,"
                      " choices: [{id: 'x', name: '<b>y</b>'}]}); spRegionDraw()")
    if await pg.evaluate("[document.querySelectorAll('#sp-region img, #sp-region b').length, window.shot53_ran || 0]") != [0, 0]:
        bad("a region name became markup")
    # a server without the region API: no line
    REGION.update(mode="absent")
    await pg.reload()
    await pg.wait_for_selector("#tab-spectrum")
    await pg.click("#tab-spectrum")
    await pg.wait_for_function("document.querySelectorAll('#sp-presets .chip').length > 3")
    await pg.wait_for_timeout(500)
    if not await pg.evaluate("document.getElementById('sp-region').hidden"):
        bad("the region line shows on a server without the region API")
    REGION.update(mode="ok", id="americas", country=None, guessed=True, source="time zone")


async def same_name_preset(pg):
    """The FM chip on, then a region whose FM preset covers another range: the chip goes off, the view stays, and a
    reload keeps the view the user left (not the new region's FM range)."""
    await pg.click("#sp-presets .chip[data-id='fm']")
    await pg.wait_for_function("!SP.busy", timeout=30000)
    v0 = await pg.evaluate("[SP.view.s, SP.view.e]")
    if not await pg.evaluate("document.querySelector(\"#sp-presets .chip[data-id='fm']\").classList.contains('on')"):
        bad("the FM chip is not on after pressing it")
    await pg.select_option("#sp-region-sel", "japan")
    await pg.wait_for_function("SP.region && SP.region.id === 'japan' && document.querySelector(\"#sp-presets .chip[data-id='fm']\").textContent === 'FM 76-99'", timeout=10000)
    await pg.wait_for_timeout(300)
    await pg.wait_for_function("!SP.busy", timeout=30000)
    chk = "() => ({v: [SP.view.s, SP.view.e], preset: SP.preset, on: document.querySelector(\"#sp-presets .chip[data-id='fm']\").classList.contains('on')," \
          " saved: (JSON.parse(localStorage.getItem('aris_spec') || '{}') || {}).p || null})"
    st = await pg.evaluate(chk)
    if st["v"] != v0 or st["preset"] is not None or st["on"] or st["saved"] is not None:
        bad(f"after a region whose FM preset is another range: {st} (view before {v0})")
    await pg.reload()
    await pg.wait_for_selector("#tab-spectrum")
    await pg.click("#tab-spectrum")
    await pg.wait_for_function("document.querySelectorAll('#sp-presets .chip').length > 3 && SP.region && SP.region.id === 'japan'", timeout=15000)
    st = await pg.evaluate(chk)
    if st["v"] != v0 or st["on"]:
        bad(f"after a reload the view is {st['v']} (left at {v0}), FM chip on: {st['on']}")
    await pg.evaluate("document.querySelector('#view-spectrum details.note').open = true; document.getElementById('sp-region').scrollIntoView({block: 'start'})")


def is_sweep(url):
    return "/api/spectrum?" in url


async def gain_edges(pg, wide=False):
    """Stand-in sweeps whose pieces were measured at different gains: dotted lines at 880 and 960 MHz with the gains
    either side, in one piece and across two; none without the server's pieces; the help says what they mean."""
    mode = {"pieces": True, "dongles": ("00017350",), "spike": None}

    async def answer(route):
        await route.fulfill(status=200, content_type="application/json",
                            body=json.dumps(standin_sweep(route.request.url, mode["pieces"], mode["dongles"], mode["spike"])))
    legend = "document.getElementById('sp-legend').textContent"
    await pg.route(is_sweep, answer)
    try:
        await pg.select_option("#sp-dongle", "00017350")
        await pg.evaluate("specDongle(); specSetView(850, 1000)")
        await sweep_once(pg)
        got = await pg.evaluate("SP.gainEdges")
        want = [{"f": 880.0, "rows": [[37.2, 29.7]], "lab": ["gain 37.2 | 29.7"]},
                {"f": 960.0, "rows": [[29.7, 37.2]], "lab": ["gain 29.7 | 37.2"]}]
        if [{k: v for k, v in x.items() if k != "box"} for x in got] != want:
            bad(f"gain edges in one piece: {got}")
        if "gain 37.2 to 29.7 to 37.2" not in await pg.evaluate(legend):
            bad(f"the legend's gains in one piece: {await pg.evaluate(legend)!r}")
        await pg.evaluate("document.getElementById('spec-canvas').scrollIntoView({block: 'center'})")
        await pg.screenshot(path=f"{OUT}/v53_gain_edges{'_1280' if wide else ''}.png")
        if wide:
            return
        # a trace reaching the top beside the 880 MHz line: that label goes to the foot, the other stays on top
        mode["spike"] = (880.5, -4.0)
        await sweep_once(pg)
        got = await pg.evaluate("() => ({e: SP.gainEdges, top: SP_T, ph: document.getElementById('spec-canvas').clientHeight - SP_T - SP_B})")
        mode["spike"] = None
        e880, e960 = got["e"][0], got["e"][1]
        mid = got["top"] + got["ph"] / 2
        if not e880["lab"] or not e880["box"] or e880["box"][1] < mid or not e960["box"] or e960["box"][1] > mid:
            bad(f"a gain label over a trace near the top: {got}")
        await pg.evaluate("document.getElementById('spec-canvas').scrollIntoView({block: 'center'})")
        await pg.screenshot(path=f"{OUT}/v53_gain_edges_spike.png")
        # two pieces of a wider sweep (cut at 950 MHz, the same gain either side: no line there); the legend lists
        # the gains in frequency order, once per run (not each piece's own list)
        await pg.evaluate("specSetView(800, 1100)")
        await sweep_once(pg)
        got = await pg.evaluate("SP.gainEdges.map(g => g.f)")
        n = await pg.evaluate("SP.plan.chunks.length")
        if got != [880.0, 960.0] or n < 2:
            bad(f"gain edges across {n} pieces: {got}")
        lg = await pg.evaluate(legend)
        if "gain 37.2 to 29.7 to 37.2" not in lg or "29.7 to 29.7" in lg:
            bad(f"the legend's gains across {n} pieces: {lg!r}")
        # the whole 0-6000 MHz: 87.5 / 108 and 880 / 960 MHz are a few pixels apart, so the lines get no labels
        # (a label must plainly belong to its own line); the legend still lists every gain in order
        await pg.evaluate("specSetView(0, 6000)")
        await sweep_once(pg)
        got = await pg.evaluate("SP.gainEdges")
        lg = await pg.evaluate(legend)
        if [x["f"] for x in got] != list(EDGES) or any(x["lab"] for x in got):
            bad(f"gain edges in the full view: {got}")
        if "gain 37.2 to 8.7 to 37.2 to 29.7 to 37.2" not in lg:
            bad(f"the legend's gains in the full view: {lg!r}")
        await pg.evaluate("document.getElementById('spec-canvas').scrollIntoView({block: 'center'})")
        await pg.screenshot(path=f"{OUT}/v53_gain_edges_full.png")
        # two dongles: one line per place, a row per dongle in its colour
        mode["dongles"] = ("00017350", "00026410")
        await pg.select_option("#sp-dongle", "all")
        await pg.evaluate("specDongle(); specSetView(850, 1000)")
        await sweep_once(pg)
        got = await pg.evaluate("SP.gainEdges")
        if [g["rows"] for g in got] != [[[37.2, 29.7], [33.8, 28.0]], [[29.7, 37.2], [28.0, 33.8]]] \
                or got[0]["lab"] != ["gain 37.2 | 29.7", "33.8 | 28.0"]:
            bad(f"gain edges of two dongles: {got}")
        await pg.evaluate("document.getElementById('spec-canvas').scrollIntoView({block: 'center'})")
        await pg.screenshot(path=f"{OUT}/v53_gain_edges_two.png")
        # a server that gives no pieces: no lines
        mode["pieces"] = False
        await sweep_once(pg)
        if await pg.evaluate("SP.gainEdges.length"):
            bad("gain edges drawn without the server's pieces")
        mode["pieces"] = True
        await sweep_once(pg)
        # the help under the Signals heading says what they mean (the list needs a signal: the stand-in has none)
        await pg.evaluate("SP.data.peaks = [{mhz: 915.0, snr_db: 12, band: 'test', kind: 'signal', flag: false}]; SP.helpOpen = true; specPeaks()")
        txt = await pg.evaluate("(document.querySelector('#sp-peaks .sp-help-text') || {}).textContent || ''")
        if "noise floor can step there" not in txt or "gain 37.2 | 29.7" not in txt:
            bad(f"the help does not explain the gain lines: {txt!r}")
        await pg.evaluate("SP.helpOpen = false; spSave(); specPeaks()")
    finally:
        await pg.unroute(is_sweep)
        await pg.select_option("#sp-dongle", "all")
        await pg.evaluate("specDongle()")


async def real_region(ctx):
    """The server's own GET /api/region, when it has one: its answer has the agreed keys and the page's line follows
    it (no stand-in on this page)."""
    pg2 = await ctx.new_page()
    try:
        r = await pg2.request.get(B + "/api/region")
        if r.status == 404:
            print("this server has no /api/region yet: only the stand-in answers were tested")
            return
        body = await r.json()
        keys = ("id", "name", "country", "country_name", "guessed", "source", "choices", "countries")
        if r.status != 200 or [k for k in keys if k not in body]:
            return bad(f"the server's /api/region: HTTP {r.status}, keys {sorted(body)}")
        await pg2.goto(B + "/")
        await pg2.click("#tab-spectrum")
        await pg2.wait_for_function("SP.region !== null", timeout=15000)
        st = await pg2.evaluate("() => ({hidden: document.getElementById('sp-region').hidden, sel: (document.getElementById('sp-region-sel') || {}).value,"
                                " text: document.getElementById('sp-region').textContent,"
                                " cs: [...document.querySelectorAll('#sp-country-sel option')].map(o => o.value).filter(Boolean)})")
        want_cs = [c["id"] for c in body["countries"] if c.get("region") == body["id"]]
        if st["hidden"] or st["sel"] != body["id"] or st["cs"] != want_cs or not st["text"].startswith("Band labels:"):
            bad(f"the region line with the server's own answer {body['id']!r}: {st}")
        print("the server's region:", body["id"], "|", st["text"][:90])
        await pg2.evaluate("document.querySelector('#view-spectrum details.note').open = true;"
                           " document.getElementById('sp-region').scrollIntoView({block: 'start'})")
        await pg2.screenshot(path=f"{OUT}/v53_region_server.png")
        if await pg2.evaluate("document.documentElement.scrollWidth > innerWidth + 1"):
            bad("with the server's region names the page scrolls sideways")
    finally:
        await pg2.close()


async def sideways(pg, where):
    if await pg.evaluate("document.documentElement.scrollWidth > innerWidth + 1 || document.getElementById('view-spectrum').scrollWidth > document.getElementById('view-spectrum').clientWidth + 1"):
        bad(f"{where}: the page scrolls sideways")


async def main():
    async with async_playwright() as p:
        br = await p.chromium.launch()
        ctx = await br.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, has_touch=True)
        pg = await ctx.new_page()
        pg.on("pageerror", lambda e: ERRORS.append(str(e)))
        pg.on("request", lambda r: SWEEPS.append((time.time(), r.url)) if "/api/spectrum?" in r.url else None)
        await pg.route("**/api/region", region_route)
        await pg.route("**/api/spectrum/meta", meta_route)
        cdp = await ctx.new_cdp_session(pg)
        await pg.goto(B + "/login")
        await pg.fill("input[name=username]", "admin")
        await pg.fill("input[name=password]", PW)
        await pg.click("button[type=submit]")
        await pg.wait_for_selector("#tab-spectrum")
        await real_region(ctx)
        await pg.click("#tab-spectrum")
        await pg.wait_for_function("document.querySelectorAll('#sp-presets .chip').length > 3")
        await pg.select_option("#sp-dongle", "all")
        await pg.evaluate("specDongle()")
        await touch_zoom(pg, cdp)
        await sideways(pg, "after the pinch")
        await zoom_sweep(pg, cdp)
        await decode_stop(pg)
        await decode_stop_errors(pg)
        await regions(pg)
        await gain_edges(pg)
        await sideways(pg, "at 390 px")
        await pg.set_viewport_size({"width": 1280, "height": 900})
        await pg.wait_for_timeout(200)
        await gain_edges(pg, wide=True)
        await pg.evaluate("document.querySelector('#view-spectrum details.note').open = true; spLoadRegion()")
        await pg.wait_for_timeout(400)
        r = await pg.evaluate("() => { const p = document.getElementById('sp-region'), s = document.getElementById('sp-region-sel');"
                              " return {h: Math.round(p.getBoundingClientRect().height), sel: !!s}; }")
        await pg.evaluate("document.getElementById('sp-region').scrollIntoView({block: 'center'})")
        await pg.screenshot(path=f"{OUT}/v53_region_1280.png")
        if not r["sel"] or r["h"] > 90:
            bad(f"at 1280 px the region line is {r['h']} px high (select shown: {r['sel']})")
        await sideways(pg, "at 1280 px")
        await br.close()
        print("page errors:", ERRORS or "none")
        if ERRORS:
            bad(f"page errors: {ERRORS}")
        if BAD:
            print(f"{len(BAD)} problem(s)")
            sys.exit(1)
        print("screenshots in", OUT)

asyncio.run(main())
