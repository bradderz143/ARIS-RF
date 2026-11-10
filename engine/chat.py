"""ARIS engine, part "chat": The LLM console: the AI's view of results, chat routing (_route_intent), direct replies, run_turn,
the tools' plain-English replies, the console commands and main().

Executed into aris.py's namespace by aris._include (see aris.py): names from aris.py's imports and from the
parts before this one are available here without imports, and everything defined here becomes aris.<name>.
"""
from __future__ import annotations

# ============================================================================
# LLM CONSOLE
# ============================================================================
_KEEP_KEYS = {
    "stage1_iq_physical_and_bitstream": ("status", "signal_present_during_capture", "physical_waveform_classification",
                                         "measured_carrier_mhz", "channel_avg_snr_db", "active_time_ms",
                                         "bursts_detected", "wideband_fm", "occupied_bw_khz",
                                         "stereo_pilot_19khz", "rds_57khz", "on_standard_fm_channel",
                                         "offset_from_channel_khz", "channel_snr_db"),
    "stage2_rtl433_telemetry": ("status", "packets_decoded_count", "identified_models", "decoded_packets",
                                "bursts_heard", "unknown_signal_fingerprints"),
    "stage3_multimon_direwolf_audio": ("status", "validated_frames", "aprs_frames", "ctcss_tone", "audio_tone"),
    "stage4_plain_english": ("device_identification_and_decode", "note"),
    # band scans: one short entry per signal (the full detail stays in /raw and last_investigation.json)
    "investigated_peaks[]": ("frequency_mhz", "channel_mhz", "snr_db", "pattern", "active_percent_of_sweep",
                             "strongest_dongle", "heard_by", "not_heard_by", "final_signal_verdict", "evidence",
                             "broadcast_station_confirmed", "measured_carrier_mhz", "stage2_rtl433_telemetry",
                             "possible_clock_harmonic", "earlier_checks_of_this_frequency", "investigation_error"),
}
_DROP_KEYS = {"ook_ask_binary_prefix", "native_bitstream_extractor", "trials", "measurements", "inventory"}
_LIST_LIMITS = {"decoded_packets": 2, "validated_frames": 4, "aprs_frames": 3, "recent_events": 4,
                "decoded_devices": 6, "undecoded_signals": 4, "threat_manifest": 6, "why": 4,
                "rejected_artifacts": 2, "all_gain_trials": 8, "recent_runs": 6, "investigated_peaks": 8,
                "top_signals": 12, "unknown_signal_fingerprints": 3, "baselines": 12, "strongest": 5,
                "earlier_checks_of_this_frequency": 2}
_STR_LIMITS = {"device_identification_and_decode": 500, "verdict": 120}


def _slim(obj, key=None, tight=False):
    """Shrink a tool result for the model (smaller prompts answer faster; full output stays on disk / in /raw).
    tight=True halves every list and shortens text, for results that are still too long."""
    if isinstance(obj, dict):
        keep = _KEEP_KEYS.get(key)
        return {k: _slim(v, k, tight) for k, v in obj.items() if (keep is None or k in keep) and k not in _DROP_KEYS}
    if isinstance(obj, list):
        lim = _LIST_LIMITS.get(key, 10)
        lim = max(1, lim // 2) if tight else lim
        items = [_slim(x, f"{key}[]" if key else None, tight) for x in obj[:lim]]
        if len(obj) > lim:
            items.append(f"... {len(obj) - lim} more omitted")
        return items
    if isinstance(obj, str):
        lim = min(_STR_LIMITS.get(key, 220), 160) if tight else _STR_LIMITS.get(key, 220)
        return obj if len(obj) <= lim else obj[:lim] + "..."
    return obj


def _clip(s: str) -> str:
    return s if len(s) <= TOOL_OUTPUT_CLIP else s[:TOOL_OUTPUT_CLIP] + "... [output truncated]"


def _for_llm(result: str) -> str:
    try:
        obj = json.loads(result)
    except ValueError:
        return _clip(result)
    txt = json.dumps(_slim(obj), separators=(",", ":"), default=_json_default)
    if len(txt) > TOOL_OUTPUT_CLIP:
        txt = json.dumps(_slim(obj, tight=True), separators=(",", ":"), default=_json_default)
    return _clip(txt)


def _get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _msg_to_dict(msg) -> dict:
    d = {"role": _get(msg, "role", "assistant") or "assistant", "content": _get(msg, "content", "") or ""}
    calls = _get(msg, "tool_calls") or []
    if calls:
        d["tool_calls"] = [{"function": {"name": _get(_get(c, "function"), "name"),
                                         "arguments": dict(_get(_get(c, "function"), "arguments") or {})}}
                           for c in calls]
    return d


OLD_TOOL_CHARS = 700        # tool results from earlier turns are cut to this (the AI already answered from them)
REPLY_TOKENS = 700          # num_predict


def _est_tokens(messages) -> int:
    """Rough token count (about 3.2 characters per token for this mix of English and JSON; errs on the high
    side so the prompt never overflows the context window)."""
    n = 0
    for m in messages:
        n += 8 + len(str(m.get("content") or "")) // 3
        for c in m.get("tool_calls") or []:
            n += 20 + len(json.dumps((c.get("function") or {}).get("arguments") or {}, default=str)) // 3
    return n


@functools.lru_cache(maxsize=1)
def _fixed_tokens() -> int:
    """System prompt plus the tool list: sent with every request (Ollama caches them while the model is loaded)."""
    return (len(SYSTEM_PROMPT) + len(json.dumps(TOOL_SPECS))) // 3 + 300


def _history_budget() -> int:
    return max(1500, NUM_CTX - REPLY_TOKENS - _fixed_tokens() - 300)


def _shorten_tool(m) -> None:
    c = str(m.get("content") or "")
    if len(c) > OLD_TOOL_CHARS + 40:
        m["content"] = c[:OLD_TOOL_CHARS] + " ...(earlier result shortened)"


def _trim_history(messages: list) -> None:
    """Keep the conversation inside the context window ourselves, so Ollama never truncates it.

    1. Tool results from earlier turns are shortened (only the newest user message's turn keeps full results).
    2. When the history is still too long, the oldest whole turns are dropped, down to well under the limit.
       Dropping in one big step (not one message per turn) keeps the start of the prompt unchanged for many turns,
       so Ollama can reuse its cache of it instead of re-reading the whole conversation every time (minutes on
       a CPU)."""
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=0)
    for m in messages[1:last_user]:
        if m.get("role") == "tool":
            _shorten_tool(m)
    hist = messages[1:]
    budget = _history_budget()
    if len(hist) <= MAX_HISTORY and _est_tokens(hist) <= budget:
        return
    starts = [i for i, m in enumerate(messages) if i > 0 and m.get("role") == "user"]
    for i in starts:
        tail = messages[i:]
        if len(tail) <= MAX_HISTORY // 2 and _est_tokens(tail) <= budget * 0.6:
            messages[:] = [messages[0]] + tail
            return
    messages[:] = [messages[0]] + (messages[starts[-1]:] if starts else [])


def _fit_turn(messages: list) -> None:
    """Within one turn (several tool rounds), shorten the older tool results if the prompt would overflow."""
    budget = _history_budget()
    tools = [m for m in messages[1:] if m.get("role") == "tool"]
    for m in tools[:-1]:
        if _est_tokens(messages[1:]) <= budget:
            return
        _shorten_tool(m)


# The role of the account behind the turn in progress (the GUI's bridge marks a line '[role:viewer] ...' for an
# extra account; a terminal console is the admin). An extra account looks, measures, captures and decodes; the
# tools that change the station's set-up are refused for it here, in the console, whatever words or alias the
# request used (v4.9.5 review: the web layer's phrase list missed 'set up everything', 'remove dongle 2',
# 'calibrate the gain', '/patrol stop').
_TURN = {"role": "admin"}
_ADMIN_TOOLS = {"auto_setup", "calibrate_gain", "calibrate_frequency", "forget_dongle", "record_baseline",
                "start_patrol", "stop_patrol", "assign_dongles", "set_antenna_note", "set_timing_defaults",
                "perform_system_maintenance"}
_ADMIN_COMMAND_RX = re.compile(r"^/(?:setup|cleanup|clear-signals)\b|^/patrol\s+(?:start|stop)\b")
_ROLE_MARK_RX = re.compile(r"^\[role:([a-z]+)\]\s*")


def _take_role_mark(line: str) -> str:
    """Reads and strips the bridge's role marker from a console line; sets the turn's role (admin without one)."""
    m = _ROLE_MARK_RX.match(line)
    _TURN["role"] = m.group(1) if m else "admin"
    return line[m.end():] if m else line


def _dispatch(name: str, args) -> str:
    fn = TOOLS.get(name)
    if fn is None:
        return json.dumps({"status": "error", "error": f"Unknown tool '{name}'", "available": sorted(set(TOOLS))})
    if _TURN.get("role") != "admin" and _canonical(name) in _ADMIN_TOOLS:
        return json.dumps({"status": "error", "error": "only the admin account can change the station's set-up "
                                                       "(setup, calibration, dongles, baselines, patrol, timings, "
                                                       "maintenance); this account can look, measure, capture and "
                                                       "decode"})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    return fn(**(args if isinstance(args, dict) else {}))


def _show_result(name: str, result: str, raw: bool) -> None:
    text = result
    if not raw:
        try:
            text = json.dumps(json.loads(result), separators=(",", ":"))
        except ValueError:
            pass
        if len(text) > 500:
            text = text[:500] + " ...(use /raw for full output)"
    print(f"[Hardware:{name}] {text}")


def _as_dict(args):
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    return dict(args or {})


_SWEEP_TOOLS = ("check_and_investigate_band", "investigate_band", "scan_band", "sweep_spectrum")
_TIME_RX = re.compile(r"(\d+(?:\.\d+)?)\s*-?\s*(seconds?|secs?|s|minutes?|mins?)\b")


def _user_words(text):
    """What the user typed, without the context the GUI appends ("(Context: ..." / "(Context, only if ...")."""
    return re.split(r"\s\(Context[,:]", str(text or ""), maxsplit=1)[0].lower()


def _requested_seconds(text):
    """'sweep for 30 seconds' / 'a 2 minute scan' in the user's own words -> seconds (or None)."""
    t = _user_words(text)
    if re.search(r"\b(?:per|each)\s+peak\b|\bdwell|\blisten", t):
        return None
    m = _TIME_RX.search(t)
    if not m:
        return None
    v = float(m.group(1)) * (60.0 if m.group(2).startswith("m") else 1.0)
    return int(round(v)) if v >= 1 else None


def _fix_sweep_args(name, args, user_text):
    """Small models put 'sweep for 30 seconds' into dwell_seconds. If the user asked for a timed sweep and
    the call has no sweep length, apply the user's number as the sweep length."""
    if name not in _SWEEP_TOOLS:
        return args
    args = _as_dict(args)
    if any(args.get(k) not in (None, "", 0, "0") for k in ("sweep_seconds", "sweep_time", "sweep_duration")):
        return args
    secs = _requested_seconds(user_text)
    if secs:
        args["sweep_seconds"] = secs
        if str(args.get("dwell_seconds", "")) == str(secs):
            args.pop("dwell_seconds")
        print(f"[HW] Using a {secs} s sweep, as requested.")
    return args


_INVESTIGATE_TOOLS = ("investigate_single_frequency", "investigate_frequency", "identify_signal")
_EXACT_RX = re.compile(r"\b(exactly|identify|precisely|find out what|keep listening|listen longer|longer|"
                       r"all (?:of )?the packets|every packet|all packets)\b")


def _said_seconds(text):
    """The listening time in the user's own words ('for 2 minutes', '45 s', '90 seconds'), or None."""
    m = _TIME_RX.search(_user_words(text))
    if not m:
        return None
    v = float(m.group(1)) * (60.0 if m.group(2).startswith("m") else 1.0)
    return int(round(v)) if v >= 1 else None


def _fix_listen_args(name, args, user_text):
    """'listen for 2 minutes' -> exactly that (the user's words win over the model's number); 'find out exactly
    what it is' -> a 60 s listen."""
    if name not in _INVESTIGATE_TOOLS:
        return args
    args = _as_dict(args)
    secs = _said_seconds(user_text)
    keys = ("listen_seconds", "dwell_seconds", "duration", "seconds")
    if secs:
        secs = max(3, secs)                   # more than 10 min: the tool explains what to use instead
        if str(args.get("listen_seconds")) != str(secs):
            for k in keys:
                args.pop(k, None)
            args["listen_seconds"] = secs
            print(f"[HW] Listening for {secs} s, as requested.")
        return args
    if any(args.get(k) not in (None, "", 0, "0") for k in keys):
        return args
    if _EXACT_RX.search(_user_words(user_text)):
        args["listen_seconds"] = 60
        print("[HW] Listening for 60 s to identify it.")
    return args


_ALL_RX = re.compile(r"\b(everything|every\s+(?:signal|peak|one|device|transmitter)|all\s+(?:the\s+|of\s+the\s+)?"
                     r"(?:signals|peaks|devices|transmitters|of\s+them)|each\s+(?:signal|peak|one|device))\b")
_EACH_TIME_RX = re.compile(r"(?:each|every|per)\s+(?:signal|peak|one|device|frequency)?\s*(?:for\s+)?"
                           r"(\d+(?:\.\d+)?)\s*-?\s*(seconds?|secs?|s|minutes?|mins?)\b|"
                           r"(\d+(?:\.\d+)?)\s*-?\s*(seconds?|secs?|s|minutes?|mins?)\s+(?:each|per\s+(?:signal|peak))")
_SCAN_TOOLS = ("check_and_investigate_band", "investigate_band", "scan_band")


def _fix_peaks_args(name, args, user_text):
    """Band scans: 'investigate everything' -> every signal (up to 12); 'listen to each for 30 s' -> that time
    per signal (and not as the sweep length)."""
    if name not in _SCAN_TOOLS:
        return args
    args = _as_dict(args)
    t = _user_words(user_text)
    try:
        cur = int(float(args.get("max_peaks_to_investigate") or 0))
    except (TypeError, ValueError):
        cur = 0
    if _ALL_RX.search(t) and cur < 12:
        args["max_peaks_to_investigate"] = 12
        print("[HW] Investigating every signal found (up to 12), as requested.")
    m = _EACH_TIME_RX.search(t)
    if m:
        v, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        secs = max(5, min(MAX_LISTEN_S, int(round(float(v) * (60.0 if unit.startswith("m") else 1.0)))))
        if str(args.get("dwell_seconds")) != str(secs):
            args["dwell_seconds"] = secs
            print(f"[HW] Listening to each signal for {secs} s, as requested.")
        if str(args.get("sweep_seconds")) == str(secs):
            args.pop("sweep_seconds")
    return args


_ACT_RX = re.compile(r"\b(investigate|analy[sz]e|identify|decode|demodulate|check|look\s+at|listen\s+(?:to|on|at)|"
                     r"find\s+out|tune\s+to|what(?:'s|\s+is)\s+(?:on|at))\b")
_WATCH_RX = re.compile(r"\b(mo[nm]+it[oe]r\w*|keep\s+(?:an\s+eye\s+on|watching|listening\s+to|logging|recording)|"
                       r"log\s+(?:everything|all|every\s+packet)|watch|capture|catch|"
                       r"grab|wait\s+for\s+(?:a|the|its)\s+(?:packet|signal|transmission)|until\s+(?:it|you)\s+"
                       r"(?:decode|get|capture|catch|hear)s?)\b")
# 'stop the capture', and also 'stop the 433 band capture' / 'stop recording the ism band' (v4.8.6 sent those to a
# band capture START: only the/all/my/that fitted between the stop word and the noun, and 'record' was missing)
_STOP_FILL = (r"(?:the|all|my|that|this|those|these|background|band|ism|current|running|active|whole|"
              r"\d{3}\s*-\s*\d{3,4}(?:\s*mhz)?|\d{3,4}(?:\.\d+)?(?:\s*mhz)?|mhz)")
# ("non-stop" is not a stop: "record the 433 band non-stop")
_STOP_WORD_RX = re.compile(r"(?<!non-)(?<!non )\b(?:stop|cancel|halt|abort|detener)\b"
                           r"(?!\s+(?:until|when|once|after|if|before)\b)")
_NO_STOP_RX = re.compile(r"\b(?:don'?t|do\s+not|never|without)\s+stop")
_STOPCAP_RX = re.compile(r"(?<!non-)(?<!non )\b(?:stop|cancel|end|halt|abort|detener|det[eé]n\w*)\s+"
                         r"(?:" + _STOP_FILL + r"\s+){0,4}"
                         r"(?:captur\w*|watchdogs?|monitor\w*|watching|listening|logging|record\w*|grabaci\w*)\b")
_SYSCHECK_RX = re.compile(r"\b(?:check|test|diagnos\w*)\s+(?:the\s+|my\s+|all\s+(?:the\s+|my\s+)?)?(?:whole\s+)?"
                          r"(?:system|dongles?|radios?|hardware|everything)\b|\bsystem\s+check\b|\b(?:are|is)\s+"
                          r"(?:my|the|all)\s+(?:\w+\s+)?(?:dongles?|radios?)\s+(?:working|ok|okay|connected|plugged)|"
                          r"\bwhich\s+dongles?\s+(?:are|is)\s+(?:attached|connected|plugged)")
_SETUP_RX = re.compile(r"\b(?:run|do|start|redo|re-?run)\s+(?:the\s+|a\s+)?(?:full\s+)?set\s?-?up\b|"
                       r"\bset\s+(?:everything|it\s+all|the\s+(?:whole\s+)?(?:system|dongles))\s+up\b|"
                       r"\bset\s?up\s+(?:everything|the\s+(?:whole\s+)?(?:system|dongles))\b")
_QUESTION_RX = re.compile(r"\bhow\b|\bwhy\b|\bwhat\b|\bexplain\b|\bshould\b|\bwhen\b|\bdid\b|\bresults?\b|"
                          r"\breport\b")
_HOURS_RX = re.compile(r"(\d+(?:\.\d+)?)\s*-?\s*(hours?|hrs?|horas?|h|minutes?|mins?|minutos?)\b")
_LOGS_RX = re.compile(r"\b(logs?|watchdog\s+(?:hear|heard|found|log|logs|caught))\b")
# 'what devices have you seen', 'list devices', 'which cars', 'any remotes?', 'device history' -> the device history
# (list_devices), when no frequency, action or band word makes it something else
_DEVICES_RX = re.compile(r"\b(?:what|which|list|show|seen|heard|any|known|history|so\s+far|decoded|all)\b.*"
                         r"\b(?:devices?|cars?|vehicles?|tyre\s+sensors?|tpms|sensors?|remotes?)\b|"
                         r"\b(?:devices?|cars?|vehicles?|sensors?|remotes?)\b.*\b(?:seen|heard|list|history|so\s+far)\b")
# v4.13.0: a question about the Wi-Fi networks -> wifi_networks, answered from the list without the AI
_WIFI_RX = re.compile(r"\bwi-?\s?fi\b|\bwlan\b|\bssids?\b|\brouters?\b|\baccess\s+points?\b|\bhotspots?\b|"
                      r"\bnetworks?\b.*\b(?:hidden|strongest|closest|nearest|new|open|channel|named|called)\b|"
                      r"\b(?:hidden|strongest|closest|nearest|new|open)\b.*\bnetworks?\b")
_WIFI_NOT_RX = re.compile(r"\bwhy\b|\bexplain\b|\bhow\s+(?:do|does|can|to|should|would)\b|\bbaseline\b|\bforget\b|"
                          r"\bmark\b|\binstall|\bhelper\b|\bset\s?up\b|\bcaptur|\brecord|\blocat|\bwhere\b|"
                          r"\bmonitor\s+mode\b|\bconnect|\bpassword\s+(?:for|of)\b|\bhack|\bcrack|\bjam")
_VEHICLE_WORD_RX = re.compile(r"\b(?:cars?|vehicles?|tyre\s+sensors?|tpms)\b")
_NOT_DIRECT_RX = re.compile(r"\bband\b|\bscan|\bsweep|\bbaseline|\baudit|\bpatrol|\bsaved\b|\bsetup\b|\bstatus\b|"
                            r"\bcalibrat|\bspectrum\b|\bwhy\b|\bexplain\b|\bhow\b")   # (and a stop word: `stopping`)
_FREQ_RX = re.compile(r"(?<![\d.])(\d{2,4}\.\d{1,6}|\d{2,4})(\s*(?:mhz|m\b))?")
_IT_RX = re.compile(r"\b(it|this|that|the signal|this signal|that signal|the same|this freq\w*|that freq\w*)\b")


def _last_checked(max_age_min=60.0):
    """Frequency of the most recent investigation (its measured carrier if known), or None."""
    try:
        with open(RECENT_CHECKS) as f:
            items = json.load(f)
    except (OSError, ValueError):
        return None
    if not items or time.time() - float(items[-1].get("epoch") or 0) > max_age_min * 60:
        return None
    return items[-1].get("measured_carrier_mhz") or items[-1].get("frequency_mhz")


_LOCATE_RX = re.compile(r"\b(locate|locali[sz]e|where\s+(?:is|'s|does|do|are)|where'?s|which\s+room|pinpoint|"
                        r"find\s+where|triangulat\w*|heat\s*map|coming\s+from)\b")


_NOT_FREQ_AFTER_RX = re.compile(r"\s*(?:%|(?:s|secs?|segs?|seconds?|segundos?|mins?|minutes?|minutos?|h|hrs?|"
                                r"hours?|horas?|ms|db|khz|hz|ghz|k|ks|x|times?|days?|packets?|bursts?|bits?|samples?|"
                                r"signals?|ppm|metres?|meters?)\b)")
# Not to be confused with _BAND_NICKNAMES (the band table above; v4.8.1 reused that name and broke every band '433').
_BARE_BAND_NUMBERS = {315, 433, 434, 868, 915}   # 'capture 433' means the band or 433.92, not 433.000: the AI asks
# A bare whole number is a frequency only right after the action ('capture 1090', 'investigate on 1090'): counts,
# durations, serials and dongle numbers elsewhere in the sentence are not ('capture it for 120', 'the last 100',
# 'check dongle 1001', 'r828d', all found by review).
_BARE_FREQ_RX = re.compile(r"\b(?:captur\w*|monitor\w*|watch\w*|record\w*|graba\w*|listen\w*|escucha\w*|"
                           r"investigat\w*|investiga|analy[sz]\w*|decode|identify|locate|find\s+out|check|look\s+at)"
                           r"\s+(?:on\s+|at\s+|to\s+|en\s+)?(\d{3,4})(?![\d.,]|\s*(?:mhz|m)\b)")


def _bare_freq(t, m):
    """A whole number without 'MHz' counts as a frequency when it comes right after the action word and is not
    followed by a unit ('capture 1090' is 1090 MHz; 'capture for 120 seconds' is not). On the real PC 'capture 1090
    for 5 minutes' went to the AI, which started a 433 band capture from the chat's history."""
    g = m.group(1)
    if "." in g or int(g) in _BARE_BAND_NUMBERS or _NOT_FREQ_AFTER_RX.match(t, m.end()):
        return False
    return any(b.start(1) == m.start(1) for b in _BARE_FREQ_RX.finditer(t))


# '433 MHz' is the everyday name of the band, not 433.000 (below the band: v4.8.6 captured there and heard nothing).
# A whole band number with 'MHz' means the band's busy channel for a single frequency, the band for a capture.
_BAND_CHANNEL = {315: 315.0, 433: 433.92, 434: 433.92, 915: 915.0}
_BAND_MHZ_RX = re.compile(r"(?<![\d.])(315|433|434|915)\s*(?:mhz|m)\b")


def _typed_freqs(t):
    """The frequencies the user actually typed (decimals, 'MHz', or a bare number right after the action).
    '433 MHz' counts as 433.92 (see _BAND_CHANNEL)."""
    out = set()
    for m in _FREQ_RX.finditer(t):
        g = m.group(1)
        if not ("." in g or m.group(2) or _bare_freq(t, m)) or not MIN_MHZ <= float(g) <= MAX_MHZ:
            continue
        out.add(_BAND_CHANNEL[int(g)] if "." not in g and m.group(2) and int(g) in _BAND_CHANNEL else float(g))
    return out


def _route_freq(t):
    """The one frequency in the text, or the last checked one for 'it' / 'this'. None if unclear."""
    freqs = _typed_freqs(t)
    if len(freqs) == 1:
        return freqs.pop()
    if not freqs and _IT_RX.search(t):
        return _last_checked()
    return None


_BANDCAP_RX = re.compile(r"\b(captur\w*|monitor\w*|watch\w*|record\w*|listen\w*|keep\s+(?:an\s+eye|logging))\b")


def _band_word(t):
    """'the ism band' -> '433', 'the 915 band' -> '915', '902-928' -> '902-928', else None. A unit may follow
    ('the 433mhz band')."""
    m = re.search(r"(?<![\d.])(\d{3}(?:\.\d+)?)\s*(?:-|to)\s*(\d{3,4}(?:\.\d+)?)(?!\d)", t)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    extra = _plugin_band_key(t)                    # a band a plug-in added ('the 868 band')
    if extra:
        return extra
    if re.search(r"(?<![\d.])9(?:15|02|28|00)(?:\.\d+)?(?!\d)", t):
        return "915"
    if re.search(r"(?<![\d.])315(?:\.\d+)?(?!\d)", t):
        return "315"
    if re.search(r"(?<![\d.])43[34](?:\.\d+)?(?!\d)|\bism\b", t):
        return "433"
    return None


# Band scans answered without the AI (v4.9.2): a scan word and one clear band. Rules: the band is an explicit
# range ('90-100 MHz', at most SCAN_MAX_SPAN_MHZ wide), a named band ('the FM band', 'the 433 band', 'ism band',
# 'the 915 band'), or 433 / 915 with the word band or a unit ('scan 433 MHz'); never a bare number alone, never with
# a question word (why / how / explain / should), a stop word, a capture word (those record the band), or the words
# that mean another tool (baseline, audit, patrol, setup, calibrate, spectrum, saved, status). 'everything' and
# 'each signal for N s' are honoured as for the AI's own calls (_fix_peaks_args); 'for N s' alone is the sweep time.
_SCAN_RX = re.compile(r"\b(?:scan\w*|sweep\w*|investigate|analy[sz]e|survey|look\s+at|check|escanea\w*|revisa\w*|"
                      r"what(?:'s|\s+is)\s+(?:on|in|transmitting\s+(?:on|in)))\b")
_SCAN_NOT_RX = re.compile(r"\bwhy\b|\bhow\b|\bexplain\b|\bshould\b|\bbaseline\b|\baudit\b|\bpatrol\b|\bsetup\b|"
                          r"\bcalibrat|\bspectrum\b|\bsaved\b|\bstatus\b|\blogs?\b|\bdevices?\b")
SCAN_MAX_SPAN_MHZ = 60.0
_SCAN_Q_RX = re.compile(r"\bwhy\b|\bhow\b|\bexplain\b|\bshould\b|\bwhen\b|\bdid\b|\bhave\s+you\b|\bhas\b|\bwas\b|"
                        r"\bwere\b|\bresults?\b|\breport\b|\blast\s+(?:scan|sweep|time)|\bago\b|\bearlier\b")
_NEGATED_RX = re.compile(r"\b(?:don'?t|do\s+not|never|no\s+need\s+to|without)\b")
_SCHEDULE_RX = re.compile(r"\b(?:every|each)\s+(?:\d+\s*)?(?:s|secs?|seconds?|mins?|minutes?|hours?|h)\b|\bhourly\b|\bdaily\b")


def _scan_band(user_text, t):
    """The band a scan request names: (lo, hi) or None (then the AI decides)."""
    rng = _parse_range(t)
    if rng:
        lo, hi = rng
        return (lo, hi) if 24.0 <= lo < hi <= 1766.0 and hi - lo <= SCAN_MAX_SPAN_MHZ else None
    nb = _named_band(user_text)
    if nb:
        return nb[0]
    if re.search(r"\bbanda\b", t) and re.search(r"\bfm\b", t):               # 'escanea la banda de fm'
        return (88.0, 108.0)
    # ('investigate 433 MHz' stays one frequency, 433.92; 'scan 433 MHz' is the band)
    if re.search(r"\bbanda?\b|\bism\b", t) or (_BAND_MHZ_RX.search(t) and re.search(r"\b(?:scan\w*|sweep\w*|survey)\b", t)):
        b = _band_word(t)
        if b in ("433", "915"):
            return _named_band(f"the {b} band")[0]
    return None


_PATROL_ASK_RX = re.compile(r"\bpatrol\b")
_PATROL_DO_RX = re.compile(r"\b(?:start|stop|begin|end|run|launch|turn|switch|enable|disable|pause|resume|set|change|"
                           r"why|explain|how\s+come|every|interval)\b")
# 'what dongles do I have and what are they doing?' (v4.8.6: the AI wrote the tool call as text, then a made-up
# 'Simulated Response' with a Dongle 3, invented signals and patrol 'Running')
_STATUS_RX = re.compile(
    r"\b(?:what|which)\s+dongles\b(?!\s+(?:heard|hears|hear|is\s+best|are\s+best))|\bhow\s+many\s+dongles\b|"
    r"\bwhat\s+(?:are|is)\s+(?:the\s+|my\s+|all\s+(?:the\s+|my\s+)?)?(?:dongles?|radios?|receivers?|aris)\s+"
    r"(?:doing|up\s+to)\b|\bwhat(?:'s|\s+is)\s+running\b|"
    r"\b(?:are|is)\s+(?:the\s+|my\s+|any\s+)?(?:dongles?|radios?)\s+(?:busy|free|in\s+use|doing\s+anything)\b|"
    r"^\W*(?:the\s+|system\s+|station\s+|aris\s+)?status\W*$|\b(?:system|station|aris|dongles?)\s+status\b|"
    r"\bwhat(?:'s|\s+is)\s+the\s+status\b(?!\s+of)")
_STATUS_THEY_RX = re.compile(r"\bwhat\s+(?:are|is)\s+(?:they|it|each\s+(?:one|of\s+them))\s+doing\b")
_FILTER_RX = re.compile(r"\b(?:for|from|about|of|on)\s+(?:the\s+|a\s+|an\s+|any\s+|my\s+)?([a-z][\w-]*(?:\s+[a-z][\w-]*){0,2})")
_NOT_A_MODEL = {"it", "this", "that", "them", "today", "now", "everything", "anything", "all", "me", "us", "you",
                "capture", "captures", "band", "watchdog", "recording", "recordings", "logs", "log", "please",
                "devices", "device", "sensors", "sensor", "packets", "signals", "signal", "remotes", "remote",
                "yesterday", "tonight", "last", "the", "ism", "dongle", "dongles", "hour", "hours", "minutes", "while"}


def _logs_filter(t):
    """'check the logs for Truck' -> 'truck': the model name to look for (None when the words after 'for' name no
    model, e.g. 'for today', 'from the capture', 'for 433.92')."""
    lm = _LOGS_RX.search(t)
    m = _FILTER_RX.search(t, lm.end() if lm else 0)
    if not m:
        return None
    words = [w for w in m.group(1).split() if w not in _NOT_A_MODEL]
    while words and words[-1] in ("sensor", "sensors", "device", "devices", "packets", "remote", "remotes"):
        words.pop()
    return " ".join(words) or None


_SECONDS_RX = re.compile(r"(\d+(?:\.\d+)?)\s*-?\s*(?:s|secs?|seconds?|segs?|segundos?)\b")


def _capture_minutes(t):
    """A capture's time limit in whole minutes from the user's words: hours, minutes, or seconds rounded up
    ('capture 1090 for 90 s' -> 2; it had no limit at all before v4.8.2). None if no time was given."""
    m = _HOURS_RX.search(t)
    if m:
        return max(1, int(round(float(m.group(1)) * (60.0 if m.group(2).startswith("h") else 1.0))))
    m = _SECONDS_RX.search(t)
    if m:
        return max(1, int(math.ceil(float(m.group(1)) / 60.0)))
    return None


def _route_intent(user_text):
    """Clear requests go straight to the right tool: 'investigate / analyse / find out <frequency or it>',
    'capture / monitor <frequency or it>', 'stop the capture', 'where is <frequency or it>', 'check the logs',
    'check the system' and 'run setup'. The small local model
    sometimes picks the wrong tool for these, and skipping its tool-choice round saves a long wait on a laptop
    CPU. Returns (tool, args) or None."""
    t = _user_words(user_text)
    dm = re.search(r"\bdongle\s*([1-9])\b", t)
    dg = {"dongle": dm.group(1)} if dm else {}
    if _STOPCAP_RX.search(t) and not _QUESTION_RX.search(t):
        if not dg and re.search(r"\bband\b|\bism\b|\brecord\w*|\d\s*-\s*\d", t):
            return "stop_background_watchdog", {"band": _band_word(t) or "any"}
        f = _route_freq(t)
        return "stop_background_watchdog", {**dg, **({"freq_mhz": f} if f is not None and not dg else {})}
    if dm and re.search(r"\b(forget|remove)\b", t) and not _QUESTION_RX.search(t):
        return "forget_dongle", dg
    stopping = bool(_STOP_WORD_RX.search(t)) and not _NO_STOP_RX.search(t)   # never START a capture from a stop
    banded = (re.search(r"\bism\b|\bband\b", t) and _BANDCAP_RX.search(t)) or (
        _BAND_MHZ_RX.search(t) and not re.search(r"\d\.\d", t) and re.search(r"\b(?:captur|record|graba|monitor|watch)", t))
    if banded and not _QUESTION_RX.search(t) and not stopping and not _EACH_TIME_RX.search(t):
        b = _band_word(t)                 # ('listen to each signal for 30 s' is a scan's per-signal time)
        if b:
            args = {"band": b, **dg}
            mins = _capture_minutes(t)
            if mins:
                args["max_minutes"] = mins
            return "start_background_watchdog", args
    if _SYSCHECK_RX.search(t) and not _QUESTION_RX.search(t) and _route_freq(t) is None:
        return "check_system", {}
    if _SETUP_RX.search(t) and not _QUESTION_RX.search(t):
        return "auto_setup", {}
    if _LOCATE_RX.search(t) and not re.search(r"\bhow\b|\bwhy\b|\bexplain\b", t):
        f = _route_freq(t)
        if f is not None:
            m = re.search(r"\b(\d{1,2})\s*(?:s|sec|secs|seconds)\b", t)
            return "locate_signal", {"frequency_mhz": f, **({"listen_seconds": int(m.group(1))} if m else {})}
    if _PATROL_ASK_RX.search(t) and not _PATROL_DO_RX.search(t) and not _typed_freqs(t):
        return "patrol_status", {}
    if (_STATUS_RX.search(t) or (_STATUS_THEY_RX.search(t) and re.search(r"\bdongles?\b|\bradios?\b", t))) \
            and not _typed_freqs(t) and not re.search(r"\b(?:why|explain|patrol|captur\w*|logs?|setup)\b", t):
        return "system_status", {}
    # (not with a typed frequency: 'investigate 433.92 on the 433 band' is that frequency; not a question about
    # past scans ('did you scan ...'), a 'don't', or a schedule ('every 10 minutes'): those stay with the AI)
    if _SCAN_RX.search(t) and not _SCAN_NOT_RX.search(t) and not _SCAN_Q_RX.search(t) and not stopping \
            and not _NEGATED_RX.search(t) and not _SCHEDULE_RX.search(t) \
            and (_EACH_TIME_RX.search(t) or not (_BANDCAP_RX.search(t) or _WATCH_RX.search(t))):
        band = _scan_band(user_text, t)
        typed = (_typed_freqs(t) - {band[0], band[1]}) if band else set()
        if not re.search(r"\d\.\d", t):
            typed -= set(_BAND_CHANNEL.values())      # ('scan 433 MHz': the band, not its channel)
        if band and not typed:
            args = {"frequency_range": f"{band[0]:g}-{band[1]:g}", **dg}
            secs = _said_seconds(t)
            if secs and not _EACH_TIME_RX.search(t):
                args["sweep_seconds"] = min(secs, 600)
            return "check_and_investigate_band", _fix_peaks_args("check_and_investigate_band", args, user_text)
    if _WIFI_RX.search(t) and not _WIFI_NOT_RX.search(t) and not stopping and not _NEGATED_RX.search(t):
        return "wifi_networks", {"question": t}
    if _NOT_DIRECT_RX.search(t) or stopping or _parse_range(t):
        return None          # ('non-stop', 'don't stop until it decodes' are no stop: they still route)
    if _DEVICES_RX.search(t) and not _typed_freqs(t) and not _ACT_RX.search(t) and not _LOGS_RX.search(t) \
            and not _WATCH_RX.search(t) and not re.search(r"\bdongles?\b|\bforget\b|\bnodes?\b|\bantennas?\b", t):
        return "list_devices", ({"filter": "tpms"} if _VEHICLE_WORD_RX.search(t) else {})
    watch = bool(_WATCH_RX.search(t)) and not _QUESTION_RX.search(t) and not stopping
    if not (watch and _route_freq(t) is not None) and _LOGS_RX.search(t) \
            and not _ACT_RX.search(t.replace("check the log", "").replace("check log", "")):
        f, mf = _route_freq(t), _logs_filter(t)
        return "read_background_watchdog", {**({"freq_mhz": f} if f is not None else {}),
                                            **({"model_filter": mf} if mf else {})}
    if not watch and not _ACT_RX.search(t):
        return None
    f = _route_freq(t)
    if f is None:
        return None
    if watch:
        args = {"freq_mhz": f, **dg}
        if _CONTINUOUS_RX.search(t):
            args["mode"] = "continuous"
        mins = _capture_minutes(t)
        if mins:
            args["max_minutes"] = mins
        return "start_background_watchdog", args
    return "investigate_single_frequency", {"frequency_mhz": f, **dg}


_BAND_TOOLS = ("check_and_investigate_band", "investigate_band", "scan_band", "sweep_spectrum", "record_baseline",
               "run_automated_threat_audit", "detect_anomalies", "calibrate_gain", "calibrate_lowest_noise_floor")
_BAND_KEYS = ("frequency_range", "start_mhz", "stop_mhz", "band", "range", "freq_range", "bands", "start_freq",
              "stop_freq", "start", "stop", "end_mhz", "end_freq", "frequency", "freq", "freq_mhz", "frequency_mhz")


def _fix_band_args(name, args, user_text):
    """If the user named a band ('FM band') but the model sent other edges (e.g. 95.3-98.5 MHz copied from saved
    signals), use the whole named band."""
    if name not in _BAND_TOOLS:
        return args
    hit = _named_band(user_text)
    if not hit:
        return args
    (lo, hi), label = hit
    args = _as_dict(args)
    try:
        cur = _resolve_band(args.get("start_mhz") or args.get("start_freq") or args.get("start"),
                            args.get("stop_mhz") or args.get("stop_freq") or args.get("stop") or args.get("end_mhz"),
                            args.get("frequency_range") or args.get("band") or args.get("range"))
    except Exception:
        cur = None
    if cur == (lo, hi):
        return args
    for k in _BAND_KEYS:
        args.pop(k, None)
    args["frequency_range"] = f"{lo:g}-{hi:g}MHz"
    print(f"[HW] Using {label}, as requested.")
    return args


def _fix_watchdog_args(name, args, user_text):
    """A capture the model starts follows the user's words: no band capture unless the user named a band (on the
    real PC 'capture 1090 for 5 minutes' became a 433 band capture copied from earlier in the chat), and the
    frequency the user typed wins over the model's."""
    if name != "start_background_watchdog":
        return args
    args = _as_dict(args)
    t = _user_words(user_text)
    # Anything that may name a band leaves the model's choice alone: band words (also Spanish / plural), a range, or
    # a band number ('record 915 tonight' is a band capture; 'record that remote on 433' may be either).
    if (re.search(r"\bism\b|\bbands?\b|\bbandas?\b|\d\s*(?:-|to|a)\s*\d", t)
            or any(int(x) in _BARE_BAND_NUMBERS for x in re.findall(r"(?<![\d.])(\d{3})(?![\d.])", t))):
        return args
    typed = _typed_freqs(t)                      # only what the user typed: never 'it' / 'that' = the last one
    if len(typed) != 1:
        return args
    f = typed.pop()
    if args.get("band") not in (None, "") or str(args.get("mode") or "").lower() == "band":
        args.pop("band", None)
        args.pop("mode", None)
        print("[HW] No band capture: no band was asked for.")
    try:
        cur = float(args.get("freq_mhz"))
    except (TypeError, ValueError):
        cur = None
    if cur is None or abs(cur - f) > 1e-6:
        args["freq_mhz"] = f
        print(f"[HW] Capturing {f:g} MHz, as asked.")
    return args


def _chat(messages, tools=True, **opts):
    kw = {"model": MODEL, "messages": messages,
          "options": {"num_ctx": NUM_CTX, "temperature": 0.2, "num_predict": REPLY_TOKENS, **opts}}
    if tools:
        kw["tools"] = TOOL_SPECS
    try:
        return _ollama().chat(keep_alive=KEEP_ALIVE, **kw)
    except TypeError:                         # older ollama Python client without keep_alive
        return _ollama().chat(**kw)


def _warm_up() -> None:
    """Load the model and pre-process the system prompt in the background, so the first question is fast."""
    try:
        _chat([{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "Reply OK."}], num_predict=1)
    except Exception:
        pass


def run_turn(messages: list, raw: bool) -> None:
    user_text = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    done = set()                     # (tool, args) already run this turn: never repeat a radio job (15-60 s each)

    def call_key(name, args):
        return name, json.dumps(_as_dict(args), sort_keys=True, default=str)

    route = _route_intent(user_text)
    if route:
        name, args = route
        args = _fix_listen_args(name, args, user_text)
        print(f"[RF Action] {name}({args})")
        result = _dispatch(name, args)
        _show_result(name, result, raw)
        done.add(call_key(name, args))
        if name in _DIRECT:
            try:
                rep = json.loads(result)
                text = (_DIRECT[name](rep) if _direct_ok(name, rep) else
                        f"That did not work: {rep.get('error')}")
            except Exception as e:                   # noqa: BLE001 - a wording bug falls back to the AI's answer
                log.warning("direct reply for %s failed: %s", name, e)
                text = None
            if text:
                messages.append({"role": "assistant", "content": text})
                print(f"\n[ARIS] > {text}")
                return
        messages.append({"role": "assistant", "content": "",
                         "tool_calls": [{"function": {"name": name, "arguments": args}}]})
        messages.append({"role": "tool", "tool_name": name, "content": _for_llm(result)})
    nudge = None

    def finish(text):
        if nudge is not None and nudge in messages:      # the reminder is for this turn only
            messages.remove(nudge)
        print(f"\n[ARIS] > {text}")

    for _ in range(MAX_TOOL_ROUNDS):
        _fit_turn(messages)
        # the radio step's status line is over: the page shows this one while the model writes (1-4 min on a test station)
        print("[AI] Writing the answer..." if messages and messages[-1].get("role") == "tool"
              else "[AI] Reading the question...")
        try:
            res = _chat(messages)
        except Exception as e:
            finish(f"I can't reach the local AI model at {OLLAMA_HOST} ({type(e).__name__}: "
                   f"{str(e)[:120]}). On the computer running ARIS, check it with: systemctl status ollama  and  ollama list")
            return
        msg = _get(res, "message", {})
        messages.append(_msg_to_dict(msg))
        calls = _get(msg, "tool_calls") or []
        content = _get(msg, "content", "") or ""
        if not calls:
            tc = _text_tool_call(content)
            if tc and tc[0] in _TEXT_CALL_OK:
                # The small model sometimes writes the call as text (```json {"name": ...}```) and then makes up
                # a 'Simulated Response': run the call it meant, and never show that text.
                calls = [{"function": {"name": tc[0], "arguments": tc[1]}}]
                messages[-1] = {"role": "assistant", "content": "", "tool_calls": calls}
            elif tc:
                text = f"I did not run that. If you want it, use {_TOOL_WORDS.get(tc[0], 'the matching command')}."
                messages[-1] = {"role": "assistant", "content": text}
                finish(text)
                return
            elif _SIMULATED_RX.search(content):
                messages.pop()                                    # a made-up answer never goes out or into history
                if nudge is None:
                    nudge = {"role": "user", "content": "(ARIS: never simulate, invent or guess tool output. Call "
                                                        "the tool you need, or answer only from the results above.)"}
                    messages.append(nudge)
                    continue
                text = ("I could not answer that from real results. Ask again, or use /status (dongles and what "
                        "they are doing) or /check.")
                messages.append({"role": "assistant", "content": text})
                finish(text)
                return
            else:
                if _get(res, "done_reason") == "length":     # cut off by num_predict: end at a whole sentence
                    content = _whole_sentences(content)
                finish(_plain_reply(content))
                return
        for call in calls:
            fn = _get(call, "function")
            name, args = _get(fn, "name"), _get(fn, "arguments") or {}
            args = _fix_peaks_args(name, _fix_listen_args(name, _fix_band_args(
                name, _fix_sweep_args(name, args, user_text), user_text), user_text), user_text)
            args = _fix_watchdog_args(_canonical(name), args, user_text)
            key = call_key(name, args)
            if key in done:
                messages.append({"role": "tool", "tool_name": name, "content": json.dumps({
                    "status": "ok", "note": "This exact check already ran in this turn; its result is above. Answer "
                                            "from it instead of running it again."})})
                continue
            done.add(key)
            if not _user_asked(_canonical(name), user_text):
                print(f"[RF Action] {name} not run: the user did not ask for it")
                messages.append({"role": "tool", "tool_name": name, "content": json.dumps({
                    "status": "not run", "note": f"{name} changes the station's state and the user did not ask for "
                                                 f"that in their message. Say what you would do and ask them first."})})
                continue
            print(f"[RF Action] {name}({dict(args) if hasattr(args, 'items') else args})")
            result = _dispatch(name, args)
            _show_result(name, result, raw)
            messages.append({"role": "tool", "tool_name": name, "content": _for_llm(result)})
    finish("Stopped after too many tool rounds; ask again more specifically.")


# Tools that change something: the model may call one only when the user's own words asked for that kind of
# action (the v4.9.4 security review: everything the model reads as a tool result, including RDS names and
# device names heard over the air and the saved records the GUI appends, could otherwise talk it into forgetting a
# dongle or running setup). `_user_words` strips the GUI's appended context, so only the typed message counts.
_STATE_TOOLS = {
    "auto_setup": re.compile(r"\bset\s?-?up\b|\bcalibrat|\bconfigur"),
    "calibrate_gain": re.compile(r"\bcalibrat|\bset\s?-?up\b|\b(?:adjust|change|set|find|lower|raise|fix|choose|pick)\b.{0,40}\bgain"),
    "calibrate_frequency": re.compile(r"\bcalibrat|\bset\s?-?up\b|\b(?:adjust|change|set|find|measure|fix)\b.{0,40}"
                                     r"\b(?:ppm|frequency\s+(?:correction|error|offset))"),
    "forget_dongle": re.compile(r"\b(?:forget|remove|drop|delete)\b.*\bdongle|\bdongle\b.*\b(?:forget|remove|drop|delete)\b"),
    "record_baseline": re.compile(r"\b(?:record|take|make|redo|re-?record|new|fresh|update)\b.{0,40}\bbaseline|"
                                  r"\bbaseline\b.{0,20}\b(?:again|now|please)\b|^\W*baseline"),
    "start_patrol": re.compile(r"\b(?:start|begin|run|launch|turn\s+on|enable|resume|switch\s+on)\b.{0,30}\bpatrol|"
                               r"\bpatrol\b.{0,20}\b(?:on|now|please|start)\b|^\W*patrol\W*$"),
    "stop_patrol": re.compile(r"\b(?:stop|end|pause|cancel|turn\s+off|disable|switch\s+off|halt)\b.{0,30}\bpatrol|"
                              r"\bpatrol\b.{0,20}\b(?:off|stop)\b"),
    "start_background_watchdog": re.compile(r"\b(?:captur|monitor|watch|record|graba|keep|listen|log(?:ging)?\s+"
                                            r"(?:everything|all|it|them|that|the|on|what)|log\s+\d)"),
    "stop_background_watchdog": re.compile(r"\b(?:stop|cancel|end|halt|abort|det[eé]n)"),
    "assign_dongles": re.compile(r"\bassign|\buse\s+dongle|\bdongle\s+\d\s+(?:for|on)\b|\bdefault\s+dongle|\bswap|"
                                 r"\bswitch\b.{0,30}\bdongle"),
    "set_antenna_note": re.compile(r"\bantenna"),
    "set_timing_defaults": re.compile(r"\btiming|\bdefault\s+(?:time|listen|sweep|dwell|audit|baseline|seconds|minutes)|"
                                      r"\b(?:always|from\s+now\s+on|every\s+time)\b.{0,40}\b(?:listen|sweep|audit|seconds|"
                                      r"minutes)|\b(?:listen|sweeps?|audits?)\b.{0,30}\b(?:always|from\s+now\s+on|by\s+default)"),
    "perform_system_maintenance": re.compile(r"\bmaintenance|\btrim\b|\bclean\s*(?:up|out)?\b|\btidy|\bdelete\s+old|"
                                             r"\bremove\s+old"),
    "locate_signal": re.compile(r"\blocat|\bwhere\s+(?:is|are|does|do|was|did)\b|\bwhich\s+room|\bfind\b.{0,40}"
                                r"\b(?:source|transmitter|where|it|position|location)"),
    "run_automated_threat_audit": re.compile(r"\baudit|\bthreat|\bcompare|\bbaseline|\bnew\s+signals?|\banything\s+new"),
}
# Tools that change the set-up never run on a question ('what does setup do?') or a negated request ('don't run
# setup'); captures, locating and audits are asked for as questions too ('can you capture 433.92?'), so they get
# only the negation rule (v4.9.5 review: 'what gain is dongle 1 on?' let calibrate_gain run).
_SETUP_TOOLS = _ADMIN_TOOLS
_ASKED_QUESTION_RX = re.compile(r"\bhow\b|\bwhy\b|\bwhat\b|\bexplain\b|\bshould\b|\bwhen\b|\bdid\b|\bdoes\b|"
                                r"\bis\b.{0,30}\?|\bare\b.{0,30}\?|\bresults?\b|\breport\b|\bshow\s+me\b")


def _user_asked(name, user_text) -> bool:
    """May the model run this tool now? Reading and measuring tools always; a state-changing tool only when the
    user's own typed words asked for that kind of action, not negated, and (for the set-up tools) not a question."""
    rx = _STATE_TOOLS.get(name)
    if rx is None:
        return True
    t = _user_words(user_text)
    if not rx.search(t) or _NEGATED_RX.search(t):
        return False
    if name in _SETUP_TOOLS and _ASKED_QUESTION_RX.search(t):
        return False
    return True


# Tools the small model may call by writing the call as text: those that only read or measure. The rest change
# something (setup, baselines, patrol, captures, config) and need a real request.
_TEXT_CALL_OK = {"system_status", "patrol_status", "read_background_watchdog", "list_saved_signals",
                 "get_last_investigation", "get_setup_report", "list_baselines", "list_nodes", "check_system",
                 "run_decoder_selftest", "investigate_single_frequency", "sweep_spectrum",
                 "check_and_investigate_band", "locate_signal", "run_automated_threat_audit", "list_old_files"}
_SIMULATED_RX = re.compile(r"\bsimulat\w*\s+(?:a\s+|the\s+|its\s+)?(?:response|output|result|data|status|reply)|"
                           r"#+\s*simulated\b|\bhypothetical\s+(?:response|output|result)|"
                           r"\b(?:here\s+is|this\s+is)\s+(?:an?\s+)?(?:example|sample)\s+(?:response|output)\b", re.I)


def _text_tool_call(content):
    """A tool call the model wrote as text instead of calling it: (tool, args) or None. Accepts {"name": ...,
    "arguments": {...}} (also "parameters" / "args", or nested in "function"), with the tool's own name or the
    command _plain_reply would show for it ('/status')."""
    text = str(content or "")
    if "{" not in text or '"name"' not in text and "'name'" not in text:
        return None
    slash = {v: k for k, v in _TOOL_WORDS.items() if v.startswith("/")}
    dec = json.JSONDecoder()
    i = text.find("{")
    while i != -1:
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            fn = obj.get("function") if isinstance(obj.get("function"), dict) else obj
            name = fn.get("name") or fn.get("tool")
            args = next((fn[k] for k in ("arguments", "parameters", "args") if isinstance(fn.get(k), dict)), {})
            if isinstance(name, str):
                n = name.strip().strip("`").lower()
                tool = n if n in TOOLS else slash.get(n)
                if tool:
                    return tool, args
        i = text.find("{", end)
    return None


def _whole_sentences(text) -> str:
    """A reply cut off by the token limit ('... New Signal: Yes, Risk') ends at its last whole sentence."""
    t = str(text or "").rstrip()
    note = " (The answer was cut short; ask for the rest.)"
    if t.endswith((".", "!", "?", ")")):
        return t
    cut = max(t.rfind(". "), t.rfind(".\n"), t.rfind("! "), t.rfind("? "), t.rfind("\n"))
    if cut < len(t) * 0.25:                    # no whole sentence near the end: keep it all, marked as cut
        return t + " ..." + note
    return t[:cut + 1].rstrip() + note


_EVIDENCE_TEXT = {"confirmed_decode": "Identified by a decoder", "signal_present": "Heard, but no decoder identified it",
                  "none": "Nothing transmitted during the check"}


def _investigation_card(res) -> dict:
    """An investigation result for people: verdict, how sure, who heard it. Used by /investigate and the Spectrum
    tab's Decode button."""
    rows = []
    for r in res.get("dongle_comparison") or []:
        if r.get("error"):
            # (the error's own brackets stay as they are: never a bracket inside a bracket)
            rows.append(f"{r['dongle']}: not used: {_cut_at_word(r['error'], 80)}")
            continue
        snr = r.get("snr_db") if r.get("snr_db") is not None else r.get("rtl_433_snr_db")
        if r.get("heard_by_rtl433") and r.get("rtl_433_snr_db") is not None:
            snr = r["rtl_433_snr_db"]                 # only rtl_433 heard it: its level, not the quiet look's
        txt = f"{r['dongle']}: " + (("heard it, " + _above_noise(snr)) if r.get("heard") and snr is not None
                                    else "heard it" if r.get("heard") else "did not hear it")
        if r.get("rtl_433_packets"):
            txt += f", {_plural(int(r['rtl_433_packets']), 'packet')} decoded"
        if r.get("adsb_verified"):                    # 1090 MHz: what the ADS-B decoder verified on this dongle
            txt += (f", {int(r.get('aircraft') or 0)} aircraft from "
                    f"{_plural(int(r['adsb_verified']), 'verified message')}")
        elif r.get("adsb_messages") and not r.get("heard"):
            n = int(r["adsb_messages"])
            txt += f" ({n} unverified {'reply' if n == 1 else 'replies'} or noise)"
        if r.get("dongle") == res.get("strongest_dongle") and r.get("heard"):
            txt += " (strongest)"
        rows.append(txt)
    s2 = res.get("stage2_rtl433_telemetry") or {}
    s1 = res.get("stage1_iq_physical_and_bitstream") or {}
    return {"freq_mhz": res.get("frequency_mhz"), "measured_mhz": res.get("measured_carrier_mhz"),
            "verdict": res.get("verdict_core") or res.get("final_signal_verdict"), "evidence": res.get("evidence"),
            "evidence_text": _EVIDENCE_TEXT.get(res.get("evidence"), ""), "listened_seconds": res.get("listened_seconds"),
            "band": res.get("probable_allocation") or _classify_frequency_allocation(float(res.get("frequency_mhz") or 0), 25.0),
            "dongles": rows, "antenna_note": res.get("antenna_note"),
            "devices": [_device_line(d) for d in res.get("identified_devices") or []],
            "unknown_signals": [_fp_line(f) for f in s2.get("unknown_signal_fingerprints") or []],
            "saved": bool((res.get("stage4_plain_english") or {}).get("saved_to_signal_list")),
            "decoders": res.get("decoders_used") or [],
            "station_name": s1.get("rds_station_name") if res.get("broadcast_station_confirmed") else None,
            "station_channel": round(float(s1.get("measured_carrier_mhz") or res.get("frequency_mhz") or 0), 1)}


def _above_noise(snr) -> str:
    """'12 dB above noise' / 'at the noise level' (v4.8.6 printed '+-3 dB')."""
    v = float(snr)
    return f"{v:.0f} dB above noise" if v >= 0.5 else "at the noise level"


def _and_list(items) -> str:
    """'Dongle 1', 'Dongle 1 and Dongle 2', 'Dongle 1, Dongle 2 and Dongle 3'."""
    items = [str(x) for x in items if x]
    if not items:
        return ""
    return ", ".join(items[:-1]) + (" and " if len(items) > 1 else "") + items[-1]


_DECODER_WORDS = {"rtl_433": "rtl_433", "rtl_adsb": "the ADS-B decoder", "multimon-ng": "the pager and tone decoders",
                  "atest": "the APRS decoder", "direwolf": "the APRS decoder"}


def _decoders_phrase(res) -> str:
    """' with rtl_433' / ' with the pager, APRS and tone decoders' / ' with the FM and RDS decoder' (an FM look),
    or '' when only the IQ analysis ran."""
    words = []
    for d in res.get("decoders_used") or []:
        w = _DECODER_WORDS.get(str(d))
        if w and w not in words:
            words.append(w)
    if "the pager and tone decoders" in words and "the APRS decoder" in words:
        words = [w for w in words if w not in ("the pager and tone decoders", "the APRS decoder")] \
            + ["the pager, APRS and tone decoders"]
    if not words and res.get("band_type") == "wide" and _in_fm_band(float(res.get("frequency_mhz") or 0)):
        words = ["the FM and RDS decoder"]
    return f" with {_and_list(words)}" if words else ""


def _next_step(res, verdict) -> str:
    """What to do when nothing was identified: a capture (say "capture" and the frequency) or a timed listen."""
    f = float(res.get("frequency_mhz") or 0)
    kind, ev = res.get("band_type"), res.get("evidence")
    if "Decoding was not possible" in str(verdict):
        return "Run /check to see why the decoder could not run."
    if kind == "adsb":
        return f'To wait for aircraft in the background, say "capture {f:g}".'
    if kind == "wide" and _in_fm_band(f):
        if ev == "none":
            return (f'To look again for longer, say "investigate {f:g} for 30 seconds"; "capture {f:g}" waits in the '
                    f"background for a station name.")
        s1 = res.get("stage1_iq_physical_and_bitstream") or {}
        if s1.get("on_standard_fm_channel") is False or "OFF the broadcast channel plan" in str(verdict):
            # a bug, a microphone or a car transmitter sends no RDS name: the useful step is finding it
            return (f'To find where it is, say "where is {f:g} coming from" (it needs antennas on the Map), or '
                    f'"investigate {f:g} for 30 seconds" for a longer look now.')
        return (f'To read its RDS name, say "capture {f:g}" (it waits in the background), or "investigate {f:g} for '
                f'30 seconds" for a longer look now.')
    if kind == "air":
        return f'To wait for the next transmission, say "capture {f:g}".'
    if ev == "none" and kind == "sensor":
        return (f'Sensors and remotes send short bursts now and then: say "capture {f:g}" to wait for one in the '
                f'background, or "investigate {f:g} for 60 seconds" to listen longer now.')
    if ev == "none":
        return (f'To wait for the next transmission in the background, say "capture {f:g}"; to listen longer now, '
                f'"investigate {f:g} for 60 seconds".')
    return (f'To identify it, say "capture {f:g}": the dongle that hears it best keeps listening until a packet '
            f'decodes. Or say "investigate {f:g} for 60 seconds" for a longer look now.')


def _investigation_text(res) -> str:
    """An investigation's result as the chat reply, without the AI round (also /investigate and 'plain_summary' of
    get_last_investigation): the frequency, the band plan, how long it listened and on what, the verdict
    ('Identified: ...' / 'Not identified ...'), each dongle's row, the RDS name, the receiver-clock note, whether it
    was saved to Signals, and what to do next when nothing was identified. Plain words only: no result field names,
    no bracket inside a bracket. (v4.8.7 and before: the 7B model wrote this reply in 1-4 min on a test station, at times
    with raw fields.)"""
    c = _investigation_card(res)
    f = float(c["freq_mhz"] or 0)
    ev = res.get("evidence")
    heads = [f"{f:g} MHz, listened {c['listened_seconds'] or 0} s"
             + (f" on {_and_list(res.get('listened_with') or [])}" if res.get("listened_with") else "")
             + _decoders_phrase(res) + "."]
    if c["band"]:
        heads.append(f"Band plan: {_sentence(c['band'])}")
    lines = [" ".join(heads)]
    verdict = str(c["verdict"] or "").strip()
    if ev == "signal_present" and not verdict.lower().startswith(("not identified", "decoding was not possible")):
        # an FM station heard without a name, an airband or voice transmission: heard, but no decoder named it
        verdict = "Heard, not identified by a decoder: " + _lc_first(verdict)
    lines.append(_sentence(verdict))
    name = c.get("station_name")
    if name and f'"{name}"' not in verdict:
        lines.append(f'RDS station name: "{name}".')
    lines += [_sentence(d) for d in c["dongles"]]
    note = str(c["antenna_note"] or "")
    if "not mean" in note or "same frequency" in note:
        lines.append(note)
    ch = res.get("possible_clock_harmonic") or {}
    if ch.get("meaning"):
        lines.append(_sentence(str(ch["meaning"])[:1].upper() + str(ch["meaning"])[1:])
                     + (f" To check, {_sentence(ch['how_to_check'])}" if ch.get("how_to_check") else ""))
    s4 = res.get("stage4_plain_english") or {}
    if s4.get("saved_to_signal_list"):
        lines.append("Saved to the Signals list.")
    elif s4.get("note"):
        lines.append(re.sub(r"^Not saved to the signal list \((.+)\)\.?$", r"Not saved to the Signals list: \1.",
                            str(s4["note"])))
    elif s4.get("db_error"):         # (the detail is in the log; never a Python class name in a reply)
        lines.append("Not saved to the Signals list: the Signals database could not be written (see the log).")
    if ev != "confirmed_decode":
        lines.append(_next_step(res, verdict))
    return "\n".join(lines)


def _capture_text() -> str:
    run, done = _running_watchdogs(), _recent_captures(5)
    lines = []
    for w in run:
        what = (f"Recording the {w.get('band_label')} on {w['dongle']}" if w.get("mode") == "band" else
                f"Logging {w['freq_mhz']:g} MHz continuously on {w['dongle']} with {_with_decoder(w.get('decoder'))}"
                if w.get("mode") == "continuous" else
                f"Capturing {w['freq_mhz']:g} MHz on {w['dongle']} with {_with_decoder(w.get('decoder'))}")
        lines.append(f"{what}, for {w.get('minutes')} min so far: {w.get('status')}.")
    for c in done:              # each verdict says what it got (identified, captured, nothing, stopped, failed)
        name = _capture_name(c)
        lines.append(f"{c.get('finished')}: {name[0].upper() + name[1:]}"
                     + (f", after {_took(c['seconds'])}" if c.get("seconds") is not None else "")
                     + f". {_sentence(c.get('verdict'))}")
    return "\n".join(lines) or "No captures yet. Start one with: /capture <frequency> [minutes]"


def _band_scan_text(rep) -> str:
    """A band scan's result as the chat reply, without the AI round: what was swept and on what, how many
    signals, then one paragraph per investigated signal (its verdict, which dongle heard it best, saved or not),
    the signals not investigated, the FM channel-plan note, dongle errors, and the limits line."""
    used = rep.get("dongles_used") or {}
    names = _and_list(list(used)) if used else "the dongles"
    mode = str(rep.get("sweep_mode") or "")
    how = (re.sub(r"^timed peak-hold sweep, (\d+) s.*$", r"watched for \1 s", mode) if mode.startswith("timed")
           else "one quick pass" if mode else "")
    n_found, inv = int(rep.get("total_real_peaks_found") or 0), rep.get("investigated_peaks") or []
    band = re.sub(r"(\d+)\.0\b", r"\1", str(rep.get("band_swept_mhz") or "the band"))      # 433.0-435.0 -> 433-435
    lines = [f"Scanned {band} on {names}" + (f" ({how})" if how else "")
             + f": {_plural(n_found, 'signal')} above the noise"
             + (f", {len(inv)} investigated." if inv else ".")]
    if not n_found:
        lines.append("Nothing was transmitting above the noise while it looked.")
    mhz = lambda v: f"{float(v):g} MHz" if isinstance(v, (int, float)) else "that frequency"
    for r in inv:
        f = r.get("frequency_mhz")
        if r.get("investigation_error"):
            lines.append(f"{mhz(f)}: could not be investigated: {_cut_at_word(str(r['investigation_error']), 120)}")
            continue
        c = _investigation_card(r)
        snr, best = r.get("snr_db"), r.get("strongest_dongle")
        bits = ([_above_noise(snr)] if isinstance(snr, (int, float)) else []) + ([f"strongest on {best}"] if best else [])
        head = mhz(f) + (f" ({', '.join(bits)})" if bits else "")
        verdict = _sentence(str(c.get("verdict") or "").strip()) if c.get("verdict") else "No verdict."
        extra = []
        name = (r.get("stage1_iq_physical_and_bitstream") or {}).get("rds_station_name")
        if name and r.get("broadcast_station_confirmed") and str(name) not in verdict:
            extra.append(f'RDS station name "{name}".')
        s4 = r.get("stage4_plain_english") or {}
        if s4.get("saved_to_signal_list"):
            extra.append("Saved to the Signals list.")
        lines.append(f"{head}: {verdict}" + (" " + " ".join(extra) if extra else ""))
    rest = rep.get("peaks_not_investigated") or []
    if rest:
        lines.append("Seen but not investigated: " + ", ".join(
            mhz(p.get("frequency_mhz")) + (f" ({_above_noise(p['snr_db'])})" if isinstance(p.get("snr_db"), (int, float)) else "")
            for p in rest) + '. Say "investigate everything" to include them, or name one.')
    off = rep.get("fm_signals_off_channel_plan_mhz")
    if off:
        lines.append("Off the FM channel plan (not a normal station): " + ", ".join(f"{x:g} MHz" for x in off) + ".")
    arts = rep.get("rejected_artifacts") or []
    if arts:
        lines.append(f"{_plural(len(arts), 'receiver artifact')} filtered out (the dongles' own spikes).")
    for e in rep.get("dongle_errors") or []:
        lines.append(f"Not used: {_cut_at_word(str(e), 120)}")
    lines.append(_sentence(str(rep.get("limits") or "")) if rep.get("limits") else "")
    return "\n".join(x for x in lines if x)


def _direct_ok(name, rep) -> bool:
    """A routed tool's result that can be put into words directly: status 'ok', or a band scan's own status line."""
    if rep.get("status") == "ok":
        return True
    return name == "check_and_investigate_band" and not rep.get("error") and "investigated_peaks" in rep


_DIRECT = {     # routed requests whose result is already plain English: answered without the AI round
    "check_and_investigate_band": lambda r: _band_scan_text(r),           # (the AI took minutes per scan)
    "check_system": lambda r: r.get("summary") or r.get("message"),
    "auto_setup": lambda r: _setup_summary(r),
    "start_background_watchdog": lambda r: r.get("message"),
    "stop_background_watchdog": lambda r: r.get("message"),
    "forget_dongle": lambda r: r.get("message"),
    "system_status": lambda r: _status_reply(r),
    "patrol_status": lambda r: _patrol_reply(r),
    "read_background_watchdog": lambda r: _logs_reply(r),
    "list_devices": lambda r: _devices_reply(r),
    "wifi_networks": lambda r: r.get("answer"),                          # (v4.13.0: from the Wi-Fi list)
    "investigate_single_frequency": lambda r: _investigation_text(r),   # (the radio takes seconds; the model minutes)
}


_TOOL_WORDS = {   # what to type instead of a tool name the small model sometimes puts in its replies
    "start_background_watchdog": 'a capture (say "capture" and the frequency)',
    "capture_signal": 'a capture (say "capture" and the frequency)',
    "start_capture": 'a capture (say "capture" and the frequency)',
    "monitor_frequency": 'a capture (say "capture" and the frequency)',
    "stop_background_watchdog": '"stop the capture"', "read_background_watchdog": '"check the logs"',
    "investigate_single_frequency": 'an investigation (say "investigate" and the frequency)',
    "investigate_frequency": 'an investigation (say "investigate" and the frequency)',
    "identify_signal": 'an investigation (say "investigate" and the frequency)',
    "check_and_investigate_band": 'a band scan (e.g. "scan the 433 band")',
    "scan_band": 'a band scan (e.g. "scan the 433 band")', "investigate_band": 'a band scan (e.g. "scan the 433 band")',
    "sweep_spectrum": 'a sweep (e.g. "sweep 88-108 MHz")', "check_system": "/check", "auto_setup": "/setup",
    "get_setup_report": '"show the setup report"', "record_baseline": '"record a baseline"',
    "run_automated_threat_audit": '"run a threat audit"', "start_patrol": "/patrol start",
    "stop_patrol": "/patrol stop", "patrol_status": "/patrol", "locate_signal": '"where is" and the frequency',
    "list_saved_signals": "the Signals tab", "list_devices": "the Devices tab",
    "wifi_networks": "the Devices tab (Wi-Fi)", "system_status": "/status",
    "run_decoder_selftest": "/selftest",
    "get_last_investigation": '"explain the last investigation"', "list_nodes": "/nodes",
}
# (a plural after the name or its backtick goes too: v4.8.6 left 'a capture (say "capture" and the frequency)s')
_TOOL_WORDS_RX = re.compile(r"(?:\b(?:the|a)\s+)?`?\b(" + "|".join(sorted(_TOOL_WORDS, key=len, reverse=True))
                            + r")(?:e?s)?\b(?:\(\))?`?(?:e?s\b)?(?:\s+(?:command|tool|function)s?)?", re.I)


_RAW_WORDS = {   # raw result fields and values the small model copies into replies (real PC, v4.8.0: "The broadcast
    # station confirmed this is a public radio station (confirmed_decode).") -> plain words. All have an underscore,
    # so ordinary words are never touched; program names (rtl_433, rtl_adsb) are not in the list.
    "confirmed_decode": "confirmed by a decoder", "signal_present": "heard, but not identified by a decoder",
    "signal_present_during_capture": "signal heard", "strongest_dongle": "strongest dongle",
    "dongle_comparison": "dongle comparison", "investigated_by_dongle": "dongle that investigated it",
    "rejected_artifacts": "signals rejected as receiver artifacts",
    "possible_clock_harmonic": "possible harmonic of the dongle's own clock", "probable_allocation": "band plan",
    "pattern_hint": "pattern match", "broadcast_station_confirmed": "broadcast station check",
    "rds_station_name": "RDS station name", "on_standard_fm_channel": "station channel check",
    "final_signal_verdict": "verdict", "verdict_core": "verdict", "verdict_note": "note",
    "physical_waveform_classification": "signal type", "measured_carrier_mhz": "measured frequency",
    "identified_devices": "identified devices", "unknown_signal_fingerprints": "unknown signals",
    "earlier_checks_of_this_frequency": "earlier checks", "saved_to_signal_list": "saved to the Signals list",
    "save_to_signal_list": "saved to the Signals list", "setup_done": "setup", "listen_seconds": "listening time",
    "listened_seconds": "listening time", "sweep_seconds": "sweep time", "dwell_seconds": "time per signal",
    "max_peaks_to_investigate": "number of signals to investigate", "frequency_range": "frequency range",
    "public_broadcast": "public broadcast", "watchdogs_running": "running captures",
    "recent_captures": "recent captures", "decoded_devices": "decoded devices",
    "undecoded_signals": "undecoded signals", "why_this_dongle": "why this dongle", "antenna_note": "antenna note",
    "channel_snr_db": "level above noise", "snr_db": "level above noise",
    "stage1_iq_physical_and_bitstream": "signal measurement", "stage2_rtl433_telemetry": "rtl_433 results",
    "stage3_multimon_direwolf_audio": "pager and tone decoder results", "stage4_plain_english": "summary",
}
_RAW_EVIDENCE = {"confirmed_decode": "a decoder's confirmation", "signal_present": "a signal no decoder identified"}
_RAW_FLAGS = {   # "broadcast_station_confirmed is true" -> words of its own
    "broadcast_station_confirmed": {"true": "it is a confirmed broadcast station",
                                    "false": "it is not a confirmed broadcast station"},
    "on_standard_fm_channel": {"true": "it is on a station channel", "false": "it is off the station channels"},
    "signal_present_during_capture": {"true": "a signal was heard", "false": "no signal was heard"},
    "public_broadcast": {"true": "it is a public broadcast", "false": "it is not a public broadcast"},
    "setup_done": {"null": "setup has not been run yet", "none": "setup has not been run yet"},
}
_Q = r"""(?P<q>[`'"]?)"""                      # a raw word in quotes or backticks loses them too
_RAW_ALT = "|".join(sorted(_RAW_WORDS, key=len, reverse=True))
_RAW_PAREN = (r"\(\s*(?:evidence\s*(?:[:=]|is)?\s*)?" + _Q + r"\b(?:" + _RAW_ALT + r")\b(?P=q)"
              r"""(?:\s*(?:[:=]|is)\s*[`'"]?(?:true|false|null|none)\b[`'"]?)?\s*\)"""
              r"""|\(\s*evidence\s*(?:[:=]|is)?\s*[`'"]?none\b[`'"]?\s*\)""")
# at the start of a line the spaces after it go with it, elsewhere the spaces before it
_RAW_PAREN_RX = re.compile(r"(?m)^[ \t]*(?:" + _RAW_PAREN + r")[ \t]*|[ \t]*(?:" + _RAW_PAREN.replace("?P<q>", "?P<q2>")
                           .replace("(?P=q)", "(?P=q2)") + r")", re.I)
_RAW_EVIDENCE_RX = re.compile(_Q + r"\b(confirmed_decode|signal_present)\b(?P=q)\s+evidence\b", re.I)
_RAW_FLAG_RX = re.compile(r"(?:\bthe\s+)?" + _Q + r"\b(" + "|".join(_RAW_FLAGS) + r")\b(?P=q)\s*(?:[:=]|is|was)\s*"
                          r"""[`'"]?(true|false|null|none)\b[`'"]?""", re.I)
_RAW_WORD_RX = re.compile(_Q + r"\b(" + _RAW_ALT + r")\b(?P=q)", re.I)


def _same_case(src, text) -> str:
    """text with a capital first letter when src had one (a raw word at the start of a sentence)."""
    src = str(src).lstrip("`'\"")
    return text[0].upper() + text[1:] if src[:1].isupper() and text else text


def _flag_words(m) -> str:
    words = _RAW_FLAGS[m.group(2).lower()].get(m.group(3).lower())
    return _same_case(m.group(0), words) if words else m.group(0)


def _plain_raw(text) -> str:
    """Raw result fields and values in an AI reply -> plain words: one alone in parentheses is removed
    ('a public radio station (confirmed_decode).' -> 'a public radio station.'), any other becomes its plain words.
    'none' is an ordinary word: only '(evidence: none)' goes."""
    t = _RAW_PAREN_RX.sub("", str(text or ""))
    t = _RAW_EVIDENCE_RX.sub(lambda m: _same_case(m.group(0), _RAW_EVIDENCE[m.group(2).lower()]), t)
    t = _RAW_FLAG_RX.sub(_flag_words, t)
    return _RAW_WORD_RX.sub(lambda m: _same_case(m.group(0), _RAW_WORDS[m.group(2).lower()]), t)


def _plain_reply(text) -> str:
    """The AI's reply as the user should read it: internal tool names become what to type or say, and raw result
    fields and values (confirmed_decode, strongest_dongle ...) plain words."""
    return _TOOL_WORDS_RX.sub(lambda m: _TOOL_WORDS[m.group(1).lower()], _plain_raw(text))


def _coerce(v: str):
    try:
        return json.loads(v)
    except ValueError:
        return v


_BAND_LABELS = {"433-435": "433 MHz", "902-928": "915 MHz", "88-108": "FM"}


def _hears(rec) -> str:
    return ", ".join(f"{_BAND_LABELS.get(b, b)} {v.get('signals', 0)}" for b, v in rec.items())


def _dongle_line(serial, r) -> str:
    """One plain-English line per dongle for the setup summary."""
    name = _short(serial)
    if r.get("present") is False:
        return f"{name}: not detected"
    usb = r.get("usb_test") or {}
    tuner = f" ({usb['tuner']})" if usb.get("tuner") else ""
    bits = ([f"USB ok{tuner}"] if usb.get("ok") else ["USB test skipped"] if usb.get("ok") is None
            else [f"USB PROBLEM{tuner}: " + "; ".join(usb.get("problems", []))])
    if r.get("ok") is False and r.get("note"):
        return f"{name}: " + ", ".join(bits + [r["note"]])
    fr = r.get("frequency") or {}
    bits.append(f"{fr['ppm']} ppm (measured on {fr['station_mhz']} MHz)" if fr.get("ok")
                else "frequency correction not measured")
    g = [f"{lab} {r[k]['gain_db']}" for k, lab in (("gain_433", "433"), ("gain_915", "915"), ("fm_gain", "FM"))
         if (r.get(k) or {}).get("ok")]
    if g:
        bits.append("gains " + " / ".join(g) + " dB")
    if r.get("reception"):
        bits.append("signals heard: " + _hears(r["reception"]))
    r433 = r.get("rtl_433") or {}
    if r433.get("ok") is not None:
        bits.append("rtl_433 works with it" if r433["ok"] else f"rtl_433 PROBLEM: {r433.get('detail')}")
    return f"{name}: " + ", ".join(bits)


def _setup_summary(rep: dict) -> str:
    if "dongles" not in rep:                                    # report from an older version
        return rep.get("message", "Setup finished") + "."
    lines = [rep.get("message", "Setup finished") + f" in {rep.get('took_seconds', '?')} s."]
    if rep.get("decoders"):
        lines.append(_selftest_line(rep["decoders"]))
    lines += [_dongle_line(s, r) for s, r in rep["dongles"].items()]
    rt = rep.get("routing") or {}
    if rt.get("best_dongle_per_band"):
        lines.append("Best dongle per band: " + ", ".join(f"{_BAND_LABELS.get(b, b)} -> {d}"
                                                           for b, d in rt["best_dongle_per_band"].items()) + ".")
    if rt.get("bands_no_dongle_heard"):
        lines.append("No dongle heard anything in: " + ", ".join(_BAND_LABELS.get(b, b)
                                                                for b in rt["bands_no_dongle_heard"])
                     + " (normal if nothing transmitted during the 30 s).")
    if rt.get("investigators"):
        lines.append(f"Investigations listen on {', '.join(rt['investigators'])} at the same time.")
    ok = [b for b in rep.get("baselines", []) if b.get("ok")]
    if rep.get("baselines"):
        lines.append(f"Baselines recorded: {len(ok)} of {len(rep['baselines'])} (one per band per dongle).")
    for n in rep.get("notes", []):
        lines.append("Note: " + n)
    if rep.get("problems"):
        lines.append("Problems: " + "; ".join(rep["problems"]))
    return "\n".join(lines)


def _status_reply(st: dict) -> str:
    """'what dongles do I have and what are they doing?' / 'status', answered from system_status without the AI
    round. The live station's AI (v4.8.6) wrote the call as text and then a made-up 'Simulated Response' with a
    Dongle 3 that is gone, signals nobody measured and patrol 'Running'."""
    ds = st.get("dongles") or {}
    here = [(n, d) for n, d in ds.items() if d.get("present") is not False]
    gone = [n for n, d in ds.items() if d.get("present") is False]
    lines = [f"ARIS v{st.get('version')} has {len(here)} dongle{'s' if len(here) != 1 else ''}"
             + (" plugged in" if st.get("dongles_found") else "") + ":"]
    best = {}
    for b, n in (st.get("best_dongle_per_band") or {}).items():
        best.setdefault(n, []).append(_BAND_LABELS.get(b, b))
    for n, d in here:
        tuner = f", {d['tuner']}" if d.get("tuner") else ""
        lines.append(f"{n} ({d.get('serial')}{tuner}): {d.get('doing') or 'free'}"
                     + (f"; best for {', '.join(best[n])}" if best.get(n) else "") + ".")
    if gone:
        lines.append(f"Not plugged in: {', '.join(gone)}.")
    pt = st.get("patrol") or {}
    lines.append(("Patrol is running" + (f" on {', '.join(pt.get('dongles') or [])}" if pt.get("dongles") else "")
                  + ".") if pt.get("running") else "Patrol is off.")
    if not st.get("setup_done"):
        lines.append("Setup has not been run yet (/setup).")
    if (st.get("disk") or {}).get("line"):
        lines.append(st["disk"]["line"] + " (the Activity tab's Clear logs button frees the logs).")
    pl = st.get("plugins") or []
    if pl:
        ok = [p["name"] for p in pl if p.get("ok")]
        bad = [p["name"] for p in pl if not p.get("ok")]
        lines.append("Plug-ins: " + (", ".join(ok) or "none loaded")
                     + (f"; not loaded: {', '.join(bad)} (/plugins says why)" if bad else "") + ".")
    return "\n".join(lines)


def _status_summary(st: dict) -> str:
    ai = st.get("ai_model", {})
    ai_txt = ("AI model ready (local, offline)" if ai.get("reachable") and ai.get("model_installed")
              else ("AI model NOT installed: run  ollama pull " + MODEL.lower()) if ai.get("reachable")
              else "AI model NOT reachable: run  systemctl start ollama")
    per = []
    for name, d in st["dongles"].items():
        g = d["gains_db"]
        rec = d["hears_per_band"]
        per.append(f"{name}: gains 433 {g['433_and_other']} / 915 {g['915']} / FM {g['fm']} dB, ppm {d['ppm']}"
                   + (f", signals heard: {_hears(rec)}" if isinstance(rec, dict) else ", not set up")
                   + (f", antenna: {d['antenna_note']}" if d.get("antenna_note") else ""))
    best = ", ".join(f"{_BAND_LABELS.get(b, b)} -> {d}" for b, d in st.get("best_dongle_per_band", {}).items())
    dec = [n for n, ok in st.get("decoders", {}).get("used_by_aris", {}).items() if ok and n != "rtl_fm"]
    return (f"ARIS v{st['version']}. Dongles found: {len(st['dongles_found']) or 'unknown'}"
            + (f" (missing: {', '.join(st['dongles_missing'])})" if st.get("dongles_missing") else "")
            + f". Setup: {st['setup_done'] or 'not done yet (type /setup)'}."
            + (f" Best dongle per band: {best}." if best else "")
            + "\n" + "\n".join(per)
            + f"\nDecoders: {', '.join(dec) or 'none found'}. Baselines: {st['baselines']}. "
              f"Patrol: {'running' if st['patrol'].get('running') else 'off'}. {ai_txt}."
            + (f"\n{st['disk']['line']}." if (st.get("disk") or {}).get("line") else ""))


HELP = """Commands:
  /setup             full setup: finds the dongles (adds new ones), self-tests, calibrates every dongle, baselines,
                     best dongle per band (3-5 min)
  /check             check everything without recalibrating: dongles plugged in (new / missing / same serial),
                     USB and live reception test per dongle, decoders, AI model (about 30 s)
  /investigate <MHz> [time]   investigate one frequency on every dongle, e.g. /investigate 433.92 2m
  /capture <MHz> [minutes]    lock the best dongle on a frequency until it decodes or demodulates a packet
  /capture stop | /captures   stop every capture | list running and recent captures
  /devices [text]    the devices decoded so far (a vehicle's tyre sensors grouped), newest first; text filters
  /status            hardware, calibration, decoders and AI model status (no AI needed)
  /selftest          check every decoder (rtl_433, multimon-ng, atest, RDS) with synthetic test signals (seconds)
  /patrol | /patrol start | /patrol stop   background threat-audit patrol on every dongle
  /clear-signals     empty the Saved Signals list (a backup copy is kept)
  /cleanup           list leftover files from earlier versions; /cleanup confirm deletes them
  /nodes             receivers, where their antennas sit on the floorplan, and calibration (Map tab sets them up)
  /plugins           the plug-ins loaded at start (what each adds, or why one failed) and the receiver types known
  /run <tool> key=value ...    call a tool directly, e.g. /run sweep_spectrum frequency_range=90-100MHz
  /tools  /raw  /reset         list tools, toggle full tool output, clear conversation memory
  exit | quit        leave
Ask in plain English, e.g. "investigate 914.84 MHz for 2 minutes", "find out exactly what it is", "capture it",
"check the logs", "sweep 433 for 60 seconds", "where is 433.92 coming from". The GUI's Activity tab shows patrol
findings and watchdog logs; the Devices tab every decoded device (a car's tyre sensors as one row); the Map tab
places antennas on a floorplan and shows where a signal probably is."""


def _command(low: str, user_input: str, raw: bool):
    """Slash commands. Each ends with an '[ARIS] >' line so the web GUI can show the result.
    Returns None if `low` is not a command, else the summary to add to the AI's conversation ("" = none)."""
    summary = ""
    if low == "/help":
        print(HELP)
        print("\n[ARIS] > " + HELP)
    elif low == "/status":
        res = TOOLS["system_status"]()
        _show_result("system_status", res, raw)
        st = json.loads(res)
        summary = _status_summary(st) if st.get("status") == "ok" else st.get("error", "status failed")
        print("\n[ARIS] > " + summary)
    elif low == "/setup":
        res = TOOLS["auto_setup"]()
        _show_result("auto_setup", res, raw)
        rep = json.loads(res)
        summary = _setup_summary(rep) if rep.get("status") == "ok" else "Setup failed: " + rep.get("error", "")
        print("\n[ARIS] > " + summary)
        summary += " (Full details: get_setup_report.)"
    elif low == "/selftest":
        print("[HW] Testing each decoder with synthetic signals...")
        summary = _selftest_line(_decoder_selftest())
        print("\n[ARIS] > " + summary)
    elif low == "/check":
        rep = json.loads(TOOLS["check_system"]())
        summary = rep.get("summary") if rep.get("status") == "ok" else "Check failed: " + str(rep.get("error"))
        print("\n[ARIS] > " + summary)
    elif low.startswith("/investigate"):
        m = re.match(r"^/investigate\s+(\d+(?:\.\d+)?)\s*(?:mhz)?(?:\s+(\d+(?:\.\d+)?)\s*([a-z]*))?\s*$", low)
        if not m:
            summary = "Usage: /investigate <frequency in MHz> [time], e.g. /investigate 433.92 or /investigate 433.92 2m"
        else:
            secs = None
            if m.group(2):
                secs = float(m.group(2)) * (60.0 if m.group(3).startswith("m") else 1.0)
            res = json.loads(TOOLS["investigate_single_frequency"](frequency_mhz=float(m.group(1)),
                                                                   listen_seconds=int(round(secs)) if secs else None))
            summary = (_investigation_text(res) if res.get("status") == "ok"
                       else "Investigation failed: " + str(res.get("error")))
        print("\n[ARIS] > " + summary)
    elif low in ("/captures", "/capture", "/capture status"):
        summary = _capture_text()
        print("\n[ARIS] > " + summary)
    elif low == "/devices" or low.startswith("/devices "):
        rep = json.loads(TOOLS["list_devices"](filter=low[8:].strip() or None))
        summary = _devices_reply(rep) if rep.get("status") == "ok" else str(rep.get("error"))
        print("\n[ARIS] > " + summary)
    elif low.startswith("/capture"):
        m = re.match(r"^/capture\s+(\d+(?:\.\d+)?)\s*(?:mhz)?(?:\s+(\d+(?:\.\d+)?)\s*([a-z]*))?\s*$", low)
        if low.strip() in ("/capture stop", "/capture stop all"):
            rep = json.loads(TOOLS["stop_background_watchdog"]())
        elif m:
            mins = None
            if m.group(2):
                mins = float(m.group(2)) * (60.0 if m.group(3).startswith("h") else 1.0)
            rep = json.loads(TOOLS["start_background_watchdog"](freq_mhz=float(m.group(1)),
                                                                max_minutes=int(round(mins)) if mins else None))
        else:
            rep = {"status": "error", "error": "Usage: /capture <frequency in MHz> [minutes], /capture stop, /captures"}
        summary = rep.get("message") if rep.get("status") == "ok" else str(rep.get("error"))
        print("\n[ARIS] > " + summary)
    elif low in ("/cleanup", "/cleanup confirm"):
        summary = _cleanup_text(_cleanup(apply=low.endswith("confirm")))
        print("\n[ARIS] > " + summary)
    elif low in ("/patrol", "/patrol status", "/patrol start", "/patrol stop"):
        name = {"/patrol start": "start_patrol", "/patrol stop": "stop_patrol"}.get(low, "patrol_status")
        res = TOOLS[name]()
        _show_result(name, res, raw)
        rep = json.loads(res)
        if rep.get("status") != "ok":
            msg = "Patrol: " + rep.get("error", "failed")
        else:
            msg = (f"Patrol is {'running' if rep.get('running') else 'off'}"
                   + (f" on {rep.get('bands')} with {', '.join(rep.get('dongles', []))} every "
                      f"{rep.get('interval_min')} min; {rep.get('audits_done', 0)} audits done, "
                      f"{rep.get('events_flagged_total', 0)} event(s) flagged." if rep.get("bands") else "."))
        print("\n[ARIS] > " + msg)
        summary = msg
    elif low == "/clear-signals":
        summary = _clear_signals()
        print("\n[ARIS] > " + summary)
    elif low == "/plugins":
        rep = list_plugins()
        lines = [f"{p['name']}: " + (p["about"] or "loaded") if p["ok"] else f"{p['name']}: NOT loaded ({p['error']})"
                 for p in rep["plugins"]] or ["No plug-ins loaded."]
        lines.append("Receiver types: " + ", ".join(rep["receiver_types"]) + ".")
        lines.append("Folders: " + ", ".join(rep["folders"]) + ".")
        summary = "\n".join(lines)
        print("\n[ARIS] > " + summary)
    elif low == "/nodes":
        rep = json.loads(TOOLS["list_nodes"]())
        if rep.get("status") != "ok":
            summary = "Nodes: " + rep.get("error", "failed")
        else:
            lines = [f"Floorplan: {rep['floorplan']} ({rep['plan_size_m'][0]:g} x {rep['plan_size_m'][1]:g} m)"
                     if rep.get("floorplan") else "No floorplan yet (Map tab)."]
            for n in rep["nodes"]:
                pos = (f"at {n['position_m'][0]:.1f}, {n['position_m'][1]:.1f} m" if n["placed_on_active_plan"]
                       else "not placed")
                cal = ", calibrated for " + ", ".join(n["calibrated_bands"]) if n["calibrated_bands"] else ""
                lines.append(f"{n['name']} ({n['kind']}{'' if n['enabled'] else ', off'}): {pos}{cal}")
            summary = "\n".join(lines)
        print("\n[ARIS] > " + summary)
    elif low == "/tools":
        print(", ".join(sorted(set(TOOLS))))
    elif low.startswith("/run "):
        parts = shlex.split(user_input)
        kwargs = {k: _coerce(v) for k, v in (p.split("=", 1) for p in parts[2:] if "=" in p)}
        _show_result(parts[1], _dispatch(parts[1], kwargs), True)
    else:
        return None
    return summary


def _cli_cleanup() -> None:
    res = _cleanup(apply=False)
    print(_cleanup_text(res).replace("Type /cleanup confirm to delete them.", "").rstrip())
    if not res["count"]:
        return
    if "--yes" not in sys.argv:
        try:
            ans = input(f"Delete these {res['count']} file(s)? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans not in ("y", "yes"):
            print("Nothing deleted.")
            return
    print(_cleanup_text(_cleanup(apply=True)))


def _file_log_handler(path: Path = None, max_bytes: int = None, keep: int = None) -> logging.Handler:
    """aris.log's handler: a new file starts at LOG_ROTATE_BYTES and LOG_ROTATE_KEEP old ones are kept (aris.log.1,
    .2), so the text log never grows past about 15 MB. Before v4.8.8 it only grew (trimmed by maintenance alone)."""
    return logging.handlers.RotatingFileHandler(str(path or LOG_FILE), maxBytes=max_bytes or LOG_ROTATE_BYTES,
                                                backupCount=LOG_ROTATE_KEEP if keep is None else keep)


def main() -> None:
    handlers = []
    try:
        handlers.append(_file_log_handler())
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=handlers or [logging.NullHandler()])
    if "--cleanup" in sys.argv:
        _cli_cleanup()
        return
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        upgrade_signal_db()
    except Exception as e:
        log.warning("signal list upgrade skipped: %s", e)
    threading.Thread(target=_warm_up, name="ai-warm-up", daemon=True).start()

    cfg = _load_config()
    ai = _ollama_check()
    ai_txt = ("ready" if ai.get("model_installed") else f"model not installed (ollama pull {MODEL.lower()})") \
        if ai.get("reachable") else "NOT reachable (systemctl start ollama)"
    print("=" * 74)
    print(f"  ARIS v{VERSION} - Multi-SDR Spectrum, Decoder and Audit Station (offline)")
    for ser in DONGLE_NAMES:
        d = _dongle_cfg(ser, cfg)
        rec = d.get("reception")
        print(f"  {_short(ser)}    : gains 433 {_gain_for(ser, cfg=cfg)} / 915 {_gain_for(ser, 902.0, 928.0, cfg)} / "
              f"FM {_gain_for(ser, 88.0, 108.0, cfg)} dB, ppm {_ppm_of(ser, cfg)}"
              + (f", hears {_hears(rec)}" if rec else ", not set up"))
    best = ", ".join(f"{_BAND_LABELS.get(b, b)} -> {_short(s)}" for b, s in cfg["band_dongles"].items())
    print(f"  Best dongle : {best or 'not chosen yet (type /setup)'}")
    print(f"  Data        : {DATA_DIR}")
    print(f"  Timing      : sweeps {str(cfg['default_sweep_seconds']) + ' s' if cfg['default_sweep_seconds'] else 'quick pass'}"
          f" | baselines {cfg['baseline_seconds']} s | audits/patrol {cfg['audit_seconds']} s")
    print(f"  AI model    : {MODEL} at {OLLAMA_HOST} -> {ai_txt}")
    print(f"  Setup       : {cfg.get('setup_done') or 'NOT DONE - type /setup'}")
    print("=" * 74)
    print("Type /help for commands.")

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    raw = False
    while True:
        try:
            user_input = input("\n[You] > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting ARIS console. Stay secure.")
            break
        if not user_input:
            continue
        user_input = _take_role_mark(user_input)         # (the GUI's bridge marks an extra account's lines)
        low = user_input.lower()
        if low in ("exit", "quit"):
            break
        try:
            if _TURN["role"] != "admin" and _ADMIN_COMMAND_RX.match(low):
                print(f"\n[ARIS] > Only the admin account can run {low.split()[0]}.")
            elif low == "/raw":
                raw = not raw
                print(f"Raw output {'ON' if raw else 'OFF'}")
            elif low == "/reset":
                messages = [{"role": "system", "content": SYSTEM_PROMPT}]
                print("Conversation cleared.")
            elif low.startswith("/") and (summary := _command(low, user_input, raw)) is not None:
                if summary:                      # so follow-up questions ("details of the setup?") work
                    messages.append({"role": "user", "content": f"(I ran the {low} command.)"})
                    messages.append({"role": "assistant", "content": summary})
                    _trim_history(messages)
            else:
                messages.append({"role": "user", "content": user_input})
                _trim_history(messages)
                run_turn(messages, raw)
        except KeyboardInterrupt:
            print("\n[Interrupted]")
        except Exception as e:
            log.error("Turn failed:\n%s", traceback.format_exc())
            print(f"\n[ARIS] > Error: {type(e).__name__}: {e}")
