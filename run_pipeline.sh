#!/bin/bash
# Kensa pipeline — full Buyee → detail → analyze cycle.
# Runs every 15 min via crontab. Uses flock so overlapping runs skip cleanly.

set -u

KENSA_DIR="/home/pi/.openclaw/workspace/agents/kensa"
VENV_PY="/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3"
LOCK="/tmp/kensa-pipeline.lock"

# Per run: max 20 new detail fetches + max 20 new analyses.
# Detail ~12s/item, analyze+eBay ~15-20s/item → run rond 10-12 min.
# Bij scraper-toename van 6+ items/run halen we de backlog altijd in.
DETAIL_LIMIT=30   # verhoogd 20->30 sequentieel — nog geen bot-signaal risico
ANALYZE_LIMIT=40  # verhoogd 20->40 om detail-throughput 120/uur bij te houden

# Adaptieve proxy-worker: als detail-fetch backlog boven deze drempel komt,
# schakelen we een tweede detail-worker in die via NL residential proxy scrapet
# (boilingproxies). Zo halen we pieken bij zonder de reguliere IP zwaarder te
# belasten. Auto-stop als backlog weer onder DRAIN_THRESHOLD komt.
PROXY_ACTIVATE_THRESHOLD=150   # activeer proxy-worker bij backlog >= 150
PROXY_DRAIN_THRESHOLD=50       # skip proxy-worker als backlog < 50
PROXY_LIMIT=30                 # aantal items per run via proxy als geactiveerd

cd "$KENSA_DIR" || exit 1

exec 9>"$LOCK"
if ! /usr/bin/flock -n 9; then
  echo "[$(date -Iseconds)] pipeline already running — skip"
  exit 0
fi

echo ""
echo "======== $(date -Iseconds) kensa pipeline start ========"

# CM-queue cleanup: verwijder pending items ouder dan 5u. Voorkomt backlog-stapeling
# als worker even weg was — recente kaarten hebben prioriteit, ouder wordt gedumped.
"$VENV_PY" -c "
import sqlite3
db = sqlite3.connect('$KENSA_DIR/kensa.db')
c = db.execute(\"DELETE FROM cardmarket_queue WHERE fetched_at IS NULL AND error IS NULL AND queued_at < datetime('now','-5 hours')\")
db.commit(); print(f'  [cm-cleanup] pending > 5u weg: {c.rowcount}')
"

# eBay/Buyee scrapers laten user_data_dirs achter in /tmp — die stapelen op
# tot ze de tmpfs volpompen. Cleanup elk run.
find /tmp -maxdepth 1 -name "kensa-ebay-*" -type d -mmin +5 -exec rm -rf {} + 2>/dev/null || true

echo "--- 1a. scrape_buyee Mercari (Psa 10) ---"
/usr/bin/timeout --kill-after=30s 180s "$VENV_PY" scrape_buyee.py 2>&1 | tail -5

echo "--- 1b. migrate Mercari → SQLite ---"
/usr/bin/timeout --kill-after=10s 60s "$VENV_PY" migrate_from_json.py 2>&1 | tail -5

echo "--- 2a. scrape_buyee PayPay Fleamarket (psa 10) ---"
PAYPAY_URL="https://buyee.jp/paypayfleamarket/search?keyword=psa%2010&brand_id=167473&order-sort=created_time&page=1&status=on_sale&currency=EUR"
/usr/bin/timeout --kill-after=30s 180s "$VENV_PY" scrape_buyee.py "$PAYPAY_URL" 2>&1 | tail -5

echo "--- 2b. migrate PayPay → SQLite ---"
/usr/bin/timeout --kill-after=10s 60s "$VENV_PY" migrate_from_json.py 2>&1 | tail -5

echo "--- 3. fetch_detail --all $DETAIL_LIMIT (max 12 min) ---"
/usr/bin/timeout --kill-after=30s 720s "$VENV_PY" fetch_detail.py --all "$DETAIL_LIMIT" 2>&1 | tail -20

# Queue-check: als backlog boven drempel → extra worker via NL residential proxy
BACKLOG=$("$VENV_PY" -c "
import sqlite3
c = sqlite3.connect('$KENSA_DIR/kensa.db')
print(c.execute('SELECT COUNT(*) FROM listings WHERE detail_scraped_at IS NULL').fetchone()[0])
" 2>/dev/null)
echo "--- 3b. detail-fetch backlog: $BACKLOG (activate proxy >= $PROXY_ACTIVATE_THRESHOLD, drain < $PROXY_DRAIN_THRESHOLD) ---"
if [ "$BACKLOG" -ge "$PROXY_ACTIVATE_THRESHOLD" ]; then
  echo "--- 3c. fetch_detail_proxy --all $PROXY_LIMIT (via boilingproxies NL residential, max 12 min) ---"
  /usr/bin/timeout --kill-after=30s 720s "$VENV_PY" fetch_detail_proxy.py --all "$PROXY_LIMIT" 2>&1 | tail -20
elif [ "$BACKLOG" -lt "$PROXY_DRAIN_THRESHOLD" ]; then
  echo "  [proxy] backlog laag, proxy-worker slaapt"
else
  echo "  [proxy] tussenzone — proxy-worker slaapt tot backlog $PROXY_ACTIVATE_THRESHOLD raakt"
fi

echo "--- 4. analyze --all --limit $ANALYZE_LIMIT (max 12 min, incl. eBay ROI via login-session) ---"
/usr/bin/timeout --kill-after=30s 720s "$VENV_PY" analyze.py --all --limit "$ANALYZE_LIMIT" 2>&1 | tail -20

# Nogmaals cleanup na de run — sommige processen laten user-data-dirs achter zelfs bij timeout
find /tmp -maxdepth 1 \( -name "kensa-ebay-*" -o -name "playwright_chromiumdev_profile-*" \) -type d -mmin +5 -exec rm -rf {} + 2>/dev/null || true

echo "======== $(date -Iseconds) kensa pipeline done ========"
