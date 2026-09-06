#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa — eBay last-sold scraper via NL residential proxy (boilingproxies).

Verschil met ebay_lastsold.py: geen ingelogde session, elke fetch via random
proxy-sessie. Voordelen: geen risico op eBay-login flaggen, verse IP per fetch.
Nadelen: kans op captcha hoger, iets trager (~10-30s extra per fetch).

Deze versie wordt gebruikt door de 2e analyze-worker (cron_analyze_proxy.sh)
zodat we parallel eBay-lookups kunnen doen zonder de ingelogde session te
belasten. Complementair aan de bestaande ebay_lastsold.py.
"""

import argparse
import json
import random
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from scrapling import StealthyFetcher

# Hergebruik parser + config uit ebay_lastsold
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ebay_lastsold import (
    build_url,
    parse_results,
    _is_captcha,
    USER_AGENTS,
    _load_proxies,
)

_PROXIES = _load_proxies()


def _fetch_via_proxy(query: str, verbose: bool = False, max_attempts: int = 2) -> Optional[str]:
    """Fetch eBay search resultaat via random NL proxy. Geen login/cookies.
    Bij captcha of block: retry met andere proxy + andere UA. Geen persistent user_data_dir."""
    if not _PROXIES:
        if verbose:
            print("[ebay-proxy] geen proxies beschikbaar", file=sys.stderr)
        return None

    url = build_url(query)
    f = StealthyFetcher()

    for attempt in range(1, max_attempts + 1):
        proxy = random.choice(_PROXIES)
        ua = random.choice(USER_AGENTS)
        proxy_conf = {"server": proxy["server"], "username": proxy["username"], "password": proxy["password"]}
        if verbose:
            print(f"[ebay-proxy] attempt {attempt} via ...{proxy['id']}  UA={ua[:50]}...", file=sys.stderr)
        try:
            p = f.fetch(
                url,
                headless=True,
                useragent=ua,
                disable_resources=True,
                network_idle=False,
                timeout=120000,
                wait=3000,
                wait_selector=".s-card, .s-item, .srp-controls, body",
                google_search=False,
                proxy=proxy_conf,
                block_webrtc=True,
            )
        except Exception as e:
            if verbose:
                print(f"[ebay-proxy] fetch exceptie: {e}", file=sys.stderr)
            continue
        html = p.html_content or ""
        final_url = getattr(p, "url", "") or ""
        if _is_captcha(html, p.status, final_url):
            if verbose:
                print(f"[ebay-proxy] captcha detected — try next proxy", file=sys.stderr)
            continue
        if p.status != 200 or "error-header__headline" in html or len(html) < 3000:
            if verbose:
                print(f"[ebay-proxy] status={p.status} len={len(html)} — retry", file=sys.stderr)
            continue
        if verbose:
            print(f"[ebay-proxy] ✓ success (attempt {attempt}, len={len(html)})", file=sys.stderr)
        return html

    if verbose:
        print(f"[ebay-proxy] {max_attempts} pogingen faalden voor {query!r}", file=sys.stderr)
    return None


def search(query: str, limit: int = 5, verbose: bool = False) -> dict:
    """Compatible signature met ebay_lastsold.search() — analyze.py kan direct switch maken."""
    html = _fetch_via_proxy(query, verbose=verbose)
    if html is None:
        return {"query": query, "error": "fetch_failed_via_proxy", "sales": []}
    sales = parse_results(html, limit=limit)
    return {"query": query, "fetched_at": datetime.now(timezone.utc).isoformat(), "sales": sales, "via": "proxy"}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", default="Pikachu 025 PSA 10")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    r = search(args.query, limit=args.limit, verbose=args.verbose)
    print(json.dumps(r, ensure_ascii=False, indent=2))
