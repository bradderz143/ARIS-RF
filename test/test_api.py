"""Web API with simulated radios: login, security checks, spectrum, map (place, measure, calibrate), the Decode
button's jobs, captures, the console's internal endpoint, engine routing.
python3 -m unittest test_api   (about a minute: the fake radios really generate IQ)"""
import json
import logging.handlers
import os
import threading
import time
import unittest

import harness
from werkzeug.security import generate_password_hash

from arisweb import app as webapp

PW = "test-password-123"
APP = webapp.configure({"admin_user": "admin", "admin_hash": generate_password_hash(PW), "secret_key": "k" * 32})
H = {"X-ARIS": "1"}


def client():
    c = APP.test_client()
    r = c.post("/login", data={"username": "admin", "password": PW})
    assert r.status_code == 302, r.status_code
    return c


class Security(unittest.TestCase):
    def test_needs_login(self):
        c = APP.test_client()
        self.assertEqual(c.get("/api/map/state").status_code, 401)
        self.assertEqual(c.get("/").status_code, 302)

    def test_engine_log_lines_reach_aris_log_from_the_gui_process(self):
        # v4.9.5 (found on a test station): the band capture's "rtl_433 decodes bursts in batches" line is log.info in the
        # engine, and the GUI process (where captures run) had no handler for it; only the chat console wrote aris.log
        import logging
        eng = __import__("arisweb.engine", fromlist=["x"]).engine()
        lg = logging.getLogger("aris")
        self.assertTrue(any(isinstance(h, logging.handlers.RotatingFileHandler) and h.baseFilename == str(eng.LOG_FILE)
                            for h in lg.handlers), lg.handlers)
        eng.log.info("band capture: rtl_433 decodes bursts in batches (test line %d)", 4950)
        for h in lg.handlers:
            h.flush()
        self.assertIn("decodes bursts in batches (test line 4950)", eng.LOG_FILE.read_text())

    def test_internal_endpoint_is_local_only(self):
        # v4.9.4: the chat console's capture endpoint takes its token from this machine only
        from arisweb import jobs
        tok = jobs.INTERNAL.get("token") or jobs.init_internal_token()
        c = APP.test_client()
        r = c.post("/api/internal/watchdog", json={"op": "stop", "args": {}}, headers={"X-ARIS": "1", "X-ARIS-Internal": tok},
                   environ_base={"REMOTE_ADDR": "192.168.1.20"})
        self.assertEqual(r.status_code, 403)
        r = c.post("/api/internal/watchdog", json={"op": "stop", "args": {}}, headers={"X-ARIS": "1", "X-ARIS-Internal": tok},
                   environ_base={"REMOTE_ADDR": "::ffff:127.0.0.1"})          # a '::' bind sees loopback IPv4-mapped
        self.assertNotEqual(r.status_code, 403)
        r = c.post("/api/internal/watchdog", json={"op": "nothing", "args": {}}, headers={"X-ARIS": "1", "X-ARIS-Internal": tok},
                   environ_base={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(r.status_code, 400)                       # past the address and token checks

    def test_behind_the_https_proxy_clients_are_counted_apart_and_the_internal_endpoint_stays_closed(self):
        # v4.9.7: `tailscale serve` connects from 127.0.0.1 for every phone and adds X-Forwarded-For
        from arisweb import jobs
        c = APP.test_client()
        lo = {"REMOTE_ADDR": "127.0.0.1"}
        for _ in range(5):
            c.post("/login", data={"username": "admin", "password": "nope"}, environ_base=lo,
                   headers={"X-Forwarded-For": "100.64.0.9"})
        r = c.post("/login", data={"username": "admin", "password": "nope"}, environ_base=lo,
                   headers={"X-Forwarded-For": "100.64.0.9"})
        self.assertEqual(r.status_code, 429)                                   # that phone is locked out ...
        r = c.post("/login", data={"username": "admin", "password": "nope"}, environ_base=lo,
                   headers={"X-Forwarded-For": "100.64.0.10"})
        self.assertEqual(r.status_code, 200)                                   # ... not every other one
        r = c.get("/login", environ_base=lo, headers={"X-Forwarded-For": "100.64.0.10", "X-Forwarded-Proto": "https"})
        self.assertIn("max-age", r.headers.get("Strict-Transport-Security", ""))
        self.assertNotIn("Strict-Transport-Security", c.get("/login", environ_base=lo).headers)    # plain local: none
        tok = jobs.INTERNAL.get("token") or jobs.init_internal_token()
        r = c.post("/api/internal/watchdog", json={"op": "nothing", "args": {}}, environ_base=lo,
                   headers={"X-ARIS": "1", "X-ARIS-Internal": tok, "X-Forwarded-For": "100.64.0.10"})
        self.assertEqual(r.status_code, 403)                                   # a request through the proxy is not local

    def test_other_console_commands_stay_out_of_the_console(self):
        # v4.9.4: '/run <tool> ...' and 'exit' typed in the chat reached the console as commands
        from arisweb import bridge
        for msg in ("/run set_timing_defaults", "/raw", "/reset", "exit", "quit", "/tools"):
            self.assertFalse(bridge.SLASH_COMMANDS.match(msg), msg)
        for msg in ("/run set_timing_defaults dongle=1", " /raw", "exit", "QUIT", "/cleanup confirm x"):
            self.assertTrue(bridge.not_a_chat_command(msg), msg)
        for msg in ("/check", "/cleanup confirm", "/devices Citroen", "investigate 433.92", "what is on 92.1", ""):
            self.assertFalse(bridge.not_a_chat_command(msg), msg)
        b = bridge.ArisAIBridge.__new__(bridge.ArisAIBridge)
        b.messages, b.seq, b.gen, b.busy, b.status_line = [], 0, 0, False, "Ready"
        b.state_lock, b.proc, b.last_notice = threading.Lock(), None, None
        b.start_process = lambda: (_ for _ in ()).throw(AssertionError("the console must not be started"))
        self.assertTrue(b.ask_async("/run set_timing_defaults"))
        self.assertEqual([m["role"] for m in b.messages], ["user", "ai"])
        self.assertIn("not a chat command", b.messages[-1]["text"])
        self.assertFalse(b.busy)
        b.ask_async("exit")
        self.assertIn("not a chat command", b.messages[-1]["text"])
        self.assertFalse(b.busy)

    def test_changes_need_header(self):
        c = client()
        self.assertEqual(c.post("/api/map/plan", json={"name": "x"}).status_code, 403)
        self.assertEqual(c.post("/api/map/plan", json={"name": "x"}, headers=H).status_code, 200)

    def test_page_has_version_and_tabs(self):
        c = client()
        html = c.get("/").get_data(as_text=True)
        eng = __import__("arisweb.engine", fromlist=["x"])
        self.assertIn("v" + eng.version(), html)
        for t in ("tab-spectrum", "tab-map", "tab-activity", "tab-devices", "/static/js/map.js",
                  "/static/js/devices.js", "dev-filter", "dev-forget"):
            self.assertIn(t, html)
        self.assertEqual(c.get("/static/js/spectrum.js").status_code, 200)
        self.assertEqual(c.get("/static/js/devices.js").status_code, 200)

    def test_wrong_password_lockout(self):
        c = APP.test_client()
        for _ in range(5):
            c.post("/login", data={"username": "admin", "password": "nope"}, environ_base={"REMOTE_ADDR": "10.9.9.9"})
        r = c.post("/login", data={"username": "admin", "password": PW}, environ_base={"REMOTE_ADDR": "10.9.9.9"})
        self.assertEqual(r.status_code, 429)


class ExtraLogins(unittest.TestCase):
    """A second account (a viewer, for someone who may look but not change settings): only its hash is stored, added and removed
    with aris_gui.py --add-user / --remove-user, valid at once (no restart), and the main login keeps working."""
    def setUp(self):
        from arisweb import auth
        self.auth = auth
        auth._write_secrets({"admin_user": "admin", "admin_hash": generate_password_hash(PW), "secret_key": "k" * 32})

    def tearDown(self):
        try:
            self.auth.SECRETS_PATH.unlink()
        except OSError:
            pass

    def _login(self, user, pw):
        return APP.test_client().post("/login", data={"username": user, "password": pw},
                                      environ_base={"REMOTE_ADDR": "10.7.7.%d" % (len(user) + len(pw))}).status_code

    def test_a_hash_this_werkzeug_cannot_read_is_a_wrong_password_never_an_error(self):
        """An older Werkzeug (before 2.3: Debian 12, Ubuntu 22.04) raises on a scrypt hash. A login must answer
        "wrong", also for an unknown name, whose made-up hash is of the kind this Werkzeug makes itself."""
        import werkzeug.security as ws
        from arisweb import auth
        real, real_load = ws.check_password_hash, auth.load_secrets

        def old_werkzeug(h, pw):
            if str(h).startswith("scrypt:"):
                raise ValueError("unsupported hash type scrypt:32768:8:1")
            return real(h, pw)
        sec = {"admin_user": "boss", "admin_hash": "scrypt:32768:8:1$salt$00",
               "extra_users": {"viewer": "scrypt:32768:8:1$salt$00"}}
        ws.check_password_hash, auth.load_secrets = old_werkzeug, (lambda: sec)
        try:
            for name in ("boss", "viewer", "nobody"):
                self.assertEqual(auth.check_login(name, "a password"), "")
        finally:
            ws.check_password_hash, auth.load_secrets = real, real_load
        made = ws.generate_password_hash("x").split("$")[0]
        self.assertEqual(auth._dummy_hash().split("$")[0], made)       # checking it costs what a real one costs
        self.assertFalse(auth._hash_matches(auth._dummy_hash(), "a password"))

    def test_add_login_remove(self):
        import io
        import os
        import stat
        self.assertEqual(self._login("viewer", "a-long-random-secret"), 200)            # not yet: the form again
        self.auth.cmd_add_user("viewer", io.StringIO(generate_password_hash("a-long-random-secret") + "\n"))
        self.assertEqual(self._login("viewer", "a-long-random-secret"), 302)
        self.assertEqual(self._login("viewer", "wrong-secret"), 200)
        self.assertEqual(self._login("admin", PW), 302)                                  # the main login unchanged
        self.assertNotIn("a-long-random-secret", self.auth.SECRETS_PATH.read_text())
        self.assertEqual(stat.S_IMODE(os.stat(self.auth.SECRETS_PATH).st_mode), 0o600)
        self.auth.cmd_remove_user("viewer")
        self.assertEqual(self._login("viewer", "a-long-random-secret"), 200)

    def test_extra_account_is_a_viewer_in_the_chat_and_the_apis(self):
        """Review (v4.9.5): 'set up everything' and 'remove dongle 2' route straight to admin tools and were not
        in the chat's phrase list; marking a device known changes what the station reports (admin only, like
        Forget); a session from before v4.9.4 has no role and must still be the admin's."""
        import io
        from arisweb import bridge
        self.auth.cmd_add_user("viewer", io.StringIO(generate_password_hash("a-long-random-secret") + "\n"))
        c = APP.test_client()
        self.assertEqual(c.post("/login", data={"username": "viewer", "password": "a-long-random-secret"},
                                environ_base={"REMOTE_ADDR": "10.7.7.1"}).status_code, 302)
        for msg in ("set up everything", "set up the dongles", "remove dongle 2", "forget dongle 2", "run the auto setup",
                    "/setup", "/patrol stop", "clear the logs"):
            self.assertEqual(c.post("/api/chat_send", json={"message": msg}, headers=H).status_code, 403, msg)
        self.assertEqual(c.post("/api/devices/mark", json={"key": "x", "known": True}, headers=H).status_code, 403)
        # what reaches the console from this account carries the role marker (the console refuses the rest there)
        sent = []
        b = bridge.ArisAIBridge.__new__(bridge.ArisAIBridge)
        b.messages, b.seq, b.gen, b.busy, b.status_line = [], 0, 0, False, "Ready"
        b.state_lock, b.proc, b.last_notice = threading.Lock(), None, None
        b.start_process = lambda: (_ for _ in ()).throw(AssertionError("the console must not be started"))
        real = threading.Thread
        class T(real):
            def __init__(self, target=None, args=(), **kw):
                sent.append(args)
                super().__init__(target=lambda: None, **kw)
        threading.Thread = T
        try:
            self.assertTrue(b.ask_async("calibrate the gain on dongle 1", role="viewer"))
            b.busy = False                                   # (the fake thread never ran the worker)
            self.assertTrue(b.ask_async("what is on 92.1", role="admin"))
        finally:
            threading.Thread = real
            b.busy = False
        self.assertEqual([x[2] for x in sent], ["viewer", "admin"])
        # the admin's old session (no role in the cookie) is still the admin's
        a = client()
        with a.session_transaction() as sess:
            sess.pop("role", None)
        self.assertNotEqual(a.post("/api/devices/mark", json={"key": "no-such-device", "known": True}, headers=H).status_code, 403)
        with a.session_transaction() as sess:
            self.assertEqual(sess.get("role"), "admin")

    def test_add_user_takes_only_a_hash(self):
        import io
        for name, line in (("viewer", "plain-password\n"), ("admin", generate_password_hash("x" * 12) + "\n"),
                           ("bad name", generate_password_hash("x" * 12) + "\n")):
            with self.assertRaises(SystemExit):
                self.auth.cmd_add_user(name, io.StringIO(line))
        self.assertNotIn("extra_users", self.auth.load_secrets())


class Spectrum(unittest.TestCase):
    def test_every_preset_is_listed_and_says_whether_a_receiver_here_tunes_it(self):
        c = client()
        m = c.get("/api/spectrum/meta").get_json()
        by = {p["id"]: p for p in m["presets"]}
        self.assertEqual(m["presets"][0]["id"], "full")
        self.assertEqual((by["full"]["start"], by["full"]["stop"]), (0.0, 6000.0))
        self.assertEqual((m["view_min_mhz"], m["view_max_mhz"]), (0.0, 6000.0))
        self.assertIn(25.0, m["steps_khz"])
        self.assertTrue(by["gnss"]["tuned"])                    # 1575 MHz: an RTL-SDR tunes it
        self.assertFalse(by["ism24"]["tuned"])                  # 2.4 GHz: none of the simulated dongles does,
        self.assertFalse(by["ism24"]["tuned_part"])             # ... and the preset is listed all the same
        self.assertEqual((by["phones2"]["tuned"], by["phones2"]["tuned_part"]), (False, True))    # 1710-1910
        self.assertEqual((by["full"]["tuned"], by["full"]["tuned_part"]), (False, True))
        self.assertEqual([d["range_mhz"] for d in m["dongles"]], [[24.0, 1766.0]] * 3)
        os.environ["ARIS_SA_SIM"] = "1"                          # a simulated SA124B plugged in
        from arisweb.engine import engine as _eng
        eng = _eng()
        try:
            eng._load_plugins()
            eng._sync_found_dongles(eng._list_devices())
            m = c.get("/api/spectrum/meta").get_json()
            by = {p["id"]: p for p in m["presets"]}
            self.assertTrue(by["ism24"]["tuned"])
            self.assertEqual(m["dongles"][-1]["range_mhz"], [0.1, 12400.0])
            from arisweb import spectrum as S
            filled, real_fill = [], S._fill_hop_spikes
            S._fill_hop_spikes = lambda *a, **k: (filled.append(a[3]), real_fill(*a, **k))[1]
            try:
                j = c.get("/api/spectrum?dongle=all&start=2400&stop=2483.5").get_json()
            finally:
                S._fill_hop_spikes = real_fill
            self.assertEqual(filled, [])                         # a receiver with its own sweep: no spike filter
            self.assertEqual([t["dongle"] for t in j["traces"]], ["sa-19184001"])     # only the one that tunes it
            self.assertEqual(j["unit"], "dBm")
            self.assertEqual(j["gaps"], [])
            self.assertEqual([(x["name"], x["why"]) for x in j["left_out"]],
                             [("Dongle 1", "range"), ("Dongle 2", "range"), ("Dongle 3", "range")])
            self.assertTrue(any(abs(p["mhz"] - 2437.0) < 0.5 for p in j["peaks"]), j["peaks"])
            # nothing was filled in or left in as a spike, and the points of its carrier are one entry in the list
            self.assertEqual((j["spikes_removed"], j["spikes_kept"]), (0, False))
            self.assertEqual(len([p for p in j["peaks"] if abs(p["mhz"] - 2437.0) < 1.0]), 1, j["peaks"])
            # a view that crosses the top of the dongles' range: each receiver shows its own part and no more
            j = c.get("/api/spectrum?dongle=all&start=1700&stop=1900").get_json()
            rng = {t["dongle"]: t["range_mhz"] for t in j["traces"]}
            self.assertEqual(rng["00001001"], [1700.0, 1766.0])
            self.assertEqual(rng["sa-19184001"], [1700.0, 1900.0])
            for t in j["traces"]:
                self.assertLessEqual(max(t["freqs"]), t["range_mhz"][1] + 1e-6, t["name"])
            self.assertEqual((j["gaps"], j["warnings"]), ([], []))    # a signal outside a range is no antenna fault
            # a narrow view: this receiver streams too slowly for the direct capture, so it sweeps it instead ...
            j = c.get("/api/spectrum?dongle=all&start=2436.75&stop=2437.25").get_json()
            self.assertEqual((j["mode"], [t["dongle"] for t in j["traces"]]), ("sweep", ["sa-19184001"]))
            self.assertTrue(any(abs(p["mhz"] - 2437.0) < 0.05 for p in j["peaks"]), j["peaks"])
            # ... and where the dongles can make it, it is left out and the answer says why
            j = c.get("/api/spectrum?dongle=all&start=433.52&stop=434.32").get_json()
            self.assertEqual((j["mode"], len(j["traces"])), ("live", 3))
            self.assertEqual([(x["name"], x["why"]) for x in j["left_out"]], [("Dongle 4", "live")])
            pl = c.get("/api/spectrum/plan?dongle=all&start=0&stop=6000").get_json()
            self.assertEqual((pl["covered"], pl["gaps"]), ([[0.1, 6000.0]], [[0.0, 0.1]]))
            self.assertEqual(pl["gap_note"], "No receiver here tunes 0-0.1 MHz.")
        finally:
            os.environ.pop("ARIS_SA_SIM", None)                  # unplugged, then forgotten
            eng.TOOLS["forget_dongle"](dongle="sa-19184001")
        self.assertNotIn("sa-19184001", eng.DONGLE_NAMES)

    def test_a_view_no_receiver_tunes_opens_no_radio_and_shows_no_level(self):
        c = client()
        from arisweb.engine import engine as _eng
        eng = _eng()
        opened, real = [], eng._use_dongle
        eng._use_dongle = lambda *a, **k: (opened.append(a), real(*a, **k))[1]
        try:
            r = c.get("/api/spectrum?dongle=all&start=2400&stop=2500")
            j = r.get_json()
            self.assertEqual(r.status_code, 200)
            self.assertEqual((j["mode"], j["traces"], j["freqs"], j["db"], j["peaks"]), ("none", [], [], [], []))
            self.assertEqual((j["gaps"], j["covered"]), ([[2400.0, 2500.0]], []))
            self.assertEqual(j["gap_note"], "No receiver here tunes 2400-2500 MHz.")
            self.assertEqual([x["name"] for x in j["left_out"]], ["Dongle 1", "Dongle 2", "Dongle 3"])
            self.assertIsNone(j["floor_db"])
            j = c.get("/api/spectrum?dongle=2&start=5725&stop=5875").get_json()
            self.assertEqual(j["mode"], "none")
            self.assertEqual(j["gap_note"], "Dongle 2 tunes 24-1766 MHz, so there is nothing to show here.")
            self.assertEqual(opened, [])                         # nothing was opened for either
        finally:
            eng._use_dongle = real

    def test_any_range_from_0_to_6000_is_swept_where_it_is_tuned(self):
        c = client()
        from arisweb.engine import engine as _eng
        eng = _eng()
        pl = c.get("/api/spectrum/plan?dongle=all&start=0&stop=6000").get_json()
        self.assertEqual((pl["mode"], pl["covered"], pl["step_khz"]), ("sweep", [[24.0, 1766.0]], 200.0))
        self.assertEqual(pl["gap_note"], "No receiver here tunes 0-24 MHz or 1766-6000 MHz.")
        self.assertTrue(8 <= len(pl["chunks"]) <= 16, len(pl["chunks"]))
        one = c.get("/api/spectrum/plan?dongle=1&start=0&stop=6000").get_json()
        self.assertEqual(one["gap_note"], "Dongle 1 tunes 24-1766 MHz, so there is nothing to show outside that.")
        from arisweb import spectrum as S
        asked, real = [], eng._rtl_power
        refs, real_ref = [], S._sweep_fullscale_db

        def spy(s, e, *a, **k):
            asked.append((s, e))
            return real(s, e, *a, **k)

        def spy_ref(eng_, s, e, step):
            refs.append((s, e, step))
            return real_ref(eng_, s, e, step)
        eng._rtl_power, S._sweep_fullscale_db = spy, spy_ref
        try:
            j = c.get("/api/spectrum?dongle=1&start=0&stop=6000").get_json()       # one request, one whole answer
            self.assertEqual(refs, [(s, e, 200.0) for s, e in asked])             # each piece's own full-scale level
            self.assertEqual((j["mode"], j["step_khz"], len(j["traces"])), ("sweep", 200.0, 1))
            t = j["traces"][0]
            self.assertEqual(t["range_mhz"], [24.0, 1766.0])
            self.assertGreaterEqual(min(t["freqs"]), 24.0)
            self.assertLessEqual(max(t["freqs"]), 1766.0)
            self.assertLess(min(t["freqs"]), 30.0)
            self.assertGreater(max(t["freqs"]), 1760.0)
            self.assertEqual(t["freqs"], sorted(set(t["freqs"])))               # joined pieces: no point twice
            self.assertTrue(400 < len(t["freqs"]) <= 1600, len(t["freqs"]))
            self.assertEqual(j["gaps"], [[0.0, 24.0], [1766.0, 6000.0]])
            self.assertEqual(j["gap_note"], one["gap_note"])
            self.assertEqual(len(asked), 9)                                       # in pieces the engine accepts
            self.assertTrue(all(e - s <= eng.MAX_SPAN_MHZ + 1e-6 and 24.0 <= s < e <= 1766.0 for s, e in asked))
            self.assertTrue(any(abs(p["mhz"] - 96.9) < 0.3 for p in j["peaks"]), j["peaks"])   # an FM station
            # one piece of the plan, as the page asks for them: the plan's step, its share of the points
            ch = pl["chunks"][2]
            del asked[:]
            j = c.get("/api/spectrum?dongle=1&start=%s&stop=%s&step_khz=%s&points=%d"
                      % (ch["start"], ch["stop"], pl["step_khz"], ch["points"])).get_json()
            self.assertEqual(asked, [(ch["start"], ch["stop"])])
            self.assertLessEqual(len(j["traces"][0]["freqs"]), ch["points"])
            self.assertEqual(j["step_khz"], 200.0)
            # a view that reaches below the tuner: only the tuned part is asked for
            del asked[:]
            j = c.get("/api/spectrum?dongle=1&start=0&stop=60").get_json()
            self.assertEqual(asked, [(24.0, 60.0)])
            self.assertEqual((j["gaps"], j["traces"][0]["range_mhz"]), ([[0.0, 24.0]], [24.0, 60.0]))
        finally:
            eng._rtl_power, S._sweep_fullscale_db = real, real_ref

    def test_a_receiver_that_is_not_plugged_in_is_left_out_of_all(self):
        c = client()
        from arisweb.engine import engine as _eng
        eng = _eng()
        eng._set_dongle("00004004", present=False)
        try:
            j = c.get("/api/spectrum?dongle=all&start=433&stop=435").get_json()
            self.assertEqual([t["dongle"] for t in j["traces"]], ["00001001", "00003003"])
            m = c.get("/api/spectrum/meta").get_json()
            self.assertEqual([d["present"] for d in m["dongles"]], [True, True, False])
        finally:
            eng._set_dongle("00004004", present=True)
        self.assertEqual(len(c.get("/api/spectrum?dongle=all&start=433&stop=435").get_json()["traces"]), 3)

    def test_a_receiver_name_nobody_has_is_refused(self):
        c = client()
        for url in ("/api/spectrum", "/api/spectrum/plan"):
            r = c.get(url + "?dongle=nope&start=88&stop=108")
            self.assertEqual((r.status_code, r.get_json()["error"]), (400, "No receiver here has that name."), url)
            self.assertEqual(c.get(url + "?dongle=dongle%202&start=88&stop=108").status_code, 200, url)

    def test_a_coarse_sweep_keeps_signals_near_the_hop_centres(self):
        """A wide view has a hop's spike core cover a large part of the hop: only the centre point standing a little
        over its surroundings is filled in there, and above the widest automatic step nothing is."""
        import numpy as np
        from arisweb import spectrum as S
        from arisweb.engine import engine as _eng
        eng = _eng()
        freqs = [round(100.0 + 0.2 * i, 4) for i in range(41)]                     # 100-108 MHz, a point every 200 kHz
        hop, guard = [104.0], eng._guard_mhz(200.0)

        def sweep(**levels):                     # a flat floor at -46 with these points raised (MHz -> dB)
            return [levels.get("f%d" % round(f * 10), -46.0) for f in freqs]

        def at(db, mhz):
            return db[freqs.index(mhz)]
        spike = sweep(f1038=-41.2, f1040=-36.0, f1042=-41.2)                       # the receiver's own spike
        db, n = S._fill_hop_spikes(freqs, spike, hop, 200.0, guard, coarse=True)
        self.assertEqual((n, at(db, 104.0), at(db, 103.8)), (1, -46.0, -46.0))
        strong = sweep(f1040=-26.0)                                                # 20 dB on the centre point: a signal
        db, n = S._fill_hop_spikes(freqs, strong, hop, 200.0, guard, coarse=True)
        self.assertEqual((n, db), (0, strong))
        beside = sweep(f1040=-36.0, f1042=-34.0)                                   # the strongest point is beside it
        db, n = S._fill_hop_spikes(freqs, beside, hop, 200.0, guard, coarse=True)
        self.assertEqual((n, db), (0, beside))
        db, n = S._fill_hop_spikes(freqs, beside, hop, 200.0, guard)               # the fine-step rule is as it was
        self.assertEqual((n, at(db, 104.2)), (1, -46.0))
        # the first preset: every simulated station is in the list (105.3 sits beside a hop centre of that sweep)
        c = client()
        j = c.get("/api/spectrum?dongle=1&start=0&stop=6000").get_json()
        for mhz in (88.9, 96.9, 101.7, 105.3):
            self.assertTrue(any(abs(p["mhz"] - mhz) <= 0.25 for p in j["peaks"]), (mhz, j["peaks"]))
        self.assertFalse(j["spikes_kept"])
        # a step of 1000 kHz is one point per hop: every point is shown as it was measured
        env = os.environ.get("FAKE_PEAKS")
        os.environ["FAKE_PEAKS"] = "150:25:300"
        asked, real = [], eng._rtl_power

        def spy(*a, **k):
            asked.append(real(*a, **k))
            return asked[-1]
        eng._rtl_power = spy
        try:
            j = c.get("/api/spectrum?dongle=1&start=140&stop=170&step_khz=1000&gain=20").get_json()
        finally:
            eng._rtl_power = real
            os.environ["FAKE_PEAKS"] = env
        self.assertEqual((j["step_khz"], j["spikes_removed"], j["spikes_kept"]), (1000.0, 0, True))
        raw = sorted((round(hz / 1e6, 4), sum(v) / len(v)) for hz, v in asked[0][0].items())
        raw = [(f, v) for f, v in raw if 140.0 <= f <= 170.0]
        ref = S._sweep_fullscale_db(eng, 140.0, 170.0, 1000.0)
        t = j["traces"][0]
        self.assertEqual(t["freqs"], [f for f, _ in raw])
        self.assertEqual(t["db"], [round(v - ref, 1) for _, v in raw])             # nothing filled in, nothing moved
        self.assertGreater(max(t["db"]) - float(np.median(t["db"])), 4.0)          # the carrier is in it

    def test_fm_stations_a_few_coarse_points_apart_are_told_apart(self):
        from arisweb import spectrum as S
        from arisweb.engine import engine as _eng
        eng = _eng()
        self.assertEqual(S._valleys([0, 10, 3, 10, 9, 2, 1, 8]), [2, 6])           # the lowest point of each clear dip
        self.assertEqual(S._valleys([0, 10, 8, 10, 0]), [])                        # a 2 dB dip parts nothing
        self.assertEqual(S._valleys([]), [])
        freqs = [round(95.0 + 0.2 * i, 4) for i in range(21)]                      # 95-99 MHz, a point every 200 kHz

        def found(**levels):
            db = [levels.get("f%d" % round(f * 10), -46.0) for f in freqs]
            return sorted(p["frequency_mhz"] for p in S._signals(eng, freqs, db, 200.0, -46.0, None))
        self.assertEqual(found(f960=-30.0, f962=-38.0, f964=-30.0), [96.0, 96.4])  # two stations, a dip between them
        self.assertEqual(len(found(f960=-30.0, f962=-32.0, f964=-30.0)), 1)        # no clear dip: one entry
        # a receiver's own spike beside a station is no second station
        db = [{96.0: -30.0, 96.2: -40.0, 96.4: -36.0, 96.6: -41.0}.get(f, -46.0) for f in freqs]
        self.assertEqual(len(S._signals(eng, freqs, db, 200.0, -46.0, [96.4])), 1)
        self.assertEqual(len(S._signals(eng, freqs, db, 200.0, -46.0, None)), 2)   # (no spike there: two signals)
        # outside the FM band the points of one signal still join step by step
        wide = [round(400.0 + 0.2 * i, 4) for i in range(21)]
        db = [{402.0: -30.0, 402.2: -38.0, 402.4: -30.0}.get(f, -46.0) for f in wide]
        self.assertEqual(len(S._signals(eng, wide, db, 200.0, -46.0, None)), 1)

    def test_step_and_points_are_checked(self):
        c = client()
        j = c.get("/api/spectrum?dongle=1&start=433&stop=435&step_khz=10").get_json()
        self.assertEqual((j["step_khz"], j["step_auto"], j["step_raised"]), (10.0, False, False))
        self.assertTrue(150 < len(j["traces"][0]["freqs"]) <= 260, len(j["traces"][0]["freqs"]))
        pl = c.get("/api/spectrum/plan?dongle=1&start=24&stop=1766&rbw=1").get_json()
        self.assertTrue(pl["step_raised"])
        self.assertEqual(pl["step_khz"], 25.0)
        self.assertIn("raised from 1 to 25 kHz", pl["step_note"])
        for q in ("start=abc&stop=100", "start=100", "start=200&stop=100", "start=-5&stop=100",
                  "start=100&stop=6001", "start=100&stop=100.001", "start=88&stop=108&step_khz=0.01",
                  "start=88&stop=108&step_khz=wide", "start=88&stop=108&points=999999",
                  "start=88&stop=108&points=many", "start=2400&stop=2500&step_khz=99999"):
            for url in ("/api/spectrum?dongle=1&", "/api/spectrum/plan?dongle=1&"):
                r = c.get(url + q)
                self.assertEqual(r.status_code, 400, (url, q))
                self.assertNotIn("Traceback", r.get_json()["error"])
                self.assertNotRegex(r.get_json()["error"], r"[a-z]_[a-z]", (url, q))     # plain words

    def test_wide_sweep_does_not_call_fm_off_plan(self):
        c = client()
        j = c.get("/api/spectrum?dongle=all&start=30&stop=200").get_json()
        self.assertFalse(j["fm_plan_checked"])
        self.assertFalse(any(p["flag"] for p in j["peaks"]), j["peaks"])
        self.assertEqual(len(j["traces"]), 3)
        d3 = [t for t in j["traces"] if t["dongle"] == "00004004"][0]
        self.assertEqual(d3["health"], "flat")                   # dongle 3 cannot hear FM in this simulation
        self.assertTrue(any("Dongle 3" in w for w in j["warnings"]))

    def test_levels_are_dbfs_at_or_below_zero(self):
        """The plot's levels are true dBFS (v4.8.9 showed stations at +10 to +15 dB on the live station): a sweep and
        a live IQ frame both sit at or below 0, say so, and still report floors and signals above the noise."""
        c = client()
        for q in ("dongle=all&start=88&stop=108", "dongle=1&start=433.52&stop=434.32"):
            j = c.get("/api/spectrum?" + q).get_json()
            self.assertEqual(j["unit"], "dBFS", q)
            self.assertLessEqual(max(j["db"]), 0.0, q)
            for t in j["traces"]:
                if t.get("db"):
                    self.assertLessEqual(max(t["db"]), 0.0, (q, t["name"]))
                    self.assertLess(t["floor_db"], max(t["db"]))
                    self.assertLess(t["floor_db"], -20.0)          # a dongle's noise is far below full scale
                    if j["mode"] == "sweep":                       # the simulated floor: -46 dBFS (-26 unconverted)
                        self.assertLess(t["floor_db"], -40.0, (q, t["name"]))
            if j["mode"] == "sweep":
                self.assertTrue(j["peaks"], q)
        self.assertEqual(j["mode"], "live")

    def test_fm_preset_checks_plan(self):
        c = client()
        j = c.get("/api/spectrum?dongle=1&start=88&stop=108").get_json()
        self.assertTrue(j["fm_plan_checked"])
        for p in j["peaks"]:                     # each entry says how far its signal reaches
            self.assertTrue(p["lo_mhz"] <= p["hi_mhz"] and abs(p["mhz"] - p["lo_mhz"]) < 0.5, p)
        self.assertIn(96.9, [p["mhz"] for p in j["peaks"]])
        self.assertFalse(any(p["flag"] for p in j["peaks"] if p["mhz"] in (88.9, 96.9, 101.7, 105.3)))


class MapFlow(unittest.TestCase):
    def test_place_measure_calibrate(self):
        c = client()
        st = c.get("/api/map/state").get_json()
        st["nodes"] = [n for n in st["nodes"] if n.get("serial") in harness.NODE_POS]   # other tests add nodes too
        self.assertEqual(len(st["nodes"]), 3)
        r = c.post("/api/map/plan", json={"name": "Flat", "width_m": 12}, headers=H).get_json()
        pid = r["id"]
        c.post(f"/api/map/plan/{pid}", json={"height_m": 9}, headers=H)
        for n in st["nodes"]:
            x, y = harness.NODE_POS[n["serial"]]
            self.assertEqual(c.post(f"/api/map/node/{n['id']}", json={"x": x, "y": y}, headers=H).status_code, 200)
        m = c.post("/api/map/measure", json={"freq_mhz": 433.92, "dwell_s": 3, "new_session": True}, headers=H)
        self.assertEqual(m.status_code, 200, m.get_data(as_text=True))
        m = m.get_json()
        self.assertTrue(m["heat"]["ok"], m)
        self.assertEqual(m["heat"]["heard_points"], 3)
        self.assertEqual(len(m["heat"]["grid"]["values"]), m["heat"]["grid"]["ny"])
        self.assertTrue(any("not calibrated" in n for n in m["notes"]))
        sid = m["session"]["id"]
        # survey point next to the transmitter, with dongle 1 carried there
        import os
        os.environ["FAKE_NODE_POS"] = "00001001:7:2.5;00003003:9:1;00004004:5:7"
        s = c.post("/api/map/measure", json={"freq_mhz": 433.92, "dwell_s": 3, "session_id": sid, "survey": True,
                                             "node_id": "dongle-00001001", "x": 7, "y": 2.5, "mode": "strength"},
                   headers=H).get_json()
        self.assertEqual(s["mode"], "strength")
        self.assertTrue(any(p["kind"] == "survey" for p in s["points"]))
        # calibration with every antenna in the same spot
        os.environ["FAKE_NODE_POS"] = "00001001:6:2;00003003:6:2;00004004:6:2"
        cal = c.post("/api/map/calibrate", json={"freq_mhz": 433.92, "dwell_s": 3}, headers=H).get_json()
        self.assertEqual(len(cal["offsets_db"]), 3, cal)
        self.assertAlmostEqual(cal["offsets_db"]["Dongle 2"] - cal["offsets_db"]["Dongle 1"], 6.0, delta=1.5)
        os.environ["FAKE_NODE_POS"] = "00001001:1:1;00003003:9:1;00004004:5:7"
        m2 = c.post("/api/map/measure", json={"freq_mhz": 433.92, "dwell_s": 3, "new_session": True},
                    headers=H).get_json()
        b = m2["heat"]["best"]
        self.assertLess(((b["x"] - 7) ** 2 + (b["y"] - 2) ** 2) ** 0.5, 2.5, m2["summary"])
        self.assertFalse(any("not calibrated" in n for n in m2["notes"]))
        v = c.get(f"/api/map/session/{m2['session']['id']}?mode=source").get_json()
        self.assertEqual(v["session"]["id"], m2["session"]["id"])

    def test_errors_are_messages(self):
        c = client()
        r = c.post("/api/map/measure", json={"freq_mhz": 5000}, headers=H)
        self.assertEqual(r.status_code, 400)
        self.assertIn("between", r.get_json()["error"])
        r = c.post("/api/map/remote", json={"name": "x", "url": "nope", "token": "t" * 20}, headers=H)
        self.assertEqual(r.status_code, 400)


class DecodeAndCapture(unittest.TestCase):
    def _wait(self, c, job, limit=90):
        t0 = time.time()
        while time.time() - t0 < limit:
            j = c.get(f"/api/decode/{job}").get_json()
            if j["state"] != "running":
                return j
            time.sleep(0.5)
        self.fail("decode job did not finish")

    def test_decode_button_job(self):
        c = client()
        self.assertEqual(c.post("/api/decode", json={"freq_mhz": 96.9, "seconds": 5}).status_code, 403)  # header
        r = c.post("/api/decode", json={"freq_mhz": 96.9, "seconds": 5}, headers=H)
        self.assertEqual(r.status_code, 200, r.get_json())
        job = r.get_json()["job"]
        busy = c.post("/api/decode", json={"freq_mhz": 433.92, "seconds": 5}, headers=H)
        self.assertEqual(busy.status_code, 409)                  # one decode at a time
        j = self._wait(c, job)
        self.assertEqual(j["state"], "done", j)
        card = j["card"]
        self.assertEqual(card["evidence"], "confirmed_decode", card)
        self.assertIn("HITS FM", card["verdict"])
        self.assertEqual(card["listened_seconds"], 5)            # the time picked, not 3 s
        self.assertTrue(card["dongles"])
        bad = c.post("/api/decode", json={"freq_mhz": 5000, "seconds": 5}, headers=H)
        self.assertEqual(bad.status_code, 400)

    def test_capture_api_and_activity(self):
        c = client()
        r = c.post("/api/capture/start", json={"freq_mhz": 96.9}, headers=H)
        self.assertEqual(r.status_code, 200, r.get_json())
        t0 = time.time()
        while time.time() - t0 < 60:
            a = c.get("/api/activity").get_json()
            if any(x["freq_mhz"] == 96.9 for x in a.get("captures") or []):
                break
            time.sleep(0.5)
        cap = next(x for x in a["captures"] if x["freq_mhz"] == 96.9)
        self.assertEqual(cap["outcome"], "identified", cap)
        self.assertTrue(c.get("/api/activity?brief=1").get_json()["captures"])
        r = c.post("/api/capture/start", json={"freq_mhz": 433.92, "mode": "continuous"}, headers=H)
        self.assertEqual(r.status_code, 200, r.get_json())
        time.sleep(1)
        run = c.get("/api/activity").get_json()["watchdog"]["watchdogs_running"]
        self.assertTrue(run)
        r = c.post("/api/capture/stop", json={"serial": run[0]["serial"]}, headers=H)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertFalse(c.get("/api/activity").get_json()["watchdog"]["watchdogs_running"])

    def test_reset_scanner_stops_everything(self):
        # Name all and a capture running: Reset Scanner stops both, ends their radio programs, frees the dongles and
        # says so; the job no longer looks "running"
        from arisweb import jobs
        eng = __import__("arisweb.engine", fromlist=["x"]).engine()
        c = client()
        # the capture first: its 3 s comparison holds every dongle, and a Name all started during it could end
        # at once with its stations "not checked" (the test then raced, and failed on a test station)
        r = c.post("/api/capture/start", json={"freq_mhz": 433.92, "mode": "continuous"}, headers=H)
        self.assertEqual(r.status_code, 200, r.get_json())
        fr = [88.9, 89.7, 90.5, 92.1, 93.7, 95.3, 96.9, 98.5, 100.1, 101.7, 103.3, 105.3, 106.9]
        job = c.post("/api/fm_names", json={"freqs": fr}, headers=H).get_json()["job"]
        time.sleep(1)
        self.assertEqual(c.get(f"/api/decode/{job}").get_json()["state"], "running")
        jobs.LAST_RESET["message"] = None
        t0 = time.time()
        self.assertEqual(c.post("/api/chat_reset", headers=H).status_code, 200)
        while jobs.LAST_RESET["message"] is None and time.time() - t0 < 30:
            time.sleep(0.2)
        msg = jobs.LAST_RESET["message"]
        self.assertLess(time.time() - t0, 25)
        self.assertIn("Stopped Name all FM stations and the capture on 433.92 MHz", msg)
        self.assertIn("are free", msg)
        self.assertNotIn("stuck", msg)
        # v4.8.5 note: only the dongles that are plugged in are called free
        os.environ["FAKE_MISSING"] = "00004004"
        try:
            jobs.LAST_RESET["message"] = None
            c.post("/api/chat_reset", headers=H)
            t0 = time.time()
            while jobs.LAST_RESET["message"] is None and time.time() - t0 < 30:
                time.sleep(0.2)
            msg2 = jobs.LAST_RESET["message"]
        finally:
            os.environ.pop("FAKE_MISSING")
        self.assertIn("Dongle 1 and Dongle 2 are free. Dongle 3 is not plugged in.", msg2)
        j = c.get(f"/api/decode/{job}").get_json()
        self.assertEqual((j["state"], j["error"]), ("error", "Stopped by Reset Scanner."))
        self.assertFalse(c.get("/api/activity").get_json()["watchdog"]["watchdogs_running"])
        self.assertEqual(eng._radio_children(), [])
        self.assertFalse(jobs.RESTART["needed"])
        r = c.post("/api/fm_names", json={"freqs": [96.9]}, headers=H)       # and new work starts normally
        self.assertEqual(self._wait(c, r.get_json()["job"])["state"], "done")

    def test_clear_data(self):
        from arisweb import jobs
        eng = __import__("arisweb.engine", fromlist=["x"]).engine()
        c = client()
        eng._remember_station(96.9, "HITS FM", "0x5969", "00001001", carrier_mhz=96.9)
        eng._log_append({"type": "burst", "t": time.time(), "aris_freq_mhz": 433.92})
        eng._notice("a test notice")
        eng.BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        (eng.BASELINE_DIR / "test_baseline.json").write_text("{}")
        cfg_before = eng.CONFIG_PATH.read_text() if eng.CONFIG_PATH.exists() else None
        r = c.post("/api/clear_data", json={"baselines": False}, headers=H).get_json()
        msg = r.get("message") or jobs.LAST_RESET["message"]
        self.assertIn("All data cleared", msg)
        self.assertRegex(msg, r"\d+ FM station names?")
        self.assertIn("Kept: settings, logins, the Map's antennas and floorplans and the baselines", msg)
        self.assertFalse(eng.FM_STATIONS.exists())
        self.assertFalse(eng.WATCHDOG_LOG.exists())
        self.assertEqual(c.get("/api/fm_known").get_json()["named"], {})
        self.assertEqual(c.get("/api/signals").get_json().get("signals", []), [])
        self.assertTrue((eng.BASELINE_DIR / "test_baseline.json").exists())     # kept unless asked
        if cfg_before is not None:
            self.assertEqual(eng.CONFIG_PATH.read_text(), cfg_before)             # settings untouched
        r = c.post("/api/clear_data", json={"baselines": True}, headers=H).get_json()
        self.assertFalse((eng.BASELINE_DIR / "test_baseline.json").exists())

    def test_names_read_elsewhere_reach_the_list(self):
        # a capture (or the chat) reads a station's name after the last sweep: the page picks it up from /api/fm_known
        eng = __import__("arisweb.engine", fromlist=["x"]).engine()
        c = client()
        k = c.get("/api/fm_known").get_json()
        self.assertEqual(c.get("/api/fm_known?have=" + k["tag"]).get_json().get("unchanged"), True)
        eng._remember_station(101.7, "TOP 40", "0x91E3", "00003003", carrier_mhz=101.7001)
        eng._remember_station(88.9, "Escucha y en vivo", "0x0889", "00001001", carrier_mhz=88.9, scrolls=True)
        k2 = c.get("/api/fm_known?have=" + k["tag"]).get_json()
        self.assertEqual(k2["named"]["101.7"]["name"], "TOP 40")
        self.assertTrue(k2["named"]["88.9"]["scrolls"])
        fr = c.get("/api/spectrum?dongle=00001001&start=88&stop=108").get_json()
        pk = {round(p["mhz"], 1): p for p in fr["peaks"]}
        self.assertEqual(pk[88.9]["band"], 'FM station, RDS scrolling text "Escucha y en vivo"')

    def test_name_all_fm_stations_job(self):
        c = client()
        r = c.post("/api/fm_names", json={"freqs": [96.9, 105.3]}, headers=H)
        self.assertEqual(r.status_code, 200, r.get_json())
        j = self._wait(c, r.get_json()["job"])
        self.assertEqual(j["state"], "done", j)
        self.assertEqual(j["names"]["named"]["96.9"]["name"], "HITS FM")
        fr = c.get("/api/spectrum?dongle=00001001&start=88&stop=108").get_json()
        named = {p["mhz"]: p.get("name") for p in fr["peaks"]}
        self.assertEqual(named.get(96.9), "HITS FM")              # the list now names it, not orange
        self.assertFalse(next(p for p in fr["peaks"] if p["mhz"] == 96.9)["flag"])

    def test_not_heard_stations_are_remembered(self):
        """live v4.8.9: after Name all the result box said 'Not heard in the check: 94.9, ...', but the rows
        still said only 'FM band, station channel' and the button counted them again. A not_heard or too_weak
        result is remembered with its time, shown on the row and in /api/fm_known, and a later reading replaces it."""
        import os
        env = {k: os.environ.get(k) for k in ("FAKE_IQ", "FAKE_PEAKS")}
        os.environ.update(FAKE_PEAKS=env["FAKE_PEAKS"] + ",99.7:22:150")          # the sweep sees it, the IQ look
        eng = __import__("arisweb.engine", fromlist=["x"]).engine()                # hears nothing there
        try:
            c = client()
            r = c.post("/api/fm_names", json={"freqs": [99.7]}, headers=H)
            j = self._wait(c, r.get_json()["job"])
            self.assertEqual(j["state"], "done", j)
            st = {x["channel_mhz"]: x for x in j["names"]["not_named"]}
            self.assertIn(st[99.7]["state"], ("not_heard", "too_weak"), st)
            state = st[99.7]["state"]
            k = c.get("/api/fm_known").get_json()
            row = next(x for x in k["checked"] if x["channel_mhz"] == 99.7)
            self.assertEqual(row["state"], state)
            self.assertTrue(row["band"].startswith("Checked: " + ("not heard in the last check" if state == "not_heard"
                                                                  else "too weak here to read in the last check") + " ("), row)
            self.assertGreater(row["epoch"], time.time() - 60)
            fr = c.get("/api/spectrum?dongle=00001001&start=88&stop=108").get_json()
            pk = {round(p["mhz"], 1): p for p in fr["peaks"]}
            self.assertEqual((pk[99.7]["checked"], pk[99.7]["flag"]), (state, False))
            self.assertRegex(pk[99.7]["band"], r"^Station channel, checked: (not heard|too weak here to read) in the last check \(20\d\d-")
            # a reading later (a station heard on the channel) replaces the note; a named station is never touched
            eng._remember_station(99.7, None, None, "00001001", carrier_mhz=99.7002, on_channel=True, rds=False)
            self.assertIsNone(eng._unheard_state(eng._known_stations()["99.7"]))
            eng._remember_station(96.9, "HITS FM", "0x53C9", "00001001", carrier_mhz=96.9, on_channel=True, rds=True)
            self.assertEqual(eng._remember_unheard({96.9: {"state": "not_heard", "serial": "00001001"}}), 0)
            self.assertEqual(eng._known_stations()["96.9"].get("name"), "HITS FM")
        finally:
            for kk, v in env.items():
                if v is None:
                    os.environ.pop(kk, None)
                else:
                    os.environ[kk] = v

    def test_checked_stations_in_the_spectrum_list(self):
        import os
        import tempfile
        env = {k: os.environ.get(k) for k in ("FAKE_IQ", "FAKE_PEAKS", "FAKE_ARGLOG")}
        log = tempfile.mktemp(prefix="args_", dir=harness.DATA)
        os.environ.update(FAKE_IQ=env["FAKE_IQ"] + ",93.7:wbfm_nords:28,94.8:wbfm_mono:20",
                          FAKE_PEAKS=env["FAKE_PEAKS"] + ",93.7:30:150,94.8:26:150", FAKE_ARGLOG=log)
        try:
            c = client()
            r = c.post("/api/fm_names", json={"freqs": [93.7, 94.8], "best": {"93.7": "Dongle 1"}}, headers=H)
            j = self._wait(c, r.get_json()["job"])
            self.assertEqual(j["state"], "done", j)
            st = {x["channel_mhz"]: x for x in j["names"]["not_named"]}
            self.assertEqual(st[93.7]["band"], "Station channel: no RDS name read in the last check")
            self.assertEqual(st[94.8]["state"], "off_channel")
            fr = c.get("/api/spectrum?dongle=00001001&start=88&stop=108").get_json()
            pk = {round(p["mhz"], 1): p for p in fr["peaks"]}
            self.assertEqual((pk[93.7]["checked"], pk[93.7]["flag"]), ("no_name", False))    # grey, not orange
            self.assertEqual((pk[94.8]["checked"], pk[94.8]["flag"]), ("off_channel", True))
            self.assertEqual(pk[94.8]["label"], "94.800 off-plan")
            c.get("/api/spectrum?dongle=00001001&start=433&stop=435")
            with open(log) as f:
                runs = [json.loads(x) for x in f if '"rtl_power"' in x]
            integ = {r_[r_.index("-f") + 1].split(":")[0]: r_[r_.index("-i") + 1] for r_ in runs}
            self.assertEqual(integ.get("88.0000M"), "2")       # FM view: 1-2 s, several rounds per hop
            self.assertEqual(integ.get("433.0000M"), "1")
        finally:
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_internal_endpoint_needs_token(self):
        from arisweb import jobs
        jobs.init_internal_token()
        c = APP.test_client()                                    # no login: the console has none
        body = {"op": "stop", "args": {}}
        self.assertEqual(c.post("/api/internal/watchdog", json=body, headers=H).status_code, 403)
        self.assertEqual(c.post("/api/internal/watchdog", json=body,
                                headers={**H, "X-ARIS-Internal": "wrong"}).status_code, 403)
        r = c.post("/api/internal/watchdog", json=body, headers={**H, "X-ARIS-Internal": jobs.INTERNAL["token"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["status"], "ok")


class ClearLogs(unittest.TestCase):
    """The Activity tab's Clear logs button (v4.8.8): the event logs and result lists go, everything else stays, a
    running capture keeps running and is listed again when it ends, the chat still gets the notices written after
    it, and /api/disk gives the sizes the tab shows."""
    LOGS = ("WATCHDOG_LOG", "PATROL_LOG", "AUDIT_HISTORY", "AUDIT_REPORT", "CAPTURES", "NOTICES", "RECENT_CHECKS")

    def _seed(self, eng):
        eng.init_db()
        eng._log_append({"type": "burst", "t": time.time(), "aris_freq_mhz": 433.92})
        eng._notice("a test notice")
        eng._jsonl_append(eng.PATROL_LOG, {"time": "2026-11-02 10:00:00", "band": "433", "flagged": 1, "top": []},
                          eng.PATROL_LOG_MAX_LINES)
        eng._jsonl_append(eng.AUDIT_HISTORY, {"timestamp": "2026-11-02 10:00:00"}, eng.AUDIT_LOG_MAX_LINES)
        eng._atomic_write_json(eng.CAPTURES, [{"t": time.time(), "freq_mhz": 96.9, "outcome": "identified"}])
        eng._atomic_write_json(eng.AUDIT_REPORT, {"timestamp": "x"})
        eng._atomic_write_json(eng.RECENT_CHECKS, [{"time": "x", "epoch": time.time(), "frequency_mhz": 96.9}])
        (eng.DATA_DIR / "check_report.json").write_text("{}")
        eng.LOG_FILE.write_text("a line of the text log\n" * 30000)                 # about 0.7 MB
        (eng.DATA_DIR / "aris.log.1").write_text("an old rotation\n")
        eng._remember_station(96.9, "HITS FM", "0x5969", "00001001", carrier_mhz=96.9)
        eng.BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        (eng.BASELINE_DIR / "test_baseline.json").write_text("{}")
        (eng.DATA_DIR / "devices.json").write_text("{}")

    def test_clear_logs_api(self):
        from pathlib import Path
        eng = __import__("arisweb.engine", fromlist=["x"]).engine()
        c = client()
        self._seed(eng)
        cfg_before = eng.CONFIG_PATH.read_text() if eng.CONFIG_PATH.exists() else None
        self.assertEqual(APP.test_client().post("/api/clear_logs", headers=H).status_code, 401)   # a login
        self.assertEqual(c.post("/api/clear_logs").status_code, 403)                              # the header
        d = c.get("/api/disk").get_json()
        for k in ("logs_mb", "signals_mb", "baselines_mb", "other_mb", "total_mb", "free_gb"):
            self.assertIsInstance(d[k], (int, float), k)
        self.assertGreaterEqual(d["logs_mb"], 0.5)
        self.assertGreater(d["free_gb"], 0)
        self.assertRegex(d["line"], r"^On disk: logs [\d.]+ [kMG]B, signals [\d.]+ [kMG]B, baselines [\d.]+ [kMG]B, "
                                    r"other [\d.]+ [kMG]B; [\d.]+ [kMG]B free$")
        r = c.post("/api/clear_logs", headers=H)
        self.assertEqual(r.status_code, 200, r.get_json())
        j = r.get_json()
        self.assertTrue(j["message"].startswith("Logs cleared: "), j)
        self.assertRegex(j["message"], r"Logs cleared: \d+ files, \d+(\.\d+)? [kM]B freed\.")
        self.assertIn("Kept: the Signals list, FM station names, device history, baselines, settings, logins and "
                      "the Map.", j["message"])
        self.assertNotIn("still running", j["message"])
        self.assertGreaterEqual(j["freed_mb"], 0.5)
        for name in ("aris.log", "aris.log.1", "check_report.json") + tuple(getattr(eng, k).name for k in self.LOGS):
            self.assertIn(name, j["deleted"], name)
        for k in self.LOGS:
            self.assertFalse(getattr(eng, k).exists(), k)
        self.assertFalse((eng.DATA_DIR / "check_report.json").exists())
        self.assertFalse((eng.DATA_DIR / "aris.log.1").exists())
        self.assertEqual(eng.LOG_FILE.stat().st_size, 0)                                  # emptied, still there
        for p in (eng.FM_STATIONS, eng.BASELINE_DIR / "test_baseline.json", eng.DATA_DIR / "devices.json",
                  Path(eng.DB_PATH), eng.SETUP_REPORT if eng.SETUP_REPORT.exists() else eng.FM_STATIONS):
            self.assertTrue(p.exists(), p)
        self.assertTrue(c.get("/api/fm_known").get_json()["named"])                      # the station names stay
        if cfg_before is not None:
            self.assertEqual(eng.CONFIG_PATH.read_text(), cfg_before)
        a = c.get("/api/activity").get_json()
        self.assertEqual((a["captures"], a["checks"], a["flagged_rounds"]), ([], [], []))
        self.assertFalse(a["watchdog"].get("decoded_devices") or a["watchdog"].get("undecoded_signals"))
        d2 = c.get("/api/disk").get_json()
        self.assertLess(d2["logs_mb"], d["logs_mb"])                                      # (the cache was dropped)
        j = c.post("/api/clear_logs", headers=H).get_json()                               # again: nothing to do
        self.assertNotIn("aris.log.1", j["deleted"])

    def test_clear_logs_while_a_capture_runs(self):
        eng = __import__("arisweb.engine", fromlist=["x"]).engine()
        c = client()
        r = c.post("/api/capture/start", json={"freq_mhz": 433.92, "mode": "continuous"}, headers=H)
        self.assertEqual(r.status_code, 200, r.get_json())
        time.sleep(2)
        run = c.get("/api/activity").get_json()["watchdog"]["watchdogs_running"]
        self.assertTrue(run)
        serial = run[0]["serial"]
        j = c.post("/api/clear_logs", headers=H).get_json()
        self.assertIn("1 capture still running: logging 433.92 MHz continuously; what it finds from now on goes "
                      "into the fresh logs.", j["message"])
        self.assertEqual(j["captures_running"], 1)
        with open(eng.WATCHDOG_STATE) as f:
            state = json.load(f)["watchdogs"]
        self.assertEqual([w["serial"] for w in state], [serial])                         # its entry stays
        run = c.get("/api/activity").get_json()["watchdog"]["watchdogs_running"]
        self.assertEqual([w["serial"] for w in run], [serial])                           # and it still runs
        time.sleep(2)
        r = c.post("/api/capture/stop", json={"serial": serial}, headers=H)
        self.assertEqual(r.status_code, 200, r.get_json())
        t0 = time.time()
        while time.time() - t0 < 15:
            a = c.get("/api/activity").get_json()
            if any(x["freq_mhz"] == 433.92 for x in a["captures"]):
                break
            time.sleep(0.3)
        self.assertFalse(a["watchdog"]["watchdogs_running"])
        cap = next(x for x in a["captures"] if x["freq_mhz"] == 433.92)                 # listed again when it ends
        self.assertEqual(cap["outcome"], "stopped", cap)

    def test_chat_gets_notices_written_after_clear_logs(self):
        import threading
        from arisweb.bridge import ArisAIBridge
        eng = __import__("arisweb.engine", fromlist=["x"]).engine()
        b = object.__new__(ArisAIBridge)                       # the notice reader alone (no console process)
        b.messages, b.seq, b.notice_pos, b.last_notice = [], 0, None, None
        b.notice_lock, b.state_lock = threading.Lock(), threading.Lock()
        eng._notice("before")
        b.pull_notices()                                       # starts at the end of the file: 'before' is old
        self.assertEqual(b.messages, [])
        eng.clear_logs()
        eng._notice("written before the page polled again")   # a capture ended right after Clear logs
        b.forget_notices()                                     # (what /api/clear_logs does)
        b.pull_notices()
        self.assertEqual([m["text"] for m in b.messages], ["written before the page polled again"])
        eng.clear_logs()
        b.pull_notices()                                       # polled while the file is gone
        eng._notice("after a poll found no file")
        b.pull_notices()
        self.assertEqual([m["text"] for m in b.messages][-1], "after a poll found no file")


class Engine(unittest.TestCase):
    def test_routing(self):
        from arisweb.engine import engine
        eng = engine()
        self.assertEqual(eng._route_intent("where is 433.92 coming from?")[0], "locate_signal")
        self.assertEqual(eng._route_intent("which room is 914.842 MHz in")[0], "locate_signal")
        self.assertEqual(eng._route_intent("investigate 433.92")[0], "investigate_single_frequency")
        self.assertEqual(eng._route_intent("monitor 433.92")[0], "start_background_watchdog")
        self.assertIsNone(eng._route_intent("how does locating work?"))

    def test_saved_records_for_the_ai(self):
        # v4.8.6: the context cut each record at 110 characters ('1) Renault, a tyre-pr') and the AI invented the
        # other devices; old 101.7 records still said 'not standard broadcast FM' after 101.7 was named TOP 40
        import sqlite3
        from arisweb import bridge
        from arisweb.engine import engine
        eng = engine()
        v = ("• ARIS verdict (confirmed decode): Identified 3 different devices on 433.92 MHz (decoded by rtl_433): "
             "1) Renault, a tyre-pressure sensor (TPMS) on a vehicle (2 packets; ID 215f46; reports tyre pressure); "
             "2) Hyundai VDO, a tyre-pressure sensor (TPMS) on a vehicle (1 packet; ID 4ac5e0)\n"
             "• rtl_433 confirmed decode: Hyundai-VDO, Renault")
        line = bridge._fact_line(433.92, 14.2, v, "2026-11-02 16:51:44")
        self.assertIn("Hyundai-VDO, Renault", line)
        self.assertNotIn("tyre-pr]", line)
        self.assertTrue(line.split("]")[0].endswith("..."), line)
        eng.init_db()
        conn = sqlite3.connect(eng.DB_PATH)
        try:
            cur = conn.execute("INSERT INTO peaks (timestamp, frequency_mhz, power_db, snr_db, bw_khz, urh_analysis, "
                               "notes) VALUES (?, ?, ?, ?, ?, ?, ?)", (
                                   "2026-11-02 00:47:00", 101.7, -20.0, 25.0, 180.0,
                                   "• ARIS verdict (heuristic): Signal in the FM band at 101.700 MHz that is not "
                                   "standard broadcast FM; worth a closer look\n• Heard on: Dongle 2",
                                   "Dongle 2 | Signal in the FM band ... not standard broadcast FM"))
            conn.commit()
            row_id = cur.lastrowid
        finally:
            conn.close()
        keep = eng.FM_STATIONS.read_text() if eng.FM_STATIONS.exists() else None
        try:
            eng.FM_STATIONS.write_text(json.dumps({}))
            eng._remember_station(101.7, "TOP 40", "0x91E3", "00001001", carrier_mhz=101.7)
            conn = sqlite3.connect(eng.DB_PATH)
            try:
                text = conn.execute("SELECT urh_analysis FROM peaks WHERE id=?", (row_id,)).fetchone()[0]
            finally:
                conn.close()
            self.assertTrue(text.startswith("• ARIS verdict (relabelled): FM broadcast station 101.7 MHz"), text)
            self.assertIn('"TOP 40"', text)
            self.assertIn("• Heard on: Dongle 2", text)
            self.assertEqual(eng._relabel_named_signals(eng._known_stations()), 0)   # once, not at every start
        finally:
            if keep is None:
                eng.FM_STATIONS.unlink()
            else:
                eng.FM_STATIONS.write_text(keep)

    def test_negative_snr_is_signed_on_the_pages(self):
        # live Signal #7 (snr_db -2.8) showed 'SNR +-2.8 dB'; the Map table and labels had the same '+' prefix
        js = {n: open(os.path.join(harness.CODE_DIR, "arisweb", "static", "js", n)).read()
              for n in ("signals.js", "map.js")}
        self.assertNotIn("'SNR +' + p.snr_db", js["signals.js"])
        self.assertIn("Number(p.snr_db) >= 0 ? '+' : ''", js["signals.js"])
        self.assertNotIn("' +' + Number(lr.snr_db)", js["map.js"])
        self.assertNotIn("'+' + Number(x.snr_db)", js["map.js"])

    def test_nodes_tool(self):
        from arisweb.engine import engine
        rep = json.loads(engine()._dispatch("list_nodes", {}))
        self.assertEqual(rep["status"], "ok")
        self.assertGreaterEqual(len(rep["nodes"]), 3)

    def test_activity_says_how_sure_in_plain_words(self):
        # the Recent investigations list showed the raw value ("confirmed_decode") next to the time
        from arisweb.engine import engine
        eng = engine()
        eng._remember_check({"frequency_mhz": 92.1, "evidence": "confirmed_decode", "strongest_dongle": "Dongle 1",
                             "final_signal_verdict": "FM broadcast station KIDS FM"})
        checks = client().get("/api/activity").get_json()["checks"]
        mine = next(c for c in checks if c["frequency_mhz"] == 92.1)
        self.assertEqual(mine["evidence_text"], "Identified by a decoder")
        with open(os.path.join(harness.CODE_DIR, "arisweb", "static", "js", "activity.js")) as f:
            js = f.read()
        self.assertIn("c.evidence_text", js)
        self.assertNotIn("(c.evidence ||", js)


class Wifi(unittest.TestCase):
    """The Wi-Fi view of the Devices tab (v4.10.0): the helper's scan file, the baseline, no helper, unchanged."""
    DUMP = ("BSS aa:bb:cc:00:00:01(on wlx) -- associated\n\tfreq: 5745.0\n\tcapability: ESS Privacy (0x1011)\n"
            "\tsignal: -48.00 dBm\n\tSSID: Casa\n\tRSN:\t * Version: 1\n\t\t * Authentication suites: PSK\n"
            "BSS aa:bb:cc:00:00:02(on wlx)\n\tfreq: 2437\n\tcapability: ESS (0x0401)\n\tsignal: -80.00 dBm\n"
            "\tSSID: Vecino\n")

    def test_wifi_api_baseline_and_helper_states(self):
        import tempfile
        from pathlib import Path
        scan = Path(tempfile.mkdtemp()) / "scan.txt"
        os.environ["ARIS_WIFI_SCAN_FILE"] = str(scan)
        try:
            c = client()
            self.assertEqual(APP.test_client().get("/api/wifi").status_code, 401)             # a login
            self.assertEqual(c.post("/api/wifi/forget", headers=H).status_code, 200)
            r = c.get("/api/wifi").get_json()                                                 # no helper yet
            self.assertEqual((r["helper"], r["networks"], r["scan_age_s"]), ("missing", 0, None))
            self.assertEqual(c.post("/api/wifi/baseline", headers=H).status_code, 409)        # nothing heard: no empty baseline
            scan.write_text("# ARIS-WIFI 1 wlx\n" + self.DUMP)
            r = c.get("/api/wifi").get_json()
            self.assertEqual((r["helper"], r["networks"], r["counts"]["unrated"]), ("ok", 2, 2))
            self.assertIsNone(r["baseline_n"])
            self.assertEqual(c.get("/api/wifi?have=" + r["tag"]).get_json()["unchanged"], True)
            # the baseline needs the admin and the CSRF header
            self.assertEqual(c.post("/api/wifi/baseline").status_code, 403)
            j = c.post("/api/wifi/baseline", headers=H).get_json()
            self.assertEqual((j["ok"], j["baseline_n"]), (True, 2))
            # a new strong network in the next scan is worth a look
            scan.write_text(self.DUMP + "BSS aa:bb:cc:00:00:03(on wlx)\n\tfreq: 2412\n\tcapability: ESS (0x0401)\n"
                            "\tsignal: -45.00 dBm\n\tSSID: \\x00\\x00\n")
            os.utime(scan, (time.time() + 5, time.time() + 5))
            r = c.get("/api/wifi").get_json()
            self.assertEqual((r["networks"], r["counts"]["look"], r["counts"]["baseline"]), (3, 1, 2))
            first = r["rows"][0]
            self.assertEqual((first["status"], first["hidden"]), ("look", True))
            self.assertTrue(first["reasons"])
            self.assertNotIn("client", json.dumps(r).lower())                                 # nothing about connected devices
            # marks (v4.12.0): admin only, validated, they change the row's status and the page's tag
            tag = r["tag"]
            self.assertEqual(c.post("/api/wifi/mark", json={"bssids": [first["key"]], "mark": "known"}).status_code, 403)
            self.assertEqual(c.post("/api/wifi/mark", headers=H, json={"bssids": [first["key"]], "mark": "maybe"}).status_code, 400)
            self.assertEqual(c.post("/api/wifi/mark", headers=H, json={"mark": "known"}).status_code, 400)
            j = c.post("/api/wifi/mark", headers=H, json={"bssids": first["bssids"], "mark": "known"}).get_json()
            self.assertEqual((j["ok"], j["changed"]), (True, len(first["bssids"])))
            r2 = c.get("/api/wifi?have=" + tag).get_json()
            self.assertNotIn("unchanged", r2)                                                 # the tag moved with the mark
            self.assertEqual((r2["counts"]["look"], r2["counts"]["known"]), (0, 1))
            c.post("/api/wifi/mark", headers=H, json={"bssids": first["bssids"], "mark": None})
            # a scan file older than 5 minutes: the helper has stopped
            old = time.time() - 600
            os.utime(scan, (old, old))
            self.assertEqual(c.get("/api/wifi").get_json()["helper"], "stale")
            self.assertEqual(c.post("/api/wifi/forget", headers=H).get_json()["ok"], True)
            self.assertEqual(c.get("/api/wifi").get_json()["baseline_n"], None)
        finally:
            os.environ.pop("ARIS_WIFI_SCAN_FILE", None)

    def test_watcher_folds_scans_in_and_posts_notices(self):
        import tempfile
        from pathlib import Path
        from arisweb import wifi as WF
        from arisweb.engine import engine
        scan = Path(tempfile.mkdtemp()) / "scan.txt"
        os.environ["ARIS_WIFI_SCAN_FILE"] = str(scan)
        try:
            c = client()
            c.post("/api/wifi/forget", headers=H)
            self.assertEqual(WF.tick(), [])                                    # no helper: nothing
            scan.write_text(self.DUMP)
            self.assertEqual(WF.tick(), [])                                    # no baseline: nothing rated, nothing said
            self.assertEqual(c.post("/api/wifi/baseline", headers=H).get_json()["baseline_n"], 2)
            self.assertEqual(WF.tick(), [])                                    # the first round only notes what is there
            notices = engine().NOTICES
            before = notices.read_text() if notices.exists() else ""
            scan.write_text(self.DUMP + "BSS aa:bb:cc:00:00:03(on wlx)\n\tfreq: 2412\n\tcapability: ESS (0x0401)\n"
                            "\tsignal: -45.00 dBm\n\tSSID: Nuevo\n")
            t = WF.tick()
            self.assertEqual(len(t), 1, t)
            self.assertTrue(t[0].startswith("Wi-Fi: a new network worth a look: 'Nuevo'"), t)
            added = notices.read_text()[len(before):]
            self.assertIn('"kind": "wifi"', added)
            self.assertIn("'Nuevo'", added)
            self.assertEqual(WF.tick(), [])                                    # once a day
            r = c.get("/api/wifi").get_json()                                  # the page gets the history
            row = next(x for x in r["rows"] if x["ssid"] == "Nuevo")
            self.assertEqual(len(row["hist"]), 96)
            self.assertEqual(row["hist"][-1], -45)
            self.assertEqual(r["hist_slot_s"], 900)
        finally:
            os.environ.pop("ARIS_WIFI_SCAN_FILE", None)


class Bluetooth(unittest.TestCase):
    """The Bluetooth view of the Devices tab (v4.11.0): the helper's summary file, the baseline, no helper, unchanged."""

    @staticmethod
    def _snap(t, devs):
        return {"v": 1, "t": t, "window_s": 60, "packets": 100,
                "stable": [{"label": "#" + x[0], "atype": "public", "company": "Acme", "uuids": [], "n": 5, "rssi_max": x[1],
                            "rssi_last": x[1], "trackers": [], "oui": "10:00:00"} for x in devs],
                "rotating": {"distinct": 40, "by_company": {"Apple, Inc.": 30}, "rssi_max": -50}, "trackers": {}}

    def test_bluetooth_api_baseline_and_helper_states(self):
        import tempfile
        from pathlib import Path
        scan = Path(tempfile.mkdtemp()) / "scan.json"
        os.environ["ARIS_BT_SCAN_FILE"] = str(scan)
        try:
            c = client()
            now = time.time()
            self.assertEqual(APP.test_client().get("/api/bluetooth").status_code, 401)
            self.assertEqual(c.post("/api/bluetooth/forget", headers=H).status_code, 200)
            r = c.get("/api/bluetooth").get_json()                                            # no helper yet
            self.assertEqual((r["helper"], r["devices"], r["scan_age_s"]), ("missing", 0, None))
            self.assertEqual(c.post("/api/bluetooth/baseline", headers=H).status_code, 409)   # nothing heard: no empty baseline
            scan.write_text(json.dumps(self._snap(now - 5, [("aaaaaa", -70), ("bbbbbb", -80)])))
            r = c.get("/api/bluetooth").get_json()
            self.assertEqual((r["helper"], r["devices"], r["counts"]["unrated"], r["rotating"]["distinct"]), ("ok", 2, 2, 40))
            self.assertEqual(c.get("/api/bluetooth?have=" + r["tag"]).get_json()["unchanged"], True)
            self.assertEqual(c.post("/api/bluetooth/baseline").status_code, 403)             # needs the CSRF header
            j = c.post("/api/bluetooth/baseline", headers=H).get_json()
            self.assertEqual((j["ok"], j["baseline_n"]), (True, 2))
            scan.write_text(json.dumps(self._snap(now - 2, [("aaaaaa", -70), ("cccccc", -40)])))   # a new, very close device
            r = c.get("/api/bluetooth").get_json()
            self.assertEqual((r["devices"], r["counts"]["look"], r["counts"]["baseline"]), (3, 1, 2))
            self.assertEqual(r["rows"][0]["label"], "#cccccc")
            self.assertNotRegex(json.dumps(r), r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}")
            scan.write_text("not json")                                                       # a broken file is "missing", not an error
            self.assertEqual(c.get("/api/bluetooth").get_json()["helper"], "missing")
            scan.write_text(json.dumps(self._snap(now - 600, [])))
            self.assertEqual(c.get("/api/bluetooth").get_json()["helper"], "stale")
            self.assertEqual(c.post("/api/bluetooth/forget", headers=H).get_json()["ok"], True)
            self.assertEqual(c.get("/api/bluetooth").get_json()["baseline_n"], None)
        finally:
            os.environ.pop("ARIS_BT_SCAN_FILE", None)


class UpdateBadge(unittest.TestCase):
    def test_update_api(self):
        self.assertEqual(APP.test_client().get("/api/update").status_code, 401)
        j = client().get("/api/update").get_json()                      # the harness switches the check off
        self.assertEqual((j["enabled"], j["available"]), (False, False))
        from arisweb.engine import engine as _eng
        eng, real = _eng(), _eng()._check_for_update
        os.environ["ARIS_UPDATE_CHECK"] = "1"
        try:
            head = eng._git_ro(eng._HERE, "rev-parse", "HEAD")        # the check saw this very commit
        except Exception:
            head = "h"                                                  # (a copy without git: nothing to compare)
        try:
            eng._check_for_update = lambda root=None: {"checked_at": int(time.time()), "available": True,
                                                        "version": eng.VERSION, "head": head, "local": "1111111",
                                                        "remote": "2222222", "command": "sudo bash /x/update.sh"}
            client().get("/api/update")                                 # starts the background check
            eng._UPDATE_RUN["thread"].join(5)
            j = client().get("/api/update").get_json()
            self.assertEqual((j["available"], j["remote"], j["command"]), (True, "2222222", "sudo bash /x/update.sh"))
            self.assertNotIn("head", j)
        finally:
            eng._check_for_update = real
            os.environ["ARIS_UPDATE_CHECK"] = "0"
            if eng.UPDATE_FILE.exists():
                eng.UPDATE_FILE.unlink()


class Devices(unittest.TestCase):
    """The Devices tab's API: the history grouped per vehicle, unchanged answers, Forget all devices."""

    def test_devices_api_and_forget(self):
        import seed_devices
        eng = __import__("arisweb.engine", fromlist=["x"]).engine()
        c = client()
        eng.forget_devices()
        self.assertEqual(APP.test_client().get("/api/devices").status_code, 401)             # a login
        r = c.get("/api/devices").get_json()
        self.assertEqual((r["rows"], r["devices"], r["sets"]), ([], 0, 0))
        self.assertIn("transmit together", r["rule"])
        seed_devices.seed(eng)
        r = c.get("/api/devices").get_json()
        self.assertEqual((r["devices"], r["sets"]), (7, 1))
        self.assertEqual(c.get("/api/devices?have=" + r["tag"]).get_json(), {"unchanged": True, "tag": r["tag"]})
        titles = [x["title"] for x in r["rows"]]
        self.assertIn("Citroen car: 4 tyre sensors", titles)
        self.assertIn("Nexus-TH temperature sensor 213", titles)
        self.assertIn("Renault tyre sensor 5b6d1e", titles)
        # the remote's title is the name the investigation reply gives it (v4.8.8 said 'Generic-Remote #1e4e6d')
        self.assertTrue(any(t.startswith("Generic fixed-code remote (PT2262 / EV1527-type chip) #") for t in titles),
                        titles)
        self.assertEqual(r["groups"], {"vehicle": 2, "sensor": 1, "remote": 1})      # v4.9.7: the car's 4 sensors are one row
        self.assertEqual(r["vehicles"], {"car": 1, "together": 0, "again": 0, "passing": 1})
        self.assertEqual([x["last_t"] for x in r["rows"]], sorted((x["last_t"] for x in r["rows"]), reverse=True))
        car = next(x for x in r["rows"] if x["kind"] == "set")
        self.assertEqual(len(car["members"]), 4)
        self.assertEqual(sorted(car["ids"]), ["92cf949a", "92cf94a1", "92cf94b3", "92cf94c7"])
        self.assertTrue(car["seen_line"].startswith("Seen 3 times since "), car["seen_line"])    # 3 passes, not 12
        # the car's last pass is 9 minutes before now: just after midnight that is yesterday (the CI runs in UTC)
        same_day = time.strftime("%Y%m%d", time.localtime(car["last_t"])) == time.strftime("%Y%m%d")
        self.assertIn("(today)" if same_day else "(yesterday)", car["seen_line"])
        # the car was heard on three different days, so it is known by the rule; the others are not
        self.assertEqual((car["known"], car["known_by"]), (True, "days"))
        self.assertEqual(r["known"], 1)                                   # the car: one row
        nexus0 = next(x for x in r["rows"] if x["model"] == "Nexus-TH")
        self.assertEqual((nexus0["known"], nexus0["known_by"], nexus0.get("mark")), (False, None, None))
        self.assertIn("Known: heard on 3 or more different days", r["known_rule"])
        # the Known toggle: mark the Nexus known, the car watched, then back to the rule
        self.assertEqual(c.post("/api/devices/mark", json={"key": nexus0["key"], "known": True}).status_code, 403)
        j = c.post("/api/devices/mark", json={"key": nexus0["key"], "known": True}, headers=H).get_json()
        self.assertEqual((j["ok"], j["changed"]), (True, 1))
        j = c.post("/api/devices/mark", json={"key": car["key"], "known": False}, headers=H).get_json()
        self.assertEqual(j["changed"], 4)
        self.assertEqual(c.post("/api/devices/mark", json={"key": "Nope|1", "known": True}, headers=H).status_code, 404)
        for bad in ("yes", 1, 0, 1.0):
            self.assertEqual(c.post("/api/devices/mark", json={"key": car["key"], "known": bad}, headers=H).status_code, 400)
        r3 = c.get("/api/devices").get_json()
        nexus3 = next(x for x in r3["rows"] if x["model"] == "Nexus-TH")
        car3 = next(x for x in r3["rows"] if x["kind"] == "set")
        self.assertEqual((nexus3["known"], nexus3["known_by"], nexus3["mark"]), (True, "you", "yes"))
        self.assertEqual((car3["known"], car3["known_by"], car3["mark"]), (False, None, "no"))
        self.assertEqual(r3["known"], 1)
        c.post("/api/devices/mark", json={"key": car["key"], "known": None}, headers=H)
        car4 = next(x for x in c.get("/api/devices").get_json()["rows"] if x["kind"] == "set")
        self.assertEqual((car4["known"], car4["mark"]), (True, None))
        c.post("/api/devices/mark", json={"key": nexus0["key"], "known": None}, headers=H)
        # the chat's list says which are known
        reply = eng._devices_reply(eng.devices_list())
        self.assertIn("1 known", reply)
        self.assertIn("Citroen car: 4 tyre sensors (known)", reply)
        self.assertNotIn("Nexus-TH temperature sensor 213 (known)", reply)
        self.assertTrue(car["reading_words"].endswith("bar, 24 °C"), car["reading_words"])   # no 'flags a0'
        self.assertEqual(r["tag"].split("-")[-1], time.strftime("%Y%m%d"))                   # a new day: new rows
        # the GUI chat sends '/devices [text]' to the console as it is (v4.8.8 wrapped it in the saved-records
        # context, which became the filter)
        from arisweb import bridge
        self.assertTrue(bridge.SLASH_COMMANDS.match("/devices") and bridge.SLASH_COMMANDS.match("/devices Citroen"))
        self.assertFalse(bridge.SLASH_COMMANDS.match("/devicesx") or bridge.SLASH_COMMANDS.match("what devices"))
        for m in car["members"]:
            self.assertTrue(m["seen_line"].startswith("Seen 3 times since "), m["seen_line"])
        nexus = next(x for x in r["rows"] if x["model"] == "Nexus-TH")
        self.assertEqual(nexus["reading_words"], "23.4 °C, 44% humidity, battery ok, channel 1")
        self.assertEqual((nexus["transmissions"], nexus["dongles"]), (40, ["Dongle 3"]))
        text = eng.DEVICES_FILE.read_text()
        for bad in ("1193046", "0F10FF01F1F0", "tristate", '"cmd"'):                      # the remote's codes
            self.assertNotIn(bad, text, bad)
        self.assertEqual(c.post("/api/devices/forget").status_code, 403)                     # the header
        j = c.post("/api/devices/forget", headers=H).get_json()
        self.assertEqual((j["ok"], j["forgotten"]), (True, 7))
        self.assertIn("Forgotten: 7 devices", j["message"])
        r2 = c.get("/api/devices").get_json()
        self.assertEqual(r2["rows"], [])
        self.assertNotEqual(r2["tag"], r["tag"])


if __name__ == "__main__":
    unittest.main()
