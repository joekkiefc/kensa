#!/bin/bash
# Kensa detail — MERCAPI ONLY (Mercari API, snel, geen scraping rate-limit).
# Gesplitst uit cron_detail.sh zodat we mercari kunnen versnellen zonder de
# Buyee/proxy scrapers te belasten (die zitten nog in cron_detail.sh op */10).
#
# Cron: */3 min. Fetch --all + recheck.

set -u
KENSA_DIR="/home/pi/.openclaw/workspace/agents/kensa"
VENV_PY="/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3"
LOCK="/tmp/kensa-detail-mercapi.lock"

MERCAPI_LIMIT="${MERCAPI_LIMIT:-250}"
MERCAPI_RECHECK_LIMIT="${MERCAPI_RECHECK_LIMIT:-100}"
MERCAPI_RECHECK_MIN_AGE_HOURS="${MERCAPI_RECHECK_MIN_AGE_HOURS:-6}"

cd "$KENSA_DIR" || exit 1

exec 9>"$LOCK"
if ! /usr/bin/flock -n 9; then
  echo "[$(date -Iseconds)] mercapi detail already running — skip"
  exit 0
fi

echo ""
echo "======== $(date -Iseconds) kensa detail-mercapi start ========"

# CUTOVER 2026-09-05 17:55 — mercapi leest queue uit Supabase.
echo "--- fetch_detail_mercapi --all $MERCAPI_LIMIT (max 8 min, READ=supabase) ---"
/usr/bin/timeout --kill-after=30s 480s env KENSA_READ_STORAGE=supabase KENSA_WRITE_STORAGE=dual "$VENV_PY" fetch_detail_mercapi.py --all "$MERCAPI_LIMIT" 2>&1 | tail -20

echo "--- fetch_detail_mercapi --recheck $MERCAPI_RECHECK_LIMIT ${MERCAPI_RECHECK_MIN_AGE_HOURS}u (max 3 min, READ=supabase) ---"
/usr/bin/timeout --kill-after=30s 180s env KENSA_READ_STORAGE=supabase KENSA_WRITE_STORAGE=dual "$VENV_PY" fetch_detail_mercapi.py --recheck "$MERCAPI_RECHECK_LIMIT" "$MERCAPI_RECHECK_MIN_AGE_HOURS" 2>&1 | tail -10

echo "======== $(date -Iseconds) kensa detail-mercapi done ========"
