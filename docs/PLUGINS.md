# Plug-ins

A plug-in is one Python file. At start ARIS loads every `*.py` (names starting with `_` are skipped) from, in order:

1. `plugins/` next to `aris.py` (the ones that come with ARIS),
2. `$ARIS_DATA_DIR/plugins/` (your own; `/var/lib/aris/plugins/` for the service),
3. every folder in `$ARIS_PLUGIN_DIRS` (separated by `:`).

A plug-in named in the config's `"plugins_disabled": ["ism_868"]` list is skipped. One that fails to load is skipped
and listed with its error; it never stops ARIS from starting. The chat's `/status` lists what was loaded.

**A plug-in runs with the station's own rights. Install only plug-ins you have read.** The project's rules apply to
plug-ins too: nothing may transmit, record or transcribe voice, unpack encrypted traffic, or keep replayable codes.

## The shape

```python
ABOUT = "one line on what this adds"            # optional, shown in the list

def setup(aris):
    ...                                          # called once at start
```

`aris` gives:

| | |
|---|---|
| `aris.register(name, description, properties, required=(), aliases=())` | decorator: a new tool for the chat and the AI |
| `aris.register_backend(backend)` | a new receiver type (an `arislib.radios.Backend`) |
| `aris.add_band(key, lo_mhz, hi_mhz, label, words=None, capture=True, preset=True)` | a named band |
| `aris.notice(text)` | a message in the chat |
| `aris.log`, `aris.data_dir`, `aris.version`, `aris.radios` | logging, the data folder, the version, `arislib.radios` |
| `aris.engine` | the whole engine module, for plug-ins that know what they are doing |

## A tool

```python
def setup(aris):
    from arislib import devices

    @aris.register("count_known_devices", "How many devices ARIS knows (heard on 3 or more days, or marked).", {})
    def count_known_devices():
        store = aris.engine._devices_read() or devices.new_store()
        return {"known": len(devices.known_keys(store))}
```

A tool takes plain values and returns a dict; raise `aris.engine.RFError("plain words")` for a problem the user
should read. The AI sees the description, so say when to use it. Tools that change the station (start a capture,
change settings) are only run when the user's own words asked for it; read `engine/chat.py` (`_STATE_TOOLS`,
`_user_asked`) before you write one.

## A band

```python
def setup(aris):
    aris.add_band("868", 863.0, 870.0, "868 MHz band (863-870)")
```

After that, "scan the 868 band" and "record the 868 band" work in the chat, band captures cover it (in slices, up to
60 MHz wide), and the Spectrum tab has an "868 MHz" preset. `words` is a regular expression for other ways people
say it (default: the key followed by "band").

## A receiver type

The engine builds every command as the rtl-sdr tools take it, and reads what they print. A backend turns such a
command into its receiver's own. The easiest kind is a **helper program** that speaks two command lines:

```
helper list                                               -> "<serial>\t<name>" per receiver
helper sdr -d SERIAL -f HZ -s RATE [-g GAIN] [-n N] -     -> 8-bit unsigned IQ (CU8) on stdout
helper power -d SERIAL -f LO:HI:STEP [-i S] [-1|-e Ns] [-g GAIN] -   -> rtl_power CSV rows on stdout
```

and a few lines to register it:

```python
import os, sys
from arislib import radios

def setup(aris):
    class MyRadio(radios.HelperBackend):
        name, prefix, label = "myradio", "myradio", "My receiver"      # device ids: myradio-<serial>
        freq_range_mhz = (1.0, 3000.0)
        gains = [0.0, 10.0, 20.0, 30.0, 40.0]
        sweep_unit = "dBm"          # its sweep rows' scale: "dBm" if calibrated, else "dB" (the default)
        max_iq_rate = None          # fastest IQ in samples/s, if limited: wider steps are refused, rtl_433 slows down

        def helper_argv(self):
            return [sys.executable, os.path.abspath(__file__)]          # this file is also the helper

    aris.register_backend(MyRadio())
```

With that, sweeps, the Spectrum tab, Name all FM stations, band captures, investigations' IQ analysis, locate
readings and live rtl_433 (through `arislib/iqpipe.py`) work on it. rtl_power rows are
`date, time, Hz low, Hz high, Hz step, samples, dB, dB, ...`; make `Hz high - Hz low` equal to the number of values
times the step, so value j is at `Hz low + j * step`, and print the last value twice, as rtl_power does. The Spectrum
tab shows the values with the unit `sweep_unit` names (only RTL-SDR sweeps are converted to dBFS). `plugins/signalhound_bb60.py` is a
complete example, including a simulated device for tests.

A receiver with tools of its own can subclass `radios.Backend` instead and implement `list_devices()`,
`translate(cmd, dev)` (return the command to run, `None` when a step does not apply, or raise
`radios.Unsupported("plain words")`), `tools` and `available()`; `radios.Soapy` is the example.

## Testing a plug-in

`ARIS_PLUGIN_DIRS=/path/to/folder python3 test/serve.py` runs the web page on simulated radios with it loaded;
`test/test_radios.py` shows how the bundled ones are tested.
