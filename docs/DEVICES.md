# Receivers

ARIS calls every receiver a *dongle* (Dongle 1, Dongle 2 ...) whatever it really is. Receivers are found when ARIS
starts with an empty data folder and every time `/check` or `/setup` runs; a new one is added at the end of the
numbering, a missing one is marked and skipped, and `forget dongle 3` removes one for good. Nothing is hard-coded.

Each receiver keeps its own gains (FM, 433 MHz and 915 MHz bands separately), frequency correction, antenna note and
baselines. `/setup` measures which receiver hears each band best, and every feature picks from that.

## How it works

The engine builds every radio command the way the rtl-sdr tools take it (`rtl_sdr`, `rtl_power`, `rtl_fm`,
`rtl_433`, `rtl_adsb`, `rtl_test`), and everything that reads their output expects what those tools print. A
*backend* (`arislib/radios.py`) turns such a command into the one its receiver understands, so the rest of ARIS never
changes. Device ids carry the backend: `soapy-hackrf-<serial>`, `bb60-<serial>`; ids with no known prefix are RTL-SDR
serials. All receivers share the same locks, Reset Scanner and settle times.

## RTL-SDR dongles

Install the rtl-sdr tools (`apt install rtl-sdr`; its udev rule gives the group `plugdev` the dongles) and rtl_433.

- **Give every dongle its own serial.** New dongles all carry `00000001`, and the rtl-sdr tools read a number they
  can parse as a device *index*, so two such dongles cannot be told apart and the wrong one would be opened. With only
  the new dongle plugged in: `rtl_eeprom -d 0 -s 00002002` (any 8 digits that no other dongle uses), unplug, replug.
  ARIS lists a dongle with a factory serial but never opens it until it has its own.
- Three or more dongles need a **powered USB hub**; on a plain hub they lose samples.
- Covers about 24-1766 MHz (R820T / R828D tuners).

## SoapySDR receivers (HackRF, Airspy, SDRplay, LimeSDR, PlutoSDR, USRP, SoapyRemote ...)

Install SoapySDR with the module for your hardware (e.g. `soapysdr-module-hackrf`), [rx_tools](https://github.com/rxseger/rx_tools)
(`rx_sdr`, `rx_power`, `rx_fm`: the rtl-sdr tools' command lines on SoapySDR), and an rtl_433 built with SoapySDR
support. `SoapySDRUtil --find` should list your receiver; `/check` then adds it.

- RTL-SDR dongles also show up in SoapySDR; ARIS leaves those to its RTL-SDR backend.
- A receiver that `--find` does not list (e.g. one behind SoapyRemote) can be named in the data folder's
  `config.json`:

  ```json
  "radios": {
    "soapy-remote-lab": {"args": "driver=remote,remote=tcp://192.0.2.10:55132", "name": "Lab HackRF"}
  }
  ```
- The Spectrum tab shows their sweeps on the receiver's own scale (dB, not calibrated, not dBFS); compare receivers
  of different kinds in "Above floor".
- Not available on these: ADS-B decoding (rtl_adsb opens RTL-SDR dongles only) and the USB self-test in `/check`.
- Tested in simulation only (fake SoapySDRUtil and rx_tools in `test/fake/bin`); please report what works on your
  hardware.

## Signal Hound BB60C / BB60D

The bundled plug-in `plugins/signalhound_bb60.py` registers the receiver type `bb60`. It needs Signal Hound's BB API
for Linux (`libbb_api.so`; set `ARIS_BB_LIB` to its path if it is not on the library path) and numpy.

- IQ comes from the BB60's IQ stream (40 MS/s divided by a power of two, resampled to the rate ARIS asks for);
  sweeps from its sweep mode, printed as rtl_power rows in calibrated dBm (the Spectrum tab shows them in dBm; RTL-SDR
  sweeps in dBFS). rtl_433 runs live on the IQ through `arislib/iqpipe.py`, and
  pager / APRS audio is demodulated in Python. ADS-B decoding needs an RTL-SDR.
- The gain ARIS asks for sets the reference level (`10 dBm - gain`), with the BB60's automatic gain and attenuation.
- The function signatures and constants follow the BB API 5 header; check them against the SDK you install.
- Tested against the plug-in's simulated device only (`ARIS_BB60_SIM=1`); please report results on real hardware.

## Signal Hound SA44B / SA124B

The bundled plug-in `plugins/signalhound_sa.py` registers the receiver type `sa`. It needs Signal Hound's SA API for
Linux (`libsa_api.so`; set `ARIS_SA_LIB` to its path if it is not on the library path) and numpy.

- **Good for sweeps:** 100 kHz to 12.4 GHz (SA124B; 4.4 GHz on an SA44B) in calibrated dBm, so Spectrum, baselines,
  patrol and the threat audit reach 2.4 and 5.8 GHz, mobile-phone bands and more (the bands above 1.7 GHz appear
  when a receiver covers them).
- **Narrow IQ:** its IQ stream is 486 kS/s at most and 250 kHz wide. rtl_433 runs at that rate, the pager / APRS
  decoders and locate readings work, but steps that need 2.048 MS/s (Name all FM stations in slices, band captures,
  an investigation's wide look) are refused in plain words and left to another receiver. `/setup` gives each band to
  the receiver that hears it best, so a mixed set works.
- **Receive-only:** a Signal Hound tracking generator attached to an SA transmits; the plug-in never uses it.
- The gain ARIS asks for sets the reference level (`10 dBm - gain`, at most +20 dBm). IQ samples arrive "scaled to
  mW" and are divided by the reference level; if real levels look tens of dB off, that is the first thing to check.
- Tested against the plug-in's simulated device only (`ARIS_SA_SIM=1`); please report results on real hardware.

## Other receivers

Write a device plug-in: see [PLUGINS.md](PLUGINS.md). The simplest way is a helper program that takes `rtl_sdr` and
`rtl_power` style command lines (`arislib.radios.HelperBackend`), as the BB60 plug-in does.
