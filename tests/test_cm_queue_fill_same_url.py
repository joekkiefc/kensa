"""Test voor cm_queue_api._sb_fill_same_url — zelfde Cardmarket-URL, één ophaling.

Draait tegen de Supabase test-tabel kensa.test_cardmarket_queue (zelfde
conventie als test_enqueue_cardmarket.py). price_cache.upsert_cm wordt
gemonkeypatcht zodat er geen testsleutels in de echte price_cache belanden.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import cm_queue_api
from storage_supabase import cardmarket_queue as sb_cmq

URL_A = "https://www.cardmarket.com/en/Pokemon/Products/Singles/Test-Set/Testmon-V1-t001?language=7&minCondition=1"
URL_B = "https://www.cardmarket.com/en/Pokemon/Products/Singles/Test-Set/Testmon-V2-t002?language=7&minCondition=1"
IDS = ("testfill_done", "testfill_same_url", "testfill_other_url")


@pytest.fixture
def rows():
    for i in IDS:
        sb_cmq.delete_test_item(i)
    sb_cmq.enqueue_test_fresh("testfill_done", URL_A, "10", "testmon:001:10")
    sb_cmq.enqueue_test_fresh("testfill_same_url", URL_A, "10", "testmon:001/100:10")
    sb_cmq.enqueue_test_fresh("testfill_other_url", URL_B, "10", "testmon:002:10")
    yield
    for i in IDS:
        sb_cmq.delete_test_item(i)


def test_fill_same_url_vult_alleen_wachtende_rijen_met_dezelfde_url(rows, monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr("storage_supabase.price_cache.upsert_cm",
                        lambda key, url, listings, ts, schema="kensa": calls.append((key, url, listings, ts)))
    listings = [{"price_eur": 12.5, "seller": "tester", "description": "NM"}]
    now = datetime.now(timezone.utc).isoformat()

    n = cm_queue_api._sb_fill_same_url(URL_A, "testfill_done", listings, now,
                                       table="test_cardmarket_queue")

    assert n == 1
    same = sb_cmq.fetch_test_row("testfill_same_url")
    assert same["fetched_at"] is not None
    assert same["listings_json"] == listings
    assert same["error"] is None
    # het item waarvan het resultaat kwam wordt níet door deze functie geraakt
    assert sb_cmq.fetch_test_row("testfill_done")["fetched_at"] is None
    # andere URL blijft wachten op de worker
    assert sb_cmq.fetch_test_row("testfill_other_url")["fetched_at"] is None
    # price_cache bijgewerkt voor de card_key van de ingevulde rij
    assert calls == [("testmon:001/100:10", URL_A, listings, now)]


def test_fill_same_url_doet_niks_bij_lege_listings(rows, monkeypatch):
    monkeypatch.setattr("storage_supabase.price_cache.upsert_cm",
                        lambda *a, **k: pytest.fail("upsert_cm mag niet aangeroepen worden"))
    n = cm_queue_api._sb_fill_same_url(URL_A, "testfill_done", [], "2026-09-08T00:00:00+00:00",
                                       table="test_cardmarket_queue")
    assert n == 0
    assert sb_cmq.fetch_test_row("testfill_same_url")["fetched_at"] is None
