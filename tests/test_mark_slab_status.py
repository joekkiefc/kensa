#!/usr/bin/env python3
"""Backwards-compatible test voor mark_slab_status.

Scenario per fixture: insert listing → mark_slab_status → verify.
Testcases:
  - status alleen (card_key=None) → alleen slab_status geset, card_key blijft NULL
  - status + card_key → beide geset
  - status + card_key twee keer (verandering) → laatste wint
"""
from __future__ import annotations
import argparse, json, sqlite3, sys
from pathlib import Path

KENSA_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KENSA_DIR))

FIXTURE = KENSA_DIR / "tests" / "write_fixtures" / "upsert_listing.json"
BASELINE_DB = Path("/tmp/kensa_baseline_slab.db")


def _fetch_sqlite(item_id):
    conn = sqlite3.connect(BASELINE_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT slab_status, card_key FROM listings WHERE item_id = ?", (item_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _build_cases(fixtures):
    """Bouw 10 slab-status cases uit de 20 insert-only listings-fixtures."""
    inserts = [f for f in fixtures if f["case"] == "insert_new"][:10]
    cases = []
    for i, base in enumerate(inserts):
        item_id = base["input"]["item_id"] + "_slabTest"
        if i < 4:  # status only
            cases.append({
                "sub": "status_only",
                "insert_input": {**base["input"], "item_id": item_id},
                "calls": [("ocr_done", None)],
                "expected": {"slab_status": "ocr_done", "card_key": None},
            })
        elif i < 8:  # status + card_key
            cases.append({
                "sub": "status_and_card_key",
                "insert_input": {**base["input"], "item_id": item_id},
                "calls": [("analyzed", "charizard:001/100:10")],
                "expected": {"slab_status": "analyzed", "card_key": "charizard:001/100:10"},
            })
        else:  # twee keer → laatste wint
            cases.append({
                "sub": "double_call",
                "insert_input": {**base["input"], "item_id": item_id},
                "calls": [("ocr_done", "old:001:10"), ("analyzed", "new:002:9")],
                "expected": {"slab_status": "analyzed", "card_key": "new:002:9"},
            })
    return cases


def run_baseline(cases):
    if BASELINE_DB.exists():
        BASELINE_DB.unlink()
    import storage
    storage._sbs = None
    storage.init_db(BASELINE_DB)
    # Ensure slab_status/card_key kolommen bestaan (worden elders via ALTER
    # toegevoegd, zijn niet in init_db-migraties opgenomen).
    conn = sqlite3.connect(BASELINE_DB)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(listings)").fetchall()}
    if "slab_status" not in cols:
        conn.execute("ALTER TABLE listings ADD COLUMN slab_status TEXT")
    if "card_key" not in cols:
        conn.execute("ALTER TABLE listings ADD COLUMN card_key TEXT")
    conn.commit()
    conn.close()

    def _mark(item_id, status, card_key):
        conn = sqlite3.connect(BASELINE_DB)
        try:
            if card_key is not None:
                conn.execute("UPDATE listings SET slab_status=?, card_key=? WHERE item_id=?",
                             (status, card_key, item_id))
            else:
                conn.execute("UPDATE listings SET slab_status=? WHERE item_id=?", (status, item_id))
            conn.commit()
        finally:
            conn.close()

    passed, failed = 0, 0
    for i, case in enumerate(cases):
        item_id = case["insert_input"]["item_id"]
        storage.upsert_listing(dict(case["insert_input"]), db_path=BASELINE_DB)
        for status, ck in case["calls"]:
            _mark(item_id, status, ck)
        actual = _fetch_sqlite(item_id)
        if not actual:
            print(f"[{i:02d}] {item_id}: FAIL geen row"); failed += 1; continue
        if actual["slab_status"] != case["expected"]["slab_status"] or actual["card_key"] != case["expected"]["card_key"]:
            print(f"[{i:02d}] {item_id} {case['sub']}: FAIL actual={actual} expected={case['expected']}")
            failed += 1
        else:
            passed += 1
    print(f"\n=== BASELINE (SQLite): {passed}/{len(cases)} passed, {failed} failed ===")
    return failed == 0


def run_supabase(cases):
    from storage_supabase import listings as sb_listings
    passed, failed = 0, 0
    for i, case in enumerate(cases):
        item_id = case["insert_input"]["item_id"]
        sb_listings.delete_test_item(item_id)
        sb_listings.upsert_test_item(dict(case["insert_input"]))
        for status, ck in case["calls"]:
            sb_listings.mark_test_slab_status(item_id, status, ck)
        actual = sb_listings.fetch_test_item(item_id)
        if not actual:
            print(f"[{i:02d}] {item_id}: FAIL geen row"); failed += 1; continue
        got = {"slab_status": actual.get("slab_status"), "card_key": actual.get("card_key")}
        if got != case["expected"]:
            print(f"[{i:02d}] {item_id} {case['sub']}: FAIL actual={got} expected={case['expected']}")
            failed += 1
        else:
            passed += 1
    print(f"\n=== SUPABASE (nieuwe): {passed}/{len(cases)} passed, {failed} failed ===")
    return failed == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["baseline", "supabase", "both"], default="both")
    args = ap.parse_args()
    fixtures = json.loads(FIXTURE.read_text())
    cases = _build_cases(fixtures)
    print(f"Built {len(cases)} slab-status cases from listings fixtures")
    ok = True
    if args.mode in ("baseline", "both"): ok &= run_baseline(cases)
    if args.mode in ("supabase", "both"): ok &= run_supabase(cases)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
