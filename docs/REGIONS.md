# Regions: band plans

ARIS labels every frequency with what normally transmits there, checks FM stations against a channel plan, and
scores short bursts lower where sensors and phones live. All of that differs between countries, so it comes from one
band plan per region, never from a table that fits only one part of the world.

A label is a band plan, never an identification. Where the region's table has nothing for a frequency, the label
says so ("Not in this region's band table: a narrowband carrier"; "Depends on the country: set your region" for World)
and never borrows a label from another region.

## The regions

| id | Covers | FM plan |
|---|---|---|
| `americas` | ITU Region 2: North, Central and South America | 87.5-108 MHz band, stations on odd tenths 88.1-107.9 (Brazil: from 76.1) |
| `europe` | ITU Region 1 under CEPT: Europe, and the parts of Africa and the Middle East that follow CEPT | 87.5-108 MHz, 100 kHz raster (50 kHz steps allowed: some countries use them) |
| `asia_pacific` | ITU Region 3, only what most countries share | 87.5-108 MHz, 100 kHz raster |
| `japan` | Japan | 76-99 MHz, 100 kHz raster |
| `australia` | Australia and New Zealand | 87.5-108 MHz, 100 kHz raster; Australia: odd tenths plus 87.6 / 87.8 / 88.0 |
| `world` | only what the ITU Radio Regulations harmonise worldwide | 87.5-108 MHz, 100 kHz raster |

Some regions have country layouts (`americas`: US, CA, MX, BR; `asia_pacific`: CN, IN, KR; `australia`: AU, NZ) for
bands that countries of one region use differently (700 / 800 MHz, 1.8-2 GHz, 3.5 GHz in the Americas). They apply
only when the country is known; without one, those bands get one neutral label ("layout differs by country").

## Choosing the region

- The config keys `region` (an id above) and `country` (a code, or `null` for "region-wide labels only").
- Not set: a guess from the computer's time zone (`TZ`, `/etc/timezone`, the `/etc/localtime` link; the country from
  the system's zone table). America/* gives the Americas, Europe/* and Africa/* Europe, Asia/Tokyo Japan,
  Australia/* and Pacific/Auckland Australia / New Zealand, other Asia/*, Pacific/* and Indian/* Asia-Pacific,
  anything else World. The zone table's country corrects the prefix where the ITU region differs: the Middle East
  west of Iran, the Caucasus, Russia, Central Asia and Mongolia (RR 5.3) and the African islands of the Indian Ocean
  are Europe (Region 1); Hawaii, the Galapagos and Easter Island are the Americas (Region 2). A time zone that cannot
  be read counts as none. The guess is never written to the config; `/status`, `/check` and the Spectrum tab say
  "Region: Americas (guessed from the time zone; change it under Spectrum)".
- `ARIS_REGION` / `ARIS_COUNTRY` in the environment stand in for the config keys when the config has none
  (installers, the test harness: an empty `ARIS_COUNTRY` means no country).
- Change it: the Spectrum tab ("How this tab works", admin account), `POST /api/region {"id": "europe", "country":
  null}` (admin, `X-ARIS: 1`), or `/region europe` (or `/region americas us`) in the chat or the console. A change
  applies at once.

## What a region decides

| Key | Used for |
|---|---|
| `allocations` | the label of a frequency (with the TV, DAB and country lists and the base region's list). `[lo, hi, label]`, or `[lo, hi, label, ["US", "CA"]]` for a label written for some of the region's countries: with another country known it is left out (without a country it shows) |
| `fm` | the FM band (`_in_fm_band`, wideband decoders), the channel plan (`_on_fm_plan`, Name all, off-plan threat score) |
| `sensor_bands` | where rtl_433-type devices live: decoder choice and the lower burst score |
| `busy_bands` | Wi-Fi, Bluetooth and phone bands: bursts there score lower |
| `hop_bands` | where frequency hopping is normal: the audit's one LOW finding says what it is typical of |
| `ism_bands`, `ism_default` | band-capture names ("433", "868", "915", "920") and what "the ISM band" means |
| `setup` | the bands /setup calibrates and patrol watches by default |
| `gain_band` | where the dongle's own sub-GHz gain applies (config key `gain_915_db`, kept for old configs: it means "the region's sub-GHz band", 902-928 MHz in the Americas, 863-870 in Europe) |
| `presets` | the Spectrum chips (a dict, or the id of a generic one: air, adsb, gnss, ism24, wifi5, ism58) |
| `device_hints` | band-plan device guesses in the Signals list |
| `named` | band names whose edges differ ("2 m band": 144-148 MHz in the Americas, 144-146 in Europe) |
| `tv`, `dab` | "Digital TV channel 24 (ATSC 1.0 / 3.0)", "DAB / DAB+ digital radio, block 12C"; both take an optional `countries` list (with another country known the table or block is left out: Australia's DAB blocks are not New Zealand's) |
| `prompt` | the band lines the chat's AI model reads |

What changed in the Americas with the regions (on purpose: 863-870 MHz is trunked land-mobile radio there, not a
short-range device band): 863-870 MHz is no longer a sensor band in the Americas, so bursts there lose the lower
sensor-band score, a steady carrier there goes to the audio decoders rather than rtl_433, and a frequency there is
no longer taken for the "868" band capture (that band is still recorded when it is named). A hop collapse outside
the region's hop bands (for example in 433-435 MHz) is described as "typical of frequency-hopping devices" without
naming meters or LoRa.

Where a decode is doubtful does not depend on the region: `SENSOR_WINDOWS_MHZ` (engine/decode.py) is the union of
every region's and country's sensor bands, because devices cross borders and a region can be set wrongly.

The aeronautical navigation band 108-117.975 MHz (VOR and ILS localizer beacons: steady AM carriers with tones, no
voice) is the same everywhere; the Decode card says so instead of calling it the aircraft voice band.

## The label rule

Every entry that contains the frequency is collected from: the country's own layout (only when the country is set),
the region's DAB blocks and TV channels (with a country known, only the tables for it), the region's allocations and
the base region's (World's) list. The narrowest entry wins; on a tie the more local list wins. So a marine channel
inside a region's VHF business band is named as the marine channel, and a TV channel inside a wide country entry is
named as the TV channel. Within one list an entry either holds another whole or stays clear of it (the tests check
this), so "narrowest" always means "most specific".

World's 433.05-434.79 MHz entry ("low-power remotes and sensors in most countries") is a hedged entry, not a band the
ITU harmonises worldwide (RR 5.138 covers Region 1 only); a region with its own rule overrides it on a tie. In Japan
it is kept on purpose: 433 MHz there is open to tyre sensors and car keys at very low power, but the exact range was
not confirmed, so Japan has no entry of its own. Asia-Pacific lists 433.05-434.79 MHz as a sensor and setup band on
purpose: China, India and much of South-East Asia allow low-power devices there (Korea and Japan restrict it), and
without it Asia-Pacific's /setup would have no low band.

## Adding or changing a region

A region is one JSON file in `arislib/data/regions/` and its id in one line of code. Copy the closest file, give it
a new `id` (also add the id to `REGION_IDS` in `arislib/regions.py`, which fixes the order of the choices), keep
`"base": "world"`, and fill in what you know. Rules:

- Frequencies in MHz, edges inclusive, `lo < hi`.
- Labels say what normally transmits there in plain words; a label is never an identification. Name no city and no
  particular station or operator; national facts ("USA: paging transmitters") are fine.
- Leave out what you are not sure of: the honest fallback is better than a wrong label. Where something is only
  usual, say "usually" or "in most countries".
- A TV table gives `lo`, `hi`, `width_mhz`, `first_channel` (or `channel_names`), `standard`, optional `skip`
  ranges (never TV) and `countries`.
- Run `python3 -m unittest test_regions` (from `test/`): it checks every file's schema, ordering, overlaps and the
  TV arithmetic.
- A file that cannot be read (missing, not valid JSON, a part of the wrong kind) is named in the error. Another
  region's broken file costs only that region (left out of the choices and the sensor-band union, logged); the
  region in force fails loudly instead of quietly taking its labels from somewhere else.

## Sources

Worldwide list (World):
- ITU Radio Regulations, Article 5 (incl. RR 5.138, 5.150, 5.266/5.267, 5.340, 5.364/5.365, 5.446A, Resolution 229)
- ITU Radio Regulations Appendix 18 (maritime VHF channels); ITU-R M.1371 (AIS); ICAO Annex 10
- NOAA OSPO (GOES rebroadcast 1686.6 MHz, HRIT/EMWIN 1694.1 MHz)

Americas:
- FCC 47 CFR 2.106 (Table of Frequency Allocations), 15.231 / 15.235 / 15.247, Parts 22, 24, 27, 73 (73.201, 73.501,
  73.603), 90, 95, 97.301
- ISED (Canada): BPR-3, SRSP-502, SRSP-518, RBR-4, the 3500 MHz decision; the Weatheradio Canada closure notice
  (2026)
- IFT (Mexico): the 806-824 / 851-869 MHz band plan (2016), protected early-warning spectrum 162.400-162.550 MHz
  (2021), IFT-002-2016 (FM)
- Anatel (Brazil): Res. 721/2020 (extended FM band), Res. 757/2022 (800 MHz), Res. 680/2017; Portaria 276/2010 and
  ABNT NBR 15601 (ISDB-Tb channels)
- The Mexican and Canadian tables follow ITU Region 2; where they differ from the USA's they are country layouts.

Europe:
- CEPT ERC Report 25 (the European Common Allocation table)
- ERC Recommendation 70-03 (short-range devices; Annexes 1, 2, 7, 11)
- ECC/DEC/(11)03 (CB), (15)05 (PMR446), (08)05 (PPDR 380-400 MHz), (02)05 (GSM-R); ERC/DEC/(94)03 (DECT)
- EU Decisions 2008/477, 2009/766, 2010/267, 2012/688, 2016/687, 2017/899, 2019/235
- ITU GE84 (FM) and GE06 (digital TV) Agreements; ETSI EN 300 401 (DAB blocks)
- Ofcom IR 2027, IR 2030 (United Kingdom interfaces); WRC-19 Final Acts (Region 1 50-52 MHz)

Asia-Pacific:
- ITU Radio Regulations Article 5 (Region 3); APT 700 MHz plan; 3GPP band tables
- MIIT (China) 2019 announcement no. 52 (micropower short-range devices)
- India G.S.R. 853(E) 2021 (865-868 MHz exemption); the Use of Low Power Radio Frequency Devices in the frequency
  band 433.05 to 434.79 MHz (Exemption from License) Rules, 2022; the Korean USN / RFID band (MSIT)

Japan:
- MIC Wide FM pages (76.0-99.0 MHz from May 2025)
- ARIB STD-T93 (315 MHz), STD-T67 (400 MHz telemeters), STD-T106 / T107 (RFID), STD-T108 (920 MHz), STD-B31 (ISDB-T)
- MIC mobile frequency assignments; the JARL band plan; MIC citizen radio rules

Australia and New Zealand:
- ACMA Low Interference Potential Devices (LIPD) Class Licence; ACMA CB Class Licence; ACMA 400 MHz band plan
- ACMA 850 / 900 MHz allocation (2021); ACMA bands for broadcasting services (TV channels 6-12, 28-51)
- New Zealand Gazette 2010 low-power FM licence (87.6-88.3, 106.7-107.7 MHz); RSM short-range device licence

Least certain (worded "usually" or left out): New Zealand TV channels (left out: the sources disagree), Japan's lower
1.7 GHz band (left out), Brazil's extended FM channels (assumed odd tenths), Italy's 50 kHz FM offsets (allowed in
Europe's check, not listed per country), the 6 GHz Wi-Fi band's upper part in the Americas.
