"""Supabase-native photos storage.

Drop-in vervanging voor storage.upsert_photos (SQLite-versie).
Zelfde signature, zelfde return: aantal NIEUW ingevoegde rijen.

Gedrag matcht SQLite:
  - Skip lege URLs.
  - Skip URLs die al bestaan voor dit item (dedup op (item_id, url_original)).
  - Nieuwe URLs krijgen photo_index = MAX(photo_index) + 1 (per item).
  - Volgorde behoud: eerste URL uit input = laagste nieuwe index.
"""
from __future__ import annotations
from . import _http
from ._logging import timed


def upsert_photos(item_id: str, photo_urls, schema: str = "kensa", table: str = "photos") -> int:
    if not photo_urls:
        return 0

    # Max index nu (per item)
    existing = _http.get(table, schema, {
        "item_id": f"eq.{item_id}",
        "select": "photo_index,url_original",
    })
    known_urls = {r["url_original"] for r in existing}
    max_idx = max((r["photo_index"] for r in existing if r["photo_index"] is not None), default=-1)
    next_idx = max_idx + 1

    to_insert = []
    for url in photo_urls:
        if not url or url in known_urls:
            continue
        to_insert.append({"item_id": item_id, "photo_index": next_idx, "url_original": url})
        known_urls.add(url)  # dedup binnen input zelf ook
        next_idx += 1

    if not to_insert:
        return 0
    r = _http.post(table, schema, to_insert, prefer="return=minimal")
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"insert {table} failed {r.status_code}: {r.text[:200]}")
    return len(to_insert)


# --- Read-functies (Supabase-native, backwards-compat met analyze._get_photos) ---

def fetch_photos(item_id: str, schema: str = "kensa",
                 table: str = "photos") -> list[dict]:
    """Alle foto's voor een item, ORDER BY photo_index ASC.

    Retourneert list van dicts met: photo_id, photo_index, url_original,
    file_hash, file_path, downloaded_at, size_bytes, width, height.
    """
    with timed("photos.fetch_photos", key=item_id) as ctx:
        rows = _http.get(table, schema, {
            "item_id": f"eq.{item_id}",
            "select": "photo_id,photo_index,url_original,file_hash,file_path,downloaded_at,size_bytes,width,height",
            "order": "photo_index.asc",
        })
        ctx["n"] = len(rows)
        return rows


# --- Test-support hooks ---

def delete_test_item(item_id: str, schema: str = "kensa", table: str = "test_photos") -> None:
    r = _http.delete(table, schema, {"item_id": f"eq.{item_id}"})
    if r.status_code not in (200, 204, 404):
        raise RuntimeError(f"delete failed {r.status_code}: {r.text[:200]}")


def fetch_test_rows(item_id: str, schema: str = "kensa", table: str = "test_photos") -> list[dict]:
    return _http.get(table, schema, {
        "item_id": f"eq.{item_id}",
        "select": "photo_index,url_original",
        "order": "photo_index.asc",
    })


def upsert_test_photos(item_id: str, photo_urls) -> int:
    return upsert_photos(item_id, photo_urls, schema="kensa", table="test_photos")


def update_metadata(photo_id: int, file_hash: str, file_path: str,
                    size_bytes: int, width: int | None, height: int | None,
                    downloaded_at: str,
                    schema: str = "kensa", table: str = "photos") -> None:
    """Update photo-row met download-metadata (na file fetch).
    Drop-in vervanging voor de UPDATE aan het eind van download_photo_if_needed."""
    r = _http.patch(table, schema, {"photo_id": f"eq.{photo_id}"}, {
        "file_hash": file_hash,
        "file_path": file_path,
        "downloaded_at": downloaded_at,
        "size_bytes": size_bytes,
        "width": width,
        "height": height,
    })
    if r.status_code not in (200, 204):
        raise RuntimeError(f"update {table} metadata failed {r.status_code}: {r.text[:200]}")
