"""parse_card v2 — CoC 22 → sub-functies < 10."""
from __future__ import annotations
from ebay_lastsold import (
    ITEM_ID_RE,
    PRICE_RE,
    _first_text,
    parse_sold_date,
)


def _extract_ebay_id_and_href(card) -> tuple[str | None, str | None]:
    """Loop over /itm/ anchors, skip placeholder id '123456'."""
    for a in card.css('a[href*="/itm/"]'):
        h = a.attrib.get("href", "")
        m = ITEM_ID_RE.search(h)
        if m and m.group(1) != "123456":
            href = h.split("?")[0] + (("?" + h.split("?", 1)[1]) if "?" in h else "")
            return (m.group(1), href)
    return (None, None)


def _extract_price(card) -> float | None:
    price_text = _first_text(card, [".s-card__price", "span.su-styled-text"])
    if not price_text:
        return None
    pm = PRICE_RE.search(price_text)
    if not pm:
        return None
    try:
        return float(pm.group(1).replace(",", ""))
    except ValueError:
        return None


def _extract_title(card) -> str | None:
    """img.alt met filter → span-fallback."""
    for img in card.css("img"):
        alt = (img.attrib.get("alt") or "").strip()
        if alt and "eBay Store" not in alt and len(alt) > 10:
            return alt
    return _first_text(card, ["span.su-styled-text[aria-hidden]", "span.su-styled-text"])


def _extract_sold_date(card) -> str | None:
    for span in card.css('span[aria-label="Sold Item"], span.su-styled-text'):
        d = parse_sold_date((span.text or "").strip())
        if d:
            return d
    return None


def _clean_url(href: str | None) -> str | None:
    return href.split("&")[0].split("?")[0] if href else None


def parse_card_v2(card) -> dict | None:
    """Zie ebay_lastsold.parse_card."""
    item_id, href = _extract_ebay_id_and_href(card)
    if not item_id:
        return None
    return {
        "item_id": item_id,
        "title": _extract_title(card),
        "price_usd": _extract_price(card),
        "sold_date": _extract_sold_date(card),
        "url": _clean_url(href),
    }
