#!/usr/bin/env bash
# Install ARIS as a service that runs as its own unprivileged user "aris" (never as root).
#   sudo bash deploy/install.sh            dry run: checks everything and prints the plan, changes NOTHING
#   sudo bash deploy/install.sh --apply    installs (or upgrades) and starts the service
#   sudo bash deploy/install.sh --remove   stops and removes the service (keeps your data in /var/lib/aris)
# What --apply does: creates the system user aris (group plugdev, which the rtl-sdr udev rule gives the dongles),
# copies this checkout to /opt/aris (owned by root, read-only for the service), creates /var/lib/aris (aris, mode
# 700: settings, baselines, logs, logins), installs deploy/aris.service and starts it. Nothing outside those is
# touched. Run it from the checkout you want to install. Afterwards: set the login (the script prints the command).
#
# On Debian and Ubuntu one command also brings everything ARIS needs, for a computer that has none of it yet:
#   sudo bash deploy/install.sh --everything            dry run: the checks and the full plan (packages too), changes NOTHING
#   sudo bash deploy/install.sh --everything --apply    does it all
#   add --no-ai to leave out Ollama and its model (the chat still answers direct commands without them)
# What --everything adds before the steps above:
#   - the system's own packages, with apt-get: Python 3 with numpy and flask, the rtl-sdr tools, rtl_433, multimon-ng,
#     direwolf (for its packet decoder "atest"; its own service stays off), git, rsync, curl. What is already on the
#     computer is left as it is; a decoder this release of the system does not have is a NOTE, not a stop.
#   - the Python module "ollama": the system's package where it has one, else from PyPI with pip (system-wide).
#   - Ollama and the AI model (about 4.7 GB). Ollama is installed by ITS OWN installer, https://ollama.com/install.sh:
#     a third-party script that runs as root. On a computer with an NVIDIA graphics card and no NVIDIA driver that
#     script also adds NVIDIA's package source and installs NVIDIA's driver. --no-ai skips all of it. If this part
#     fails, the rest is still installed.
#   - one file, /etc/modprobe.d/aris-rtl-sdr.conf, that keeps the kernel's television driver off RTL-SDR dongles
#     (the system's rtl-sdr packages bring the access rule for the dongles, but no such file).
#   - at the end it asks for the web page's login when there is a terminal, else it prints the command.
set -euo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd /       # python3 looks for modules in the current folder first: never run it as root in a folder others can write to
ROOT="${ARIS_ROOT:-}"      # empty on a real computer. The tests point it at a folder that stands in for "/".
REPO="${ARIS_REPO:-/opt/aris}"; DATA="${ARIS_DATA:-/var/lib/aris}"; UNIT=$ROOT/etc/systemd/system/aris.service
BLACKLIST=$ROOT/etc/modprobe.d/aris-rtl-sdr.conf
MODE=dry; EVERYTHING=0; AI=1
for a in "$@"; do
  case "$a" in --apply) MODE=apply;; --remove) MODE=remove;; --everything) EVERYTHING=1;; --no-ai) AI=0;; "") ;;
    *) echo "usage: $0 [--everything [--no-ai]] [--apply|--remove]"; exit 2;; esac
done
[[ $AI -eq 1 || $EVERYTHING -eq 1 ]] || { echo "--no-ai goes with --everything"; exit 2; }
ok(){ printf '  \e[32mOK\e[0m   %s\n' "$*"; }
warn(){ printf '  \e[33mNOTE\e[0m %s\n' "$*"; }
bad(){ printf '  \e[31mFIX\e[0m  %s\n' "$*"; FAIL=1; }
FAIL=0
[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0 $*"; exit 1; }
PATH=$PATH:/usr/local/sbin:/usr/sbin:/sbin      # a root shell from plain "su" lacks these (adduser, usermod, dpkg's helpers)

if [[ $MODE == remove ]]; then
  systemctl disable --now aris.service 2>/dev/null && ok "service stopped and disabled" || warn "no aris service was running"
  rm -f "$UNIT" && systemctl daemon-reload && ok "unit removed"
  if [[ -f $BLACKLIST ]]; then
    rm -f "$BLACKLIST" && ok "removed $BLACKLIST (after a restart the television driver may take RTL-SDR dongles again)"
    warn "kept: the packages, Ollama and its model that --everything installed"
  fi
  warn "kept: $REPO (code) and $DATA (your data). Delete them yourself if you want them gone."
  exit 0
fi

N=0; step(){ N=$((N + 1)); echo "$N. $*"; }
SETPW="sudo -u aris env ARIS_DATA_DIR=$DATA python3 $REPO/aris_gui.py --set-password"
SELF=$0; [[ -z ${ARIS_FROM_GET:-} ]] || SELF=$REPO/deploy/install.sh     # deploy/get.sh runs a copy it then removes
AGAIN="sudo bash $SELF --everything$([[ $AI -eq 1 ]] || echo ' --no-ai') --apply"

# rtl_433 23.11 is the first release that knows "-F log" (its changelog: "Added log output"). 21.12 and 22.11 answer
# "Invalid output format log" and stop. ARIS always passes it (it is how rtl_433 reports its own errors), so with an
# older rtl_433 a live decode ends at once.
r433_new(){   # does this rtl_433's help list "log" among its -F outputs?
  local h l; h=$(rtl_433 -h 2>&1 || true)
  while IFS= read -r l; do
    [[ $l == *"[-F "* ]] || continue
    [[ $l == *"[-F log "* || $l == *" log "* ]] && return 0
  done <<<"$h"
  return 1
}
r433_ver(){ local v; v=$(rtl_433 -V 2>&1 || true); if [[ $v =~ version\ ([^ ]+) ]]; then echo "${BASH_REMATCH[1]}"; else echo "an unknown version"; fi; }
R433_SAID=0
r433_old(){ R433_SAID=1
            warn "rtl_433 $1 is older than 23.11: ARIS always asks rtl_433 for its own log lines (\"-F log\"), and older"
            warn "  versions refuse that, so most sensor decoding will not work. Build a current rtl_433 into /usr/local/bin"
            warn "  (https://github.com/merbanan/rtl_433, docs/BUILDING.md; the service looks there first), or use a newer"
            warn "  release of the system (Debian 13, Ubuntu 24.04 or later)."; }

# ---------------------------------------------------------------------------------------------------------------
# What --everything adds (Debian and Ubuntu). Each part first looks (in the dry run that is all it does).
# ---------------------------------------------------------------------------------------------------------------
AI_MODEL=qwen2.5:7b                           # the default model of engine/core.py, about 4.7 GB
OLLAMA_SH=https://ollama.com/install.sh       # Ollama's own installer (third party)
PKGS=(); OPT_PKGS=(); PIP=(); AI_OLLAMA=0; AI_PULL=0; AI_SKIP=""; LISTS=1

osr(){ sed -n "/^$1=/{s/^$1=//;s/\"//g;p;q}" "$ROOT/etc/os-release" 2>/dev/null || true; }
has_systemd(){
  [[ -d $ROOT/run/systemd/system ]] && return 0
  local s; s=$(systemctl is-system-running 2>/dev/null || true)
  case "$s" in running|degraded|starting|initializing|maintenance|stopping) return 0;; esac
  return 1
}
is_wsl(){ local v; v=$(cat "$ROOT/proc/version" 2>/dev/null || true); [[ ${v,,} == *microsoft* ]]; }
pkg_has(){ [[ $(dpkg-query -W -f='${db:Status-Status}' "$1" 2>/dev/null || true) == installed ]]; }
pkg_cand(){ local c; c=$(LC_ALL=C apt-cache policy "$1" 2>/dev/null | awk '$1=="Candidate:"{print $2}') || c=""       # LC_ALL=C: apt answers in the user's language
            [[ $c == "(none)" ]] && c=""; echo "$c"; }
lists_here(){ local l; l=$(LC_ALL=C apt-cache policy 2>/dev/null || true); [[ $l == *" Packages"* ]]; }   # an installed package has a candidate also without lists
mod_ok(){ runuser -u nobody -- python3 -c "import $1" >/dev/null 2>&1; }
before_2311(){ [[ $1 =~ ^([0-9]+:)?([0-9]+)\.([0-9]+) ]] && (( 10#${BASH_REMATCH[2]} * 100 + 10#${BASH_REMATCH[3]} < 2311 )); }
has_model(){ local l; l=$(ollama list 2>/dev/null || true); [[ ${l,,} == *"$AI_MODEL"* ]]; }
ollama_up(){ curl -s -m 3 -o /dev/null http://127.0.0.1:11434/api/tags; }
has_tty(){ ( : </dev/tty ) 2>/dev/null; }
apt_install(){ DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get install -y -q --no-install-recommends \
                 -o DPkg::Lock::Timeout=120 "$@" </dev/null; }

computer(){
  local id like name
  id=$(osr ID); like=$(osr ID_LIKE); name=$(osr PRETTY_NAME)
  if command -v apt-get >/dev/null && [[ " $id $like " == *" debian "* || " $id $like " == *" ubuntu "* ]]; then
    ok "${name:-Debian or Ubuntu}: packages come from its own archive, with apt-get"
  else
    bad "--everything is for Debian and Ubuntu (it installs packages with apt-get); this is ${name:-another system}."
    bad "  Here, install the tools yourself (README, Quick start), then run this without --everything."
  fi
  if has_systemd; then ok "systemd is running (ARIS is installed as a systemd service)"
  elif is_wsl; then
    bad "systemd is not running in this WSL system. Add these two lines to /etc/wsl.conf:   [boot]   systemd=true"
    bad "  then run  wsl --shutdown  in Windows, open this system again and run this again."
  else
    bad "systemd is not running here (a container, or a system started without it), and ARIS is installed as a"
    bad "  systemd service. Without systemd, start ARIS by hand instead: python3 aris_gui.py (README, Quick start)."
  fi
}

need(){   # need <package> <required 1|0> <what it is>: queue a package that is not on the computer yet
  local c; c=$(pkg_cand "$1")
  if [[ $2 -eq 1 ]]; then PKGS+=("$1"); else OPT_PKGS+=("$1"); fi
  if [[ -n $c ]]; then warn "$3: not here yet, to install (package $1 $c)"
  elif [[ $LISTS -eq 0 ]]; then warn "$3: not here yet, to install (package $1)"
  else warn "$3: not here yet, and this computer's package lists have no package $1 (looked for again once they are refreshed)"; fi
}

packages(){   # looks only: fills PKGS (required), OPT_PKGS (a NOTE when they cannot be had) and PIP
  local p m c
  PKGS=(); OPT_PKGS=(); PIP=(); LISTS=1
  lists_here || { LISTS=0; warn "this computer has no package lists yet: what its release offers is known once they are fetched (apt-get update)"; }
  for p in python3 rsync git curl adduser; do
    if command -v "$p" >/dev/null; then ok "$p"; else need "$p" 1 "$p"; fi
  done
  if pkg_has ca-certificates; then ok "ca-certificates (to check https addresses)"; else need ca-certificates 1 "ca-certificates (to check https addresses)"; fi
  for m in numpy flask werkzeug; do
    if mod_ok "$m"; then ok "python module $m"
    elif pkg_has "python3-$m"; then PIP+=("$m"); warn "python module $m: the package python3-$m is here, but the python3 that runs first does not see it: to install with pip (PyPI, system-wide)"
    else need "python3-$m" 1 "python module $m"; fi
  done
  if mod_ok ollama; then ok "python module ollama"
  elif [[ -n $(pkg_cand python3-ollama) ]] && ! pkg_has python3-ollama; then need python3-ollama 1 "python module ollama"
  elif [[ $LISTS -eq 0 ]]; then PKGS+=(python3-ollama); warn "python module ollama: not here yet, to install (package python3-ollama where this release has it, else with pip from PyPI)"
  else
    PIP+=(ollama); warn "python module ollama: not here yet, and this release has no package for it: to install with pip (PyPI, system-wide)"
    # its needs (httpx and what that needs) come from the system's own package where there is one, so pip adds less
    if ! pkg_has python3-httpx && [[ -n $(pkg_cand python3-httpx) ]]; then OPT_PKGS+=(python3-httpx); fi
  fi
  if [[ ${#PIP[@]} -gt 0 ]] && ! python3 -m pip --version >/dev/null 2>&1; then need python3-pip 1 "pip (to install ${PIP[*]})"; fi
  if command -v rtl_sdr >/dev/null && command -v rtl_power >/dev/null && command -v rtl_test >/dev/null; then ok "rtl-sdr tools (RTL-SDR dongles)"
  else need rtl-sdr 0 "rtl-sdr tools (RTL-SDR dongles)"; fi
  if command -v rtl_433 >/dev/null; then
    if r433_new; then ok "rtl_433 $(r433_ver) (sensor decoding)"; else r433_old "$(r433_ver)"; fi
  else
    need rtl-433 0 "rtl_433 (sensor decoding)"
    c=$(pkg_cand rtl-433); if [[ -n $c ]] && before_2311 "$c"; then r433_old "in this release ($c)"; fi
  fi
  if command -v multimon-ng >/dev/null; then ok "multimon-ng (pager and tone decoders)"; else need multimon-ng 0 "multimon-ng (pager and tone decoders)"; fi
  if command -v atest >/dev/null; then ok "atest (packet decoder, from direwolf)"; else need direwolf 0 "atest (packet decoder; the package direwolf brings it, its own service stays off)"; fi
}

packages_install(){
  local p have=() gone=() pip=() m flags=() h
  [[ ${#PKGS[@]} -gt 0 || ${#OPT_PKGS[@]} -gt 0 || ${#PIP[@]} -gt 0 || $LISTS -eq 0 ]] || { ok "nothing to install"; return 0; }
  echo "  Fetching the package lists (apt-get update) ..."
  apt-get update -q </dev/null || warn "apt-get update reported a problem (see above): going on with the package lists as they are"
  for p in ${PKGS[@]+"${PKGS[@]}"}; do if [[ -n $(pkg_cand "$p") ]]; then have+=("$p"); else gone+=("$p"); fi; done
  if [[ ${#have[@]} -gt 0 ]]; then
    if apt_install "${have[@]}"; then ok "installed: ${have[*]}"; else bad "apt-get could not install: ${have[*]} (see above)"; return 0; fi
  fi
  for p in ${gone[@]+"${gone[@]}"}; do
    case "$p" in
      python3-numpy|python3-flask|python3-werkzeug|python3-ollama) warn "no package $p in this release: the module comes from pip instead";;
      *) bad "no package $p in this computer's package sources (on Ubuntu some of these are in \"universe\": sudo add-apt-repository universe)";;
    esac
  done
  for p in ${OPT_PKGS[@]+"${OPT_PKGS[@]}"}; do
    if [[ -z $(pkg_cand "$p") ]]; then
      warn "no package $p in this computer's package sources: left out (on Ubuntu these are in \"universe\": sudo add-apt-repository universe)"
    elif apt_install "$p"; then ok "installed: $p"
    else warn "apt-get could not install $p (see above): left out"; fi
  done
  hash -r
  # Python modules: whatever the packages did not bring for the python3 that runs first comes from PyPI.
  if command -v python3 >/dev/null; then
    for m in numpy flask werkzeug ollama; do mod_ok "$m" || pip+=("$m"); done
  fi
  if [[ ${#pip[@]} -gt 0 ]]; then
    if ! python3 -m pip --version >/dev/null 2>&1; then
      if [[ -n $(pkg_cand python3-pip) ]] && apt_install python3-pip; then ok "installed: python3-pip"; else warn "pip could not be installed"; fi
    fi
    h=$(python3 -m pip install --help 2>/dev/null || true)      # newer systems refuse pip outside a virtual
    [[ $h == *--break-system-packages* ]] && flags+=(--break-system-packages)   # environment without this flag;
    [[ $h == *--root-user-action* ]] && flags+=(--root-user-action=ignore)      # older pip does not know it
    echo "  Installing with pip, system-wide: ${pip[*]} ..."
    if ( umask 022; python3 -m pip install -q --disable-pip-version-check --no-input ${flags[@]+"${flags[@]}"} "${pip[@]}" </dev/null ); then
      ok "python modules from PyPI: ${pip[*]}"
    else warn "pip could not install: ${pip[*]} (see above)"; fi
  fi
  if [[ $R433_SAID -eq 0 ]] && command -v rtl_433 >/dev/null && ! r433_new; then r433_old "$(r433_ver)"; fi
}

ai_look(){   # looks only: sets AI_OLLAMA, AI_PULL and, when the disk is too full, AI_SKIP
  local free="" gb=6 at=/usr/share
  AI_OLLAMA=0; AI_PULL=0; AI_SKIP=""
  if [[ $AI -eq 0 ]]; then warn "left out (--no-ai): the chat still answers direct commands. Later: install Ollama (https://ollama.com), then  ollama pull $AI_MODEL"; return 0; fi
  if command -v ollama >/dev/null; then
    ok "Ollama is installed"
    if has_model; then ok "the model $AI_MODEL is there"
    else AI_PULL=1; warn "the model $AI_MODEL is not there yet (or Ollama is not running): to download, about 4.7 GB"; fi
  else
    AI_OLLAMA=1; AI_PULL=1; gb=10
    warn "Ollama is not installed: to install with Ollama's own installer, $OLLAMA_SH"
    warn "  (a third-party script that runs as root: read it first if you like, or leave the AI part out with --no-ai)"
    warn "the model $AI_MODEL: to download, about 4.7 GB"
  fi
  [[ $AI_PULL -eq 1 ]] || return 0
  [[ -d /usr/share/ollama ]] && at=/usr/share/ollama
  free=$(df -Pk "$at" 2>/dev/null | awk 'NR==2{printf "%d", $4 / 1048576}') || free=""
  if [[ -n $free ]] && (( free < gb )); then
    AI_SKIP="only $free GB free under $at, and the AI part needs about $gb GB: left out. Free some space and run this again."
    AI_OLLAMA=0; AI_PULL=0; warn "$AI_SKIP"; return 0
  elif [[ -n $free ]]; then ok "$free GB free under $at (the AI part needs about $gb GB)"; fi
  if [[ $AI_OLLAMA -eq 1 ]] && ! command -v zstd >/dev/null; then
    OPT_PKGS+=(zstd); warn "zstd: not here yet, to install (Ollama's installer may need it to unpack its download)"
  fi
}

ai_install(){   # never stops the install: every problem here is a NOTE
  local f i up=0
  [[ $AI -eq 1 && -z $AI_SKIP ]] || return 0
  [[ $AI_OLLAMA -eq 1 || $AI_PULL -eq 1 ]] || return 0
  if [[ $AI_OLLAMA -eq 1 ]]; then
    echo "  Getting Ollama's own installer from $OLLAMA_SH and running it (not part of ARIS) ..."
    f=$(mktemp)
    if curl -fsSL --proto '=https' --tlsv1.2 -o "$f" "$OLLAMA_SH" && sh "$f" </dev/null; then ok "Ollama's installer finished"
    else rm -f "$f"; warn "Ollama could not be installed (see above). ARIS is installed without the AI part; to add it later: $AGAIN"; return 0; fi
    rm -f "$f"; hash -r
  fi
  command -v ollama >/dev/null || { warn "the program ollama is not there after its installer ran. To add the AI part later: $AGAIN"; return 0; }
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    if ollama_up; then up=1; break; fi
    [[ $i -eq 1 ]] && { systemctl start ollama.service 2>/dev/null || true; }
    sleep 2
  done
  [[ $up -eq 1 ]] || { warn "Ollama does not answer on 127.0.0.1:11434 (sudo systemctl status ollama). Once it does:  ollama pull $AI_MODEL"; return 0; }
  if has_model; then ok "the model $AI_MODEL is there"; return 0; fi
  echo "  Downloading the model $AI_MODEL (about 4.7 GB: this can take a long time) ..."
  if ollama pull "$AI_MODEL" </dev/null; then ok "the model $AI_MODEL is downloaded"
  else warn "the model could not be downloaded (see above). Later:  ollama pull $AI_MODEL"; fi
}

tv_driver_look(){
  if grep -rqsE '^[[:space:]]*blacklist[[:space:]]+dvb_usb_rtl28xxu' "$ROOT/etc/modprobe.d" "$ROOT/usr/lib/modprobe.d" "$ROOT/lib/modprobe.d" "$ROOT/run/modprobe.d"; then
    ok "the kernel's television driver is already kept off RTL-SDR dongles (a blacklist of dvb_usb_rtl28xxu is in place)"; return 0
  fi
  return 1
}
tv_driver(){
  tv_driver_look && return 0
  mkdir -p "$(dirname "$BLACKLIST")"
  cat > "$BLACKLIST" <<'CONF'
# Written by ARIS (deploy/install.sh --everything); "install.sh --remove" deletes it again.
# An RTL-SDR dongle is a television stick to the kernel, and its DVB-T driver takes the dongle when it is plugged
# in. The system's rtl-sdr packages bring no file like this one: their library takes the dongle back from that
# driver every time a program opens it. ARIS opens the dongles very often, so this keeps the driver off for good.
# Delete this file if you watch television with such a stick on this computer.
blacklist dvb_usb_rtl28xxu
CONF
  chmod 644 "$BLACKLIST" && ok "$BLACKLIST written (keeps the kernel's television driver off RTL-SDR dongles)"
  if [[ -d $ROOT/sys/module/dvb_usb_rtl28xxu ]]; then
    if modprobe -r dvb_usb_rtl28xxu 2>/dev/null; then ok "the television driver, which was loaded, is unloaded"
    else warn "the television driver is loaded and in use: it stays until the computer is restarted"; fi
  fi
}

login(){   # ask for the web page's login on the terminal (the service starts by itself once there is one)
  local i
  if [[ -s $DATA/gui_secrets.json ]]; then ok "a login is set already (to change it: $SETPW)"; return 0; fi
  if ! has_tty; then warn "no terminal here to ask for the login. Set it with:  $SETPW"; return 0; fi
  echo "  Choose the name and the password for the web page (the password is not shown while you type)."
  if runuser -u aris -- env ARIS_DATA_DIR="$DATA" python3 "$REPO/aris_gui.py" --set-password </dev/tty; then
    ok "login set"
    for i in 1 2 3 4 5 6 7 8 9 10; do systemctl is-active --quiet aris.service && break; sleep 2; done
    systemctl is-active --quiet aris.service && ok "service running (logs: journalctl -u aris -f)" \
      || bad "the service did not start: journalctl -u aris -n 50"
  else warn "no login was set. Set it with:  $SETPW"; fi
}

SOFT=0   # 1 in the --everything dry run: what the install itself brings is a NOTE in the checks, not a FIX
if [[ $EVERYTHING -eq 1 ]]; then
  [[ $MODE == dry ]] && SOFT=1
  step "This computer"; computer
  [[ $FAIL -eq 0 ]] || { echo "Fix the items marked FIX first. Nothing was changed."; exit 1; }
  step "Packages"; packages
  step "AI part"; ai_look
  if [[ $MODE == apply ]]; then
    umask 022
    step "Installing the packages"; packages_install
    [[ $FAIL -eq 0 ]] || { echo "Fix the items marked FIX first. ARIS itself was not installed."; exit 1; }
    step "Installing the AI part"
    if [[ $AI -eq 0 ]]; then warn "left out (--no-ai)"; elif [[ -n $AI_SKIP ]]; then warn "$AI_SKIP"
    elif [[ $AI_OLLAMA -eq 0 && $AI_PULL -eq 0 ]]; then ok "nothing to do"; else ai_install; fi
  fi
fi

step "Checks"
if [[ $SOFT -eq 1 ]] && ! command -v python3 >/dev/null; then warn "python3 is not here yet: the install brings it"
else python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' && ok "$(python3 --version)" || bad "Python 3.8 or newer needed"; fi
if runuser -u nobody -- python3 -c 'import numpy, flask, werkzeug, ollama' 2>/dev/null; then ok "python modules numpy, flask, werkzeug, ollama (visible to an ordinary user)"
elif [[ $SOFT -eq 1 ]]; then warn "python modules numpy, flask, werkzeug, ollama: not all there yet for an ordinary user (the install brings them)"
else bad "an ordinary user cannot import numpy / flask / werkzeug / ollama: install them system-wide (apt, or pip install --break-system-packages numpy flask werkzeug ollama)"; fi
RX=0
if command -v rtl_sdr >/dev/null && command -v rtl_power >/dev/null && command -v rtl_test >/dev/null; then ok "rtl-sdr tools (RTL-SDR dongles)"; RX=1; fi
if command -v SoapySDRUtil >/dev/null && command -v rx_sdr >/dev/null && command -v rx_power >/dev/null; then ok "SoapySDR and rx_tools (other SDRs)"; RX=1; fi
[[ $RX -eq 1 ]] || warn "no receiver tools found: apt install rtl-sdr (RTL-SDR dongles), or SoapySDR + rx_tools, or a device plug-in"
if ! command -v rtl_433 >/dev/null; then warn "rtl_433 not found: apt install rtl-433 (most decoding needs it)"
elif r433_new; then ok "rtl_433 (sensor decoding)"
elif [[ $EVERYTHING -eq 1 ]]; then warn "rtl_433 $(r433_ver) is too old for ARIS (see above)"
else r433_old "$(r433_ver)"; fi
for t in multimon-ng atest rtl_adsb; do command -v $t >/dev/null && ok "$t" || warn "$t not found (optional decoder)"; done
getent group plugdev >/dev/null && ok "group plugdev exists" || warn "no group plugdev (the rtl-sdr package creates it with its udev rule)"
if [[ $EVERYTHING -eq 1 && $AI -eq 0 ]]; then warn "Ollama: left out (--no-ai)"
else curl -s -m 3 -o /dev/null http://127.0.0.1:11434/api/tags && ok "Ollama answers on 127.0.0.1:11434" || warn "Ollama does not answer: the chat works for direct commands, the AI part needs it (ollama pull qwen2.5:7b)"; fi
if [[ $SOFT -eq 1 ]] && ! command -v python3 >/dev/null; then warn "the code is compiled as a check once python3 is there"
else python3 -c 'import sys
for f in sys.argv[1:]: compile(open(f, "rb").read(), f, "exec")' "$SRC/aris.py" "$SRC/aris_gui.py" "$SRC"/engine/*.py "$SRC"/arislib/*.py "$SRC"/arisweb/*.py \
  && ok "code compiles ($(grep -m1 '^VERSION' "$SRC/aris.py"))" || bad "the code does not compile"; fi   # in memory: a dry run writes nothing
[[ -f $SRC/deploy/aris.service ]] && ok "deploy/aris.service" || bad "deploy/aris.service missing"
if [[ $EVERYTHING -eq 1 && $MODE == apply ]]; then
  [[ $FAIL -eq 0 ]] || { echo "Fix the items marked FIX first. The packages above were installed; ARIS itself was not."; exit 1; }
else
  [[ $FAIL -eq 0 ]] || { echo "Fix the items marked FIX first. Nothing was changed."; exit 1; }
fi

step "Plan"
if [[ $EVERYTHING -eq 1 && $MODE == dry ]]; then
  ALL=(${PKGS[@]+"${PKGS[@]}"} ${OPT_PKGS[@]+"${OPT_PKGS[@]}"})
  if [[ ${#ALL[@]} -gt 0 ]]; then
    echo "  - fetch the package lists (apt-get update) and install from the system's own archive, with apt-get: ${ALL[*]}"
    echo "    (only these and what they need; nothing that is already here is changed)"
  elif [[ ${#PIP[@]} -gt 0 ]]; then echo "  - fetch the package lists (apt-get update); no package to install"
  else echo "  - packages: nothing to install"; fi
  [[ $LISTS -eq 1 ]] || echo "  - the Python module ollama: the package python3-ollama where this release has it, else with pip from PyPI"
  [[ ${#PIP[@]} -eq 0 ]] || echo "  - install with pip, system-wide, from PyPI: ${PIP[*]} (and what they need; they go to /usr/local/lib/python3*/dist-packages, where every Python program on this computer finds them first)"
  if [[ $AI -eq 0 ]]; then echo "  - the AI part is left out (--no-ai)"
  elif [[ -n $AI_SKIP ]]; then echo "  - the AI part is left out: the disk is too full for it"
  else
    if [[ $AI_OLLAMA -eq 1 ]]; then
      echo "  - Ollama: download $OLLAMA_SH and run it as root. This is Ollama's own installer: a third-party"
      echo "    script, not part of ARIS. It installs Ollama as a service of its own. On a computer with an NVIDIA graphics"
      echo "    card and no NVIDIA driver it also adds NVIDIA's package source and installs NVIDIA's driver."
      echo "    Leave all of it out with --no-ai."
    fi
    [[ $AI_PULL -eq 0 ]] || echo "  - download the AI model: ollama pull $AI_MODEL (about 4.7 GB)"
    [[ $AI_OLLAMA -eq 1 || $AI_PULL -eq 1 ]] || echo "  - the AI part: nothing to do"
  fi
  tv_driver_look >/dev/null || echo "  - write $BLACKLIST: it keeps the kernel's television driver (dvb_usb_rtl28xxu) off RTL-SDR dongles (and unload that driver now if it is loaded and not in use)"
fi
cat <<PLAN
  - create the system user aris (no login, home $DATA, groups aris and plugdev)
  - copy $SRC -> $REPO (owned by root, not writable by aris)
  - create $DATA (owned by aris, mode 700) if it is not there; existing data is kept
  - install deploy/aris.service as $UNIT, enable it at boot and (re)start it
PLAN
if [[ $EVERYTHING -eq 1 && $MODE == dry ]]; then
  echo "  - ask for the web page's login (name and password) when there is a terminal, else print the command for it"
  echo
  if [[ -n ${ARIS_FROM_GET:-} ]]; then echo "Dry run: nothing was changed."; else echo "Dry run: nothing was changed. To install:  $AGAIN"; fi
  exit 0
fi
[[ $MODE == dry ]] && { echo; echo "Dry run: nothing was changed. To install:  sudo bash $0 --apply"; exit 0; }

step "Installing"
id aris >/dev/null 2>&1 && ok "user aris exists" || { adduser --system --group --home "$DATA" --no-create-home --shell /usr/sbin/nologin aris && ok "user aris created"; }
getent group plugdev >/dev/null && usermod -aG plugdev aris && ok "aris is in plugdev"
systemctl stop aris.service 2>/dev/null && ok "old service stopped" || true
mkdir -p "$REPO" && rsync -a --delete --exclude '__pycache__' --exclude 'test/shots' "$SRC"/ "$REPO"/ \
  && chown -R root:root "$REPO" && chmod -R a+rX,go-w "$REPO" && ok "code in $REPO"
mkdir -p "$DATA" && chown aris:aris "$DATA" && chmod 700 "$DATA" && ok "data folder $DATA"
[[ $EVERYTHING -eq 0 ]] || tv_driver
sed "s#@REPO@#$REPO#g" "$REPO/deploy/aris.service" > "$UNIT" && systemctl daemon-reload && ok "service unit installed"
systemctl enable --now aris.service >/dev/null 2>&1; sleep 5
if systemctl is-active --quiet aris.service; then ok "service running (logs: journalctl -u aris -f)"
elif [[ ! -s $DATA/gui_secrets.json ]]; then warn "the service waits for a login: it starts by itself a few seconds after one is set"
else bad "the service did not start: journalctl -u aris -n 50"; fi
IP=$(hostname -I 2>/dev/null | awk '{print $1}') || IP=""
if [[ $EVERYTHING -eq 1 ]]; then
  step "Login"; login
  OPEN="http://localhost:5000 on this computer, or http://${IP:-<the address of this computer>}:5000 from another device on your network"
  ! is_wsl || OPEN="http://localhost:5000 in a browser on this Windows computer (from other devices: docs/WINDOWS.md says how)"
  cat <<NEXT
$((N + 1)). Next
  Open:     $OPEN
  Then:     in the chat type /check (finds and tests the receivers and decoders), then /setup (calibrates them)
  Dongles:  a dongle that was plugged in before this install: unplug it and plug it in again, so the new rules apply
  Login:    $SETPW   (sets or changes it)
  Updates:  sudo bash $REPO/deploy/update.sh
  Keep it off the network (local only): add Environment=ARIS_GUI_HOST=127.0.0.1 to the unit, or see
            deploy/tailscale-https.sh for HTTPS through your own tailnet.
NEXT
  exit $FAIL
fi
cat <<NEXT
$((N + 1)). Next
  Set the login:   $SETPW
  Open:            http://${IP:-<this computer>}:5000   then in the chat: /check, then /setup
  Updates:         sudo bash $REPO/deploy/update.sh
  Keep it off the network (local only): add Environment=ARIS_GUI_HOST=127.0.0.1 to the unit, or see
                   deploy/tailscale-https.sh for HTTPS through your own tailnet.
NEXT
exit $FAIL
