"""Phone-width screenshots and checks of the Decode card (Spectrum tab) and the two collapsed help texts. The twelve
example cards are made here, at run time, from the signal report's own rules (make_decode_fixtures.example_cards:
arislib.sigreport's example cases through build and card_fields), so a rule that renames or reshapes a key shows on
the page that is tested; the three 'extra' cards and the old card are the hand-written ones of
fixtures_decode_cards.json. Every card is drawn by the page's own decResult(), so no radio time is needed; the
card's buttons are pressed against answers this script gives in the server's place, one of them a refusal. Needs
serve.py running.  python3 shot52.py [port] [outdir]
Ends with status 1 when the page scrolls sideways, a card is wider than its column or holds more than one
highlighted button or another link than 'Explain this result', something that is pressed (in a card, or the panel's
'Decode for' buttons) is under 39 px high, a card
has no tag, the first example's closed card is taller than MAX_1A_PX, a key left out of a card or a card of an
unexpected form breaks it, a long word breaks the layout, text heard over the air becomes markup, a button does
something other than its step says, a refused start loses the card, a double tap starts two jobs, or a result or
the progress lands off the screen."""
import asyncio
import copy
import json
import os
import sys

from playwright.async_api import async_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_decode_fixtures import example_cards, load  # noqa: E402

PORT = sys.argv[1] if len(sys.argv) > 1 else "5097"
OUT = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("ARIS_TEST_DIR", ".")
B = f"http://127.0.0.1:{PORT}"
PW = os.environ.get("ARIS_TEST_PASSWORD", "test-password-123")
FIX = load()
CARDS = example_cards(FIX)          # the examples as the rules give them now, not as a file remembers them
LINK = "Explain this result"
MAX_1A_PX = 430
MIN_TAP_PX = 39
BAD = []
ERRORS = []

MEASURE = """() => {
  const box = document.getElementById('sp-result'), c = box.querySelector('.dec-card'), v = document.getElementById('view-spectrum');
  if (!c) return {card: false, kids: [...box.children].map(e => e.className)};
  const R = x => x.getBoundingClientRect(), r = R(c), col = R(box), port = R(v);
  const out = [...c.querySelectorAll('*')].filter(x => { const q = R(x); return q.width && (q.right > r.right + 0.5 || q.left < r.left - 0.5); });
  const btn = c.querySelector('.dec-acts .btn'), link = c.querySelector('.dec-acts .dec-link'), acts = c.querySelector('.dec-acts'), sum = c.querySelector('.dec-more summary');
  const mid = x => (R(x).top + R(x).bottom) / 2, trk = [...c.querySelectorAll('.dec-bars .trk')].map(x => R(x).width);
  return {card: true, w: Math.round(r.width), h: Math.round(r.height), col: Math.round(col.width),
          wide: r.width > col.width + 0.5 || c.scrollWidth > c.clientWidth + 1 || out.length > 0,
          sideways: document.documentElement.scrollWidth > innerWidth + 1 || v.scrollWidth > v.clientWidth + 1,
          btns: c.querySelectorAll('.btn').length, tag: !!c.querySelector('.tag'), rep: c.classList.contains('rep'),
          buttons: [...c.querySelectorAll('button')].map(b => b.textContent), open: !!c.querySelector('details[open]'),
          links: [...c.querySelectorAll('.dec-link')].map(b => b.textContent),
          taps: [...c.querySelectorAll('button, summary')].map(x => [x.textContent.slice(0, 24), Math.round(R(x).height * 10) / 10]),
          one_row: btn && link ? Math.abs(mid(btn) - mid(link)) < 3 : null,
          gap: acts && sum ? Math.round(R(sum).top - R(acts).bottom) : null,
          trk: trk.length ? Math.round(Math.min(...trk)) : null,
          in_view: r.top >= Math.max(0, port.top) - 1 && r.bottom <= Math.min(innerHeight, port.bottom) + 1,
          rem: parseFloat(getComputedStyle(document.documentElement).fontSize),
          head: ((c.querySelector('.dec-head') || {}).textContent || '').replace(/ /g, ' ')};
}"""
STATE = ("() => ({tab: activeTab(), sel: SP.sel, t: SP.decT, job: SP.job, on: (document.querySelector('#sp-dec-t button.on') || {dataset: {}}).dataset.s,"
         " head: ((document.querySelector('#sp-result .dec-head') || {}).textContent || '').replace(/ /g, ' '),"
         " kids: [...document.getElementById('sp-result').children].map(e => e.className.split(' ')[0]),"
         " msg: [...document.querySelectorAll('#sp-result .infobox, #sp-result .warnbox')].map(e => e.textContent),"
         " off: document.querySelectorAll('#sp-sel button:disabled').length, held: document.getElementById('sp-result').style.minHeight,"
         " box_h: Math.round(document.getElementById('sp-result').getBoundingClientRect().height),"
         " map: typeof MAP === 'undefined' ? null : MAP.freq})")


def bad(what):
    BAD.append(what)
    print("FAIL:", what)


def with_step(action):
    """The first card (examples, then extras) whose step is `action`: (name, card), or (None, None)."""
    for name, card in list(CARDS.items()) + list(FIX["extra"].items()):
        if (card.get("step") or {}).get("action") == action and (card.get("step") or {}).get("label"):
            return name, card
    bad(f"no card with the step {action!r}: its button is not tested")
    return None, None


def step_mhz(card):
    return (card.get("step") or {}).get("mhz") or card.get("measured_mhz") or card["freq_mhz"]


async def draw(pg, card, seconds=30, top=True):
    """The card on the page, as a finished Decode job gives it. top=False: without this script's own scroll."""
    await pg.evaluate("([k, s, top]) => { const f = Number(k.freq_mhz); if (k.freq_mhz != null && isFinite(f)) specSelect(f);"
                      " decResult({state: 'done', seconds: s, card: k});"
                      " if (top) document.getElementById('sp-sel').scrollIntoView({block: 'start'}); }", [card, seconds, top])
    await pg.wait_for_timeout(120)
    return await pg.evaluate(MEASURE)


def check(name, m, card):
    if not m.get("card") or not m["tag"]:
        return bad(f"{name}: no .dec-card .tag")
    if m["sideways"]:
        bad(f"{name}: the page scrolls sideways")
    if m["wide"]:
        bad(f"{name}: the card ({m['w']} px) is wider than its column ({m['col']} px) or something sticks out of it")
    if m["btns"] > 1:
        bad(f"{name}: {m['btns']} highlighted buttons in one card")
    if not m["rep"]:
        return
    step = card.get("step") or {}
    has = step.get("action") not in (None, "none")
    if m["btns"] != (1 if has else 0):
        bad(f"{name}: step {step.get('action')!r} but {m['btns']} highlighted buttons")
    if card.get("headline") and m["head"] != card["headline"]:
        bad(f"{name}: the headline is not the card's")
    # one highlighted step and one quiet link; capture and locate are the panel's buttons, or the step itself
    if m["links"] != [LINK] or m["buttons"] != ([step.get("label")] if has else []) + [LINK]:
        bad(f"{name}: the card's buttons are {m['buttons']}")
    small = [t for t in m["taps"] if t[1] < MIN_TAP_PX]
    if small:
        bad(f"{name}: pressed things under {MIN_TAP_PX} px high: {small}")
    if m["gap"] is not None and m["gap"] < 4:
        bad(f"{name}: {m['gap']} px between the button row and Details")


async def press(pg, card, text, routes, after=STATE, taps=1):
    """Draw the card, press its button `text` with the server's answers replaced by `routes` {url glob: reply, or
    (status, reply)}, and return what `after` (JavaScript) says once the page has settled, the bodies the page sent,
    and the messages under the card right after the press. taps=2: two taps 120 ms apart on the same button, as a
    finger does them (no waiting for the page between them)."""
    seen = []

    def answer(reply):
        status, body = reply if isinstance(reply, tuple) else (200, reply)

        async def handler(route):
            if route.request.method == "POST":
                seen.append(json.loads(route.request.post_data or "null"))
            await asyncio.sleep(0.4)
            await route.fulfill(status=status, content_type="application/json", body=json.dumps(body))
        return handler

    for glob, reply in routes.items():
        await pg.route(glob, answer(reply))
    await draw(pg, card)
    if taps == 1:
        await pg.click(f"#sp-result .dec-card button:text-is('{text}')")
    else:
        await pg.evaluate("async (t) => { const b = [...document.querySelectorAll('#sp-result .dec-card button')].find(x => x.textContent === t);"
                          " b.click(); await new Promise(r => setTimeout(r, 120)); b.click(); }", text)
    early = await pg.evaluate("() => [...document.querySelectorAll('#sp-result .infobox, #sp-result .warnbox')].map(x => x.textContent)")
    await pg.wait_for_timeout(1900)
    got = await pg.evaluate(after)
    for glob in routes:
        await pg.unroute(glob)
    await pg.evaluate("switchTab('spectrum')")
    return got, seen, early


async def buttons(pg):
    """Every step action does what it says and nothing more; a capture keeps the card and puts its message below it;
    a start the server refuses keeps the card too."""
    name, card = with_step("decode")
    if card:
        step, f = card["step"], step_mhz(card)
        ask = [{"freq_mhz": f, "seconds": step["seconds"]}]
        done = {"id": "t52", "state": "done", "freq_mhz": f, "seconds": step["seconds"], "card": CARDS["2"]}
        got, seen, _ = await press(pg, card, step["label"], {"**/api/decode": {"job": "t52"}, "**/api/decode/t52": done})
        # the step's time is for that press: "Decode for" stays at the 30 s it was left on
        if seen != ask or got["sel"] != f or got["on"] != "30" or got["t"] != 30 or got["head"] != CARDS["2"]["headline"] \
                or got["off"] or got["held"] or got["job"]:
            bad(f"decode step of {name}: asked {seen}, then {got}")
        # the server is busy with another job: the card stays, the message goes under it, the buttons come back
        got, seen, _ = await press(pg, card, step["label"], {"**/api/decode": (409, {"error": "Already decoding 98.7 MHz", "job": "abc"})})
        if seen != ask or got["kids"] != ["dec-card", "warnbox"] or got["head"] != card.get("headline") or got["off"] \
                or got["msg"] != ["Could not start: Already decoding 98.7 MHz"] or got["t"] != 30 or got["held"] or got["job"]:
            bad(f"a refused decode step of {name}: asked {seen}, then {got}")
        await pg.screenshot(path=f"{OUT}/v52_refused.png")
    started = {"status": "ok", "message": "Capture started on Dongle 1 at 433.92 MHz."}
    name, card = with_step("capture")
    if card:
        ask = {"freq_mhz": step_mhz(card)}
        if (card["step"].get("minutes") or 0) > 0:
            ask["minutes"] = card["step"]["minutes"]
        for taps in (1, 2):                                 # two taps are still one start
            got, seen, early = await press(pg, card, card["step"]["label"], {"**/api/capture/start": started}, taps=taps)
            if seen != [ask] or got["kids"] != ["dec-card", "infobox"] or got["msg"] != [started["message"]] \
                    or len(early) != 1 or not early[0].startswith("Starting"):
                bad(f"capture step of {name}, {taps} tap(s): asked {seen}, first {early}, then {got}")
    got, seen, early = await press(pg, FIX["old_card"], "Capture until decoded", {"**/api/capture/start": started})
    if seen != [{"freq_mhz": 921.60003}] or got["kids"] != ["infobox"] or not early[0].startswith("Starting"):
        bad(f"the old card's capture: asked {seen}, first {early}, then {got}")
    name, card = with_step("check")
    if card:
        got, seen, _ = await press(pg, card, card["step"]["label"], {"**/api/chat_send": {"ok": True}})
        if seen != [{"message": "/check"}] or got["tab"] != "chat":
            bad(f"check step of {name}: asked {seen}, then {got}")
    for action, tab in (("activity", "activity"), ("devices", "devices"), ("locate", "map")):
        name, card = with_step(action)
        if card:
            got, _, _ = await press(pg, card, card["step"]["label"], {})
            if got["tab"] != tab or (tab == "map" and abs(got["map"] - step_mhz(card)) > 0.001):
                bad(f"{action} step of {name}: ended on {got['tab']} (map at {got['map']})")
    got, seen, _ = await press(pg, CARDS["2"], LINK, {"**/api/chat_send": {"ok": True}})
    if got["tab"] != "chat" or f"{CARDS['2']['freq_mhz']:.4f} MHz" not in (seen or [{}])[0].get("message", ""):
        bad(f"{LINK}: asked {seen}, then {got}")


async def robust(pg):
    """A card with any new key left out, null or of another form still draws (without report_v: the old card), a
    word of any length stays inside the card, and text is never markup."""
    base = CARDS["1a"]
    for key in FIX["new_keys"]:
        for how in ("missing", "null"):
            k = copy.deepcopy(base)
            if how == "missing":
                del k[key]
            else:
                k[key] = None
            m = await draw(pg, k)
            if not m.get("card") or not m["tag"] or m["sideways"] or m["wide"] or m["rep"] != (key != "report_v"):
                bad(f"1a with {key} {how}: {m}")
    k = {"freq_mhz": 172.8231, "report_v": 1}               # nothing but the mark that a report exists
    m = await draw(pg, k)
    if not m.get("card") or not m["tag"]:
        bad(f"a card with only report_v: {m}")
    # forms nobody promised: the card says less or falls back to the plain card, it is never an empty panel
    odd = {"bars [null]": {"bars": [None]}, "bars a word": {"bars": "x"}, "details an object": {"details": {}},
           "details a word": {"details": "x"}, "lines a word": {"details": [{"title": "T", "lines": "x"}]},
           "slots a word": {"details": [{"title": "T", "lines": ["a"], "strip": {"slots": "x"}}]},
           "step a word": {"step": "x"}, "carrier a word": {"carrier": "x"}, "doubtful a number": {"doubtful": 3},
           "dongles a word": {"dongles": "x", "report_v": None}, "no frequency": {"freq_mhz": None, "measured_mhz": None, "carrier": None}}
    for name, change in odd.items():
        k = copy.deepcopy(base)
        k.update(change)
        m = await draw(pg, k)
        if not m.get("card") or not m["tag"] or m["sideways"] or m["wide"]:
            bad(f"1a with {name}: {m}")
    text = await pg.evaluate("document.querySelector('#sp-result .dec-card').textContent")
    if "0.0000" in text or "NaN" in text:
        bad(f"a card without a frequency shows one: {text[:80]!r}")
    k = {"report_v": 1, "tag": "HEARD", "step": {"action": "decode", "label": "Decode again", "seconds": 10}}
    m = await draw(pg, k)                                   # a radio step on a card that names no frequency: no button
    if not m.get("card") or m["btns"] or m["buttons"] != [LINK]:
        bad(f"a decode step without a frequency: {m}")
    if ERRORS:
        bad(f"page errors from cards of an unexpected form: {ERRORS}")
    # a 300-character word in every slot at once, then in each alone
    long = "W" * 300

    def fill(k, word, only=None):
        slots = {"headline": lambda: k.update(headline=word), "sure": lambda: k.update(sure=word),
                 "history_line": lambda: k.update(history_line=word), "bars_note": lambda: k.update(bars_note=word),
                 "tag": lambda: k.update(tag=word), "history_tag": lambda: k.update(history_tag=word),
                 "carrier.text": lambda: k["carrier"].update(text=word), "carrier.note": lambda: k["carrier"].update(note=word),
                 "step.label": lambda: k["step"].update(label=word), "step.reason": lambda: k["step"].update(reason=word),
                 "bars.dongle": lambda: k["bars"][0].update(dongle=word), "bars.text": lambda: k["bars"][0].update(text=word),
                 "details.title": lambda: k["details"][0].update(title=word), "details.lines": lambda: k["details"][0].update(lines=[word])}
        for name, put in slots.items():
            if only in (None, name):
                put()
        return list(slots)

    names = fill(copy.deepcopy(base), long)
    for only in [None] + names:
        k = copy.deepcopy(base)
        fill(k, long, only)
        m = await draw(pg, k)
        if only is None:
            await pg.screenshot(path=f"{OUT}/v52_long_words.png")
        if await pg.locator("#sp-result .dec-more summary").count():
            await pg.click("#sp-result .dec-more summary")
            m = await pg.evaluate(MEASURE)
        if not m.get("card") or m["wide"] or m["sideways"]:
            bad(f"a 300-character word in {only or 'every slot'}: wide {m.get('wide')}, sideways {m.get('sideways')}, card {m.get('w')} px")
    k = copy.deepcopy(base)                                 # a receiver's label of 40 characters: the bars stay bars
    for b in k["bars"]:
        b["dongle"] = "X" * 40
    m = await draw(pg, k)
    if m["wide"] or m["sideways"] or (m["trk"] is not None and m["trk"] < 50):
        bad(f"receiver labels of 40 characters: the shortest bar track is {m['trk']} px")
    evil = "<img src=x onerror=\"window.shot52_ran=1\"><b>x</b> **bold** `code`"
    k = copy.deepcopy(base)
    fill(k, evil)
    k["details"][0]["strip"] = {"slots": [1, 0, 0.5], "label": evil}
    await draw(pg, k)
    n = await pg.evaluate("() => [document.querySelectorAll('#sp-result img, #sp-result .dec-card strong, #sp-result .dec-card code').length, window.shot52_ran || 0]")
    if n != [0, 0]:
        bad(f"text heard over the air became markup: {n}")


async def helps(pg):
    """The Signals help and the tab's notes: closed until asked for, big enough to press, and the help stays as it
    was left."""
    box = "document.getElementById('sp-peaks')"
    state = ("() => ({text: !!document.querySelector('#sp-peaks .sp-help-text'), pk: document.querySelector('#sp-peaks .sp-help').classList.contains('pk'),"
             " first: (document.querySelector('#sp-peaks .pk .f') || {}).textContent || '', saved: (JSON.parse(localStorage.getItem('aris_spec') || '{}')).help,"
             " h: Math.round(document.querySelector('#sp-peaks .sp-head').getBoundingClientRect().height),"
             " tap: Math.round(document.querySelector('#sp-peaks .sp-help').getBoundingClientRect().height),"
             " sideways: document.documentElement.scrollWidth > innerWidth + 1})")
    await pg.evaluate("specClear()")
    await pg.click("#sp-presets .chip[data-id='fm']")
    await pg.click("text=Sweep once")
    await pg.wait_for_function("SP.data !== null && !SP.busy", timeout=60000)
    await pg.evaluate(f"{box}.scrollIntoView({{block: 'start'}})")
    a = await pg.evaluate(state)
    await pg.screenshot(path=f"{OUT}/v52_help_closed.png")
    await pg.click("#sp-peaks .sp-help")
    await pg.evaluate("specPeaks()")                         # the list is rebuilt after every sweep
    await pg.evaluate(f"{box}.scrollIntoView({{block: 'start'}})")
    b = await pg.evaluate(state)
    await pg.screenshot(path=f"{OUT}/v52_help_open.png")
    await pg.click("#sp-peaks .sp-help")
    c = await pg.evaluate(state)
    if a["text"] or not b["text"] or c["text"] or b["saved"] is not True or c["saved"] is not False:
        bad(f"the Signals help does not open, stay and close: {a} {b} {c}")
    if a["pk"] or not a["first"] or a["sideways"] or b["sideways"] or a["tap"] < MIN_TAP_PX:
        bad(f"the Signals help row: {a}")
    note = "document.querySelector('#view-spectrum details.note')"
    if await pg.evaluate(f"{note}.open"):
        bad("the tab's notes are open before anyone asked")
    tap = await pg.evaluate(f"Math.round({note}.querySelector('summary').getBoundingClientRect().height)")
    if tap < MIN_TAP_PX:
        bad(f"'How this tab works' is {tap} px high")
    await pg.evaluate(f"{note}.scrollIntoView({{block: 'end'}})")
    await pg.screenshot(path=f"{OUT}/v52_notes_closed.png")
    await pg.click("#view-spectrum details.note summary")
    await pg.evaluate(f"{note}.scrollIntoView({{block: 'start'}})")
    await pg.screenshot(path=f"{OUT}/v52_notes_open.png")
    if not await pg.evaluate(f"{note}.open") or await pg.evaluate("document.documentElement.scrollWidth > innerWidth + 1"):
        bad("the tab's notes do not open cleanly")
    await pg.click("#view-spectrum details.note summary")


async def under_finger(pg):
    """With a swept view (the Signals list sits below the panel) and nothing scrolled by this script: a result
    comes into view by itself; a press shows its progress on the screen; the box keeps its height during the run, so
    a second tap lands on nothing that acts and one job is started; the panel's buttons are off meanwhile."""
    name, card = with_step("decode")
    if not card:
        return
    f, secs = step_mhz(card), card["step"]["seconds"]
    edge = "document.getElementById('sp-dec-go').scrollIntoView({block: 'end'})"       # the result box is below the screen
    await pg.evaluate("([k]) => { specSelect(Number(k.freq_mhz)); decBox().innerHTML = ''; " + edge + "; }", [card])
    m = await draw(pg, card, top=False)
    await pg.screenshot(path=f"{OUT}/v52_result_in_view.png")
    if not m.get("card") or not m["in_view"]:
        bad(f"a result that arrives with the Decode button at the bottom edge is not brought into view: {m}")
    posts, polls = [], {"n": 0}
    pg.on("request", lambda r: posts.append(r.url.split("/api/")[-1]) if r.method == "POST" else None)

    async def start(route):
        await asyncio.sleep(0.3)
        await route.fulfill(status=200, content_type="application/json", body=json.dumps({"job": "t52b"}))

    async def poll(route):
        polls["n"] += 1
        j = {"id": "t52b", "state": "running" if polls["n"] < 3 else "done", "freq_mhz": f, "seconds": secs, "elapsed": polls["n"],
             "expected": secs, "progress": "Listening on every dongle...", "card": CARDS["2"]}
        await route.fulfill(status=200, content_type="application/json", body=json.dumps(j))

    async def refuse(route):                                # a tap that slipped onto the list must not start a real job here
        await route.fulfill(status=409, content_type="application/json", body=json.dumps({"error": "test"}))

    await pg.route("**/api/decode", start)
    await pg.route("**/api/decode/t52b", poll)
    await pg.route("**/api/fm_names", refuse)
    prog = ("() => { const c = document.querySelector('#sp-result .dec-card'), r = c && c.getBoundingClientRect();"
            " const p = document.getElementById('view-spectrum').getBoundingClientRect();"
            " return {card: !!c, rep: !!c && c.classList.contains('rep'), off: document.querySelectorAll('#sp-sel .btns button:disabled').length,"
            " in_view: !!c && r.top >= Math.max(0, p.top) - 1 && r.bottom <= Math.min(innerHeight, p.bottom) + 1,"
            " box_h: Math.round(document.getElementById('sp-result').getBoundingClientRect().height), job: SP.job, t: SP.decT}; }")
    # 1. the panel's own Decode, pressed with the box below the screen
    await pg.evaluate("() => { decBox().innerHTML = ''; " + edge + "; }")
    await pg.click("#sp-dec-go")
    await pg.wait_for_timeout(700)
    a = await pg.evaluate(prog)
    await pg.screenshot(path=f"{OUT}/v52_progress_in_view.png")
    await pg.wait_for_function("SP.job === null", timeout=15000)
    await pg.wait_for_timeout(200)
    b = await pg.evaluate(MEASURE)
    off = await pg.evaluate("document.querySelectorAll('#sp-sel button:disabled').length")
    if not a["card"] or a["rep"] or not a["in_view"] or a["off"] != 4 or not a["job"]:
        bad(f"a Decode pressed at the bottom edge: its progress is not on screen or the panel's buttons stay live: {a}")
    if not b.get("card") or not b["rep"] or not b["in_view"] or off:
        bad(f"the result of that Decode is not on screen, or buttons stayed off ({off}): {b}")
    # 2. two taps on the card's step, the second on whatever is under the finger by then
    polls["n"] = 0
    del posts[:]
    m = await draw(pg, card)
    under = await pg.evaluate("async (t) => { const b = [...document.querySelectorAll('#sp-result .dec-card button')].find(x => x.textContent === t);"
                              " const r = b.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;"
                              " const tap = () => { const e = document.elementFromPoint(x, y); if (!e) return 'nothing';"
                              "   const where = e.closest('#sp-result') ? 'in the result box' : e.tagName + ' ' + (e.textContent || '').slice(0, 40); e.click(); return where; };"
                              " b.click(); await new Promise(z => setTimeout(z, 120)); const one = tap();"     # the server has not answered yet
                              " await new Promise(z => setTimeout(z, 500)); return [one, tap()]; }",        # the progress is up
                              card["step"]["label"])
    a = await pg.evaluate(prog)
    await pg.wait_for_function("SP.job === null", timeout=15000)
    await pg.wait_for_timeout(200)
    b = await pg.evaluate(STATE)
    if posts != ["decode"] or under != ["in the result box"] * 2 or a["box_h"] < m["h"] or a["rep"] or a["off"] != 4:
        bad(f"a double tap on the step of {name}: POSTs {posts}, the later taps landed {under}, box {a['box_h']} px for a card of {m['h']} px, {a}")
    if b["held"] or b["off"] or b["t"] != 30 or b["on"] != "30" or b["head"] != CARDS["2"]["headline"]:
        bad(f"after that Decode: {b}")
    for glob in ("**/api/decode", "**/api/decode/t52b", "**/api/fm_names"):
        await pg.unroute(glob)


async def wide(pg):
    """On a wide screen the card stays a column of text (about 46rem)."""
    await pg.set_viewport_size({"width": 1280, "height": 900})
    await draw(pg, CARDS["1a"])
    if await pg.locator("#sp-result .dec-more summary").count():
        await pg.click("#sp-result .dec-more summary")
    m = await pg.evaluate(MEASURE)
    await pg.screenshot(path=f"{OUT}/v52_card_1a_1280.png")
    if not m.get("card") or m["w"] > 46 * m["rem"] + 2 or m["wide"] or m["sideways"]:
        bad(f"at 1280 px the card is {m.get('w')} px wide (at most {46 * m.get('rem', 16):.0f})")
    await pg.set_viewport_size({"width": 390, "height": 844})


async def main():
    async with async_playwright() as p:
        br = await p.chromium.launch()
        pg = await br.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2, has_touch=True)
        pg.on("pageerror", lambda e: ERRORS.append(str(e)))
        await pg.goto(B + "/login")
        await pg.fill("input[name=username]", "admin")
        await pg.fill("input[name=password]", PW)
        await pg.click("button[type=submit]")
        await pg.wait_for_selector("#tab-spectrum")
        await pg.click("#tab-spectrum")
        await pg.wait_for_function("document.querySelectorAll('#sp-presets .chip').length > 3")
        sizes, first = {}, {}
        for group, cards in (("cards", CARDS), ("extra", FIX["extra"])):
            for name, card in cards.items():
                m = await draw(pg, card, 30)
                check(name, m, card)
                sizes[name] = m.get("h")
                first.setdefault(name, m)
                await pg.screenshot(path=f"{OUT}/v52_card_{name}.png")                      # what the phone shows
                if await pg.locator("#sp-result .dec-more summary").count():
                    await pg.click("#sp-result .dec-more summary")
                    await pg.wait_for_timeout(100)
                    check(name + " (Details open)", await pg.evaluate(MEASURE), card)
                    await pg.set_viewport_size({"width": 390, "height": 2600})             # the whole opened card
                    await pg.locator("#sp-result .dec-card").screenshot(path=f"{OUT}/v52_card_{name}_details.png")
                    await pg.set_viewport_size({"width": 390, "height": 844})
                elif card.get("details"):
                    bad(f"{name}: no Details")
        if (sizes.get("1a") or 999) > MAX_1A_PX:
            bad(f"the closed card of case 1a is {sizes.get('1a')} px tall (at most {MAX_1A_PX})")
        # the panel's "Decode for" buttons are pressed like everything else: about 40 px high
        hs = await pg.evaluate("[...document.querySelectorAll('#sp-dec-t button')].map(b => Math.round(b.getBoundingClientRect().height * 10) / 10)")
        if not hs or min(hs) < MIN_TAP_PX:
            bad(f"the 'Decode for' buttons are {hs} px high")
        if first.get("1a", {}).get("one_row") is False:
            bad("case 1a: the link is not on the step button's row")
        m = await draw(pg, FIX["old_card"], 10)                                            # no report: the card as before
        check("old card", m, FIX["old_card"])
        if m.get("rep") or m.get("buttons") != ["Capture until decoded", "Ask AI about it", "Locate"]:
            bad(f"the old card changed: {m}")
        await pg.screenshot(path=f"{OUT}/v52_card_old.png")
        await robust(pg)
        await buttons(pg)
        await wide(pg)
        await helps(pg)
        await under_finger(pg)
        print("closed card heights (px):", sizes)
        print("page errors:", ERRORS or "none")
        await br.close()
        if ERRORS:
            bad(f"page errors: {ERRORS}")
        if BAD:
            print(f"{len(BAD)} problem(s)")
            sys.exit(1)
        print("screenshots in", OUT)

asyncio.run(main())
