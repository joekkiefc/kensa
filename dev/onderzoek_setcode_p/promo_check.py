#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Zij-effect-check: verliezen ECHTE promo's hun -P als 'S8a-P' uit het voorbeeld gaat (P2)?
Zelfde live codepad/hooks als experiment_prompt.py. Max 10 calls, sequentieel, backlog-check vooraf.
Input: promo_kandidaten.json [{item_id, sc, lbl, url_original}]"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HIER = Path(__file__).resolve().parent
sys.path.insert(0, str(HIER))
import experiment_prompt as E  # noqa: E402  (installeert raw/foto-hooks, voert main() niet uit)

UIT = HIER / "promo_check.jsonl"
MAX = 10
cands = json.load(open(HIER / "promo_kandidaten.json"))
b = E.backlog()
E.log(f"promo-check start: backlog={b}, qwen={E.QL.qwen_bereikbaar()}")
if b > 40 or not E.QL.qwen_bereikbaar():
    E.log("promo-check STOP (backlog of Qwen)")
    raise SystemExit(1)
calls = 0
gedaan = 0
for c in cands:
    if gedaan >= 5 or calls >= MAX:
        break
    rs = []
    for v in ("P1", "P2"):
        E.QL.PROMPT_FIXED = E.VARIANTEN[v]
        E._laatste.clear()
        try:
            lez = E.QL.lees_slab_foto(c["url_original"])
        except E.QL.QwenOnbereikbaar as e:
            E.log(f"promo-check STOP onbereikbaar: {e}")
            raise SystemExit(1)
        finally:
            E.QL.PROMPT_FIXED = E.ORIG
        if (lez or {}).get("_error", "").startswith("foto:"):
            break
        calls += 1
        raw = E.QL.parse_model_json(E._laatste.get("raw") or "") if E._laatste.get("raw") else {}
        rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "item_id": c["item_id"], "variant": v,
               "label_live": c["lbl"], "set_code_live": c["sc"], "raw_set_code": raw.get("set_code"),
               "raw_number": raw.get("number"), "raw_label_name": raw.get("label_name"),
               "opgeschoond_set_code": lez.get("set_code"), "opgeschoond_number": lez.get("number"),
               "foto_sha1": E._laatste.get("sha1"), "raw": E._laatste.get("raw")}
        rs.append(rec)
        with UIT.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        E.log(f"promo #{calls} {c['item_id']} {v} live={c['sc']} raw_set={rec['raw_set_code']!r} → {rec['opgeschoond_set_code']}")
    if len(rs) == 2:
        gedaan += 1
E.log(f"promo-check KLAAR: {calls} calls, backlog nu {E.backlog()}")
