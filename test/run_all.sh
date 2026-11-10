#!/usr/bin/env bash
# All automated checks, with simulated radios (no dongles needed).   bash test/run_all.sh [--shots]
#   --shots  also start the GUI and save phone-width screenshots of Spectrum, Map, Decode, Activity (incl. Clear logs),
#            Devices and chat
#            (needs Playwright)
set -euo pipefail
T="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; R="$(dirname "$T")"
cd "$R"
echo "== compile"; python3 -m py_compile aris.py aris_gui.py aris_node.py engine/*.py arislib/*.py arisweb/*.py plugins/*.py test/*.py && echo ok
for f in arisweb/static/js/*.js; do command -v node >/dev/null && node --check "$f"; done
cd "$T"
if python3 -c "import pytest" 2>/dev/null; then python3 -m pytest -q test_unit.py test_api.py test_node.py test_radios.py test_engine.py test_flow.py
else python3 -m unittest -v test_unit test_api test_node test_radios test_engine test_flow; fi
if [[ "${1:-}" == "--shots" ]]; then
  PORT=5099; OUT="$T/shots"; mkdir -p "$OUT"
  FAKE_WD_PERIOD=1 ARIS_SEED_DEVICES=1 python3 serve.py $PORT > "$OUT/serve.log" 2>&1 & PID=$!
  trap 'kill $PID 2>/dev/null' EXIT
  sleep 6; python3 shot46.py $PORT "$OUT"; python3 shot47.py $PORT "$OUT"; python3 shot48.py $PORT "$OUT"
  python3 shot49.py $PORT "$OUT"
  echo "screenshots in $OUT"
fi
