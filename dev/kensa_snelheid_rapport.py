#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa doorloop-/snelheidsrapport (Tommy 10-9): hoe snel gaat een kaart van
'gezien' naar 'oordeel', waar zit de vertraging, en groeit het eBay-notitieboek?

Bronnen (read-only, Supabase):
  listings.first_seen_at            → moment dat de kaart in beeld kwam
  analysis trap=slab_ocr .created_at → gelezen (Qwen)
  analysis trap=ebay_prices          → eBay-prijs (from_cache/cache_source/error)
  analysis trap=summary .created_at  → eindoordeel

Toont: per-uur tabel (doorstroom, doorlooptijd, eBay-notitieboek%), stap-breakdown
(gezien→gelezen, gelezen→prijs, prijs→oordeel), traagste kaarten, notitieboek-grootte.

Gebruik:  dev/kensa_snelheid_rapport.py [--sinds ISO] [--json]
Default sinds = vandaag 00:00 Europe/Amsterdam.
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

KENSA = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KENSA))
from storage_supabase import _http  # noqa: E402

TZ = ZoneInfo("Europe/Amsterdam")


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def med(xs):
    return round(statistics.median(xs), 1) if xs else None


def p90(xs):
    return round(sorted(xs)[int(len(xs) * 0.9) - 1], 1) if len(xs) >= 10 else None


def haal(trap: str, sinds: str, select: str) -> list[dict]:
    out, off = [], 0
    while True:
        rows = _http.get("analysis", "kensa", [
            ("select", select), ("trap", f"eq.{trap}"),
            ("created_at", f"gt.{sinds}"), ("order", "created_at.asc"),
            ("limit", "1000"), ("offset", str(off)),
        ])
        out += rows
        if len(rows) < 1000:
            return out
        off += 1000


def first_seen_map(item_ids: list[str]) -> dict[str, datetime]:
    fs = {}
    for i in range(0, len(item_ids), 100):
        stuk = ",".join(item_ids[i:i + 100])
        for r in _http.get("listings", "kensa", [("select", "item_id,first_seen_at"),
                                                 ("item_id", f"in.({stuk})"), ("limit", "100")]):
            if r.get("first_seen_at"):
                fs[r["item_id"]] = ts(r["first_seen_at"])
    return fs


def notitieboek_grootte() -> int:
    """price_cache-rijen met een eBay-antwoord van de laatste 3 dagen (Content-Range count)."""
    cut = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    r = _http.get_raw("price_cache", "kensa", [("select", "card_key"), ("ebay_fetched_at", f"gte.{cut}"),
                                               ("limit", "1")], prefer="count=exact") \
        if hasattr(_http, "get_raw") else None
    if r is not None:
        cr = r.headers.get("content-range", "")
        if "/" in cr:
            return int(cr.split("/")[-1])
    # fallback: paginate-tel
    n, off = 0, 0
    while True:
        rows = _http.get("price_cache", "kensa", [("select", "card_key"), ("ebay_fetched_at", f"gte.{cut}"),
                                                  ("limit", "1000"), ("offset", str(off))])
        n += len(rows)
        if len(rows) < 1000:
            return n
        off += 1000


def rapport(sinds: str) -> dict:
    ocr = haal("slab_ocr", sinds, "item_id,result_json,created_at")
    ebay = haal("ebay_prices", sinds, "item_id,result_json,created_at")
    summ = haal("summary", sinds, "item_id,created_at")
    ocr_t = {r["item_id"]: ts(r["created_at"]) for r in ocr}
    ebay_t = {r["item_id"]: ts(r["created_at"]) for r in ebay}
    summ_t = {r["item_id"]: ts(r["created_at"]) for r in summ}
    fs = first_seen_map(list({*ocr_t, *summ_t}))

    # eBay-bron
    bron = collections.Counter()
    for r in ebay:
        rj = r["result_json"] or {}
        if rj.get("from_cache"):
            bron[rj.get("cache_source") or "cache_zoekzin"] += 1
        elif rj.get("error"):
            bron["live_fout"] += 1
        else:
            bron["live_gelukt"] += 1
    n_ebay = len(ebay) or 1
    uit_note = sum(v for k, v in bron.items() if not k.startswith("live"))

    # per-uur (op moment van oordeel)
    per_uur = collections.defaultdict(lambda: {"gezien_oordeel": [], "ebay": 0, "cache": 0, "fout": 0})
    for r in summ:
        iid = r["item_id"]; u = ts(r["created_at"]).astimezone(TZ).strftime("%H")
        if iid in fs:
            dt = (summ_t[iid] - fs[iid]).total_seconds() / 60
            if 0 <= dt < 360:
                per_uur[u]["gezien_oordeel"].append(dt)
    for r in ebay:
        u = ts(r["created_at"]).astimezone(TZ).strftime("%H"); rj = r["result_json"] or {}
        per_uur[u]["ebay"] += 1
        if rj.get("from_cache"):
            per_uur[u]["cache"] += 1
        elif rj.get("error"):
            per_uur[u]["fout"] += 1

    # stap-breakdown + traagste
    stap = {"gezien_gelezen": [], "gelezen_prijs": [], "prijs_oordeel": [], "totaal": []}
    traag = []
    for iid, o in summ_t.items():
        if iid not in fs:
            continue
        tot = (o - fs[iid]).total_seconds() / 60
        if not (0 <= tot < 360):
            continue
        stap["totaal"].append(tot)
        if iid in ocr_t:
            stap["gezien_gelezen"].append((ocr_t[iid] - fs[iid]).total_seconds() / 60)
        if iid in ocr_t and iid in ebay_t:
            stap["gelezen_prijs"].append((ebay_t[iid] - ocr_t[iid]).total_seconds() / 60)
        if iid in ebay_t:
            stap["prijs_oordeel"].append((o - ebay_t[iid]).total_seconds() / 60)
        traag.append((round(tot, 1), iid))
    traag.sort(reverse=True)

    return {
        "sinds": sinds,
        "kaarten": {"gelezen": len(ocr), "ebay_opzoek": len(ebay), "oordeel": len(summ)},
        "doorlooptijd_min": {"mediaan": med(stap["totaal"]), "p90": p90(stap["totaal"]), "n": len(stap["totaal"])},
        "stap_mediaan_min": {k: med(v) for k, v in stap.items() if k != "totaal"},
        "ebay_notitieboek_pct": round(100 * uit_note / n_ebay, 1),
        "ebay_bron": dict(bron),
        "ebay_met_prijs_pct": round(100 * sum(1 for r in ebay if (r["result_json"] or {}).get("sales")) / n_ebay, 1),
        "notitieboek_grootte_3d": notitieboek_grootte(),
        "traagste_5": traag[:5],
        "per_uur": {u: {"oordeel": len(d["gezien_oordeel"]), "med_min": med(d["gezien_oordeel"]),
                        "p90_min": p90(d["gezien_oordeel"]), "ebay": d["ebay"],
                        "cache_pct": round(100 * d["cache"] / d["ebay"]) if d["ebay"] else None,
                        "fout_pct": round(100 * d["fout"] / d["ebay"]) if d["ebay"] else None}
                    for u, d in sorted(per_uur.items())},
    }


def tekst(rap: dict) -> str:
    L = []
    k = rap["kaarten"]; d = rap["doorlooptijd_min"]; s = rap["stap_mediaan_min"]
    L.append(f"Doorstroom: {k['oordeel']} kaarten met oordeel · {k['ebay_opzoek']} eBay-opzoekingen · {k['gelezen']} gelezen")
    L.append(f"Doorlooptijd gezien→oordeel: mediaan {d['mediaan']} min · p90 {d['p90']} min (n={d['n']})")
    L.append(f"  stappen (mediaan): gezien→gelezen {s['gezien_gelezen']} · gelezen→prijs {s['gelezen_prijs']} · prijs→oordeel {s['prijs_oordeel']}")
    L.append(f"eBay uit notitieboek: {rap['ebay_notitieboek_pct']}% · met prijs {rap['ebay_met_prijs_pct']}% · notitieboek(3d) {rap['notitieboek_grootte_3d']} kaarten")
    L.append("Per uur (uur · oordeel · mediaan min · cache% · fout%):")
    for u, d2 in rap["per_uur"].items():
        L.append(f"  {u}:00 · {d2['oordeel']:>3} · {d2['med_min']} min · cache {d2['cache_pct']}% · fout {d2['fout_pct']}%")
    L.append("Traagste 5 (min · item):")
    for m, iid in rap["traagste_5"]:
        L.append(f"  {m} min · {iid}")
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sinds", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    sinds = a.sinds or datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
    rap = rapport(sinds)
    print(json.dumps(rap, ensure_ascii=False, indent=1) if a.json else tekst(rap))
