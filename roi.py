#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa — ROI-berekening.

Buyee aankoop → landed cost NL:
  landed_cost_eur = price_eur * (1 + PCT_FEE) + FIXED_FEE
  waarbij PCT_FEE = 0.027 (2.7% import/verzend)
  en     FIXED_FEE = 2.55 + 2.00 = 4.55 EUR

Verwachte verkoop (uit eBay last-sold):
  verkoop_eur = ebay_avg_usd * fx(USD→EUR)

Marge:
  winst_eur = verkoop_eur - landed_cost_eur
  winst_pct = winst_eur / landed_cost_eur * 100
"""

import json
import time
import urllib.request
from pathlib import Path

FIXED_FEE_EUR = 4.55       # 2.55 + 2.00 (import + verzendkosten vast)
PCT_FEE = 0.027            # 2.7% variabel

SCRIPT_DIR = Path(__file__).resolve().parent
FX_CACHE = SCRIPT_DIR / ".fx_cache.json"
FX_TTL_S = 12 * 3600       # 12u cache
FX_FALLBACK = 0.92         # als API onbereikbaar


def _fx_usd_eur() -> float:
    now = time.time()
    cached: dict = {}
    if FX_CACHE.exists():
        try:
            cached = json.loads(FX_CACHE.read_text())
            if now - cached.get("ts", 0) < FX_TTL_S and cached.get("rate"):
                return float(cached["rate"])
        except Exception:
            cached = {}
    try:
        req = urllib.request.Request(
            "https://api.frankfurter.app/latest?from=USD&to=EUR",
            headers={"User-Agent": "kensa/1.0"},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode())
        rate = float(data["rates"]["EUR"])
        FX_CACHE.write_text(json.dumps({"ts": now, "rate": rate}))
        return rate
    except Exception:
        # API onbereikbaar → gebruik laatst opgehaalde cache-waarde (ook al is TTL verlopen).
        # Alleen als er nooit een succesvolle fetch is geweest, val terug op FX_FALLBACK.
        if cached.get("rate"):
            return float(cached["rate"])
        return FX_FALLBACK


def landed_cost_eur(buyee_price_eur: float) -> float:
    return round(buyee_price_eur * (1 + PCT_FEE) + FIXED_FEE_EUR, 2)


def _scenario(label: str, ebay_usd: float | None, buyee_eur: float, fx: float) -> dict | None:
    if ebay_usd is None or not buyee_eur:
        return None
    verkoop_eur = round(ebay_usd * fx, 2)
    landed = landed_cost_eur(buyee_eur)
    winst = round(verkoop_eur - landed, 2)
    pct = round(winst / landed * 100, 1) if landed else None
    return {
        "label": label,
        "ebay_usd": round(ebay_usd, 2),
        "verkoop_eur": verkoop_eur,
        "winst_eur": winst,
        "winst_pct": pct,
    }


def compute(buyee_price_eur: float | None, ebay_stats: dict | None) -> dict:
    if not buyee_price_eur or not ebay_stats:
        return {}
    fx = _fx_usd_eur()
    landed = landed_cost_eur(buyee_price_eur)
    last = (ebay_stats.get("last_sale") or {}).get("price_usd")
    a3 = (ebay_stats.get("avg_last_3") or {}).get("avg_usd")
    a5 = (ebay_stats.get("avg_last_5") or {}).get("avg_usd")
    return {
        "buyee_eur": round(buyee_price_eur, 2),
        "fixed_fee_eur": FIXED_FEE_EUR,
        "pct_fee": PCT_FEE,
        "landed_cost_eur": landed,
        "fx_usd_eur": round(fx, 4),
        "scenarios": {
            "last":  _scenario("Laatste sale",   last, buyee_price_eur, fx),
            "avg3":  _scenario("Gem. laatste 3", a3,   buyee_price_eur, fx),
            "avg5":  _scenario("Gem. laatste 5", a5,   buyee_price_eur, fx),
        },
    }


if __name__ == "__main__":
    demo_stats = {
        "last_sale":   {"price_usd": 485.00, "sold_date": "2026-07-21"},
        "avg_last_3":  {"avg_usd": 485.66,   "n": 3},
        "avg_last_5":  {"avg_usd": 462.40,   "n": 5},
    }
    print(json.dumps(compute(85.00, demo_stats), indent=2))
