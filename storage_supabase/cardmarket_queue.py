"""Supabase-native cardmarket_queue storage.

Drop-in vervanging voor de twee enqueue-paths in analyze_split/enqueue_cardmarket.py:

  enqueue_from_cache(item_id, url, grade, card_key, listings_json)
     → UPSERT MET fetched_at + listings_json (cache-hit pad)

  enqueue_fresh(item_id, url, grade, card_key)
     → UPSERT ZONDER fetched_at / listings_json + expliciete reset
       (bugfix 2026-09-01: anders bleef oude "0 listings"-cache voor eeuwig).

Beide gedragen zich als SQLite's `INSERT ... ON CONFLICT(item_id) DO UPDATE SET`.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from . import _http


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _upsert(row: dict, schema: str, table: str) -> None:
    r = _http.post(table, schema, row,
                   prefer="return=minimal,resolution=merge-duplicates")
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"upsert {table} failed {r.status_code}: {r.text[:200]}")


def enqueue_from_cache(item_id: str, url: str, grade: str, card_key: str,
                       listings_json: str | list,
                       schema: str = "kensa", table: str = "cardmarket_queue") -> None:
    """Cache-hit pad: alle velden geset, error=NULL."""
    now = _now_iso()
    if isinstance(listings_json, str):
        try:
            listings_json = json.loads(listings_json)
        except Exception:
            pass
    row = {
        "item_id": item_id,
        "url": url,
        "grade": grade,
        "queued_at": now,
        "fetched_at": now,
        "listings_json": listings_json,
        "card_key": card_key,
        "error": None,
    }
    _upsert(row, schema, table)


def enqueue_fresh(item_id: str, url: str, grade: str, card_key: str | None,
                  schema: str = "kensa", table: str = "cardmarket_queue") -> None:
    """Fresh-insert pad: geen fetched_at/listings_json (worker moet scrapen).
    Bij CONFLICT: reset ook fetched_at + listings_json + error zodat oude
    "0 listings"-cache niet blijft plakken (bugfix 2026-09-01)."""
    now = _now_iso()
    row = {
        "item_id": item_id,
        "url": url,
        "grade": grade,
        "queued_at": now,
        "card_key": card_key,
        "fetched_at": None,
        "listings_json": None,
        "error": None,
    }
    _upsert(row, schema, table)


# --- Test-support hooks ---

def delete_test_item(item_id: str, schema: str = "kensa", table: str = "test_cardmarket_queue") -> None:
    r = _http.delete(table, schema, {"item_id": f"eq.{item_id}"})
    if r.status_code not in (200, 204, 404):
        raise RuntimeError(f"delete failed {r.status_code}: {r.text[:200]}")


def fetch_test_row(item_id: str, schema: str = "kensa", table: str = "test_cardmarket_queue") -> dict | None:
    rows = _http.get(table, schema, {"item_id": f"eq.{item_id}", "select": "*"})
    return rows[0] if rows else None


def enqueue_test_from_cache(item_id, url, grade, card_key, listings_json):
    enqueue_from_cache(item_id, url, grade, card_key, listings_json,
                       schema="kensa", table="test_cardmarket_queue")


def enqueue_test_fresh(item_id, url, grade, card_key):
    enqueue_fresh(item_id, url, grade, card_key,
                  schema="kensa", table="test_cardmarket_queue")
