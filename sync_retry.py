"""Kensa — retry-queue voor gefaalde Supabase writes.

Wanneer `supabase_sync._post()` of `_patch()` faalt, wordt de call hierin
geparkeerd. Een drain-worker (cron) probeert 'm periodiek opnieuw met
exponentiele backoff. Na max_attempts komt de row op dead-letter en
gaat een Discord-alert richting #algemeen.

Ontwerp-regels:
- Geen bestaande logica breken: nieuwe module, enqueue is puur additief.
- Alle functies < 22 CC (regel 1 uit code-quality rules).
- Idempotent: een retry-poging heeft dezelfde semantiek als de originele call
  (Supabase-endpoints gebruiken merge-duplicates / PATCH op keys).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "kensa.db"

# Backoff-schema: 1e retry na 2 min, dan 10 min, 1u, 4u, 24u. Na 6 pogingen dead-letter.
BACKOFF_MINUTES = [2, 10, 60, 240, 1440]
MAX_ATTEMPTS = 6

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sync_retry (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    method         TEXT NOT NULL,          -- 'POST' | 'PATCH' | 'DELETE'
    path           TEXT NOT NULL,          -- 'listings', 'cardmarket_queue', ...
    payload_json   TEXT NOT NULL,          -- POST: {"rows":[...], "prefer":"..."}
                                           -- PATCH: {"query":{...}, "patch":{...}}
                                           -- DELETE: {"query":{...}}
    attempts       INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT,
    created_at     TEXT NOT NULL,          -- ISO-8601 UTC
    next_retry_at  TEXT NOT NULL,          -- ISO-8601 UTC
    dead_letter    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_sync_retry_due
    ON sync_retry(next_retry_at) WHERE dead_letter = 0;
CREATE INDEX IF NOT EXISTS ix_sync_retry_dl
    ON sync_retry(dead_letter);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), timeout=10)
    c.row_factory = sqlite3.Row
    return c


def ensure_table() -> None:
    """Idempotent: maakt tabel + indexen aan als ze niet bestaan."""
    with _connect() as c:
        c.executescript(SCHEMA_SQL)
        c.commit()


def _backoff_seconds(attempts: int) -> int:
    """Retry-vertraging in seconden voor de attempts-e poging (1-indexed).

    attempts=1 -> BACKOFF_MINUTES[0], enz. Boven range -> laatste waarde.
    """
    idx = min(max(attempts, 1) - 1, len(BACKOFF_MINUTES) - 1)
    return BACKOFF_MINUTES[idx] * 60


def _next_retry(attempts: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=_backoff_seconds(attempts))).isoformat()


def enqueue_post(path: str, rows, prefer: str | None, error: str) -> int:
    """Park een gefaalde POST voor latere retry. Return de nieuwe row-id."""
    payload = {"rows": rows if isinstance(rows, list) else [rows], "prefer": prefer}
    with _connect() as c:
        cur = c.execute(
            """INSERT INTO sync_retry
               (method, path, payload_json, attempts, last_error, created_at, next_retry_at)
               VALUES ('POST', ?, ?, 0, ?, ?, ?)""",
            (path, json.dumps(payload, ensure_ascii=False), error[:500], _now_iso(), _now_iso()),
        )
        c.commit()
        return cur.lastrowid


def enqueue_patch(path: str, query: dict, patch: dict, error: str) -> int:
    """Park een gefaalde PATCH voor latere retry. Return de nieuwe row-id."""
    payload = {"query": query, "patch": patch}
    with _connect() as c:
        cur = c.execute(
            """INSERT INTO sync_retry
               (method, path, payload_json, attempts, last_error, created_at, next_retry_at)
               VALUES ('PATCH', ?, ?, 0, ?, ?, ?)""",
            (path, json.dumps(payload, ensure_ascii=False), error[:500], _now_iso(), _now_iso()),
        )
        c.commit()
        return cur.lastrowid


def enqueue_delete(path: str, query: dict, error: str) -> int:
    """Park een gefaalde DELETE voor latere retry. Return de nieuwe row-id."""
    payload = {"query": query}
    with _connect() as c:
        cur = c.execute(
            """INSERT INTO sync_retry
               (method, path, payload_json, attempts, last_error, created_at, next_retry_at)
               VALUES ('DELETE', ?, ?, 0, ?, ?, ?)""",
            (path, json.dumps(payload, ensure_ascii=False), error[:500], _now_iso(), _now_iso()),
        )
        c.commit()
        return cur.lastrowid


def fetch_due(limit: int = 100) -> list[dict]:
    """Rijen die klaar zijn voor een (re)try. Oudste eerst, cap op `limit`."""
    with _connect() as c:
        rows = c.execute(
            """SELECT id, method, path, payload_json, attempts, created_at
               FROM sync_retry
               WHERE dead_letter = 0 AND next_retry_at <= ?
               ORDER BY next_retry_at ASC
               LIMIT ?""",
            (_now_iso(), limit),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_success(retry_id: int) -> None:
    """Retry gelukt → row weg."""
    with _connect() as c:
        c.execute("DELETE FROM sync_retry WHERE id = ?", (retry_id,))
        c.commit()


def bump_retry(retry_id: int, error: str) -> bool:
    """Retry mislukt → attempts++, plan volgende. Return True als dead-letter."""
    with _connect() as c:
        row = c.execute(
            "SELECT attempts FROM sync_retry WHERE id = ?", (retry_id,)
        ).fetchone()
        if row is None:
            return False
        new_attempts = row["attempts"] + 1
        if new_attempts >= MAX_ATTEMPTS:
            c.execute(
                """UPDATE sync_retry
                   SET attempts = ?, last_error = ?, dead_letter = 1
                   WHERE id = ?""",
                (new_attempts, error[:500], retry_id),
            )
            c.commit()
            return True
        c.execute(
            """UPDATE sync_retry
               SET attempts = ?, last_error = ?, next_retry_at = ?
               WHERE id = ?""",
            (new_attempts, error[:500], _next_retry(new_attempts), retry_id),
        )
        c.commit()
        return False


def stats() -> dict:
    """Overzicht van queue-gezondheid. Voor dashboard/alert."""
    with _connect() as c:
        total = c.execute(
            "SELECT COUNT(*) FROM sync_retry WHERE dead_letter = 0"
        ).fetchone()[0]
        due = c.execute(
            "SELECT COUNT(*) FROM sync_retry WHERE dead_letter = 0 AND next_retry_at <= ?",
            (_now_iso(),),
        ).fetchone()[0]
        dead = c.execute(
            "SELECT COUNT(*) FROM sync_retry WHERE dead_letter = 1"
        ).fetchone()[0]
        oldest = c.execute(
            "SELECT MIN(created_at) FROM sync_retry WHERE dead_letter = 0"
        ).fetchone()[0]
        return {"pending": total, "due_now": due, "dead_letter": dead, "oldest": oldest}


def dead_letter_since(iso_since: str) -> list[dict]:
    """Dead-letter rows die sinds iso_since erbij zijn gekomen (voor alerting)."""
    with _connect() as c:
        rows = c.execute(
            """SELECT id, method, path, attempts, last_error, created_at
               FROM sync_retry
               WHERE dead_letter = 1 AND created_at >= ?
               ORDER BY id DESC""",
            (iso_since,),
        ).fetchall()
        return [dict(r) for r in rows]


if __name__ == "__main__":
    ensure_table()
    print("sync_retry tabel klaar")
    print(json.dumps(stats(), indent=2))
