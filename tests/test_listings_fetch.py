"""Blok A3 — backwards-compat tests voor storage_supabase.listings.fetch_*"""
from __future__ import annotations

from storage_supabase.listings import fetch_listing, fetch_needing_detail
from tests._pi_helpers import pi_one

BASELINE_ITEM = "m24912557313"  # Mega Gardevoir ex MA PSA10


def test_fetch_listing_baseline():
    row = fetch_listing(BASELINE_ITEM)
    assert row is not None
    assert row["item_id"] == BASELINE_ITEM
    assert row["price_jpy"] == 5600
    # slab_status kan veranderd zijn naarmate pipeline verder gaat (ocr_done → analyzed → …)
    assert row["slab_status"] in ("ocr_done", "analyzed", "ebay_done", "final")
    assert row["card_key"] == "gardevoir:226/193:10:m2a"
    assert row["detail_scraped_at"] is not None
    assert row["first_seen_at"] is not None
    assert "メガサーナイト" in row["title_jp"]


def test_fetch_listing_nonexistent():
    row = fetch_listing("m00000000000")
    assert row is None


def test_fetch_needing_detail_shape():
    items = fetch_needing_detail(limit=50)
    assert isinstance(items, list)
    for iid in items:
        assert isinstance(iid, str)
        assert len(iid) >= 3, f"item_id te kort: {iid!r}"


def test_pi_matches_supabase_listing():
    pi_row = pi_one(
        "SELECT item_id, title_jp, price_jpy, slab_status, card_key, "
        "detail_scraped_at, first_seen_at "
        "FROM listings WHERE item_id = ?", (BASELINE_ITEM,)
    )
    sb_row = fetch_listing(BASELINE_ITEM)
    assert pi_row is not None
    assert sb_row is not None
    for col in ("item_id", "title_jp", "price_jpy", "slab_status", "card_key"):
        assert pi_row[col] == sb_row[col], f"mismatch op {col}"
