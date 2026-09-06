#!/bin/bash
# Kensa worker 2/3: cache-hit finalizer. Pakt slab_status=ocr_done + verse price_cache.
# Elke 5 min. Snel — ~1s per item, dus grote batch OK.

set -u
KENSA_DIR="/home/pi/.openclaw/workspace/agents/kensa"
VENV_PY="/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3"
LOCK="/tmp/kensa-worker-cache.lock"
LIMIT="${WORKER_CACHE_LIMIT:-200}"
PARALLEL="${WORKER_CACHE_PARALLEL:-4}"

# Storage-migratie: writes gaan naar Pi + Supabase (dual).
# READS staan nog op Pi omdat analyze.py's downstream reads (slab_data, cm_lookup)
# nog niet volledig Supabase-first zijn. pick_batch swap zonder analyze-migratie
# leverde "no slab data" crashes op (item wel bekend op Supabase, slab-JSON alleen
# op Pi). Volledige switch komt in gecombineerde sessie met detail+ebay reads.
export KENSA_WRITE_STORAGE=dual
# Fase F1 (dev-mode cutover 2026-09-05): reads via Supabase.
# Als er problemen zijn: comment deze regel uit → worker leest weer Pi (rollback in 1 min).
# Cutover 2e poging 2026-09-05 12:35 — na fix in pick_cache_batch_supabase:
#   - query omgedraaid (listings-first, dan cache-check) → omzeilt 1000-row cap
#   - card_keys quoted voor special chars
#   - chunk 200→50 voor URL-safety
export KENSA_READ_STORAGE=supabase

cd "$KENSA_DIR" || exit 1

exec 9>"$LOCK"
if ! /usr/bin/flock -n 9; then
  echo "[$(date -Iseconds)] worker-cache already running — skip"
  exit 0
fi

echo ""
echo "======== $(date -Iseconds) worker-cache start (limit=$LIMIT parallel=$PARALLEL) ========"
/usr/bin/timeout --kill-after=30s 240s "$VENV_PY" worker_cache.py --limit "$LIMIT" --parallel "$PARALLEL" 2>&1
echo "======== $(date -Iseconds) worker-cache done ========"
