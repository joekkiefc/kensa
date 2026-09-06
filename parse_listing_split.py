"""parse_listing v2 — CoC 22 → sub-functies < 10.

LAZY-imports uit scrape_buyee om circular imports te voorkomen wanneer
scrape_buyee als CLI-script wordt gerund (__name__ == '__main__').
"""
from __future__ import annotations
import re


def _sb():
    """Lazy proxy naar scrape_buyee — pas ophalen tijdens function-call, niet bij module-load."""
    import scrape_buyee
    return scrape_buyee


def _extract_thumbnail(img) -> str | None:
    """img.src → data-src → data-bind → None."""
    if img is None:
        return None
    sb = _sb()
    thumbnail = sb.normalize_image_url(img.attrib.get("src") or img.attrib.get("data-src"))
    if thumbnail:
        return thumbnail
    db = img.attrib.get("data-bind", "")
    m = re.search(r"imagePath:\s*'([^']+)'", db)
    return sb.normalize_image_url(m.group(1)) if m else None


def _extract_title(a, img) -> str | None:
    """img.alt → h2.name → None."""
    if img is not None:
        alt = (img.attrib.get("alt") or "").strip()
        if alt:
            return alt
    name_el = _sb().first(a, "h2.name")
    return (name_el.text or "").strip() or None if name_el else None


def _extract_price_pair(a) -> tuple:
    sb = _sb()
    price_el = sb.first(a, "p.price")
    price_jpy = sb.parse_number(price_el.text if price_el else "", sb.PRICE_JPY_RE)
    price_fx_el = sb.first(a, "p.price-fx")
    price_eur = sb.parse_number(price_fx_el.text if price_fx_el else "", sb.PRICE_EUR_RE)
    return price_jpy, price_eur


def _extract_flags(li, a) -> tuple[bool, bool]:
    """(authenticated, sold)."""
    appraisal = _sb().first(a, ".appraisal__text")
    authenticated = bool(appraisal and (appraisal.text or "").strip())
    sold = "sold" in (li.attrib.get("class", "") or "").lower()
    return authenticated, sold


def parse_listing_v2(li):
    sb = _sb()
    a = sb.first(li, "a[href*='/mercari/item/'], a[href*='/paypayfleamarket/item/']")
    if not a:
        return None

    href = a.attrib.get("href", "")
    item_id_match = sb.ITEM_ID_RE.search(href)
    item_id = item_id_match.group(1) if item_id_match else None

    img = sb.first(a, "img.thumbnail") or sb.first(a, "img")
    thumbnail = _extract_thumbnail(img)
    title_jp = _extract_title(a, img)
    price_jpy, price_eur = _extract_price_pair(a)
    authenticated, sold = _extract_flags(li, a)

    return {
        "item_id": item_id,
        "title_jp": title_jp,
        "title_en": None,
        "price_jpy": price_jpy,
        "price_eur": price_eur,
        "thumbnail": thumbnail,
        "detail_url": sb.normalize_detail_url(href),
        "authenticated": authenticated,
        "sold": sold,
    }
