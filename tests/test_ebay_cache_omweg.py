"""eBay-notitieboek omweg (Tommy 9-9): zelfde kaart onder andere spelling → toch cache-hit.
SQLite-pad (conftest zet KENSA_READ_STORAGE uit), geen netwerk."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import analyze_split.ebay_cache_omweg as OM
import analyze_split.ebay_phase as EP


def _iso(dagen_geleden: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=dagen_geleden)).isoformat()


@pytest.fixture
def db(tmp_path, monkeypatch):
    p = tmp_path / "kensa.db"
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE price_cache (card_key TEXT PRIMARY KEY, ebay_query TEXT, ebay_result_json TEXT, "
                 "ebay_fetched_at TEXT, cm_url TEXT, cm_listings_json TEXT, cm_fetched_at TEXT)")
    conn.execute("CREATE TABLE cardmarket_queue (item_id TEXT, url TEXT, queued_at TEXT)")
    rows = [
        # oude Gemini-spelling, vers, geen set → mag via kern
        ("gengar:094/165:10", "Gengar 094 PSA 10", json.dumps({"sales": [1, 2, 3], "query": "Gengar 094 PSA 10"}), _iso(1.5), None),
        # andere set → botst, mag NIET via kern
        ("gengar:094/165:10:sv3", "Gengar 094 PSA 10", json.dumps({"sales": [9]}), _iso(0.5), None),
        # verlopen (4 dagen) → niet
        ("pikachu:025/165:10", "Pikachu 025 PSA 10", json.dumps({"sales": [1]}), _iso(4), None),
        # zelfde CM-URL, andere spelling, vers → via URL
        ("steelix:073/063:10", "Steelix 073 PSA 10", json.dumps({"sales": [4, 5], "query": "Steelix 073 PSA 10"}), _iso(2),
         "https://www.cardmarket.com/en/Pokemon/Products/Singles/Mega-Brave/Steelix-V2-m1L073?language=7&minCondition=1"),
    ]
    conn.executemany("INSERT INTO price_cache (card_key, ebay_query, ebay_result_json, ebay_fetched_at, cm_url) VALUES (?,?,?,?,?)", rows)
    conn.execute("INSERT INTO cardmarket_queue VALUES (?,?,?)", ("m1", rows[3][4], _iso(0.1)))
    conn.commit(); conn.close()
    monkeypatch.setattr(OM, "DB_PATH", p)
    monkeypatch.setattr(EP, "DB_PATH", p)
    monkeypatch.delenv("KENSA_EBAY_CACHE_OMWEG", raising=False)
    return p


def test_kern_en_set():
    assert OM.kern("gengar:094/165:10:sv2a") == "gengar:94:10"
    assert OM.kern("pikachu:001:10:sv-p") == "pikachu:1:10"
    assert OM.kern("kapot") is None
    assert OM.set_van("gengar:094:10:sv2a") == "sv2a" and OM.set_van("gengar:094:10") is None
    assert not OM.sets_botsen(None, "sv2a") and not OM.sets_botsen("SV2A", "sv2a")
    assert OM.sets_botsen("sv2a", "sv3")
    assert not OM.sets_botsen("s-p", "swshp")          # alias: S-P = SWSH-P


def test_via_kern_vindt_oude_spelling_maar_niet_andere_set(db):
    rij = OM.zoek_via_kern("gengar:094:10:sv2a")
    assert rij and rij["card_key"] == "gengar:094/165:10"       # sv3-rij overgeslagen (botst)
    assert isinstance(rij["ebay_result_json"], str)


def test_via_kern_zonder_voorloopnullen(db):
    rij = OM.zoek_via_kern("gengar:94:10")
    assert rij and rij["card_key"] in ("gengar:094/165:10", "gengar:094/165:10:sv3")   # geen eigen set → beide mogen


def test_verlopen_telt_niet(db):
    assert OM.zoek_via_kern("pikachu:025:10:sv2a") is None


def test_via_url_gaat_voor(db):
    rij, bron = OM.zoek("steelix:073:10:m1l", "m1")
    assert bron == "price_cache_url" and rij["card_key"] == "steelix:073/063:10"


def test_kill_switch(db, monkeypatch):
    monkeypatch.setenv("KENSA_EBAY_CACHE_OMWEG", "0")
    assert OM.zoek("gengar:094:10:sv2a", None) == (None, None)


def test_cache_check_gebruikt_omweg_en_kopieert_met_oude_tijd(db, monkeypatch):
    slab = {"card_name": "Gengar", "number": "094/SV2A", "grade": "10", "set_code": "SV2A"}
    monkeypatch.setattr(EP, "_price_cache_get", lambda ck: None)          # exacte sleutel mist
    monkeypatch.setattr(EP, "_cached_ebay_for_query", lambda q: None)
    kopie = {}
    monkeypatch.setattr(EP, "_price_cache_upsert_ebay",
                        lambda ck, q, res, fetched_at=None: kopie.update(ck=ck, q=q, res=res, ts=fetched_at))
    card_key, res = EP._ebay_cache_check(slab, None, "Gengar 094 SV2A PSA 10", False, listing={"item_id": "m-x"})
    assert card_key == "gengar:094:10:sv2a"
    assert res["from_cache"] is True and res["cache_source"] == "price_cache_kern" and res["cache_via"] == "gengar:094/165:10"
    assert res["sales"] == [1, 2, 3]
    assert kopie["ck"] == "gengar:094:10:sv2a" and kopie["ts"] and kopie["res"]["sales"] == [1, 2, 3]
    assert kopie["q"] == "Gengar 094 PSA 10"


def test_cache_check_zonder_omweg_hit_gaat_door_naar_query_cache(db, monkeypatch):
    slab = {"card_name": "Snorlax", "number": "181", "grade": "10"}
    monkeypatch.setattr(EP, "_price_cache_get", lambda ck: None)
    monkeypatch.setattr(EP, "_cached_ebay_for_query", lambda q: {"sales": [7]})
    _, res = EP._ebay_cache_check(slab, None, "Snorlax 181 PSA 10", False, listing={"item_id": "m-y"})
    assert res == {"sales": [7], "from_cache": True}


@pytest.mark.parametrize("slab", [
    {"card_name": "Gengar", "number": "094/SV2A", "grade": "10", "set_code": "SV2A"},
    {"card_name": "Pikachu", "number": "001/SV-P", "grade": "10"},
    {"card_name": "Glaceon", "number": "217/172", "grade": "10", "set_code": "s12a"},
    {"card_name": "Charizard ex", "number": "223", "grade": "9"},
])
def test_beide_sleutelbouwers_geven_dezelfde_sleutel(slab):
    from analyze import _build_card_key as bouw_a
    assert EP._build_card_key(slab, None) == bouw_a(slab, None)
    assert EP._build_card_key(slab, None).count("/") <= 1
