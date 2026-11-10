#!/usr/bin/env bash
# ARIS Wi-Fi scan helper installer (v4.10.0). Run as root:
#   bash deploy/aris-wifi.sh --iface NAME             dry run: checks and the plan, changes nothing
#   bash deploy/aris-wifi.sh --iface NAME --apply     install and start the helper
#   bash deploy/aris-wifi.sh --rollback               stop and remove it
# What it does: installs one small systemd unit (aris-wifi.service) that runs `iw dev NAME scan passive` every minute
# (it listens only: no probe requests, nothing is sent) and writes the result to /run/aris-wifi/scan.txt, which the
# ARIS web server reads for the Wi-Fi view of the Devices tab. The unit runs as an unprivileged dynamic user with the
# single capability CAP_NET_ADMIN; the main ARIS service is not changed. Use the USB adapter, NOT the interface this PC
# is connected through: the dry run refuses an interface that has a connection. Nothing is deleted.
set -euo pipefail
MODE=dry; IFACE=""
while [[ $# -gt 0 ]]; do
  case $1 in --apply) MODE=apply;; --rollback) MODE=rollback;; --iface) IFACE=${2:-}; shift;; *) echo "unknown option $1"; exit 1;; esac
  shift
done
UNIT=/etc/systemd/system/aris-wifi.service; SRC=/opt/aris/deploy
ok(){ printf '  \033[32mOK\033[0m   %s\n' "$*"; }; bad(){ printf '  \033[31mFIX\033[0m  %s\n' "$*"; FAIL=1; }; FAIL=0
[[ $EUID -eq 0 ]] || { echo "Run as root (sudo -s first)."; exit 1; }

if [[ $MODE == rollback ]]; then
  systemctl disable --now aris-wifi.service 2>/dev/null || true
  rm -f "$UNIT"; systemctl daemon-reload
  echo "The Wi-Fi scan helper is stopped and removed. The Wi-Fi view keeps its list and baseline but gets no new scans."; exit 0
fi

echo "1. Checks"
command -v iw >/dev/null && ok "iw is installed" || bad "iw is not installed (sudo apt install iw)"
[[ -f $SRC/aris-wifi-scan.sh && -f $SRC/aris-wifi.service ]] && ok "the helper files are in $SRC" \
  || bad "the helper files are not in $SRC: update ARIS first (sudo bash /opt/aris/deploy/update.sh)"
if [[ -z $IFACE ]]; then
  bad "no interface given. Wi-Fi interfaces here: $(iw dev 2>/dev/null | awk '$1=="Interface"{print $2}' | tr '\n' ' ')(use --iface NAME with the USB adapter)"
else
  if iw dev "$IFACE" info >/dev/null 2>&1; then ok "interface $IFACE exists"; else bad "interface $IFACE not found (iw dev lists them)"; fi
  if iw dev "$IFACE" link 2>/dev/null | grep -q "^Connected to"; then
    bad "$IFACE is connected to a network: use another adapter, so this PC's own connection is not disturbed"
  else ok "$IFACE has no connection of its own"; fi
fi
systemctl is-active --quiet aris.service && ok "the aris service runs (it will read /run/aris-wifi/scan.txt)" || bad "the aris service is not running"
[[ $FAIL -ne 0 ]] && { echo "Fix the items marked FIX first. Nothing was changed."; exit 1; }

echo "2. Plan"
echo "  - write $UNIT for interface $IFACE (passive scan every 60 s, unprivileged user, CAP_NET_ADMIN only)"
echo "  - start it and wait up to 90 s for /run/aris-wifi/scan.txt with at least one network"
echo "  - undo everything if no scan arrives"
[[ $MODE == dry ]] && { echo "Dry run: nothing was changed. Add --apply to do it."; exit 0; }

echo "3. Applying"
sed "s/@IFACE@/$IFACE/" "$SRC/aris-wifi.service" > "$UNIT"
systemctl daemon-reload; systemctl enable --now aris-wifi.service
for _ in $(seq 1 45); do
  if grep -qs "^BSS " /run/aris-wifi/scan.txt 2>/dev/null; then
    n=$(grep -c "^BSS " /run/aris-wifi/scan.txt); ok "first passive scan: $n networks"
    echo "Done. Open the Devices tab > Wi-Fi in the page. To undo: bash deploy/aris-wifi.sh --rollback"; exit 0
  fi
  sleep 2
done
echo "No scan arrived in 90 s: undoing."; journalctl -u aris-wifi.service -n 15 --no-pager || true
systemctl disable --now aris-wifi.service 2>/dev/null || true; rm -f "$UNIT"; systemctl daemon-reload
exit 1
