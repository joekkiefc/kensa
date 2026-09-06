"""Blok A1 — backwards-compat tests voor storage_supabase.price_cache.

Twee lagen:
  1. Baseline-fingerprint: bekende card_key uit Supabase, exacte waardes checken
  2. Parallel-consistency: Pi SQLite vs Supabase moeten identiek zijn

Baseline-item is gekozen op 2026-09-05 uit live Supabase: pikachu:223/284:10
Als deze rij ooit uit de DB wordt gewist verandert de test — schrijf dan een
nieuwe baseline op basis van een actuele known-good rij.
"""
from __future__ import annotations
import json
import pytest

from storage_supabase.price_cache import (
    fetch_by_card_key, is_ebay_fresh, is_cm_fresh,
)
from tests._pi_helpers import pi_one

BASELINE_CARD_KEY = "pikachu:223/284:10"


def test_baseline_fingerprint_pikachu_vmax():
    """Bekende live-rij: alle kernvelden moeten matchen."""
    row = fetch_by_card_key(BASELINE_CARD_KEY)
    assert row is not None, f"baseline card_key {BASELINE_CARD_KEY} niet gevonden — is 't verwijderd?"
    assert row["card_key"] == BASELINE_CARD_KEY
    assert row["ebay_query"] == "Pikachu VMAX 223 PSA 10"
    assert row["ebay_fetched_at"] is not None
    assert row["cm_fetched_at"] is not None
    assert row["cm_url"].startswith("https://www.cardmarket.com")
    # jsonb-typen: ebay is dict, cm is list
    assert isinstance(row["ebay_result_json"], dict), (
        f"ebay_result_json moet dict zijn, kreeg {type(row['ebay_result_json'])}"
    )
    assert isinstance(row["cm_listings_json"], list), (
        f"cm_listings_json moet list zijn, kreeg {type(row['cm_listings_json'])}"
    )


def test_freshness_helpers():
    row = fetch_by_card_key(BASELINE_CARD_KEY)
    # 999u TTL: alles is nog fresh
    assert is_ebay_fresh(row, ttl_hours=999) is True
    assert is_cm_fresh(row, ttl_hours=999) is True
    # 0u TTL: niets is fresh
    assert is_ebay_fresh(row, ttl_hours=0) is False
    assert is_cm_fresh(row, ttl_hours=0) is False
    # None-row is nooit fresh
    assert is_ebay_fresh(None) is False
    assert is_cm_fresh(None) is False


def test_fetch_nonexistent_returns_none():
    row = fetch_by_card_key("this-card-does-not-exist-999999:xxx:99")
    assert row is None


@pytest.mark.parametrize("card_key", [BASELINE_CARD_KEY])
def test_pi_matches_supabase(card_key):
    """Parallel-consistency: Pi SQLite en Supabase moeten dezelfde rij hebben."""
    pi_row = pi_one(
        "SELECT card_key, ebay_query, ebay_result_json, cm_url, cm_listings_json "
        "FROM price_cache WHERE card_key = ?",
        (card_key,),
    )
    sb_row = fetch_by_card_key(card_key)
    assert pi_row is not None, f"Pi mist card_key {card_key}"
    assert sb_row is not None, f"Supabase mist card_key {card_key}"
    assert pi_row["card_key"] == sb_row["card_key"]
    assert pi_row["ebay_query"] == sb_row["ebay_query"]
    assert pi_row["cm_url"] == sb_row["cm_url"]
    # Pi slaat jsonb als string op, Supabase als native — parse Pi
    pi_ebay = json.loads(pi_row["ebay_result_json"]) if pi_row["ebay_result_json"] else None
    pi_cm = json.loads(pi_row["cm_listings_json"]) if pi_row["cm_listings_json"] else None
    assert pi_ebay == sb_row["ebay_result_json"], "ebay_result_json mismatch Pi vs Supabase"
    assert pi_cm == sb_row["cm_listings_json"], "cm_listings_json mismatch Pi vs Supabase"
