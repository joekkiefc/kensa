#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa — hybride slab-check:
  1. Titel-filter: multi-slab lot / non-PSA grader → SKIP (geen OCR-call)
  2. Lezer op foto 1-2-3 tot compleet:
       - fase 4 (KENSA_SLAB_LEZER=qwen): lokale Qwen op Bionic; ALLEEN als Qwen
         niet bereikbaar is → Gemini multimodal zoals voorheen (Tommy 9-9 16:35)
       - anders: Gemini multimodal (oude gedrag)
  3. Als de lezing onvolledig blijft: val terug op klassieke Vision-check_slab
     (voor Qwen én Gemini hetzelfde — onvolledig is geen reden voor Gemini)

Return-dict is DROP-IN compatible met check_slab.check_slab() zodat analyze.py
niks hoeft te weten van de nieuwe flow. Extra velden: '_source', '_reason'.
  _source: skip | qwen | gemini_fallback | multimodal | vision_fallback

Activering via env-vars (cron_worker_ocr.sh):
  KENSA_USE_MULTIMODAL=1   → deze hybride flow i.p.v. check_slab
  KENSA_SLAB_LEZER=qwen    → Qwen als lezer (fase 4). Regel weg = terug naar Gemini.
"""

import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_slab import check_slab as _classic_check_slab, _load_photo_urls, _merge_best, DB_PATH
from ocr_router import is_multi_slab_lot
from llm_client import interpret_slab_photo
from qwen_lezer import QwenOnbereikbaar, lees_slab_foto as _qwen_lees

LEZER = os.environ.get("KENSA_SLAB_LEZER", "gemini").strip().lower()   # 'qwen' = fase 4

_LEEG = {"cert": None, "grade": None, "grade_text": None, "card_name": None,
         "set_name": None, "number": None, "year": None, "found_psa": False}


def _normalize_multimodal(r: dict) -> dict:
    """Lezer geeft 'name'/'set_code'/'variant'. Zet om naar check_slab schema."""
    if not r or r.get("_error"):
        return {}
    return {
        "cert": r.get("cert"),
        "grade": str(r.get("grade")) if r.get("grade") is not None else None,
        "grade_text": None,
        "card_name": r.get("name"),
        "set_name": r.get("set_name"),
        "set_code": r.get("set_code"),   # 2026-09-09: bewaard voor de set-check in cm_lookup
        "label_name": r.get("label_name"),   # fase 4: letterlijke labelregel (Qwen) → accept-only set-hint
        "number": r.get("number"),
        "year": r.get("year"),
        "found_psa": True if r.get("grade") else False,
    }


def _is_multimodal_complete(fields: dict) -> bool:
    """Zelfde als check_slab._is_complete: grade + card_name volstaan."""
    return bool(fields.get("grade") and fields.get("card_name"))


def _fetch_titles(item_id: str, db_path: Path) -> tuple[str | None, str | None]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT title_jp, title_en FROM listings WHERE item_id=?", (item_id,)).fetchone()
        return (row["title_jp"], row["title_en"]) if row else (None, None)
    finally:
        conn.close()


def _lees_fotos(candidates, lees, lezer: str) -> tuple[dict, int | None, list, int]:
    """Foto voor foto lezen tot compleet; anders beste velden samenvoegen.
    `lees(url) -> dict` mag QwenOnbereikbaar gooien (Qwen) — dat gaat door naar de caller."""
    best_fields = dict(_LEEG)
    source_idx = None
    per_photo = []
    calls = 0
    for photo_idx, url in candidates:
        t0 = time.perf_counter()
        raw = lees(url)
        took_ms = int((time.perf_counter() - t0) * 1000)
        calls += 1
        fields = _normalize_multimodal(raw)
        err = raw.get("_error") if raw else "no_response"
        per_photo.append({
            "photo_idx": photo_idx,
            "lezer": lezer,
            "ocr_chars": len(str(raw)) if raw else 0,
            "ocr_error": err,
            "fields": fields,
            "took_ms": took_ms,
            **({"cert_afgekeurd": raw["_cert_afgekeurd"]} if raw and raw.get("_cert_afgekeurd") else {}),
        })
        if _is_multimodal_complete(fields):
            return fields, photo_idx, per_photo, calls
        # Merge partial: hou beste velden
        prev_score = sum(1 for k in ("cert", "grade", "card_name") if best_fields.get(k))
        new_score = sum(1 for k in ("cert", "grade", "card_name") if fields.get(k))
        best_fields = _merge_best(best_fields, fields)
        if new_score > prev_score:
            source_idx = photo_idx
    return best_fields, source_idx, per_photo, calls


def check_slab_hybrid(item_id: str, max_photos: int = 3, db_path: Path = DB_PATH) -> dict:
    """Lezer-first (Qwen of Gemini), Vision-fallback. Drop-in vervanger voor check_slab.check_slab()."""
    title_jp, title_en = _fetch_titles(item_id, db_path)

    # Stap 1: titel-filter
    is_lot, pat = is_multi_slab_lot(title_jp, title_en)
    if is_lot:
        return {
            "status": "skip",
            **_LEEG,
            "source_photo_idx": None,
            "photos_tried": 0,
            "ocr_calls": 0,
            "per_photo": [],
            "_source": "skip",
            "_reason": f"multi_slab_lot:{pat}",
        }

    # Stap 2: lezer op de orig foto's
    photos = _load_photo_urls(item_id, db_path)
    if not photos:
        return {"status": "skip", "reason": "no photos", "photos_tried": 0, "ocr_calls": 0,
                "per_photo": [], "_source": "skip", "_reason": "no_photos"}
    orig = [(idx, url) for idx, url in photos if "/orig/" in url or "/detail/orig/" in url]
    candidates = (orig or photos)[:max_photos]

    lezer = LEZER
    reden_lezer = None
    best_fields, source_idx, per_photo, calls = dict(_LEEG), None, [], 0
    if lezer == "qwen":
        try:
            best_fields, source_idx, per_photo, calls = _lees_fotos(candidates, _qwen_lees, "qwen")
        except QwenOnbereikbaar as e:
            # Tommy 9-9: dit is de ENIGE weg naar Gemini.
            lezer, reden_lezer = "gemini_fallback", f"qwen_onbereikbaar: {str(e)[:120]}"
    if lezer != "qwen":
        best_fields, source_idx, per_photo, calls = _lees_fotos(
            candidates, lambda url: interpret_slab_photo(url, title_en, title_jp), "gemini")

    source = {"qwen": "qwen", "gemini_fallback": "gemini_fallback"}.get(lezer, "multimodal")

    # Stap 3: lezing compleet?
    if _is_multimodal_complete(best_fields):
        return {
            "status": "pass",
            **best_fields,
            "source_photo_idx": source_idx,
            "photos_tried": len(per_photo),
            "ocr_calls": calls,
            "per_photo": per_photo,
            "_source": source,
            "_reason": reden_lezer or "ok",
        }

    # Stap 4: fallback naar Vision (klassieke check_slab) — voor Qwen én Gemini hetzelfde
    fallback = _classic_check_slab(item_id, max_photos=max_photos, db_path=db_path)
    fallback["_source"] = "vision_fallback"
    fallback["_reason"] = ("qwen_incomplete" if source == "qwen" else "multimodal_incomplete") \
        + (f" ({reden_lezer})" if reden_lezer else "")
    fallback["_lezer"] = source
    # Bewaar per-photo info van beide (lezer-pogingen + vision)
    fallback["_multimodal_per_photo"] = per_photo
    return fallback


if __name__ == "__main__":
    import json
    iid = sys.argv[1] if len(sys.argv) > 1 else "m27984770699"
    r = check_slab_hybrid(iid)
    print(json.dumps({k: v for k, v in r.items() if k != "per_photo"}, indent=2, ensure_ascii=False, default=str))
    print(f"\n_source: {r.get('_source')} · _reason: {r.get('_reason')}")
    for p in r.get("per_photo", []):
        print(f"  photo[{p['photo_idx']}] {p.get('lezer')} {p['took_ms']}ms  err={p.get('ocr_error')}")
