#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""One-shot migrator: read last_scrape.json → upsert listings + thumbnail URLs."""

import json
from pathlib import Path

from storage import db_stats, init_db, upsert_listing, upsert_photos
from alerts import check_and_post

SCRIPT_DIR = Path(__file__).resolve().parent
SCRAPE_JSON = SCRIPT_DIR / "last_scrape.json"


def main() -> None:
    init_db()

    with open(SCRAPE_JSON, encoding="utf-8") as f:
        data = json.load(f)

    listings = data.get("listings", [])
    print(f"Migrating {len(listings)} listings from {SCRAPE_JSON.name}...")

    inserted_listings = 0
    inserted_photos = 0
    alerts_fired = 0
    for row in listings:
        is_new = upsert_listing({
            "item_id": row["item_id"],
            "title_jp": row.get("title_jp"),
            "price_jpy": row.get("price_jpy"),
            "price_eur": row.get("price_eur"),
            "detail_url": row.get("detail_url"),
            "authenticated": row.get("authenticated"),
            "sold": row.get("sold"),
        })
        inserted_listings += 1
        if row.get("thumbnail"):
            inserted_photos += upsert_photos(row["item_id"], [row["thumbnail"]])
        # Alert-check ALLEEN voor nieuwe items — voorkomt dubbele meldingen bij relist-updates
        if is_new:
            try:
                alerts_fired += check_and_post(row)
            except Exception as e:
                print(f"  alert-check fout voor {row.get('item_id')}: {e}")

    print(f"Listings upserted: {inserted_listings}")
    print(f"Photo URLs inserted: {inserted_photos}")
    print(f"Alerts fired: {alerts_fired}")
    print()
    print("db_stats():")
    print(json.dumps(db_stats(), indent=2))


if __name__ == "__main__":
    main()
