"""Supabase-native price_cache storage.

Vervangt SQLite `price_cache` tabel voor eBay + Cardmarket resultaten
gedeeld per card_key. Alle score-workers lezen dit + cm_queue_api schrijft
dit.

Schema (kensa.price_cache):
  card_key text PRIMARY KEY
  ebay_query text, ebay_result_json jsonb, ebay_fetched_at timestamptz
  cm_url text, cm_listings_json jsonb, cm_fetched_at timestamptz
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from . import _http
from ._logging import timed, log_call

TABLE = "price_cache"


def fetch_by_card_key(card_key: str, schema: str = "kensa") -> dict | None:
    """Volledige price_cache row voor een card_key. None als niet bestaat.

    Retourneert dict met alle 7 kolommen. jsonb-kolommen zijn native Python
    (dict of list), niet stringified — dat is wat Postgrest teruggeeft.
    """
    with timed("price_cache.fetch_by_card_key", key=card_key) as ctx:
        rows = _http.get(TABLE, schema, {
            "card_key": f"eq.{card_key}",
            "select": "card_key,ebay_query,ebay_result_json,ebay_fetched_at,cm_url,cm_listings_json,cm_fetched_at",
            "limit": "1",
        })
        ctx["n"] = len(rows)
        return rows[0] if rows else None


def upsert_ebay(card_key: str, ebay_query: str, ebay_result,
                fetched_at: str | None = None,
                schema: str = "kensa") -> None:
    """Update alleen ebay_* kolommen. Insert als card_key nog niet bestaat."""
    ts = fetched_at or datetime.now(timezone.utc).isoformat()
    payload = {
        "card_key": card_key,
        "ebay_query": ebay_query,
        "ebay_result_json": ebay_result,
        "ebay_fetched_at": ts,
    }
    with timed("price_cache.upsert_ebay", key=card_key):
        r = _http.post(TABLE, schema, payload,
                       prefer="resolution=merge-duplicates,return=minimal")
        if r.status_code not in (200, 201, 204):
            raise RuntimeError(f"upsert_ebay failed {r.status_code}: {r.text[:200]}")


def upsert_cm(card_key: str, cm_url: str, cm_listings,
              fetched_at: str | None = None,
              schema: str = "kensa") -> None:
    """Update alleen cm_* kolommen. Insert als card_key nog niet bestaat."""
    ts = fetched_at or datetime.now(timezone.utc).isoformat()
    payload = {
        "card_key": card_key,
        "cm_url": cm_url,
        "cm_listings_json": cm_listings,
        "cm_fetched_at": ts,
    }
    with timed("price_cache.upsert_cm", key=card_key):
        r = _http.post(TABLE, schema, payload,
                       prefer="resolution=merge-duplicates,return=minimal")
        if r.status_code not in (200, 201, 204):
            raise RuntimeError(f"upsert_cm failed {r.status_code}: {r.text[:200]}")


def is_ebay_fresh(row: dict | None, ttl_hours: int = 72) -> bool:
    if not row or not row.get("ebay_fetched_at"):
        return False
    try:
        ts = datetime.fromisoformat(row["ebay_fetched_at"].replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ts) < timedelta(hours=ttl_hours)
    except Exception:
        return False


def is_cm_fresh(row: dict | None, ttl_hours: int = 72) -> bool:
    if not row or not row.get("cm_fetched_at"):
        return False
    try:
        ts = datetime.fromisoformat(row["cm_fetched_at"].replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - ts) < timedelta(hours=ttl_hours)
    except Exception:
        return False


# --- Test-support hooks ---

def delete_test_card_key(card_key: str, schema: str = "kensa",
                         table: str = "test_price_cache") -> None:
    r = _http.delete(table, schema, {"card_key": f"eq.{card_key}"})
    if r.status_code not in (200, 204, 404):
        raise RuntimeError(f"delete failed {r.status_code}: {r.text[:200]}")


def fetch_test_row(card_key: str, schema: str = "kensa",
                   table: str = "test_price_cache") -> dict | None:
    rows = _http.get(table, schema, {
        "card_key": f"eq.{card_key}",
        "select": "*",
        "limit": "1",
    })
    return rows[0] if rows else None
