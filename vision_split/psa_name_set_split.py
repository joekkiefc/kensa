"""_psa_name_and_set v2 — CoC 22 → sub-functies < 10."""
from __future__ import annotations
from typing import Optional


def _extract_subtype(name_line: str, next_line: str | None):
    """SUBTYPE-suffix uit naam-regel of buurregel."""
    from vision_split.psa_parser import SUBTYPE_RE
    sm = SUBTYPE_RE.search(name_line.upper())
    if not sm and next_line:
        sm = SUBTYPE_RE.search(next_line.upper())
    return sm.group(1) if sm else None


def _compose_card_name(name_line: str, subtype: str | None) -> str:
    base = name_line.strip()
    if subtype and subtype not in base.upper():
        return f"{base} {subtype}"
    return base


def _find_promo_set_marker(head: list[str]) -> Optional[str]:
    promo_marks = ("PROMO", "CAMPAIGN", "CMPGN", "GIVEAWAY", "PIKAPIKA")
    for probe in head[:8]:
        u = probe.upper()
        if any(m in u for m in promo_marks) and "PSA" not in u:
            return probe
    return None


def _find_year_set_headline(head: list[str]) -> Optional[str]:
    from vision_split.psa_parser import YEAR_RE
    for probe in head[:3]:
        u = probe.upper()
        if YEAR_RE.search(probe) and ("POKEMON" in u or "JAPANESE" in u):
            return probe
    return None


def _psa_name_and_set_v2(head: list[str]) -> tuple[Optional[str], Optional[str]]:
    """Zie vision_split.psa_parser._psa_name_and_set."""
    from vision_split.psa_parser import _find_label_name
    card_name = None
    name_line, name_idx = _find_label_name(head)
    if name_line:
        next_line = head[name_idx + 1] if name_idx + 1 < len(head) else None
        subtype = _extract_subtype(name_line, next_line)
        card_name = _compose_card_name(name_line, subtype)

    set_name = _find_promo_set_marker(head) or _find_year_set_headline(head)
    return card_name, set_name
