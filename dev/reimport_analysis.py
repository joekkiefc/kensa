#!/usr/bin/env python3
"""Re-import alleen kensa.analysis (na TRUNCATE + unique-constraint).

Alleen analyses die horen bij een bestaande listing in Supabase.
"""
import json, sqlite3, time
from pathlib import Path
import requests

S = json.loads(Path("/home/pi/.openclaw/secrets.json").read_text())["supabase"]
DB = "/home/pi/.openclaw/workspace/agents/kensa/kensa.db"
BATCH = 300

H = {
    "apikey": S["service_role_key"],
    "Authorization": f"Bearer {S['service_role_key']}",
    "Content-Type": "application/json",
    "Content-Profile": "kensa",
    "Accept-Profile": "kensa",
    "Prefer": "return=minimal,resolution=ignore-duplicates",
}


def post(url, chunk, table):
    delay = 1
    for _ in range(6):
        try:
            r = requests.post(url, headers=H, json=chunk, timeout=120)
        except (requests.ConnectionError, requests.Timeout):
            time.sleep(delay); delay = min(delay*2, 32); continue
        if r.status_code < 300 or r.status_code == 409:
            return
        if 500 <= r.status_code < 600:
            time.sleep(delay); delay = min(delay*2, 32); continue
        raise RuntimeError(f"{table} {r.status_code}: {r.text[:400]}")
    raise RuntimeError(f"{table} exhausted retries")


def main():
    # Haal set van behouden item_ids op (via listings)
    print("Kandidaat item_ids ophalen uit Supabase.listings…")
    keep = set()
    offset = 0
    while True:
        r = requests.get(
            f"{S['url']}/rest/v1/listings",
            headers={**H, "Range-Unit": "items", "Range": f"{offset}-{offset+999}"},
            params={"select": "item_id"},
            timeout=60,
        )
        if r.status_code not in (200, 206):
            raise RuntimeError(f"listings fetch: {r.status_code} {r.text[:200]}")
        batch = r.json()
        if not batch:
            break
        keep.update(x["item_id"] for x in batch)
        offset += len(batch)
        if len(batch) < 1000:
            break
    print(f"  Behouden listings: {len(keep):,}")

    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row

    rows = []
    seen = set()
    sent = 0
    q = con.execute("SELECT item_id, trap, result_json, confidence, card_id, created_at FROM analysis")
    for r in q:
        if r["item_id"] not in keep:
            continue
        key = (r["item_id"], r["trap"], r["created_at"])
        if key in seen:
            continue
        seen.add(key)
        d = dict(r)
        try:
            d["result_json"] = json.loads(d["result_json"]) if d["result_json"] else None
        except Exception:
            d["result_json"] = None
        rows.append(d)
        if len(rows) >= BATCH:
            post(f"{S['url']}/rest/v1/analysis", rows, "analysis")
            sent += len(rows)
            if sent % 30000 == 0:
                print(f"  · {sent:,}")
            rows = []
    if rows:
        post(f"{S['url']}/rest/v1/analysis", rows, "analysis")
        sent += len(rows)
    print(f"  Totaal ingezet: {sent:,}")


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"Duur: {time.time()-t0:.0f}s")
