"""Supabase-native analysis storage.

Drop-in vervanging voor analyze._save_trap:
  - DELETE alle bestaande rijen voor (item_id, trap).
  - INSERT nieuwe rij met result_json/confidence/card_id/created_at.

Idempotent: dezelfde call twee keer geeft één rij (laatste wint).
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from . import _http
from ._logging import timed


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_trap(item_id: str, trap: str, result: dict,
              confidence: float | None = None, card_id: str | None = None,
              schema: str = "kensa", table: str = "analysis") -> None:
    ts = _now_iso()
    # DELETE oude rijen voor (item_id, trap)
    r = _http.delete(table, schema, {
        "item_id": f"eq.{item_id}",
        "trap": f"eq.{trap}",
    })
    if r.status_code not in (200, 204, 404):
        raise RuntimeError(f"delete {table} failed {r.status_code}: {r.text[:200]}")

    # INSERT nieuwe rij — result_json is jsonb, geen JSON.dumps nodig als 't al dict is
    row = {
        "item_id": item_id,
        "trap": trap,
        "result_json": result,
        "confidence": confidence,
        "card_id": card_id,
        "created_at": ts,
    }
    r = _http.post(table, schema, row, prefer="return=minimal")
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"insert {table} failed {r.status_code}: {r.text[:200]}")


# --- Read-functies (Supabase-native, backwards-compat met analyze._load_stored_slab_llm) ---

def fetch_latest_trap(item_id: str, trap: str,
                      schema: str = "kensa", table: str = "analysis") -> dict | None:
    """Meest recente analysis-rij voor (item_id, trap). None als niet bestaat.

    Retourneert dict met keys: analysis_id, item_id, trap, result_json (dict),
    confidence (float|None), card_id (str|None), created_at (ISO str).
    result_json is native (jsonb → dict), geen JSON.dumps nodig.
    """
    with timed("analysis.fetch_latest_trap", key=f"{item_id}/{trap}") as ctx:
        rows = _http.get(table, schema, {
            "item_id": f"eq.{item_id}",
            "trap": f"eq.{trap}",
            "select": "analysis_id,item_id,trap,result_json,confidence,card_id,created_at",
            "order": "analysis_id.desc",
            "limit": "1",
        })
        ctx["n"] = len(rows)
        return rows[0] if rows else None


def fetch_all_traps(item_id: str,
                    schema: str = "kensa", table: str = "analysis") -> list[dict]:
    """Alle analysis-rijen voor een item, ORDER analysis_id DESC.

    Voor items met meerdere traps: de eerste hit per trap is de meest recente.
    """
    with timed("analysis.fetch_all_traps", key=item_id) as ctx:
        rows = _http.get(table, schema, {
            "item_id": f"eq.{item_id}",
            "select": "analysis_id,item_id,trap,result_json,confidence,card_id,created_at",
            "order": "analysis_id.desc",
        })
        ctx["n"] = len(rows)
        return rows


# --- Test-support hooks ---

def delete_test_item(item_id: str, schema: str = "kensa", table: str = "test_analysis") -> None:
    r = _http.delete(table, schema, {"item_id": f"eq.{item_id}"})
    if r.status_code not in (200, 204, 404):
        raise RuntimeError(f"delete failed {r.status_code}: {r.text[:200]}")


def fetch_test_traps(item_id: str, schema: str = "kensa", table: str = "test_analysis") -> list[dict]:
    return _http.get(table, schema, {
        "item_id": f"eq.{item_id}",
        "select": "trap,result_json,confidence,card_id",
        "order": "trap.asc",
    })


def save_test_trap(item_id: str, trap: str, result: dict,
                   confidence: float | None = None, card_id: str | None = None) -> None:
    save_trap(item_id, trap, result, confidence, card_id,
              schema="kensa", table="test_analysis")
