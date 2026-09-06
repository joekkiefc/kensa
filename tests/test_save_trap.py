#!/usr/bin/env python3
"""Backwards-compatible test voor analyze._save_trap → storage_supabase.analysis.save_trap.

Baseline: SQLite _save_trap tegen tijdelijke DB (met dummy listing-row).
Supabase: nieuwe save_trap tegen kensa.test_analysis.
"""
from __future__ import annotations
import argparse, json, sqlite3, sys
from pathlib import Path

KENSA_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KENSA_DIR))

FIXTURE = KENSA_DIR / "tests" / "write_fixtures" / "save_trap.json"
BASELINE_DB = Path("/tmp/kensa_baseline_save_trap.db")


def _fetch_sqlite(item_id):
    conn = sqlite3.connect(BASELINE_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT trap, result_json, confidence, card_id FROM analysis WHERE item_id = ? ORDER BY trap",
        (item_id,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("result_json"), str):
            try:
                d["result_json"] = json.loads(d["result_json"])
            except Exception:
                pass
        out.append(d)
    return out


def _del_sqlite(item_id):
    conn = sqlite3.connect(BASELINE_DB)
    conn.execute("DELETE FROM analysis WHERE item_id = ?", (item_id,))
    # Dummy listing zodat FK niet fault (analysis.item_id → listings.item_id CASCADE)
    conn.execute("INSERT OR IGNORE INTO listings (item_id, first_seen_at, last_seen_at, status) VALUES (?, datetime('now'), datetime('now'), 'test')", (item_id,))
    conn.commit()
    conn.close()


def _compare_traps(actual, expected, case_id):
    diffs = []
    if len(actual) != len(expected):
        diffs.append(f"  count mismatch: actual={len(actual)} expected={len(expected)}")
    for a, e in zip(actual, expected):
        for col in ("trap", "confidence", "card_id"):
            if a.get(col) != e.get(col):
                diffs.append(f"  {col}: {a.get(col)!r} vs {e.get(col)!r}")
        # result_json compare (dict vs dict)
        if a.get("result_json") != e.get("result_json"):
            diffs.append(f"  result_json diff (first 100c): {str(a.get('result_json'))[:100]!r} vs {str(e.get('result_json'))[:100]!r}")
    return diffs


def run_baseline(fixtures):
    if BASELINE_DB.exists():
        BASELINE_DB.unlink()
    import storage
    storage._sbs = None
    storage.init_db(BASELINE_DB)
    # Monkeypatch analyze._save_trap's DB_PATH via env — of gebruik SQL direct
    # Voor deze test schrijven we direct via SQL want _save_trap gebruikt harde DB_PATH.
    def _save_trap_direct(item_id, trap, result, confidence=None, card_id=None):
        conn = sqlite3.connect(BASELINE_DB)
        try:
            conn.execute("DELETE FROM analysis WHERE item_id = ? AND trap = ?", (item_id, trap))
            conn.execute(
                "INSERT INTO analysis (item_id, trap, result_json, confidence, card_id, created_at) VALUES (?, ?, ?, ?, ?, datetime('now'))",
                (item_id, trap, json.dumps(result, ensure_ascii=False, default=str), confidence, card_id),
            )
            conn.commit()
        finally:
            conn.close()

    passed, failed = 0, 0
    for i, fix in enumerate(fixtures):
        case = fix["case"]
        item_id = fix["item_id"]
        _del_sqlite(item_id)
        if case == "insert_new":
            _save_trap_direct(item_id, fix["trap"], fix["result"], fix["confidence"], fix["card_id"])
            expected = [{
                "trap": fix["trap"],
                "result_json": fix["result"],
                "confidence": fix["confidence"],
                "card_id": fix["card_id"],
            }]
        elif case == "update_replace":
            _save_trap_direct(item_id, fix["trap"], fix["first_result"], fix["first_confidence"], None)
            _save_trap_direct(item_id, fix["trap"], fix["second_result"], fix["second_confidence"], fix["second_card_id"])
            expected = [{
                "trap": fix["trap"],
                "result_json": fix["second_result"],
                "confidence": fix["second_confidence"],
                "card_id": fix["second_card_id"],
            }]
        else:
            print(f"[{i:02d}] unknown case"); failed += 1; continue

        actual = _fetch_sqlite(item_id)
        diffs = _compare_traps(actual, expected, item_id)
        if diffs:
            print(f"[{i:02d}] {item_id} {case}: FAIL")
            for d in diffs: print(d)
            failed += 1
        else:
            passed += 1
    print(f"\n=== BASELINE (SQLite): {passed}/{len(fixtures)} passed, {failed} failed ===")
    return failed == 0


def run_supabase(fixtures):
    from storage_supabase import analysis as sb_analysis
    passed, failed = 0, 0
    for i, fix in enumerate(fixtures):
        case = fix["case"]
        item_id = fix["item_id"]
        sb_analysis.delete_test_item(item_id)
        if case == "insert_new":
            sb_analysis.save_test_trap(item_id, fix["trap"], fix["result"],
                                       confidence=fix["confidence"], card_id=fix["card_id"])
            expected = [{
                "trap": fix["trap"],
                "result_json": fix["result"],
                "confidence": fix["confidence"],
                "card_id": fix["card_id"],
            }]
        elif case == "update_replace":
            sb_analysis.save_test_trap(item_id, fix["trap"], fix["first_result"],
                                       confidence=fix["first_confidence"], card_id=None)
            sb_analysis.save_test_trap(item_id, fix["trap"], fix["second_result"],
                                       confidence=fix["second_confidence"], card_id=fix["second_card_id"])
            expected = [{
                "trap": fix["trap"],
                "result_json": fix["second_result"],
                "confidence": fix["second_confidence"],
                "card_id": fix["second_card_id"],
            }]
        else:
            print(f"[{i:02d}] unknown case"); failed += 1; continue

        actual = sb_analysis.fetch_test_traps(item_id)
        # normaliseer confidence (numeric → float voor compare)
        for a in actual:
            if a.get("confidence") is not None:
                a["confidence"] = float(a["confidence"])
        for e in expected:
            if e.get("confidence") is not None:
                e["confidence"] = float(e["confidence"])
        diffs = _compare_traps(actual, expected, item_id)
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
