"""Supabase-native cert_sightings storage.

Drop-in vervanging voor storage.record_cert_sighting:
  INSERT OR IGNORE INTO cert_sightings (cert, item_id, seller_hint, seen_at)
  SELECT COUNT + MIN(seen_at) voor die cert.

Return: {"times_seen": N, "first_seen_at": <ts>}
"""
from __future__ import annotations
from datetime import datetime, timezone
from . import _http
from ._logging import timed


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_sighting(cert: str, item_id: str, seller_hint: str | None = None,
                    schema: str = "kensa", table: str = "cert_sightings") -> dict:
    """1 request via database-functie kensa.record_cert_sighting (15-9). Bij een fout
    van de functie-aanroep: terugvallen op de oude 3-staps REST-route (zelfde uitkomst)."""
    if not cert or not item_id:
        return {"times_seen": 0, "first_seen_at": None}
    if schema == "kensa" and table == "cert_sightings":
        try:
            with timed("cert_sightings.record_rpc", key=f"{cert}/{item_id}") as ctx:
                rows = _http.rpc("record_cert_sighting", schema,
                                 {"p_cert": cert, "p_item_id": item_id, "p_seller_hint": seller_hint})
                ctx["n"] = len(rows)
            if rows:
                r = rows[0]
                return {"times_seen": int(r.get("times_seen") or 0), "first_seen_at": r.get("first_seen_at")}
        except Exception as e:
            print(f"[storage_supabase] record_cert_sighting rpc faalt ({type(e).__name__}: {e}) "
                  f"→ REST-route", file=__import__("sys").stderr)
    return _record_sighting_rest(cert, item_id, seller_hint, schema, table)


def _record_sighting_rest(cert: str, item_id: str, seller_hint: str | None = None,
                          schema: str = "kensa", table: str = "cert_sightings") -> dict:
    """Oude 3-staps route (bestaat-check, insert, aggregatie). Referentie + vangnet."""
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
