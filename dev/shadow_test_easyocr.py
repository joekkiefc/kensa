#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Shadow-test: vergelijk EasyOCR (Tommy's PC) met Google Vision op laatste N slabs.

Pakt de meest recente items met slab_ocr-analysis in de DB, gebruikt hun eerste foto
door beide OCR-engines, en tabelleert de agreement op de kritieke velden
(cert, grade, card_name) via parse_psa_fields().
"""

import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vision import ocr_and_parse, parse_psa_fields

DB = Path("/home/pi/.openclaw/workspace/agents/kensa/kensa.db")
EASYOCR_URL = "http://100.125.116.37:8898/ocr"
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 200


def easyocr_ocr(photo_url: str, timeout: int = 30) -> tuple[str | None, int, str | None]:
    """POST photo URL naar EasyOCR-endpoint. Return (text, latency_ms, error)."""
    t0 = time.time()
    body = json.dumps({"image_url": photo_url}).encode()
    req = urllib.request.Request(
        EASYOCR_URL,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            d = json.loads(resp.read())
        return d.get("text", ""), int((time.time() - t0) * 1000), None
    except Exception as e:
        return None, int((time.time() - t0) * 1000), f"{type(e).__name__}: {e}"


def _norm(v):
    return (str(v).strip().upper() if v else "").replace(" ", "")


def compare(a: dict, b: dict) -> dict:
    """Return per-field agreement."""
    fields = ["cert", "grade", "card_name", "set_name", "number", "year"]
    result = {}
    for f in fields:
        va = _norm(a.get(f))
        vb = _norm(b.get(f))
        match = (va == vb) if (va or vb) else True
        result[f] = {"vision": a.get(f), "easyocr": b.get(f), "match": match}
    return result


def main():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    # Alleen bevestigde PSA-slabs (geen ARS/BGS/CGC vervuiling)
    rows = conn.execute(
        """SELECT l.item_id,
                  (SELECT url_original FROM photos p WHERE p.item_id = l.item_id
                   ORDER BY p.photo_index LIMIT 1) as first_photo
           FROM listings l
           JOIN analysis a ON a.item_id = l.item_id AND a.trap = 'slab_ocr'
           WHERE l.detail_scraped_at IS NOT NULL
             AND (SELECT COUNT(*) FROM photos p WHERE p.item_id = l.item_id) > 0
             AND json_extract(a.result_json, '$.status') = 'pass'
             AND json_extract(a.result_json, '$.cert') IS NOT NULL
             AND (l.title_en LIKE '%PSA%' OR l.title_jp LIKE '%PSA%')
           ORDER BY a.created_at DESC
           LIMIT ?""",
        (LIMIT,),
    ).fetchall()

    print(f"Vergelijking op {len(rows)} slabs", file=sys.stderr)
    total = len(rows)
    counts = {f: 0 for f in ["cert", "grade", "card_name", "set_name", "number", "year"]}
    both_none = {f: 0 for f in counts}
    easyocr_fail = 0
    vision_fail = 0
    latencies_v = []
    latencies_e = []
    mismatches = []

    for i, r in enumerate(rows, 1):
        iid = r["item_id"]
        photo = r["first_photo"]
        if not photo:
            continue
        # Vision
        t0 = time.time()
        try:
            v_full = ocr_and_parse(photo)
            latencies_v.append(int((time.time() - t0) * 1000))
            if v_full.get("ocr", {}).get("error"):
                vision_fail += 1
                print(f"[{i}/{total}] {iid} VISION ERR: {v_full['ocr']['error']}", file=sys.stderr)
                continue
            vision = v_full["fields"]
        except Exception as e:
            vision_fail += 1
            print(f"[{i}/{total}] {iid} VISION FAIL: {e}", file=sys.stderr)
            continue

        # EasyOCR
        text, lat_e, err = easyocr_ocr(photo)
        if err:
            easyocr_fail += 1
            print(f"[{i}/{total}] {iid} EASY FAIL: {err}", file=sys.stderr)
            continue
        latencies_e.append(lat_e)
        easy_parsed = parse_psa_fields(text)

        cmp = compare(vision, easy_parsed)
        row_mismatches = []
        for f, d in cmp.items():
            if d["match"]:
                counts[f] += 1
                if not (d["vision"] or d["easyocr"]):
                    both_none[f] += 1
            else:
                row_mismatches.append((f, d["vision"], d["easyocr"]))
        if row_mismatches:
            mismatches.append((iid, row_mismatches))

        if i % 25 == 0:
            print(f"[{i}/{total}] processed  v_lat_avg={sum(latencies_v)/len(latencies_v):.0f}ms  "
                  f"e_lat_avg={sum(latencies_e)/max(1,len(latencies_e)):.0f}ms", file=sys.stderr)

    print("\n" + "=" * 60, file=sys.stderr)
    print(f"TOTAAL: {total} items  ·  vision_fail={vision_fail}  easyocr_fail={easyocr_fail}", file=sys.stderr)
    if latencies_v:
        print(f"  Vision  avg latency: {sum(latencies_v)/len(latencies_v):.0f}ms", file=sys.stderr)
    if latencies_e:
        print(f"  EasyOCR avg latency: {sum(latencies_e)/len(latencies_e):.0f}ms", file=sys.stderr)

    print("\nAGREEMENT PER VELD:", file=sys.stderr)
    valid = total - vision_fail - easyocr_fail
    for f, m in counts.items():
        pct = 100 * m / max(1, valid)
        both = both_none[f]
        real_match = m - both
        real_total = valid - both
        real_pct = 100 * real_match / max(1, real_total)
        print(f"  {f:12s}  match {m:4d}/{valid} ({pct:5.1f}%)   "
              f"waarvan beide leeg: {both}    real-match: {real_match}/{real_total} ({real_pct:5.1f}%)",
              file=sys.stderr)

    print(f"\nMISMATCH VOORBEELDEN (eerste 10):", file=sys.stderr)
    for iid, mms in mismatches[:10]:
        print(f"  {iid}:", file=sys.stderr)
        for f, v, e in mms:
            print(f"    {f}: vision={v!r}  easyocr={e!r}", file=sys.stderr)


if __name__ == "__main__":
    main()
