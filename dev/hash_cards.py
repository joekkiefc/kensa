#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Perceptual hash builder voor public.cards → inventory.card_image_hashes.

Doel: elke kaart met een `card_image` in de DB krijgt 3 perceptual hashes
(phash, dhash, colorhash) zodat we bij scan-input in <100ms een match kunnen
vinden onder 45k+ kaarten.

Gebruik:
    python3 hash_cards.py --limit 1000 --workers 2   # test-sample
    python3 hash_cards.py --all --workers 4          # volledige run
    python3 hash_cards.py --resume --workers 2       # alleen nog-niet-gehasht

Meet Pi-belasting (RAM/CPU/swap) elke 30 sec en logt.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from hashlib import sha256
from pathlib import Path

import imagehash
from PIL import Image

SECRETS = Path("/home/pi/.openclaw/secrets.json")


def _config() -> tuple[str, dict]:
    s = json.loads(SECRETS.read_text())["supabase"]
    return s["url"], {
        "apikey": s["service_role_key"],
        "Authorization": f"Bearer {s['service_role_key']}",
    }


def _fetch_batch(base: str, headers: dict, offset: int, limit: int) -> list[dict]:
    """Fetch een batch cards met card_image gevuld (geen filter, plain fetch)."""
    qs = urllib.parse.urlencode({
        "select": "id,card_image",
        "card_image": "not.is.null",
        "limit": str(limit),
        "offset": str(offset),
        "order": "id.asc",
    })
    req = urllib.request.Request(f"{base}/rest/v1/cards?{qs}", headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def _fetch_existing_ids(base: str, headers: dict) -> set[str]:
    """Haal alle card_ids op die al gehashed zijn (voor --resume mode)."""
    ids: set[str] = set()
    offset = 0
    page = 1000
    while True:
        qs = urllib.parse.urlencode({
            "select": "card_id",
            "limit": str(page),
            "offset": str(offset),
        })
        req = urllib.request.Request(
            f"{base}/rest/v1/card_image_hashes?{qs}",
            headers={**headers, "Accept-Profile": "inventory"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            batch = json.loads(resp.read())
        if not batch:
            break
        for row in batch:
            ids.add(row["card_id"])
        if len(batch) < page:
            break
        offset += page
    return ids


def _decode_bytea(hex_string: str) -> bytes:
    """PostgreSQL bytea in \\x-hex format decoderen naar bytes."""
    if hex_string.startswith("\\x"):
        return bytes.fromhex(hex_string[2:])
    return hex_string.encode()


def _to_signed_bigint(v: int) -> int:
    """Postgres bigint is signed 64-bit. Unsigned 64-bit hashes moeten
    wrappen naar negatief zodat ze passen. Bit-patroon blijft hetzelfde
    → XOR + bit_count werken identiek bij lookup."""
    if v >= 2**63:
        return v - 2**64
    return v


def _hash_one(card_id: str, image_bytes: bytes) -> dict | None:
    """Bereken alle 3 hashes voor 1 image. Returns dict of None bij error."""
    try:
        source_hash = sha256(image_bytes).hexdigest()
        im = Image.open(io.BytesIO(image_bytes))
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        # imagehash returns objects — .hash geeft numpy bool-array, we serialiseren als int
        phash = _to_signed_bigint(int(str(imagehash.phash(im, hash_size=8)), 16))
        dhash = _to_signed_bigint(int(str(imagehash.dhash(im, hash_size=8)), 16))
        colorhash = _to_signed_bigint(int(str(imagehash.colorhash(im, binbits=3)), 16))
        return {
            "card_id": card_id,
            "phash": phash,
            "dhash": dhash,
            "colorhash": colorhash,
            "source_hash": source_hash,
        }
    except Exception as e:
        print(f"  ❌ {card_id[:8]}...: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def _upsert_batch(base: str, headers: dict, rows: list[dict]) -> int:
    """Insert of update batch resultaten."""
    if not rows:
        return 0
    payload = json.dumps(rows).encode()
    req = urllib.request.Request(
        f"{base}/rest/v1/card_image_hashes",
        data=payload,
        method="POST",
        headers={
            **headers,
            "Content-Type": "application/json",
            "Content-Profile": "inventory",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return len(rows) if resp.status in (200, 201, 204) else 0


def _pi_snapshot() -> str:
    """Lees load/mem/swap uit /proc."""
    try:
        load = os.getloadavg()
        with open("/proc/meminfo") as f:
            meminfo = {}
            for line in f:
                key, _, rest = line.partition(":")
                val_kb = int(rest.strip().split()[0])
                meminfo[key] = val_kb
        mem_used_mb = (meminfo["MemTotal"] - meminfo["MemAvailable"]) // 1024
        mem_total_mb = meminfo["MemTotal"] // 1024
        swap_used_mb = (meminfo["SwapTotal"] - meminfo["SwapFree"]) // 1024
        swap_total_mb = meminfo["SwapTotal"] // 1024
        return (
            f"load={load[0]:.2f}/{load[1]:.2f}/{load[2]:.2f} "
            f"mem={mem_used_mb}/{mem_total_mb}MB "
            f"swap={swap_used_mb}/{swap_total_mb}MB"
        )
    except Exception as e:
        return f"snapshot err: {e}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=1000,
                        help="max cards om te verwerken (default: 1000)")
    parser.add_argument("--all", action="store_true",
                        help="verwerk ALLE cards (negeert --limit)")
    parser.add_argument("--resume", action="store_true",
                        help="skip cards die al een hash hebben")
    parser.add_argument("--workers", type=int, default=2,
                        help="parallelle workers (default: 2)")
    parser.add_argument("--batch-size", type=int, default=50,
                        help="cards per fetch-batch (default: 50)")
    parser.add_argument("--monitor-interval", type=int, default=30,
                        help="seconden tussen Pi-snapshot logs (default: 30)")
    args = parser.parse_args()

    base, headers = _config()
    start = time.perf_counter()
    total_processed = 0
    total_stored = 0
    total_errors = 0
    last_snapshot = start
    target = None if args.all else args.limit

    print(f"=== hash_cards.py start ===")
    print(f"target: {'ALL' if args.all else args.limit} cards")
    print(f"workers: {args.workers}")
    print(f"batch-size: {args.batch_size}")
    print(f"resume: {args.resume}")
    print(f"initial: {_pi_snapshot()}", flush=True)

    existing_ids: set[str] = set()
    if args.resume:
        print("→ Fetching existing hashed IDs...", flush=True)
        existing_ids = _fetch_existing_ids(base, headers)
        print(f"→ {len(existing_ids)} cards al gehashed, skip die.", flush=True)
    print()

    offset = 0
    while target is None or total_processed < target:
        # Bepaal fetch-limit voor deze batch
        fetch_limit = args.batch_size
        if target is not None:
            remaining = target - total_processed
            if remaining <= 0:
                break
            fetch_limit = min(args.batch_size, remaining)

        raw_rows = _fetch_batch(base, headers, offset=offset, limit=fetch_limit)
        if not raw_rows:
            print(f"→ Geen cards meer om op te halen op offset {offset}. Klaar.")
            break
        # Advance offset by ALL fetched (niet gefilterd) zodat we niet vasthangen
        offset += len(raw_rows)
        rows = [r for r in raw_rows if r["id"] not in existing_ids] if args.resume else raw_rows
        if not rows:
            # Deze batch bevatte alleen al-gehashde cards; ga door naar de volgende
            continue

        # Parallel hashing
        results = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(_hash_one, row["id"], _decode_bytea(row["card_image"])): row
                for row in rows
            }
            for fut in as_completed(futures):
                r = fut.result()
                if r:
                    results.append(r)
                else:
                    total_errors += 1

        # Upsert
        stored = _upsert_batch(base, headers, results)
        total_stored += stored
        total_processed += len(rows)
        # NB: offset is al advanced boven met len(raw_rows)

        # Pi-monitor snapshot
        now = time.perf_counter()
        if now - last_snapshot >= args.monitor_interval:
            elapsed = now - start
            rate = total_processed / elapsed if elapsed > 0 else 0
            eta = None
            if target is not None:
                remaining = target - total_processed
                eta = f", ETA {remaining/rate/60:.1f}min" if rate > 0 else ""
            print(
                f"[{elapsed:6.1f}s] processed={total_processed} "
                f"stored={total_stored} errors={total_errors} "
                f"rate={rate:.1f}/s | {_pi_snapshot()}"
                f"{eta or ''}"
            )
            last_snapshot = now

    elapsed = time.perf_counter() - start
    print()
    print(f"=== KLAAR ===")
    print(f"Verwerkt: {total_processed} cards in {elapsed:.1f}s "
          f"({total_processed/elapsed:.1f}/s)")
    print(f"Opgeslagen: {total_stored} · errors: {total_errors}")
    print(f"final: {_pi_snapshot()}")


if __name__ == "__main__":
    main()
