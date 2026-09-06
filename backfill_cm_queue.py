#!/usr/bin/env python3
"""Eenmalige backfill: cardmarket_queue rijen die in SQLite staan
maar (nog) niet in Supabase. Gebruikt resolution=ignore-duplicates
zodat bestaande rijen ongemoeid blijven. Item_ids waarvan de
listing niet in Supabase zit (FK) worden overgeslagen met log-regel.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import requests

SECRETS = json.loads(Path("/home/pi/.openclaw/secrets.json").read_text())
SB_URL = SECRETS["supabase"]["url"]
SB_KEY = SECRETS["supabase"]["service_role_key"]
DB = "/home/pi/.openclaw/workspace/agents/kensa/kensa.db"
BATCH = 300

HEADERS = {
    "apikey": SB_KEY,
    "Authorization": f"Bearer {SB_KEY}",
    "Content-Type": "application/json",
    "Content-Profile": "kensa",
    "Accept-Profile": "kensa",
    "Prefer": "return=minimal,resolution=ignore-duplicates",
}


def _parse_json(v):
    if v is None or v == "":
        return None
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return None


def existing_supabase_keys() -> set[tuple[str, str]]:
    """Fetch alle (item_id, url) pairs uit Supabase in pagina's van 1000."""
    print("Snapshot Supabase cardmarket_queue keys...", flush=True)
    keys: set[tuple[str, str]] = set()
    offset = 0
    while True:
        r = requests.get(
            f"{SB_URL}/rest/v1/cardmarket_queue",
            headers={**HEADERS, "Range-Unit": "items", "Range": f"{offset}-{offset+999}"},
            params={"select": "item_id,url"},
            timeout=60,
        )
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        for row in rows:
            keys.add((row["item_id"], row["url"]))
        offset += len(rows)
        if offset % 5000 == 0 or len(rows) < 1000:
            print(f"  · {offset:,} keys gelezen", flush=True)
        if len(rows) < 1000:
            break
    print(f"Supabase heeft {len(keys):,} (item_id, url) pairs", flush=True)
    return keys


def existing_supabase_listings() -> set[str]:
    """Item_ids die WEL in Supabase listings zitten (FK-target)."""
    print("Snapshot Supabase listings item_ids...", flush=True)
    ids: set[str] = set()
    offset = 0
    while True:
        r = requests.get(
            f"{SB_URL}/rest/v1/listings",
            headers={**HEADERS, "Range-Unit": "items", "Range": f"{offset}-{offset+999}"},
            params={"select": "item_id"},
            timeout=60,
        )
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        for row in rows:
            ids.add(row["item_id"])
        offset += len(rows)
        if offset % 10000 == 0 or len(rows) < 1000:
            print(f"  · {offset:,} listings gelezen", flush=True)
        if len(rows) < 1000:
            break
    print(f"Supabase heeft {len(ids):,} listing item_ids", flush=True)
    return ids


def _post_with_retry(rows: list[dict]) -> tuple[int, int]:
    """Return (ok, failed)."""
    delay = 1
    for attempt in range(6):
        try:
            r = requests.post(
                f"{SB_URL}/rest/v1/cardmarket_queue",
                headers=HEADERS,
                json=rows,
                timeout=120,
            )
        except (requests.ConnectionError, requests.Timeout) as e:
            print(f"  ~ netwerk: {type(e).__name__} — retry in {delay}s", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 32)
            continue
        if r.status_code < 300:
            return len(rows), 0
        if r.status_code == 409:
            return len(rows), 0  # duplicaten ok
        if 500 <= r.status_code < 600:
            print(f"  ~ {r.status_code} — retry in {delay}s", flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 32)
            continue
        print(f"  ! chunk faal {r.status_code}: {r.text[:500]}", flush=True)
        return 0, len(rows)
    print(f"  ! chunk faal na 6 retries", flush=True)
    return 0, len(rows)


def main():
    have_keys = existing_supabase_keys()
    have_listings = existing_supabase_listings()

    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")

    print("Lees SQLite cardmarket_queue...", flush=True)
    to_push: list[dict] = []
    skipped_fk = 0
    skipped_dup = 0
    total = 0
    for r in con.execute(
        "SELECT item_id, url, queued_at, fetched_at, listings_json, error, grade, card_key "
        "FROM cardmarket_queue"
    ):
        total += 1
        key = (r["item_id"], r["url"])
        if key in have_keys:
            skipped_dup += 1
            continue
        if r["item_id"] not in have_listings:
            skipped_fk += 1
            continue
        to_push.append({
            "item_id": r["item_id"],
            "url": r["url"],
            "queued_at": r["queued_at"],
            "fetched_at": r["fetched_at"],
            "listings_json": _parse_json(r["listings_json"]),
            "error": r["error"],
            "grade": r["grade"],
            "card_key": r["card_key"],
        })
    print(f"SQLite totaal: {total:,}", flush=True)
    print(f"  Al in Supabase: {skipped_dup:,}", flush=True)
    print(f"  Skip (listing niet in Supabase): {skipped_fk:,}", flush=True)
    print(f"  Te backfillen: {len(to_push):,}", flush=True)
    print()

    if not to_push:
        print("Niks te backfillen.")
        return

    ok = 0
    failed = 0
    for i in range(0, len(to_push), BATCH):
        chunk = to_push[i : i + BATCH]
        chunk_ok, chunk_fail = _post_with_retry(chunk)
        ok += chunk_ok
        failed += chunk_fail
        if (i // BATCH) % 5 == 0 or i + len(chunk) == len(to_push):
            print(f"  · {i + len(chunk):,}/{len(to_push):,} (ok={ok:,} fail={failed:,})", flush=True)
    print(f"\nKLAAR: ok={ok:,} failed={failed:,}", flush=True)


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"Duur: {time.time() - t0:.0f}s")
