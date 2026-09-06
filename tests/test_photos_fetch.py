"""Blok A4 — backwards-compat tests voor storage_supabase.photos.fetch_photos"""
from __future__ import annotations

from storage_supabase.photos import fetch_photos
from tests._pi_helpers import pi_all

BASELINE_ITEM = "m24912557313"  # 3 foto's op moment van schrijven


def test_fetch_photos_baseline():
    photos = fetch_photos(BASELINE_ITEM)
    assert len(photos) == 3, f"verwachtte 3 foto's, kreeg {len(photos)}"
    # DESC=false → ASC volgorde 0,1,2
    assert [p["photo_index"] for p in photos] == [0, 1, 2]
    # photo_id's uit baseline
    assert photos[0]["photo_id"] == 328053
    assert photos[1]["photo_id"] == 328215
    assert photos[2]["photo_id"] == 328216
    # url-patroon van mercari
    assert "mercdn.net" in photos[0]["url_original"]


def test_fetch_photos_empty_for_unknown():
    photos = fetch_photos("m00000000000")
    assert photos == []


def test_fetch_photos_all_have_required_keys():
    photos = fetch_photos(BASELINE_ITEM)
    for p in photos:
        for k in ("photo_id", "photo_index", "url_original",
                  "file_hash", "file_path", "size_bytes", "width", "height"):
            assert k in p, f"key {k} ontbreekt in photo-row"


def test_pi_matches_supabase_photos():
    pi_rows = pi_all(
        "SELECT photo_index, url_original FROM photos "
        "WHERE item_id = ? ORDER BY photo_index ASC",
        (BASELINE_ITEM,)
    )
    sb_rows = fetch_photos(BASELINE_ITEM)
    assert len(pi_rows) == len(sb_rows)
    for pi, sb in zip(pi_rows, sb_rows):
        assert pi["photo_index"] == sb["photo_index"]
        assert pi["url_original"] == sb["url_original"]
