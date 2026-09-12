#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""ONDERZOEK Gemini (11-9) — ALLEEN LEZEN uit Supabase (HTTP GET, geen POST/PATCH/DELETE).

Haalt alle Qwen-items sinds fase 4 (2026-09-09 14:44 UTC = 16:44 Amsterdam) op:
analysis-traps slab_ocr/llm_slab/summary/ebay_prices, listings, photos, cardmarket_queue.
Schrijft alleen naar dev/onderzoek_gemini/data/.
"""
import json
import sys
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parent / "data"
OUT.mkdir(exist_ok=True)
CUTOFF = "2026-09-09T14:44:00+00:00"

s = json.loads(Path("/home/pi/.openclaw/secrets.json").read_text())["supabase"]
BASE = s["url"].rstrip("/") + "/rest/v1/"
H = {"apikey": s["service_role_key"], "Authorization": f"Bearer {s['service_role_key']}",
     "Accept-Profile": "kensa"}
SES = requests.Session()
N_GET = 0


def get(table, params):
    """Alleen GET. Pagineert via offset op een vaste order."""
    global N_GET
    out, off = [], 0
    while True:
        p = dict(params, limit="1000", offset=str(off))
        r = SES.get(BASE + table, params=p, headers=H, timeout=(5, 60))
        N_GET += 1
        r.raise_for_status()
        rows = r.json()
        out.extend(rows)
        if len(rows) < 1000:
            return out
        off += 1000


def chunks(xs, n=80):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


traps = {}
for trap in ("slab_ocr", "llm_slab", "summary", "ebay_prices"):
    rows = get("analysis", {"trap": f"eq.{trap}", "created_at": f"gte.{CUTOFF}",
                            "select": "analysis_id,item_id,trap,result_json,created_at",
                            "order": "analysis_id.asc"})
    traps[trap] = {r["item_id"]: r for r in rows}
    print(trap, len(rows), "rijen", len(traps[trap]), "items", file=sys.stderr)

qwen_items = sorted(i for i, r in traps["slab_ocr"].items() if (r["result_json"] or {}).get("_source") == "qwen")
llm_items = sorted(traps["llm_slab"])
wanted = sorted(set(qwen_items) | set(llm_items))
print("qwen-items", len(qwen_items), "llm-items", len(llm_items), "samen", len(wanted), file=sys.stderr)

listings, photos, cmq = {}, {}, {}
for ch in chunks(wanted):
    inlist = "in.(" + ",".join(f'"{i}"' for i in ch) + ")"
    for r in get("listings", {"item_id": inlist, "order": "item_id.asc",
                              "select": "item_id,title_en,title_jp,price_eur,card_key,slab_status,first_seen_at,source"}):
        listings[r["item_id"]] = r
    for r in get("photos", {"item_id": inlist, "order": "photo_id.asc",
                            "select": "item_id,photo_index,url_original"}):
        photos.setdefault(r["item_id"], []).append([r["photo_index"], r["url_original"]])
    for r in get("cardmarket_queue", {"item_id": inlist, "order": "item_id.asc",
                                      "select": "item_id,url,grade,card_key,queued_at,fetched_at,error"}):
        cmq[r["item_id"]] = r

data = {"cutoff": CUTOFF, "traps": traps, "qwen_items": qwen_items, "llm_items": llm_items,
        "listings": listings, "photos": photos, "cardmarket_queue": cmq}
(OUT / "supabase_pull.json").write_text(json.dumps(data, ensure_ascii=False))
print(f"klaar: listings={len(listings)} photos-items={len(photos)} cmq={len(cmq)} GET-calls={N_GET}", file=sys.stderr)
