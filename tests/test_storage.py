"""Integration-tests voor storage.py (SQLite-tabel + upserts + retention)."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import gzip
import json
import sqlite3
from unittest.mock import patch

import pytest

import storage


@pytest.fixture
def db(tmp_path):
    """Fresh SQLite in tmp_path met init_db-schema geladen."""
    p = tmp_path / "test_kensa.db"
    storage.init_db(db_path=p)
    return p


@pytest.mark.unit
def test_now_iso_format():
    iso = storage.now_iso()
    # ISO-8601 met tz-suffix
    assert "T" in iso and ("+" in iso or "Z" in iso)


@pytest.mark.integration
def test_init_db_creates_tables(db):
    conn = sqlite3.connect(str(db))
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    for expected in ["listings", "photos", "raw_pages", "cert_sightings",
                     "analysis", "cardmarket_queue"]:
        assert expected in tables


@pytest.mark.integration
def test_init_db_idempotent(db):
    # Twee keer init mag niet crashen
    storage.init_db(db_path=db)
    storage.init_db(db_path=db)


@pytest.mark.integration
def test_upsert_listing_insert_and_update(db):
    row = {"item_id": "m1", "title_jp": "PSA10 テスト", "price_jpy": 1000}
    is_new = storage.upsert_listing(row, db_path=db)
    assert is_new is True

    row2 = {"item_id": "m1", "title_jp": "PSA10 テスト", "price_jpy": 1500}
    is_new2 = storage.upsert_listing(row2, db_path=db)
    assert is_new2 is False  # update, geen insert

    conn = sqlite3.connect(str(db))
    p = conn.execute("SELECT price_jpy FROM listings WHERE item_id=?", ("m1",)).fetchone()
    conn.close()
    assert p[0] == 1500


@pytest.mark.integration
def test_upsert_listing_ignores_unknown_keys(db):
    row = {"item_id": "m2", "title_jp": "X", "some_unknown_field": "moet niet crashen"}
    storage.upsert_listing(row, db_path=db)


@pytest.mark.integration
def test_upsert_photos_adds_rows(db):
    # Bootstrap listing (photos.item_id FK)
    storage.upsert_listing({"item_id": "m3", "title_jp": "x"}, db_path=db)
    n = storage.upsert_photos("m3",
        ["https://example.com/p_0.jpg", "https://example.com/p_1.jpg"],
        db_path=db)
    assert n == 2
    conn = sqlite3.connect(str(db))
    ph = conn.execute(
        "SELECT photo_index,url_original FROM photos WHERE item_id=? ORDER BY photo_index",
        ("m3",),
    ).fetchall()
    conn.close()
    assert len(ph) == 2
    assert ph[0][1].endswith("p_0.jpg")


@pytest.mark.integration
def test_upsert_photos_dedup_on_reinsert(db):
    storage.upsert_listing({"item_id": "m4", "title_jp": "x"}, db_path=db)
    storage.upsert_photos("m4", ["https://example.com/p_0.jpg"], db_path=db)
    # Zelfde url opnieuw → geen 2e rij
    storage.upsert_photos("m4", ["https://example.com/p_0.jpg"], db_path=db)
    conn = sqlite3.connect(str(db))
    n = conn.execute("SELECT COUNT(*) FROM photos WHERE item_id=?", ("m4",)).fetchone()[0]
    conn.close()
    assert n == 1


@pytest.mark.integration
def test_save_raw_page_compresses(db):
    html = "<html><body>" + "x" * 5000 + "</body></html>"
    pid = storage.save_raw_page("https://x/1", "detail", html, db_path=db)
    assert isinstance(pid, int)
    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT html_gz FROM raw_pages WHERE page_id=?", (pid,)).fetchone()
    conn.close()
    stored = gzip.decompress(row[0]).decode()
    assert stored == html


@pytest.mark.integration
def test_mark_seen_in_search_updates_last_seen(db):
    storage.upsert_listing({"item_id": "m5", "title_jp": "x", "first_seen_at": "2026-01-01T00:00:00+00:00"}, db_path=db)
    n = storage.mark_seen_in_search(["m5"], db_path=db)
    assert n == 1
    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT last_seen_at FROM listings WHERE item_id=?", ("m5",)).fetchone()
    conn.close()
    assert row[0] is not None
    assert row[0] != "2026-01-01T00:00:00+00:00"  # is bijgewerkt


@pytest.mark.integration
def test_record_cert_sighting_counts_up(db):
    storage.upsert_listing({"item_id": "m6", "title_jp": "x"}, db_path=db)
    storage.upsert_listing({"item_id": "m7", "title_jp": "y"}, db_path=db)
    r1 = storage.record_cert_sighting("12345678", "m6", db_path=db)
    r2 = storage.record_cert_sighting("12345678", "m7", db_path=db)
    assert r1["times_seen"] == 1
    assert r2["times_seen"] == 2


@pytest.mark.integration
def test_cleanup_raw_pages_removes_old(db):
    # Insert een 'oud' raw_pages record via directe SQL
    conn = sqlite3.connect(str(db))
    old_iso = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    conn.execute(
        "INSERT INTO raw_pages (url, page_type, html_gz, fetched_at) VALUES (?, ?, ?, ?)",
        ("https://old", "detail", gzip.compress(b"hi"), old_iso),
    )
    conn.commit()
    conn.close()
    deleted = storage.cleanup_raw_pages(days=7, db_path=db)
    assert deleted >= 1


@pytest.mark.integration
def test_db_stats_returns_dict(db):
    stats = storage.db_stats(db_path=db)
    assert isinstance(stats, dict)
    # Ten minste enkele bekende keys
    for k in ["listings", "photos", "raw_pages"]:
        assert k in stats
