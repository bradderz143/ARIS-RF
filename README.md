# ARIS

**A receive-only radio monitoring station that runs on your own computer. Use it from any browser: on the computer
itself (`http://localhost:5000`), or from a phone, tablet or laptop on your network.**

ARIS watches the radio spectrum with one or more software-defined radios, finds what is transmitting, identifies the
devices a decoder can recognise (weather sensors, tyre-pressure sensors, remotes, FM stations, pagers, aircraft),
records whole ISM bands burst by burst, flags transmitters that are new, and estimates where a transmitter is on a
floorplan. Everything runs offline, including the AI chat (a local model through Ollama); the one thing that reaches
out is a read-only check of its own git repository every few hours, so the page can show an "update" badge (switch it
off with `"update_check": false` in the data folder's `config.json`). It never transmits, and it keeps metadata only.

<p align="center">
  <img src="docs/screenshots/phone-chat.png" width="200" alt="Chat: an investigation and the station's status in plain words">
  <img src="docs/screenshots/phone-spectrum.png" width="200" alt="Spectrum: three receivers overlaid on the FM band">
  <img src="docs/screenshots/phone-devices.png" width="200" alt="Devices: sensors, cars' tyre sensors and remotes">
  <img src="docs/screenshots/phone-activity.png" width="200" alt="Activity: a band capture and its result">
</p>
<p align="center">
  <img src="docs/screenshots/desktop-spectrum.png" width="820" alt="Spectrum on a desktop browser">
</p>

*Screenshots from the built-in simulator (no radios attached), at phone width and on a desktop browser:
`bash test/run_all.sh --shots` makes them again.*

## What it does

- **Spectrum**: a spectrum analyzer in the browser. Pick any range from 0 to 6000 MHz by Start / Stop or Centre /
  Span; zoom and pan with the buttons, the mouse wheel or by dragging across the plot. Every receiver sweeps the part
  of the range it tunes, at the same time, overlaid and each against its own noise floor; a wide range is swept in
  pieces and the picture fills in as they come back. What no receiver here tunes is hatched and labelled, never drawn
  as a level (an RTL-SDR covers 24-1766 MHz; above that the plot says so until a receiver that reaches further is
  plugged in). Step between points, level axis, peak hold, averaging, a marker with Peak, Next peak and Delta, a
  waterfall, signal lists and presets for common bands. True dBFS levels for RTL-SDR dongles and calibrated dBm for
  Signal Hound receivers. The threat audit knows the bands above 1.7 GHz too (a steady signal in the GPS band is
  flagged as a possible jammer).
- **Investigate and decode**: listen on a frequency for up to 10 minutes on every receiver; rtl_433's 200+ device
  decoders, FM station names (RDS, decoded and checksummed by ARIS itself), pager and APRS metadata, ADS-B aircraft
  (checksum-verified only). Anything a decoder does not recognise is described by what was measured, never guessed.
- **Band capture**: records a whole band (433 MHz, 902-928 MHz, or any range up to 60 MHz), finds every burst in time
  and frequency, cuts it out and decodes it, so devices that transmit for a few milliseconds are not missed.
- **Devices**: everything decoded, grouped (a car's tyre sensors as one row, passing cars apart from the ones that
  come back), with "known" devices folded away so new ones stand out. A passive Wi-Fi view and a Bluetooth view
  (with an Ubertooth One) list what access points and advertisers announce.
- **Patrol and threat levels**: scheduled sweeps against each receiver's own baseline; a new continuous transmitter
  is investigated automatically.
- **Locate**: signal-strength readings from several receivers (local dongles or remote nodes on the network) give a
  heat map of where a transmitter probably is, on your own floorplan.
- **Chat**: ask in plain words ("scan the 433 band", "what devices have you seen", "capture 1090"). Common requests
  are answered directly; open questions go to a local AI model that can only use ARIS's own tools.
- **Reset Scanner**: one button stops everything and frees every receiver.

## Receivers

| Kind | How | Status |
|---|---|---|
| RTL-SDR dongles (RTL2832U with R820T / R828D / E4000 ...) | the rtl-sdr tools | used daily |
| Any SoapySDR receiver: HackRF, Airspy, SDRplay, LimeSDR, PlutoSDR, USRP, SoapyRemote ... | SoapySDR + [rx_tools](https://github.com/rxseger/rx_tools) | tested in simulation |
| Signal Hound BB60C / BB60D | the bundled plug-in `plugins/signalhound_bb60.py` with Signal Hound's BB API | tested in simulation |
| Signal Hound SA44B / SA124B (sweeps to 12.4 GHz; narrow IQ) | the bundled plug-in `plugins/signalhound_sa.py` with Signal Hound's SA API | tested in simulation |
| Anything else that can stream IQ | a device plug-in (one Python file) | see [docs/PLUGINS.md](docs/PLUGINS.md) |

Receivers are found when ARIS starts and by `/check`; nothing is hard-coded. Mix and match: each receiver gets its own
gains, frequency correction and baselines, and ARIS works out which one hears each band best. Details:
[docs/DEVICES.md](docs/DEVICES.md).

## Quick start

Try it without any radio, in the simulator:

```
python3 -m venv .venv && . .venv/bin/activate     # Debian 12+ and Ubuntu 23.04+ refuse pip outside a virtual environment
pip install numpy flask werkzeug ollama pytest
python3 test/serve.py 5097          # then open http://127.0.0.1:5097  (login: admin / test-password-123)
```

Install it for real on Debian or Ubuntu with one command. It shows the whole plan first and asks before it installs
anything (only git is installed before the question, when the computer has none):

```
curl -fsSL https://raw.githubusercontent.com/bradderz143/ARIS-RF/main/deploy/get.sh | sudo bash
```

If the computer says that curl is not found (a fresh Ubuntu Desktop or Debian has none), run `sudo apt install curl`
first. In a root shell without sudo, leave out the word `sudo`.

It installs the system's own packages for the radio tools and decoders (rtl-sdr, rtl_433, multimon-ng, direwolf), the
Python modules (the `ollama` module from PyPI with pip where the system has no package for it), Ollama and the AI
model (about 4.7 GB), and ARIS itself as a service that runs as its own unprivileged user `aris`, never as root. At
the end it asks for the web page's login. Ollama comes through its own installer, a third-party script that runs as
root; on a computer with an NVIDIA graphics card and no NVIDIA driver that script also installs NVIDIA's driver. Add
`-s -- --no-ai` after `sudo bash` to leave the AI part out.

The same from a checkout, or step by step on any other Linux with systemd:

```
git clone https://github.com/bradderz143/ARIS-RF.git aris && cd aris
sudo bash deploy/install.sh --everything             # Debian / Ubuntu: dry run, the whole plan, changes nothing
sudo bash deploy/install.sh --everything --apply     # packages, Ollama and the model, the service, the login

sudo bash deploy/install.sh                          # any Linux, tools installed by you: dry run, changes nothing
sudo bash deploy/install.sh --apply                  # installs to /opt/aris, data in /var/lib/aris
sudo -u aris env ARIS_DATA_DIR=/var/lib/aris python3 /opt/aris/aris_gui.py --set-password
```

The one-command install is new: it was run on Ubuntu 24.04 in a container, with stand-ins for systemd and for
Ollama's installer, and not yet on a real Debian or Ubuntu computer.

Open `http://localhost:5000` on the computer itself, or `http://<the computer's address>:5000` from another device
on your network, then in the chat: **/check** (finds and tests the receivers and decoders,
changes nothing) and **/setup** (calibrates each receiver: gains, frequency correction, which one hears which band
best; a few minutes). For the AI chat without the one-command install, install [Ollama](https://ollama.com) and
`ollama pull qwen2.5:7b` (any model with tool calling works; set `ARIS_MODEL`). Updates: `sudo bash /opt/aris/deploy/update.sh` (the page shows an
"update" badge when a newer version is on GitHub).

Keep the web page off your network if you do not need it there: set `ARIS_GUI_HOST=127.0.0.1`, and reach it through
SSH or `deploy/tailscale-https.sh` (HTTPS inside your own tailnet).

**Windows.** ARIS has no Windows version. It runs inside WSL 2 (Ubuntu on Windows 10 / 11), and
`deploy/windows/aris-usb.ps1` hands the receivers from Windows to WSL with
[usbipd-win](https://github.com/dorssel/usbipd-win): with no switch it only lists what it found, `-Attach` does it,
`-Detach` gives them back. [docs/WINDOWS.md](docs/WINDOWS.md) goes step by step from a fresh Windows computer to the
page at `http://localhost:5000`. The guide and the helper are written from the documentation and have not yet been
run on a Windows computer.

## Plug-ins

A plug-in is one Python file in `plugins/` (or `$ARIS_DATA_DIR/plugins/`). It can add a receiver type, chat tools
and decoders, and named bands (scans, band captures and Spectrum presets understand them). Three come with ARIS:
`signalhound_bb60.py` and `signalhound_sa.py` (receiver types) and `ism_868.py` (the 863-870 MHz band, as a short
example). See
[docs/PLUGINS.md](docs/PLUGINS.md).

## Rules it keeps

- **Receive-only.** No feature transmits, and nothing may be added that does.
- **Metadata only.** No voice or audio is recorded, kept or transcribed; decoders get audio in memory and it is
  deleted at once. Encrypted or private traffic is never unpacked.
- **No replayable codes.** Remote-control and rolling codes are stripped from every log, reply and record; remotes
  are shown by a keyed label, and recorded bursts are never kept on disk.
- **Honest verdicts.** "Identified" only when a decoder recognised the device's own data; a pattern match is
  called a pattern match; locate results are a rough area, never a spot.

Listening to the radio spectrum is regulated differently in each country, and intercepting private communications
is a crime in most. **Check your local law before you use ARIS**, and use it only where and how you are allowed to.

## Layout

| Path | What |
|---|---|
| `aris.py`, `engine/` | the engine: sweeps, investigations, decoding, captures, band capture, patrol, setup, chat |
| `arislib/` | engine-independent pieces (numpy only): receivers, RDS, burst detection, devices, locate maths |
| `aris_gui.py`, `arisweb/` | the web page: Chat, Signals, Spectrum, Map, Activity, Devices |
| `aris_node.py` | a remote node: another computer with a receiver, for locating |
| `plugins/` | plug-ins loaded at start |
| `deploy/` | installers (`get.sh`, `install.sh`), update, systemd units, the passive Wi-Fi and Bluetooth helpers, the Windows helper |
| `test/` | simulated radios and the automated tests: `bash test/run_all.sh` |
| `docs/` | how it works ([ARCHITECTURE](docs/ARCHITECTURE.md)), receivers, plug-ins, development, [Windows through WSL 2](docs/WINDOWS.md) |

Settings, logins, node tokens, databases, baselines and logs live in the data folder (`$ARIS_DATA_DIR`, default
`~/aris_data`; `/var/lib/aris` for the service), never in the repository.

## Licence

MIT: see [LICENSE](LICENSE). The licence covers ARIS's own code; the programs it runs (rtl-sdr, rtl_433,
SoapySDR, rx_tools, multimon-ng, Ollama, Signal Hound's BB API) keep their own licences and are installed separately.
