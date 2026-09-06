"""analyze_full.py — refactor-split van `analyze()` uit `../analyze.py`
(regel 816, CC 51).

Acht sub-functies (F1-F8), plus een orkestrator `analyze_v2` die exact hetzelfde
gedrag oplevert als het origineel:
zelfde DB-writes, zelfde volgorde van `_save_trap`-calls (per baseline-doc),
zelfde stderr-prints, identiek return-dict shape.

Helpers hergebruikt uit vorige splits (DRY — geen re-implementatie):
  - `_load_listing`, `_save_trap`, `_score_regex` (uit `score_only.py`)
  - `run_ebay_phase_v2`                            (uit `ebay_phase.py`)
  - `_looks_like_bundle`,
    `enqueue_cardmarket_if_possible_v2`            (uit `enqueue_cardmarket.py`)

Kritieke analyze-afwijkingen t.o.v. `analyze_score_only_v2`:
  1. F2 doet OOK `trap='slab_ocr'` write (score_only slaat die over — die trap
     komt uit fase A).
  2. GEEN `_mark_slab_status('analyzed')` call — `analyze()` zet slab_status niet.
  3. `llm_retry_count = prev+1 if llm_needed_but_failed else prev`
     (score_only geeft altijd `prev` door).
  4. `llm_needed_but_failed`-vlag telt ALLEEN voor initiële enrichment (F2),
     niet voor hoge-ROI verify-retry in F5.
  5. Return-dict = `{slab, title, desc, score, summary}` — GEEN `mode`,
     `cache_hit` of `phase_mode` velden.

`check_slab`, `_needs_llm` en `_llm_enrich_slab` worden lazy-geïmporteerd uit
`analyze` om circular-imports te voorkomen én de `KENSA_SLAB_HYBRID`-env-toggle
voor de hybrid slab-checker te respecteren (analyze.py resolvet die binding
al bij import).

Zie `../analyze_refactor_fixtures_analyze/BASELINE_ANALYSIS.md` voor de
43 branch-punten en 8-fase indeling.
"""

import json
import sqlite3
import sys
import time
from pathlib import Path

from roi import compute as compute_roi
from storage import record_cert_sighting

from analyze_split.ebay_phase import run_ebay_phase_v2
from analyze_split.enqueue_cardmarket import (
    _looks_like_bundle,
    enqueue_cardmarket_if_possible_v2,
)
from analyze_split.score_only import (
    _load_listing,
    _save_trap,
    _score_regex,
)


# ---------------------------------------------------------------------------
# Config-constants (gekopieerd uit analyze.py — dezelfde waarden)
# ---------------------------------------------------------------------------
_KENSA_DIR = Path(__file__).resolve().parent.parent
DB_PATH = _KENSA_DIR / "kensa.db"


# ---------------------------------------------------------------------------
# F1 — Load & guard  (baseline-fase F1, regels 817-819 + 831-841)
# ---------------------------------------------------------------------------
def _af_load_and_guard(item_id: str) -> dict:
    """Laad listing + `prev_retry_count` uit vorige summary-trap.

    Returns:
      - `{"error": "..."}` — orkestrator retourneert die direct
      - anders `{"listing": <row>, "prev_retry_count": <int>}`
    """
    listing = _load_listing(item_id)
    if not listing:
        return {"error": f"listing {item_id} not found"}
    prev_retry_count = 0

    # Dispatcher: Supabase-read als env-flag aan, anders SQLite (Pi).
    from analyze_split.score_only import _read_via_supabase
    if _read_via_supabase():
        from storage_supabase.analysis import fetch_latest_trap as _sb_trap
        row = _sb_trap(item_id, "summary")
        if row and row.get("result_json"):
            try:
                prev_retry_count = int(row["result_json"].get("llm_retry_count") or 0)
            except Exception:
                pass
        return {"listing": listing, "prev_retry_count": prev_retry_count}

    conn = sqlite3.connect(str(DB_PATH))
    try:
        row = conn.execute(
            "SELECT result_json FROM analysis WHERE item_id=? AND trap='summary' "
            "ORDER BY analysis_id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
    finally:
        conn.close()
    if row and row[0]:
        try:
            prev_retry_count = int(json.loads(row[0]).get("llm_retry_count") or 0)
        except Exception:
            pass
    return {"listing": listing, "prev_retry_count": prev_retry_count}


# ---------------------------------------------------------------------------
# F2 + F3 — Slab-OCR + LLM-tier enrichment  (baseline-fase F2/F3, regels 821-858)
# ---------------------------------------------------------------------------
def _af_slab_ocr_and_llm(
    item_id: str, listing: dict, verbose: bool,
) -> tuple[dict, dict | None, bool]:
    """Live `check_slab()` + optionele Gemini-enrichment.

    Schrijft traps in deze volgorde:
      1. `trap='llm_slab'` — alleen als LLM een geldig resultaat gaf
      2. `trap='slab_ocr'` — altijd (confidence 100 bij pass, 0 anders)

    Returns `(slab, llm_data, llm_needed_but_failed)`:
      - `slab`: dict uit `check_slab(item_id, max_photos=2)`
      - `llm_data`: dict of None (None = gate dicht óf 0%-skip óf LLM faalde)
      - `llm_needed_but_failed`: True enkel als gate open was, slab.status=pass
        én `_llm_enrich_slab` None retourneerde (429/parse/etc). Retry-marker
        voor volgende cron-run.
    """
    # Lazy-import om circular-import + env-toggle-drift te vermijden.
    from analyze import _llm_enrich_slab, _needs_llm, check_slab

    slab = check_slab(item_id, max_photos=2)

    llm_data = None
    llm_needed_but_failed = False
    needs, reason = _needs_llm(slab)
    # Gate open MAAR slab-OCR mislukte → LLM zou op ruis draaien → skip
    if needs and slab.get("status") != "pass":
        if verbose:
            print(
                f"  [llm] gate open maar slab.status={slab.get('status')} "
                f"→ skip LLM (0%-item)",
                file=sys.stderr,
            )
    elif needs:
        if verbose:
            print(f"  [llm] gate open — {reason}", file=sys.stderr)
        llm_data = _llm_enrich_slab(item_id, slab, listing, verbose=verbose)
        if llm_data:
            _save_trap(item_id, "llm_slab", llm_data)
        else:
            # LLM was nodig maar faalde — vlag voor retry in volgende cron
            llm_needed_but_failed = True
            if verbose:
                print("  [llm] gefaald — item gemarkeerd voor retry", file=sys.stderr)

    _save_trap(
        item_id,
        "slab_ocr",
        slab,
        confidence=100.0 if slab["status"] == "pass" else 0.0,
        card_id=slab.get("cert"),
    )
    return (slab, llm_data, llm_needed_but_failed)


# ---------------------------------------------------------------------------
# F4 — Bundle + 0%-guard + eBay-phase  (baseline-fase F4, regels 860-876)
# ---------------------------------------------------------------------------
def _af_ebay_phase_with_toggle(
    item_id: str,
    slab: dict,
    listing: dict,
    llm_data: dict | None,
    do_ebay: bool,
    verbose: bool,
) -> dict:
    """Bundle-detectie + 0%-item guard + `run_ebay_phase_v2`.

    Returns bundle-dict `{"ebay", "is_bundle"}`:
      - `ebay`: dict van v2-helper, óf None (bundle/0%-skip, óf do_ebay=False
        gefiltered door v2-helper intern)
      - `is_bundle`: bool — altijd berekend want summary heeft dit veld nodig

    NB: `is_bundle` wordt ALTIJD berekend (ook bij do_ebay=False) — anders zou
    de dashboard-vlag ontbreken bij een --no-ebay run.
    """
    is_bundle = bool(_looks_like_bundle(listing.get("title_jp"), listing.get("title_en")))
    if is_bundle:
        if verbose:
            print(
                "  [ebay] skip: bundle-lot → geen zinvolle single-card ROI mogelijk",
                file=sys.stderr,
            )
        ebay = None
    elif slab.get("status") != "pass" and not llm_data:
        if verbose:
            print(
                "  [ebay] skip: slab niet betrouwbaar EN LLM leverde niks — 0% item",
                file=sys.stderr,
            )
        ebay = None
    else:
        ebay = run_ebay_phase_v2(slab, listing, do_ebay, verbose, llm_data=llm_data)
    return {"ebay": ebay, "is_bundle": is_bundle}


# ---------------------------------------------------------------------------
# F5 — Hoge-ROI verify-retry  (baseline-fase F5, regels 878-890)
# ---------------------------------------------------------------------------
def _af_high_roi_retry(
    item_id: str,
    slab: dict,
    listing: dict,
    llm_data: dict | None,
    ebay: dict | None,
    do_ebay: bool,
    verbose: bool,
) -> tuple[dict | None, dict | None]:
    """Force LLM-double-check als regex-tier deal met winst_pct > 50% oplevert
    ZONDER dat LLM al is geraadpleegd (te-goed-om-waar-te-zijn detector).

    Returns `(new_ebay, new_llm_data)`:
      - `(None, None)` → geen retry gedraaid, óf retry faalde → caller houdt
        bestaande waarden
      - `(dict, dict)` → LLM slaagde + eBay-fase opnieuw gedraaid met LLM-query
        + LLM-judge → caller vervangt beide

    NB: deze functie past `llm_needed_but_failed` NIET aan. De vlag geldt enkel
    voor de initiële enrichment (F2); een falende hoge-ROI retry telt niet mee
    voor de cron-retry-cap.
    """
    # Guard: alleen doorgaan als LLM nog niet is gebruikt in F2
    if llm_data is not None:
        return (None, None)
    # Guard: ebay-basis + prijs moeten aanwezig zijn voor tmp-ROI-berekening
    if not (ebay and ebay.get("stats") and listing.get("price_eur")):
        return (None, None)

    _tmp_roi = compute_roi(listing["price_eur"], ebay["stats"])
    _pct = ((_tmp_roi or {}).get("scenarios", {}).get("avg3") or {}).get("winst_pct")
    if _pct is None or _pct <= 50:
        return (None, None)

    if verbose:
        print(f"  [llm] verify-hoge-roi ({_pct:.0f}%) — force LLM check", file=sys.stderr)
    # Lazy-import ivm circular-import (analyze.py importeert dit module).
    from analyze import _llm_enrich_slab
    new_llm = _llm_enrich_slab(item_id, slab, listing, verbose=verbose)
    if not new_llm:
        return (None, None)
    _save_trap(item_id, "llm_slab", new_llm)
    # Draai eBay-fase OPNIEUW met LLM-query en LLM-judge
    new_ebay = run_ebay_phase_v2(slab, listing, do_ebay, verbose, llm_data=new_llm)
    return (new_ebay, new_llm)


# ---------------------------------------------------------------------------
# F6 — Cert-sighting + trap-writes  (baseline-fase F6, regels 893-902)
# ---------------------------------------------------------------------------
def _af_cert_and_traps(
    item_id: str,
    slab: dict,
    listing: dict,
    tit: dict,
    desc: dict,
    score: dict,
    ebay: dict | None,
) -> dict | None:
    """Record cert-sighting + schrijf `title_match`, `desc_match`, `ebay_prices`
    traps (in die volgorde — matcht origineel wall-clock).

    NB: `trap='slab_ocr'` is AL geschreven in F2 — hier NIET dupliceren!
    `score` staat bewust in de signature voor eventuele latere sighting-
    confidence-koppeling; nu ongebruikt (dead-arg = OK, geen semantiek-drift).

    Returns: sighting-dict van `record_cert_sighting`, óf None (geen cert).
    """
    sighting = None
    if slab.get("cert"):
        sighting = record_cert_sighting(slab["cert"], item_id, listing.get("seller_id"))

    _save_trap(item_id, "title_match", tit)
    _save_trap(item_id, "desc_match", desc)
    if ebay and ebay.get("query"):
        _save_trap(item_id, "ebay_prices", ebay, card_id=slab.get("cert"))
    return sighting


# ---------------------------------------------------------------------------
# F7 — ROI + Cardmarket-enqueue  (baseline-fase F7, regels 903-911)
# ---------------------------------------------------------------------------
def _af_roi_and_enqueue(
    item_id: str,
    slab: dict,
    listing: dict,
    llm_data: dict | None,
    ebay: dict | None,
    verbose: bool,
) -> dict | None:
    """Compute ROI + trap-write, dan Cardmarket-queue (idempotent upsert).

    Returns: roi_data dict (voor summary-veld), óf None (geen ebay-stats
    óf geen price_eur óf `compute_roi` gaf falsy).

    Deze is bewust NIET hergebruikt van `_score_post_process` omdat die óók
    cert-sighting + title/desc/ebay_prices traps schrijft — die zitten al in
    F6. Hier alleen ROI + enqueue om trap-ordering identiek te houden.
    """
    roi_data = None
    if ebay and ebay.get("stats") and listing.get("price_eur"):
        roi_data = compute_roi(listing["price_eur"], ebay["stats"])
        if roi_data:
            _save_trap(item_id, "roi", roi_data, card_id=slab.get("cert"))
    # Cardmarket-enqueue — interne guards kunnen zelf skippen
    enqueue_cardmarket_if_possible_v2(item_id, slab, llm_data, verbose, listing=listing)
    return roi_data


# ---------------------------------------------------------------------------
# F8 — Summary-write + verbose log  (baseline-fase F8, regels 913-945)
# ---------------------------------------------------------------------------
def _af_summary_write(
    item_id: str,
    listing: dict,
    slab: dict,
    llm_data: dict | None,
    tit: dict,
    desc: dict,
    score: dict,
    ebay: dict | None,
    roi_data: dict | None,
    sighting: dict | None,
    is_bundle: bool,
    llm_needed_but_failed: bool,
    prev_retry_count: int,
    took_ms: int,
    verbose: bool,
) -> dict:
    """Bouw summary-dict, schrijf `trap='summary'`, print verbose log-regel.

    GEEN `_mark_slab_status('analyzed')` call — `analyze()` zet slab_status
    bewust niet (score_only doet dat wel).

    `llm_retry_count`-logica (analyze-specifiek):
      - `prev + 1` als F2's `_llm_enrich_slab` faalde (retry-cap ticker)
      - `prev`     in alle andere gevallen

    `listing` en `llm_data` staan in de signature voor consistentie met de
    orkestrator-call en toekomstige uitbreiding; niet direct in de dict
    gebruikt (dead-args = OK, geen semantiek-drift).
    """
    summary = {
        "score": score["score"],
        "verdict": score["verdict"],
        "base": score["base"],
        "delta_title": score["delta_title"],
        "delta_desc": score["delta_desc"],
        "reasons": score["reasons"],
        "check1_status": slab["status"],
        "check2_status": tit["status"],
        "check3_status": desc["status"],
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
        "llm_retry_count": (prev_retry_count + 1) if llm_needed_but_failed else prev_retry_count,
        "is_bundle": is_bundle,
        "query_source": (ebay or {}).get("query_source", "regex"),
    }
    _save_trap(item_id, "summary", summary, confidence=float(score["score"]), card_id=slab.get("cert"))

    if verbose:
        print(
            f"[{item_id}] score={summary['score']}% {summary['verdict']} "
            f"(c1={summary['check1_status']}/c2={summary['check2_status']}"
            f"/c3={summary['check3_status']}) "
            f"cert={summary['cert']} seen={summary['cert_times_seen']}x "
            f"ocr={summary['ocr_calls']} {took_ms}ms"
        )
    return summary


# ---------------------------------------------------------------------------
# Orkestrator — signature IDENTIEK aan `analyze`
# ---------------------------------------------------------------------------
def analyze_v2(item_id: str, verbose: bool = False, do_ebay: bool = True) -> dict:
    """Refactor-orkestrator van `analyze` (`analyze.py:816`).

    Zelfde inputs, zelfde outputs, zelfde volgorde van DB-writes, HTTP-calls
    en stderr-prints als het origineel. Splitst logica in 8 sub-functies
    zonder semantiek-drift.

    Args:
      item_id  : PK van `listings` (Buyee/Mercari/PayPay Flea Market item)
      verbose  : stderr-prints aan/uit
      do_ebay  : doorgegeven aan `run_ebay_phase_v2` — --no-ebay CLI-flag
                 skipt live/cache fetch

    Returns:
      - `{"error": "..."}` als listing niet bestaat
      - anders `{"slab", "title", "desc", "score", "summary"}`
    """
    # F1 — Load & guard
    loaded = _af_load_and_guard(item_id)
    if "error" in loaded:
        return loaded
    listing = loaded["listing"]
    prev_retry_count = loaded["prev_retry_count"]

    t0 = time.perf_counter()

    # F2 + F3 — Slab-OCR + LLM enrichment (schrijft traps 'llm_slab' + 'slab_ocr')
    slab, llm_data, llm_needed_but_failed = _af_slab_ocr_and_llm(item_id, listing, verbose)

    # Regex-scoring (title/desc/score) — hergebruikt uit score_only helper
    regex = _score_regex(item_id, slab, listing)
    tit, desc, score = regex["title"], regex["desc"], regex["score"]

    # F4 — Bundle + 0%-guard + eBay
    ebay_bundle = _af_ebay_phase_with_toggle(item_id, slab, listing, llm_data, do_ebay, verbose)
    ebay = ebay_bundle["ebay"]
    is_bundle = ebay_bundle["is_bundle"]

    # F5 — Hoge-ROI verify-retry (mogelijk update llm_data + ebay)
    new_ebay, new_llm = _af_high_roi_retry(
        item_id, slab, listing, llm_data, ebay, do_ebay, verbose,
    )
    if new_llm is not None:
        llm_data = new_llm
        ebay = new_ebay

    took_ms = int((time.perf_counter() - t0) * 1000)

    # F6 — Cert-sighting + trap-writes (title_match / desc_match / ebay_prices)
    sighting = _af_cert_and_traps(item_id, slab, listing, tit, desc, score, ebay)

    # F7 — ROI-berekening + Cardmarket-enqueue
    roi_data = _af_roi_and_enqueue(item_id, slab, listing, llm_data, ebay, verbose)

    # F8 — Summary-write + verbose log
    summary = _af_summary_write(
        item_id, listing, slab, llm_data, tit, desc, score,
        ebay, roi_data, sighting, is_bundle,
        llm_needed_but_failed, prev_retry_count, took_ms, verbose,
    )

    return {"slab": slab, "title": tit, "desc": desc, "score": score, "summary": summary}
