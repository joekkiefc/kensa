#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa Stap 2b — Buyee detail-fetch via residential proxy (boilingproxies).

Wordt aangeroepen door run_pipeline.sh als de detail-fetch backlog boven de
drempel (150 items) uitkomt. Draait EXTRA items af via een NL residential
proxy zodat we niet vanaf hetzelfde IP als de reguliere fetch_detail scrapen
en Buyee ons niet als bot flagt.

Data-usage wordt bijgehouden in data/proxy_usage.json — waarschuwt als het
budget (default 25 GB) voor 80% verbruikt is.

Usage:
  ./fetch_detail_proxy.py --all <n>   # scrape <n> items uit backlog via proxy
"""

import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Optional

# Import hergebruik uit gewone fetch_detail — één source of truth voor parse-logica
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_detail import (
    _detail_url_for,
    _source_of,
    _wait_for_content,
    parse_detail,
)
from scrapling import StealthyFetcher
import storage
import translate as translate_mod

SCRIPT_DIR = Path(__file__).resolve().parent
PROXIES_PATH = SCRIPT_DIR / "proxies.txt"
USAGE_PATH = SCRIPT_DIR / "data" / "proxy_usage.json"
BUDGET_BYTES = 25 * 1024 * 1024 * 1024   # 25 GB default budget
WARN_AT = 0.80                            # waarschuw bij 80% gebruik


def _load_proxies() -> list[dict]:
    """Lees proxies.txt formaat: host:port:user:password (: in password blijft intact)."""
    proxies = []
    if not PROXIES_PATH.exists():
        return proxies
    for line in PROXIES_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":", 3)  # max 3 splits — password kan ':' bevatten
        if len(parts) != 4:
            continue
        host, port, user, password = parts
        proxies.append({
            "server": f"http://{host}:{port}",
            "username": user,
            "password": password,
        })
    return proxies


def _load_usage() -> dict:
    if USAGE_PATH.exists():
        try:
            return json.loads(USAGE_PATH.read_text())
        except Exception:
            pass
    return {"total_bytes": 0, "sessions": []}


def _save_usage(usage: dict) -> None:
    USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    USAGE_PATH.write_text(json.dumps(usage, indent=2))


def _track_bytes(n_bytes: int, item_id: str) -> None:
    usage = _load_usage()
    usage["total_bytes"] += n_bytes
    usage.setdefault("sessions", []).append({
        "ts": storage.now_iso(),
        "item_id": item_id,
        "bytes": n_bytes,
    })
    # Trim sessions log tot laatste 500 om file niet oneindig te laten groeien
    usage["sessions"] = usage["sessions"][-500:]
    _save_usage(usage)


def _check_budget() -> bool:
    """Return False als budget op is (>100% gebruikt), True anders. Log waarschuwing bij 80%."""
    usage = _load_usage()
    used = usage["total_bytes"]
    pct = used / BUDGET_BYTES
    if pct >= 1.0:
        print(f"  [proxy] BUDGET OP: {used/1024/1024/1024:.2f} GB van {BUDGET_BYTES/1024/1024/1024:.0f} GB — stop", file=sys.stderr)
        return False
    if pct >= WARN_AT:
        print(f"  [proxy] WAARSCHUWING: {pct*100:.0f}% budget gebruikt ({used/1024/1024/1024:.2f} GB)", file=sys.stderr)
    return True


def fetch_detail_via_proxy(item_id: str, proxy: dict) -> tuple[str, int]:
    """Zelfde als fetch_detail.fetch_detail_html maar via NL residential proxy."""
    url = _detail_url_for(item_id)
    t0 = time.time()
    # Via proxy loopt Buyee's `network_idle=True` fataal traag/vast (proxy latency stapelt
    # op elk sub-request). Zonder network_idle en met wait_for_selector via page_action
    # krijgen we snel de essentie (JSON-LD + h1) — ~6s per fetch ipv timeout.
    page = StealthyFetcher().fetch(
        url,
        headless=True,
        network_idle=False,
        timeout=120000,
        wait=3000,
        page_action=_wait_for_content,
        proxy=proxy,
        block_webrtc=True,  # voorkomt IP-leak (echte IP zichtbaar via WebRTC anders)
    )
    html = page.html_content or ""
    if "awsWaf" in html or "AWS WAF" in html:
        raise RuntimeError(f"WAF challenge not solved for {item_id} (via proxy)")
    if len(html) < 5000:
        raise RuntimeError(f"HTML too small ({len(html)} bytes) for {item_id} (via proxy)")
    print(f"  [proxy] fetched {item_id}: status={page.status} len={len(html)} time={time.time()-t0:.1f}s", file=sys.stderr)
    return html, page.status


def scrape_detail_and_store_proxy(item_id: str, proxy: dict) -> dict:
    """Detail via proxy. Description-fetch is bewust weggelaten (Tommy 2026-08-02)."""
    detail_html, _status = fetch_detail_via_proxy(item_id, proxy)
    parsed = parse_detail(detail_html, item_id)

    if parsed.get("title_jp"):
        parsed["title_en"] = translate_mod.translate(parsed["title_jp"])

    photos = parsed.pop("photo_urls", [])
    listing_row = {k: v for k, v in parsed.items() if k != "photo_urls"}
    storage.upsert_listing(listing_row)
    inserted_photos = storage.upsert_photos(item_id, photos)
    detail_page_id = storage.save_raw_page(_detail_url_for(item_id), "detail", detail_html)

    _track_bytes(len(detail_html), item_id)

    parsed["_meta"] = {
        "photos_inserted": inserted_photos,
        "photos_total": len(photos),
        "raw_page_id": detail_page_id,
        "detail_bytes": len(detail_html),
        "via_proxy": True,
    }
    return parsed


def _pending_items(limit: int) -> list[str]:
    """Zelfde queue als fetch_detail — nieuwste eerst, alleen items zonder detail."""
    if os.environ.get("KENSA_READ_STORAGE", "").lower() == "supabase":
        from storage_supabase import pick_detail_proxy_batch_supabase
        return pick_detail_proxy_batch_supabase(limit)
    conn = storage._connect()
    try:
        return [r["item_id"] for r in conn.execute(
            "SELECT item_id FROM listings WHERE detail_scraped_at IS NULL ORDER BY first_seen_at DESC LIMIT ?",
            (limit,)
        ).fetchall()]
    finally:
        conn.close()


def main(argv):
    storage.init_db()

    if len(argv) < 2 or argv[1] != "--all":
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    limit = int(argv[2]) if len(argv) > 2 else 20

    if not _check_budget():
        sys.exit(0)  # geen error, gewoon geen werk doen

    proxies = _load_proxies()
    if not proxies:
        print("  [proxy] geen proxies in proxies.txt — sla proxy-run over", file=sys.stderr)
        sys.exit(0)

    ids = _pending_items(limit)
    if not ids:
        print("  [proxy] queue leeg — niks te doen", file=sys.stderr)
        sys.exit(0)

    print(f"  [proxy] {len(ids)} items via NL residential proxy (budget: {_load_usage()['total_bytes']/1024/1024/1024:.2f}/{BUDGET_BYTES/1024/1024/1024:.0f} GB)", file=sys.stderr)

    for i, iid in enumerate(ids, 1):
        # Kies willekeurig een proxy-sessie (elke sessie = 1 uur IP-stability)
        proxy = random.choice(proxies)
        print(f"\n[{i}/{len(ids)}] {iid}  (proxy sessie: ...{proxy['password'][-25:]})", file=sys.stderr)
        try:
            r = scrape_detail_and_store_proxy(iid, proxy)
            print(f"  [proxy] OK: {r.get('title_jp','?')[:60]} — ¥{r.get('price_jpy','?')} — photos {r['_meta']['photos_total']}", file=sys.stderr)
        except Exception as e:
            print(f"  [proxy] FAIL: {e}", file=sys.stderr)

        # Check budget na elk item
        if not _check_budget():
            print(f"  [proxy] budget op na item {i}/{len(ids)} — stop", file=sys.stderr)
            break

        # Kleine pauze tussen items (respect voor Buyee, ook via proxy)
        time.sleep(2)

    usage = _load_usage()
    print(f"\n  [proxy] klaar. Totaal budget-gebruik: {usage['total_bytes']/1024/1024/1024:.2f} GB / {BUDGET_BYTES/1024/1024/1024:.0f} GB", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv)
