#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa worker 2/3 — Cache-hit finalizer.

Pakt items met slab_status='ocr_done' + card_key + verse price_cache eBay-hit,
draait score + CM enqueue + summary. Elk item ~1s (geen live eBay).
"""

import argparse
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze import DB_PATH, PRICE_CACHE_DAYS, analyze_score_only  # noqa: E402


def pick_batch(limit: int) -> list[str]:
    # Supabase-first switch: KENSA_READ_STORAGE=supabase gebruikt de Supabase-read.
    # Fallback naar SQLite als env-var niet gezet is (backwards compat + rollback).
    if os.environ.get("KENSA_READ_STORAGE", "").lower() == "supabase":
        from storage_supabase import pick_cache_batch_supabase
        return pick_cache_batch_supabase(limit, PRICE_CACHE_DAYS)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"""SELECT l.item_id
                FROM listings l
                JOIN price_cache pc ON pc.card_key = l.card_key
                WHERE l.slab_status='ocr_done'
                  AND l.card_key IS NOT NULL
                  AND pc.ebay_fetched_at IS NOT NULL
                  AND pc.ebay_result_json IS NOT NULL
                  AND datetime(pc.ebay_fetched_at) >= datetime('now', '-{int(PRICE_CACHE_DAYS)} days')
                ORDER BY l.first_seen_at DESC
                LIMIT ?""",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [r["item_id"] for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    verbose = not args.quiet
    t0 = time.perf_counter()

    batch = pick_batch(args.limit)
    if not batch:
        print(f"[cache] geen cache-hit items in queue", file=sys.stderr)
        return 0
    print(f"[cache] batch: {len(batch)} items, parallel={args.parallel}", file=sys.stderr)

    done = skip = fail = 0
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futs = {pool.submit(analyze_score_only, iid, verbose, "cache_only"): iid for iid in batch}
        for fut in as_completed(futs):
            iid = futs[fut]
            try:
                r = fut.result()
                if "error" in r:
                    fail += 1
                    print(f"  [cache] {iid} error: {r['error']}", file=sys.stderr)
                elif "skip" in r:
                    skip += 1
                else:
                    done += 1
            except Exception as e:
                fail += 1
                print(f"  [cache] {iid} crash: {e}", file=sys.stderr)

    took = time.perf_counter() - t0
    print(f"[cache] klaar — {done} done / {skip} skip / {fail} fail in {took:.1f}s "
          f"({done / max(took, 1) * 3600:.0f}/u)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
