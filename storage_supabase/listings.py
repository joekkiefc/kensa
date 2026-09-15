"""Supabase-native listings storage.

Drop-in vervanging voor storage.upsert_listing (SQLite-versie).
Zelfde signature, zelfde return-semantiek:
  - True als een NIEUW item werd aangemaakt (INSERT).
  - False als een bestaand item werd bijgewerkt (UPDATE).

Gedrag matcht COALESCE-semantiek van SQLite: NULL-waarden in de input
overschrijven bestaande waarden niet. Bij UPDATE wordt first_seen_at
niet aangeraakt (alleen last_seen_at wordt vernieuwd).
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from . import _http
from ._logging import timed

LISTING_COLS = (
    "item_id", "title_jp", "title_en", "description_jp",
    "price_jpy", "price_eur", "shipping_jpy",
    "seller_id", "seller_name", "seller_rating",
    "category_path", "condition", "authenticated", "sold",
    "detail_url", "detail_scraped_at", "status", "extra_json",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _maybe_json(v):
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def _prepare_payload(row: dict) -> dict:
    """Normaliseer input naar Postgres-types (bool i.p.v. int, dict i.p.v. json-string)."""
    payload = {c: row.get(c) for c in LISTING_COLS}
    for b in ("authenticated", "sold"):
        if payload.get(b) is not None:
            payload[b] = bool(payload[b])
    if payload.get("status") is None:
        payload["status"] = "new"
    payload["extra_json"] = _maybe_json(payload.get("extra_json"))
    return payload


def upsert_listing(row: dict, schema: str = "kensa", table: str = "listings") -> bool:
    """Insert of update. Return True bij INSERT (nieuw), False bij UPDATE.

    `table` is parametrisch zodat we tegen `kensa.test_listings` kunnen testen
    zonder productie-listings te raken (kensa_test schema is niet exposed via
    PostgREST op Supabase Cloud).
    """
    if not row.get("item_id"):
        raise ValueError("item_id is required")
    payload = _prepare_payload(row)
    item_id = payload["item_id"]
    ts = _now_iso()

    # Strip None-waarden — SQLite COALESCE(:col, col) laat oude waarde staan.
    # Upsert (merge-duplicates) overschrijft alleen de meegestuurde kolommen → zelfde
    # semantiek als de vroegere PATCH.
    non_null = {k: v for k, v in payload.items() if v is not None}
    non_null["item_id"] = item_id
    non_null["last_seen_at"] = ts
    # Geboortedatum: bij INSERT de meegegeven waarde (bv. oude Pi-listing) of nu. Bij
    # UPDATE zet de database-trigger listings_keep_first_seen 'm terug op de oude waarde,
    # dus we hoeven niet meer vooraf te kijken of de rij bestaat (15-9: 1 request i.p.v. 2).
    non_null["first_seen_at"] = row.get("first_seen_at") or ts

    r = _http.post(table, schema, non_null,
                   prefer="resolution=merge-duplicates,return=representation",
                   params={"on_conflict": "item_id", "select": "item_id,first_seen_at,last_seen_at"})
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"upsert {table} failed {r.status_code}: {r.text[:200]}")
    # is_new afleiden uit de teruggestuurde rij: bij INSERT zijn first_seen_at en
    # last_seen_at allebei `ts` (tenzij de aanroeper een eigen first_seen_at meegaf —
    # alleen migraties; productie gebruikt de returnwaarde niet). Geparkeerde write
    # (vangnet) → geen rij bekend → False.
    try:
        body = r.json()
        rec = body[0] if isinstance(body, list) and body else {}
    except Exception:
        rec = {}
    if not rec:
        return False
    return rec.get("first_seen_at") is not None and rec.get("first_seen_at") == rec.get("last_seen_at")


# --- Read-functies (Supabase-native, backwards-compat met analyze._load_listing) ---

FETCH_LISTING_COLS = (
    "item_id,title_jp,title_en,description_jp,"
    "price_jpy,price_eur,shipping_jpy,"
    "seller_id,seller_name,seller_rating,"
    "category_path,condition,authenticated,sold,"
    "detail_url,status,slab_status,card_key,extra_json,"
    "first_seen_at,last_seen_at,detail_scraped_at,"
    "locked_by,locked_at,source"
)


def fetch_listing(item_id: str, schema: str = "kensa",
                  table: str = "listings") -> dict | None:
    """Volledige listing row voor item_id. None als niet bestaat."""
    with timed("listings.fetch_listing", key=item_id) as ctx:
        rows = _http.get(table, schema, {
            "item_id": f"eq.{item_id}",
            "select": FETCH_LISTING_COLS,
            "limit": "1",
        })
        ctx["n"] = len(rows)
        return rows[0] if rows else None


def fetch_needing_detail(limit: int = 100, schema: str = "kensa",
                         table: str = "listings") -> list[str]:
    """item_ids waarvoor detail_scraped_at IS NULL, ORDER BY first_seen_at DESC."""
    with timed("listings.fetch_needing_detail", key=f"limit={limit}") as ctx:
        rows = _http.get(table, schema, {
            "detail_scraped_at": "is.null",
            "select": "item_id",
            "order": "first_seen_at.desc",
            "limit": str(limit),
        })
        ctx["n"] = len(rows)
        return [r["item_id"] for r in rows]


# --- Test-support hooks (tegen kensa.test_listings) ---

def delete_test_item(item_id: str, schema: str = "kensa", table: str = "test_listings") -> None:
    r = _http.delete(table, schema, {"item_id": f"eq.{item_id}"})
    if r.status_code not in (200, 204, 404):
        raise RuntimeError(f"delete failed {r.status_code}: {r.text[:200]}")


def fetch_test_item(item_id: str, schema: str = "kensa", table: str = "test_listings") -> dict | None:
    rows = _http.get(table, schema, {"item_id": f"eq.{item_id}", "select": "*"})
    return rows[0] if rows else None


def upsert_test_item(row: dict) -> bool:
    """Test-only: gooi upsert tegen kensa.test_listings i.p.v. kensa.listings."""
    return upsert_listing(row, schema="kensa", table="test_listings")


# --- mark_slab_status: drop-in vervanging voor analyze._mark_slab_status ---

def mark_slab_status(item_id: str, status: str, card_key: str | None = None,
                     schema: str = "kensa", table: str = "listings") -> None:
    """UPDATE listings SET slab_status=<status>[, card_key=<card_key>] WHERE item_id."""
    patch = {"slab_status": status}
    if card_key is not None:
        patch["card_key"] = card_key
    r = _http.patch(table, schema, {"item_id": f"eq.{item_id}"}, patch)
    if r.status_code not in (200, 204):
        raise RuntimeError(f"update {table}.slab_status failed {r.status_code}: {r.text[:200]}")


def mark_test_slab_status(item_id: str, status: str, card_key: str | None = None) -> None:
    mark_slab_status(item_id, status, card_key, schema="kensa", table="test_listings")
