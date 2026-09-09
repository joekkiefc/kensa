"""enqueue_cardmarket.py — refactor-split van `_enqueue_cardmarket_if_possible`
uit `../analyze.py` (regel 211).

Vijf sub-functies, één per baseline-fase, plus een orkestrator
`enqueue_cardmarket_if_possible_v2` die exact hetzelfde gedrag oplevert als het
origineel (identieke DB-writes, Supabase-sync calls in dezelfde volgorde,
identieke stderr-prints, altijd `None` return).

De helpers `_build_card_key`, `_price_cache_get`, `_cache_is_fresh` en
`_pokemon_en_lookup` worden hergebruikt uit `ebay_phase.py` (DRY — zelfde
subdir, zelfde refactor-familie). Alleen `_looks_like_bundle` +
regex-constanten zijn hier gekopieerd omdat `ebay_phase.py` ze niet nodig had.

Zie `../analyze_refactor_fixtures_enqueue/BASELINE_ANALYSIS.md` voor de
22 branch-punten en 5-fase indeling.
"""

import json
import re as _re
import sqlite3
import sys
from pathlib import Path

from check_title import _extract_slab_pokemon_en
from supabase_client import lookup as cm_lookup

from analyze_split.ebay_phase import (
    _build_card_key,
    _cache_is_fresh,
    _pokemon_en_lookup,
    _price_cache_get,
    _price_cache_get_by_cm_url,
)


# ---------------------------------------------------------------------------
# Config-constants (gekopieerd uit analyze.py — dezelfde waarden)
# ---------------------------------------------------------------------------
_KENSA_DIR = Path(__file__).resolve().parent.parent
DB_PATH = _KENSA_DIR / "kensa.db"


# ---------------------------------------------------------------------------
# Bundle-detectie — gekopieerd uit analyze.py (regel 186-208). `ebay_phase.py`
# gebruikt dit niet, dus we importeren niet uit analyze.py om circular-imports
# te voorkomen.
# ---------------------------------------------------------------------------
BUNDLE_KEYWORDS_RE = _re.compile(
    r"(まとめ売り|枚セット|連番|引退品|大量|合計|コンプリート|"
    r"bulk\s*sale|consecutive\s*numbers|piece\s*set)",
    _re.IGNORECASE,
)
BUNDLE_DOUBLE_PSA_RE = _re.compile(r"psa\s*10[^a-zA-Z0-9]+.*psa\s*10", _re.IGNORECASE)


def _looks_like_bundle(title_jp: str | None, title_en: str | None) -> str | None:
    """Return de gematchte bundle-term, of None."""
    combined = f"{title_jp or ''}  {title_en or ''}"
    m = BUNDLE_KEYWORDS_RE.search(combined)
    if m:
        return m.group(0)
    if title_jp and BUNDLE_DOUBLE_PSA_RE.search(title_jp):
        return "PSA10-x2 (JP)"
    return None


# ---------------------------------------------------------------------------
# F1 — Guards  (baseline branches: B1, B2, B3)
# ---------------------------------------------------------------------------
def _enq_guards(slab: dict, listing: dict | None, verbose: bool) -> bool:
    """Return True als enqueue mag doorgaan.

    Print skip-redenen naar stderr wanneer verbose. Combineert de slab-status
    check en de bundle-titel check — beide vroege returns in het origineel.
    """
    # (B1) — slab-OCR moet betrouwbaar zijn
    if slab.get("status") != "pass":
        if verbose:
            print(
                f"  [cm] skip: slab niet betrouwbaar (status={slab.get('status')})",
                file=sys.stderr,
            )
        return False
    # (B2, B3) — bundle-check: geen CM-lookup voor lots
    if listing:
        hit_word = _looks_like_bundle(listing.get("title_jp"), listing.get("title_en"))
        if hit_word:
            if verbose:
                print(f"  [cm] skip: bundle-signaal '{hit_word}' in titel", file=sys.stderr)
            return False
    return True


# ---------------------------------------------------------------------------
# F2 — Identity extraction  (baseline branches: B4, B5, B6, B7)
# ---------------------------------------------------------------------------
def _enq_extract_identity(
    slab: dict, llm_data: dict | None,
) -> tuple[str | None, str | None]:
    """Extraheer (pokemon, number) uit LLM-uitkomst (voorrang) of slab-OCR.

    LLM-pad: loop door woorden in `name`, pak eerste ≥4 chars dat in pokedex
    zit (voorkomt 'Mega Darkrai ex' → 'Mega Lopunny'-mismatch).
    Non-LLM-pad: fall-back op `_extract_slab_pokemon_en` + slab.number.
    """
    if llm_data:
        # (B4, B5) — LLM-pad met pokedex-loop voor multi-word namen
        name_words = (llm_data.get("name") or "").split()
        pokedex = _pokemon_en_lookup()
        pokemon = next(
            (w.lower() for w in name_words
             if len(w) >= 4 and w.lower() in pokedex),
            None,
        )
        # (B6) — LLM.number wint, fallback slab.number
        number = llm_data.get("number") or slab.get("number")
    else:
        # (B7) — Non-LLM-pad
        pokemon = _extract_slab_pokemon_en(slab.get("card_name") or "")
        number = slab.get("number")
    return (pokemon, number)


# ---------------------------------------------------------------------------
# F3 — Supabase lookup + grade  (baseline branches: B9, B10, B11-B15)
# ---------------------------------------------------------------------------
def _enq_resolve_grade(
    slab: dict, llm_data: dict | None, verbose: bool,
) -> str:
    """Bepaal grade-string uit slab (voorrang) of LLM (≥0.7 confidence).

    Return "" als geen bruikbare grade — orkestrator returnt dan zonder
    default naar '10' (bewuste keuze: verkeerd vergelijken > niet vergelijken).
    """
    raw_grade = slab.get("grade")
    # (B12, B13, B14) — LLM-grade fallback bij lege slab-grade
    if raw_grade in (None, "") and llm_data:
        llm_grade = llm_data.get("grade")
        llm_conf = float(llm_data.get("confidence") or 0)
        if llm_grade and str(llm_grade).strip() not in ("", "null") and llm_conf >= 0.7:
            raw_grade = llm_grade
        elif llm_grade:
            if verbose:
                print(
                    f"  [cm] skip: LLM-grade '{llm_grade}' maar confidence {llm_conf:.2f} < 0.7",
                    file=sys.stderr,
                )
    grade = str(raw_grade).strip() if raw_grade not in (None, "", "null") else ""
    # (B15) — Grade nog steeds leeg → skip
    if not grade:
        if verbose:
            print(
                f"  [cm] skip: grade onbekend in slab én LLM — geen fallback naar PSA 10",
                file=sys.stderr,
            )
        return ""
    return grade


def _enq_lookup_and_grade(
    pokemon: str | None,
    number: str | None,
    slab: dict,
    llm_data: dict | None,
    verbose: bool,
) -> tuple[dict | None, str | None]:
    """Doe Supabase cm_lookup + resolve grade.

    Returns `(hit, grade)`. Als `hit is None` → geen enqueue (lookup faalde of
    geen match). Als `hit` gevuld maar `grade is None` → geen enqueue (grade
    onbepaald). Beide velden gevuld → orkestrator kan verder naar F4/F5.
    """
    # (B9) — cm_lookup call — kan exception gooien
    # 2026-09-09 (Tommy): set-info standaard meegeven → harde set-check in lookup.
    # pokemon+nummer is niet uniek (Pikachu-promo's); zonder set matchte de
    # lookup kaarten uit andere sets. Geen set-info → lookup gedraagt zich als voorheen.
    ld = llm_data or {}
    set_code = ld.get("set_code") or slab.get("set_code")
    set_hint = ld.get("set_name") or slab.get("set_name")
    try:
        hit = cm_lookup(pokemon, number, set_hint=set_hint, set_code=set_code)
    except Exception as e:
        if verbose:
            print(f"  [cm] supabase-lookup fout: {e}", file=sys.stderr)
        return (None, None)
    # (B10) — Geen Supabase-hit
    if not hit or not hit.get("url"):
        if verbose:
            print(f"  [cm] geen Supabase-hit voor {pokemon!r} #{number}", file=sys.stderr)
        return (None, None)
    # (B11-B15) — Grade bepalen
    grade = _enq_resolve_grade(slab, llm_data, verbose)
    if not grade:
        return (hit, None)
    return (hit, grade)


# ---------------------------------------------------------------------------
# F4 — Cache-hit fast-path  (baseline branches: B16, B17, B18, B19, B20)
# ---------------------------------------------------------------------------
def _enq_try_cache_hit(
    item_id: str,
    hit: dict,
    grade: str,
    slab: dict,
    llm_data: dict | None,
    verbose: bool,
) -> bool:
    """Als price_cache een verse cm_fetched_at + listings heeft: upsert queue
    MET fetched_at + listings_json (worker hoeft niet te scrapen).

    Return True als upsert (cache-hit pad) gebeurde, anders False → orkestrator
    valt door naar F5 fresh-insert.
    """
    from datetime import datetime, timezone

    # (B16, B17) — card_key bouwen; None → geen cache-lookup mogelijk
    card_key = _build_card_key(slab, llm_data)
    if not card_key:
        return False
    # (B18) — Cache-lookup + freshness op card_key (exacte match)
    cached = _price_cache_get(card_key)
    if not (cached and _cache_is_fresh(cached.get("cm_fetched_at"))):
        # FALLBACK 2026-09-05: card_key-mismatch (variant-drift zoals
        # 'charizard:223/193:10:m2a' vs '...:sv3a') — probeer of enige andere
        # card_key voor dezelfde CM URL wel een verse cache heeft. Zo ja:
        # hergebruik die listings_json (allen dezelfde fysieke kaart).
        alt = _price_cache_get_by_cm_url(hit["url"])
        if not (alt and _cache_is_fresh(alt.get("cm_fetched_at"))):
            return False
        cached = alt
        if verbose:
            print(
                f"  [cm] cache hit via URL-fallback ({card_key} → {alt.get('card_key')})",
                file=sys.stderr,
            )
    listings_json = cached.get("cm_listings_json") or "[]"
    now = datetime.now(timezone.utc).isoformat()
    # Dispatcher: env KENSA_WRITE_STORAGE stuurt writes naar Supabase-native i.p.v. SQLite+sync.
    import os as _os
    mode = _os.environ.get("KENSA_WRITE_STORAGE", "sqlite")
    if mode == "supabase":
        from storage_supabase.cardmarket_queue import enqueue_from_cache as _sb_enqueue_cache
        _sb_enqueue_cache(item_id, hit["url"], grade, card_key, listings_json)
    else:
        # (B19) — SQLite upsert MET fetched_at + listings_json
        conn = sqlite3.connect(str(DB_PATH))
        try:
            conn.execute(
                """INSERT INTO cardmarket_queue (item_id, url, grade, queued_at, fetched_at, listings_json, card_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(item_id) DO UPDATE SET
                       url=excluded.url, grade=excluded.grade,
                       fetched_at=excluded.fetched_at, listings_json=excluded.listings_json,
                       card_key=excluded.card_key, error=NULL""",
                (item_id, hit["url"], grade, now, now, listings_json, card_key),
            )
            conn.commit()
        finally:
            conn.close()
        if mode == "dual":
            from storage_supabase.cardmarket_queue import enqueue_from_cache as _sb_enqueue_cache
            _sb_enqueue_cache(item_id, hit["url"], grade, card_key, listings_json)
        else:
            # (B20) — Supabase-sync met fetched_at + listings_json kwargs
            try:
                import supabase_sync as _sbs
                _sbs.sync_cm_queue_upsert(
                    item_id, hit["url"], grade, now,
                    fetched_at=now, listings_json=listings_json, card_key=card_key,
                )
            except Exception:
                pass
    if verbose:
        n = len(json.loads(listings_json)) if listings_json else 0
        print(
            f"  [cm] cache hit ({card_key}) — {n} listings hergebruikt, geen worker-call",
            file=sys.stderr,
        )
    return True


# ---------------------------------------------------------------------------
# F5 — Fresh-insert pad  (baseline branches: B21, B22)
# ---------------------------------------------------------------------------
def _enq_fresh_insert(
    item_id: str,
    hit: dict,
    grade: str,
    slab: dict,
    llm_data: dict | None,
    verbose: bool,
) -> None:
    """Upsert queue ZONDER fetched_at / listings_json (worker moet scrapen).

    Bij CONFLICT (item_id staat al in de queue) resetten we bewust óók
    `fetched_at`, `listings_json` en `error`. Bugfix 2026-09-01: zonder deze
    reset bleef een oude "0 listings gevonden" resultaat voor eeuwig gecached
    (worker skipt op fetched_at IS NOT NULL). We komen alleen in deze branch
    als de kaart-brede price_cache expired is (>3 dagen) — dat is precies het
    moment waarop we ook de per-item queue-rij willen ververst hebben.

    `card_key` mag None zijn (bv. wanneer `_build_card_key` niet genoeg data
    heeft) — dan wordt de kolom NULL in SQLite, gelijk aan het origineel.
    """
    from datetime import datetime, timezone

    card_key = _build_card_key(slab, llm_data)
    now = datetime.now(timezone.utc).isoformat()
    # Dispatcher: env KENSA_WRITE_STORAGE stuurt writes naar Supabase-native i.p.v. SQLite+sync.
    import os as _os
    mode = _os.environ.get("KENSA_WRITE_STORAGE", "sqlite")
    if mode == "supabase":
        from storage_supabase.cardmarket_queue import enqueue_fresh as _sb_enqueue_fresh
        _sb_enqueue_fresh(item_id, hit["url"], grade, card_key)
    else:
        # (B21) — SQLite upsert; bij CONFLICT ook fetched_at/listings_json/error resetten
        conn = sqlite3.connect(str(DB_PATH))
        try:
            conn.execute(
                """INSERT INTO cardmarket_queue (item_id, url, grade, queued_at, card_key)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(item_id) DO UPDATE SET
                       url=excluded.url,
                       grade=excluded.grade,
                       card_key=excluded.card_key,
                       queued_at=excluded.queued_at,
                       fetched_at=NULL,
                       listings_json=NULL,
                       error=NULL""",
                (item_id, hit["url"], grade, now, card_key),
            )
            conn.commit()
        finally:
            conn.close()
        if mode == "dual":
            from storage_supabase.cardmarket_queue import enqueue_fresh as _sb_enqueue_fresh
            _sb_enqueue_fresh(item_id, hit["url"], grade, card_key)
        else:
            # (B22) — Supabase-sync met expliciete NULL-reset voor fetched_at/listings_json
            try:
                import supabase_sync as _sbs
                _sbs.sync_cm_queue_upsert(item_id, hit["url"], grade, now,
                                           fetched_at=None, listings_json=None,
                                           card_key=card_key)
            except Exception:
                pass
    if verbose:
        number = (llm_data or {}).get("number") or slab.get("number")
        print(
            f"  [cm] queued: {hit['name']} #{number} → {hit['url'][:80]}...",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Orkestrator — signature identiek aan `_enqueue_cardmarket_if_possible`
# ---------------------------------------------------------------------------
def enqueue_cardmarket_if_possible_v2(
    item_id: str,
    slab: dict,
    llm_data: dict | None,
    verbose: bool,
    listing: dict | None = None,
) -> None:
    """Refactor-orkestrator van `_enqueue_cardmarket_if_possible`.

    Zelfde inputs, zelfde outputs (altijd `None`), zelfde volgorde van
    DB-writes, Supabase-sync calls en stderr-prints. Splitst logica in
    5 sub-functies zonder semantiek-drift.
    """
    # F1 — Guards (slab-status + bundle)
    if not _enq_guards(slab, listing, verbose):
        return

    # F2 — Identity extraction (pokemon + number)
    pokemon, number = _enq_extract_identity(slab, llm_data)
    if not pokemon or not number:
        if verbose:
            print(f"  [cm] skip: geen bruikbare pokemon-naam of nummer", file=sys.stderr)
        return

    # F3 — Supabase lookup + grade
    hit, grade = _enq_lookup_and_grade(pokemon, number, slab, llm_data, verbose)
    if hit is None or grade is None:
        return

    # F4 — Cache-hit fast-path (upsert MET fetched_at + listings)
    if _enq_try_cache_hit(item_id, hit, grade, slab, llm_data, verbose):
        return

    # F5 — Fresh-insert (upsert ZONDER fetched_at — worker moet scrapen)
    _enq_fresh_insert(item_id, hit, grade, slab, llm_data, verbose)
