#!/usr/bin/env python3
"""Backwards-compatible test voor upsert_listing.

- Baseline mode: run OUDE upsert_listing (SQLite) tegen tijdelijke DB.
  Expected: 20/20 fixtures match. Bewijst dat de fixture correct is.
- Supabase mode: run NIEUWE upsert_listing_sb tegen kensa_test schema.
  Expected: 20/20 fixtures match. Bewijst dat nieuwe code identiek gedrag heeft.

Gebruik:
  python3 tests/test_upsert_listing.py --mode baseline
  python3 tests/test_upsert_listing.py --mode supabase
"""
from __future__ import annotations
import argparse, json, os, sqlite3, sys
from pathlib import Path

KENSA_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KENSA_DIR))

FIXTURE = KENSA_DIR / "tests" / "write_fixtures" / "upsert_listing.json"
BASELINE_DB = Path("/tmp/kensa_baseline_upsert.db")

# Velden die door upsert direct worden geset op basis van input.
# Andere velden (slab_status/card_key/locked_*/source) worden pas later
# door andere flows geset — moeten NULL zijn na een verse insert.
FIELDS_FROM_INPUT = {
    "item_id", "title_jp", "title_en", "description_jp",
    "price_jpy", "price_eur", "shipping_jpy",
    "seller_id", "seller_name", "seller_rating",
    "category_path", "condition", "authenticated", "sold",
    "detail_url", "detail_scraped_at", "status", "extra_json",
}
FIELDS_TIMESTAMP = {"first_seen_at", "last_seen_at"}
FIELDS_UNSET_ON_INSERT = {"slab_status", "card_key", "locked_by", "locked_at", "source"}


TIMESTAMP_COLS = {"first_seen_at", "last_seen_at", "detail_scraped_at", "locked_at"}


def _normalize(v, col):
    """Normalize types tussen SQLite (TEXT) en Postgres (bool/jsonb/timestamptz)."""
    if v is None:
        return None
    if col in ("authenticated", "sold"):
        return bool(v)
    if col == "extra_json":
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return v
        return v
    if col == "price_eur":
        return float(v)
    if col in TIMESTAMP_COLS and isinstance(v, str):
        # Parse tot datetime — Postgres dropt trailing zeros op microseconden
        # (05:33:20.328850 → 05:33:20.32885). Semantisch identiek.
        from datetime import datetime
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            return v
    return v


def _compare(actual: dict, expected: dict, case_id: str) -> list[str]:
    diffs = []
    # velden die exact match moeten
    for col in FIELDS_FROM_INPUT:
        a = _normalize(actual.get(col), col)
        e = _normalize(expected.get(col), col)
        if a != e:
            diffs.append(f"  {col}: actual={a!r} expected={e!r}")
    # timestamps moeten NIET-NULL en well-formed zijn (exacte waarde varieert per run)
    for col in FIELDS_TIMESTAMP:
        a = actual.get(col)
        if not a:
            diffs.append(f"  {col}: NULL (expected non-null)")
    # velden die NULL horen te zijn na verse insert
    for col in FIELDS_UNSET_ON_INSERT:
        a = actual.get(col)
        if a is not None:
            diffs.append(f"  {col}: {a!r} (expected NULL na insert)")
    return diffs


def _fetch_sqlite(item_id):
    conn = sqlite3.connect(BASELINE_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM listings WHERE item_id = ?", (item_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _del_sqlite(item_id):
    conn = sqlite3.connect(BASELINE_DB)
    conn.execute("DELETE FROM listings WHERE item_id = ?", (item_id,))
    conn.commit()
    conn.close()


def run_baseline(fixtures):
    """Run oude SQLite upsert_listing tegen tijdelijke DB."""
    if BASELINE_DB.exists():
        BASELINE_DB.unlink()
    import storage
    storage._sbs = None  # KRITIEK: geen sync naar productie-Supabase
    storage.init_db(BASELINE_DB)

    passed, failed = 0, 0
    for i, fix in enumerate(fixtures):
        case = fix["case"]
        if case == "insert_new":
            inp = fix["input"]
            item_id = inp["item_id"]
            _del_sqlite(item_id)
            is_new = storage.upsert_listing(dict(inp), db_path=BASELINE_DB)
            if not is_new:
                print(f"[{i:02d}] {item_id} insert: FAIL — return False (expected True)")
                failed += 1; continue
            actual = _fetch_sqlite(item_id)
        elif case == "update_existing":
            insert_in = fix["insert_input"]
            update_in = fix["update_input"]
            item_id = insert_in["item_id"]
            _del_sqlite(item_id)
            storage.upsert_listing(dict(insert_in), db_path=BASELINE_DB)
            is_new = storage.upsert_listing(dict(update_in), db_path=BASELINE_DB)
            if is_new:
                print(f"[{i:02d}] {item_id} update: FAIL — return True (expected False)")
                failed += 1; continue
            actual = _fetch_sqlite(item_id)
        else:
            print(f"[{i:02d}] unknown case {case}"); failed += 1; continue

        if not actual:
            print(f"[{i:02d}] {item_id} {case}: FAIL — geen row"); failed += 1; continue
        diffs = _compare(actual, fix["expected_after"], item_id)
        if diffs:
            print(f"[{i:02d}] {item_id} {case}: FAIL")
            for d in diffs: print(d)
            failed += 1
        else:
            passed += 1
    print(f"\n=== BASELINE (SQLite oude code): {passed}/{len(fixtures)} passed, {failed} failed ===")
    return failed == 0


def run_supabase(fixtures):
    """Run nieuwe storage_supabase/listings.py upsert tegen kensa_test schema."""
    try:
        from storage_supabase import listings as sb_listings
    except Exception as e:
        print(f"FAIL: kan storage_supabase.listings niet importeren ({e}).")
        print("Bouw eerst /home/pi/.openclaw/workspace/agents/kensa/storage_supabase/listings.py")
        return False
    passed, failed = 0, 0
    for i, fix in enumerate(fixtures):
        case = fix["case"]
        if case == "insert_new":
            inp = fix["input"]
            item_id = inp["item_id"]
            sb_listings.delete_test_item(item_id)
            is_new = sb_listings.upsert_test_item(dict(inp))
            if not is_new:
                print(f"[{i:02d}] {item_id} insert: FAIL — return False (expected True)")
                failed += 1; continue
            actual = sb_listings.fetch_test_item(item_id)
        elif case == "update_existing":
            insert_in = fix["insert_input"]
            update_in = fix["update_input"]
            item_id = insert_in["item_id"]
            sb_listings.delete_test_item(item_id)
            sb_listings.upsert_test_item(dict(insert_in))
            is_new = sb_listings.upsert_test_item(dict(update_in))
            if is_new:
                print(f"[{i:02d}] {item_id} update: FAIL — return True (expected False)")
                failed += 1; continue
            actual = sb_listings.fetch_test_item(item_id)
        else:
            print(f"[{i:02d}] unknown case {case}"); failed += 1; continue

        if not actual:
            print(f"[{i:02d}] {item_id} {case}: FAIL — geen row"); failed += 1; continue
        diffs = _compare(actual, fix["expected_after"], item_id)
        if diffs:
            print(f"[{i:02d}] {item_id} {case}: FAIL")
            for d in diffs: print(d)
            failed += 1
        else:
            passed += 1
    print(f"\n=== SUPABASE (nieuwe code): {passed}/{len(fixtures)} passed, {failed} failed ===")
    return failed == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["baseline", "supabase", "both"], default="baseline")
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
