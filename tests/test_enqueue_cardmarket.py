#!/usr/bin/env python3
"""Backwards-compatible test voor cardmarket_queue enqueue-paths.

Cases:
  1. fresh_insert nieuw item          → row met NULL fetched_at + listings_json
  2. from_cache nieuw item            → row met fetched_at + listings_json gevuld
  3. fresh_insert over from_cache row → resets fetched_at + listings_json + error
                                        (bugfix 2026-09-01)
  4. from_cache over fresh_insert row → vult fetched_at + listings_json
  5. fresh_insert met card_key=None   → row met card_key NULL
"""
from __future__ import annotations
import argparse, json, sqlite3, sys
from pathlib import Path

KENSA_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KENSA_DIR))

BASELINE_DB = Path("/tmp/kensa_baseline_cmq.db")

CASES = [
    {
        "name": "fresh_only",
        "steps": [("fresh", "url-A", "10", "chariz:001:10")],
        "expected": {"url": "url-A", "grade": "10", "card_key": "chariz:001:10",
                     "fetched_at": None, "listings_json": None, "error": None},
    },
    {
        "name": "cache_only",
        "steps": [("cache", "url-B", "10", "mew:143:9", [{"p": 1.5}])],
        "expected": {"url": "url-B", "grade": "10", "card_key": "mew:143:9",
                     "fetched_at_not_null": True,
                     "listings_json": [{"p": 1.5}], "error": None},
    },
    {
        "name": "fresh_over_cache_resets",
        "steps": [
            ("cache", "url-C", "10", "old:001:10", [{"p": 99}]),
            ("fresh", "url-C", "10", "old:001:10"),
        ],
        "expected": {"url": "url-C", "grade": "10", "card_key": "old:001:10",
                     "fetched_at": None, "listings_json": None, "error": None},
    },
    {
        "name": "cache_over_fresh_fills",
        "steps": [
            ("fresh", "url-D", "9", "raichu:025:9"),
            ("cache", "url-D", "9", "raichu:025:9", [{"p": 3.0}]),
        ],
        "expected": {"url": "url-D", "grade": "9", "card_key": "raichu:025:9",
                     "fetched_at_not_null": True,
                     "listings_json": [{"p": 3.0}], "error": None},
    },
    {
        "name": "fresh_null_card_key",
        "steps": [("fresh", "url-E", "10", None)],
        "expected": {"url": "url-E", "grade": "10", "card_key": None,
                     "fetched_at": None, "listings_json": None, "error": None},
    },
]


def _fetch_sqlite(item_id):
    conn = sqlite3.connect(BASELINE_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM cardmarket_queue WHERE item_id = ?", (item_id,)).fetchone()
    conn.close()
    if not row: return None
    d = dict(row)
    if isinstance(d.get("listings_json"), str) and d["listings_json"]:
        try: d["listings_json"] = json.loads(d["listings_json"])
        except Exception: pass
    return d


def _del_sqlite(item_id):
    conn = sqlite3.connect(BASELINE_DB)
    conn.execute("DELETE FROM cardmarket_queue WHERE item_id = ?", (item_id,))
    conn.commit(); conn.close()


def _sql_fresh(item_id, url, grade, card_key):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(BASELINE_DB)
    conn.execute(
        """INSERT INTO cardmarket_queue (item_id, url, grade, queued_at, card_key)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(item_id) DO UPDATE SET
             url=excluded.url, grade=excluded.grade, card_key=excluded.card_key,
             fetched_at=NULL, listings_json=NULL, error=NULL""",
        (item_id, url, grade, now, card_key),
    )
    conn.commit(); conn.close()


def _sql_cache(item_id, url, grade, card_key, listings):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(BASELINE_DB)
    conn.execute(
        """INSERT INTO cardmarket_queue (item_id, url, grade, queued_at, fetched_at, listings_json, card_key)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(item_id) DO UPDATE SET
             url=excluded.url, grade=excluded.grade,
             fetched_at=excluded.fetched_at, listings_json=excluded.listings_json,
             card_key=excluded.card_key, error=NULL""",
        (item_id, url, grade, now, now, json.dumps(listings), card_key),
    )
    conn.commit(); conn.close()


def _verify(actual, expected, name):
    diffs = []
    for k in ("url", "grade", "card_key", "error"):
        if actual.get(k) != expected.get(k):
            diffs.append(f"  {k}: {actual.get(k)!r} vs {expected.get(k)!r}")
    if "fetched_at" in expected:
        if actual.get("fetched_at") not in (None, ""):
            diffs.append(f"  fetched_at: {actual.get('fetched_at')!r} (expected NULL)")
    if expected.get("fetched_at_not_null"):
        if not actual.get("fetched_at"):
            diffs.append(f"  fetched_at: NULL (expected non-null)")
    if "listings_json" in expected:
        ea = actual.get("listings_json")
        exp = expected["listings_json"]
        if ea != exp:
            diffs.append(f"  listings_json: {ea!r} vs {exp!r}")
    return diffs


def run_baseline():
    if BASELINE_DB.exists(): BASELINE_DB.unlink()
    import storage
    storage._sbs = None
    storage.init_db(BASELINE_DB)
    # grade + card_key zijn later via ALTER toegevoegd, niet in init_db-SCHEMA
    conn = sqlite3.connect(BASELINE_DB)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(cardmarket_queue)").fetchall()}
    if "grade" not in cols:
        conn.execute("ALTER TABLE cardmarket_queue ADD COLUMN grade TEXT")
    if "card_key" not in cols:
        conn.execute("ALTER TABLE cardmarket_queue ADD COLUMN card_key TEXT")
    conn.commit(); conn.close()
    passed, failed = 0, 0
    for i, case in enumerate(CASES):
        item_id = f"testcmq_{i}"
        _del_sqlite(item_id)
        for step in case["steps"]:
            kind = step[0]
            if kind == "fresh":
                _sql_fresh(item_id, step[1], step[2], step[3])
            else:
                _sql_cache(item_id, step[1], step[2], step[3], step[4])
        actual = _fetch_sqlite(item_id)
        if not actual: print(f"[{i}] {case['name']}: FAIL no row"); failed += 1; continue
        diffs = _verify(actual, case["expected"], case["name"])
        if diffs: print(f"[{i}] {case['name']}: FAIL"); [print(d) for d in diffs]; failed += 1
        else: passed += 1
    print(f"\n=== BASELINE (SQLite): {passed}/{len(CASES)} passed, {failed} failed ===")
    return failed == 0


def run_supabase():
    from storage_supabase import cardmarket_queue as sb_cmq
    passed, failed = 0, 0
    for i, case in enumerate(CASES):
        item_id = f"testcmq_{i}"
        sb_cmq.delete_test_item(item_id)
        for step in case["steps"]:
            kind = step[0]
            if kind == "fresh":
                sb_cmq.enqueue_test_fresh(item_id, step[1], step[2], step[3])
            else:
                sb_cmq.enqueue_test_from_cache(item_id, step[1], step[2], step[3], step[4])
        actual = sb_cmq.fetch_test_row(item_id)
        if not actual: print(f"[{i}] {case['name']}: FAIL no row"); failed += 1; continue
        diffs = _verify(actual, case["expected"], case["name"])
        if diffs: print(f"[{i}] {case['name']}: FAIL"); [print(d) for d in diffs]; failed += 1
        else: passed += 1
    print(f"\n=== SUPABASE (nieuwe): {passed}/{len(CASES)} passed, {failed} failed ===")
    return failed == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["baseline", "supabase", "both"], default="both")
    args = ap.parse_args()
    ok = True
    if args.mode in ("baseline", "both"): ok &= run_baseline()
    if args.mode in ("supabase", "both"): ok &= run_supabase()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
