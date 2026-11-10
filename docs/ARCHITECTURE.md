# How ARIS works

## Processes

- **The web server** (`aris_gui.py` → `arisweb/`, Flask, port 5000) loads the engine in-process (`arisweb/engine.py`).
  Background work that must outlive a chat reset runs here: captures, band captures, Decode and Name-all jobs, the
  Wi-Fi watcher.
- **The chat console** (`aris.py` on a pty, started by `arisweb/bridge.py`) answers the chat. Common requests are
  routed straight to a tool and answered from its result (`_route_intent`, `_DIRECT`); the rest go to the local model
  through Ollama, which may only call ARIS's registered tools. Captures the console starts are run by the web server
  (`/api/internal/watchdog`, a per-start token, localhost only).
- **Helpers** with their own small privileges: `deploy/aris-wifi-scan.sh` (passive Wi-Fi scan, `CAP_NET_ADMIN`
  only) and `deploy/aris-bt-scan.py` (Ubertooth, advertisements only). They write summaries ARIS reads.
- **Remote nodes** (`aris_node.py`, HTTP with a token) measure signal strength for locating.

## The engine

`aris.py` runs the parts under `engine/` into one namespace, in order: `core` (configuration, receivers and their
numbering, the tool registry, locks), `sweep` (rtl_power sweeps, peak clustering, IQ captures), `decode` (IQ analysis,
wideband FM and RDS, rtl_433, audio decoders, self-tests), `investigate`, `fm` (Name all FM stations), `autosetup`
(`/check`, `/setup`, calibration), `audit` (baselines, threat scores, patrol), `logs` (the capture log, the device
history), `captures` (background captures across processes), `band` (band capture), `status`, `chat` (routing, plain
replies, the AI loop) and `plugins`. Every top-level name is defined in one part only (a test checks).

`arislib/` holds the pieces that need no engine (numpy only, each with unit tests): `radios` (receiver backends),
`iqpipe`, `radioproc` (stopping radio programs cleanly), `rtlpower` (rtl_power's own arithmetic), `rds`,
`bandwatch` (burst detection), `adsb` (Mode S CRC), `devices` (the device history's rules), `wireless`, `bluetooth`,
`measure`, `localize`, `nodes`, `floorplan`, `store`, `locate`.

## Receivers and locks

- Every radio command is built rtl-sdr style and turned into the receiver's own by `arislib/radios.py` (see
  [DEVICES.md](DEVICES.md)). Output formats stay the same for every receiver: CU8 IQ, rtl_power CSV, rtl_433 JSON.
- Each receiver has an `fcntl` lock (`_use_dongle`). Everything goes through it: GUI, console, captures, locate,
  Spectrum. A receiver held by a capture in any ARIS process is skipped at once. The lock also enforces a short
  settle time between one program closing a receiver and the next opening it, so no code sleeps after radio tools.
- Stopping a radio program: signal, short grace, SIGKILL, wait until it is gone, and only then release the lock
  (`radioproc`); the pipe is drained meanwhile, because rtl_sdr blocked on a full pipe never handles its signal.
- **Reset Scanner** stops every job, capture and patrol in every ARIS process and ends the radio programs ARIS
  started; a receiver still held by a stuck thread after 10 s makes the web server restart itself.

## Decoding rules

- rtl_433 picks an RTL-SDR by serial only as `-d :SERIAL`; always run with `-F log` (its own errors), stderr merged
  into stdout (the order tells decoded packages from undecoded ones), `-Y autolevel` only (never `-Y minmax`, never
  `-G`). Undecoded packages become fingerprints (modulation, pulse widths, bits per packet); the demodulated bits are
  never kept.
- "Identified" only from a decoder's own result; everything else starts "Not identified" and gives the measurement.
- Remote-control and rolling codes are stripped from every log, reply and record (`_safe_packet`); remotes get a
  keyed label (HMAC with a random per-station secret).
- FM: RDS is decoded by ARIS (subcarrier found and followed, block CRC checked, limited error correction, scrolling
  text recognised). What an IQ look finds on a channel is remembered (`fm_stations.json`).
- Receivers of different kinds tune different ranges (an RTL-SDR 24-1766 MHz, a Signal Hound SA124B to 12.4 GHz):
  every radio command is checked against its receiver's range (`arislib.radios.covers`), multi-receiver work picks
  only receivers that tune the band (`_pick_serials(band=)`), and the Spectrum presets and band names above 1.7 GHz
  appear only when one does. Allocation labels, busy bands (Wi-Fi, Bluetooth, phones: bursts score lower) and the
  GPS band (a steady new signal scores higher: a jammer looks like that) cover up to 12.75 GHz.
- The FM channel-plan check assumes the Americas' plan (stations on odd tenths, 88.1-107.9 MHz) and the threat rules
  know the Americas' bands (902-928 MHz ISM). Elsewhere these hints are wrong: treat off-plan flags accordingly.
- ADS-B: only checksum-verified squitters count as aircraft.
- Audio (pagers, APRS, tones) lives in memory only while the decoders read it; nothing is recorded or transcribed.

## Band capture

A receiver streams a band (2.4 MS/s slices); `BurstDetector` finds bursts in time and frequency (FFT frames against a
slowly followed per-bin floor, FSK tones joined, side-bands and images dropped); each burst is cut from a ring of IQ,
mixed to 0 Hz, filtered to its own width and decoded by rtl_433 in batches. Faint and very wide bursts are counted,
not decoded. Recorded bursts are never kept on disk (a remote's IQ could be replayed).

## Devices, known and passing

Every decoded packet feeds a device history (`devices.json`): one record per model and ID, transmissions, sensors of
one make heard together (a car's tyre sensors), heard-once noise. A device heard on 3 or more days, or marked by the
user, is *known* and folded into one clause in results; tyre sensors of passing cars are grouped by make, never
learned one by one, and never attributed to anyone.

## Threat levels and patrol

Scores from the measured signal (continuous carriers weigh more, bursts in sensor bands less, new to the baseline
adds, an FM carrier off the plan adds): HIGH from 25, MEDIUM from 15. Patrol sweeps against each receiver's own
baseline and investigates HIGH findings once an hour per frequency.

## Locating

Readings: channel power from IQ (the 90th percentile of "on" blocks, noise removed, input-referred, plus a per-node
per-band offset from calibration). `localize.source_map` fits a log-distance model with unknown power and exponent,
counts receivers that missed the signal as upper bounds, and reports the best area, mirror-image alternatives and a
spread. Results are always described as a rough area.

## Security

- The service runs as its own user with `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`.
- Logins: password hashes only (`gui_secrets.json`, mode 600), lockout after failed attempts, extra viewer accounts
  that cannot change settings. Changing APIs need an `X-ARIS: 1` header.
- The AI's state-changing tools run only when the user's own words asked for that action (never on a negated message
  or a question); data heard over the air is never an instruction to the model.
- Device ids from receivers are checked before they become file names, lock names or arguments.

## Update badge

The web server asks the code's own git remote every few hours whether its branch has moved (`git ls-remote` and
`merge-base` only: the service's code folder is read-only, so nothing is fetched or written; logins are never
prompted for and never appear in errors). The page then shows an "update" badge with the command to run on the
computer; the update itself needs root and is never run from the page. `"update_check": false` in `config.json`
or `ARIS_UPDATE_CHECK=0` switches it off.

## Performance

Radio tools write to stdout and are parsed as they stream; config and baselines are cached by file time; the GUI polls
only for changes; the AI's history is trimmed in large steps so the model's prompt cache stays valid. `test/bench.py`
times the hot paths.
