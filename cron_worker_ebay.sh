#!/bin/bash
# Kensa worker 3/3: eBay-fetcher voor cache-misses. Pakt slab_status=ocr_done
# zonder verse price_cache. Traag — 30-80s per item.
# Elke 10 min. Kleine batch om binnen timeout te blijven.

set -u
KENSA_DIR="/home/pi/.openclaw/workspace/agents/kensa"
VENV_PY="/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3"
LOCK="/tmp/kensa-worker-ebay.lock"
LIMIT="${WORKER_EBAY_LIMIT:-20}"
PARALLEL="${WORKER_EBAY_PARALLEL:-1}"

# Storage-migratie: writes direct naar Supabase (analyze._save_trap/_mark_slab_status/enqueue_cardmarket).
# Pick/claim gaat via Supabase (pick_ebay_batch_supabase) zodra KENSA_READ_STORAGE=supabase; worker_claim (SQLite-lock) is alleen nog de terugval zonder die env-var.
export KENSA_WRITE_STORAGE=dual
# CUTOVER 2026-09-05 17:07 — reads uit Supabase (rollback: comment onderstaande regel uit)
export KENSA_READ_STORAGE=supabase

cd "$KENSA_DIR" || exit 1

exec 9>"$LOCK"
if ! /usr/bin/flock -n 9; then
  echo "[$(date -Iseconds)] worker-ebay already running — skip"
  exit 0
fi

echo ""
echo "======== $(date -Iseconds) worker-ebay start (limit=$LIMIT parallel=$PARALLEL) ========"
/usr/bin/timeout --kill-after=60s 540s "$VENV_PY" worker_ebay.py --limit "$LIMIT" --parallel "$PARALLEL" 2>&1
echo "======== $(date -Iseconds) worker-ebay done ========"
