#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa — spike: OpenCV PSA-label crop-detectie visueel testen.

Pakt N recente Mercari-listings met foto's, download alle foto's,
probeert de rode PSA-rand te detecteren en te croppen. Sla origineel +
crop op naar disk + genereer JSON met resultaten. Kensa-webapp toont
dit dan op /spike-crop pagina.
"""

import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crop_psa_label import detect_red_label_bbox, crop_from_bytes

DB = Path("/home/pi/.openclaw/workspace/agents/kensa/kensa.db")
MEDIA_DIR = Path("/home/pi/.openclaw/workspace/agents/kensa/spike_crop_media")
MEDIA_DIR.mkdir(exist_ok=True)
RESULTS_JSON = MEDIA_DIR / "results.json"
UA = {"User-Agent": "Mozilla/5.0"}
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 20
OFFSET = int(sys.argv[2]) if len(sys.argv) > 2 else 0
MAX_PHOTOS = 3


def download(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=15).read()


def main():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    # Pak recente Mercari items met minstens 1 foto
    # Alleen bevestigde PSA-slabs — check_slab.status='pass' + cert gevonden.
    # ARS/BGS/CGC slabs hebben geen rode rand, zouden vals-negatief scoren in crop-test.
    rows = conn.execute(
        """SELECT DISTINCT l.item_id, l.title_jp, l.title_en, l.price_jpy
           FROM listings l
           JOIN analysis a ON a.item_id = l.item_id AND a.trap = 'slab_ocr'
           WHERE l.detail_scraped_at IS NOT NULL
             AND l.item_id LIKE 'm%'
             AND (SELECT COUNT(*) FROM photos p WHERE p.item_id = l.item_id) > 0
             AND json_extract(a.result_json, '$.status') = 'pass'
             AND json_extract(a.result_json, '$.cert') IS NOT NULL
             AND (l.title_en LIKE '%PSA%' OR l.title_jp LIKE '%PSA%')
           ORDER BY a.created_at DESC
           LIMIT ? OFFSET ?""",
        (LIMIT, OFFSET),
    ).fetchall()

    print(f"Verwerkt {len(rows)} items (max {MAX_PHOTOS} foto's per item)", file=sys.stderr)
    results = []

    for i, r in enumerate(rows, 1):
        iid = r["item_id"]
        photos = conn.execute(
            """SELECT photo_index, url_original FROM photos
               WHERE item_id = ? ORDER BY photo_index LIMIT ?""",
            (iid, MAX_PHOTOS),
        ).fetchall()

        item_photos = []
        for p in photos:
            photo_idx = p["photo_index"]
            url = p["url_original"]
            key = f"{iid}_{photo_idx}"
            orig_path = MEDIA_DIR / f"{key}_orig.jpg"
            crop_path = MEDIA_DIR / f"{key}_crop.jpg"

            try:
                img_bytes = download(url)
            except Exception as e:
                print(f"  [{i}/{len(rows)}] {key} download FAIL: {e}", file=sys.stderr)
                continue

            orig_path.write_bytes(img_bytes)
            try:
                crop_bytes, bbox = crop_from_bytes(img_bytes)
            except Exception as e:
                item_photos.append({
                    "photo_idx": photo_idx,
                    "url": url,
                    "orig_file": orig_path.name,
                    "crop_file": None,
                    "bbox": None,
                    "error": f"{type(e).__name__}: {e}",
                })
                continue

            if crop_bytes:
                crop_path.write_bytes(crop_bytes)
                item_photos.append({
                    "photo_idx": photo_idx,
                    "url": url,
                    "orig_file": orig_path.name,
                    "crop_file": crop_path.name,
                    "bbox": list(bbox),
                    "orig_bytes": len(img_bytes),
                    "crop_bytes": len(crop_bytes),
                    "error": None,
                })
            else:
                item_photos.append({
                    "photo_idx": photo_idx,
                    "url": url,
                    "orig_file": orig_path.name,
                    "crop_file": None,
                    "bbox": None,
                    "error": "no_red_label_detected",
                })

        results.append({
            "item_id": iid,
            "title": r["title_en"] or r["title_jp"],
            "price_jpy": r["price_jpy"],
            "photos": item_photos,
        })
        print(f"  [{i}/{len(rows)}] {iid}: {sum(1 for p in item_photos if p.get('crop_file'))}/{len(item_photos)} crops OK", file=sys.stderr)

    RESULTS_JSON.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    total_photos = sum(len(r["photos"]) for r in results)
    total_crops = sum(1 for r in results for p in r["photos"] if p.get("crop_file"))
    print(f"\n=== Klaar. {total_crops}/{total_photos} foto's gecropt "
          f"({100*total_crops/max(1,total_photos):.0f}% detectie-succes) ===", file=sys.stderr)
    print(f"Media: {MEDIA_DIR}", file=sys.stderr)
    print(f"JSON:  {RESULTS_JSON}", file=sys.stderr)


if __name__ == "__main__":
    main()
