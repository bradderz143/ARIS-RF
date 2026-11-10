#!/usr/bin/env bash
# Install ARIS as a service that runs as its own unprivileged user "aris" (never as root).
#   sudo bash deploy/install.sh            dry run: checks everything and prints the plan, changes NOTHING
#   sudo bash deploy/install.sh --apply    installs (or upgrades) and starts the service
#   sudo bash deploy/install.sh --remove   stops and removes the service (keeps your data in /var/lib/aris)
# What --apply does: creates the system user aris (group plugdev, which the rtl-sdr udev rule gives the dongles),
# copies this checkout to /opt/aris (owned by root, read-only for the service), creates /var/lib/aris (aris, mode
# 700: settings, baselines, logs, logins), installs deploy/aris.service and starts it. Nothing outside those is
# touched. Run it from the checkout you want to install. Afterwards: set the login (the script prints the command).
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="${ARIS_REPO:-/opt/aris}"; DATA="${ARIS_DATA:-/var/lib/aris}"; UNIT=/etc/systemd/system/aris.service
MODE=dry; case "${1:-}" in --apply) MODE=apply;; --remove) MODE=remove;; "") ;; *) echo "usage: $0 [--apply|--remove]"; exit 2;; esac
ok(){ printf '  \e[32mOK\e[0m   %s\n' "$*"; }
warn(){ printf '  \e[33mNOTE\e[0m %s\n' "$*"; }
bad(){ printf '  \e[31mFIX\e[0m  %s\n' "$*"; FAIL=1; }
FAIL=0
[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0 $*"; exit 1; }

if [[ $MODE == remove ]]; then
  systemctl disable --now aris.service 2>/dev/null && ok "service stopped and disabled" || warn "no aris service was running"
  rm -f "$UNIT" && systemctl daemon-reload && ok "unit removed"
  warn "kept: $REPO (code) and $DATA (your data). Delete them yourself if you want them gone."
  exit 0
fi

echo "1. Checks"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' && ok "$(python3 --version)" || bad "Python 3.8 or newer needed"
if runuser -u nobody -- python3 -c 'import numpy, flask, werkzeug, ollama' 2>/dev/null; then ok "python modules numpy, flask, werkzeug, ollama (visible to an ordinary user)"
else bad "an ordinary user cannot import numpy / flask / werkzeug / ollama: install them system-wide (apt, or pip install --break-system-packages numpy flask werkzeug ollama)"; fi
RX=0
if command -v rtl_sdr >/dev/null && command -v rtl_power >/dev/null && command -v rtl_test >/dev/null; then ok "rtl-sdr tools (RTL-SDR dongles)"; RX=1; fi
if command -v SoapySDRUtil >/dev/null && command -v rx_sdr >/dev/null && command -v rx_power >/dev/null; then ok "SoapySDR and rx_tools (other SDRs)"; RX=1; fi
[[ $RX -eq 1 ]] || warn "no receiver tools found: apt install rtl-sdr (RTL-SDR dongles), or SoapySDR + rx_tools, or a device plug-in"
command -v rtl_433 >/dev/null && ok "rtl_433 (sensor decoding)" || warn "rtl_433 not found: apt install rtl-433 (most decoding needs it)"
for t in multimon-ng atest rtl_adsb; do command -v $t >/dev/null && ok "$t" || warn "$t not found (optional decoder)"; done
getent group plugdev >/dev/null && ok "group plugdev exists" || warn "no group plugdev (the rtl-sdr package creates it with its udev rule)"
curl -s -m 3 -o /dev/null http://127.0.0.1:11434/api/tags && ok "Ollama answers on 127.0.0.1:11434" || warn "Ollama does not answer: the chat works for direct commands, the AI part needs it (ollama pull qwen2.5:7b)"
python3 -m py_compile "$SRC/aris.py" "$SRC/aris_gui.py" "$SRC"/engine/*.py "$SRC"/arislib/*.py "$SRC"/arisweb/*.py \
  && ok "code compiles ($(grep -m1 '^VERSION' "$SRC/aris.py"))" || bad "the code does not compile"
[[ -f $SRC/deploy/aris.service ]] && ok "deploy/aris.service" || bad "deploy/aris.service missing"
[[ $FAIL -eq 0 ]] || { echo "Fix the items marked FIX first. Nothing was changed."; exit 1; }

echo "2. Plan"
cat <<PLAN
  - create the system user aris (no login, home $DATA, groups aris and plugdev)
  - copy $SRC -> $REPO (owned by root, not writable by aris)
  - create $DATA (owned by aris, mode 700) if it is not there; existing data is kept
  - install deploy/aris.service as $UNIT, enable it at boot and (re)start it
PLAN
[[ $MODE == dry ]] && { echo; echo "Dry run: nothing was changed. To install:  sudo bash $0 --apply"; exit 0; }

echo "3. Installing"
id aris >/dev/null 2>&1 && ok "user aris exists" || { adduser --system --group --home "$DATA" --no-create-home --shell /usr/sbin/nologin aris && ok "user aris created"; }
getent group plugdev >/dev/null && usermod -aG plugdev aris && ok "aris is in plugdev"
systemctl stop aris.service 2>/dev/null && ok "old service stopped" || true
mkdir -p "$REPO" && rsync -a --delete --exclude '__pycache__' --exclude 'test/shots' "$SRC"/ "$REPO"/ \
  && chown -R root:root "$REPO" && chmod -R go-w "$REPO" && ok "code in $REPO"
mkdir -p "$DATA" && chown aris:aris "$DATA" && chmod 700 "$DATA" && ok "data folder $DATA"
sed "s#@REPO@#$REPO#g" "$REPO/deploy/aris.service" > "$UNIT" && systemctl daemon-reload && ok "service unit installed"
systemctl enable --now aris.service >/dev/null 2>&1; sleep 5
systemctl is-active --quiet aris.service && ok "service running (logs: journalctl -u aris -f)" || bad "the service did not start: journalctl -u aris -n 50"
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
cat <<NEXT
4. Next
  Set the login:   sudo -u aris env ARIS_DATA_DIR=$DATA python3 $REPO/aris_gui.py --set-password
  Open:            http://${IP:-<this computer>}:5000   then in the chat: /check, then /setup
  Updates:         sudo bash $REPO/deploy/update.sh
  Keep it off the network (local only): add Environment=ARIS_GUI_HOST=127.0.0.1 to the unit, or see
                   deploy/tailscale-https.sh for HTTPS through your own tailnet.
NEXT
exit $FAIL
