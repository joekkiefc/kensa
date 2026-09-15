"""15-9: schrijfroutes in 1 request (cert-registratie via database-functie, listing-upsert
zonder kijk-vooraf + geboortedatum-trigger). Live tegen Supabase; testrijen worden opgeruimd.
"""
from __future__ import annotations
import time
import uuid

from storage_supabase import _http
from storage_supabase import cert_sightings as sb_cert
from storage_supabase import listings as sb_listings


def _cleanup_cert(cert: str) -> None:
    r = _http.delete("cert_sightings", "kensa", {"cert": f"eq.{cert}"})
    assert r.status_code in (200, 204), r.text


def test_record_cert_sighting_rpc_matches_rest_semantics():
    cert = f"TEST-15-9-{uuid.uuid4().hex[:10]}"
    # cert_sightings.item_id heeft een foreign key naar listings → twee echte kaarten lenen
    # (de cert zelf is synthetisch en wordt opgeruimd).
    ids = [r["item_id"] for r in _http.get("listings", "kensa", {"select": "item_id", "limit": "2",
                                                                 "order": "first_seen_at.desc"})]
    assert len(ids) == 2
    item_a, item_b = ids
    try:
        a = sb_cert.record_sighting(cert, item_a, "seller_x")
        assert a["times_seen"] == 1 and a["first_seen_at"]
        b = sb_cert.record_sighting(cert, item_b, None)
        assert b["times_seen"] == 2
        assert b["first_seen_at"] == a["first_seen_at"], "eerste seen_at moet stabiel blijven"
        # idempotent: zelfde (cert, item) nogmaals → geen extra rij
        c = sb_cert.record_sighting(cert, item_a, "seller_x")
        assert c["times_seen"] == 2
        # oude REST-route ziet exact hetzelfde
        rest = sb_cert._record_sighting_rest(cert, item_b, None)
        assert rest["times_seen"] == 2 and rest["first_seen_at"] == a["first_seen_at"]
        # lege input → nul
        assert sb_cert.record_sighting("", "x") == {"times_seen": 0, "first_seen_at": None}
    finally:
        _cleanup_cert(cert)


def test_listing_upsert_one_request_keeps_first_seen():
    item_id = f"test15-9-{uuid.uuid4().hex[:10]}"
    try:
        is_new = sb_listings.upsert_test_item({"item_id": item_id, "title_jp": "eerste", "price_jpy": 100})
        assert is_new is True
        row1 = sb_listings.fetch_test_item(item_id)
        assert row1["title_jp"] == "eerste" and row1["price_jpy"] == 100
        assert row1["first_seen_at"] == row1["last_seen_at"]

        time.sleep(0.05)
        # update: None-velden laten oude waarde staan, meegegeven first_seen_at wordt genegeerd (trigger)
        is_new2 = sb_listings.upsert_test_item({"item_id": item_id, "title_jp": None, "price_jpy": 200,
                                                "first_seen_at": "2020-01-01T00:00:00+00:00"})
        assert is_new2 is False
        row2 = sb_listings.fetch_test_item(item_id)
        assert row2["title_jp"] == "eerste"           # None overschrijft niet
        assert row2["price_jpy"] == 200               # meegegeven waarde wel
        assert row2["first_seen_at"] == row1["first_seen_at"], "geboortedatum mag nooit veranderen"
        assert row2["last_seen_at"] > row1["last_seen_at"]
    finally:
        sb_listings.delete_test_item(item_id)
