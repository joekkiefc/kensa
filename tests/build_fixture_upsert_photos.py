#!/usr/bin/env python3
"""Bouw golden fixture voor upsert_photos.

Pakt 20 items met photos uit Pi + de UPDATE variant: 5 items waar we
eerst 2 URLs inserten, dan 2 extra URLs waarvan één duplicate (moet
overgeslagen worden) en één nieuw (moet toegevoegd met next index).

Fixture structuur:
  {"case": "insert_new",
   "item_id": ..., "input_urls": [...],
   "expected_rows": [{"photo_index": 0, "url_original": ...}, ...],
   "expected_return": <int aantal newly inserted>}

  {"case": "update_partial_dupe",
   "item_id": ..., "first_urls": [u1, u2],
   "second_urls": [u2, u3],   # u2 = dupe, u3 = nieuw
   "expected_rows_after": [
      {"photo_index": 0, "url_original": u1},
      {"photo_index": 1, "url_original": u2},
      {"photo_index": 2, "url_original": u3}
   ],
   "expected_return_first": 2,
   "expected_return_second": 1}
"""
from __future__ import annotations
import json, sqlite3
from pathlib import Path

DB = "/home/pi/.openclaw/workspace/agents/kensa/kensa.db"
OUT = Path("/home/pi/.openclaw/workspace/agents/kensa/tests/write_fixtures/upsert_photos.json")


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    # 20 items met 2+ photos, gepakt uit recent
    item_rows = conn.execute("""
        SELECT p.item_id, COUNT(*) as n
        FROM photos p
        JOIN listings l ON l.item_id = p.item_id
        GROUP BY p.item_id
        HAVING n >= 2
        ORDER BY MAX(p.photo_id) DESC
        LIMIT 20
    """).fetchall()

    fixtures = []
    for r in item_rows:
        item_id = r["item_id"]
        photos = conn.execute("""
            SELECT photo_index, url_original FROM photos
            WHERE item_id = ? ORDER BY photo_index
        """, (item_id,)).fetchall()
        urls = [p["url_original"] for p in photos]
        # verwacht: photo_index oplopend vanaf 0
        expected_rows = [{"photo_index": i, "url_original": u} for i, u in enumerate(urls)]
        fixtures.append({
            "case": "insert_new",
            "item_id": item_id,
            "input_urls": urls,
            "expected_rows": expected_rows,
            "expected_return": len(urls),
        })

    # Update-cases: eerste 5 items → insert 2 URLs, dan 2 URLs waarvan 1 dupe
    for base in fixtures[:5]:
        urls = base["input_urls"]
        if len(urls) < 3:
            continue
        u1, u2, u3 = urls[0], urls[1], urls[2]
        fixtures.append({
            "case": "update_partial_dupe",
            "item_id": base["item_id"] + "_upd",  # test-item_id, verse start
            "first_urls": [u1, u2],
            "second_urls": [u2, u3],
            "expected_rows_after": [
                {"photo_index": 0, "url_original": u1},
                {"photo_index": 1, "url_original": u2},
                {"photo_index": 2, "url_original": u3},
            ],
            "expected_return_first": 2,
            "expected_return_second": 1,
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fixtures, indent=2, ensure_ascii=False, default=str))
    inserts = sum(1 for f in fixtures if f["case"] == "insert_new")
    updates = sum(1 for f in fixtures if f["case"] == "update_partial_dupe")
    print(f"Wrote {len(fixtures)} fixtures ({inserts} insert, {updates} update) -> {OUT}")


if __name__ == "__main__":
    main()
