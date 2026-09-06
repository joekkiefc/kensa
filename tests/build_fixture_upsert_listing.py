#!/usr/bin/env python3
"""Bouw golden fixture voor upsert_listing.

Pakt 20 recente insert-only items uit Pi SQLite (first_seen ≈ last_seen,
allemaal detail_scraped_at gevuld) en slaat op als:

  {"case": "insert_new", "input": {...}, "expected_after": {...}}

Deze fixtures worden gebruikt door test_upsert_listing.py om zowel de OUDE
(SQLite) als NIEUWE (Supabase) upsert-implementatie te verifieren.

Output: tests/write_fixtures/upsert_listing.json
"""
from __future__ import annotations
import json, sqlite3
from pathlib import Path

DB = "/home/pi/.openclaw/workspace/agents/kensa/kensa.db"
OUT = Path("/home/pi/.openclaw/workspace/agents/kensa/tests/write_fixtures/upsert_listing.json")

# Velden die de scraper (dus upsert_listing input) aanlevert.
# Overige velden (first_seen_at/last_seen_at/slab_status/card_key/locked_*/source)
# worden door upsert of latere flows gezet, NIET door upsert-input.
SCRAPER_COLS = (
    "item_id", "title_jp", "title_en", "description_jp",
    "price_jpy", "price_eur", "shipping_jpy",
    "seller_id", "seller_name", "seller_rating",
    "category_path", "condition", "authenticated", "sold",
    "detail_url", "detail_scraped_at", "status", "extra_json",
)


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT * FROM listings
        WHERE first_seen_at IS NOT NULL
          AND detail_scraped_at IS NOT NULL
          AND ABS(strftime('%s',last_seen_at) - strftime('%s',first_seen_at)) < 60
        ORDER BY first_seen_at DESC
        LIMIT 20
    """).fetchall()

    fixtures = []
    for r in rows:
        d = dict(r)
        input_row = {c: d.get(c) for c in SCRAPER_COLS}
        for b in ("authenticated", "sold"):
            if input_row.get(b) is not None:
                input_row[b] = bool(input_row[b])
        expected = {c: d.get(c) for c in d.keys()}
        fixtures.append({
            "case": "insert_new",
            "input": input_row,
            "expected_after": expected,
        })

    # --- UPDATE cases: neem eerste 5 items, doe tweede upsert met alleen
    # price_jpy + status gewijzigd. Verwacht: andere velden blijven, price+status nieuw.
    for base_fix in fixtures[:5]:
        insert_input = base_fix["input"]
        item_id = insert_input["item_id"]
        new_price = (insert_input.get("price_jpy") or 1000) + 500
        update_input = {
            "item_id": item_id,
            "price_jpy": new_price,
            "status": "analyzed",
        }
        # Expected na update = originele row + alleen price+status gewijzigd
        expected_after = dict(base_fix["expected_after"])
        expected_after["price_jpy"] = new_price
        expected_after["status"] = "analyzed"
        fixtures.append({
            "case": "update_existing",
            "insert_input": insert_input,
            "update_input": update_input,
            "expected_after": expected_after,
        })
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fixtures, indent=2, ensure_ascii=False, default=str))
    inserts = sum(1 for f in fixtures if f["case"] == "insert_new")
    updates = sum(1 for f in fixtures if f["case"] == "update_existing")
    print(f"Wrote {len(fixtures)} fixtures ({inserts} insert, {updates} update) -> {OUT}")


if __name__ == "__main__":
    main()
