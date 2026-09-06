"""Test-helpers: directe Pi SQLite queries voor backwards-compat tests.

Nieuwe Supabase fetch-functies moeten dezelfde data teruggeven als de oude
SQLite queries op Pi. Deze helper doet een raw SQLite lookup zodat de test
zowel Pi als Supabase kan bevragen en vergelijken.
"""
from __future__ import annotations
import sqlite3
from pathlib import Path

PI_DB = Path("/home/pi/.openclaw/workspace/agents/kensa/kensa.db")


def pi_one(sql: str, params: tuple = ()) -> dict | None:
    conn = sqlite3.connect(str(PI_DB), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def pi_all(sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(str(PI_DB), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()
