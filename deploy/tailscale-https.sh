#!/usr/bin/env bash
# ARIS over HTTPS through Tailscale (v4.9.7). Run as root:
#   bash deploy/tailscale-https.sh            dry run: checks and the plan, changes nothing
#   bash deploy/tailscale-https.sh --apply    HTTPS front + ARIS listens on this PC only
#   bash deploy/tailscale-https.sh --rollback back to plain HTTP on every address
# What it does: `tailscale serve` takes the port 443 of this machine's tailnet name (a real certificate, no browser
# warning, only devices in the tailnet can reach it: not Funnel, not the internet) and passes requests to ARIS on
# 127.0.0.1:5000; a systemd drop-in makes ARIS listen on 127.0.0.1 only, so the plain-HTTP port is no longer open
# on the Wi-Fi, the hotspot or Tailscale. Other devices on your tailnet (a phone, a laptop) then use https://<this machine's name>.<tailnet>.ts.net
# (it needs Tailscale switched on). Nothing is deleted. Undoes itself when the HTTPS address does not answer.
set -euo pipefail
MODE=dry; [[ ${1:-} == --apply ]] && MODE=apply; [[ ${1:-} == --rollback ]] && MODE=rollback
DROPIN_DIR=/etc/systemd/system/aris.service.d; DROPIN=$DROPIN_DIR/loopback.conf
ok(){ printf '  \033[32mOK\033[0m   %s\n' "$*"; }; bad(){ printf '  \033[31mFIX\033[0m  %s\n' "$*"; FAIL=1; }; FAIL=0
[[ $EUID -eq 0 ]] || { echo "Run as root (sudo -s first)."; exit 1; }

if [[ $MODE == rollback ]]; then
  rm -f "$DROPIN"; rmdir "$DROPIN_DIR" 2>/dev/null || true
  tailscale serve reset || true
  systemctl daemon-reload; systemctl restart aris.service
  echo "Back to plain HTTP on every address (port 5000). The HTTPS front is switched off."; exit 0
fi

echo "1. Checks"
command -v tailscale >/dev/null && ok "tailscale is installed" || bad "tailscale is not installed"
systemctl is-active --quiet aris.service && ok "the aris service runs" || bad "the aris service is not running"
grep -qs 'def via_proxy' /opt/aris/arisweb/auth.py && ok "the installed ARIS knows it sits behind a proxy (v4.9.7 or later)" \
  || bad "the installed ARIS is older than v4.9.7: its login lockout and capture endpoint would treat every remote browser as this computer. Update first: sudo bash /opt/aris/deploy/update.sh"
NAME=""; CERTS=""
if command -v tailscale >/dev/null; then
  read -r NAME CERTS < <(tailscale status --json 2>/dev/null | python3 -c '
import json, sys
d = json.load(sys.stdin)
print((d["Self"]["DNSName"] or "").rstrip("."), len(d.get("CertDomains") or []))' || echo "- 0")
fi
[[ -n $NAME && $NAME != "-" ]] && ok "this machine is $NAME on the tailnet" || bad "no tailnet name (is Tailscale logged in, and MagicDNS on?)"
[[ ${CERTS:-0} -gt 0 ]] && ok "HTTPS certificates are enabled for the tailnet" \
  || bad "HTTPS certificates are off: Tailscale admin page > DNS > HTTPS Certificates > Enable, then run this again"
[[ $FAIL -ne 0 ]] && { echo "Fix the items marked FIX first. Nothing was changed."; exit 1; }

echo "2. Plan"
cat <<PLAN
  - tailscale serve: https://$NAME (port 443, tailnet only) -> http://127.0.0.1:5000
  - $DROPIN: ARIS listens on 127.0.0.1 only (plain HTTP no longer reachable from the network)
  - restart the aris service (about 10 s; captures resume), check https://$NAME/login, undo if it does not answer
PLAN
[[ $MODE == dry ]] && { echo; echo "Dry run: nothing was changed. To do it:  bash $0 --apply"; exit 0; }

echo "3. Doing it"
undo(){ echo "  undoing: $1"; rm -f "$DROPIN"; tailscale serve reset || true; systemctl daemon-reload
        systemctl restart aris.service || true; echo "Back to plain HTTP. Look at: journalctl -u aris -n 50; tailscale serve status"; exit 1; }
tailscale serve --bg --https=443 http://127.0.0.1:5000 && ok "tailscale serve is set" || undo "tailscale serve failed"
mkdir -p "$DROPIN_DIR"
printf '[Service]\nEnvironment=ARIS_GUI_HOST=127.0.0.1\n' > "$DROPIN"
systemctl daemon-reload && systemctl restart aris.service && ok "aris listens on 127.0.0.1 only"
# The first HTTPS request makes Tailscale fetch a certificate (20-30 s the first time): ask for it now, then try for a minute.
echo "  getting the certificate (the first time this takes up to half a minute) ..."
timeout 90 tailscale cert --cert-file /dev/null --key-file /dev/null "$NAME" >/dev/null 2>&1 || true
code=000
for _ in 1 2 3 4 5 6; do
  code="$(curl -s -m 10 -o /dev/null -w '%{http_code}' "https://$NAME/login" || true)"
  [[ $code == 200 ]] && break
  sleep 5
done
[[ $code == 200 ]] && ok "https://$NAME/login answers" || undo "https://$NAME/login answered '$code' after a minute"
echo "4. Done. From any device on your tailnet (Tailscale on): https://$NAME"
echo "   Log in again (the old http address and the Wi-Fi address no longer work). Undo: bash $0 --rollback"
