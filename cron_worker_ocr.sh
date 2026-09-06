#!/bin/bash
# Kensa worker 1/3: OCR+LLM fase. Draait continu, pakt slab_status=pending items.
# Elke 5 min. Eigen flock zodat het parallel loopt met cache/ebay workers.

set -u
KENSA_DIR="/home/pi/.openclaw/workspace/agents/kensa"
VENV_PY="/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3"
LOCK="/tmp/kensa-worker-ocr.lock"
LIMIT="${WORKER_OCR_LIMIT:-40}"
PARALLEL="${WORKER_OCR_PARALLEL:-6}"

# Opt-in hybride multimodal-first flow (Gemini multimodal + Vision-fallback).
# Verwacht ~80% Vision-kostenbesparing. Uitzetten = deze regel weghalen.
export KENSA_USE_MULTIMODAL=1

# Supabase-first: OCR-worker leest listings + photos direct uit Supabase i.p.v.
# lokale SQLite. Writes gaan nog steeds dual (Pi + Supabase) tot alle workers
# over zijn. Uitzetten = deze regel weghalen → valt terug op SQLite-reads.
export KENSA_READ_STORAGE=supabase
# Writes ook direct naar Supabase (analyze._save_trap / _mark_slab_status via dispatcher).
export KENSA_WRITE_STORAGE=dual

cd "$KENSA_DIR" || exit 1

exec 9>"$LOCK"
if ! /usr/bin/flock -n 9; then
  echo "[$(date -Iseconds)] worker-ocr already running — skip"
  exit 0
fi

echo ""
echo "======== $(date -Iseconds) worker-ocr start (limit=$LIMIT parallel=$PARALLEL) ========"
/usr/bin/timeout --kill-after=30s 540s "$VENV_PY" worker_ocr.py --limit "$LIMIT" --parallel "$PARALLEL" 2>&1
echo "======== $(date -Iseconds) worker-ocr done ========"
