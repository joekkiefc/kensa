#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa — Google Vision OCR client + PSA label parser.

Vision TEXT_DETECTION on a photo URL, then regex-parse the PSA slab label
into structured fields: cert#, grade, card name, set, number, year.
"""

import base64
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Optional
# Refactored PSA-fields parser — CC 42 → max 16 per sub-functie.
# Zie ook alias-override aan het EIND van deze file zodat externe modules
# (ocr_router, llm_analyze, shadow_test_easyocr) via `from vision import parse_psa_fields`
# automatisch de v2-implementatie krijgen zonder hun code te wijzigen.
from vision_split.psa_parser import parse_psa_fields_v2 as _parse_psa_fields_new

SCRIPT_DIR = Path(__file__).resolve().parent
SECRETS_PATH = Path("/home/pi/.openclaw/secrets.json")
POKEDEX_PATH = SCRIPT_DIR / "pokedex_ja_en.json"

VISION_URL = "https://vision.googleapis.com/v1/images:annotate?key={key}"

_POKEDEX_CACHE: dict | None = None
_POKEMON_EN_SET: set[str] | None = None


def _pokedex() -> dict:
    global _POKEDEX_CACHE
    if _POKEDEX_CACHE is None:
        _POKEDEX_CACHE = json.loads(POKEDEX_PATH.read_text())
    return _POKEDEX_CACHE


def _pokemon_en_set() -> set[str]:
    global _POKEMON_EN_SET
    if _POKEMON_EN_SET is None:
        _POKEMON_EN_SET = {en.upper() for en in _pokedex().values() if len(en) >= 3}
    return _POKEMON_EN_SET


SUBTYPE_RE = re.compile(r"\b(VMAX|VSTAR|GX|EX|V|SAR|SR|UR|HR|AR|CHR)\b")


# Regels die op de PSA-label kop staan maar géén card-name zijn.
# Skip-patronen: jaar/set, grade, cert, nummer, keywords.
_LABEL_SKIP_KEYWORDS = {
    "PSA", "POKEMON", "JAPANESE", "GEM", "MT", "MINT", "NM", "EX-MT", "EX", "VG-EX",
    "VG", "GOOD", "FAIR", "POOR", "AUTHENTIC", "GRADE",
}


def _is_metadata_line(line: str) -> bool:
    """True als deze regel PSA-metadata is (jaar/set/grade/cert/nummer) en dus GEEN card-name."""
    s = line.strip()
    if not s:
        return True
    u = s.upper()
    # Skip jaar-regels ("2024 POKEMON SV8a JP")
    if YEAR_RE.search(s):
        return True
    # Skip nummer-regels ("#068" of "068/187")
    if s.startswith("#") or re.fullmatch(r"#?\d{1,4}(/[A-Z0-9\-]+)?", u):
        return True
    # Skip cert (8-10 digits)
    if re.fullmatch(r"\d{8,10}", s):
        return True
    # Skip losse grade-nummer ("10", "9.5")
    if re.fullmatch(r"10|9\.5|9|8\.5|8|7|6|5|4|3|2|1", s):
        return True
    # Skip grade-text
    if GRADE_RE.fullmatch(u):
        return True
    # Skip als alle tokens metadata-keywords zijn
    tokens = re.findall(r"[A-Z]+", u)
    if tokens and all(t in _LABEL_SKIP_KEYWORDS for t in tokens):
        return True
    return False


def _find_label_name(head: list[str]) -> tuple[str | None, int | None]:
    """Positional card-name detectie: eerste ALL-CAPS non-metadata regel op de PSA-label.

    Werkt voor Pokemon ('SYLVEON', 'CHARIZARD VMAX') én non-Pokemon
    (trainers 'IONO', supporters 'PROFESSOR JUNIPER', items etc.) —
    op de PSA-label staat de naam altijd in Engels op regel 1-2.
    """
    for i, ln in enumerate(head[:10]):
        if _is_metadata_line(ln):
            continue
        # Geen Japanse/CJK tekens — PSA labels zijn Engels
        if any(ord(c) > 127 for c in ln):
            continue
        u = ln.strip().upper()
        letters = re.findall(r"[A-Z]", u)
        # Minstens 3 letters, geen puur getal, geen slash+cijfer
        if len(letters) < 3:
            continue
        return (ln.strip(), i)
    return (None, None)


def _api_key() -> str:
    return json.loads(SECRETS_PATH.read_text())["google"]["api_key"]


def ocr_image(source: str, timeout: int = 30) -> dict:
    """Run Vision TEXT_DETECTION. `source` = http(s) URL or local path.
    Returns {full_text, chars, error?}.
    """
    if source.startswith(("http://", "https://")):
        image_field = {"source": {"imageUri": source}}
    else:
        data = Path(source).read_bytes()
        image_field = {"content": base64.b64encode(data).decode()}

    body = {"requests": [{"image": image_field, "features": [{"type": "TEXT_DETECTION", "maxResults": 1}]}]}
    req = urllib.request.Request(
        VISION_URL.format(key=_api_key()),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"full_text": "", "chars": 0, "error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"full_text": "", "chars": 0, "error": f"{type(e).__name__}: {e}"}

    resp0 = (data.get("responses") or [{}])[0]
    if "error" in resp0:
        return {"full_text": "", "chars": 0, "error": resp0["error"].get("message", "unknown")}
    text = resp0.get("fullTextAnnotation", {}).get("text", "")
    return {"full_text": text, "chars": len(text)}


CERT_RE = re.compile(r"(?<!\d)(\d{8,10})(?!\d)")
GRADE_RE = re.compile(
    r"\b(GEM\s*MT|GEM\s*MINT|MINT|NM-?MT|NM|EX-?MT|EX|VG-?EX|VG|GOOD|FAIR|POOR|AUTHENTIC)\b",
    re.IGNORECASE,
)
GRADE_NUM_RE = re.compile(r"(?<!\d)(10|9\.5|9|8\.5|8|7|6|5|4|3|2|1)(?!\d)")
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
NUMBER_WITH_SET_RE = re.compile(r"\b(\d{1,4}/[A-Z0-9\-]+)\b")
NUMBER_RE = re.compile(r"#\s*(\d{1,4}(?:/[A-Z0-9\-]+)?)")
PROMO_RE = re.compile(r"\b(\d{1,4}/S-?P|\d{1,4}/SV-?P|PROMO)\b", re.IGNORECASE)


# NB: `parse_psa_fields` is verhuisd naar `vision_split/psa_parser.py`.
#     De oude def (CC 42) is verwijderd na refactor 2026-09-01.
#     Backup: agents/kensa/_legacy_pre_refactor/vision.py.20260901
#     De naam wordt aan het EIND van deze file gealiast (zoek `_parse_psa_fields_new`).


def ocr_and_parse(source: str) -> dict:
    """Convenience: OCR + parse in one call. Returns both."""
    ocr = ocr_image(source)
    fields = parse_psa_fields(ocr.get("full_text", ""))
    return {"ocr": ocr, "fields": fields}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        url = "https://static.mercdn.net/item/detail/orig/photos/m10228250509_1.jpg"
        print(f"[test] Espeon VMAX foto 1: {url}")
    else:
        url = sys.argv[1]

    r = ocr_and_parse(url)
    ocr = r["ocr"]
    if ocr.get("error"):
        print(f"OCR ERROR: {ocr['error']}")
        sys.exit(1)
    print(f"OCR: {ocr['chars']} chars")
    print("\n--- PARSED PSA FIELDS ---")
    print(json.dumps(r["fields"], ensure_ascii=False, indent=2))
    print("\n--- OCR HEAD (20 lines) ---")
    for ln in ocr["full_text"].splitlines()[:20]:
        print(f"  {ln}")


# ---------------------------------------------------------------------------
# Refactor switch (2026-09-01): route `parse_psa_fields` naar de v2 in
# vision_split/psa_parser.py. De originele def hierboven (regel 148) blijft
# in de file staan voor snelle rollback (verwijder de regel eronder).
# Externe modules (ocr_router.py, llm_analyze.py, shadow_test_easyocr.py)
# blijven ongewijzigd — hun `from vision import parse_psa_fields` krijgt
# automatisch de v2-implementatie.
# ---------------------------------------------------------------------------
parse_psa_fields = _parse_psa_fields_new
