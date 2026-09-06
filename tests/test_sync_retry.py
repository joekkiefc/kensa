"""Unit-tests voor sync_retry queue-module (100% belangrijke functies)."""
import pytest
import sqlite3
from unittest.mock import patch

import sync_retry


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Use a tmp SQLite DB voor test-isolatie."""
    db = tmp_path / "test_kensa.db"
    monkeypatch.setattr(sync_retry, "DB_PATH", db)
    sync_retry.ensure_table()
    return db


@pytest.mark.unit
def test_ensure_table_creates_schema(tmp_db):
    conn = sqlite3.connect(str(tmp_db))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sync_retry)").fetchall()}
    conn.close()
    assert {"id", "method", "path", "payload_json", "attempts", "last_error",
            "created_at", "next_retry_at", "dead_letter"}.issubset(cols)


@pytest.mark.unit
def test_ensure_table_idempotent(tmp_db):
    sync_retry.ensure_table()
    sync_retry.ensure_table()  # 2nd call moet niet crashen


@pytest.mark.unit
def test_backoff_seconds_progression():
    assert sync_retry._backoff_seconds(1) == 2 * 60
    assert sync_retry._backoff_seconds(2) == 10 * 60
    assert sync_retry._backoff_seconds(3) == 60 * 60
    assert sync_retry._backoff_seconds(4) == 240 * 60
    assert sync_retry._backoff_seconds(5) == 1440 * 60
    # Boven range = laatste waarde
    assert sync_retry._backoff_seconds(99) == 1440 * 60
    # attempts=0 → behandeld als 1
    assert sync_retry._backoff_seconds(0) == 2 * 60


@pytest.mark.unit
def test_enqueue_post_roundtrip(tmp_db):
    rid = sync_retry.enqueue_post("listings", [{"item_id": "m1"}], "return=minimal", "test error")
    assert rid == 1
    due = sync_retry.fetch_due(10)
    assert len(due) == 1
    assert due[0]["method"] == "POST"
    assert due[0]["path"] == "listings"
    assert due[0]["attempts"] == 0


@pytest.mark.unit
def test_enqueue_patch_roundtrip(tmp_db):
    rid = sync_retry.enqueue_patch(
        "listings", {"item_id": "eq.m1"}, {"slab_status": "ok"}, "test error",
    )
    due = sync_retry.fetch_due(10)
    assert len(due) == 1
    assert due[0]["method"] == "PATCH"


@pytest.mark.unit
def test_mark_success_deletes_row(tmp_db):
    rid = sync_retry.enqueue_post("x", [{}], None, "err")
    sync_retry.mark_success(rid)
    assert sync_retry.fetch_due(10) == []


@pytest.mark.unit
def test_bump_retry_attempts_and_backoff(tmp_db):
    rid = sync_retry.enqueue_post("x", [{}], None, "err")
    # 1e bump → attempts=1, geen dead-letter, next_retry_at in toekomst
    is_dead = sync_retry.bump_retry(rid, "again")
    assert is_dead is False
    # Fetch due mag 'm niet meer teruggeven (next_retry_at is in toekomst)
    assert sync_retry.fetch_due(10) == []


@pytest.mark.unit
def test_bump_retry_hits_dead_letter(tmp_db):
    rid = sync_retry.enqueue_post("x", [{}], None, "err")
    # Simuleer 5x bump (attempts=5), 6e bump = dead letter (MAX_ATTEMPTS=6)
    conn = sqlite3.connect(str(tmp_db))
    conn.execute("UPDATE sync_retry SET attempts=5 WHERE id=?", (rid,))
    conn.commit(); conn.close()
    is_dead = sync_retry.bump_retry(rid, "final")
    assert is_dead is True

    stats = sync_retry.stats()
    assert stats["dead_letter"] == 1
    assert stats["pending"] == 0  # dead-letter telt niet als pending


@pytest.mark.unit
def test_bump_retry_unknown_id_returns_false(tmp_db):
    assert sync_retry.bump_retry(999, "err") is False


@pytest.mark.unit
def test_stats_empty(tmp_db):
    assert sync_retry.stats() == {"pending": 0, "due_now": 0, "dead_letter": 0, "oldest": None}


@pytest.mark.unit
def test_stats_with_mixed_state(tmp_db):
    sync_retry.enqueue_post("a", [{}], None, "err")
    sync_retry.enqueue_post("b", [{}], None, "err")
    stats = sync_retry.stats()
    assert stats["pending"] == 2
    assert stats["due_now"] == 2  # net toegevoegd → nu al due


@pytest.mark.unit
def test_dead_letter_since_filter(tmp_db):
    rid = sync_retry.enqueue_post("x", [{}], None, "err")
    conn = sqlite3.connect(str(tmp_db))
    conn.execute("UPDATE sync_retry SET dead_letter=1 WHERE id=?", (rid,))
    conn.commit(); conn.close()

    # Since een tijd VOOR de row = 1 hit
    dead = sync_retry.dead_letter_since("2000-01-01T00:00:00+00:00")
    assert len(dead) == 1
    assert dead[0]["method"] == "POST"

    # Since een tijd IN de toekomst = 0
    dead = sync_retry.dead_letter_since("2099-01-01T00:00:00+00:00")
    assert len(dead) == 0
