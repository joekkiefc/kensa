"""Supabase-native cert_sightings storage.

Drop-in vervanging voor storage.record_cert_sighting:
  INSERT OR IGNORE INTO cert_sightings (cert, item_id, seller_hint, seen_at)
  SELECT COUNT + MIN(seen_at) voor die cert.

Return: {"times_seen": N, "first_seen_at": <ts>}
"""
from __future__ import annotations
from datetime import datetime, timezone
from . import _http


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_sighting(cert: str, item_id: str, seller_hint: str | None = None,
                    schema: str = "kensa", table: str = "cert_sightings") -> dict:
    if not cert or not item_id:
        return {"times_seen": 0, "first_seen_at": None}
    ts = _now_iso()
    # INSERT — bij unieke constraint conflict negeren (INSERT OR IGNORE gedrag).
    # cert_sightings heeft geen PK op (cert, item_id) op Supabase; we filteren
    # zelf op existing eerst zodat we niet dubbel inserten.
    existing = _http.get(table, schema, {
        "cert": f"eq.{cert}",
        "item_id": f"eq.{item_id}",
        "select": "sighting_id",
    })
    if not existing:
        r = _http.post(table, schema, {
            "cert": cert,
            "item_id": item_id,
            "seller_hint": seller_hint,
            "seen_at": ts,
        }, prefer="return=minimal")
        if r.status_code not in (200, 201, 204):
            raise RuntimeError(f"insert {table} failed {r.status_code}: {r.text[:200]}")

    # Aggregate: aantal + eerste seen_at voor deze cert
    rows = _http.get(table, schema, {
        "cert": f"eq.{cert}",
        "select": "seen_at",
        "order": "seen_at.asc",
    })
    if not rows:
        return {"times_seen": 0, "first_seen_at": None}
    return {"times_seen": len(rows), "first_seen_at": rows[0].get("seen_at")}
