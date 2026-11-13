#!/usr/bin/env bash
# ARIS on a Debian or Ubuntu computer with one command (no checkout needed):
#
#   curl -fsSL https://raw.githubusercontent.com/bradderz143/ARIS-RF/main/deploy/get.sh | sudo bash
#
# What it does, in this order:
#   1. checks that this is Debian or Ubuntu with apt-get and a running systemd, and that git is there
#      (git is installed with apt-get when it is missing: the one change made before you are asked);
#   2. fetches ARIS from https://github.com/bradderz143/ARIS-RF.git into a temporary folder;
#   3. shows the plan: the dry run of "deploy/install.sh --everything" from that folder, which changes nothing;
#   4. asks "Install? [y/N]" on the terminal;
#   5. on "y" runs "deploy/install.sh --everything --apply" (read the top of that file for what it installs);
#   6. removes the temporary folder, whatever the answer was.
#
# Options go after "bash -s --":
#   curl -fsSL https://raw.githubusercontent.com/bradderz143/ARIS-RF/main/deploy/get.sh | sudo bash -s -- --yes
#   --yes            do not ask before installing (needed where there is no terminal to ask on; without it nothing is
#                    installed there). On a terminal the web page's login is still asked for at the end.
#   --no-ai          leave out Ollama and the AI model
#   --branch NAME    fetch another branch than main
# For a fork, or for tests: the environment variable ARIS_GET_URL names another https:// address, or a folder on
# this computer that holds a checkout. Nothing but https is used over the network.
#
# The whole script is one function, called on the last line: a download that was cut off runs nothing.
aris_get() {
  set -euo pipefail
  umask 022     # the code is installed with the modes it is fetched with, and the service user must be able to read it
  cd /          # nothing is run from the folder this was started in
  local url="${ARIS_GET_URL:-https://github.com/bradderz143/ARIS-RF.git}" branch=main root="${ARIS_ROOT:-}"
  local line="curl -fsSL https://raw.githubusercontent.com/bradderz143/ARIS-RF/main/deploy/get.sh | sudo bash"
  local yes=0 sysd=0 pass=() allow=() id like name ver ans rc
  ARIS_GET_TMP=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --yes|-y) yes=1;;
      --no-ai) pass+=(--no-ai);;
      --branch) [[ $# -ge 2 ]] || { echo "--branch needs a name"; exit 2; }; branch=$2; shift;;
      *) echo "usage: $line -s -- [--yes] [--no-ai] [--branch NAME]"; exit 2;;
    esac
    shift
  done
  [[ $EUID -eq 0 ]] || { echo "Run as root:  $line"; exit 1; }
  PATH=$PATH:/usr/local/sbin:/usr/sbin:/sbin      # a root shell from plain "su" lacks these (dpkg's helpers)

  if [[ $yes -eq 1 ]]; then echo "ARIS: installing on this computer (--yes: it does not ask before it installs; on a terminal it still asks for the web page's login at the end)."
  else echo "ARIS: install on this computer. Nothing is installed before you say yes (except git, if it is missing)."; fi
  id=$(sed -n '/^ID=/{s/^ID=//;s/"//g;p;q}' "$root/etc/os-release" 2>/dev/null || true)
  like=$(sed -n '/^ID_LIKE=/{s/^ID_LIKE=//;s/"//g;p;q}' "$root/etc/os-release" 2>/dev/null || true)
  name=$(sed -n '/^PRETTY_NAME=/{s/^PRETTY_NAME=//;s/"//g;p;q}' "$root/etc/os-release" 2>/dev/null || true)
  if ! command -v apt-get >/dev/null || [[ " $id $like " != *" debian "* && " $id $like " != *" ubuntu "* ]]; then
    echo "This installer is for Debian and Ubuntu (it installs packages with apt-get); this is ${name:-another system}."
    echo "Here: get the code with git, install the tools yourself and run deploy/install.sh (README, Quick start)."
    exit 1
  fi
  sysd=0
  if [[ -d $root/run/systemd/system ]]; then sysd=1
  else
    case "$(systemctl is-system-running 2>/dev/null || true)" in
      running|degraded|starting|initializing|maintenance|stopping) sysd=1;;
    esac
  fi
  if [[ $sysd -eq 0 ]]; then
    ver=$(cat "$root/proc/version" 2>/dev/null || true)
    if [[ ${ver,,} == *microsoft* ]]; then
      echo "systemd is not running in this WSL system. Add these two lines to /etc/wsl.conf:"
      echo "  [boot]"
      echo "  systemd=true"
      echo "then run  wsl --shutdown  in Windows, open this system again and run this again."
    else
      echo "systemd is not running here (a container, or a system started without it), and ARIS is installed as a"
      echo "systemd service. Without systemd: get the code with git and start it by hand (README, Quick start)."
    fi
    exit 1
  fi
  case "$url" in
    https://*) allow=(-c protocol.allow=never -c protocol.https.allow=always);;      # the network: https only
    /*) [[ -d $url ]] || { echo "ARIS_GET_URL: no such folder: $url"; exit 2; }      # a checkout on this computer
        allow=(-c protocol.allow=never -c protocol.file.allow=always);;
    *) echo "ARIS_GET_URL must be an https:// address (or a folder on this computer): $url"; exit 2;;
  esac

  if ! command -v git >/dev/null; then
    echo "git is needed to fetch ARIS: installing it (apt-get update; apt-get install git ca-certificates) ..."
    apt-get update -q </dev/null || true
    DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get install -y -q --no-install-recommends \
      -o DPkg::Lock::Timeout=120 git ca-certificates </dev/null || { echo "git could not be installed (see above). Nothing else was changed."; exit 1; }
  fi

  ARIS_GET_TMP=$(mktemp -d "${TMPDIR:-/tmp}/aris-get.XXXXXXXX")
  # shellcheck disable=SC2317  # reached through the trap
  aris_get_cleanup() {
    if [[ -n ${ARIS_GET_TMP:-} && -d $ARIS_GET_TMP && $ARIS_GET_TMP == */aris-get.* ]]; then
      rm -rf -- "$ARIS_GET_TMP" && echo "Removed the temporary folder $ARIS_GET_TMP."
    fi
    ARIS_GET_TMP=""
  }
  trap aris_get_cleanup EXIT
  echo "Fetching ARIS (branch $branch) from $url ..."
  if ! GIT_TERMINAL_PROMPT=0 git "${allow[@]}" clone --quiet --branch "$branch" -- "$url" "$ARIS_GET_TMP/aris" </dev/null; then
    echo "The code could not be fetched (see above). Nothing was installed."; exit 1
  fi
  if [[ ! -f $ARIS_GET_TMP/aris/deploy/install.sh ]] || ! grep -q -- '--everything' "$ARIS_GET_TMP/aris/deploy/install.sh"; then
    echo "The branch $branch has no installer that knows --everything. Nothing was installed."; exit 1
  fi
  ver=$(sed -n 's/^VERSION = "\(.*\)".*/\1/p;T;q' "$ARIS_GET_TMP/aris/aris.py" 2>/dev/null || true)
  echo "ARIS ${ver:-(version unknown)}, commit $(git -C "$ARIS_GET_TMP/aris" rev-parse --short HEAD 2>/dev/null || echo unknown). The plan:"
  echo

  rc=0
  ARIS_FROM_GET=1 bash "$ARIS_GET_TMP/aris/deploy/install.sh" --everything ${pass[@]+"${pass[@]}"} </dev/null || rc=$?
  if [[ $rc -ne 0 ]]; then echo; echo "Nothing was installed."; exit "$rc"; fi
  echo
  if [[ $yes -eq 0 ]]; then
    if ! ( : </dev/tty ) 2>/dev/null; then
      echo "There is no terminal to ask on, so nothing was installed. To install without the question:"
      echo "  $line -s -- --yes"
      exit 1
    fi
    printf 'Install? [y/N] ' >/dev/tty
    ans=""; read -r ans </dev/tty || true
    case "$ans" in
      y|Y|yes|Yes|YES) ;;
      *) echo "Nothing was installed."; exit 0;;
    esac
  fi
  rc=0
  ARIS_FROM_GET=1 bash "$ARIS_GET_TMP/aris/deploy/install.sh" --everything --apply ${pass[@]+"${pass[@]}"} </dev/null || rc=$?
  exit "$rc"
}
aris_get "$@"
