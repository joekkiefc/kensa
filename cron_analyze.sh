#!/bin/bash
# Kensa analyze-cron — analyseer listings die detail hebben maar nog geen summary.
# Draait elke 10 min. Eigen flock. Parallel-analyze in analyze.py (3 threads).
# Doet OCR/LLM/eBay/CM per item.

set -u

KENSA_DIR="/home/pi/.openclaw/workspace/agents/kensa"
VENV_PY="/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3"
LOCK="/tmp/kensa-analyze.lock"

ANALYZE_LIMIT=40

cd "$KENSA_DIR" || exit 1

exec 9>"$LOCK"
if ! /usr/bin/flock -n 9; then
  echo "[$(date -Iseconds)] analyze already running — skip"
  exit 0
fi

echo ""
echo "======== $(date -Iseconds) kensa analyze start ========"

echo "--- analyze --all --limit $ANALYZE_LIMIT (max 12 min, parallel 3 threads) ---"
/usr/bin/timeout --kill-after=30s 720s "$VENV_PY" analyze.py --all --limit "$ANALYZE_LIMIT" 2>&1 | tail -20

echo "======== $(date -Iseconds) kensa analyze done ========"
