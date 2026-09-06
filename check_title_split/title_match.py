"""Kensa check_title v2 — CoC-gesplitste implementatie.

Semantiek IDENTIEK aan check_title.py:check_title (regel 68, CoC 43).
Doel: elke sub-functie CoC < 15, orkestrator CoC < 10.
Wordt aliased vanaf check_title.py na verifier + replay green.
"""
from __future__ import annotations

import re

from check_title import (
    _extract_slab_pokemon_en,
    _grade_in_title,
    _pokedex,
    _pokemon_in_text,
)


def _title_only_fallback(title_jp: str, title_en: str) -> dict:
    """Slab niet leesbaar → probeer titel-only match. Kern: grade + pokemon-naam."""
    combined_grade = (title_jp or "") + " " + (title_en or "")
    combined_pk = (title_en or "") + " " + (title_jp or "")
    title_grade = _grade_in_title(combined_grade)
    title_pokemon = _pokemon_in_text(combined_pk)
    if title_grade and title_pokemon:
        msg = f"titel heeft grade PSA{title_grade} + pokemon-naam '{title_pokemon}' (zonder slab-anker)"
        return {"status": "pass", "matches": [msg], "mismatches": [],
                "reasons": ["slab niet leesbaar — check gedaan op interne titel-consistentie"]}
    return {"status": "skip", "matches": [], "mismatches": [],
            "reasons": ["slab niet leesbaar én titel mist grade/naam om mee te vergelijken"]}


def _find_jp_name_for_en(slab_en: str) -> str | None:
    """Zoek de JP-vorm van een EN pokemon-naam in de pokedex (case-insensitive)."""
    slab_low = slab_en.lower()
    for jp, en in _pokedex().items():
        if en.lower() == slab_low:
            return jp
    return None


def _match_pokemon_name(slab_en: str, title_jp: str, title_en: str) -> tuple[list[str], list[str]]:
    """Return (matches, mismatches) voor de pokemon-naam-check."""
    if not slab_en:
        return ([], [])
    title_haystack = ((title_en or "") + " " + (title_jp or "")).lower()
    if slab_en in title_haystack:
        return ([f"pokemon-naam '{slab_en}' zit in titel"], [])
    jp_name = _find_jp_name_for_en(slab_en)
    if jp_name and title_jp and jp_name in title_jp:
        return ([f"pokemon-naam (JP: {jp_name}) zit in titel"], [])
    return ([], [f"pokemon-naam '{slab_en}' NIET in titel"])


def _match_grade(slab_grade, title_jp: str, title_en: str) -> tuple[list[str], list[str], list[str]]:
    """Return (matches, mismatches, reasons) voor de grade-check."""
    title_grade = _grade_in_title((title_jp or "") + " " + (title_en or ""))
    if not title_grade:
        return ([], [], ["titel noemt geen grade"])
    if not slab_grade:
        return ([], [], [f"titel zegt PSA{title_grade}, geen slab-grade ter vergelijking"])
    if title_grade == slab_grade:
        return ([f"grade PSA{slab_grade} matcht slab"], [], [])
    return ([], [f"titel zegt PSA{title_grade} maar slab = PSA{slab_grade}"], [])


def _match_number(slab_number, title_jp: str, title_en: str) -> list[str]:
    """Return matches lijst (0 of 1 entry) voor de nummer-bonus-check."""
    if not slab_number:
        return []
    number_head = slab_number.split("/")[0]
    haystack = (title_jp or "") + " " + (title_en or "")
    if re.search(re.escape(number_head), haystack):
        return [f"kaartnummer {slab_number} zit in titel"]
    return []


def _finalize_status(matches: list[str], mismatches: list[str]) -> str:
    """Bereken eind-status uit matches/mismatches."""
    if mismatches:
        return "fail"
    if matches:
        return "pass"
    return "skip"


def check_title_v2(slab: dict, title_jp: str, title_en: str) -> dict:
    """slab = result of check_slab. Returns {status, matches, mismatches, reasons}.

    IDENTIEK aan check_title.py:check_title — CoC 43 → orkestrator CoC <10 + 5 kleine helpers.
    """
    if slab.get("status") != "pass" or not slab.get("card_name"):
        return _title_only_fallback(title_jp, title_en)

    matches: list[str] = []
    mismatches: list[str] = []
    reasons: list[str] = []

    slab_en = _extract_slab_pokemon_en(slab.get("card_name", ""))
    pm_matches, pm_mismatches = _match_pokemon_name(slab_en, title_jp, title_en)
    matches.extend(pm_matches)
    mismatches.extend(pm_mismatches)

    gr_matches, gr_mismatches, gr_reasons = _match_grade(slab.get("grade"), title_jp, title_en)
    matches.extend(gr_matches)
    mismatches.extend(gr_mismatches)
    reasons.extend(gr_reasons)

    matches.extend(_match_number(slab.get("number"), title_jp, title_en))

    return {"status": _finalize_status(matches, mismatches),
            "matches": matches, "mismatches": mismatches, "reasons": reasons}
