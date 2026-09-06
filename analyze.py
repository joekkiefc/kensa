#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa — analysis runner (orchestrator).

For a listing:
  1. slab OCR   (check_slab)
  2. title match (check_title)
  3. desc match  (check_desc)
  4. compute confidence score
  5. record cert-sighting (if cert extracted)
  6. save all trap-results + a combined 'summary' row to `analysis` table

CLI:
  ./analyze.py <item_id>         # 1 item
  ./analyze.py --all             # all listings without a summary
  ./analyze.py --all --limit N   # first N pending
"""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

from check_desc import check_desc
from check_slab import check_slab as _check_slab_vision
# Opt-in multimodal flow: KENSA_USE_MULTIMODAL=1 → hybride multimodal-first, Vision-fallback
# Anders: klassiek Vision-only (huidige gedrag)
import os as _os
if _os.environ.get("KENSA_USE_MULTIMODAL") == "1":
    from check_slab_hybrid import check_slab_hybrid as check_slab
else:
    check_slab = _check_slab_vision
from check_title import check_title, _extract_slab_pokemon_en, _pokemon_in_text, _pokedex

_POKEDEX_EN_CACHE: set[str] | None = None


def _pokemon_en_lookup() -> set[str]:
    """Set van alle EN pokemon-namen (lowercase) uit pokedex, voor snelle 'is dit een bekende naam' check."""
    global _POKEDEX_EN_CACHE
    if _POKEDEX_EN_CACHE is None:
        _POKEDEX_EN_CACHE = {en.lower() for en in _pokedex().values() if len(en) >= 3}
    return _POKEDEX_EN_CACHE
from ebay_filter import filter_sales as filter_ebay_sales, query_is_specific, query_is_very_specific
from ebay_lastsold import search as ebay_search
from llm_client import interpret_slab as llm_interpret_slab, judge_sales as llm_judge_sales
from supabase_client import lookup as cm_lookup
from price_stats import compute as compute_price_stats
from query_builder import build_query
from roi import compute as compute_roi
from score import compute_score
from storage import now_iso, record_cert_sighting
# Refactored eBay-phase — CC gesplitst in analyze_split/ebay_phase.py.
# Oude _run_ebay_phase (regel 464) blijft in deze file staan voor rollback;
# alle call-sites verwijzen sinds 2026-08-31 naar run_ebay_phase_v2.
from analyze_split.ebay_phase import run_ebay_phase_v2
# Refactored cardmarket-enqueue — CC 41 → max 12 per sub-functie.
# Oude _enqueue_cardmarket_if_possible (regel 211) blijft in deze file staan voor rollback;
# alle call-sites verwijzen sinds 2026-08-31 naar enqueue_cardmarket_if_possible_v2.
from analyze_split.enqueue_cardmarket import enqueue_cardmarket_if_possible_v2
# Refactored score-only orchestrator — CC 46 → max 12 per sub-functie.
# Zie ook alias-override aan het EIND van deze file zodat externe workers
# (worker_ebay*, worker_cache) via `from analyze import analyze_score_only`
# automatisch de v2-implementatie krijgen zonder hun code te wijzigen.
from analyze_split.score_only import analyze_score_only_v2 as _analyze_score_only_new
# Refactored hoofd-orkestrator — CC 51 → max 13 per sub-functie.
# Oude analyze (regel 816) blijft in deze file staan voor rollback;
# call-sites op regels 983 en 1026 verwijzen sinds 2026-09-01 naar analyze_v2.
from analyze_split.analyze_full import analyze_v2

SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "kensa.db"

EBAY_CACHE_HOURS = 24  # skip eBay-lookup als er al een verse cache is voor dezelfde query
PRICE_CACHE_DAYS = 3   # price_cache tabel: eBay + CM resultaten hergebruiken per kaart-identiteit


def _build_card_key(slab: dict, llm_data: dict | None) -> str | None:
    """Bouw een stabiele card-identity key uit slab + LLM data.

    Format: 'pokemon:number:grade[:set_code]'. Alle 3 kernvelden (pokemon, number,
    grade) MOETEN aanwezig zijn — anders return None (geen cache mogelijk).
    """
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


def _ensure_price_cache_table() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS price_cache (
                card_key         TEXT PRIMARY KEY,
                ebay_query       TEXT,
                ebay_result_json TEXT,
                ebay_fetched_at  TEXT,
                cm_url           TEXT,
                cm_listings_json TEXT,
                cm_fetched_at    TEXT
            )"""
        )
        conn.commit()
    finally:
        conn.close()


# _price_cache_{get,upsert_ebay,upsert_cm} + _cache_is_fresh: duplicate met
# analyze_split/ebay_phase.py + dood in analyze.py — verwijderd 2026-09-01
# (regel 4, LoC<500). Backup: _legacy_pre_refactor/analyze.py.20260901-rev2



_ensure_price_cache_table()

# Bundle-lot woorden — items met deze in titel bevatten 2+ kaarten, dus zinloos voor CM-lookup
# (Cardmarket heeft 1 kaart per URL). Woorden komen uit Gemini-analyse van 300 laatste listings.
import re as _re
BUNDLE_KEYWORDS_RE = _re.compile(
    r"(まとめ売り|枚セット|連番|引退品|大量|合計|コンプリート|"
    r"bulk\s*sale|consecutive\s*numbers|piece\s*set)",
    _re.IGNORECASE,
)
# 2x PSA10 in JP-titel = 2 slabs. Alleen op title_jp checken want title_en bevat vaak
# ook 'PSA10' door de vertaling (false-positive risico bij combined string).
BUNDLE_DOUBLE_PSA_RE = _re.compile(r"psa\s*10[^a-zA-Z0-9]+.*psa\s*10", _re.IGNORECASE)


def _looks_like_bundle(title_jp: str | None, title_en: str | None) -> str | None:
    """Return de gematchte bundle-term, of None."""
    combined = f"{title_jp or ''}  {title_en or ''}"
    m = BUNDLE_KEYWORDS_RE.search(combined)
    if m:
        return m.group(0)
    # Double-PSA check ALLEEN op JP-titel (EN-vertaling voegt vaak extra PSA10 toe)
    if title_jp and BUNDLE_DOUBLE_PSA_RE.search(title_jp):
        return "PSA10-x2 (JP)"
    return None


# NB: `_enqueue_cardmarket_if_possible` is verhuisd naar `analyze_split/` (v2).
#     De oude def (regel 224) is verwijderd na refactor 2026-09-01.
#     Backup: agents/kensa/_legacy_pre_refactor/analyze.py.20260901



def _needs_llm(slab: dict) -> tuple[bool, str]:
    """Regex-tier gate: alleen LLM aanroepen als de slab-data onvolledig/verdacht is."""
    if slab.get("status") != "pass":
        return True, "slab-status niet pass"
    name = slab.get("card_name") or ""
    if not name:
        return True, "geen card_name"
    slab_pk = _extract_slab_pokemon_en(name)
    if not slab_pk or slab_pk not in _pokemon_en_lookup():
        return True, f"slab-name '{slab_pk}' niet in pokedex"
    # Multi-word/possessive namen ('Rocket's Nidoking', 'Galarian Zapdos', 'Erika's Hospitality')
    # of extra tokens naast de pokemon-naam → regex zou naam-prefix wegstrippen → LLM beter
    import re as _re
    clean = _re.sub(r"\b(FA|SA|RR|VMAX|VSTAR|GX|EX|V|SAR|SR|UR|HR|AR|CHR)\b", "", name, flags=_re.IGNORECASE)
    if "'" in clean or "-" in clean:
        return True, "possessive/hyphen naam (multi-word)"
    tokens = [t for t in _re.split(r"[\s/]+", clean) if t.strip().isalpha() and len(t) >= 3]
    if len(tokens) > 1:
        return True, f"multi-word naam ({len(tokens)} tokens)"
    if not slab.get("number"):
        return True, "geen nummer"
    if not slab.get("set_name"):
        return True, "geen set_name"
    return False, "compleet — regex volstaat"


def _llm_enrich_slab(item_id: str, slab: dict, listing: dict, verbose: bool = False) -> dict | None:
    """Roept Gemini aan met RAW OCR-text (opnieuw opgevraagd) + listing-titel.

    Voert 1 verse OCR-call uit op de source photo om de volledige tekst te hebben
    (per_photo bewaart alleen char-count, geen full_text). Fallback: geparste velden.
    """
    from vision import ocr_image
    ocr_text = ""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        photos = conn.execute(
            "SELECT photo_index, url_original FROM photos WHERE item_id=? ORDER BY photo_index",
            (item_id,),
        ).fetchall()
    finally:
        conn.close()
    src_idx = slab.get("source_photo_idx")
    url = None
    for i, u in photos:
        if i == src_idx or (src_idx is None and "/orig/" in u):
            url = u
            break
    if url:
        try:
            r = ocr_image(url)
            ocr_text = r.get("full_text") or ""
        except Exception:
            ocr_text = ""
    if not ocr_text:
        parts = []
        for k in ("year", "set_name", "card_name", "number", "grade_text", "grade", "cert"):
            v = slab.get(k)
            if v: parts.append(str(v))
        ocr_text = "\n".join(parts)

    result = llm_interpret_slab(
        ocr_text,
        listing.get("title_en"),
        listing.get("title_jp"),
        verbose=verbose,
    )
    if verbose:
        if "_error" in result:
            print(f"  [llm] fout: {result['_error']}", file=sys.stderr)
        else:
            meta = result.get("_meta") or {}
            print(f"  [llm] {meta.get('latency_ms')}ms  in={meta.get('in_tokens')} out={meta.get('out_tokens')}  → {result.get('name')} · {result.get('ebay_query')}", file=sys.stderr)
    return result if "_error" not in result else None


def _load_listing(item_id: str) -> dict | None:
    # Supabase-first switch: KENSA_READ_STORAGE=supabase leest van Supabase.
    if _os.environ.get("KENSA_READ_STORAGE", "").lower() == "supabase":
        from storage_supabase import load_listing_supabase
        return load_listing_supabase(item_id)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM listings WHERE item_id = ?", (item_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _save_trap(item_id: str, trap: str, result: dict, confidence: float | None = None, card_id: str | None = None) -> None:
    # Dispatcher: env KENSA_WRITE_STORAGE stuurt naar Supabase-native i.p.v. SQLite+sync.
    mode = _os.environ.get("KENSA_WRITE_STORAGE", "sqlite")
    if mode == "supabase":
        from storage_supabase.analysis import save_trap as _sb_save_trap
        _sb_save_trap(item_id, trap, result, confidence, card_id)
        return
    ts = now_iso()
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("DELETE FROM analysis WHERE item_id = ? AND trap = ?", (item_id, trap))
        conn.execute(
            """INSERT INTO analysis (item_id, trap, result_json, confidence, card_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (item_id, trap, json.dumps(result, ensure_ascii=False, default=str),
             confidence, card_id, ts),
        )
        conn.commit()
    finally:
        conn.close()
    if mode == "dual":
        from storage_supabase.analysis import save_trap as _sb_save_trap
        _sb_save_trap(item_id, trap, result, confidence, card_id)
        return
    try:
        import supabase_sync as _sbs
        _sbs.sync_replace_analysis(item_id, trap, result, confidence, card_id, ts)
    except Exception:
        pass


def _cached_ebay_for_query(query: str) -> dict | None:
    """Zoek een bestaande ebay_prices record met dezelfde query < 24u oud."""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=EBAY_CACHE_HOURS)).isoformat()
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


# NB: `_run_ebay_phase` is verhuisd naar `analyze_split/` (v2).
#     De oude def (regel 481) is verwijderd na refactor 2026-09-01.
#     Backup: agents/kensa/_legacy_pre_refactor/analyze.py.20260901



def _mark_slab_status(item_id: str, status: str, card_key: str | None = None) -> None:
    mode = _os.environ.get("KENSA_WRITE_STORAGE", "sqlite")
    if mode == "supabase":
        from storage_supabase.listings import mark_slab_status as _sb_mark
        _sb_mark(item_id, status, card_key)
        return
    conn = sqlite3.connect(str(DB_PATH))
    try:
        if card_key is not None:
            conn.execute("UPDATE listings SET slab_status=?, card_key=? WHERE item_id=?",
                         (status, card_key, item_id))
        else:
            conn.execute("UPDATE listings SET slab_status=? WHERE item_id=?", (status, item_id))
        conn.commit()
    finally:
        conn.close()
    if mode == "dual":
        from storage_supabase.listings import mark_slab_status as _sb_mark
        _sb_mark(item_id, status, card_key)
        return
    try:
        import supabase_sync as _sbs
        _sbs.sync_slab_status(item_id, status, card_key)
    except Exception:
        pass


def _load_stored_slab_llm(item_id: str) -> tuple[dict | None, dict | None, int]:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        slab_row = conn.execute(
            "SELECT result_json FROM analysis WHERE item_id=? AND trap='slab_ocr' ORDER BY analysis_id DESC LIMIT 1",
            (item_id,)).fetchone()
        llm_row = conn.execute(
            "SELECT result_json FROM analysis WHERE item_id=? AND trap='llm_slab' ORDER BY analysis_id DESC LIMIT 1",
            (item_id,)).fetchone()
        prev_sum = conn.execute(
            "SELECT result_json FROM analysis WHERE item_id=? AND trap='summary' ORDER BY analysis_id DESC LIMIT 1",
            (item_id,)).fetchone()
    finally:
        conn.close()
    slab = json.loads(slab_row["result_json"]) if slab_row else None
    llm = json.loads(llm_row["result_json"]) if llm_row else None
    prev_retry = 0
    if prev_sum and prev_sum["result_json"]:
        try:
            prev_retry = int(json.loads(prev_sum["result_json"]).get("llm_retry_count") or 0)
        except Exception:
            pass
    return slab, llm, prev_retry


def analyze_ocr_only(item_id: str, verbose: bool = False) -> dict:
    """Fase A — check_slab + LLM enrichment. Slaat resultaten op en zet slab_status='ocr_done'."""
    listing = _load_listing(item_id)
    if not listing:
        return {"error": f"listing {item_id} not found"}

    t0 = time.perf_counter()
    slab = check_slab(item_id, max_photos=2)

    llm_data = None
    llm_needed_but_failed = False
    needs, reason = _needs_llm(slab)
    if needs and slab.get("status") != "pass":
        if verbose:
            print(f"  [llm] gate open maar slab.status={slab.get('status')} → skip LLM (0%-item)", file=sys.stderr)
    elif needs:
        if verbose:
            print(f"  [llm] gate open — {reason}", file=sys.stderr)
        llm_data = _llm_enrich_slab(item_id, slab, listing, verbose=verbose)
        if llm_data:
            _save_trap(item_id, "llm_slab", llm_data)
        else:
            llm_needed_but_failed = True

    _save_trap(item_id, "slab_ocr", slab,
               confidence=100.0 if slab["status"] == "pass" else 0.0,
               card_id=slab.get("cert"))

    card_key = _build_card_key(slab, llm_data)
    _mark_slab_status(item_id, "ocr_done", card_key=card_key)

    took_ms = int((time.perf_counter() - t0) * 1000)
    if verbose:
        print(f"[{item_id}] OCR-phase done in {took_ms}ms  slab.status={slab.get('status')} "
              f"card_key={card_key} llm={'ok' if llm_data else ('fail' if llm_needed_but_failed else 'skip')}",
              file=sys.stderr)
    return {
        "item_id": item_id, "slab": slab, "llm_data": llm_data,
        "card_key": card_key, "llm_needed_but_failed": llm_needed_but_failed,
        "took_ms": took_ms,
    }


# NB: `analyze_score_only` is verhuisd naar `analyze_split/` (v2).
#     De oude def (regel 712) is verwijderd na refactor 2026-09-01.
#     Backup: agents/kensa/_legacy_pre_refactor/analyze.py.20260901



# NB: `analyze` is verhuisd naar `analyze_split/` (v2).
#     De oude def (regel 820) is verwijderd na refactor 2026-09-01.
#     Backup: agents/kensa/_legacy_pre_refactor/analyze.py.20260901



def analyze_all(limit: int | None = None, force: bool = False, do_ebay: bool = True) -> list[dict]:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        if force:
            sql = "SELECT item_id FROM listings WHERE detail_scraped_at IS NOT NULL ORDER BY first_seen_at DESC"
        else:
            # Unanalyzed items ÉN items waarvan LLM eerder faalde (llm_needed_but_failed=True)
            # Retry-cap: skip items die al 3× LLM-fail hebben gehad (voorkomt oneindige Gemini-verspilling)
            # ORDER BY DESC = nieuwste eerst; dashboard toont dan snel de meest recente items.
            sql = """SELECT l.item_id FROM listings l
                     LEFT JOIN analysis a ON a.item_id = l.item_id AND a.trap = 'summary'
                     WHERE l.detail_scraped_at IS NOT NULL
                       AND (a.analysis_id IS NULL
                            OR (json_extract(a.result_json, '$.llm_needed_but_failed') = 1
                                AND COALESCE(json_extract(a.result_json, '$.llm_retry_count'), 0) < 3))
                     ORDER BY l.first_seen_at DESC"""
        rows = conn.execute(sql).fetchall()
    finally:
        conn.close()

    if limit:
        rows = rows[:limit]

    results = []
    import random, time as _t
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Parallel-analyze met 3 threads: OCR/LLM/eBay per item lopen tegelijk voor
    # verschillende items. SQLite calls in analyze() openen elk hun eigen connection
    # dus thread-safe. eBay heeft eigen bot-detectie los van Buyee — 3 gelijktijdige
    # eBay-calls is nog binnen veilige marge. Bij problemen: PARALLELISM=1 zetten.
    PARALLELISM = 3

    def _one(item_id: str) -> dict:
        r = analyze_v2(item_id, verbose=True, do_ebay=do_ebay)
        return {"item_id": item_id, **r.get("summary", {}), "_from_cache": (r.get("summary") or {}).get("ebay_from_cache")}

    # Track hoeveel live eBay-hits er gebeurden — bepaalt of we globale pauze nemen
    # tussen batches (voorkomt eBay account-flag bij te veel opeenvolgende live-fetches)
    live_hits_this_batch = 0
    batch = [row["item_id"] for row in rows]
    with ThreadPoolExecutor(max_workers=PARALLELISM) as pool:
        futures = {pool.submit(_one, iid): iid for iid in batch}
        for fut in as_completed(futures):
            try:
                r = fut.result()
            except Exception as e:
                iid = futures[fut]
                print(f"  [analyze] {iid} crashte: {e}", file=sys.stderr)
                continue
            results.append({k: v for k, v in r.items() if k != "_from_cache"})
            if do_ebay and r.get("_from_cache") is False:
                live_hits_this_batch += 1

    # Pauze na batch bij veel live-hits (spread eBay-load over volgende cron-runs)
    if do_ebay and live_hits_this_batch >= 5:
        pause = random.uniform(30.0, 60.0)
        print(f"  [pace] {live_hits_this_batch} live eBay-fetches — koelt af {pause:.0f}s", file=sys.stderr)
        _t.sleep(pause)
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("item_id", nargs="?", help="analyze single item")
    ap.add_argument("--all", action="store_true", help="analyze all unanalyzed listings")
    ap.add_argument("--limit", type=int, help="max items when --all")
    ap.add_argument("--force", action="store_true", help="re-analyze even if already summarized")
    ap.add_argument("--no-ebay", action="store_true", help="skip eBay-fase")
    args = ap.parse_args()

    if args.all:
        results = analyze_all(limit=args.limit, force=args.force, do_ebay=not args.no_ebay)
        print(f"\n=== done: {len(results)} items ===")
        for r in results:
            print(f"  {r['item_id']:16} {r.get('score','?'):>3}% {r.get('verdict','?')}")
    elif args.item_id:
        r = analyze_v2(args.item_id, verbose=True, do_ebay=not args.no_ebay)
        print(json.dumps(r["summary"], ensure_ascii=False, indent=2))
    else:
        ap.print_help()
        sys.exit(1)


# ---------------------------------------------------------------------------
# Refactor switch (2026-08-31/2026-09-01): route publieke + private namen naar
# hun v2-implementatie in analyze_split/. De originele defs zijn verwijderd
# na cleanup 2026-09-01; backup in _legacy_pre_refactor/analyze.py.20260901.
# Deze aliases houden externe imports (`from analyze import analyze_score_only`)
# en dot-access (`analyze.analyze(...)`) transparant werkend.
# ---------------------------------------------------------------------------
analyze_score_only = _analyze_score_only_new
analyze = analyze_v2
_run_ebay_phase = run_ebay_phase_v2
_enqueue_cardmarket_if_possible = enqueue_cardmarket_if_possible_v2
