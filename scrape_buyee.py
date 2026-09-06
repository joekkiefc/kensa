#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa Stap 1 — Buyee (Mercari) zoekpagina scraper.

Fetch one Buyee /mercari/search page via Scrapling StealthyFetcher (bypasses
AWS WAF challenge), parses all listings, and writes structured JSON to
last_scrape.json + prints a table to stdout.

Usage:
  ./scrape_buyee.py                # uses the default POC URL
  ./scrape_buyee.py "<full url>"   # custom URL

Notes:
- The Buyee search HTML contains **Japanese** titles only. English titles
  are rendered client-side by the embedded Google Translate widget, so they
  are not available server-side. `title_en` is therefore left null for
  now — resolving it requires either a translation API or fetching the
  per-item detail page (out of scope for Stap 1).
- Sold-out listings are already filtered out by Mercari/Buyee, so no sold
  markers appear in search results. If they ever do, add a `.sold` check
  in `parse_listing`.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

from scrapling import StealthyFetcher

DEFAULT_URL = (
    "https://buyee.jp/mercari/search"
    "?limit=100&lang=en&page=1&searchType=filter"
    "&keyword=psa+10&category_id=1289&currencyCode=EUR"
    "&order-sort=desc-created_time"
    "&price_min=4200&price_max=850000"
)
# Category 1289 = brede Trading Cards categorie (in praktijk >90% Pokemon voor PSA-listings).
# price_min=4200 JPY (~€24) — geen bulk/raw lots of te goedkope items.
# price_max=850000 JPY (~€5000) — geen top-end slabs (te weinig marge, hoge verzendkosten).
# order-sort=desc-created_time = nieuwste eerst zodat we recente listings niet missen.

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_PATH = os.path.join(SCRIPT_DIR, "last_scrape.json")
DEBUG_HTML_PATH = "/tmp/buyee_debug.html"


def wait_for_content(page):
    """Wait for the AWS WAF challenge to resolve and real content to load."""
    try:
        page.wait_for_selector(
            "ul.item-lists, a[href*='/mercari/item/'], a[href*='/paypayfleamarket/item/']", timeout=60000
        )
    except Exception as e:
        print(f"  page_action wait_for_selector: {e}", file=sys.stderr)
    return page


def fetch_html(url):
    """Fetch the search page and return HTML string (or None on failure)."""
    print(f"Fetching: {url}", file=sys.stderr)
    page = StealthyFetcher().fetch(
        url,
        headless=True,
        network_idle=True,
        timeout=120000,
        wait=3000,
        page_action=wait_for_content,
    )
    print(f"Status: {page.status}", file=sys.stderr)
    html = page.html_content or ""
    if page.status != 200 or "awsWaf" in html or len(html) < 5000:
        with open(DEBUG_HTML_PATH, "w", encoding="utf-8") as f:
            f.write(html)
        print(
            f"Fetch mislukt (status={page.status}, len={len(html)}). "
            f"HTML dump: {DEBUG_HTML_PATH}",
            file=sys.stderr,
        )
        return None
    return html


PRICE_JPY_RE = re.compile(r"([\d,]+)\s*YEN", re.IGNORECASE)
PRICE_EUR_RE = re.compile(r"€\s*([\d,]+(?:\.\d+)?)")
ITEM_ID_RE = re.compile(r"/(?:mercari|paypayfleamarket)/item/([A-Za-z0-9]+)")


def first(el, sel):
    """Return first match of a CSS selector on a Scrapling element, or None."""
    if el is None:
        return None
    matches = el.css(sel)
    return matches[0] if matches else None


def parse_number(text, pattern):
    m = pattern.search(text or "")
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    try:
        return float(raw) if "." in raw else int(raw)
    except ValueError:
        return None


def normalize_image_url(src):
    if not src:
        return None
    src = src.strip()
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return "https://buyee.jp" + src
    return src


def normalize_detail_url(href):
    if not href:
        return None
    href = href.split("?")[0]
    if href.startswith("/"):
        return "https://buyee.jp" + href
    return href


# `parse_listing` verhuisd naar _split/ (v2). Verwijderd 2026-09-01 (regel 2 cleanup).
# Backup: _legacy_pre_refactor/... — alias-override staat lager in de file.



def parse_html(html):
    """Return list of listing dicts."""
    from scrapling.parser import Selector

    doc = Selector(content=html)
    ul = first(doc, "ul.item-lists")
    if not ul:
        print("Geen <ul class='item-lists'> gevonden in HTML", file=sys.stderr)
        return []
    listings = []
    for li in ul.css("li.list"):
        row = parse_listing(li)
        if row and row["item_id"]:
            listings.append(row)
    return listings


def print_table(rows):
    if not rows:
        print("Geen listings.")
        return
    print(
        f"{'#':>3}  {'item_id':<14}  {'¥':>10}  {'€':>9}  "
        f"{'auth':>5}  title"
    )
    print("-" * 100)
    for i, r in enumerate(rows, 1):
        title = (r["title_jp"] or "")[:60]
        jpy = f"{r['price_jpy']:,}" if r["price_jpy"] else "-"
        eur = f"{r['price_eur']:,.2f}" if r["price_eur"] else "-"
        auth = "✓" if r["authenticated"] else ""
        print(f"{i:>3}  {r['item_id']:<14}  {jpy:>10}  {eur:>9}  {auth:>5}  {title}")


def main():
    url = sys.argv[1].strip() if len(sys.argv) > 1 else DEFAULT_URL

    html = fetch_html(url)
    if html is None:
        sys.exit(1)

    listings = parse_html(html)
    print(f"\nGevonden: {len(listings)} listings", file=sys.stderr)

    result = {
        "source_url": url,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "count": len(listings),
        "listings": listings,
    }

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"JSON opgeslagen: {OUTPUT_PATH}", file=sys.stderr)

    print_table(listings)



# Refactor-switch(es) voor if __name__ zodat CLI-run de alias ook heeft.
# Refactor switch (2026-09-01, regel 2 #8): parse_listing -> v2.
# Backup: _legacy_pre_refactor/scrape_buyee.py.20260901-regel2-func8
from parse_listing_split import parse_listing_v2 as _parse_listing_new  # noqa: E402
parse_listing = _parse_listing_new

if __name__ == "__main__":
    main()


