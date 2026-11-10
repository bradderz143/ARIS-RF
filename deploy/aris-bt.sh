#!/usr/bin/env bash
# ARIS Bluetooth helper installer (v4.11.0). Run as root:
#   bash deploy/aris-bt.sh             dry run: checks and the plan, changes nothing
#   bash deploy/aris-bt.sh --apply     install and start the helper
#   bash deploy/aris-bt.sh --rollback  stop and remove it
# What it does: installs one small systemd unit (aris-bt.service) that runs `ubertooth-btle -n` (it listens only) and
# writes a summary (counts, manufacturers, signal strength, hashed labels; no addresses, names or payloads) to
# /run/aris-bt/scan.json, which the ARIS web server reads for the Bluetooth view of the Devices tab. The unit runs as an
# unprivileged dynamic user in the plugdev group; the main ARIS service is not changed. Nothing is deleted.
set -euo pipefail
MODE=dry
while [[ $# -gt 0 ]]; do
  case $1 in --apply) MODE=apply;; --rollback) MODE=rollback;; *) echo "unknown option $1"; exit 1;; esac
  shift
done
UNIT=/etc/systemd/system/aris-bt.service; SRC=/opt/aris/deploy
ok(){ printf '  \033[32mOK\033[0m   %s\n' "$*"; }; bad(){ printf '  \033[31mFIX\033[0m  %s\n' "$*"; FAIL=1; }; FAIL=0
[[ $EUID -eq 0 ]] || { echo "Run as root (sudo -s first)."; exit 1; }

if [[ $MODE == rollback ]]; then
  systemctl disable --now aris-bt.service 2>/dev/null || true
  rm -f "$UNIT"; systemctl daemon-reload
  echo "The Bluetooth helper is stopped and removed. The Bluetooth view keeps its list and baseline but gets no new data."; exit 0
fi

echo "1. Checks"
command -v ubertooth-btle >/dev/null && ok "ubertooth-btle is installed" || bad "ubertooth tools are not installed (sudo apt install ubertooth)"
[[ -f $SRC/aris-bt-scan.py && -f $SRC/aris-bt.service ]] && ok "the helper files are in $SRC" \
  || bad "the helper files are not in $SRC: update ARIS first (sudo bash /opt/aris/deploy/update.sh)"
if lsusb 2>/dev/null | grep -q "1d50:6002"; then
  ok "an Ubertooth One is plugged in"
  if v=$(ubertooth-util -v 2>/dev/null); then ok "it answers: $v"; else bad "ubertooth-util -v could not open it"; fi
  node=$(lsusb | awk '/1d50:6002/{gsub(":","",$4); print "/dev/bus/usb/"$2"/"$4}' | head -1)
  grp=$(stat -c %G "$node" 2>/dev/null || true); mode=$(stat -c %a "$node" 2>/dev/null || true)
  if [[ $grp == plugdev && ${mode:1:1} -ge 6 ]]; then ok "its device file is open to the plugdev group ($grp $mode)"
  else bad "its device file is $grp $mode, not plugdev 66x: unplug and plug the Ubertooth in again (the udev rule 40-ubertooth.rules sets the group)"; fi
else bad "no Ubertooth One (1d50:6002) is plugged in"; fi
systemctl is-active --quiet aris.service && ok "the aris service runs (it will read /run/aris-bt/scan.json)" || bad "the aris service is not running"
[[ $FAIL -ne 0 ]] && { echo "Fix the items marked FIX first. Nothing was changed."; exit 1; }

echo "2. Plan"
echo "  - write $UNIT (ubertooth-btle -n, unprivileged user in plugdev, summary every 60 s)"
echo "  - start it and wait up to 100 s for /run/aris-bt/scan.json"
echo "  - undo everything if no summary arrives"
[[ $MODE == dry ]] && { echo "Dry run: nothing was changed. Add --apply to do it."; exit 0; }

echo "3. Applying"
cp "$SRC/aris-bt.service" "$UNIT"
systemctl daemon-reload; systemctl enable --now aris-bt.service
for _ in $(seq 1 50); do
  if [[ -s /run/aris-bt/scan.json ]]; then
    n=$(python3 -c "import json;d=json.load(open('/run/aris-bt/scan.json'));print(d.get('packets',0))" 2>/dev/null || echo "?")
    ok "first summary: $n advertisements heard"
    echo "Done. Open the Devices tab > Bluetooth in the page. To undo: bash deploy/aris-bt.sh --rollback"; exit 0
  fi
  sleep 2
done
echo "No summary arrived in 100 s: undoing."; journalctl -u aris-bt.service -n 15 --no-pager || true
systemctl disable --now aris-bt.service 2>/dev/null || true; rm -f "$UNIT"; systemctl daemon-reload
exit 1
