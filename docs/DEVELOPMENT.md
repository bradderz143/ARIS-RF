# Developing ARIS

## Tests

```
bash test/run_all.sh            # everything, on simulated radios (no hardware needed), about 10 minutes
bash test/run_all.sh --shots    # also screenshots at phone and desktop width (needs Playwright and Chromium)
```

`test/fake/bin` has simulated `rtl_*` tools, `SoapySDRUtil` and rx_tools, `multimon-ng`, `atest` and a scripted
Ollama (`test/fake/mods`). The simulated rtl_sdr streams real FM with real RDS, ISM bursts, band-edge roll-off and the
real tools' quirks; the simulated rtl_433 hands recordings to a real rtl_433 (set `REAL_BIN` to the folder that has
one; tests that need it are skipped without it). `python3 test/serve.py [port]` runs the web page on simulated radios
(login `admin` / `test-password-123`).

Tests never reach the network: the harness switches the update check off (`ARIS_UPDATE_CHECK=0`).

Simulated results are not proof on real radios: say so when you report a change, and test on hardware before calling
it done.

## Rules for changes

- **Receive-only, metadata only.** Nothing may transmit, record or transcribe voice, unpack encrypted or private
  traffic, or keep demodulated bits or remote codes. Locate measures signal strength only.
- **Never bypass the receiver locks** (`_use_dongle`), and never add fixed sleeps after radio tools (the lock handles
  settle time).
- **Plain words** in every reply the user reads: no field names, no tool names (`_plain_reply` catches the AI's).
- **Honest verdicts**: "Identified" only from a decoder; a pattern match is a pattern match; locate is a rough area.
- Engine-independent logic goes in `arislib/` with unit tests; new web APIs get a blueprint in `arisweb/`; new
  receiver types and bands are plug-ins where possible.
- Every top-level name in the engine is defined once across `aris.py` and `engine/` (a test checks); never put code
  between a `@register(...)` decorator and its function.
- Keep chat routing (`_route_intent`) and the tool registry in step; a direct reply builder that fails falls back to
  the AI.
- Every file that grows has a cap (`_jsonl_append` with a line limit).
- Version bumps: `VERSION` in `aris.py` and its docstring; the page reads it from the engine.
- Python 3.8 syntax; the CI runs 3.8 and the newest.
- Measure hot paths before and after with `python3 test/bench.py`.
