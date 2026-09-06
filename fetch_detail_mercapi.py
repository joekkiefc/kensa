#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa Stap 2 — Mercari detail-fetch via mercapi (directe API).

Vervangt de trage Buyee-scrape voor Mercari-items (id start met 'm'). Werkt
tegen api.mercari.jp met JWT (mercapi lib regelt dat), ~0.35s per item ipv
20-25s met Chromium+WAF.

PayPay-items (id start met 'z') worden GESKIPT — die blijven via
fetch_detail.py naar Buyee lopen.

Usage:
  ./fetch_detail_mercapi.py <item_id>              # scrape één item, print result
  ./fetch_detail_mercapi.py --all <limit>          # scrape <limit> nieuwe Mercari-items uit queue
  ./fetch_detail_mercapi.py --recheck <limit> [min-age-hours]
                                                    # re-check bestaande deal-items op sold-status
"""

import asyncio
import json
import os
import sys
import time

from mercapi import Mercapi

import storage
import translate as translate_mod

DETAIL_URL_TEMPLATE = "https://buyee.jp/mercari/item/{item_id}"


def _map_item_to_row(item, item_id: str) -> dict:
    """Zet een mercapi Item om naar een LISTING_COLS-compatible dict.

    Mercari-status → onze sold-flag:
      - 'sold_out' → sold=1
      - 'on_sale' / 'trading' → sold=0
    """
    status = getattr(item, "status", None)
    status_str = getattr(status, "value", None) or str(status or "")
    sold_flag = 1 if status_str.lower() in ("sold_out", "sold", "trading_sold") else 0

    seller = getattr(item, "seller", None)
    seller_id = str(getattr(seller, "id_", "") or "") if seller else None
    seller_name = getattr(seller, "name", None) if seller else None

    category = getattr(item, "item_category", None)
    category_path = getattr(category, "name", None) if category else None

    condition = getattr(item, "item_condition", None)
    condition_str = getattr(condition, "name", None) if condition else None

    row = {
        "item_id": item_id,
        "title_jp": getattr(item, "name", None),
        "price_jpy": int(getattr(item, "price", 0) or 0) or None,
        "seller_id": seller_id,
        "seller_name": seller_name,
        "category_path": category_path,
        "condition": condition_str,
        "sold": sold_flag,
        "description_jp": getattr(item, "description", None),
        "detail_url": DETAIL_URL_TEMPLATE.format(item_id=item_id),
        "detail_scraped_at": storage.now_iso(),
    }

    # Store extra Mercari-only signals in extra_json (num_likes, is_shop_item, etc)
    extra = {
        "num_likes": getattr(item, "num_likes", None),
        "num_comments": getattr(item, "num_comments", None),
        "is_shop_item": str(getattr(item, "is_shop_item", "")) or None,
        "mercari_status": status_str or None,
    }
    extra = {k: v for k, v in extra.items() if v is not None}
    if extra:
        row["extra_json"] = json.dumps(extra, ensure_ascii=False)

    return row


def _photo_urls(item) -> list[str]:
    """mercapi.item().photos is een list van string-URLs (static.mercdn.net)."""
    photos = getattr(item, "photos", None) or []
    seen = set()
    out = []
    for p in photos:
        u = str(p).split("?")[0]  # strip cache-busting querystring
        if u in seen:
            continue
        seen.add(u)
        out.append(str(p))
    return out


async def _fetch_one(m: Mercapi, item_id: str, db_path=None) -> dict | None:
    """Fetch één Mercari-item, upsert in DB, return meta-dict.
    Return None als item niet bestaat (sold/removed uit Mercari)."""
    t0 = time.time()
    try:
        item = await m.item(item_id)
    except KeyError as e:
        # mercapi gooit KeyError('data') als Mercari geen response geeft — item is verwijderd/verkocht.
        # Markeer sold zodat we 'm niet blijven proberen.
        if str(e) == "'data'":
            print(f"  SOLD {item_id}: mercari 404 (verkocht/verwijderd) — sold=1", file=sys.stderr)
            storage.upsert_listing({
                "item_id": item_id,
                "sold": 1,
                "detail_scraped_at": storage.now_iso(),
                "detail_url": DETAIL_URL_TEMPLATE.format(item_id=item_id),
            }, db_path=db_path)
            return None
        print(f"  FAIL {item_id}: KeyError: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  FAIL {item_id}: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    if item is None:
        print(f"  SOLD {item_id}: item niet gevonden — sold=1", file=sys.stderr)
        storage.upsert_listing({
            "item_id": item_id,
            "sold": 1,
            "detail_scraped_at": storage.now_iso(),
            "detail_url": DETAIL_URL_TEMPLATE.format(item_id=item_id),
        }, db_path=db_path)
        return None

    row = _map_item_to_row(item, item_id)
    if row.get("title_jp"):
        row["title_en"] = translate_mod.translate(row["title_jp"])

    storage.upsert_listing(row, db_path=db_path)
    photos = _photo_urls(item)
    inserted_photos = storage.upsert_photos(item_id, photos, db_path=db_path)

    dt = time.time() - t0
    print(
        f"  OK {item_id}: {(row.get('title_jp') or '')[:50]} — ¥{row.get('price_jpy','?')} "
        f"— photos {len(photos)} — {dt:.2f}s",
        file=sys.stderr,
    )
    return {
        "item_id": item_id,
        "photos_inserted": inserted_photos,
        "photos_total": len(photos),
        "fetch_seconds": dt,
    }


def _pending_mercari_ids(db_path=None, limit: int | None = None) -> list[str]:
    """Alle listings zonder detail_scraped_at, alleen Mercari. Nieuwste eerst.
    Selecteer via detail_url (buyee.jp/mercari/...) zodat zowel legacy 'm*'-ids
    als nieuwe '2J*'-ids na Buyee's format-change van 2026 worden gepakt."""
    if db_path is None and os.environ.get("KENSA_READ_STORAGE", "").lower() == "supabase":
        from storage_supabase import pick_mercapi_batch_supabase
        return pick_mercapi_batch_supabase(limit=limit)
    conn = storage._connect(db_path)
    try:
        sql = ("SELECT item_id FROM listings "
               "WHERE detail_scraped_at IS NULL AND detail_url LIKE '%/mercari/%' "
               "ORDER BY first_seen_at DESC")
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [r["item_id"] for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def _deal_recheck_ids(db_path=None, limit: int = 100, min_age_hours: int = 4) -> list[str]:
    """Mercari-items die als deal zichtbaar zijn (roi>0, niet onbetrouwbaar), nog niet sold,
    en langer dan min_age_hours niet gezien. Voor sold-status refresh — geen nieuwe items."""
    if db_path is None and os.environ.get("KENSA_READ_STORAGE", "").lower() == "supabase":
        from storage_supabase import pick_mercapi_recheck_batch_supabase
        return pick_mercapi_recheck_batch_supabase(limit=limit, min_age_hours=min_age_hours)
    conn = storage._connect(db_path)
    try:
        rows = conn.execute(
            """SELECT l.item_id
               FROM listings l
               JOIN analysis a ON a.item_id = l.item_id AND a.trap = 'summary'
               WHERE l.detail_url LIKE '%/mercari/%'
                 AND (l.sold IS NULL OR l.sold = 0)
                 AND json_extract(a.result_json, '$.roi_avg3_pct') > 0
                 AND json_extract(a.result_json, '$.roi_avg3_pct') <= 100
                 AND json_extract(a.result_json, '$.verdict') != 'onbetrouwbaar'
                 AND l.last_seen_at < datetime('now', ?)
               ORDER BY l.last_seen_at ASC
               LIMIT ?""",
            (f"-{int(min_age_hours)} hours", int(limit)),
        ).fetchall()
        return [r["item_id"] for r in rows]
    finally:
        conn.close()


async def _run_batch(ids: list[str], throttle: float = 0.3) -> tuple[int, int, int]:
    """Sequentieel fetchen met kleine throttle tussen items. Returns (ok, missing, fail)."""
    m = Mercapi()
    ok = missing = fail = 0
    for i, iid in enumerate(ids, 1):
        result = await _fetch_one(m, iid)
        if result is not None:
            ok += 1
        else:
            # onderscheid tussen missing (upsert wel gebeurd) en fail (upsert niet)
            # via detail_scraped_at controle — kort en simpel: alleen fail als het
            # item nog steeds pending is
            if os.environ.get("KENSA_READ_STORAGE", "").lower() == "supabase":
                from storage_supabase import load_detail_status_supabase
                ts = load_detail_status_supabase(iid)
                if ts:
                    missing += 1
                else:
                    fail += 1
            else:
                conn = storage._connect()
                try:
                    row = conn.execute(
                        "SELECT detail_scraped_at FROM listings WHERE item_id=?", (iid,)
                    ).fetchone()
                    if row and row["detail_scraped_at"]:
                        missing += 1
                    else:
                        fail += 1
                finally:
                    conn.close()
        if i < len(ids) and throttle > 0:
            await asyncio.sleep(throttle)
    return ok, missing, fail


def main(argv):
    storage.init_db()

    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    if argv[1] == "--all":
        limit = int(argv[2]) if len(argv) > 2 else 100
        ids = _pending_mercari_ids(limit=limit)
        print(f"Pending Mercari-items: {len(ids)}", file=sys.stderr)
        if not ids:
            return
        t0 = time.time()
        ok, missing, fail = asyncio.run(_run_batch(ids))
        dt = time.time() - t0
        print(
            f"\n=== mercapi batch klaar: {ok} OK, {missing} missing (marked sold), "
            f"{fail} FAIL — {dt:.1f}s totaal ({dt/max(1,len(ids)):.2f}s per item) ===",
            file=sys.stderr,
        )
    elif argv[1] == "--recheck":
        limit = int(argv[2]) if len(argv) > 2 else 100
        min_age = int(argv[3]) if len(argv) > 3 else 4
        ids = _deal_recheck_ids(limit=limit, min_age_hours=min_age)
        print(f"Recheck (deal-items): {len(ids)} kandidaten (limit={limit}, min-age={min_age}u)",
              file=sys.stderr)
        if not ids:
            return
        t0 = time.time()
        ok, missing, fail = asyncio.run(_run_batch(ids))
        dt = time.time() - t0
        print(
            f"\n=== mercapi recheck klaar: {ok} nog beschikbaar, {missing} inmiddels SOLD, "
            f"{fail} FAIL — {dt:.1f}s ({dt/max(1,len(ids)):.2f}s per item) ===",
            file=sys.stderr,
        )
    else:
        iid = argv[1]
        if not iid.startswith("m"):
            print(f"ERROR: {iid} is geen Mercari-id (start niet met 'm')", file=sys.stderr)
            sys.exit(2)
        m = Mercapi()
        result = asyncio.run(_fetch_one(m, iid))
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main(sys.argv)
