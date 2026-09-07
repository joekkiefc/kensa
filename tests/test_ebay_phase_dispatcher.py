"""Blok B2 — dispatcher-consistency tests voor analyze_split/ebay_phase.

Verifieert dat _price_cache_get en _cached_ebay_for_query identieke output
geven onder READ=pi (default) en READ=supabase.
"""
from __future__ import annotations
import json
import os

BASELINE_CARD_KEY = "pikachu:223/284:10"


def _run_with_env(env_val, callable_):
    old = os.environ.get("KENSA_READ_STORAGE")
    if env_val is None:
        os.environ.pop("KENSA_READ_STORAGE", None)
    else:
        os.environ["KENSA_READ_STORAGE"] = env_val
    try:
        return callable_()
    finally:
        if old is None:
            os.environ.pop("KENSA_READ_STORAGE", None)
        else:
            os.environ["KENSA_READ_STORAGE"] = old


def test_price_cache_get_pi_vs_supabase_identical():
    from analyze_split.ebay_phase import _price_cache_get
    pi = _run_with_env(None, lambda: _price_cache_get(BASELINE_CARD_KEY))
    sb = _run_with_env("supabase", lambda: _price_cache_get(BASELINE_CARD_KEY))
    assert pi is not None
    assert sb is not None
    # Kernvelden matchen
    assert pi["card_key"] == sb["card_key"]
    assert pi["ebay_query"] == sb["ebay_query"]
    # jsonb-velden moeten in BEIDE takken string zijn (backwards-compat)
    assert isinstance(sb["ebay_result_json"], str), "Supabase-tak moet jsonb → string serialiseren"
    assert isinstance(pi["ebay_result_json"], str)
    # Parsed inhoud moet identiek
    assert json.loads(pi["ebay_result_json"]) == json.loads(sb["ebay_result_json"])
    if pi.get("cm_listings_json") or sb.get("cm_listings_json"):
        assert json.loads(pi["cm_listings_json"]) == json.loads(sb["cm_listings_json"])


def test_price_cache_get_returns_none_for_unknown():
    from analyze_split.ebay_phase import _price_cache_get
    for env in (None, "supabase"):
        result = _run_with_env(env, lambda: _price_cache_get("this-does-not-exist:0:0"))
        assert result is None, f"env={env}: verwachtte None, kreeg {result!r}"


def test_cached_ebay_for_query_returns_none_for_unknown_query():
    """Er is geen ebay_prices trap met deze query — beide takken moeten None geven."""
    from analyze_split.ebay_phase import _cached_ebay_for_query
    q = "impossible-query-that-will-not-be-in-cache-12345xyz"
    for env in (None, "supabase"):
        result = _run_with_env(env, lambda: _cached_ebay_for_query(q))
        assert result is None, f"env={env}: verwachtte None, kreeg {type(result)}"


def test_cached_ebay_for_query_shape_when_hit():
    """Supabase-leesroute vindt een recente ebay_prices query terug (juiste shape).

    Pre-cutover vergeleek deze test Pi vs Supabase op symmetrie. Sinds batch 3
    (2026-09-07) schrijft de eBay-worker alleen nog Supabase — de Pi-kant is
    bevroren en per definitie asymmetrisch. Bron voor de recente query is nu
    Supabase zelf (de productie-route).
    """
    from analyze_split.ebay_phase import _cached_ebay_for_query
    from storage_supabase import _http
    rows = _run_with_env("supabase", lambda: _http.get("analysis", "kensa", {
        "trap": "eq.ebay_prices",
        "select": "result_json",
        "order": "analysis_id.desc",
        "limit": "1",
    }))
    if not rows:
        return  # nog geen data om te testen
    data = rows[0].get("result_json")
    recent_query = data.get("query") if isinstance(data, dict) else None
    if not recent_query:
        return
    sb = _run_with_env("supabase", lambda: _cached_ebay_for_query(recent_query))
    # De net-opgehaalde nieuwste rij ligt binnen het 24u-venster → moet hit zijn
    assert sb is not None, f"supabase-route vindt recente query {recent_query!r} niet terug"
    assert sb.get("query") == recent_query


# ---------------------------------------------------------------------------
# Write-dispatcher: _price_cache_upsert_ebay routeert per KENSA_WRITE_STORAGE
# (zelfde valkuil als batch 2: zonder schakelaar blijft de Pi stiekem gevuld)
# ---------------------------------------------------------------------------

def _run_with_write_env(env_val, callable_):
    old = os.environ.get("KENSA_WRITE_STORAGE")
    if env_val is None:
        os.environ.pop("KENSA_WRITE_STORAGE", None)
    else:
        os.environ["KENSA_WRITE_STORAGE"] = env_val
    try:
        return callable_()
    finally:
        if old is None:
            os.environ.pop("KENSA_WRITE_STORAGE", None)
        else:
            os.environ["KENSA_WRITE_STORAGE"] = old


def _maak_lege_price_cache_db(tmp_path):
    import sqlite3
    db = tmp_path / "test_kensa.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE price_cache (
        card_key TEXT PRIMARY KEY, ebay_query TEXT, ebay_result_json TEXT,
        ebay_fetched_at TEXT, cm_url TEXT, cm_listings_json TEXT, cm_fetched_at TEXT)""")
    conn.commit()
    conn.close()
    return db


def test_upsert_ebay_supabase_mode_raakt_sqlite_niet(tmp_path, monkeypatch):
    """WRITE=supabase → alleen native route, geen enkele Pi-write."""
    import analyze_split.ebay_phase as ep
    import storage_supabase.price_cache as sb_pc
    db = _maak_lege_price_cache_db(tmp_path)
    monkeypatch.setattr(ep, "DB_PATH", db)
    calls = []
    monkeypatch.setattr(sb_pc, "upsert_ebay",
                        lambda ck, q, r, ts=None, schema="kensa": calls.append((ck, q, r)))
    _run_with_write_env("supabase",
                        lambda: ep._price_cache_upsert_ebay("test:1/1:10", "q", {"sales": []}))
    assert calls == [("test:1/1:10", "q", {"sales": []})]
    import sqlite3
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM price_cache").fetchone()[0] == 0
    conn.close()


def test_upsert_ebay_dual_mode_schrijft_beide(tmp_path, monkeypatch):
    """WRITE=dual → SQLite én native route."""
    import analyze_split.ebay_phase as ep
    import storage_supabase.price_cache as sb_pc
    db = _maak_lege_price_cache_db(tmp_path)
    monkeypatch.setattr(ep, "DB_PATH", db)
    calls = []
    monkeypatch.setattr(sb_pc, "upsert_ebay",
                        lambda ck, q, r, ts=None, schema="kensa": calls.append(ck))
    _run_with_write_env("dual",
                        lambda: ep._price_cache_upsert_ebay("test:2/2:10", "q2", {"sales": [1]}))
    assert calls == ["test:2/2:10"]
    import sqlite3
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT card_key, ebay_query FROM price_cache").fetchone()
    conn.close()
    assert row == ("test:2/2:10", "q2")


def test_upsert_ebay_default_sqlite_plus_legacy_sync(tmp_path, monkeypatch):
    """Geen env (default) → SQLite + oude best-effort sync, géén native route."""
    import analyze_split.ebay_phase as ep
    import storage_supabase.price_cache as sb_pc
    import supabase_sync as sbs
    db = _maak_lege_price_cache_db(tmp_path)
    monkeypatch.setattr(ep, "DB_PATH", db)
    native, legacy = [], []
    monkeypatch.setattr(sb_pc, "upsert_ebay",
                        lambda *a, **k: native.append(a))
    monkeypatch.setattr(sbs, "sync_price_cache_ebay",
                        lambda ck, q, r, ts: legacy.append(ck))
    _run_with_write_env(None,
                        lambda: ep._price_cache_upsert_ebay("test:3/3:10", "q3", {}))
    assert native == []
    assert legacy == ["test:3/3:10"]
    import sqlite3
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM price_cache").fetchone()[0] == 1
    conn.close()
