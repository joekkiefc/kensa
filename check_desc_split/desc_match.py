"""Kensa check_desc v2 — CoC-gesplitste implementatie.

Semantiek IDENTIEK aan check_desc.py:check_desc (regel 57, CoC 27).
Sub-functies < 10 CoC, orkestrator < 10.
"""
from __future__ import annotations

import json
from pathlib import Path

from check_desc import (
    _grade_in_desc,
    _title_grade_from_check,
    _title_pokemon_from_check,
)

SCRIPT_DIR = Path(__file__).resolve().parent.parent
_POKEDEX_CACHE: dict | None = None


def _pokedex() -> dict:
    global _POKEDEX_CACHE
    if _POKEDEX_CACHE is None:
        _POKEDEX_CACHE = json.loads((SCRIPT_DIR / "pokedex_ja_en.json").read_text())
    return _POKEDEX_CACHE


def _compare_grade(desc_grade: str | None, slab_grade: str | None,
                   title_grade: str | None) -> tuple[list[str], list[str], list[str]]:
    """Return (matches, mismatches, reasons) voor de grade-vergelijking."""
    if not desc_grade:
        return ([], [], [])
    if slab_grade:
        if desc_grade == slab_grade:
            return ([f"beschrijving noemt PSA{desc_grade} = slab-grade"], [], [])
        return ([], [f"beschrijving zegt PSA{desc_grade} maar slab = PSA{slab_grade}"], [])
    if title_grade:
        if desc_grade == title_grade:
            return ([f"beschrijving noemt PSA{desc_grade} = titel-grade"], [], [])
        return ([], [f"beschrijving zegt PSA{desc_grade} maar titel zegt PSA{title_grade}"], [])
    return ([], [], [f"beschrijving noemt PSA{desc_grade} (geen slab/titel-grade om mee te vergelijken)"])


def _resolve_target_name(slab: dict, title_check: dict) -> tuple[str | None, str | None]:
    """Return (target_name, source) — source is 'slab' of 'titel' of None."""
    from check_title import _extract_slab_pokemon_en
    slab_name_en = _extract_slab_pokemon_en(slab.get("card_name", "")) if slab.get("card_name") else None
    if slab_name_en:
        return (slab_name_en, "slab")
    title_name = _title_pokemon_from_check(title_check)
    return (title_name, "titel") if title_name else (None, None)


def _match_name_in_desc(description_jp: str, target_name: str, name_source: str) -> list[str]:
    """Kijk of de EN-naam of de JP-vorm in de beschrijving voorkomt."""
    if not target_name:
        return []
    if target_name in description_jp.lower():
        return [f"pokemon-naam '{target_name}' in beschrijving (van {name_source})"]
    jp_name = next((jp for jp, en in _pokedex().items() if en.lower() == target_name), None)
    if jp_name and jp_name in description_jp:
        return [f"pokemon-naam (JP: {jp_name}) in beschrijving (van {name_source})"]
    return []


def _finalize_status(matches: list[str], mismatches: list[str]) -> str:
    if mismatches:
        return "fail"
    if matches:
        return "pass"
    return "skip"


def check_desc_v2(slab: dict, description_jp: str, title_check: dict) -> dict:
    """Zie check_desc.check_desc. Orkestrator: skip-check → grade → naam → status."""
    if not description_jp or not description_jp.strip():
        return {"status": "skip", "matches": [], "mismatches": [], "red_flags": [],
                "reasons": ["geen seller-beschrijving beschikbaar"]}

    desc_grade = _grade_in_desc(description_jp)
    grade_matches, grade_mismatches, grade_reasons = _compare_grade(
        desc_grade, slab.get("grade"), _title_grade_from_check(title_check),
    )

    target_name, name_source = _resolve_target_name(slab, title_check)
    name_matches = _match_name_in_desc(description_jp, target_name, name_source or "") if target_name else []

    matches = grade_matches + name_matches
    mismatches = grade_mismatches
    return {"status": _finalize_status(matches, mismatches),
            "matches": matches, "mismatches": mismatches,
            "red_flags": [], "reasons": grade_reasons}
