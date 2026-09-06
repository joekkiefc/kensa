"""Supabase-native listings.last_seen_at batch-update.

Drop-in vervanging voor storage.mark_seen_in_search:
  UPDATE listings SET last_seen_at = <now> WHERE item_id IN (...)

Return: aantal items dat gemarkeerd is (batch-count, geen row-level count).
"""
from __future__ import annotations
from datetime import datetime, timezone
from . import _http


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mark_seen(item_ids, schema: str = "kensa", table: str = "listings") -> int:
    if not item_ids:
        return 0
    ts = _now_iso()
    items = list(item_ids)
    # PostgREST IN-clause via ?item_id=in.("a","b","c")
    # Chunken op 200 om URL-lengte te beperken.
    total = 0
    for i in range(0, len(items), 200):
        chunk = items[i:i+200]
        or_clause = ",".join(f'"{iid}"' for iid in chunk)
        r = _http.patch(table, schema,
                        {"item_id": f"in.({or_clause})"},
                        {"last_seen_at": ts})
        if r.status_code not in (200, 204):
            raise RuntimeError(f"mark_seen batch failed {r.status_code}: {r.text[:200]}")
        total += len(chunk)
    return total
