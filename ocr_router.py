#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa OCR router — hybride pipeline:

  1. Titel-check → is dit een multi-slab lot? Skip.
  2. OpenCV rode-rand crop op de foto.
  3. Lokaal OCR (GOT-endpoint op werk-PC via Tailscale) op de crop.
  4. parse_psa_fields() → validate 4 kritieke velden (cert/grade/card_name/number).
  5. Als valid → return lokaal result. Anders → Vision-fallback op de VOLLE originele foto.

Output-structuur is identiek aan wat vision.ocr_and_parse() geeft (backwards compat):
  {"ocr": {...}, "fields": {...}, "_route": "local"|"vision"|"skip", "_reason": str}
"""

import re
import sys
import time
import urllib.request
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crop_psa_label import crop_from_bytes
from vision import ocr_and_parse as vision_ocr_and_parse, parse_psa_fields, CERT_RE, GRADE_NUM_RE

LOCAL_OCR_URL = "http://100.125.116.37:8898/ocr"
LOCAL_OCR_TIMEOUT = 15
UA_HEADERS = {"User-Agent": "Mozilla/5.0"}

# Multi-slab / multi-card lot patronen — als één van deze in titel: skip OCR.
# Zowel Engelse als Japanse indicatoren.
MULTI_SLAB_PATTERNS = [
    r"\b連番\b",                     # Japans "consecutive numbers"
    r"consecutive\s+numbers?",
    r"\bセット\b",                    # set
    r"\bset\s+of\s+\d+\b",
    r"\d+\s+cards?\b",              # "8 cards"
    r"\d+\s*pack\b",                # "3 pack"
    r"\blot\s+of\s+\d+\b",
    r"\bbundle\b",
    r"(?:psa\s*10.*?){2,}",         # 2+ x "PSA10" in titel
]
MULTI_SLAB_RE = re.compile("|".join(MULTI_SLAB_PATTERNS), re.IGNORECASE | re.UNICODE)

NUMBER_VALID_RE = re.compile(r"^\d{1,4}(/[A-Z0-9\-]+)?$", re.IGNORECASE)
CARD_NAME_VALID_RE = re.compile(r"^[A-Z][A-Z0-9 .\-'/&]{2,}$")


def is_multi_slab_lot(title_jp: str | None, title_en: str | None) -> tuple[bool, str]:
    """Return (is_lot, matched_pattern)."""
    for t in (title_jp, title_en):
        if not t: continue
        m = MULTI_SLAB_RE.search(t)
        if m:
            return True, m.group(0)
    return False, ""


def _normalize_local_ocr_text(text: str) -> str:
    """GOT-OCR geeft PSA-label soms op één regel met spaties tussen tokens.
    Onze parser (parse_psa_fields) verwacht regels. Split op key-patronen:
    vóór # (number), vóór grade-labels, vóór cert (8-10 digits), vóór PSA."""
    if not text:
        return text
    # Nummer #123 op eigen regel: splits vóór # én tussen #<digits> en volgend woord
    text = re.sub(r"\s+#\s*", "\n#", text)
    text = re.sub(r"(#\d{1,4}(?:/[A-Z0-9\-]+)?)\s+", r"\1\n", text)
    # Split vóór grade-woorden
    text = re.sub(r"\s+(GEM\s*MT|GEM\s*MINT|MINT|EX[- ]?MT|VG[- ]?EX|POOR|FAIR|GOOD|AUTHENTIC)\s*",
                  r"\n\1 ", text, flags=re.IGNORECASE)
    # Split voor PSA-losse token
    text = re.sub(r"\s+(PSA)\s+", r"\n\1\n", text)
    # Split vóór cert-nummer (8-10 digits)
    text = re.sub(r"\s+(\d{8,10})\b", r"\n\1", text)
    # Split vóór grade cijfer aan einde string / vóór cert (grade zit vaak "GEM MT 10 129287568")
    text = re.sub(r"\bMT\s+(10|9\.5|9|8\.5|8|7|6|5|4|3|2|1)\s*\n", r"MT\n\1\n", text)
    # Split vóór 'SPECIAL ART' etc — variant marker
    text = re.sub(r"\s+(SPECIAL\s+ART|ALT\s+ART|FULL\s+ART|RAINBOW\s+RARE)\s*",
                  r"\n\1", text, flags=re.IGNORECASE)
    # PSA-labels zijn ALL CAPS. GOT lowercased soms 'ex'/'v'/'gx'. Force uppercase
    # zodat onze card_name regex (die ALL-CAPS eist) matches vindt.
    text = text.upper()
    return text.strip()


def _validate_fields(fields: dict) -> tuple[bool, list[str]]:
    """Check dat cert/grade/card_name/number allemaal aanwezig+geldig zijn.
    Return (all_valid, list_of_missing_or_invalid_fields)."""
    missing = []
    cert = fields.get("cert")
    if not cert or not CERT_RE.fullmatch(str(cert)):
        missing.append("cert")
    grade = fields.get("grade")
    if not grade or not GRADE_NUM_RE.fullmatch(str(grade)):
        missing.append("grade")
    card_name = fields.get("card_name")
    if not card_name or not CARD_NAME_VALID_RE.fullmatch(str(card_name).strip()):
        missing.append("card_name")
    number = fields.get("number")
    if not number or not NUMBER_VALID_RE.match(str(number).strip()):
        missing.append("number")
    return len(missing) == 0, missing


def _download(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers=UA_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _call_local_ocr_on_bytes(img_bytes: bytes) -> tuple[str, int]:
    """POST image bytes as multipart naar de GOT-endpoint. Return (text, latency_ms)."""
    import uuid
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="crop.jpg"\r\n'
        f"Content-Type: image/jpeg\r\n\r\n"
    ).encode() + img_bytes + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        LOCAL_OCR_URL,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=LOCAL_OCR_TIMEOUT) as resp:
        import json as _json
        d = _json.loads(resp.read())
    return d.get("text", ""), int((time.time() - t0) * 1000)


def ocr_and_parse_hybrid(
    photo_url: str,
    title_jp: str | None = None,
    title_en: str | None = None,
) -> dict:
    """Hybride OCR pipeline. Return same shape als vision.ocr_and_parse:
        {"ocr": {"full_text": str, "chars": int, "error": str|None},
         "fields": {...},
         "_route": "skip" | "local" | "vision",
         "_reason": str,
         "_latency_ms": int}
    """
    t0_total = time.time()

    # Stap 1: multi-slab skip
    is_lot, pat = is_multi_slab_lot(title_jp, title_en)
    if is_lot:
        empty_fields = {"cert": None, "grade": None, "grade_text": None,
                        "card_name": None, "set_name": None, "number": None,
                        "year": None, "found_psa": False}
        return {
            "ocr": {"full_text": "", "chars": 0, "error": "skipped_multi_slab_lot"},
            "fields": empty_fields,
            "_route": "skip",
            "_reason": f"multi_slab_lot_pattern:{pat}",
            "_latency_ms": int((time.time() - t0_total) * 1000),
        }

    # Stap 2: download foto
    try:
        img_bytes = _download(photo_url)
    except Exception as e:
        # Als download faalt, delegate naar Vision (die downloadt zelf)
        result = vision_ocr_and_parse(photo_url)
        result["_route"] = "vision"
        result["_reason"] = f"download_fail_locally:{type(e).__name__}"
        result["_latency_ms"] = int((time.time() - t0_total) * 1000)
        return result

    # Stap 3: OpenCV crop op de foto
    try:
        crop_bytes, bbox = crop_from_bytes(img_bytes)
    except Exception as e:
        crop_bytes, bbox = None, None

    if crop_bytes is None:
        # Crop faalde → val direct terug op Vision met de VOLLE foto
        result = vision_ocr_and_parse(photo_url)
        result["_route"] = "vision"
        result["_reason"] = "no_red_label_crop"
        result["_latency_ms"] = int((time.time() - t0_total) * 1000)
        return result

    # Stap 4: lokale OCR op de crop
    try:
        text, local_lat = _call_local_ocr_on_bytes(crop_bytes)
    except Exception as e:
        # Local OCR down/fout → Vision fallback op volle foto
        result = vision_ocr_and_parse(photo_url)
        result["_route"] = "vision"
        result["_reason"] = f"local_ocr_fail:{type(e).__name__}"
        result["_latency_ms"] = int((time.time() - t0_total) * 1000)
        return result

    # Stap 5: normaliseer GOT-output (splits op key-patronen) + parse + validate
    normalized = _normalize_local_ocr_text(text)
    fields = parse_psa_fields(normalized)
    valid, missing = _validate_fields(fields)

    if valid:
        return {
            "ocr": {"full_text": text, "chars": len(text), "error": None},
            "fields": fields,
            "_route": "local",
            "_reason": "ok",
            "_latency_ms": int((time.time() - t0_total) * 1000),
            "_crop_bbox": list(bbox) if bbox else None,
        }

    # Fields incompleet/onbetrouwbaar → Vision fallback op volle foto
    result = vision_ocr_and_parse(photo_url)
    result["_route"] = "vision"
    result["_reason"] = f"local_invalid_missing:{','.join(missing)}"
    result["_latency_ms"] = int((time.time() - t0_total) * 1000)
    return result


if __name__ == "__main__":
    import json
    if len(sys.argv) < 2:
        url = "https://static.mercdn.net/item/detail/orig/photos/m27984770699_1.jpg"
        title = "PSA10 リザードンEX RR 1ED 055/080 ポケカ"
    else:
        url = sys.argv[1]
        title = sys.argv[2] if len(sys.argv) > 2 else ""
    r = ocr_and_parse_hybrid(url, title_jp=title)
    print(json.dumps({
        "route": r["_route"], "reason": r["_reason"],
        "latency_ms": r["_latency_ms"],
        "fields": r["fields"],
        "ocr_chars": r["ocr"].get("chars", 0),
    }, indent=2, ensure_ascii=False))
