#!/usr/bin/env bash
# Get the latest ARIS from GitHub and restart it.   bash deploy/update.sh
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
before=$(git rev-parse --short HEAD)
git pull --ff-only
after=$(git rev-parse --short HEAD)
python3 -m py_compile aris.py aris_gui.py aris_node.py engine/*.py arislib/*.py arisweb/*.py
echo "ARIS $(grep -m1 '^VERSION' aris.py) ($before -> $after)"
[[ "$before" == "$after" ]] && { echo "Already up to date."; exit 0; }
git --no-pager log --oneline "$before..$after" | head -20
if systemctl is-active --quiet aris.service 2>/dev/null; then
  systemctl restart aris.service && echo "Restarted the aris service."
else
  echo "The aris service is not running: sudo systemctl start aris   (by hand: python3 $REPO/aris_gui.py)"
fi
