#!/usr/bin/env bash
# ARIS Wi-Fi scan helper (v4.10.0), run by aris-wifi.service. LISTENS ONLY: `iw ... scan passive` sends no probe
# request (a normal scan does). Writes what the access points announce (name, channel, signal, security) to
# $RUNTIME_DIRECTORY/scan.txt every $EVERY seconds; the ARIS web server reads that file. Needs CAP_NET_ADMIN only
# (the unit gives it to an unprivileged dynamic user: no root).
set -u
IFACE=${1:?usage: aris-wifi-scan.sh <interface> [seconds]}
EVERY=${2:-60}
OUT=${RUNTIME_DIRECTORY:-/run/aris-wifi}/scan.txt
while true; do
  ip link set "$IFACE" up 2>/dev/null || true
  if body=$(timeout 45 iw dev "$IFACE" scan passive 2>/dev/null) && [[ -n $body ]]; then
    { printf '# ARIS-WIFI %s %s passive\n' "$(date +%s)" "$IFACE"; printf '%s\n' "$body"; } > "$OUT.tmp" && mv -f "$OUT.tmp" "$OUT"
  fi
  sleep "$EVERY"
done
