"""ebay_phase.py — refactor-split van `_run_ebay_phase` uit `../analyze.py`.

Vijf sub-functies, één per baseline-fase, plus een orkestrator `run_ebay_phase_v2`
die exact hetzelfde gedrag oplevert als `_run_ebay_phase` (zelfde return-shape,
zelfde DB-writes, zelfde HTTP-calls in dezelfde volgorde).

De sub-functies zijn LOSSTAAND: SQLite-helpers en `_build_card_key` zijn hier
gekopieerd i.p.v. geïmporteerd uit analyze.py (zie _meta.md § "Complicaties").
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

from check_title import _extract_slab_pokemon_en, _pokemon_in_text, _pokedex
from ebay_filter import (
    filter_sales as filter_ebay_sales,
    query_is_specific,
    query_is_very_specific,
)
from ebay_lastsold import search as ebay_search
from llm_client import judge_sales as llm_judge_sales
from price_stats import compute as compute_price_stats
from query_builder import build_query
from storage import now_iso


# ---------------------------------------------------------------------------
# Config-constants (gekopieerd uit analyze.py — dezelfde waarden)
# ---------------------------------------------------------------------------
_KENSA_DIR = Path(__file__).resolve().parent.parent
DB_PATH = _KENSA_DIR / "kensa.db"
EBAY_CACHE_HOURS = 24
PRICE_CACHE_DAYS = 3


# ---------------------------------------------------------------------------
# Helpers — gekopieerd uit analyze.py zodat deze module losstaand werkt.
# GEDRAG identiek aan de originele functies; niet aanpassen tijdens refactor.
# ---------------------------------------------------------------------------
_POKEDEX_EN_CACHE: set[str] | None = None


def _pokemon_en_lookup() -> set[str]:
    global _POKEDEX_EN_CACHE
    if _POKEDEX_EN_CACHE is None:
        _POKEDEX_EN_CACHE = {en.lower() for en in _pokedex().values() if len(en) >= 3}
    return _POKEDEX_EN_CACHE


def _build_card_key(slab: dict, llm_data: dict | None) -> str | None:
    """Bouw stabiele card-identity key: 'pokemon:number:grade[:set_code]'."""
    pokemon = None
    if llm_data:
        name_words = (llm_data.get("name") or "").split()
        pokedex = _pokemon_en_lookup()
        pokemon = next(
            (w.lower() for w in name_words if len(w) >= 4 and w.lower() in pokedex),
            None,
        )
    if not pokemon:
        pokemon = _extract_slab_pokemon_en(slab.get("card_name") or "")
    number = (llm_data or {}).get("number") or slab.get("number")
    grade = slab.get("grade") or (llm_data or {}).get("grade")
    if not (pokemon and number and grade):
        return None
    set_code = (llm_data or {}).get("set_code") or slab.get("set_code") or ""
    key = f"{str(pokemon).lower().strip()}:{str(number).strip()}:{str(grade).strip()}"
    if set_code:
        key += f":{str(set_code).lower().strip()}"
    return key


def _read_via_supabase() -> bool:
    """True als env KENSA_READ_STORAGE=supabase staat."""
    return os.environ.get("KENSA_READ_STORAGE", "").lower() == "supabase"


def _price_cache_get(card_key: str) -> dict | None:
    """Dispatcher: Supabase-read als env-flag aan, anders SQLite (Pi).

    Voor backwards-compat serialiseren we de jsonb-kolommen naar string zodat
    downstream code (die json.loads doet) identiek blijft werken.
    """
    if _read_via_supabase():
        from storage_supabase.price_cache import fetch_by_card_key as _sb_fetch
        row = _sb_fetch(card_key)
        if not row:
            return None
        out = dict(row)
        for k in ("ebay_result_json", "cm_listings_json"):
            if out.get(k) is not None and not isinstance(out[k], str):
                out[k] = json.dumps(out[k], ensure_ascii=False)
        return out
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM price_cache WHERE card_key=?", (card_key,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _price_cache_get_by_cm_url(cm_url: str) -> dict | None:
    """Fallback-lookup: zoek in price_cache naar enige rij met deze cm_url én
    verse cm_fetched_at. Nuttig als dezelfde fysieke kaart onder meerdere
    card_key-varianten is opgeslagen (bv `charizard:223/193:10:m2a` vs
    `charizard:223/193:10:sv3a` — allebei dezelfde Cardmarket URL).

    Retourneert de meest recent gefetche rij, of None. jsonb-velden altijd als
    string (backwards-compat met _price_cache_get).
    """
    if not cm_url:
        return None
    if _read_via_supabase():
        from storage_supabase import _http
        from storage_supabase._logging import timed
        with timed("price_cache.fetch_by_cm_url", key=cm_url[-60:]) as ctx:
            rows = _http.get("price_cache", "kensa", {
                "cm_url": f"eq.{cm_url}",
                "cm_fetched_at": "not.is.null",
                "select": "card_key,cm_url,cm_listings_json,cm_fetched_at",
                "order": "cm_fetched_at.desc",
                "limit": "1",
            })
            ctx["n"] = len(rows)
        if not rows:
            return None
        out = dict(rows[0])
        if out.get("cm_listings_json") is not None and not isinstance(out["cm_listings_json"], str):
            out["cm_listings_json"] = json.dumps(out["cm_listings_json"], ensure_ascii=False)
        return out
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT card_key, cm_url, cm_listings_json, cm_fetched_at "
            "FROM price_cache WHERE cm_url=? AND cm_fetched_at IS NOT NULL "
            "ORDER BY cm_fetched_at DESC LIMIT 1",
            (cm_url,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _cache_is_fresh(fetched_at: str | None) -> bool:
    if not fetched_at:
        return False
    from datetime import datetime, timedelta, timezone
    try:
        ts = datetime.fromisoformat(fetched_at)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts >= datetime.now(timezone.utc) - timedelta(days=PRICE_CACHE_DAYS)


def _price_cache_upsert_ebay(card_key: str, query: str, result: dict) -> None:
    now = now_iso()
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            """INSERT INTO price_cache (card_key, ebay_query, ebay_result_json, ebay_fetched_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(card_key) DO UPDATE SET
                   ebay_query=excluded.ebay_query,
                   ebay_result_json=excluded.ebay_result_json,
                   ebay_fetched_at=excluded.ebay_fetched_at""",
            (card_key, query, json.dumps(result, ensure_ascii=False), now),
        )
        conn.commit()
    finally:
        conn.close()
    try:
        import supabase_sync as _sbs
        _sbs.sync_price_cache_ebay(card_key, query, result, now)
    except Exception:
        pass


def _cached_ebay_for_query(query: str) -> dict | None:
    """Zoek bestaande ebay_prices analysis-rij met dezelfde query < 24u oud.

    Dispatcher: Supabase-read als env-flag aan, anders SQLite (Pi).
    """
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=EBAY_CACHE_HOURS)).isoformat()

    if _read_via_supabase():
        # Direct via _http omdat we een filter op trap+created_at nodig hebben
        # die niet één van de standard fetch_* helpers dekt.
        from storage_supabase import _http
        from storage_supabase._logging import timed
        with timed("analysis.fetch_recent_ebay_prices", key=f"cutoff={cutoff[:19]}") as ctx:
            rows = _http.get("analysis", "kensa", {
                "trap": "eq.ebay_prices",
                "created_at": f"gte.{cutoff}",
                "select": "result_json",
                "order": "analysis_id.desc",
                "limit": "20",
            })
            ctx["n"] = len(rows)
        for r in rows:
            data = r.get("result_json")
            if isinstance(data, dict) and data.get("query") == query:
                return data
        return None

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT result_json FROM analysis
               WHERE trap = 'ebay_prices' AND created_at >= ?
               ORDER BY analysis_id DESC LIMIT 20""",
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        try:
            data = json.loads(r["result_json"])
            if data.get("query") == query:
                return data
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# F1 — Guard  (baseline branches: B1, B2)
# ---------------------------------------------------------------------------
def _ebay_guard(slab: dict, do_ebay: bool) -> bool:
    """Return True als de eBay-fase mag doorgaan.

    False-scenario's worden door de orkestrator vertaald naar de juiste
    return-value (`None` bij `do_ebay=False`, skip-dict bij slab-fail).
    """
    if not do_ebay:
        return False
    if slab.get("status") != "pass":
        return False
    return True


# ---------------------------------------------------------------------------
# F2 — Query bouwen  (baseline branches: B3, B4, B5, B6, B7)
# ---------------------------------------------------------------------------
# `_ebay_build_query` verhuisd naar _split/ (v2). Verwijderd 2026-09-01 (regel 2 cleanup).
# Backup: _legacy_pre_refactor/... — alias-override staat lager in de file.




# ---------------------------------------------------------------------------
# F3 — Cache-check  (baseline branches: B8, B9)
# ---------------------------------------------------------------------------
def _ebay_cache_check(
    slab: dict,
    llm_data: dict | None,
    query: str,
    verbose: bool,
) -> tuple[str | None, dict | None]:
    """Check twee cache-lagen (identiteit-cache 3d, query-cache 24u).

    Returns: `(card_key, cached_result)`. Als cached_result niet None,
    dan retourneert de orkestrator die direct.
    """
    card_key = _build_card_key(slab, llm_data)

    # (B8) — Kaart-identiteit cache
    if card_key:
        pc = _price_cache_get(card_key)
        if pc and _cache_is_fresh(pc.get("ebay_fetched_at")) and pc.get("ebay_result_json"):
            try:
                pc_result = json.loads(pc["ebay_result_json"])
                if verbose:
                    print(
                        f"  [ebay] price_cache hit ({card_key}) — {len(pc_result.get('sales') or [])} sales hergebruikt",
                        file=sys.stderr,
                    )
                return (card_key, {**pc_result, "from_cache": True, "cache_source": "price_cache"})
            except Exception:
                pass

    # (B9) — Query-cache (analysis-tabel, 24u)
    cached = _cached_ebay_for_query(query)
    if cached:
        if verbose:
            print(f"  [ebay] cache hit: {query}", file=sys.stderr)
        return (card_key, {**cached, "from_cache": True})

    return (card_key, None)


# ---------------------------------------------------------------------------
# F4 — Live fetch + filter  (baseline branches: B10, B11, B12, B13, B14)
# ---------------------------------------------------------------------------
def _ebay_fetch_and_filter(
    query: str,
    llm_data: dict | None,
    verbose: bool,
) -> tuple[dict, list, dict]:
    """Doe live eBay-fetch, regex-filter, optionele LLM-judge.

    Returns: `(result, filtered_sales, filter_meta)` — genoeg data voor F5
    om `sales_raw_count`, `fetched_at`, `error` én de filter-metrics te
    reconstrueren. `filter_meta['judge_source']` is 'llm' als de LLM-judge
    het regex-filter overrulede, anders afwezig (default → 'regex' in F5).
    """
    # (B10) — Live HTTP fetch
    if verbose:
        print(f"  [ebay] fetching: {query}", file=sys.stderr)
    result = ebay_search(query, limit=15, verbose=False)
    raw_sales = result.get("sales", [])

    # (B11) — Regex-filter
    filtered_sales, filter_meta = filter_ebay_sales(raw_sales, query)
    if verbose and filter_meta["removed"]:
        print(
            f"  [ebay] regex-filter: {filter_meta['kept']}/{len(raw_sales)} sales gehouden",
            file=sys.stderr,
        )

    # (B12) — very-specific fast-path: LLM-judge overslaan
    very_specific, why_specific = query_is_very_specific(query)
    if very_specific and verbose:
        print(
            f"  [ebay] query zeer scherp ({why_specific}) — LLM-judge overgeslagen",
            file=sys.stderr,
        )

    # (B13) + (B14) — LLM-judge oordeel (of fallback op regex)
    if llm_data and raw_sales and not very_specific:
        filtered_sales, filter_meta = _apply_llm_judge(
            query, raw_sales, llm_data, filtered_sales, filter_meta, verbose,
        )

    return (result, filtered_sales, filter_meta)


def _apply_llm_judge(
    query: str,
    raw_sales: list,
    llm_data: dict,
    filtered_sales: list,
    filter_meta: dict,
    verbose: bool,
) -> tuple[list, dict]:
    """LLM-judge sub-stap uit F4 — apart om F4's CC laag te houden."""
    context = {"variant": llm_data.get("variant")} if llm_data.get("variant") else None
    llm_judge_result = llm_judge_sales(query, raw_sales, context=context, verbose=verbose)
    if llm_judge_result and "decisions" in llm_judge_result:
        llm_keeps = {d["i"] for d in llm_judge_result["decisions"] if d.get("keep")}
        llm_filtered = [raw_sales[i] for i in range(len(raw_sales)) if i in llm_keeps]
        if verbose:
            m = llm_judge_result.get("_meta") or {}
            print(
                f"  [llm-judge] {len(llm_filtered)}/{len(raw_sales)} sales gehouden — {m.get('latency_ms')}ms",
                file=sys.stderr,
            )
        new_sales = llm_filtered[:5]
        new_meta = {
            "kept": len(new_sales),
            "removed": len(raw_sales) - len(new_sales),
            "removed_reasons": [
                f"[llm] {d['reason']}"
                for d in llm_judge_result["decisions"]
                if not d.get("keep")
            ],
            "trimmed": max(0, len(llm_filtered) - 5),
            "judge_source": "llm",
        }
        return (new_sales, new_meta)
    if verbose and llm_judge_result:
        print(
            f"  [llm-judge] fout: {llm_judge_result.get('_error')} — fallback op regex-filter",
            file=sys.stderr,
        )
    return (filtered_sales, filter_meta)


# ---------------------------------------------------------------------------
# F5 — Stats + persist  (baseline branches: B15, B16)
# ---------------------------------------------------------------------------
def _ebay_persist_and_stats(
    query: str,
    query_source: str,
    result: dict,
    filtered_sales: list,
    filter_meta: dict,
    raw_sales_count: int,
    card_key: str | None,
    verbose: bool,
) -> dict:
    """Bouw output-dict, upsert price_cache (SQLite + supabase-sync fire-and-forget)."""
    # (B15) — stats
    stats = compute_price_stats(filtered_sales)
    out = {
        "query": query,
        "query_source": query_source,
        "fetched_at": result.get("fetched_at"),
        "error": result.get("error"),
        "sales": filtered_sales[:5],
        "sales_raw_count": raw_sales_count,
        "filter_removed_count": filter_meta["removed"],
        "filter_trimmed_count": filter_meta.get("trimmed", 0),
        "filter_removed_reasons": filter_meta["removed_reasons"],
        "filter_source": filter_meta.get("judge_source", "regex"),
        "stats": stats,
        "from_cache": False,
    }
    # (B16) — price_cache upsert (SQLite + supabase in die volgorde, via helper)
    if card_key and not result.get("error"):
        try:
            _price_cache_upsert_ebay(card_key, query, out)
        except Exception as e:
            if verbose:
                print(f"  [ebay] price_cache upsert fout: {e}", file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# Orkestrator — signature identiek aan `_run_ebay_phase`
# ---------------------------------------------------------------------------
def run_ebay_phase_v2(
    slab: dict,
    listing: dict,
    do_ebay: bool,
    verbose: bool,
    llm_data: dict | None = None,
) -> dict | None:
    """Refactor-orkestrator van `_run_ebay_phase`.

    Zelfde inputs, zelfde outputs, zelfde volgorde van DB-writes en HTTP-calls.
    Splits de logica in 5 sub-functies zonder semantiek-drift.
    """
    # F1 — Guard
    if not _ebay_guard(slab, do_ebay):
        if not do_ebay:
            return None
        return {"query": None, "skipped": "slab niet leesbaar"}

    # F2 — Query bouwen
    query, query_source, skip_result = _ebay_build_query(slab, listing, llm_data, verbose)
    if skip_result is not None:
        return skip_result
    # Vanaf hier is `query` gegarandeerd een niet-lege string.
    assert query is not None

    # F3 — Cache-check
    card_key, cached_result = _ebay_cache_check(slab, llm_data, query, verbose)
    if cached_result is not None:
        return cached_result

    # F4 — Live fetch + filter
    result, filtered_sales, filter_meta = _ebay_fetch_and_filter(query, llm_data, verbose)
    raw_sales_count = len(result.get("sales", []))

    # F5 — Stats + persist
    return _ebay_persist_and_stats(
        query,
        query_source,
        result,
        filtered_sales,
        filter_meta,
        raw_sales_count,
        card_key,
        verbose,
    )


# Refactor switch (2026-09-01, regel 2 #6): route _ebay_build_query -> v2.
# Backup: _legacy_pre_refactor/ebay_phase.py.20260901-regel2-func6
from analyze_split.ebay_query_split import _ebay_build_query_v2 as _ebay_build_query_new  # noqa: E402
_ebay_build_query = _ebay_build_query_new
