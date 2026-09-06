"""Regressie-test voor bugfix 2026-09-01: _enq_fresh_insert reset fetched_at
bij ON CONFLICT zodat stale-empty CM-resultaten opnieuw worden gefetched.

Zonder deze reset bleef 'listings=[]' van bijv 3 weken terug voor eeuwig
gecached en werd Cardmarket nooit opnieuw geraadpleegd.
"""
import sqlite3
from unittest.mock import patch

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Fresh SQLite met minimale cardmarket_queue tabel."""
    p = tmp_path / "test_kensa.db"
    conn = sqlite3.connect(str(p))
    conn.execute(
        """CREATE TABLE cardmarket_queue (
            item_id       TEXT PRIMARY KEY,
            url           TEXT NOT NULL,
            queued_at     TEXT NOT NULL,
            fetched_at    TEXT,
            listings_json TEXT,
            error         TEXT,
            grade         TEXT,
            card_key      TEXT
        )"""
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("analyze_split.enqueue_cardmarket.DB_PATH", p)
    return p


@pytest.mark.integration
def test_fresh_insert_resets_fetched_at_on_conflict(db):
    """Als een item al een oude fetched_at + lege listings heeft, moet _enq_fresh_insert
    die resetten naar NULL zodat de worker opnieuw fetched."""
    from analyze_split.enqueue_cardmarket import _enq_fresh_insert

    # Bootstrap: bestaande rij met een 3-weken-oude fetched_at + lege listings
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO cardmarket_queue (item_id,url,queued_at,fetched_at,listings_json,grade) "
        "VALUES (?,?,?,?,?,?)",
        ("m123", "https://old.example", "2026-08-01T00:00:00Z",
         "2026-08-10T00:00:00Z", "[]", "10"),
    )
    conn.commit()
    conn.close()

    # Re-enqueue via de bugfix-code
    hit = {"url": "https://new.example", "name": "Test"}
    slab = {"grade": "10", "card_name": "TEST", "number": "1", "set_name": "X"}
    with patch("analyze_split.enqueue_cardmarket._build_card_key", return_value="test:1:10"):
        # supabase_sync moet niet crashen
        with patch("supabase_sync.enabled", return_value=False):
            _enq_fresh_insert("m123", hit, "10", slab, None, verbose=False)

    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT url, fetched_at, listings_json, error FROM cardmarket_queue WHERE item_id=?",
        ("m123",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "https://new.example", "url moet zijn geüpdatet"
    assert row[1] is None, "fetched_at moet gereset zijn"
    assert row[2] is None, "listings_json moet gereset zijn"
    assert row[3] is None, "error moet gereset zijn"


@pytest.mark.integration
def test_fresh_insert_new_item_inserts_null_fetched_at(db):
    """Nieuw item: gewoon INSERT met NULL fetched_at."""
    from analyze_split.enqueue_cardmarket import _enq_fresh_insert

    hit = {"url": "https://x.com", "name": "New"}
    slab = {"grade": "10", "card_name": "NEW", "number": "1", "set_name": "X"}
    with patch("analyze_split.enqueue_cardmarket._build_card_key", return_value="new:1:10"):
        with patch("supabase_sync.enabled", return_value=False):
            _enq_fresh_insert("m999", hit, "10", slab, None, verbose=False)

    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT fetched_at, listings_json FROM cardmarket_queue WHERE item_id=?",
        ("m999",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] is None and row[1] is None
