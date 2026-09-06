"""Kensa — atomic claim helper voor parallelle eBay-workers.

Zorgt dat worker_ebay (v1) en worker_ebay_2 (v2) niet dezelfde items oppakken.
Klaim = SELECT + UPDATE binnen één transactie (BEGIN IMMEDIATE), zodat
tegelijkertijd draaiende workers elkaar wegblokkeren.

Stale locks (> STALE_LOCK_MIN) worden gerecycled — als een worker crasht laat
het item niet permanent gelockt achter.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze import DB_PATH, PRICE_CACHE_DAYS  # noqa: E402

STALE_LOCK_MIN = 10  # locks ouder dan 10 min worden overschreven


def _ensure_lock_columns(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(listings)").fetchall()}
    if "locked_by" not in cols:
        conn.execute("ALTER TABLE listings ADD COLUMN locked_by TEXT")
    if "locked_at" not in cols:
        conn.execute("ALTER TABLE listings ADD COLUMN locked_at TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_listings_locked ON listings(locked_by, locked_at)"
    )


def claim_ebay_batch(worker_name: str, limit: int) -> list[str]:
    """Atomair claim van een batch cache-miss items voor de eBay-worker.

    Selecteert items met slab_status='ocr_done' zonder verse price_cache en
    zonder actieve lock (of met stale lock). Zet meteen locked_by=worker_name +
    locked_at=now, commit, retourneer item_ids.

    De transactie is BEGIN IMMEDIATE zodat de tweede worker moet wachten tot
    de eerste klaar is met claimen. Onder WAL is dat een goedkope kortstondige
    schrijf-lock.
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_lock_columns(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            f"""SELECT l.item_id
                FROM listings l
                LEFT JOIN price_cache pc
                       ON pc.card_key = l.card_key
                      AND pc.ebay_fetched_at IS NOT NULL
                      AND datetime(pc.ebay_fetched_at) >= datetime('now', '-{int(PRICE_CACHE_DAYS)} days')
                WHERE l.slab_status='ocr_done'
                  AND (l.card_key IS NULL OR pc.card_key IS NULL)
                  AND (
                        l.locked_by IS NULL
                        OR l.locked_at IS NULL
                        OR datetime(l.locked_at) < datetime('now', '-{int(STALE_LOCK_MIN)} minutes')
                  )
                ORDER BY l.first_seen_at DESC
                LIMIT ?""",
            (limit,),
        ).fetchall()
        ids = [r["item_id"] for r in rows]
        if ids:
            placeholders = ",".join("?" * len(ids))
            conn.execute(
                f"""UPDATE listings
                       SET locked_by = ?,
                           locked_at = datetime('now')
                     WHERE item_id IN ({placeholders})""",
                (worker_name, *ids),
            )
        conn.commit()
        return ids
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def release_locks(item_ids: list[str]) -> None:
    """Geef eventuele locks vrij (na fout / voor cleanup). analyze_score_only
    verandert slab_status → items worden hoe dan ook niet meer opgepakt door
    de reguliere WHERE-clause, maar dit houdt het schema schoon."""
    if not item_ids:
        return
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    try:
        placeholders = ",".join("?" * len(item_ids))
        conn.execute(
            f"UPDATE listings SET locked_by=NULL, locked_at=NULL WHERE item_id IN ({placeholders})",
            item_ids,
        )
        conn.commit()
    finally:
        conn.close()
