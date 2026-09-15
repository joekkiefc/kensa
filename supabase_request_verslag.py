#!/usr/bin/env python3
"""supabase_request_verslag.py — elke 12 uur: hoeveel Supabase-requests maakt Kensa nu,
vergeleken met de nulmeting van vóór de fixes (14-9 10:00 → 15-9 10:00 NL).

Meetlat = Supabase edge-logs (dezelfde bron als de nulmeting), afzender python-requests
(= de Pi/Kensa). Cijfers worden per 12 uur vergeleken, en per verwerkte kaart
(aantal 'summary'-uitslagen in het venster), zodat drukke en rustige uren eerlijk
vergelijkbaar zijn.

Gebruik:  supabase_request_verslag.py [--uren 12] [--dry-run]
Cron:     0 8,20 * * *  → Discord #algemeen
Log:      supabase_request_verslag.log
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HIER = Path(__file__).resolve().parent
LOG = HIER / "supabase_request_verslag.log"
PROJECT_REF = "buapbvrzrzkholjkzizo"
DISCORD_ALGEMEEN = "1379489498835976424"

# Nulmeting 2026-09-14T08:00Z → 2026-09-15T08:00Z (24 uur), afzender python-requests.
BASIS_UREN = 24
BASIS_TOTAAL_PI = 184_868
BASIS_KAARTEN = 2_559          # summary-uitslagen op 14-9
BASIS_PER_PATROON = {          # (method, path) → requests in 24u
    ("GET", "/rest/v1/listings"): 37_401,
    ("GET", "/rest/v1/price_cache"): 26_670,
    ("GET", "/rest/v1/analysis"): 24_248,
    ("PATCH", "/rest/v1/listings"): 20_496,
    ("GET", "/rest/v1/photos"): 20_114,
    ("DELETE", "/rest/v1/analysis"): 16_435,
    ("POST", "/rest/v1/analysis"): 16_431,
    ("POST", "/rest/v1/photos"): 4_998,
    ("GET", "/rest/v1/cert_sightings"): 3_879,
    ("GET", "/rest/v1/cards"): 3_678,
    ("GET", "/rest/v1/cardmarket_queue"): 3_260,
    ("POST", "/rest/v1/listings"): 2_624,
    ("POST", "/rest/v1/cert_sightings"): 1_933,
    ("POST", "/rest/v1/price_cache"): 1_212,
}
UITLEG = {
    ("DELETE", "/rest/v1/analysis"): "uitslag eerst weggooien (stap 6)",
    ("POST", "/rest/v1/analysis"): "uitslag schrijven (stap 6: 5 → 1 per kaart)",
    ("GET", "/rest/v1/listings"): "kaart opvragen (stap 2, 7)",
    ("GET", "/rest/v1/photos"): "foto's opvragen (stap 8)",
    ("GET", "/rest/v1/price_cache"): "prijs-cache polls (stap 4)",
    ("GET", "/rest/v1/analysis"): "uitslagen lezen (stap 3, 6)",
    ("GET", "/rest/v1/cert_sightings"): "cert-check (stap 5)",
    ("POST", "/rest/v1/photos"): "foto's schrijven (stap 8 → rpc)",
    ("POST", "/rest/v1/listings"): "kaart schrijven (stap 7: nu ook updates)",
}


def _token() -> str:
    cfg = json.loads(Path("/home/pi/.openclaw/openclaw.json").read_text())
    env = cfg["mcp"]["servers"]["supabase"].get("env", {})
    for v in env.values():
        if str(v).startswith("sbp_"):
            return v
    raise SystemExit("geen Supabase management-token gevonden in openclaw.json")


def logs_query(sql: str, start: datetime, end: datetime) -> list[dict]:
    q = urllib.parse.urlencode({
        "sql": sql,
        "iso_timestamp_start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "iso_timestamp_end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    req = urllib.request.Request(
        f"https://api.supabase.com/v1/projects/{PROJECT_REF}/analytics/endpoints/logs.all?{q}",
        headers={"Authorization": f"Bearer {_token()}"})
    laatste = None
    for _ in range(3):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=120).read())["result"]
        except Exception as e:  # backend hikje → nog eens
            laatste = e
    raise RuntimeError(f"logs-API faalt: {laatste!r}")


_PI = ("cross join unnest(metadata) m cross join unnest(m.request) r "
       "cross join unnest(r.headers) h where h.user_agent like 'python-requests%'")


def meet(start: datetime, end: datetime) -> dict:
    totaal_alles = logs_query("select count(*) as n from edge_logs", start, end)[0]["n"]
    totaal_pi = logs_query(f"select count(*) as n from edge_logs {_PI}", start, end)[0]["n"]
    rijen = logs_query(
        f"select r.method as m, r.path as p, count(*) as n from edge_logs {_PI} "
        "group by m, p order by n desc limit 40", start, end)
    per_patroon = {(r["m"], r["p"]): r["n"] for r in rijen}
    return {"totaal_alles": totaal_alles, "totaal_pi": totaal_pi, "per_patroon": per_patroon}


def kaarten_verwerkt(start: datetime, end: datetime) -> int:
    s = json.loads(Path("/home/pi/.openclaw/secrets.json").read_text())["supabase"]
    q = urllib.parse.urlencode({
        "select": "item_id", "trap": "eq.summary",
        "created_at": f"gte.{start.isoformat()}", "and": f"(created_at.lt.{end.isoformat()})",
    })
    req = urllib.request.Request(
        f"{s['url']}/rest/v1/analysis?{q}", method="HEAD",
        headers={"apikey": s["service_role_key"], "Authorization": f"Bearer {s['service_role_key']}",
                 "Accept-Profile": "kensa", "Prefer": "count=exact"})
    with urllib.request.urlopen(req, timeout=60) as r:
        cr = r.headers.get("Content-Range", "*/0")
    return int(cr.split("/")[-1])


def g(n: float) -> str:
    """Getal met NL duizendtal-punt (alleen op getallen, niet op tekst)."""
    return f"{n:,.0f}".replace(",", ".")


def pct(oud: float, nieuw: float) -> str:
    if oud <= 0:
        return "n.v.t."
    d = (nieuw - oud) / oud * 100
    return f"{d:+.0f}%"


def verslag(uren: int) -> str:
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(hours=uren)
    nu = meet(start, end)
    kaarten = kaarten_verwerkt(start, end)
    schaal = uren / BASIS_UREN
    basis_pi = BASIS_TOTAAL_PI * schaal
    basis_kaarten = BASIS_KAARTEN * schaal
    per_kaart_oud = BASIS_TOTAAL_PI / BASIS_KAARTEN
    per_kaart_nu = (nu["totaal_pi"] / kaarten) if kaarten else 0

    nl = lambda d: (d + timedelta(hours=2)).strftime("%d-%m %H:%M")  # zomertijd NL
    regels = [
        f"**Supabase-requests Kensa — {uren}u-verslag** ({nl(start)} → {nl(end)} NL)",
        f"• Totaal Pi-requests: **{g(nu['totaal_pi'])}** (vóór de fix: {g(basis_pi)} per {uren}u) → **{pct(basis_pi, nu['totaal_pi'])}**",
        f"• Kaarten verwerkt: {g(kaarten)} (vóór: {g(basis_kaarten)} per {uren}u)",
        f"• Requests per kaart: **{per_kaart_nu:.0f}** (vóór: {per_kaart_oud:.0f}) → **{pct(per_kaart_oud, per_kaart_nu)}**",
        f"• Alle afzenders samen (incl. HoS-app): {g(nu['totaal_alles'])}",
        "",
        "Per patroon (vóór per {0}u → nu):".format(uren),
    ]
    for (m, p), basis in sorted(BASIS_PER_PATROON.items(), key=lambda kv: -kv[1]):
        b = basis * schaal
        n = nu["per_patroon"].get((m, p), 0)
        naam = p.replace("/rest/v1/", "")
        toel = UITLEG.get((m, p), "")
        regels.append(f"• {m} {naam}: {g(b)} → {g(n)} ({pct(b, n)}){' — ' + toel if toel else ''}")
    rpc = [(p.replace("/rest/v1/rpc/", ""), n) for (m, p), n in nu["per_patroon"].items() if "/rpc/" in p]
    if rpc:
        regels.append("• Nieuw (database-functies, 1 request i.p.v. meerdere): " +
                      ", ".join(f"{naam} {g(n)}" for naam, n in sorted(rpc, key=lambda t: -t[1])))
    return "\n".join(regels)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--uren", type=int, default=12)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    tekst = verslag(a.uren)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"\n=== {datetime.now(timezone.utc).isoformat(timespec='seconds')} ===\n{tekst}\n")
    if a.dry_run:
        print(tekst)
        return 0
    r = subprocess.run(["openclaw", "message", "send", "--channel", "discord",
                        "--target", DISCORD_ALGEMEEN, "-m", tekst],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        print(f"discord-send faalt: {r.stderr[:300]}", file=sys.stderr)
        print(tekst)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
