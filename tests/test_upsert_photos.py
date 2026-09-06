#!/usr/bin/env python3
"""Backwards-compatible test voor upsert_photos.

- Baseline mode: oude SQLite upsert_photos tegen tijdelijke DB → verify.
- Supabase mode: nieuwe storage_supabase.photos tegen kensa.test_photos → verify.

Beide moeten identieke eind-state produceren (photo_index reeks + dedup).
"""
from __future__ import annotations
import argparse, json, sqlite3, sys
from pathlib import Path

KENSA_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KENSA_DIR))

FIXTURE = KENSA_DIR / "tests" / "write_fixtures" / "upsert_photos.json"
BASELINE_DB = Path("/tmp/kensa_baseline_photos.db")


def _fetch_sqlite(item_id):
    conn = sqlite3.connect(BASELINE_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT photo_index, url_original FROM photos WHERE item_id = ? ORDER BY photo_index",
        (item_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _del_sqlite(item_id):
    conn = sqlite3.connect(BASELINE_DB)
    conn.execute("DELETE FROM photos WHERE item_id = ?", (item_id,))
    # Ook uit listings — FK constraint (photos.item_id → listings.item_id) blokkeert anders
    conn.execute("INSERT OR IGNORE INTO listings (item_id, first_seen_at, last_seen_at, status) VALUES (?, datetime('now'), datetime('now'), 'test')", (item_id,))
    conn.commit()
    conn.close()


def _compare_rows(actual, expected, case_id):
    diffs = []
    if len(actual) != len(expected):
        diffs.append(f"  count mismatch: actual={len(actual)} expected={len(expected)}")
        return diffs
    for a, e in zip(actual, expected):
        if a.get("photo_index") != e.get("photo_index"):
            diffs.append(f"  photo_index mismatch: {a.get('photo_index')} vs {e.get('photo_index')}")
        if a.get("url_original") != e.get("url_original"):
            diffs.append(f"  url mismatch @index {e.get('photo_index')}: {a.get('url_original')[:80]!r} vs {e.get('url_original')[:80]!r}")
    return diffs


def run_baseline(fixtures):
    if BASELINE_DB.exists():
        BASELINE_DB.unlink()
    import storage
    storage._sbs = None
    storage.init_db(BASELINE_DB)

    passed, failed = 0, 0
    for i, fix in enumerate(fixtures):
        case = fix["case"]
        item_id = fix["item_id"]
        _del_sqlite(item_id)
        if case == "insert_new":
            ret = storage.upsert_photos(item_id, fix["input_urls"], db_path=BASELINE_DB)
            if ret != fix["expected_return"]:
                print(f"[{i:02d}] {item_id}: FAIL return={ret} expected={fix['expected_return']}")
                failed += 1; continue
            actual = _fetch_sqlite(item_id)
            expected = fix["expected_rows"]
        elif case == "update_partial_dupe":
            r1 = storage.upsert_photos(item_id, fix["first_urls"], db_path=BASELINE_DB)
            r2 = storage.upsert_photos(item_id, fix["second_urls"], db_path=BASELINE_DB)
            if r1 != fix["expected_return_first"] or r2 != fix["expected_return_second"]:
                print(f"[{i:02d}] {item_id}: FAIL return1={r1}/{fix['expected_return_first']} return2={r2}/{fix['expected_return_second']}")
                failed += 1; continue
            actual = _fetch_sqlite(item_id)
            expected = fix["expected_rows_after"]
        else:
            print(f"[{i:02d}] unknown case"); failed += 1; continue

        diffs = _compare_rows(actual, expected, item_id)
        if diffs:
            print(f"[{i:02d}] {item_id} {case}: FAIL")
            for d in diffs: print(d)
            failed += 1
        else:
            passed += 1
    print(f"\n=== BASELINE (SQLite): {passed}/{len(fixtures)} passed, {failed} failed ===")
    return failed == 0


def run_supabase(fixtures):
    try:
        from storage_supabase import photos as sb_photos
    except Exception as e:
        print(f"FAIL import storage_supabase.photos: {e}")
        return False
    passed, failed = 0, 0
    for i, fix in enumerate(fixtures):
        case = fix["case"]
        item_id = fix["item_id"]
        sb_photos.delete_test_item(item_id)
        if case == "insert_new":
            ret = sb_photos.upsert_test_photos(item_id, fix["input_urls"])
            if ret != fix["expected_return"]:
                print(f"[{i:02d}] {item_id}: FAIL return={ret} expected={fix['expected_return']}")
                failed += 1; continue
            actual = sb_photos.fetch_test_rows(item_id)
            expected = fix["expected_rows"]
        elif case == "update_partial_dupe":
            r1 = sb_photos.upsert_test_photos(item_id, fix["first_urls"])
            r2 = sb_photos.upsert_test_photos(item_id, fix["second_urls"])
            if r1 != fix["expected_return_first"] or r2 != fix["expected_return_second"]:
                print(f"[{i:02d}] {item_id}: FAIL return1={r1}/{fix['expected_return_first']} return2={r2}/{fix['expected_return_second']}")
                failed += 1; continue
            actual = sb_photos.fetch_test_rows(item_id)
            expected = fix["expected_rows_after"]
        else:
            print(f"[{i:02d}] unknown case"); failed += 1; continue

        diffs = _compare_rows(actual, expected, item_id)
        if diffs:
            print(f"[{i:02d}] {item_id} {case}: FAIL")
            for d in diffs: print(d)
            failed += 1
        else:
            passed += 1
    print(f"\n=== SUPABASE (nieuwe): {passed}/{len(fixtures)} passed, {failed} failed ===")
    return failed == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["baseline", "supabase", "both"], default="both")
    args = ap.parse_args()
    fixtures = json.loads(FIXTURE.read_text())
    print(f"Loaded {len(fixtures)} fixtures from {FIXTURE}")
    ok = True
    if args.mode in ("baseline", "both"):
        ok &= run_baseline(fixtures)
    if args.mode in ("supabase", "both"):
        ok &= run_supabase(fixtures)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
