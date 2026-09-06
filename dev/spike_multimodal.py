#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa spike: Gemini multimodal direct-op-foto vs Vision baseline.

Voor N items: probeer alle 3 (orig) foto's met interpret_slab_photo tot fields
compleet zijn. Sla ook Vision-baseline op ter vergelijking. Resultaat JSON →
webapp /spike-multimodal toont visueel per item de flow + faal-redenen.

Contract: cert + grade + card_name + number allemaal gevonden = success.
"""

import json
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm_client import interpret_slab_photo
from vision import ocr_and_parse as vision_ocr_and_parse, CERT_RE, GRADE_NUM_RE

DB = Path("/home/pi/.openclaw/workspace/agents/kensa/kensa.db")
MEDIA_DIR = Path("/home/pi/.openclaw/workspace/agents/kensa/spike_multimodal_media")
MEDIA_DIR.mkdir(exist_ok=True)
RESULTS_JSON = MEDIA_DIR / "results.json"
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 20
MAX_PHOTOS = 3

NUMBER_VALID_RE = re.compile(r"^\d{1,4}(/[A-Z0-9\-]+)?$", re.IGNORECASE)


def is_complete(fields: dict) -> tuple[bool, list[str]]:
    """Return (complete, missing_fields). Complete = cert+grade+card_name+number allemaal geldig."""
    missing = []
    cert = fields.get("cert")
    if not cert or not CERT_RE.fullmatch(str(cert)):
        missing.append("cert")
    grade = fields.get("grade")
    if not grade or not GRADE_NUM_RE.fullmatch(str(grade)):
        missing.append("grade")
    name = fields.get("name") or fields.get("card_name")
    if not name or len(str(name).strip()) < 3:
        missing.append("card_name")
    number = fields.get("number")
    if not number or not NUMBER_VALID_RE.match(str(number).strip()):
        missing.append("number")
    return len(missing) == 0, missing


def normalize_multimodal_fields(r: dict) -> dict:
    """Multimodal geeft 'name' + 'set_code' etc. Zet om naar zelfde velden als vision-parse."""
    if not r or r.get("_error"):
        return {}
    return {
        "cert": r.get("cert"),
        "grade": r.get("grade"),
        "card_name": r.get("name"),
        "set_name": r.get("set_name"),
        "number": r.get("number"),
        "year": r.get("year"),
        "subtype": r.get("subtype"),
        "variant": r.get("variant"),
    }


def main():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT l.item_id, l.title_jp, l.title_en, l.price_jpy
           FROM listings l
           JOIN analysis a ON a.item_id = l.item_id AND a.trap = 'slab_ocr'
           WHERE l.detail_scraped_at IS NOT NULL
             AND l.item_id LIKE 'm%'
             AND (SELECT COUNT(*) FROM photos p WHERE p.item_id = l.item_id
                  AND (url_original LIKE '%/orig/%' OR url_original LIKE '%/detail/orig/%')) > 0
             AND json_extract(a.result_json, '$.status') = 'pass'
             AND (l.title_en LIKE '%PSA%' OR l.title_jp LIKE '%PSA%')
             AND l.title_en NOT LIKE '%BGS%'
             AND l.title_en NOT LIKE '%CGC%'
             AND l.title_en NOT LIKE '%ARS%'
           ORDER BY a.created_at DESC LIMIT ?""",
        (LIMIT,),
    ).fetchall()

    print(f"Test multimodal op {len(rows)} items", file=sys.stderr)
    results = []
    total_multi_cost_usd = 0.0
    n_success = 0
    n_fail = 0

    for i, r in enumerate(rows, 1):
        iid = r["item_id"]
        photos = conn.execute(
            """SELECT photo_index, url_original FROM photos WHERE item_id=?
               AND (url_original LIKE '%/orig/%' OR url_original LIKE '%/detail/orig/%')
               ORDER BY photo_index LIMIT ?""",
            (iid, MAX_PHOTOS),
        ).fetchall()

        per_photo = []
        success = False
        success_photo = None
        final_fields = {}
        total_lat = 0

        for p in photos:
            t0 = time.time()
            raw = interpret_slab_photo(p["url_original"], r["title_en"], r["title_jp"])
            lat = int((time.time() - t0) * 1000)
            total_lat += lat
            in_tokens = raw.get("_meta", {}).get("in_tokens", 0) if raw else 0
            out_tokens = raw.get("_meta", {}).get("out_tokens", 0) if raw else 0
            img_bytes = raw.get("_meta", {}).get("image_bytes", 0) if raw else 0
            fields = normalize_multimodal_fields(raw)
            ok, missing = is_complete(fields)
            per_photo.append({
                "photo_idx": p["photo_index"],
                "url": p["url_original"],
                "latency_ms": lat,
                "in_tokens": in_tokens,
                "out_tokens": out_tokens,
                "image_bytes": img_bytes,
                "fields": fields,
                "valid": ok,
                "missing": missing,
                "error": raw.get("_error") if raw else "no_response",
            })
            # Gemini Flash cost: $0.10/M in, $0.40/M out
            total_multi_cost_usd += (in_tokens / 1e6 * 0.10) + (out_tokens / 1e6 * 0.40)
            if ok:
                success = True
                success_photo = p["photo_index"]
                final_fields = fields
                break

        # Baseline: Vision op eerste foto
        first = photos[0]["url_original"] if photos else None
        vision_baseline = None
        if first:
            try:
                v = vision_ocr_and_parse(first)
                vision_baseline = v.get("fields") if v else None
            except Exception as e:
                vision_baseline = {"_error": str(e)}

        if success:
            n_success += 1
        else:
            n_fail += 1

        results.append({
            "item_id": iid,
            "title": r["title_en"] or r["title_jp"] or "?",
            "price_jpy": r["price_jpy"],
            "success": success,
            "success_photo_idx": success_photo,
            "photos_tried": len(per_photo),
            "total_latency_ms": total_lat,
            "final_fields": final_fields,
            "per_photo": per_photo,
            "vision_baseline": vision_baseline,
        })

        print(f"[{i}/{len(rows)}] {iid}  photos={len(per_photo)}  {'✅ OK' if success else '❌ FAIL'}  "
              f"lat={total_lat}ms  {'missing:' + ','.join(per_photo[-1]['missing']) if not success and per_photo else ''}",
              file=sys.stderr)

    RESULTS_JSON.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\n=== SAMENVATTING ===", file=sys.stderr)
    print(f"  Success: {n_success}/{len(results)} ({100*n_success/max(1,len(results)):.0f}%)", file=sys.stderr)
    print(f"  Fail:    {n_fail}", file=sys.stderr)
    print(f"  Gemini multimodal kost: ${total_multi_cost_usd:.4f} voor {len(results)} items", file=sys.stderr)
    print(f"  Per 1000: ${total_multi_cost_usd*1000/max(1,len(results)):.2f}", file=sys.stderr)
    print(f"  Extrapolation naar Kensa volume (~90k slabs/mnd): "
          f"${total_multi_cost_usd*90000/max(1,len(results)):.0f}/mnd = €{total_multi_cost_usd*90000/max(1,len(results))*0.93:.0f}/mnd",
          file=sys.stderr)
    print(f"\nResultaten: {RESULTS_JSON}", file=sys.stderr)
    print(f"Bekijk in browser: /spike-multimodal", file=sys.stderr)


if __name__ == "__main__":
    main()
