#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa worker 3/3 — eBay-fetcher (cache-miss).

Pakt items met slab_status='ocr_done' zonder verse price_cache hit,
draait de trage live eBay-fetch + score + CM + summary. Elk item 30-80s.
"""

import argparse
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import os
from analyze import analyze_score_only, PRICE_CACHE_DAYS  # noqa: E402
from worker_claim import claim_ebay_batch  # noqa: E402

WORKER_NAME = "ebay_1"


def pick_batch(limit: int) -> list[str]:
    # Supabase-first switch: KENSA_READ_STORAGE=supabase leest van Supabase.
    # Locking is niet nodig want cron_worker_ebay.sh gebruikt flock (single-instance)
    # en --parallel 1 default. Fallback op SQLite met lock-mechaniek bij afwezige env-var.
    if os.environ.get("KENSA_READ_STORAGE", "").lower() == "supabase":
        from storage_supabase import pick_ebay_batch_supabase
        return pick_ebay_batch_supabase(limit, PRICE_CACHE_DAYS)
    return claim_ebay_batch(WORKER_NAME, limit)


CRASH_STATUS = "ebay_error"   # ≠ 'ocr_done' → pick_ebay_batch_supabase / claim_ebay_batch zien 'm niet meer


def _sluit_af_na_crash(item_id: str, exc: BaseException) -> None:
    """Een crash in analyze_score_only liet het item op 'ocr_done' staan → elke run
    opnieuw (462x/24u op 2 lots, 11-9). Er was geen fout-pad: 'error'-dicts markeren
    niets. Minimaal vangnet: slab_status → 'ebay_error' zodat de picker 'm uitsluit.
    Schrijft via dezelfde dispatcher als de score-fase (KENSA_WRITE_STORAGE)."""
    try:
        from analyze_split.score_only import _mark_slab_status
        _mark_slab_status(item_id, CRASH_STATUS)
        print(f"  [ebay] {item_id} → slab_status={CRASH_STATUS} ({type(exc).__name__})", file=sys.stderr)
    except Exception as e2:
        print(f"  [ebay] {item_id} afsluiten na crash MISLUKT: {type(e2).__name__}: {e2}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    # Chromium ProcessSingleton laat maar 1 instance per PROFILE_DIR toe;
    # ebay_lastsold serializeert al via _PROFILE_LOCK, dus >1 thread is verspilling.
    ap.add_argument("--parallel", type=int, default=1)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    verbose = not args.quiet
    t0 = time.perf_counter()

    batch = pick_batch(args.limit)
    if not batch:
        print(f"[ebay] geen cache-miss items in queue", file=sys.stderr)
        return 0
    print(f"[ebay] batch: {len(batch)} items, parallel={args.parallel}", file=sys.stderr)

    done = skip = fail = live_hits = 0
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futs = {pool.submit(analyze_score_only, iid, verbose, "ebay_only"): iid for iid in batch}
        for fut in as_completed(futs):
            iid = futs[fut]
            try:
                r = fut.result()
                if "error" in r:
                    fail += 1
                    print(f"  [ebay] {iid} error: {r['error']}", file=sys.stderr)
                elif "skip" in r:
                    skip += 1
                else:
                    done += 1
                    if not (r.get("summary") or {}).get("ebay_from_cache"):
                        live_hits += 1
            except Exception as e:
                fail += 1
                print(f"  [ebay] {iid} crash: {type(e).__name__}: {e}", file=sys.stderr)
                _sluit_af_na_crash(iid, e)

    if live_hits >= 5:
        pause = random.uniform(30.0, 60.0)
        print(f"  [ebay-pace] {live_hits} live-fetches — cooldown {pause:.0f}s", file=sys.stderr)
        time.sleep(pause)

    took = time.perf_counter() - t0
    print(f"[ebay] klaar — {done} done ({live_hits} live) / {skip} skip / {fail} fail in {took:.1f}s "
          f"({done / max(took, 1) * 3600:.0f}/u)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
