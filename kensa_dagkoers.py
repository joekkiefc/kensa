#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa dagkoers — bepaalt de JPY->EUR koers uit de scrape en schrijft 'm weg.

Waarom: Buyee/PayPay rekenen een eigen koers-marge die structureel boven de
officiele (IMF/ECB) koers zit. De enige juiste bron is wat Buyee ONS rekent,
en dat staat op elke listing: price_jpy naast price_eur. koers = eur / jpy.

Werkwijze (1x per dag, cron 09:00):
  1. Pak de EERSTE PP-listing van vandaag uit Supabase (source=paypay,
     eerste first_seen_at). koers = price_eur / price_jpy.
  2. Veiligheid: wijkt die koers >5% af van gisteren -> pak de 2e listing.
  3. Wijkt de 2e OOK >5% af -> laat de koers van gisteren staan (schrijf niks
     nieuws; log de reden). Zo kan 1 rare/kapotte listing de koers niet slopen.
  4. Anders -> schrijf de nieuwe dagkoers naar kensa.fx_rate.

De web-app (hos-voorraad-v2) leest kensa.fx_rate voor de landed-cost formule.
"""

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests

SECRETS = Path("/home/pi/.openclaw/secrets.json")
LOG = Path(__file__).with_name("kensa_dagkoers.log")

TIMEOUT: tuple[float, float] = (5.0, 25.0)
AFWIJK_DREMPEL = 0.05  # >5% t.o.v. gisteren = verdacht


def _config() -> tuple[str, dict]:
    s = json.loads(SECRETS.read_text())["supabase"]
    return s["url"], {
        "apikey": s["service_role_key"],
        "Authorization": f"Bearer {s['service_role_key']}",
        "Accept-Profile": "kensa",
        "Content-Profile": "kensa",
        "Content-Type": "application/json",
    }


def _log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line)
    with LOG.open("a") as f:
        f.write(line + "\n")


def _eerste_pp_listings(base: str, headers: dict, n: int = 5) -> list[dict]:
    """De eerste n PP-listings van vandaag, oplopend op first_seen_at."""
    today = date.today().isoformat()
    url = (
        f"{base}/rest/v1/listings"
        "?select=item_id,price_jpy,price_eur,first_seen_at"
        "&source=eq.paypay"
        "&price_jpy=gt.0"
        "&price_eur=gt.0"
        f"&first_seen_at=gte.{today}T00:00:00"
        "&order=first_seen_at.asc"
        f"&limit={n}"
    )
    r = requests.get(url, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _koers_gisteren(base: str, headers: dict) -> float | None:
    """Meest recente eerder opgeslagen koers (voor de afwijk-check)."""
    url = (
        f"{base}/rest/v1/fx_rate"
        "?select=date,rate"
        "&order=date.desc"
        "&limit=1"
    )
    r = requests.get(url, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    rows = r.json()
    return float(rows[0]["rate"]) if rows else None


def _wijkt_af(koers: float, ref: float) -> bool:
    if ref <= 0:
        return False
    return abs(koers - ref) / ref > AFWIJK_DREMPEL


def _schrijf(base: str, headers: dict, row: dict) -> None:
    """Upsert 1 rij per dag (PK = date)."""
    h = dict(headers)
    h["Prefer"] = "resolution=merge-duplicates"
    url = f"{base}/rest/v1/fx_rate"
    r = requests.post(url, headers=h, data=json.dumps([row]), timeout=TIMEOUT)
    r.raise_for_status()


def main() -> int:
    base, headers = _config()
    today = date.today().isoformat()

    try:
        listings = _eerste_pp_listings(base, headers, n=5)
    except Exception as e:  # noqa: BLE001
        _log(f"FOUT bij ophalen listings: {e}")
        return 1

    if not listings:
        _log("GEEN PP-listings vandaag gevonden — koers van gisteren blijft staan.")
        return 0

    try:
        gisteren = _koers_gisteren(base, headers)
    except Exception as e:  # noqa: BLE001
        _log(f"FOUT bij ophalen koers-gisteren: {e}")
        gisteren = None

    def koers_van(l: dict) -> float:
        return float(l["price_eur"]) / float(l["price_jpy"])

    kandidaat = listings[0]
    koers = koers_van(kandidaat)

    # Veiligheid: 1e wijkt >5% af -> probeer 2e.
    if gisteren is not None and _wijkt_af(koers, gisteren):
        _log(
            f"1e listing {kandidaat['item_id']} koers {koers:.6f} wijkt "
            f">5% af van gisteren {gisteren:.6f} — probeer 2e."
        )
        if len(listings) >= 2:
            kandidaat = listings[1]
            koers = koers_van(kandidaat)
            if _wijkt_af(koers, gisteren):
                _log(
                    f"2e listing {kandidaat['item_id']} koers {koers:.6f} wijkt "
                    f"OOK >5% af — koers van gisteren ({gisteren:.6f}) blijft staan."
                )
                # Schrijf een rij voor vandaag met gisteren's koers + reden,
                # zodat de web-app altijd een verse 'date' ziet.
                try:
                    _schrijf(base, headers, {
                        "date": today,
                        "rate": gisteren,
                        "source_item_id": kandidaat["item_id"],
                        "price_jpy": kandidaat["price_jpy"],
                        "price_eur": float(kandidaat["price_eur"]),
                        "fallback_reason": (
                            f"1e+2e listing >5% afwijking; koers gisteren "
                            f"({gisteren:.6f}) overgenomen"
                        ),
                    })
                    _log(f"OK — vandaag {today} = {gisteren:.6f} (fallback gisteren).")
                except Exception as e:  # noqa: BLE001
                    _log(f"FOUT bij schrijven fallback-koers: {e}")
                    return 1
                return 0
        else:
            _log("Geen 2e listing beschikbaar — koers van gisteren blijft staan.")
            return 0

    # Normale weg: schrijf de nieuwe koers.
    try:
        _schrijf(base, headers, {
            "date": today,
            "rate": round(koers, 8),
            "source_item_id": kandidaat["item_id"],
            "price_jpy": kandidaat["price_jpy"],
            "price_eur": float(kandidaat["price_eur"]),
            "fallback_reason": None,
        })
        _log(
            f"OK — vandaag {today} = {koers:.6f} "
            f"(uit {kandidaat['item_id']}: ¥{kandidaat['price_jpy']} = "
            f"€{kandidaat['price_eur']})."
        )
    except Exception as e:  # noqa: BLE001
        _log(f"FOUT bij schrijven koers: {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
