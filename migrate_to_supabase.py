#!/usr/bin/env python3
"""Kensa stap 2 — bulk-migratie SQLite → Supabase met cleanup-regels.

Regels:
  * listings mét card_key: per card_key max 20 nieuwste (last_seen_at DESC).
  * listings zonder card_key: alleen last_seen_at >= now - 28 dagen.
  * gerelateerde tabellen (analysis, cardmarket_queue, photos, cert_sightings)
    alleen voor behouden listings.
  * price_cache + alerts: alles mee (kleine + los).
  * user_verdicts: alles mee.
  * raw_pages + llm_slab_cache blijven bewust lokaal op de Pi.

Requires: requests (stdlib-only fallback via urllib zou ook kunnen).
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

SECRETS = json.loads(Path("/home/pi/.openclaw/secrets.json").read_text())
SB_URL = SECRETS["supabase"]["url"]
SB_KEY = SECRETS["supabase"]["service_role_key"]
DB = "/home/pi/.openclaw/workspace/agents/kensa/kensa.db"
BATCH = 300
CUTOFF_UNMATCHED = (datetime.now(timezone.utc) - timedelta(days=28)).isoformat()

HEADERS = {
    "apikey": SB_KEY,
    "Authorization": f"Bearer {SB_KEY}",
    "Content-Type": "application/json",
    "Content-Profile": "kensa",
    "Accept-Profile": "kensa",
    "Prefer": "return=minimal,resolution=ignore-duplicates",
}


def _post_with_retry(url: str, chunk: list[dict], table: str) -> None:
    """POST met exponential-backoff bij netwerk-hikjes."""
    delay = 1
    for attempt in range(6):
        try:
            r = requests.post(url, headers=HEADERS, json=chunk, timeout=120)
        except (requests.ConnectionError, requests.Timeout) as e:
            print(f"  ~ {table}: netwerk-hikje ({type(e).__name__}) — retry in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 32)
            continue
        if r.status_code < 300:
            return
        # 409 mag NIET gebeuren met resolution=ignore-duplicates, maar voor
        # de zekerheid: als 't tóch komt, tolereren (batch al binnen).
        if r.status_code == 409:
            return
        # 5xx = probeer nog eens
        if 500 <= r.status_code < 600:
            print(f"  ~ {table}: {r.status_code} — retry in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 32)
            continue
        raise RuntimeError(f"{table} chunk failed {r.status_code}: {r.text[:500]}")
    raise RuntimeError(f"{table} chunk failed na 6 retries")


def rest(table: str, rows: list[dict]) -> None:
    if not rows:
        return
    url = f"{SB_URL}/rest/v1/{table}"
    for i in range(0, len(rows), BATCH):
        chunk = rows[i : i + BATCH]
        _post_with_retry(url, chunk, table)
        if (i // BATCH) % 10 == 0 or i + len(chunk) == len(rows):
            print(f"  · {table}: {i + len(chunk):,}/{len(rows):,}")


def parse_json_maybe(s):
    if s is None or s == "":
        return None
    if isinstance(s, (dict, list)):
        return s
    try:
        return json.loads(s)
    except Exception:
        return None


def to_bool(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "t", "yes", "y")
    return None


def norm_ts(v):
    """SQLite TEXT-datetime → ISO. Retour None als niks."""
    if v is None or v == "":
        return None
    return v  # SQLite houdt al ISO-strings aan; PG accepteert het.


def main():
    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")

    # Bepaal welke item_ids we behouden.
    print("== Cleanup-plan opstellen ==")
    keep_matched = con.execute(
        """
        SELECT item_id FROM (
          SELECT item_id, card_key,
            ROW_NUMBER() OVER (
              PARTITION BY card_key
              ORDER BY COALESCE(last_seen_at, first_seen_at, detail_scraped_at) DESC
            ) AS rn
          FROM listings
          WHERE card_key IS NOT NULL
        )
        WHERE rn <= 20
        """
    ).fetchall()
    keep_matched_ids = {r[0] for r in keep_matched}

    keep_unmatched = con.execute(
        """
        SELECT item_id FROM listings
        WHERE card_key IS NULL
          AND COALESCE(last_seen_at, first_seen_at, detail_scraped_at) >= ?
        """,
        (CUTOFF_UNMATCHED,),
    ).fetchall()
    keep_unmatched_ids = {r[0] for r in keep_unmatched}

    keep = keep_matched_ids | keep_unmatched_ids
    total = con.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    print(f"  Totaal listings: {total:,}")
    print(f"  Behoud mét kaart: {len(keep_matched_ids):,}")
    print(f"  Behoud zonder kaart (≤4wk): {len(keep_unmatched_ids):,}")
    print(f"  Behoud totaal: {len(keep):,} ({len(keep)/total*100:.0f}%)")
    print(f"  Verwijderd: {total - len(keep):,}")
    print()

    # ------ listings ------
    print("== listings ==")
    cols = [
        "item_id", "title_jp", "title_en", "description_jp",
        "price_jpy", "price_eur", "shipping_jpy",
        "seller_id", "seller_name", "seller_rating",
        "category_path", "condition", "authenticated", "sold",
        "detail_url", "status", "slab_status", "card_key",
        "extra_json", "first_seen_at", "last_seen_at", "detail_scraped_at",
        "locked_by", "locked_at",
    ]
    rows = []
    q = con.execute(f"SELECT {', '.join(cols)} FROM listings")
    for r in q:
        if r["item_id"] not in keep:
            continue
        d = dict(r)
        d["authenticated"] = to_bool(d["authenticated"])
        d["sold"] = to_bool(d["sold"])
        d["extra_json"] = parse_json_maybe(d["extra_json"])
        for k in ("first_seen_at", "last_seen_at", "detail_scraped_at", "locked_at"):
            d[k] = norm_ts(d[k])
        rows.append(d)
    print(f"  {len(rows):,} rijen te sturen")
    rest("listings", rows)

    # ------ analysis (only voor behouden items) ------
    print("== analysis ==")
    rows = []
    q = con.execute("SELECT item_id, trap, result_json, confidence, card_id, created_at FROM analysis")
    for r in q:
        if r["item_id"] not in keep:
            continue
        d = dict(r)
        d["result_json"] = parse_json_maybe(d["result_json"])
        rows.append(d)
    print(f"  {len(rows):,} rijen te sturen")
    rest("analysis", rows)

    # ------ cardmarket_queue ------
    print("== cardmarket_queue ==")
    rows = []
    q = con.execute("SELECT item_id, url, queued_at, fetched_at, listings_json, error, grade, card_key FROM cardmarket_queue")
    for r in q:
        if r["item_id"] not in keep:
            continue
        d = dict(r)
        d["listings_json"] = parse_json_maybe(d["listings_json"])
        rows.append(d)
    print(f"  {len(rows):,} rijen te sturen")
    rest("cardmarket_queue", rows)

    # ------ photos ------
    print("== photos ==")
    rows = []
    q = con.execute("SELECT item_id, photo_index, url_original, file_hash, file_path, downloaded_at, size_bytes, width, height FROM photos")
    for r in q:
        if r["item_id"] not in keep:
            continue
        rows.append(dict(r))
    print(f"  {len(rows):,} rijen te sturen")
    rest("photos", rows)

    # ------ cert_sightings ------
    print("== cert_sightings ==")
    rows = []
    q = con.execute("SELECT cert, item_id, seller_hint, seen_at FROM cert_sightings")
    for r in q:
        if r["item_id"] not in keep:
            continue
        rows.append(dict(r))
    print(f"  {len(rows):,} rijen te sturen")
    rest("cert_sightings", rows)

    # ------ price_cache (allemaal) ------
    print("== price_cache ==")
    rows = []
    q = con.execute("SELECT card_key, ebay_query, ebay_result_json, ebay_fetched_at, cm_url, cm_listings_json, cm_fetched_at FROM price_cache")
    for r in q:
        d = dict(r)
        d["ebay_result_json"] = parse_json_maybe(d["ebay_result_json"])
        d["cm_listings_json"] = parse_json_maybe(d["cm_listings_json"])
        rows.append(d)
    print(f"  {len(rows):,} rijen te sturen")
    rest("price_cache", rows)

    # ------ user_verdicts ------
    print("== user_verdicts ==")
    rows = [dict(r) for r in con.execute("SELECT item_id, seen_at, verdict, notes, updated_at FROM user_verdicts")]
    rows = [r for r in rows if r["item_id"] in keep]
    print(f"  {len(rows):,} rijen te sturen")
    rest("user_verdicts", rows)

    # ------ alerts (allemaal, klein) ------
    print("== alerts ==")
    rows = [dict(r) for r in con.execute("SELECT query, max_yen, min_yen, active, all_grades, created_at FROM alerts")]
    for r in rows:
        r["active"] = to_bool(r["active"])
        r["all_grades"] = to_bool(r["all_grades"])
    print(f"  {len(rows):,} rijen te sturen")
    rest("alerts", rows)

    print("\n== KLAAR ==")


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"Duur: {time.time() - t0:.0f}s")
