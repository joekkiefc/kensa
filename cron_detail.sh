#!/bin/bash
# Kensa detail-cron — draait elke 10 min. Eigen flock.
#
# Router:
#   1. mercapi voor Mercari-items (m*-ids) — ~1.5s per item, geen WAF
#   2. mercapi-recheck van bestaande deal-items — vangt sold-status voor dashboard
#   3. Buyee-scrape (Chromium) voor PayPay-items (z*-ids) + eventuele mercapi-fails
#   4. Buyee-proxy fallback als backlog boven drempel (voor grote inhaal-runs)

set -u

KENSA_DIR="/home/pi/.openclaw/workspace/agents/kensa"
VENV_PY="/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3"
LOCK="/tmp/kensa-detail.lock"

MERCAPI_LIMIT=250
MERCAPI_RECHECK_LIMIT=100
MERCAPI_RECHECK_MIN_AGE_HOURS=4
BUYEE_LIMIT=35
PROXY_ACTIVATE_THRESHOLD=150
PROXY_DRAIN_THRESHOLD=50
PROXY_LIMIT=30

# Storage-migratie: writes via storage.py (upsert_listing/upsert_photos/download-metadata)
# dispatchen op Supabase. `save_raw_page` blijft LOKAAL op Pi (Tommy 2026-09-03:
# raw_pages retentie 7 dagen, geen langere opslag nodig).
export KENSA_WRITE_STORAGE=dual

cd "$KENSA_DIR" || exit 1

exec 9>"$LOCK"
if ! /usr/bin/flock -n 9; then
  echo "[$(date -Iseconds)] detail already running — skip"
  exit 0
fi

echo ""
echo "======== $(date -Iseconds) kensa detail start ========"

# tmp cleanup
find /tmp -maxdepth 1 -name "kensa-ebay-*" -type d -mmin +5 -exec rm -rf {} + 2>/dev/null || true

# CUTOVER 2026-09-05 17:55 — mercapi leest queue uit Supabase.
# Snelheids-monster: eerst mercapi voor alle m*-ids (verwacht 1-2s per item, dus 200 items in ~5 min)
echo "--- fetch_detail_mercapi --all $MERCAPI_LIMIT (max 8 min, READ=supabase) ---"
/usr/bin/timeout --kill-after=30s 480s env KENSA_READ_STORAGE=supabase KENSA_WRITE_STORAGE=dual "$VENV_PY" fetch_detail_mercapi.py --all "$MERCAPI_LIMIT" 2>&1 | tail -20

# Recheck: bestaande deal-items opnieuw langs mercapi om verkochte kaarten te markeren (sold=1)
echo "--- fetch_detail_mercapi --recheck $MERCAPI_RECHECK_LIMIT ${MERCAPI_RECHECK_MIN_AGE_HOURS}u (max 3 min, READ=supabase) ---"
/usr/bin/timeout --kill-after=30s 180s env KENSA_READ_STORAGE=supabase KENSA_WRITE_STORAGE=dual "$VENV_PY" fetch_detail_mercapi.py --recheck "$MERCAPI_RECHECK_LIMIT" "$MERCAPI_RECHECK_MIN_AGE_HOURS" 2>&1 | tail -10

# Rest via Buyee-scrape: PayPay (z*-ids) + eventuele mercapi-fails (m*-ids die faalden)
# CUTOVER 2026-09-05 17:52 — fetch_detail leest queue uit Supabase (mercapi/proxy nog Pi).
echo "--- fetch_detail --all $BUYEE_LIMIT (max 10 min, READ=supabase) ---"
/usr/bin/timeout --kill-after=30s 600s env KENSA_READ_STORAGE=supabase KENSA_WRITE_STORAGE=dual "$VENV_PY" fetch_detail.py --all "$BUYEE_LIMIT" 2>&1 | tail -20

# Backlog-check: proxy alleen inzetten bij grote inhaal (bijv. na een captcha-storm)
# CUTOVER 2026-09-05 17:55 — leest count uit Supabase (Pi-fallback bij HTTP-fail).
BACKLOG=$("$VENV_PY" -c "
try:
    from storage_supabase_legacy import _load_secrets, _headers, _CACHED
    import requests
    _load_secrets()
    r = requests.get(f\"{_CACHED['url']}/rest/v1/listings\",
        headers={**_headers(), 'Prefer': 'count=exact'},
        params={'detail_scraped_at': 'is.null', 'select': 'item_id', 'limit': '0'},
        timeout=6)
    n = int((r.headers.get('content-range','*/0').split('/')[-1]) or 0)
    print(n)
except Exception:
    import sqlite3
    c = sqlite3.connect('$KENSA_DIR/kensa.db')
    print(c.execute('SELECT COUNT(*) FROM listings WHERE detail_scraped_at IS NULL').fetchone()[0])
" 2>/dev/null)
echo "--- detail-fetch backlog: $BACKLOG (activate proxy >= $PROXY_ACTIVATE_THRESHOLD, drain < $PROXY_DRAIN_THRESHOLD) ---"
if [ "$BACKLOG" -ge "$PROXY_ACTIVATE_THRESHOLD" ]; then
  echo "--- fetch_detail_proxy --all $PROXY_LIMIT (via boilingproxies NL residential, max 8 min, READ=supabase) ---"
  /usr/bin/timeout --kill-after=30s 480s env KENSA_READ_STORAGE=supabase KENSA_WRITE_STORAGE=dual "$VENV_PY" fetch_detail_proxy.py --all "$PROXY_LIMIT" 2>&1 | tail -20
elif [ "$BACKLOG" -lt "$PROXY_DRAIN_THRESHOLD" ]; then
  echo "  [proxy] backlog laag, proxy-worker slaapt"
else
  echo "  [proxy] tussenzone — proxy-worker slaapt tot backlog $PROXY_ACTIVATE_THRESHOLD raakt"
fi

# tmp cleanup na de run
find /tmp -maxdepth 1 \( -name "kensa-ebay-*" -o -name "playwright_chromiumdev_profile-*" \) -type d -mmin +5 -exec rm -rf {} + 2>/dev/null || true

echo "======== $(date -Iseconds) kensa detail done ========"
