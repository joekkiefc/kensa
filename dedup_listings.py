#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa dedup-listings — houdt Pi + Supabase in sync met dezelfde dedup-regel.

Regels (gelijk aan migrate_to_supabase.py comment sectie):
  1. Per card_key IS NOT NULL: max 20 nieuwste rijen
     ORDER BY COALESCE(last_seen_at, first_seen_at, detail_scraped_at) DESC.
  2. card_key IS NULL: alleen behouden als last_seen_at >= now - 28 dagen.

Dry-run default (print counts + sample). --apply doet daadwerkelijke deletes.
Beide DBs worden geraakt zodat ze consistent blijven. Cascade-deletes gaan
automatisch via FK ON DELETE CASCADE (photos, analysis, cardmarket_queue,
cert_sightings).

Log: dedup_listings.log
Backup dry-run: /tmp/kensa_dedup_preview.json (dry-run) of
                ~/.openclaw/backups/kensa-dedup-<ts>.json (live).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

DB_PATH = "/home/pi/.openclaw/workspace/agents/kensa/kensa.db"
SECRETS_PATH = "/home/pi/.openclaw/secrets.json"
BACKUP_DIR = Path("/home/pi/.openclaw/backups")
LOG_PATH = Path("/home/pi/.openclaw/workspace/agents/kensa/dedup_listings.log")
BATCH_DELETE = 200  # Supabase IN-clause chunk size


def _log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat()} {msg}\n"
    try:
        with LOG_PATH.open("a") as f:
            f.write(line)
    except Exception:
        pass
    print(msg, file=sys.stderr)


def _load_supabase_creds() -> tuple[str, str]:
    s = json.loads(Path(SECRETS_PATH).read_text())["supabase"]
    return s["url"], s["service_role_key"]


def _sb_headers(key: str, prefer: str = "return=minimal") -> dict:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept-Profile": "kensa",
        "Content-Profile": "kensa",
        "Prefer": prefer,
    }


def pi_ids_to_delete() -> list[str]:
    """Verzamel op Pi de item_ids die weg moeten volgens de regels."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        # Regel 1: excess per card_key
        rows_ck = conn.execute("""
            WITH ranked AS (
                SELECT item_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY card_key
                        ORDER BY COALESCE(last_seen_at, first_seen_at, detail_scraped_at) DESC
                    ) rn
                FROM listings WHERE card_key IS NOT NULL
            )
            SELECT item_id FROM ranked WHERE rn > 20
        """).fetchall()

        # Regel 2: null card_key + ouder dan 28 dagen
        rows_null = conn.execute("""
            SELECT item_id FROM listings
            WHERE card_key IS NULL
              AND (last_seen_at IS NULL OR datetime(last_seen_at) < datetime('now', '-28 days'))
        """).fetchall()
    finally:
        conn.close()

    ids = [r["item_id"] for r in rows_ck] + [r["item_id"] for r in rows_null]
    return sorted(set(ids))


def sb_ids_to_delete(url: str, key: str) -> list[str]:
    """Verzamel op Supabase de item_ids die weg moeten volgens dezelfde regels.

    Postgres ondersteunt window functions native — via een RPC-vrije aanpak
    gebruiken we een SQL-query via de PostgREST /rpc-endpoint alternatief:
    ophalen van alle card_key groups en client-side dedupen. Voor 72k rijen
    is dat een ~seconde werk, prima acceptabel voor een dagelijkse cron.
    """
    # PostgREST hard-cap = 1000 rows per response. Range-header voor pagination.
    all_rows: list[dict] = []
    offset = 0
    page = 1000
    while True:
        headers = _sb_headers(key, "return=representation")
        headers["Range-Unit"] = "items"
        headers["Range"] = f"{offset}-{offset + page - 1}"
        r = requests.get(
            f"{url}/rest/v1/listings",
            headers=headers,
            params={
                "select": "item_id,card_key,last_seen_at,first_seen_at,detail_scraped_at",
                "order": "card_key.asc.nullsfirst,item_id.asc",
            },
            timeout=60,
        )
        if r.status_code >= 300 and r.status_code != 206:
            raise RuntimeError(f"sb_ids fetch offset={offset}: HTTP {r.status_code} {r.text[:200]}")
        chunk = r.json()
        if not chunk:
            break
        all_rows.extend(chunk)
        if len(chunk) < page:
            break
        offset += page

    # Client-side dedup per regel
    from datetime import timedelta
    cutoff_28d = datetime.now(timezone.utc) - timedelta(days=28)

    # Regel 1: per card_key top 20 (sort DESC on last/first/detail)
    by_ck: dict[str, list[dict]] = {}
    to_delete: list[str] = []
    for row in all_rows:
        ck = row.get("card_key")
        iid = row["item_id"]
        if ck is None:
            # Regel 2 check
            lsa = row.get("last_seen_at")
            keep = False
            if lsa:
                try:
                    lsa_dt = datetime.fromisoformat(lsa.replace("Z", "+00:00"))
                    if lsa_dt.tzinfo is None:
                        lsa_dt = lsa_dt.replace(tzinfo=timezone.utc)
                    keep = lsa_dt >= cutoff_28d
                except Exception:
                    pass
            if not keep:
                to_delete.append(iid)
        else:
            by_ck.setdefault(ck, []).append(row)

    def _sort_key(r: dict) -> str:
        return (r.get("last_seen_at") or r.get("first_seen_at")
                or r.get("detail_scraped_at") or "")

    for ck, rows in by_ck.items():
        rows.sort(key=_sort_key, reverse=True)
        for r in rows[20:]:
            to_delete.append(r["item_id"])

    return sorted(set(to_delete))


def delete_from_pi(ids: list[str]) -> int:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        # In batches (SQLite handelt IN-clauses tot ~999 parameters)
        deleted = 0
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            placeholders = ",".join(["?"] * len(chunk))
            cur = conn.execute(f"DELETE FROM listings WHERE item_id IN ({placeholders})", chunk)
            deleted += cur.rowcount
        conn.commit()
        return deleted
    finally:
        conn.close()


def delete_from_supabase(url: str, key: str, ids: list[str]) -> int:
    deleted = 0
    for i in range(0, len(ids), BATCH_DELETE):
        chunk = ids[i:i + BATCH_DELETE]
        # PostgREST DELETE ondersteunt IN-clause via item_id=in.(a,b,c)
        r = requests.delete(
            f"{url}/rest/v1/listings",
            headers=_sb_headers(key, "return=minimal,count=exact"),
            params={"item_id": f"in.({','.join(chunk)})"},
            timeout=60,
        )
        if r.status_code >= 300:
            _log(f"sb DELETE chunk fail HTTP {r.status_code}: {r.text[:200]}")
            continue
        # Content-Range header bevat count
        cr = r.headers.get("content-range", "")
        try:
            deleted += int(cr.split("/")[-1])
        except (ValueError, IndexError):
            deleted += len(chunk)  # aanname als count niet parseerbaar
    return deleted


def backup_ids(pi_ids: list[str], sb_ids: list[str], apply: bool) -> Path:
    """Sla lijst van te-verwijderen IDs op voor rollback / audit."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if apply:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        path = BACKUP_DIR / f"kensa-dedup-{ts}.json"
    else:
        path = Path(f"/tmp/kensa_dedup_preview_{ts}.json")
    path.write_text(json.dumps({
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "apply": apply,
        "pi_ids": pi_ids,
        "sb_ids": sb_ids,
        "counts": {"pi": len(pi_ids), "sb": len(sb_ids), "overlap": len(set(pi_ids) & set(sb_ids))},
    }, indent=2))
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Voer daadwerkelijk deletes uit. Default = dry-run.")
    ap.add_argument("--skip-supabase", action="store_true", help="Alleen Pi opruimen, Supabase overslaan.")
    ap.add_argument("--skip-pi", action="store_true", help="Alleen Supabase opruimen, Pi overslaan.")
    args = ap.parse_args()

    t0 = time.perf_counter()
    _log(f"dedup start apply={args.apply} skip_pi={args.skip_pi} skip_sb={args.skip_supabase}")

    url, key = _load_supabase_creds()

    if not args.skip_pi:
        _log("Pi: verzamelen te-verwijderen item_ids...")
        pi_ids = pi_ids_to_delete()
    else:
        pi_ids = []
    _log(f"Pi: {len(pi_ids)} items te verwijderen")

    if not args.skip_supabase:
        _log("Supabase: verzamelen te-verwijderen item_ids...")
        sb_ids = sb_ids_to_delete(url, key)
    else:
        sb_ids = []
    _log(f"Supabase: {len(sb_ids)} items te verwijderen")

    overlap = len(set(pi_ids) & set(sb_ids)) if pi_ids and sb_ids else 0
    _log(f"Overlap: {overlap} items zijn op beide DBs weg te halen")

    backup_path = backup_ids(pi_ids, sb_ids, args.apply)
    _log(f"Backup/preview: {backup_path}")

    if not args.apply:
        _log("Dry-run — GEEN deletes uitgevoerd. Gebruik --apply voor live-run.")
        _log(f"Klaar in {time.perf_counter()-t0:.1f}s")
        return 0

    if pi_ids and not args.skip_pi:
        _log(f"Pi: DELETE start ({len(pi_ids)} items)...")
        n = delete_from_pi(pi_ids)
        _log(f"Pi: DELETE klaar — {n} rows deleted (cascade naar photos/analysis/cm_queue via FK)")

    if sb_ids and not args.skip_supabase:
        _log(f"Supabase: DELETE start ({len(sb_ids)} items)...")
        n = delete_from_supabase(url, key, sb_ids)
        _log(f"Supabase: DELETE klaar — {n} rows deleted (cascade via schema FKs)")

    _log(f"dedup done in {time.perf_counter()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
