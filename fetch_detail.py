#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa Stap 2 — Buyee detail-pagina fetch + parse + store.

Gebaseerd op probe 1 (2026-07-21 07:16) — die bewezen werkte: StealthyFetcher
met wait_for_selector op de item-header selectors. Buyee's WAF challenge wordt
door StealthyFetcher afgehandeld, zelfde als in scrape_buyee.py.

Parse-strategie: JSON-LD (schema.org/Product) is de robuuste bron voor
naam+prijs+valuta+image. BreadcrumbList levert category_path. Fallback naar
CSS-selectors (`m-goodsDetail__price*`, `<h1>`) wanneer JSON-LD ontbreekt.

Usage:
  ./fetch_detail.py <item_id>          # scrape one item, print result
  ./fetch_detail.py --all              # scrape all listings without detail_scraped_at
"""

import json
import os
import re
import sys
import time
from typing import Optional

from scrapling import StealthyFetcher

import storage
import translate as translate_mod

DETAIL_URL_TEMPLATE = "https://buyee.jp/mercari/item/{item_id}"
PAYPAY_DETAIL_URL_TEMPLATE = "https://buyee.jp/paypayfleamarket/item/{item_id}"


def _source_of(item_id: str, detail_url: Optional[str] = None) -> str:
    """Bepaal source. Gebruikt detail_url als beschikbaar (100% te vertrouwen —
    Buyee-URL bevat /mercari/ of /paypayfleamarket/); anders legacy prefix.
    Sinds Buyee's id-format-change van 2026 zijn nieuwe Mercari-ids '2J...'
    en nieuwe PayPay-ids 'c1/d1/g1/k1/q1...' — vandaar de detail_url-check
    ipv item_id-prefix (die zowel Mercari-2J als PayPay-c1 misklasseerde)."""
    if detail_url:
        if "/mercari/" in detail_url:
            return "mercari"
        if "/paypayfleamarket/" in detail_url:
            return "paypay"
    if item_id.startswith("m"):
        return "mercari"
    if item_id.startswith("z"):
        return "paypay"
    return "unknown"


def _detail_url_for(item_id: str, detail_url: Optional[str] = None) -> str:
    """Return opgeslagen detail_url als beschikbaar; anders bouw uit template.
    De opgeslagen URL is altijd betrouwbaarder dan een template omdat Buyee
    van formaat kan veranderen (mei/aug 2026 gebeurd)."""
    if detail_url:
        return detail_url
    src = _source_of(item_id)
    template = PAYPAY_DETAIL_URL_TEMPLATE if src == "paypay" else DETAIL_URL_TEMPLATE
    return template.format(item_id=item_id)


def _wait_for_content(page):
    """Same page_action as probe 1 — proved to bypass AWS WAF reliably."""
    try:
        page.wait_for_selector(
            "h1, .itemName, .g-item-photo, .item-name", timeout=60000
        )
    except Exception as e:
        print(f"  wait_for_selector: {e}", file=sys.stderr)
    return page


def fetch_detail_html(item_id: str, detail_url: Optional[str] = None) -> tuple[str, int]:
    """Returns (html, http_status). Raises on total failure.
    detail_url mag meegegeven worden om DB-lookup te sparen én om zeker de
    juiste (Buyee-native) URL te gebruiken bij nieuwe id-formaten."""
    url = _detail_url_for(item_id, detail_url)
    t0 = time.time()
    page = StealthyFetcher().fetch(
        url,
        headless=True,
        network_idle=True,
        timeout=120000,  # 2 min max — items die langer duren komen via retry-flag terug
        wait=3000,
        page_action=_wait_for_content,
    )
    html = page.html_content or ""
    if "awsWaf" in html or "AWS WAF" in html:
        raise RuntimeError(f"WAF challenge not solved for {item_id}")
    if len(html) < 5000:
        raise RuntimeError(f"HTML too small ({len(html)} bytes) for {item_id}")
    print(f"  fetched {item_id}: status={page.status} len={len(html)} time={time.time()-t0:.1f}s", file=sys.stderr)
    return html, page.status


def _extract_jsonld(html: str) -> list[dict]:
    blocks = re.findall(
        r'<script[^>]*type=[\'"]application/ld\+json[\'"][^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    out = []
    for b in blocks:
        try:
            out.append(json.loads(b.strip()))
        except json.JSONDecodeError:
            continue
    return out


def _first_of_type(jsonld_list: list[dict], typename: str) -> Optional[dict]:
    for obj in jsonld_list:
        if obj.get("@type") == typename:
            return obj
    return None


def _parse_paypay_detail(html: str, item_id: str, detail_url: Optional[str] = None) -> dict:
    """PayPay Fleamarket detail-page heeft geen JSON-LD; HTML-based parse.
    - Title in <h1>
    - Prijs in .flmIdp__itemPrice (bevat 'X,XXX YEN (€Y.YY)')
    - Photos op cdnyauction.buyee.jp
    - Description bestaat NIET (Buyee toont die niet voor PayPay).
    """
    result: dict = {"item_id": item_id,
                    "detail_url": _detail_url_for(item_id, detail_url)}

    m = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
    if m:
        result["title_jp"] = m.group(1).strip()

    m = re.search(r'class="[^"]*flmIdp__itemPrice[^"]*"[^>]*>\s*([\d,]+)\s*YEN', html)
    if m:
        try:
            result["price_jpy"] = int(m.group(1).replace(",", ""))
        except ValueError:
            pass
    m = re.search(r'class="[^"]*flmIdp__itemPrice[^"]*"[^>]*>[\s\S]*?\(€([\d,.]+)\)', html)
    if m:
        try:
            result["price_eur"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass

    # Photos — cdnyauction CDN. Alleen echte fotobestanden, dedup op base-URL.
    photo_pattern = r'https://cdnyauction\.buyee\.jp/[^"\'\s\)]+?\.jpg'
    seen = set()
    photos = []
    for match in re.finditer(photo_pattern, html):
        u = match.group(0).split("?")[0]
        if u in seen:
            continue
        seen.add(u)
        photos.append(u)
    result["photo_urls"] = photos

    result["detail_scraped_at"] = storage.now_iso()
    return result


# `parse_detail` verhuisd naar _split/ (v2). Verwijderd 2026-09-01 (regel 2 cleanup).
# Backup: _legacy_pre_refactor/... — alias-override staat lager in de file.



def scrape_detail_and_store(item_id: str, db_path=None, detail_url: Optional[str] = None) -> dict:
    """Fetch detail → parse → upsert_listing + upsert_photos + save_raw_page.
    Returns parsed dict with `_meta` diagnostics. detail_url mag meegegeven
    worden om DB-lookup te sparen en zeker de juiste URL te gebruiken
    (Buyee's nieuwe id-formaten mappen niet netjes op template-URLs).

    Description-fetch is bewust weggelaten (Tommy 2026-08-02): kostte ~8s per item
    voor <0.3% score-impact. check_desc.py handelt lege description als 'skip' af."""
    detail_html, _status = fetch_detail_html(item_id, detail_url)
    parsed = parse_detail(detail_html, item_id, detail_url)

    if parsed.get("title_jp"):
        parsed["title_en"] = translate_mod.translate(parsed["title_jp"])

    photos = parsed.pop("photo_urls", [])
    listing_row = {k: v for k, v in parsed.items() if k not in ("photo_urls",)}
    storage.upsert_listing(listing_row, db_path=db_path)
    inserted_photos = storage.upsert_photos(item_id, photos, db_path=db_path)
    detail_page_id = storage.save_raw_page(
        _detail_url_for(item_id), "detail", detail_html, db_path=db_path
    )

    parsed["_meta"] = {
        "photos_inserted": inserted_photos,
        "photos_total": len(photos),
        "raw_page_id": detail_page_id,
        "detail_bytes": len(detail_html),
    }
    return parsed


def _is_broken_pipe_error(e: BaseException) -> bool:
    """Detecteer EPIPE / broken-pipe errors uit patchright/Chromium subprocess.
    Deze komen voor als Chromium tijdens fetch gekild wordt door timeout of memory-druk."""
    if isinstance(e, BrokenPipeError):
        return True
    s = str(e).lower()
    return "epipe" in s or "broken pipe" in s or "connection closed" in s or "pipe closed" in s


def _scrape_with_retry(item_id: str, db_path=None, detail_url: Optional[str] = None) -> Optional[dict]:
    """Wrap scrape_detail_and_store met 1 retry bij EPIPE / broken-pipe.
    Return parsed dict bij succes, None bij falen (foutmelding wordt gelogd naar stderr)."""
    try:
        return scrape_detail_and_store(item_id, db_path=db_path, detail_url=detail_url)
    except Exception as e:
        if not _is_broken_pipe_error(e):
            print(f"  FAIL: {e}", file=sys.stderr)
            return None
        print(f"  EPIPE/broken-pipe voor {item_id} — retry na 5s met verse browser: {e}", file=sys.stderr)
    time.sleep(5)
    try:
        return scrape_detail_and_store(item_id, db_path=db_path, detail_url=detail_url)
    except Exception as e:
        print(f"  FAIL (na retry): {e}", file=sys.stderr)
        return None


def _pending_items(db_path=None, limit: int = None) -> list[tuple[str, Optional[str]]]:
    """Return list of (item_id, detail_url) tuples. detail_url is nodig zodat
    de scraper de juiste Buyee-URL kent — de template-based URL werkt niet
    voor de nieuwe Mercari/PayPay id-formaten (2J... en c1... e.d.).

    Mercari-items (detail_url met /mercari/) worden UITGESLOTEN: die zijn
    exclusief voor fetch_detail_mercapi.py (mercapi API, ~99% succes). Zo
    verspilt de Buyee-scraper zijn 35-slot quota niet aan m*-items die
    parallel al door mercapi worden opgepakt (race-conditie op detail_scraped_at
    NULL waarbij Buyee ze eerst kreeg — cost quota, gaf 3x fail per item)."""
    # Supabase-first switch: KENSA_READ_STORAGE=supabase leest van Supabase.
    # db_path override forceert Pi-mode (voor tests met tmp-db).
    if db_path is None and os.environ.get("KENSA_READ_STORAGE", "").lower() == "supabase":
        from storage_supabase import pick_detail_batch_supabase
        return pick_detail_batch_supabase(limit=limit)
    conn = storage._connect(db_path)
    try:
        sql = ("SELECT item_id, detail_url FROM listings "
               "WHERE detail_scraped_at IS NULL "
               "  AND (detail_url IS NULL OR detail_url NOT LIKE '%/mercari/%') "
               "ORDER BY first_seen_at DESC")
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [(r["item_id"], r["detail_url"]) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def main(argv):
    storage.init_db()

    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    if argv[1] == "--all":
        limit = int(argv[2]) if len(argv) > 2 else None
        items = _pending_items(limit=limit)
        print(f"Pending: {len(items)} items to scrape", file=sys.stderr)
        for i, (iid, durl) in enumerate(items, 1):
            print(f"\n[{i}/{len(items)}] {iid}", file=sys.stderr)
            r = _scrape_with_retry(iid, detail_url=durl)
            if r is not None:
                print(f"  OK: {r.get('title_jp','?')[:60]} — ¥{r.get('price_jpy','?')} — photos {r['_meta']['photos_total']}", file=sys.stderr)
    else:
        iid = argv[1]
        r = scrape_detail_and_store(iid)
        print(json.dumps(r, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Refactor switch (2026-09-01, regel 2 #3): route `parse_detail` naar de v2 in
# parse_detail_split/mercari_parser.py. De originele def hierboven (regel 145,
# CoC 30) blijft staan voor snelle rollback (verwijder de regel eronder).
# Externe importers (fetch_detail_proxy.py) blijven ongewijzigd — hun
# `from fetch_detail import parse_detail` krijgt automatisch de v2.
# Backup: _legacy_pre_refactor/fetch_detail.py.20260901-regel2-func3
# ---------------------------------------------------------------------------
from parse_detail_split.mercari_parser import parse_detail_v2 as _parse_detail_new  # noqa: E402
parse_detail = _parse_detail_new


if __name__ == "__main__":
    main(sys.argv)
