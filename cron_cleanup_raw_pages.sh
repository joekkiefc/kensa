#!/bin/bash
# Kensa daily cleanup: verwijdert raw_pages ouder dan 3 dagen.
# Draait dagelijks 04:00 via crontab. Logs naar pipeline_cleanup.log.

set -u
KENSA_DIR="/home/pi/.openclaw/workspace/agents/kensa"
VENV_PY="/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3"
LOCK="/tmp/kensa-cleanup-raw-pages.lock"
RETENTION_DAYS="${KENSA_RAW_RETENTION_DAYS:-3}"

cd "$KENSA_DIR" || exit 1

exec 9>"$LOCK"
if ! /usr/bin/flock -n 9; then
  echo "[$(date -Iseconds)] cleanup-raw-pages already running — skip"
  exit 0
fi

echo ""
echo "======== $(date -Iseconds) cleanup-raw-pages start (retention=${RETENTION_DAYS}d) ========"
"$VENV_PY" - <<PYEOF
import sys
sys.path.insert(0, "$KENSA_DIR")
import storage
n = storage.cleanup_raw_pages(days=${RETENTION_DAYS})
print(f"deleted_rows={n}")
PYEOF
echo "======== $(date -Iseconds) cleanup-raw-pages done ========"
