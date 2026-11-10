import sys, time, os
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from load import call
def show(label, r):
    print(f"\n===== {label}  ({r.get('_t')} s)")
    if r.get("status") == "error":
        print("ERROR", r["error"]); return
    for k in ("final_signal_verdict", "evidence", "strongest_dongle", "measured_carrier_mhz", "decoders_used", "antenna_note"):
        print(f"  {k}: {r.get(k)}")
    for row in r.get("dongle_comparison", []):
        print("   ", {k: v for k, v in row.items() if k not in ("waveform",)}, "|", (row.get("waveform") or "")[:60])
    s2 = r.get("stage2_rtl433_telemetry") or {}
    if s2:
        print("  rtl_433:", s2.get("identified_models"), "fps:", [(f["modulation"], f["pattern"], f["short_pulse_us"], f["long_pulse_us"], f["snr_db"], f.get("heard_by"), f.get("pattern_hint")) for f in s2.get("unknown_signal_fingerprints", [])])
    s3 = r.get("stage3_multimon_direwolf_audio") or {}
    if s3: print("  audio:", s3.get("validated_frames"), s3.get("aprs_frames"), s3.get("decoders_used"))
    for k in ("adsb", "possible_clock_harmonic", "earlier_checks_of_this_frequency", "broadcast_station_confirmed"):
        if k in r: print(f"  {k}: {r[k]}")
    print("  saved:", (r.get("stage4_plain_english") or {}).get("saved_to_signal_list", (r.get("stage4_plain_english") or {}).get("note")))
cases = sys.argv[1:] or ["914.842", "433.92", "96.9", "1090", "921.6@3", "101.0"]
for c in cases:
    f, _, d = c.partition("@")
    t = time.time()
    kw = {"frequency_mhz": float(f)}
    if d: kw["dongle"] = d
    r = call("investigate_single_frequency", **kw)
    r["_t"] = round(time.time() - t, 1)
    show(c, r)
