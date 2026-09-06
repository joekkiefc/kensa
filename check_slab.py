#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa — Check 1: slab OCR + parse.

Iterate photos sequentially. Stop as soon as we have a "complete" slab-read
(cert + grade + card_name). If we exhaust `max_photos` still incomplete,
return the best partial we found.

Return dict:
{
    status: "pass" | "skip",           # pass = complete slab-read
    cert, grade, grade_text, card_name, set_name, number, year, found_psa,
    source_photo_idx: int | None,
    photos_tried: int,
    ocr_calls: int,
    per_photo: [ {photo_idx, ocr_chars, fields, took_ms} ... ]
}
"""

import sqlite3
import sys
import time
from pathlib import Path

from vision import ocr_and_parse

SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "kensa.db"


def _is_complete(fields: dict) -> bool:
    """grade + card_name volstaan. Cert is een pre (voor relist-detectie) maar geen must —
    veel Buyee-verkopers fotograferen alleen de bovenkant van de PSA-slab en missen daardoor
    het cert-nummer aan de onderkant."""
    return bool(fields.get("grade") and fields.get("card_name"))


def _merge_best(current: dict, new: dict) -> dict:
    """Combine incremental info across photos (fill in missing fields only)."""
    out = dict(current)
    for k, v in new.items():
        if v is not None and out.get(k) is None:
            out[k] = v
    return out


def _load_photo_urls(item_id: str, db_path: Path = DB_PATH) -> list[tuple[int, str]]:
    # Supabase-first switch: KENSA_READ_STORAGE=supabase leest van Supabase.
    import os
    if os.environ.get("KENSA_READ_STORAGE", "").lower() == "supabase":
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from storage_supabase import load_photo_urls_supabase
        return load_photo_urls_supabase(item_id)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT photo_index, url_original FROM photos
               WHERE item_id = ? ORDER BY photo_index""",
            (item_id,),
        ).fetchall()
        return [(r["photo_index"], r["url_original"]) for r in rows]
    finally:
        conn.close()


def check_slab(item_id: str, max_photos: int = 3, db_path: Path = DB_PATH) -> dict:
    photos = _load_photo_urls(item_id, db_path)
    if not photos:
        return {"status": "skip", "reason": "no photos", "photos_tried": 0, "ocr_calls": 0, "per_photo": []}

    orig_photos = [(idx, url) for idx, url in photos if "/orig/" in url or "/detail/orig/" in url]
    candidates = orig_photos or photos

    best_fields = {"cert": None, "grade": None, "grade_text": None, "card_name": None,
                   "set_name": None, "number": None, "year": None, "found_psa": False}
    source_idx = None
    per_photo = []
    ocr_calls = 0

    for photo_idx, url in candidates[:max_photos]:
        t0 = time.perf_counter()
        result = ocr_and_parse(url)
        took_ms = int((time.perf_counter() - t0) * 1000)
        ocr = result.get("ocr", {})
        fields = result.get("fields", {})
        ocr_calls += 1
        per_photo.append({
            "photo_idx": photo_idx,
            "ocr_chars": ocr.get("chars", 0),
            "ocr_error": ocr.get("error"),
            "fields": fields,
            "took_ms": took_ms,
        })
        if _is_complete(fields):
            best_fields = fields
            source_idx = photo_idx
            break
        prev_score = sum(1 for k in ("cert", "grade", "card_name") if best_fields.get(k))
        new_score = sum(1 for k in ("cert", "grade", "card_name") if fields.get(k))
        if new_score > prev_score:
            best_fields = _merge_best(best_fields, fields)
            source_idx = photo_idx
        else:
            best_fields = _merge_best(best_fields, fields)

    status = "pass" if _is_complete(best_fields) else "skip"
    return {
        "status": status,
        **best_fields,
        "source_photo_idx": source_idx,
        "photos_tried": len(per_photo),
        "ocr_calls": ocr_calls,
        "per_photo": per_photo,
    }


if __name__ == "__main__":
    import json
    item_id = sys.argv[1] if len(sys.argv) > 1 else "m10228250509"
    result = check_slab(item_id)
    print(f"=== check_slab({item_id}) ===")
    print(f"status: {result['status']}  photos_tried: {result['photos_tried']}  ocr_calls: {result['ocr_calls']}")
    print(f"cert: {result.get('cert')}  grade: {result.get('grade')} ({result.get('grade_text')})")
    print(f"card: {result.get('card_name')}  set: {result.get('set_name')}  #{result.get('number')} ({result.get('year')})")
    print(f"source photo idx: {result.get('source_photo_idx')}")
    for p in result["per_photo"]:
        print(f"  photo[{p['photo_idx']}] {p['took_ms']}ms {p['ocr_chars']}chars")
