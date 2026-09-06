"""Blok A2 — backwards-compat tests voor storage_supabase.analysis.fetch_*

Baseline-item: m24912557313 (Mega Gardevoir ex, gescand 2026-09-05 07:04).
Op moment van schrijven heeft dit item 7 traps met bekende analysis_id's.
Als het item ooit verwijderd wordt uit Supabase: pak een ander volledig
verwerkt item en werk BASELINE-constants bij.
"""
from __future__ import annotations
import json

from storage_supabase.analysis import fetch_latest_trap, fetch_all_traps
from tests._pi_helpers import pi_one, pi_all

BASELINE_ITEM = "m24912557313"

# Bekend uit Supabase op moment van schrijven (analysis_id's van meest recente per trap):
BASELINE_LATEST_IDS = {
    "summary": 1605497,
    "roi": 1605496,
    "ebay_prices": 1605495,
    "desc_match": 1605494,
    "title_match": 1605493,
    "slab_ocr": 1605195,
    "llm_slab": 1605193,
}


def test_fetch_latest_slab_ocr_baseline():
    row = fetch_latest_trap(BASELINE_ITEM, "slab_ocr")
    assert row is not None, "baseline item mist slab_ocr — is 't verwijderd?"
    assert row["analysis_id"] == BASELINE_LATEST_IDS["slab_ocr"]
    assert row["item_id"] == BASELINE_ITEM
    assert row["trap"] == "slab_ocr"
    assert float(row["confidence"]) == 100.0
    assert isinstance(row["result_json"], dict)
    # slab_ocr fingerprint voor deze kaart:
    assert row["result_json"].get("grade") == "10"
    assert row["result_json"].get("number") == "226/193"
    assert row["result_json"].get("status") == "pass"


def test_fetch_latest_summary_baseline():
    row = fetch_latest_trap(BASELINE_ITEM, "summary")
    assert row is not None
    assert row["analysis_id"] == BASELINE_LATEST_IDS["summary"]
    assert float(row["confidence"]) == 65.0


def test_fetch_latest_nonexistent_returns_none():
    row = fetch_latest_trap(BASELINE_ITEM, "trap-does-not-exist")
    assert row is None
    row = fetch_latest_trap("m00000000000", "slab_ocr")
    assert row is None


def test_fetch_all_traps_count_and_order():
    rows = fetch_all_traps(BASELINE_ITEM)
    # Baseline: minstens de 7 bekende traps + evt latere reruns
    assert len(rows) >= 7
    # DESC-order: eerste rij is meest recente
    assert rows[0]["analysis_id"] >= rows[-1]["analysis_id"]
    # Alle bekende trap-namen aanwezig
    trap_names = {r["trap"] for r in rows}
    for expected in BASELINE_LATEST_IDS:
        assert expected in trap_names, f"trap {expected} ontbreekt in fetch_all_traps"


def test_pi_matches_supabase_all_traps():
    """Parallel-consistency: voor elke trap moet Pi's result_json exact matchen met Supabase."""
    for trap in ("slab_ocr", "llm_slab", "summary", "ebay_prices", "roi", "desc_match", "title_match"):
        pi_row = pi_one(
            "SELECT result_json, confidence, card_id FROM analysis "
            "WHERE item_id=? AND trap=? ORDER BY analysis_id DESC LIMIT 1",
            (BASELINE_ITEM, trap),
        )
        sb_row = fetch_latest_trap(BASELINE_ITEM, trap)
        assert pi_row is not None, f"Pi mist trap {trap} voor {BASELINE_ITEM}"
        assert sb_row is not None, f"Supabase mist trap {trap} voor {BASELINE_ITEM}"
        # Pi slaat jsonb als text op, Supabase als native dict — parse Pi voor vergelijk
        pi_json = json.loads(pi_row["result_json"])
        assert pi_json == sb_row["result_json"], f"result_json mismatch op trap={trap}"
        # confidence en card_id direct
        assert (str(pi_row["confidence"]) if pi_row["confidence"] is not None else None) == (
            str(sb_row["confidence"]) if sb_row["confidence"] is not None else None
        ), f"confidence mismatch op trap={trap}"
