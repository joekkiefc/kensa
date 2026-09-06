#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa worker 1/3 — OCR + LLM fase.

Pakt items met slab_status='pending' (detail-fetch klaar, OCR nog niet gedaan),
draait check_slab + LLM-enrichment en zet slab_status='ocr_done' met card_key.
"""

import argparse
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze import DB_PATH, analyze_ocr_only  # noqa: E402


def pick_batch(limit: int) -> list[str]:
    # Supabase-first switch: KENSA_READ_STORAGE=supabase gebruikt de Supabase-read.
    if os.environ.get("KENSA_READ_STORAGE", "").lower() == "supabase":
        from storage_supabase import pick_ocr_batch_supabase
        return pick_ocr_batch_supabase(limit)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT item_id FROM listings
               WHERE detail_scraped_at IS NOT NULL
                 AND (slab_status='pending' OR slab_status IS NULL)
               ORDER BY first_seen_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [r["item_id"] for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    verbose = not args.quiet
    t0 = time.perf_counter()

    batch = pick_batch(args.limit)
    if not batch:
        print(f"[ocr] geen pending items", file=sys.stderr)
        return 0
    print(f"[ocr] batch: {len(batch)} items, parallel={args.parallel}", file=sys.stderr)

    ok = fail = 0
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futs = {pool.submit(analyze_ocr_only, iid, verbose): iid for iid in batch}
        for fut in as_completed(futs):
            iid = futs[fut]
            try:
                r = fut.result()
                if "error" in r:
                    fail += 1
                    print(f"  [ocr] {iid} error: {r['error']}", file=sys.stderr)
                else:
                    ok += 1
            except Exception as e:
                fail += 1
                print(f"  [ocr] {iid} crash: {e}", file=sys.stderr)

    took = time.perf_counter() - t0
    print(f"[ocr] klaar — {ok} ok / {fail} fail in {took:.1f}s ({ok / max(took, 1) * 3600:.0f}/u)",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
