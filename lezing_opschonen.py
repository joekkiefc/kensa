"""Kensa — Qwen-lezing opschonen naar de velden die de rest van Kensa verwacht.

Qwen kopieert het PSA-label letterlijk ('FA/MEW V', 'VENUSAUR-HOLO', 'Kairiki',
nummer '#074/187', set_code '2023 POKEMON SV...'). De rest van Kensa (card_key,
eBay-zoekzin, Cardmarket-zoeker) is gebouwd op Gemini-achtige velden: een nette
Engelse naam, nummer zonder '#', alleen een échte set-code.

Dit is exact de trechter die in het test-harnas gemeten is (dev/qwen_test60.py,
uitkomst5) — live doet dus wat er getest is:
  - fix 4: pokemon-woord uit naam/label-tokens tegen de pokédex (prefix Mega/Dark/… blijft)
  - JP→EN vangnet (jp_naam_vangnet): katakana/romaji → officiële Engelse naam
  - fix 1: échte set-code als nummer-suffix ('218/SV-P'), rommel eruit

Geen netwerk, geen state. Pure functies, dus makkelijk te testen.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from jp_naam_vangnet import naar_engels
from supabase_client import geldige_setcode

_KENSA = Path(__file__).resolve().parent
POKEDEX_EN = {v.lower() for v in json.load(open(_KENSA / "pokedex_ja_en.json")).values() if len(v) >= 3}
PREFIX = {"mega", "dark", "light", "shining", "radiant", "galarian", "alolan", "hisuian", "paldean"}
SUBTYPES = ["VMAX", "VSTAR", "GX", "EX", "V", "BREAK"]


def pokemon_uit(*teksten) -> str | None:
    """Eerste pokédex-woord (≥3 letters) uit de teksten, op volgorde; prefix ervoor blijft."""
    for t in teksten:
        toks = [x.lower() for x in re.split(r"[^A-Za-z]+", t or "") if x]
        for i, tok in enumerate(toks):
            if tok in POKEDEX_EN and len(tok) >= 3:
                if i > 0 and toks[i - 1] in PREFIX:
                    return f"{toks[i - 1]} {tok}"
                return tok
    return None


def subtype_uit(*teksten) -> str | None:
    for t in teksten:
        up = (t or "").upper()
        for s in SUBTYPES:
            if re.search(rf"\b{s}\b", up):
                return s
    return None


def schone_naam(pokemon: str | None, subtype: str | None) -> str | None:
    if not pokemon:
        return None
    naam = " ".join(w.capitalize() for w in pokemon.split())
    return f"{naam} {subtype}" if subtype else naam


def schoon_nummer(number, set_code) -> str:
    """Cijfers + alleen een ÉCHTE set-code als suffix. '' als er geen cijfers zijn."""
    raw = str(number or "").strip().lstrip("#")
    m = re.match(r"\d{1,4}", raw.split("/")[0])
    if not m:
        return ""
    num = m.group(0)
    suffix = raw.split("/", 1)[1] if "/" in raw else ""
    code = geldige_setcode(suffix) or geldige_setcode(set_code)
    return f"{num}/{code}" if code else num


def opschonen(lezing: dict) -> dict:
    """Ruwe Qwen-lezing → zelfde dict, met nette 'name'/'number'/'set_code' + 'name_raw'/'pokemon'."""
    if not lezing or lezing.get("_error"):
        return lezing
    name_raw = lezing.get("name")
    label = lezing.get("label_name")
    pokemon = pokemon_uit(name_raw, label)
    reden = "pokedex"
    if not pokemon:                                   # vangnet: Japans/romaji → officiële Engelse naam
        en, reden = naar_engels(name_raw)
        if not en:
            en, reden = naar_engels(label)
        pokemon = en.lower() if en else None
    subtype = subtype_uit(name_raw, label)
    naam = schone_naam(pokemon, subtype)
    set_code = geldige_setcode(lezing.get("set_code"))
    nummer = schoon_nummer(lezing.get("number"), set_code)
    out = dict(lezing)
    out["name_raw"] = name_raw
    out["name"] = naam or name_raw            # geen pokémon herkend → ruwe naam, rest van Kensa beslist (LLM-gate)
    out["number"] = nummer or lezing.get("number")
    out["set_code"] = set_code                # '2023 POKEMON SV' / 'CSR' → None; 'SV-P' blijft
    out["pokemon"] = pokemon
    out["subtype"] = subtype
    out["_naam_bron"] = reden if pokemon else "onbekend"
    return out
