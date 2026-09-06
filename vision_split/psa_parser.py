"""psa_parser.py — refactor-split van `parse_psa_fields` uit `../vision.py`.

Vijf sub-functies (één per baseline-fase F1–F5) plus een orkestrator
`parse_psa_fields_v2` die exact hetzelfde gedrag oplevert als `parse_psa_fields`
(zelfde return-shape met 8 keys, zelfde branch-volgorde, zelfde regex-matches).

Regex-constants en helpers (`_is_metadata_line`, `_find_label_name`) zijn
LOSSTAAND gekopieerd i.p.v. geïmporteerd uit vision.py — externe callers
(`ocr_router.py`, `llm_analyze.py`, `shadow_test_easyocr.py`) importeren
`CERT_RE`/`GRADE_NUM_RE`/`parse_psa_fields` uit `vision`; die re-export
gebeurt LATER in Fase 3-A.

Zie `agents/kensa/analyze_refactor_fixtures_psa/BASELINE_ANALYSIS.md` voor
de F1–F5 fase-split en volledige branch-inventaris.
"""

import re
from typing import Optional


# ---------------------------------------------------------------------------
# Regex-constants (gekopieerd uit vision.py — identiek patroon + flags)
# ---------------------------------------------------------------------------

SUBTYPE_RE = re.compile(r"\b(VMAX|VSTAR|GX|EX|V|SAR|SR|UR|HR|AR|CHR)\b")

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

# Regels die op de PSA-label kop staan maar géén card-name zijn.
_LABEL_SKIP_KEYWORDS = {
    "PSA", "POKEMON", "JAPANESE", "GEM", "MT", "MINT", "NM", "EX-MT", "EX", "VG-EX",
    "VG", "GOOD", "FAIR", "POOR", "AUTHENTIC", "GRADE",
}


# ---------------------------------------------------------------------------
# Helpers (gekopieerd uit vision.py — module-level voor externe re-export)
# ---------------------------------------------------------------------------


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


def _find_label_name(head: list[str]) -> tuple[Optional[str], Optional[int]]:
    """Positional card-name detectie: eerste ALL-CAPS non-metadata regel op de PSA-label.

    Werkt voor Pokemon ('SYLVEON', 'CHARIZARD VMAX') én non-Pokemon
    (trainers 'IONO', supporters 'PROFESSOR JUNIPER', items etc.).
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


# ---------------------------------------------------------------------------
# Sub-functies (F1–F5) — één per baseline-fase
# ---------------------------------------------------------------------------


def _psa_normalize(text: str) -> Optional[tuple[list[str], list[str], str]]:
    """F1 — Normalize.

    Early-return None bij lege input; anders `(lines, head, label_text)`.
    label_text = head[:8] joined — bewust beperkt tot label-kop want
    kaart-body-text onder de label geeft vals-positieve nummers.
    """
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    head = lines[:20]
    label_text = "\n".join(head[:8])
    return lines, head, label_text


def _psa_simple_fields(head_text: str, head: list[str]) -> dict:
    """F2 — Simple field extraction.

    One-shot regex over head/label text voor `found_psa`, `year`, `number`,
    `grade_text` — in exact dezelfde volgorde als origineel (regel 166-177).
    """
    label_text = "\n".join(head[:8])

    found_psa = bool(re.search(r"\bP[SA]{1,2}\b", head_text)) or bool(GRADE_RE.search(head_text))

    year_m = YEAR_RE.search(head_text)
    year = int(year_m.group(0)) if year_m else None

    # Prefer number-with-set-suffix ("123/S-P") over bare "#123" so we keep the set code.
    number_m = NUMBER_WITH_SET_RE.search(label_text) or NUMBER_RE.search(label_text) or PROMO_RE.search(label_text)
    number = number_m.group(1) if number_m and number_m.groups() else (number_m.group(0) if number_m else None)

    grade_text_m = GRADE_RE.search(head_text)
    grade_text = grade_text_m.group(1).upper().replace("  ", " ") if grade_text_m else None

    return {
        "found_psa": found_psa,
        "year": year,
        "number": number,
        "grade_text": grade_text,
    }


def _psa_grade_number(head: list[str], grade_text: Optional[str]) -> Optional[str]:
    """F3 — Grade number extraction.

    Primair: eerste head-regel die volledig een grade-cijfer is (fullmatch).
    Fallback: vind eerste GRADE_RE-hit regel, kijk 3 regels vooruit voor
    een grade-cijfer via `search` (niet fullmatch).

    Caller-verantwoordelijk: alleen aanroepen als `found_psa=True` (originele
    gate op regel 180 — anders pikt hij random cijfers op uit non-PSA-fotos).
    """
    for ln in head:
        if GRADE_NUM_RE.fullmatch(ln):
            return ln
    if not grade_text:
        return None
    idx = None
    for i, ln in enumerate(head):
        if GRADE_RE.search(ln):
            idx = i
            break
    if idx is None:
        return None
    for ln in head[idx + 1:idx + 4]:
        m = GRADE_NUM_RE.search(ln)
        if m:
            return m.group(1)
    return None


def _psa_cert(head: list[str]) -> Optional[str]:
    """F4 — Cert extraction.

    Primair: reverse-loop met `fullmatch` (cert staat onderaan de label als
    losse regel). Fallback: forward-loop met `search` voor gecombineerde
    regels als "PSA 151414658".
    """
    for ln in reversed(head):
        m = CERT_RE.fullmatch(ln)
        if m:
            return m.group(1)
    for ln in head:
        m = CERT_RE.search(ln)
        if m:
            return m.group(1)
    return None


# `_psa_name_and_set` verhuisd naar _split/ (v2). Verwijderd 2026-09-01 (regel 2 cleanup).
# Backup: _legacy_pre_refactor/... — alias-override staat lager in de file.



# ---------------------------------------------------------------------------
# Orkestrator — identieke signature + output-shape als vision.parse_psa_fields
# ---------------------------------------------------------------------------


def parse_psa_fields_v2(text: str) -> dict:
    """Parse PSA slab label OCR into structured fields.

    Identiek in signature en output-shape (8 keys) aan `vision.parse_psa_fields`.
    Zie `agents/kensa/analyze_refactor_fixtures_psa/BASELINE_ANALYSIS.md`.
    """
    normalized = _psa_normalize(text)
    if normalized is None:
        return {"cert": None, "grade": None, "grade_text": None, "card_name": None,
                "set_name": None, "number": None, "year": None, "found_psa": False}
    _lines, head, _label_text = normalized
    head_text = "\n".join(head)

    simple = _psa_simple_fields(head_text, head)
    found_psa = simple["found_psa"]
    year = simple["year"]
    number = simple["number"]
    grade_text = simple["grade_text"]

    grade = _psa_grade_number(head, grade_text) if found_psa else None
    cert = _psa_cert(head)
    card_name, set_name = _psa_name_and_set(head)

    return {
        "cert": cert,
        "grade": grade,
        "grade_text": grade_text,
        "card_name": card_name,
        "set_name": set_name,
        "number": number,
        "year": year,
        "found_psa": found_psa,
    }


# Refactor switch (2026-09-01, regel 2 #7): _psa_name_and_set -> v2.
# Backup: _legacy_pre_refactor/psa_parser.py.20260901-regel2-func7
from vision_split.psa_name_set_split import _psa_name_and_set_v2 as _psa_name_and_set_new  # noqa: E402
_psa_name_and_set = _psa_name_and_set_new
