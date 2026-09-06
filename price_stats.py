#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa — prijs-statistieken uit lijst van eBay sales.

Input: list of sales dicts sorted newest-first, each with price_usd + sold_date.

Output:
  last_sale        : {price_usd, sold_date}
  avg_last_3       : {avg_usd, from_date, to_date, n}   # to = laatste, from = 3e-laatste
  avg_last_5       : {avg_usd, from_date, to_date, n}   # to = laatste, from = 5e-laatste
"""

import json
import sys


def _avg(prices: list[float]) -> float | None:
    return round(sum(prices) / len(prices), 2) if prices else None


def _range_stats(sales: list[dict], n: int) -> dict:
    subset = sales[:n]
    if not subset:
        return {"avg_usd": None, "from_date": None, "to_date": None, "n": 0}
    dates = [s.get("sold_date") for s in subset if s.get("sold_date")]
    prices = [s["price_usd"] for s in subset if s.get("price_usd") is not None]
    return {
        "avg_usd": _avg(prices),
        "to_date": max(dates) if dates else None,
        "from_date": min(dates) if dates else None,
        "n": len(prices),
    }


def compute(sales: list[dict]) -> dict:
    if not sales:
        return {"n_sales": 0, "last_sale": None, "avg_last_3": None, "avg_last_5": None}
    return {
        "n_sales": len(sales),
        "last_sale": {"price_usd": sales[0].get("price_usd"), "sold_date": sales[0].get("sold_date")},
        "avg_last_3": _range_stats(sales, 3),
        "avg_last_5": _range_stats(sales, 5),
    }


if __name__ == "__main__":
    # Test with real Espeon data from earlier probe
    espeon_sales = [
        {"price_usd": 485.00, "sold_date": "2026-07-21", "title": "Espeon VMAX"},
        {"price_usd": 549.99, "sold_date": "2026-07-20", "title": "Espeon VMAX"},
        {"price_usd": 422.00, "sold_date": "2026-07-19", "title": "Espeon VMAX"},
        {"price_usd": 449.99, "sold_date": "2026-07-18", "title": "Espeon VMAX"},
        {"price_usd": 405.00, "sold_date": "2026-07-13", "title": "Espeon VMAX"},
    ]
    stats = compute(espeon_sales)
    print(json.dumps(stats, indent=2))
