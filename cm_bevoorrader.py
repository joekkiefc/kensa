#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""cm_bevoorrader.py — één plek die beslist welke kaarten een Cardmarket-prijs nodig hebben.

Vroeger zat die beslissing verstopt in de scoor-flow van de cache-worker én de
eBay-worker. Nu loopt dit script elke paar minuten de recente kaarten langs
(in Supabase) en bestelt een CM-prijs voor elke kaart die:

  1. actief is (status='new') en recent gezien (laatste KIJK_DAGEN dagen)
  2. een gelezen slab heeft (grade bekend -> card_key bestaat)
  3. GEEN verse Cardmarket-prijs heeft (price_cache, CM_VERS_DAGEN)
  4. niet al in de wachtrij staat (fetched_at leeg = Windows-worker is bezig)
  5. niet net al geprobeerd is (bestel-administratie, herprobeer na HERPROBEER_UREN)

De échte beslisregels (bundel? naam+nummer? welke grade? cache-hit?) blijven
ongewijzigd in analyze_split/enqueue_cardmarket.enqueue_cardmarket_if_possible_v2.
De bevoorrader kiest alleen WIE er langs die regels gaat — één eigenaar.

Draait via cron_cm_bevoorrader.sh met:
  KENSA_READ_STORAGE=supabase  (kaart-data lezen uit Supabase = bron van waarheid)
  KENSA_WRITE_STORAGE=dual     (wachtrij naar Pi én Supabase; de Windows-worker
                                leest de Pi tot de CM-migratie als laatste stap)

Dempers tegen overbelasting:
  - nieuwste kaarten eerst, maximaal MAX_BESTELLINGEN_PER_RUN per ronde
  - hele ronde overslaan als de Pi-wachtrij > WACHTRIJ_MAX_PENDING staat
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HIER = Path(__file__).resolve().parent
sys.path.insert(0, str(HIER))

from analyze import DB_PATH, PRICE_CACHE_DAYS  # noqa: E402
from analyze_split.score_only import _load_listing, _load_stored_slab_llm  # noqa: E402
from analyze_split.enqueue_cardmarket import enqueue_cardmarket_if_possible_v2  # noqa: E402
from storage_supabase import _http  # noqa: E402

KIJK_DAGEN = 3                  # hoe ver terug we naar actieve kaarten kijken
CM_VERS_DAGEN = PRICE_CACHE_DAYS  # zelfde versheid als de rest van het systeem (3d)
MAX_BESTELLINGEN_PER_RUN = 40   # dosering: nieuwste eerst, rest volgende ronde
WACHTRIJ_MAX_PENDING = 150      # Pi-wachtrij voller dan dit? -> ronde overslaan
HERPROBEER_UREN = 24            # kaart zonder CM-match pas na 24u opnieuw proberen
ADMINISTRATIE = HIER / "cm_bevoorrader_administratie.json"


def _nu() -> datetime:
    return datetime.now(timezone.utc)


def _log(tekst: str) -> None:
    print(f"[{_nu().isoformat(timespec='seconds')}] [bevoorrader] {tekst}", flush=True)


# ---------------------------------------------------------------------------
# Stap 1 — recente actieve kaarten uit Supabase (nieuwste listing per card_key)
# ---------------------------------------------------------------------------

def haal_recente_kaarten() -> dict[str, str]:
    """card_key -> nieuwste item_id, voor actieve listings van de laatste KIJK_DAGEN."""
    cutoff = (_nu() - timedelta(days=KIJK_DAGEN)).isoformat()
    kaarten: dict[str, str] = {}
    offset = 0
    while True:
        rows = _http.get("listings", "kensa", {
            "status": "eq.new",
            "card_key": "not.is.null",
            "first_seen_at": f"gte.{cutoff}",
            "select": "item_id,card_key",
            "order": "first_seen_at.desc",
            "limit": "1000",
            "offset": str(offset),
        })
        if not rows:
            break
        for r in rows:
            kaarten.setdefault(r["card_key"], r["item_id"])  # nieuwste wint
        offset += len(rows)
        if len(rows) < 1000:
            break
    return kaarten


# ---------------------------------------------------------------------------
# Stap 2 — welke daarvan hebben al een VERSE Cardmarket-prijs?
# ---------------------------------------------------------------------------

def haal_kaarten_met_verse_cm(card_keys: list[str]) -> set[str]:
    cutoff = (_nu() - timedelta(days=CM_VERS_DAGEN)).isoformat()
    vers: set[str] = set()
    for i in range(0, len(card_keys), 100):
        stuk = card_keys[i:i + 100]
        quoted = ",".join('"' + k.replace('"', '""') + '"' for k in stuk)
        rows = _http.get("price_cache", "kensa", {
            "select": "card_key",
            "card_key": f"in.({quoted})",
            "cm_fetched_at": f"gte.{cutoff}",
            "cm_listings_json": "not.is.null",
            "limit": str(len(stuk)),
        })
        vers.update(r["card_key"] for r in rows if r.get("card_key"))
    return vers


# ---------------------------------------------------------------------------
# Stap 3 — wat staat er al in de wachtrij? (Pi = de live wachtrij tot CM-migratie)
# ---------------------------------------------------------------------------

def haal_wachtrij_stand() -> tuple[int, set[str]]:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    try:
        pending = conn.execute(
            "SELECT COUNT(*) FROM cardmarket_queue WHERE fetched_at IS NULL").fetchone()[0]
        keys = {r[0] for r in conn.execute(
            "SELECT DISTINCT card_key FROM cardmarket_queue WHERE fetched_at IS NULL AND card_key IS NOT NULL")}
        return pending, keys
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Stap 4 — bestel-administratie (niet elke ronde dezelfde kaart proberen)
# ---------------------------------------------------------------------------

def lees_administratie() -> dict[str, str]:
    try:
        return json.loads(ADMINISTRATIE.read_text())
    except Exception:
        return {}


def schrijf_administratie(admin: dict[str, str]) -> None:
    # oude regels (>7d) opruimen zodat het bestand klein blijft
    grens = (_nu() - timedelta(days=7)).isoformat()
    admin = {k: v for k, v in admin.items() if v >= grens}
    ADMINISTRATIE.write_text(json.dumps(admin, ensure_ascii=False, indent=0))


def recent_geprobeerd(admin: dict[str, str], card_key: str) -> bool:
    t = admin.get(card_key)
    if not t:
        return False
    return t >= (_nu() - timedelta(hours=HERPROBEER_UREN)).isoformat()


# ---------------------------------------------------------------------------
# Stap 5 — bestellen (via de bestaande, ongewijzigde beslisregels)
# ---------------------------------------------------------------------------

def bestel(card_key: str, item_id: str, dry_run: bool) -> None:
    listing = _load_listing(item_id)
    if not listing:
        _log(f"skip {card_key}: listing {item_id} niet gevonden")
        return
    slab, llm_data, _ = _load_stored_slab_llm(item_id)
    if not slab:
        _log(f"skip {card_key}: geen slab-data voor {item_id}")
        return
    if dry_run:
        _log(f"DRY-RUN zou bestellen: {card_key} (via {item_id})")
        return
    enqueue_cardmarket_if_possible_v2(item_id, slab, llm_data, True, listing=listing)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="alleen tonen wat besteld zou worden")
    ap.add_argument("--limit", type=int, default=MAX_BESTELLINGEN_PER_RUN)
    args = ap.parse_args()

    pending, in_wachtrij = haal_wachtrij_stand()
    if pending > WACHTRIJ_MAX_PENDING:
        _log(f"wachtrij vol ({pending} pending > {WACHTRIJ_MAX_PENDING}) — ronde overgeslagen")
        return 0

    kaarten = haal_recente_kaarten()
    vers = haal_kaarten_met_verse_cm(list(kaarten.keys()))
    admin = lees_administratie()

    kandidaten = [(ck, iid) for ck, iid in kaarten.items()
                  if ck not in vers and ck not in in_wachtrij
                  and not recent_geprobeerd(admin, ck)]
    _log(f"recent={len(kaarten)} vers-cm={len(vers)} in-wachtrij={len(in_wachtrij)} "
         f"kandidaten={len(kandidaten)} (max {args.limit} deze ronde)")

    besteld = 0
    for ck, iid in kandidaten[:args.limit]:
        try:
            bestel(ck, iid, args.dry_run)
        except Exception as e:
            _log(f"FOUT bij {ck}/{iid}: {type(e).__name__}: {e}")
        if not args.dry_run:
            admin[ck] = _nu().isoformat()
        besteld += 1
    if not args.dry_run:
        schrijf_administratie(admin)
    _log(f"klaar — {besteld} kaarten langs de beslisregels gestuurd")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
