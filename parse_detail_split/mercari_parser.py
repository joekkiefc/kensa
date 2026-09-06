"""Kensa parse_detail v2 — CoC-gesplitste Buyee Mercari HTML parser.

Semantiek IDENTIEK aan fetch_detail.py:parse_detail (regel 145, CoC 30).
LAZY-imports uit fetch_detail zodat CLI-run van fetch_detail.py niet crasht
op circular import (fetch_detail.py roept `python fetch_detail.py --all` in cron).
"""
from __future__ import annotations

import re

import storage


def _fd():
    """Lazy proxy naar fetch_detail — pas ophalen tijdens function-call."""
    import fetch_detail
    return fetch_detail


def _extract_product_meta(product: dict, item_id: str) -> dict:
    """Trek title + JPY-prijs + detail-URL uit een JSON-LD Product-object."""
    fd = _fd()
    out: dict = {}
    out["title_jp"] = product.get("name")
    offers = product.get("offers") or {}
    price = offers.get("price")
    if price is not None:
        try:
            out["price_jpy"] = int(price)
        except (TypeError, ValueError):
            pass
    out["detail_url"] = offers.get("url") or fd.DETAIL_URL_TEMPLATE.format(item_id=item_id)
    return out


def _extract_title_fallback(html: str) -> str | None:
    m = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
    return m.group(1).strip() if m else None


def _extract_price_fallback(html: str) -> int | None:
    m = re.search(r"m-goodsDetail__price[^>]*>[\s\S]*?([\d,]+)\s*YEN", html)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _extract_price_eur(html: str) -> float | None:
    m = re.search(r"m-goodsDetail__priceFX[^>]*>\(€([\d,.]+)\)", html)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _extract_category_path(breadcrumb: dict | None) -> str | None:
    if not breadcrumb:
        return None
    crumbs = breadcrumb.get("itemListElement") or []
    names = [c.get("name") for c in crumbs if c.get("name")]
    return " > ".join(names) if names else None


def _extract_photo_urls(html: str, item_id: str) -> list[str]:
    """Regex-scan naar foto's + dedup op basis-URL (query-stripped)."""
    pattern = rf"https://static\.mercdn\.net/item/detail/orig/photos/{re.escape(item_id)}_\d+\.jpg(?:\?\d+)?"
    seen: set[str] = set()
    photos: list[str] = []
    for m in re.finditer(pattern, html):
        url = m.group(0)
        base = url.split("?")[0]
        if base in seen:
            continue
        seen.add(base)
        photos.append(url)
    return photos


def _detect_authenticated(html: str) -> int | None:
    if "authenticated" in html.lower() or "本物保証" in html or "鑑定済" in html:
        return 1
    return None


def parse_detail_v2(html: str, item_id: str, detail_url=None) -> dict:
    """Zie fetch_detail.parse_detail. Orkestrator: dispatch → JSON-LD → fallbacks → fields.
    detail_url wordt bij voorkeur meegegeven omdat de source-dispatch niet
    meer betrouwbaar uit het item_id te halen is sinds Buyee's 2026-formaat-
    change (Mercari '2J...', PayPay 'c1/d1/g1/k1/q1...')."""
    fd = _fd()
    if fd._source_of(item_id, detail_url) == "paypay":
        return fd._parse_paypay_detail(html, item_id, detail_url)

    result: dict = {"item_id": item_id}
    jsonld = fd._extract_jsonld(html)
    product = fd._first_of_type(jsonld, "Product")
    breadcrumb = fd._first_of_type(jsonld, "BreadcrumbList")

    if product:
        result.update(_extract_product_meta(product, item_id))

    if not result.get("title_jp"):
        t = _extract_title_fallback(html)
        if t:
            result["title_jp"] = t

    if result.get("price_jpy") is None:
        p = _extract_price_fallback(html)
        if p is not None:
            result["price_jpy"] = p

    eur = _extract_price_eur(html)
    if eur is not None:
        result["price_eur"] = eur

    cat = _extract_category_path(breadcrumb)
    if cat is not None:
        result["category_path"] = cat

    result["photo_urls"] = _extract_photo_urls(html, item_id)

    auth = _detect_authenticated(html)
    if auth is not None:
        result["authenticated"] = auth

    result["detail_scraped_at"] = storage.now_iso()
    return result
