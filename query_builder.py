#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa — bouw eBay zoekquery uit slab-data.

Format: "<pokemon-naam> <kaartvariant> <nummer> [<set-code>] PSA <grade>"

Voorbeelden:
  FA/ESPEON VMAX + 189/S-P + grade 10          → "Espeon VMAX 189 S-P PSA 10"
  FA/PIKACHU VMAX + 123 + PIKAPIKA CAMPAIGN    → "Pikachu VMAX 123 S-P PSA 10"
  SR CHARIZARD VSTAR + 018/172 + year 2023     → "Charizard VSTAR 018 SV1 PSA 10"

Set-code detectie:
  1. Als `number` een suffix bevat ("123/S-P") → gebruik die suffix
  2. Anders: als set_name promo/campaign markers bevat → S-P (year<2023) of SV-P (>=2023)
  3. Anders: skip set-code (huidige gedrag)
"""

import re
import sys

PROMO_MARKERS = ("PROMO", "CAMPAIGN", "CMPGN", "GIVEAWAY", "PIKAPIKA")


def build_query(
    card_name: str | None,
    number: str | None,
    grade: str | None,
    set_name: str | None = None,
    year: int | None = None,
    pokemon_override: str | None = None,
) -> str | None:
    """Als pokemon_override gegeven → gebruik die als naam-hoofdwoord ipv slab OCR
    (nuttig wanneer OCR de naam misread heeft — bv. 'MYLE' voor 'SYLVEON')."""
    if not card_name and not pokemon_override:
        return None
    if pokemon_override:
        name = _override_name(pokemon_override, card_name)
    else:
        name = _clean_name(card_name)
    if not name:
        return None
    parts = [name]
    num_clean = _clean_number(number) if number else None
    if num_clean:
        parts.append(num_clean)
    set_code = _detect_set_code(number, set_name, year)
    if set_code:
        parts.append(set_code)
    parts.append(f"PSA {grade}" if grade else "PSA 10")
    return " ".join(parts)


def _override_name(pokemon: str, card_name: str | None) -> str:
    """Pokemon-naam uit titel + eventueel subtype uit slab (VMAX/VSTAR/GX/EX/V/SAR)."""
    base = pokemon.strip().capitalize()
    subtype = None
    if card_name:
        for m in re.finditer(r"\b(VMAX|VSTAR|GX|EX|V|SAR|SR|UR|HR|AR|CHR)\b", card_name.upper()):
            subtype = m.group(1)
            break
    return f"{base} {subtype}" if subtype else base


def _clean_name(card_name: str) -> str:
    """FA/ESPEON VMAX → Espeon VMAX. Behoud subtype (VMAX/VSTAR/GX/EX/V/SAR/…)."""
    s = re.sub(r"^(FA|SA|RR|SR|SAR|UR|HR|CHR|AR)[/\s]+", "", card_name, flags=re.IGNORECASE)
    tokens = re.split(r"[\s/]+", s.strip())
    keep = []
    for t in tokens:
        u = t.upper()
        if not t or u in {"FA", "SA", "RR"}:
            continue
        if u in {"VMAX", "VSTAR", "GX", "EX", "V", "SAR", "SR", "UR", "HR", "AR", "CHR"}:
            keep.append(u)
        elif re.fullmatch(r"[A-Za-z]+", t) and len(t) >= 3:
            keep.append(t.capitalize())
    return " ".join(keep)


def _clean_number(number: str) -> str | None:
    """189/S-P → 189. 123/SP → 123. #189 → 189."""
    m = re.search(r"(\d{1,4})", number.split("/")[0].lstrip("#"))
    return m.group(1) if m else None


def _detect_set_code(number: str | None, set_name: str | None, year: int | None) -> str | None:
    """Return set-code like 'S-P', 'SV-P', 'S6a' if detectable, else None."""
    if number and "/" in number:
        suffix = number.split("/", 1)[1].strip().upper()
        suffix = re.sub(r"[^A-Z0-9\-]", "", suffix)
        if suffix and not suffix.isdigit():
            return _normalize_set_code(suffix)
    if set_name:
        s_upper = set_name.upper()
        if any(m in s_upper for m in PROMO_MARKERS):
            if year is not None and year >= 2023:
                return "SV-P"
            return "S-P"
    return None


def _normalize_set_code(code: str) -> str:
    """Standardize known variants: SP → S-P, SVP → SV-P."""
    if code in {"SP", "S-P"}:
        return "S-P"
    if code in {"SVP", "SV-P"}:
        return "SV-P"
    if code in {"SMP", "SM-P"}:
        return "SM-P"
    if code in {"XYP", "XY-P"}:
        return "XY-P"
    return code


if __name__ == "__main__":
    tests = [
        ("FA/ESPEON VMAX",  "189/S-P",              "10", None,                             None),
        ("FA/PIKACHU VMAX", "123",                  "10", "PIKAPIKA! PIKACHU! CMPGN.",     2021),
        ("FA/PIKACHU VMAX", "006",                  "10", "25TH ANNIV-GOLDEN BOX",         2021),
        ("SR CHARIZARD VSTAR", "018/172",           "9",  None,                             None),
        ("FA/HOOPA",         "155",                 "10", "ARCHDJINNI/RINGS GIVEAWAY",     2016),
        ("FA/RAYQUAZA V",    "236/SV-P",            "10", None,                             2024),
        ("MEWTWO GX",        "31",                  None, None,                             None),
        ("",                 None,                  "10", None,                             None),
    ]
    for name, num, grade, sn, yr in tests:
        q = build_query(name, num, grade, sn, yr)
        print(f"  {name!r:22} + {num!r:12} + set={sn!r:34} year={yr} → {q!r}")
