"""Engine part: plug-ins.

A plug-in is one Python file. ARIS loads every `*.py` (not starting with "_") from, in order:
  1. plugins/ next to aris.py (the ones that come with ARIS),
  2. $ARIS_DATA_DIR/plugins/ (your own),
  3. each folder in $ARIS_PLUGIN_DIRS (separated by ':').
A file whose name is in the config's "plugins_disabled" list is skipped. A plug-in defines

    ABOUT = "one line on what it adds"          (optional)
    def setup(aris): ...                        (called once at start with the API below)

and through `aris` it can:
  - aris.register(name, description, properties, required=(), aliases=())  decorator: a new tool for the chat and
    the AI (same rules as the engine's own tools: plain words in, a dict out; it must never transmit);
  - aris.register_backend(backend)   a new receiver type (an arislib.radios.Backend; see plugins/signalhound_bb60.py);
  - aris.add_band(key, lo_mhz, hi_mhz, label, words=None, capture=True, preset=True)   a named band: the chat's
    scans and band captures understand it, and the Spectrum tab gets a preset;
  - aris.notice(text)   a message in the chat;
  - aris.log, aris.data_dir, aris.version, aris.radios (arislib.radios), and aris.engine (everything else, for
    plug-ins that know what they are doing).
A plug-in runs with the station's own rights: install only plug-ins you have read. One that fails to load is
skipped and listed with its error (/status, `/plugins`); it never stops ARIS from starting.
"""

PLUGINS: list = []                 # [{"name", "path", "ok", "about", "error"}] in load order
PLUGIN_PRESETS: list = []          # extra Spectrum presets from plug-ins: {"id", "name", "start", "stop"}
_EXTRA_BAND_WORDS: list = []       # [(compiled regex, band key)] for band captures ('record the 868 band')


class _PluginAPI:
    """What a plug-in's setup(aris) receives."""

    def __init__(self, name):
        self.name = name
        self.log = logging.getLogger(f"aris.plugin.{name}")
        self.version = VERSION
        self.data_dir = DATA_DIR
        from arislib import radios as _radios
        self.radios = _radios
        self.engine = sys.modules.get(__name__)

    def register(self, name, description, properties, required=(), aliases=()):
        if name in TOOLS:
            raise ValueError(f"a tool named {name!r} exists already")
        return register(name, description, properties, required=required, aliases=aliases)

    def register_backend(self, backend):
        from arislib import radios as _radios
        b = _radios.register_backend(backend)
        _refresh_tuner_range()
        return b

    def add_band(self, key, lo_mhz, hi_mhz, label, words=None, capture=True, preset=True):
        return _add_band(key, lo_mhz, hi_mhz, label, words=words, capture=capture, preset=preset)

    def notice(self, text):
        _notice(f"[{self.name}] {text}", kind="plugin")


def _add_band(key, lo_mhz, hi_mhz, label, words=None, capture=True, preset=True):
    """A named band from a plug-in. key: a short id ('868'); words: a regex for how people say it (default: the
    key followed by 'band', e.g. 'the 868 band')."""
    from arislib import bandwatch
    key = str(key).strip()
    lo, hi = sorted((float(lo_mhz), float(hi_mhz)))
    if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", key) or hi - lo <= 0 or hi - lo > 60.0:
        raise ValueError("a band needs a short key and a range up to 60 MHz wide")
    rx = re.compile(words if words else rf"\b{re.escape(key)}\s*(?:mhz)?\s*(?:ism\s*)?band\b", re.I)
    _NAMED_BANDS.append((rx, (lo, hi), f"the {label}"))
    if capture:
        bandwatch.BANDS[key] = (lo, hi, str(label))
        _EXTRA_BAND_WORDS.append((rx, key))
    if preset and not any(p["id"] == key for p in PLUGIN_PRESETS):
        short = f"{key} MHz" if key.isdigit() else str(label)[:16]
        PLUGIN_PRESETS.append({"id": key, "name": short, "start": lo, "stop": hi})
    return key


def _plugin_band_key(text):
    """A plug-in band named in text ('record the 868 band' -> '868'), else None."""
    t = str(text or "")
    for rx, key in _EXTRA_BAND_WORDS:
        if rx.search(t):
            return key
    return None


def _plugin_dirs() -> list:
    dirs = [Path(_HERE) / "plugins", Path(DATA_DIR) / "plugins"]
    dirs += [Path(p) for p in os.environ.get("ARIS_PLUGIN_DIRS", "").split(os.pathsep) if p.strip()]
    out = []
    for d in dirs:
        try:
            r = d.resolve()
        except OSError:
            continue
        if r.is_dir() and r not in out:
            out.append(r)
    return out


def _load_plugins() -> list:
    """Load every plug-in once (a second call does nothing). Returns PLUGINS."""
    import importlib.util
    if PLUGINS:
        return PLUGINS
    try:
        disabled = set(_load_config().get("plugins_disabled") or [])
    except Exception:       # noqa: BLE001 - a broken config must not stop the plug-ins' listing
        disabled = set()
    for d in _plugin_dirs():
        for f in sorted(d.glob("*.py")):
            if f.name.startswith("_") or f.stem in disabled or any(p["name"] == f.stem for p in PLUGINS):
                continue
            rec = {"name": f.stem, "path": str(f), "ok": False, "about": "", "error": ""}
            try:
                spec = importlib.util.spec_from_file_location(f"aris_plugin_{f.stem}", str(f))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                rec["about"] = str(getattr(mod, "ABOUT", "") or "")[:200]
                setup = getattr(mod, "setup", None)
                if callable(setup):
                    setup(_PluginAPI(f.stem))
                rec["ok"] = True
            except Exception as e:      # noqa: BLE001 - a plug-in's error is shown, never fatal
                rec["error"] = f"{type(e).__name__}: {str(e)[:200]}"
                log.warning("plug-in %s not loaded: %s", f.stem, rec["error"])
            PLUGINS.append(rec)
    _refresh_tuner_range()
    return PLUGINS


def list_plugins():
    """The plug-ins loaded at start, their folders and the receiver types known (for /status and /plugins)."""
    _load_plugins()
    return {"plugins": [{k: v for k, v in p.items() if k != "path"} for p in PLUGINS],
            "folders": [str(d) for d in _plugin_dirs()],
            "receiver_types": [b.name for b in _radios_mod().backends()]}


def _radios_mod():
    from arislib import radios as _radios
    return _radios


def _first_start_devices() -> None:
    """A fresh data folder (no receiver known yet): find the receivers plugged in and number them, once. Nothing is
    built in, so a new station starts with whatever it finds; /check does the same any time later."""
    try:
        if _load_config().get("dongle_order"):
            return
        found = _list_devices()
    except Exception as e:      # noqa: BLE001 - never stop the start
        log.warning("looking for receivers at the first start failed: %s", e)
        return
    if found:
        _sync_found_dongles(found)
