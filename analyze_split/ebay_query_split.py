"""_ebay_build_query v2 — CoC 22 → sub-functies < 10, orkestrator ~ 7."""
from __future__ import annotations

import sys

from check_title import _extract_slab_pokemon_en, _pokemon_in_text
from ebay_filter import query_is_specific
from query_builder import build_query


def _resolve_pokemon_override(slab: dict, listing: dict, verbose: bool) -> str | None:
    """Als slab-naam niet in pokedex bekend, probeer uit titel."""
    from analyze_split.ebay_phase import _pokemon_en_lookup
    slab_pokemon = _extract_slab_pokemon_en(slab.get("card_name") or "")
    slab_is_known = bool(slab_pokemon and slab_pokemon in _pokemon_en_lookup())
    if slab_is_known:
        return None
    title_haystack = (listing.get("title_en") or "") + " " + (listing.get("title_jp") or "")
    title_pokemon = _pokemon_in_text(title_haystack)
    if title_pokemon and verbose:
        print(
            f"  [ebay] slab-naam {slab_pokemon!r} niet in pokedex → override uit titel: {title_pokemon!r}",
            file=sys.stderr,
        )
    return title_pokemon


def _pick_llm_query(llm_data: dict | None, verbose: bool) -> tuple[str | None, str]:
    """(query, source). source is altijd 'llm' of 'regex'."""
    if llm_data and llm_data.get("ebay_query"):
        query = str(llm_data["ebay_query"]).strip()
        if verbose:
            print(f"  [ebay] query uit LLM: {query!r}", file=sys.stderr)
        return (query, "llm")
    return (None, "regex")


def _build_regex_query(slab: dict, pokemon_override: str | None) -> str | None:
    """Regex-pad. Return None als grade ontbreekt of build faalt."""
    slab_grade = slab.get("grade")
    if not slab_grade or not str(slab_grade).strip():
        return None
    return build_query(
        slab.get("card_name"),
        slab.get("number"),
        str(slab_grade).strip(),
        set_name=slab.get("set_name"),
        year=slab.get("year"),
        pokemon_override=pokemon_override,
    )


def _ebay_build_query_v2(
    slab: dict,
    listing: dict,
    llm_data: dict | None,
    verbose: bool,
) -> tuple[str | None, str, dict | None]:
    """Zie analyze_split.ebay_phase._ebay_build_query."""
    pokemon_override = _resolve_pokemon_override(slab, listing, verbose)
    query, query_source = _pick_llm_query(llm_data, verbose)

    if not query:
        slab_grade = slab.get("grade")
        if not slab_grade or not str(slab_grade).strip():
            return (None, query_source, {"query": None, "skipped": "grade onbekend (geen default naar 10)"})
        query = _build_regex_query(slab, pokemon_override)

    if not query:
        return (None, query_source, {"query": None, "skipped": "query kon niet gebouwd worden"})

    gate_ok, gate_why = query_is_specific(query)
    if not gate_ok:
        if verbose:
            print(f"  [ebay] gate skip: {query!r} — {gate_why}", file=sys.stderr)
        return (query, query_source, {"query": query, "skipped": f"query te vaag: {gate_why}"})

    return (query, query_source, None)
