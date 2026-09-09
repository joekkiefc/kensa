#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Tommy 9-9 20:00: "test met de data van vanavond of het daadwerkelijk zo is".

Bewering die getoetst wordt: sinds de flip (16:44:54) raakt de eBay-cache minder
omdat Qwen de kaart-identiteit anders spelt dan Gemini ('glaceon:206:10:sv8a-p' vs
'glaceon:206/167:10:sv8a'). Een kaart die vanavond LIVE naar eBay ging, had dus vaak
wél een vers antwoord in het notitieboek, maar onder de oude spelling.

Per kaart sinds 16:45 (en ter vergelijking 12:45-16:45 op Gemini):
  - eBay-uitkomst: uit cache / live gelukt / live eBay-fout
  - lag er op dat moment een VERS (≤3 dagen) eBay-antwoord in price_cache voor
    DEZELFDE kaart onder een ANDERE sleutel?  Zelfde kaart =
      (a) zelfde Cardmarket-URL (zeker), of
      (b) zelfde kern pokemon:nummer-vóór-de-slash:grade (waarschijnlijk)
Read-only.
"""
from __future__ import annotations

import collections
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

KENSA = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KENSA))
from storage_supabase import _http  # noqa: E402

FLIP = "2026-09-09T14:44:54+00:00"
VOOR = ("2026-09-09T10:44:54+00:00", FLIP)
NA = (FLIP, "2026-09-09T18:05:00+00:00")
CACHE_DAGEN = 3


def alles(tabel, params, select):
    out, off = [], 0
    while True:
        rows = _http.get(tabel, "kensa", params + [("select", select), ("limit", "1000"), ("offset", str(off))])
        out += rows
        if len(rows) < 1000:
            return out
        off += 1000


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def kern(card_key: str | None) -> str | None:
    if not card_key:
        return None
    d = card_key.split(":")
    if len(d) < 3:
        return None
    nr = d[1].split("/")[0].lstrip("#").lstrip("0") or "0"
    return f"{d[0]}:{nr}:{d[2]}"


def main() -> int:
    # 1) notitieboek: alle rijen met een eBay-antwoord van de laatste 4 dagen
    pc = alles("price_cache", [("ebay_fetched_at", "gte.2026-09-05T00:00:00+00:00")],
               "card_key,ebay_fetched_at,cm_url,ebay_query")
    per_url = collections.defaultdict(list)
    per_kern = collections.defaultdict(list)
    for r in pc:
        r["_t"] = ts(r["ebay_fetched_at"])
        if r.get("cm_url"):
            per_url[r["cm_url"]].append(r)
        k = kern(r["card_key"])
        if k:
            per_kern[k].append(r)
    print(f"notitieboek (price_cache met eBay-antwoord sinds 5-9): {len(pc)} rijen")

    for label, (sinds, tot) in (("VOOR flip (Gemini, 12:45-16:45)", VOOR), ("NA flip (Qwen, 16:45-20:05)", NA)):
        eb = alles("analysis", [("trap", "eq.ebay_prices"), ("created_at", f"gt.{sinds}"), ("created_at", f"lt.{tot}")],
                   "item_id,result_json,created_at")
        items = [r["item_id"] for r in eb]
        keys, urls = {}, {}
        for i in range(0, len(items), 100):
            stuk = ",".join(items[i:i + 100])
            for r in _http.get("listings", "kensa", [("select", "item_id,card_key"), ("item_id", f"in.({stuk})"), ("limit", "100")]):
                keys[r["item_id"]] = r.get("card_key")
            for r in _http.get("cardmarket_queue", "kensa", [("select", "item_id,url"), ("item_id", f"in.({stuk})"), ("limit", "100")]):
                urls[r["item_id"]] = r.get("url")
        tel = collections.Counter()
        gemist_fout = []
        for r in eb:
            rj = r["result_json"] or {}
            t = ts(r["created_at"])
            eigen = keys.get(r["item_id"])
            if rj.get("from_cache"):
                tel["uit cache"] += 1
                continue
            live_ok = bool(rj.get("sales")) or (not rj.get("error"))
            uitkomst = "live eBay-fout" if rj.get("error") else "live gelukt"
            tel[uitkomst] += 1
            # lag er een vers antwoord onder een ANDERE sleutel?
            vers_url = [c for c in per_url.get(urls.get(r["item_id"]) or "", [])
                        if c["card_key"] != eigen and t - timedelta(days=CACHE_DAGEN) <= c["_t"] < t]
            vers_kern = [c for c in per_kern.get(kern(eigen) or "", [])
                         if c["card_key"] != eigen and t - timedelta(days=CACHE_DAGEN) <= c["_t"] < t]
            if vers_url:
                tel[f"{uitkomst} → had vers antwoord (zelfde CM-URL, andere spelling)"] += 1
            elif vers_kern:
                tel[f"{uitkomst} → had vers antwoord (zelfde kern, andere spelling)"] += 1
            else:
                tel[f"{uitkomst} → écht nieuw (geen vers antwoord onder andere spelling)"] += 1
            if rj.get("error") and (vers_url or vers_kern):
                oud = (vers_url or vers_kern)[0]
                gemist_fout.append((r["item_id"], eigen, oud["card_key"], oud["ebay_fetched_at"][:16]))
        n = len(eb)
        print(f"\n== {label}: {n} eBay-opzoekingen ==")
        for k, v in tel.most_common():
            print(f"  {v:4d} ({100 * v / n:4.0f}%)  {k}")
        if gemist_fout:
            print(f"  → kaarten die een eBay-FOUT kregen terwijl er een vers antwoord lag (eerste 8 van {len(gemist_fout)}):")
            for iid, eigen, oud, wanneer in gemist_fout[:8]:
                print(f"      {iid}: nu '{eigen}'  ←  notitieboek '{oud}' ({wanneer})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
