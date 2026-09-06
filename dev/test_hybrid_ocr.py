#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Test hybride OCR-pipeline op N items — tel route (skip/local/vision) + latency.
Volgt productie-gedrag: probeer foto 1, als niet compleet → foto 2, dan 3 (max)."""

import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ocr_router import ocr_and_parse_hybrid, _validate_fields

DB = Path("/home/pi/.openclaw/workspace/agents/kensa/kensa.db")
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 30
MAX_PHOTOS = 3


def main():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    # Zelfde strenge filter als spike_crop_gallery.py:
    # - Alleen Mercari (m*) — geen PayPay/BGS-vervuiling
    # - status='pass' + cert gevonden = productie erkent 't als PSA-slab
    # - Titel bevat "PSA" — extra bescherming tegen ARS/BGS/CGC mix
    rows = conn.execute(
        """SELECT l.item_id, l.title_jp, l.title_en
           FROM listings l
           JOIN analysis a ON a.item_id = l.item_id AND a.trap = 'slab_ocr'
           WHERE l.detail_scraped_at IS NOT NULL
             AND l.item_id LIKE 'm%'
             AND (SELECT COUNT(*) FROM photos p WHERE p.item_id = l.item_id) > 0
             AND json_extract(a.result_json, '$.status') = 'pass'
             AND json_extract(a.result_json, '$.cert') IS NOT NULL
             AND (l.title_en LIKE '%PSA%' OR l.title_jp LIKE '%PSA%')
             AND l.title_en NOT LIKE '%BGS%'
             AND l.title_en NOT LIKE '%CGC%'
             AND l.title_en NOT LIKE '%ARS%'
           ORDER BY a.created_at DESC
           LIMIT ?""",
        (LIMIT,),
    ).fetchall()

    print(f"Test hybride OCR-pipeline op {len(rows)} items", file=sys.stderr)

    counts = {"skip": 0, "local": 0, "vision": 0}
    total_lat_ms = 0
    lat_by_route = {"skip": 0, "local": 0, "vision": 0}
    n_by_route = {"skip": 0, "local": 0, "vision": 0}
    missing_reasons = {}

    for i, r in enumerate(rows, 1):
        iid = r["item_id"]
        # Haal alle foto's op (max MAX_PHOTOS)
        # Filter alleen /orig/ URLs (productie doet ditzelfde, thumbnails skippen)
        photos = conn.execute(
            """SELECT url_original FROM photos WHERE item_id=?
               AND (url_original LIKE '%/orig/%' OR url_original LIKE '%/detail/orig/%')
               ORDER BY photo_index LIMIT ?""",
            (iid, MAX_PHOTOS),
        ).fetchall()
        # Fallback: als er geen orig-URLs zijn, gebruik gewoon alles
        if not photos:
            photos = conn.execute(
                "SELECT url_original FROM photos WHERE item_id=? ORDER BY photo_index LIMIT ?",
                (iid, MAX_PHOTOS),
            ).fetchall()
        if not photos:
            continue

        # Probeer per foto tot een LOCAL succes of tot alle 3 mislukten
        best_result = None
        photos_tried = 0
        total_item_lat = 0
        chose_route = None

        for p in photos:
            photos_tried += 1
            try:
                result = ocr_and_parse_hybrid(p["url_original"], title_jp=r["title_jp"], title_en=r["title_en"])
            except Exception as e:
                print(f"[{i}/{len(rows)}] {iid} photo{photos_tried} EXCEPTION: {e}", file=sys.stderr)
                continue
            total_item_lat += result.get("_latency_ms", 0)
            route = result.get("_route", "?")
            if route == "skip":
                # Skip is per-item beslissing → geen volgende foto proberen
                best_result = result
                chose_route = "skip"
                break
            if route == "local":
                best_result = result
                chose_route = "local"
                break  # goed, klaar
            # Vision-fallback: bewaar deze maar probeer volgende foto voor local kans
            if best_result is None:
                best_result = result
                chose_route = "vision"

        if best_result is None:
            print(f"[{i}/{len(rows)}] {iid} ALL PHOTOS FAILED", file=sys.stderr)
            continue

        route = chose_route
        counts[route] = counts.get(route, 0) + 1
        total_lat_ms += total_item_lat
        lat_by_route[route] += total_item_lat
        n_by_route[route] += 1

        if route == "vision":
            reason = best_result.get("_reason", "?").split(":")[0]
            missing_reasons[reason] = missing_reasons.get(reason, 0) + 1

        f = best_result.get("fields", {})
        print(f"[{i}/{len(rows)}] {iid}  photos={photos_tried} route={route:6s}  lat={total_item_lat}ms  "
              f"cert={f.get('cert')} grade={f.get('grade')} name={(f.get('card_name') or '')[:25]}",
              file=sys.stderr)

    print("\n" + "=" * 60, file=sys.stderr)
    print(f"ROUTE BREAKDOWN ({sum(counts.values())} items):", file=sys.stderr)
    for r in ("skip", "local", "vision"):
        c = counts.get(r, 0)
        avg_lat = lat_by_route[r] / max(1, n_by_route[r])
        print(f"  {r:6s}: {c:4d} ({100*c/max(1,sum(counts.values())):5.1f}%)  "
              f"avg_lat={avg_lat:.0f}ms", file=sys.stderr)

    print("\nVISION-FALLBACK REDENEN:", file=sys.stderr)
    for reason, cnt in sorted(missing_reasons.items(), key=lambda x: -x[1]):
        print(f"  {reason}: {cnt}", file=sys.stderr)

    # Kostenprojectie: Vision = $1.50 / 1000 units. Skip + local = €0.
    tot = sum(counts.values())
    vision_pct = 100 * counts["vision"] / max(1, tot)
    print(f"\nKOSTEN-PROJECTIE:", file=sys.stderr)
    print(f"  {vision_pct:.0f}% van items → Vision (€200/mnd × {vision_pct:.0f}% = €{200*vision_pct/100:.0f}/mnd)", file=sys.stderr)
    print(f"  Besparing t.o.v. all-Vision: €{200 - 200*vision_pct/100:.0f}/mnd", file=sys.stderr)


if __name__ == "__main__":
    main()
