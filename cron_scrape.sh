#!/bin/bash
# Kensa scrape-cron — alleen Buyee zoekpagina's + migrate naar SQLite.
# Draait elke 15 min. Snel (~2 min per run). Eigen flock zodat het niet
# blokkeert op detail-fetch of analyze die parallel kunnen lopen.

set -u

KENSA_DIR="/home/pi/.openclaw/workspace/agents/kensa"
VENV_PY="/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3"
LOCK="/tmp/kensa-scrape.lock"

# Storage-migratie: scrape-writes (upsert_listing / mark_seen_in_search /
# record_cert_sighting) direct naar Supabase. save_raw_page blijft LOKAAL op Pi.
export KENSA_WRITE_STORAGE=dual

cd "$KENSA_DIR" || exit 1

exec 9>"$LOCK"
if ! /usr/bin/flock -n 9; then
  echo "[$(date -Iseconds)] scrape already running — skip"
  exit 0
fi

echo ""
echo "======== $(date -Iseconds) kensa scrape start ========"

# CM-queue cleanup: verwijder pending items ouder dan 5u
"$VENV_PY" -c "
import sqlite3
db = sqlite3.connect('$KENSA_DIR/kensa.db')
c = db.execute(\"DELETE FROM cardmarket_queue WHERE fetched_at IS NULL AND error IS NULL AND queued_at < datetime('now','-5 hours')\")
db.commit(); print(f'  [cm-cleanup] pending > 5u weg: {c.rowcount}')
"

# tmp cleanup
find /tmp -maxdepth 1 -name "kensa-ebay-*" -type d -mmin +5 -exec rm -rf {} + 2>/dev/null || true

echo "--- 1a. scrape_buyee Mercari (psa 10) ---"
/usr/bin/timeout --kill-after=30s 180s "$VENV_PY" scrape_buyee.py 2>&1 | tail -5
echo "--- 1b. migrate Mercari → SQLite ---"
/usr/bin/timeout --kill-after=10s 60s "$VENV_PY" migrate_from_json.py 2>&1 | tail -5

echo "--- 2a. scrape_buyee PayPay Fleamarket (psa 10) ---"
# Correcte PP URL: /search met category_id=2420 + brand_id=167473, price in EUR (€30-€5000).
# Deze vorm honoreert price_min/price_max filters wel.
PAYPAY_URL="https://buyee.jp/paypayfleamarket/search?keyword=psa%2010&category_id=2420&brand_id=167473&order-sort=created_time&page=1&status=on_sale&price_min=30.00&price_max=5000.00&currency=EUR"
/usr/bin/timeout --kill-after=30s 180s "$VENV_PY" scrape_buyee.py "$PAYPAY_URL" 2>&1 | tail -5

echo "--- 2b. migrate PayPay → SQLite ---"
/usr/bin/timeout --kill-after=10s 60s "$VENV_PY" migrate_from_json.py 2>&1 | tail -5

echo "======== $(date -Iseconds) kensa scrape done ========"
