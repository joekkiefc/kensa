#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa — Check 2: title ↔ slab match.

Compare seller title (JP + EN) against slab-parsed fields.

Match rules:
  - card name: EN pokemon name of slab (from FA/ESPEON VMAX → "espeon")
    must appear in EN title (case-insensitive) OR JP form in JP title
  - grade: if title mentions "PSA10"/"PSA 10"/"psa10", grade in slab must be "10"
  - number: if title contains slab number (e.g. "189"), OK; else neutral (not a mismatch)

status:
  pass  = card name matches AND grade matches AND no explicit mismatches
  fail  = at least one explicit mismatch (bv. seller schrijft "PSA10" maar slab = "9")
  skip  = check1 gaf onvoldoende data om te vergelijken
"""

import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
POKEDEX_PATH = SCRIPT_DIR / "pokedex_ja_en.json"


def _pokedex() -> dict:
    return json.loads(POKEDEX_PATH.read_text())


_NAME_PREFIXES = {"MEGA", "DARK", "SHINING", "RADIANT"}
# Multi-word possessive-trainers (2-woord prefix eindigend op 'S).
_POSSESSIVE_MULTI = {"TEAM ROCKET'S", "TEAM AQUA'S", "TEAM MAGMA'S",
                      "TEAM GALACTIC'S", "TEAM PLASMA'S", "TEAM SKULL'S",
                      "TEAM YELL'S", "TEAM FLARE'S"}
_POSSESSIVE_RE = re.compile(r"[A-Z]+'S$", re.IGNORECASE)


def _extract_slab_pokemon_en(card_name: str) -> str | None:
    """Slab card_name is often 'FA/ESPEON VMAX' or 'ESPEON VMAX' → 'espeon'.

    Voor kaart-VARIANTEN met een naam-prefix (Mega/Dark/Shining/Radiant + Pokemon)
    of possessive-trainer (Team Rocket's / Ethan's / Cynthia's + Pokemon) behouden
    we het prefix zodat 'MEGA GENGAR EX' → 'mega gengar' en 'TEAM ROCKET'S MOLTRES'
    → 'team rocket's moltres'. Dat voorkomt dat variant-kaarten en gewone kaarten
    dezelfde card_key krijgen en dat de CM-URL aan de verkeerde variant matched.
    """
    if not card_name:
        return None
    clean = re.sub(r"^(FA|SA|RR|SR|SAR|UR|HR|CHR|AR|GX|EX|V|VMAX|VSTAR)[/\s]+", "", card_name, flags=re.IGNORECASE)
    clean = re.sub(r"\b(VMAX|VSTAR|GX|EX|V|SAR|SR|UR|HR|AR|CHR|FA|SA|RR)\b", "", clean, flags=re.IGNORECASE)

    # 1) 2-woord possessive-trainer (bv 'TEAM ROCKET'S MOLTRES') — check op raw split
    #    (voordat we .isalpha() filteren want 's ROCKET's'.isalpha() = False)
    raw_tokens = [t for t in re.split(r"[\s/]+", clean.strip()) if t]
    if len(raw_tokens) >= 3:
        prefix2 = f"{raw_tokens[0]} {raw_tokens[1]}".upper()
        if prefix2 in _POSSESSIVE_MULTI:
            pokemon = next((t.lower() for t in raw_tokens[2:] if t.isalpha() and len(t) >= 3), None)
            if pokemon:
                return f"{prefix2.lower()} {pokemon}"

    # 2) 1-woord possessive-trainer (bv 'ETHAN'S PIKACHU')
    if len(raw_tokens) >= 2 and _POSSESSIVE_RE.match(raw_tokens[0]):
        pokemon = next((t.lower() for t in raw_tokens[1:] if t.isalpha() and len(t) >= 3), None)
        if pokemon:
            return f"{raw_tokens[0].lower()} {pokemon}"

    # 3) Vaste alpha-only-woorden pad
    words = [w for w in raw_tokens if w.isalpha()]
    if not words:
        return None
    # Naam-prefix + pokemon-naam → behoud beide als 'prefix pokemon'
    if len(words) >= 2 and words[0].upper() in _NAME_PREFIXES:
        return f"{words[0].lower()} {words[1].lower()}"
    return words[0].lower()


def _grade_in_title(title: str) -> str | None:
    if not title:
        return None
    m = re.search(r"PSA\s*(10|9\.5|9|8\.5|8|7|6|5|4|3|2|1)", title, re.IGNORECASE)
    return m.group(1) if m else None


def _pokemon_in_text(text: str) -> str | None:
    """Return the EN pokemon name if any is mentioned in `text` (JP or EN).

    Gebruikt LANGSTE-MATCH: Zorua (ゾロア) is een substring van Zoroark (ゾロアーク),
    dus alle matches verzamelen en de langste retourneren. Voorkomt dat we een
    kortere naam kiezen wanneer de langere in de tekst voorkomt.
    """
    if not text:
        return None
    low = text.lower()
    pokedex = _pokedex()
    best: tuple[int, str] | None = None
    for jp, en in pokedex.items():
        if jp in text and (best is None or len(jp) > best[0]):
            best = (len(jp), en.lower())
        if len(en) >= 4 and en.lower() in low and (best is None or len(en) > best[0]):
            best = (len(en), en.lower())
    return best[1] if best else None


# `check_title` verhuisd naar _split/ (v2). Verwijderd 2026-09-01 (regel 2 cleanup).
# Backup: _legacy_pre_refactor/... — alias-override staat lager in de file.



# ---------------------------------------------------------------------------
# Refactor switch (2026-09-01, regel 2 #1): route `check_title` naar de v2 in
# check_title_split/title_match.py. Origineel def hierboven (regel 68, CoC 43)
# blijft staan voor snelle rollback. Alle externe importers
# (analyze.py, analyze_split/score_only.py, check_desc.py) krijgen automatisch
# de v2-implementatie via `from check_title import check_title`.
# Backup: _legacy_pre_refactor/check_title.py.20260901-regel2-func1
# ---------------------------------------------------------------------------
from check_title_split.title_match import check_title_v2 as _check_title_new  # noqa: E402
check_title = _check_title_new


if __name__ == "__main__":
    from check_slab import check_slab
    import sqlite3
    item_id = sys.argv[1] if len(sys.argv) > 1 else "m10228250509"
    conn = sqlite3.connect(str(SCRIPT_DIR / "kensa.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT title_jp, title_en FROM listings WHERE item_id = ?", (item_id,)).fetchone()
    slab = check_slab(item_id)
    print(f"--- slab ---\ncert={slab.get('cert')} grade={slab.get('grade')} name={slab.get('card_name')}")
    print(f"--- title ---\nJP: {row['title_jp']}\nEN: {row['title_en']}")
    r = check_title(slab, row["title_jp"], row["title_en"])
    print(f"\n--- check_title → {r['status']} ---")
    for m in r["matches"]: print(f"  MATCH: {m}")
    for m in r["mismatches"]: print(f"  MISMATCH: {m}")
    for m in r["reasons"]: print(f"  note: {m}")
