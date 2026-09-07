"""score_only.py — refactor-split van `analyze_score_only` uit `../analyze.py`
(regel 703, CC 46).

Zes sub-functies (F1-F6), plus een orkestrator `analyze_score_only_v2` die
exact hetzelfde gedrag oplevert als het origineel:
zelfde DB-writes, zelfde volgorde van `_save_trap`-calls, zelfde stderr-prints,
identiek return-dict shape, identieke mode-guards.

Helpers hergebruikt uit `ebay_phase.py` (`_build_card_key`, `_price_cache_get`,
`_cache_is_fresh`) en uit `enqueue_cardmarket.py` (`_looks_like_bundle`).
`run_ebay_phase_v2` wordt gebruikt als
al-gerefactorde v2-helpers. Kleine analyze.py-helpers (`_load_listing`,
`_load_stored_slab_llm`, `_save_trap`, `_mark_slab_status`) zijn hier
gekopieerd — DB-only, geen semantiek-drift. `_llm_enrich_slab` wordt lazy
geïmporteerd binnen de hoge-ROI retry-fase om circular-import te voorkomen.

Zie `../analyze_refactor_fixtures_score_only/BASELINE_ANALYSIS.md` voor de
41 branch-punten en 6-fase indeling.
"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from check_desc import check_desc
from check_title import check_title
from roi import compute as compute_roi
from score import compute_score
from storage import now_iso, record_cert_sighting

from analyze_split.ebay_phase import (
    _build_card_key,
    _cache_is_fresh,
    _price_cache_get,
    run_ebay_phase_v2,
)
from analyze_split.enqueue_cardmarket import (
    _looks_like_bundle,
)


# ---------------------------------------------------------------------------
# Config-constants (gekopieerd uit analyze.py — dezelfde waarden)
# ---------------------------------------------------------------------------
_KENSA_DIR = Path(__file__).resolve().parent.parent
DB_PATH = _KENSA_DIR / "kensa.db"


# ---------------------------------------------------------------------------
# Helpers — gekopieerd uit analyze.py zodat deze module losstaand werkt.
# GEDRAG identiek aan de originele functies; niet aanpassen tijdens refactor.
# ---------------------------------------------------------------------------
def _read_via_supabase() -> bool:
    """True als env KENSA_READ_STORAGE=supabase staat."""
    return os.environ.get("KENSA_READ_STORAGE", "").lower() == "supabase"


def _load_listing(item_id: str) -> dict | None:
    """Dispatcher: Supabase-read als env-flag aan, anders SQLite (Pi)."""
    if _read_via_supabase():
        from storage_supabase.listings import fetch_listing as _sb_fetch
        return _sb_fetch(item_id)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM listings WHERE item_id = ?", (item_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _load_stored_slab_llm(
    item_id: str,
) -> tuple[dict | None, dict | None, int]:
    """Dispatcher: Supabase-read als env-flag aan, anders SQLite (Pi).

    Retourneert (slab_dict, llm_dict, prev_retry_count). slab en llm zijn
    het geparste result_json (of None); prev_retry_count komt uit summary's
    llm_retry_count (of 0).
    """
    if _read_via_supabase():
        from storage_supabase.analysis import fetch_latest_trap as _sb_trap
        slab_row = _sb_trap(item_id, "slab_ocr")
        llm_row = _sb_trap(item_id, "llm_slab")
        prev_sum = _sb_trap(item_id, "summary")
        slab = slab_row["result_json"] if slab_row else None
        llm = llm_row["result_json"] if llm_row else None
        prev_retry = 0
        if prev_sum and prev_sum.get("result_json"):
            try:
                prev_retry = int(prev_sum["result_json"].get("llm_retry_count") or 0)
            except Exception:
                pass
        return slab, llm, prev_retry
    # Pi-pad (bestaande logica)
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


def _save_trap(
    item_id: str,
    trap: str,
    result: dict,
    confidence: float | None = None,
    card_id: str | None = None,
) -> None:
    # Dispatcher: zelfde patroon als analyze.py (2026-09-07 gelijkgetrokken
    # t.b.v. cutover). KENSA_WRITE_STORAGE=supabase -> alleen Supabase-native.
    mode = os.environ.get("KENSA_WRITE_STORAGE", "sqlite")
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


def _mark_slab_status(item_id: str, status: str, card_key: str | None = None) -> None:
    # Dispatcher: zelfde patroon als analyze.py (2026-09-07 gelijkgetrokken).
    mode = os.environ.get("KENSA_WRITE_STORAGE", "sqlite")
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


# ---------------------------------------------------------------------------
# F1 — Load & guards  (baseline branches: B1, B2)
# ---------------------------------------------------------------------------
def _score_load_and_guard(
    item_id: str,
) -> tuple[dict, dict, dict | None, int] | dict:
    """Load listing + slab + llm_data + prev_retry_count.

    Returns:
      - error-dict `{"error": ...}` als een guard faalt (listing niet
        gevonden OF geen slab-data). Orkestrator retourneert die direct.
      - anders 4-tuple `(listing, slab, llm_data, prev_retry_count)`.
    """
    # (B1) — listing bestaat
    listing = _load_listing(item_id)
    if not listing:
        return {"error": f"listing {item_id} not found"}
    # (B2) — slab-OCR van fase A aanwezig
    slab, llm_data, prev_retry_count = _load_stored_slab_llm(item_id)
    if not slab:
        return {"error": "no slab data — run OCR phase first"}
    return (listing, slab, llm_data, prev_retry_count)


# ---------------------------------------------------------------------------
# F2 — Cache-lookup + mode-gates  (baseline branches: B3-B12)
# ---------------------------------------------------------------------------
def _score_cache_and_mode_gate(
    item_id: str,
    slab: dict,
    llm_data: dict | None,
    mode: str,
) -> tuple[bool, dict | None]:
    """Bepaal `has_cache` + evalueer mode-gates.

    Returns: `(has_cache, skip_result_dict_or_None)`.
      - Als `skip_result_dict` niet None → orkestrator retourneert die direct
        (geen enkele DB-write mag gebeuren).
      - `has_cache` wordt anders doorgegeven aan F6 voor `cache_hit`-veld en
        de verbose-log.
    """
    # (B3-B6) — card-identity cache-check
    card_key = _build_card_key(slab, llm_data)
    has_cache = False
    if card_key:
        pc = _price_cache_get(card_key)
        has_cache = bool(pc and _cache_is_fresh(pc.get("ebay_fetched_at")) and pc.get("ebay_result_json"))
    # (B7-B9) — cache_only mode: alleen doorgaan bij cache-hit
    if mode == "cache_only" and not has_cache:
        return has_cache, {"skip": "no cache hit", "item_id": item_id}
    # (B10-B12) — ebay_only mode: alleen doorgaan bij cache-miss
    if mode == "ebay_only" and has_cache:
        return has_cache, {"skip": "cache hit — laat over aan cache-worker", "item_id": item_id}
    return has_cache, None


# ---------------------------------------------------------------------------
# F3 — Regex-scoring  (geen extra branches — pure berekening)
# ---------------------------------------------------------------------------
def _score_regex(item_id: str, slab: dict, listing: dict) -> dict:
    """Draai `check_title` / `check_desc` / `compute_score` op de opgeslagen slab
    + listing-titel/description. Geen DB-writes, geen HTTP-calls.

    Returns dict met keys `title`, `desc`, `score` (elk het volledige sub-result).
    """
    tit = check_title(slab, listing.get("title_jp") or "", listing.get("title_en") or "")
    desc = check_desc(slab, listing.get("description_jp") or "", tit)
    score = compute_score(slab, tit, desc)
    return {"title": tit, "desc": desc, "score": score}


# ---------------------------------------------------------------------------
# F4 — eBay-fase + hoge-ROI retry  (baseline branches: B13-B28)
# ---------------------------------------------------------------------------
def _score_ebay_phase(
    item_id: str,
    slab: dict,
    listing: dict,
    llm_data: dict | None,
    verbose: bool,
) -> dict:
    """Bundle-guard + 0%-item guard + `run_ebay_phase_v2` + hoge-ROI retry-pad.

    Retourneert een bundle-dict `{"ebay", "is_bundle", "llm_data"}`:
      - `ebay`: return-value van `run_ebay_phase_v2`, of None als gestapt is
        (bundle-lot of 0%-item zonder LLM-data).
      - `is_bundle`: bool voor summary-veld.
      - `llm_data`: mogelijk bijgewerkt in de retry-pad (bij >50% ROI zonder
        LLM-data wordt Gemini alsnog geraadpleegd + `llm_slab`-trap
        weggeschreven en eBay-fase opnieuw gedraaid).
    """
    # (B13, B14) — Bundle-lot detectie
    is_bundle = bool(_looks_like_bundle(listing.get("title_jp"), listing.get("title_en")))
    if is_bundle:
        if verbose:
            print(f"  [ebay] skip: bundle-lot", file=sys.stderr)
        ebay = None
    # (B16, B17) — 0%-item guard: slab-fail zonder LLM = geen eBay
    elif slab.get("status") != "pass" and not llm_data:
        if verbose:
            print(f"  [ebay] skip: 0% item", file=sys.stderr)
        ebay = None
    # (B19) — normale eBay-fase via v2-helper
    else:
        ebay = run_ebay_phase_v2(slab, listing, do_ebay=True, verbose=verbose, llm_data=llm_data)

    # (B20-B28) — Hoge-ROI verify-pad: zonder LLM-data + winst_pct>50 → force LLM
    if llm_data is None and ebay and ebay.get("stats") and listing.get("price_eur"):
        llm_data = _score_high_roi_retry(item_id, slab, listing, ebay, verbose)
        if llm_data is not None:
            ebay = run_ebay_phase_v2(slab, listing, do_ebay=True, verbose=verbose, llm_data=llm_data)

    return {"ebay": ebay, "is_bundle": is_bundle, "llm_data": llm_data}


def _score_high_roi_retry(
    item_id: str,
    slab: dict,
    listing: dict,
    ebay: dict,
    verbose: bool,
) -> dict | None:
    """Sub-stap van F4 — hoge-ROI LLM-retry (apart om F4's CC laag te houden).

    Berekent tijdelijk-ROI; alleen als winst_pct > 50 wordt Gemini aangeroepen
    (lazy-import ivm circular-import) + `llm_slab`-trap weggeschreven.
    Return: verse `llm_data`-dict of `None` (geen retry, of LLM faalde).
    """
    _tmp_roi = compute_roi(listing["price_eur"], ebay["stats"])
    _pct = ((_tmp_roi or {}).get("scenarios", {}).get("avg3") or {}).get("winst_pct")
    if _pct is None or _pct <= 50:
        return None
    if verbose:
        print(f"  [llm] verify-hoge-roi ({_pct:.0f}%) — force LLM check", file=sys.stderr)
    # Lazy-import: `_llm_enrich_slab` zit in analyze.py en heeft vision-deps.
    from analyze import _llm_enrich_slab
    llm_data = _llm_enrich_slab(item_id, slab, listing, verbose=verbose)
    if llm_data:
        _save_trap(item_id, "llm_slab", llm_data)
        return llm_data
    return None


# ---------------------------------------------------------------------------
# F5 — Post-processing  (baseline branches: B29-B35)
# ---------------------------------------------------------------------------
def _score_post_process(
    item_id: str,
    slab: dict,
    llm_data: dict | None,
    ebay: dict | None,
    listing: dict,
    tit: dict,
    desc: dict,
    verbose: bool,
) -> dict:
    """Cert-sighting + ROI + trap-writes.

    Zelfde volgorde als origineel:
      1. cert_sighting (indien slab.cert)
      2. _save_trap title_match
      3. _save_trap desc_match
      4. _save_trap ebay_prices (indien ebay.query)
      5. compute_roi + _save_trap roi (indien stats+price_eur+truthy)

    Cardmarket-enqueue zit hier sinds 2026-09-07 NIET meer in: die beslissing
    heeft één eigenaar, cm_bevoorrader.py (cron */3). Workers scoren alleen.

    Returns dict `{"sighting", "roi_data"}` voor F6.
    """
    # (B29) — cert-sighting
    sighting = None
    if slab.get("cert"):
        sighting = record_cert_sighting(slab["cert"], item_id, listing.get("seller_id"))

    # Trap-writes — volgorde exact als origineel
    _save_trap(item_id, "title_match", tit)
    _save_trap(item_id, "desc_match", desc)
    # (B30, B31) — ebay_prices trap
    if ebay and ebay.get("query"):
        _save_trap(item_id, "ebay_prices", ebay, card_id=slab.get("cert"))

    # (B32-B35) — ROI-berekening + trap
    roi_data = None
    if ebay and ebay.get("stats") and listing.get("price_eur"):
        roi_data = compute_roi(listing["price_eur"], ebay["stats"])
        if roi_data:
            _save_trap(item_id, "roi", roi_data, card_id=slab.get("cert"))

    # Cardmarket-enqueue is verhuisd naar cm_bevoorrader.py (één beslisser).
    return {"sighting": sighting, "roi_data": roi_data}


# ---------------------------------------------------------------------------
# F6 — Summary-write + status-flip  (baseline branches: B36-B41)
# ---------------------------------------------------------------------------
def _score_summary_write(
    item_id: str,
    listing: dict,
    slab: dict,
    ebay: dict | None,
    roi_data: dict | None,
    tit: dict,
    desc: dict,
    score: dict,
    sighting: dict | None,
    is_bundle: bool,
    llm_needed_but_failed: bool,
    prev_retry_count: int,
    took_ms: int,
    mode: str,
    verbose: bool,
    has_cache: bool,
) -> dict:
    """Bouw summary-dict, schrijf `trap='summary'`, flip `slab_status='analyzed'`.

    Return: de summary-dict (nodig voor de orkestrator-return).
    """
    # (B36-B40) — short-circuit expressies voor optional velden
    summary = {
        "score": score["score"], "verdict": score["verdict"],
        "base": score["base"], "delta_title": score["delta_title"], "delta_desc": score["delta_desc"],
        "reasons": score["reasons"],
        "check1_status": slab["status"], "check2_status": tit["status"], "check3_status": desc["status"],
        "cert": slab.get("cert"),
        "cert_times_seen": (sighting or {}).get("times_seen", 0),
        "cert_first_seen": (sighting or {}).get("first_seen_at"),
        "ocr_calls": slab.get("ocr_calls", 0),
        "photos_tried": slab.get("photos_tried", 0),
        "took_ms": took_ms,
        "ebay_query": (ebay or {}).get("query"),
        "ebay_stats": (ebay or {}).get("stats"),
        "ebay_from_cache": (ebay or {}).get("from_cache"),
        "roi_avg3_pct": ((roi_data or {}).get("scenarios", {}).get("avg3") or {}).get("winst_pct"),
        "roi_avg3_eur": ((roi_data or {}).get("scenarios", {}).get("avg3") or {}).get("winst_eur"),
        "llm_needed_but_failed": llm_needed_but_failed,
        "llm_retry_count": prev_retry_count,
        "is_bundle": is_bundle,
        "query_source": (ebay or {}).get("query_source", "regex"),
        "score_phase_mode": mode,
    }
    _save_trap(item_id, "summary", summary, confidence=float(score["score"]), card_id=slab.get("cert"))
    _mark_slab_status(item_id, "analyzed")
    # (B41) — verbose log-regel
    if verbose:
        print(f"[{item_id}] SCORE-phase({mode}) score={summary['score']}% {summary['verdict']} "
              f"cache={has_cache} {took_ms}ms", file=sys.stderr)
    return summary


# ---------------------------------------------------------------------------
# Orkestrator — signature identiek aan `analyze_score_only`
# ---------------------------------------------------------------------------
def analyze_score_only_v2(item_id: str, verbose: bool = False, mode: str = "auto") -> dict:
    """Refactor-orkestrator van `analyze_score_only`.

    Zelfde inputs, zelfde outputs, zelfde volgorde van DB-writes, HTTP-calls
    en stderr-prints. Splitst de logica in 6 sub-functies zonder semantiek-drift.

    mode:
      - 'auto'       : normaal — eBay-fetch of cache, wat nodig is
      - 'cache_only' : alleen als cache-hit — anders skip (blijft ocr_done)
      - 'ebay_only'  : draai altijd live eBay (voor cache-misses)
    """
    # F1 — Load & guards
    loaded = _score_load_and_guard(item_id)
    if isinstance(loaded, dict):
        return loaded
    listing, slab, llm_data, prev_retry_count = loaded

    # F2 — Cache-lookup + mode-gates
    has_cache, skip_result = _score_cache_and_mode_gate(item_id, slab, llm_data, mode)
    if skip_result is not None:
        return skip_result

    # F3 — Regex-scoring (start timer voor took_ms — zelfde punt als origineel)
    t0 = time.perf_counter()
    regex = _score_regex(item_id, slab, listing)
    tit, desc, score = regex["title"], regex["desc"], regex["score"]

    # F4 — eBay-fase + hoge-ROI retry
    ebay_bundle = _score_ebay_phase(item_id, slab, listing, llm_data, verbose)
    ebay = ebay_bundle["ebay"]
    is_bundle = ebay_bundle["is_bundle"]
    llm_data = ebay_bundle["llm_data"]  # kan bijgewerkt zijn in retry-pad
    # In het origineel is `llm_needed_but_failed` altijd False in deze functie.
    llm_needed_but_failed = False

    took_ms = int((time.perf_counter() - t0) * 1000)

    # F5 — Post-processing (cert-sighting + traps + ROI + CM-enqueue)
    post = _score_post_process(item_id, slab, llm_data, ebay, listing, tit, desc, verbose)
    sighting = post["sighting"]
    roi_data = post["roi_data"]

    # F6 — Summary-write + status-flip
    summary = _score_summary_write(
        item_id, listing, slab, ebay, roi_data, tit, desc, score,
        sighting, is_bundle, llm_needed_but_failed, prev_retry_count,
        took_ms, mode, verbose, has_cache,
    )

    return {
        "slab": slab, "title": tit, "desc": desc, "score": score, "summary": summary,
        "item_id": item_id, "phase_mode": mode, "cache_hit": has_cache,
    }
