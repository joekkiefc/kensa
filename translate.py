#!/usr/bin/env python3
"""Kensa translate — free unofficial Google Translate endpoint (JP → EN).

Pipeline per title:
  1. Replace known Pokémon names (JP → official EN) using pokedex_ja_en.json.
     Google's phonetic transliteration ('エーフィ' → 'Efi') is wrong for
     Pokémon; the official names ('Espeon') are the ones cards use.
  2. Pass the result through Google Translate for anything left (adjectives,
     card-set jargon, item conditions, etc.).

No API key needed. Rate-limit is loose; keep call volume modest.
Returns None on failure so callers can fall back to the JP original.
"""

import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ENDPOINT = "https://translate.googleapis.com/translate_a/single"
UA = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
POKEDEX_PATH = Path(__file__).resolve().parent / "pokedex_ja_en.json"

_POKEDEX_CACHE: list[tuple[str, str]] | None = None


def _load_pokedex() -> list[tuple[str, str]]:
    """Load JP→EN Pokémon dictionary sorted by JP length DESC.
    Longest-first prevents 'エーフィV' from matching 'エーフィ' before 'エーフィV' has a chance."""
    global _POKEDEX_CACHE
    if _POKEDEX_CACHE is not None:
        return _POKEDEX_CACHE
    if not POKEDEX_PATH.exists():
        _POKEDEX_CACHE = []
        return _POKEDEX_CACHE
    try:
        data = json.loads(POKEDEX_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"pokedex load failed: {e}", file=sys.stderr)
        _POKEDEX_CACHE = []
        return _POKEDEX_CACHE
    _POKEDEX_CACHE = sorted(data.items(), key=lambda kv: -len(kv[0]))
    return _POKEDEX_CACHE


def replace_pokemon_names(text: str) -> str:
    """Substitute every known JP Pokémon name with its official EN name.
    Latin-alphabet result is left alone by Google Translate afterwards."""
    if not text:
        return text
    for jp, en in _load_pokedex():
        if jp in text:
            # Pad with spaces so 'エーフィVMAX' → 'Espeon VMAX' (Google separates it cleaner)
            text = text.replace(jp, f" {en} ")
    # Collapse repeated whitespace introduced by padding
    return re.sub(r"\s+", " ", text).strip()


def translate(text: str, source: str = "ja", target: str = "en", timeout: int = 10) -> str | None:
    if not text:
        return None
    preprocessed = replace_pokemon_names(text)
    url = ENDPOINT + "?" + urllib.parse.urlencode({
        "client": "gtx", "sl": source, "tl": target, "dt": "t", "q": preprocessed,
    })
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"translate failed: {e}", file=sys.stderr)
        return None
    if not data or not data[0]:
        return None
    result = "".join(seg[0] for seg in data[0] if seg and seg[0]).strip() or None
    return result


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else "【PSA10】エーフィVMAX SA189/S-P プロモ ポケモンカード"
    print("Pre-processed:", replace_pokemon_names(src))
    print("Final EN:     ", translate(src))
