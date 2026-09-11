#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Replay (alleen lezen): oude vs nieuwe lezing_opschonen op ALLE Qwen-lezingen sinds 9-9 16:44.

Input per foto = de opgeslagen per_photo[].fields (label_name/set_name zijn ruw; set_code/number
waren al opgeschoond, opschonen is daarop idempotent). Oud = lezing_opschonen uit git HEAD,
nieuw = werkmap. Verschil = puur de -P-regel. Schrijft replay_resultaat.json + stdout-rapport.
"""
import collections
import importlib.util
import json
import re
import subprocess
import sys
import types
from pathlib import Path

KENSA = Path("/home/pi/.openclaw/workspace/agents/kensa")
HIER = Path(__file__).resolve().parent
sys.path.insert(0, str(KENSA))

import lezing_opschonen as NIEUW  # noqa: E402
from supabase_client import _config, _session  # noqa: E402

bron = subprocess.run(["git", "-C", str(KENSA), "show", f"{sys.argv[1] if len(sys.argv) > 1 else 'HEAD'}:lezing_opschonen.py"],
                      capture_output=True, text=True, check=True).stdout
OUD = types.ModuleType("lezing_opschonen_oud")
OUD.__file__ = str(KENSA / "lezing_opschonen.py")
exec(compile(bron, "lezing_opschonen_oud", "exec"), OUD.__dict__)
assert not hasattr(OUD, "zonder_valse_p"), "baseline bevat de regel al"

SINDS = "2026-09-09T14:44:00+00:00"   # 16:44 NL
FAMILIE_PROMO = re.compile(r"^[A-Z]+-P$")          # SV-P, SM-P, S-P, XY-P, SWSH-P, M-P, PM-P, BW-P …
PCP_PROMO = {"S8A-P", "S8-P"}

base, hdr = _config()
sess = _session()
rijen, laatste = [], 0
while True:
    r = sess.get(f"{base}/rest/v1/analysis", headers={**hdr, "Accept-Profile": "kensa"}, timeout=(5, 60), params={
        "select": "analysis_id,item_id,created_at,result_json", "trap": "eq.slab_ocr",
        "created_at": f"gte.{SINDS}", "analysis_id": f"gt.{laatste}", "order": "analysis_id", "limit": "1000"})
    r.raise_for_status()
    blok = r.json()
    if not blok:
        break
    rijen += blok
    laatste = blok[-1]["analysis_id"]

fotos = []
for a in rijen:
    rj = a["result_json"] or {}
    for p in (rj.get("per_photo") or []) + (rj.get("_multimodal_per_photo") or []):
        if p.get("lezer") == "qwen" and isinstance(p.get("fields"), dict):
            fotos.append((a, p))

per_code, geraakt, afwijk_opgeslagen = collections.Counter(), [], 0
for a, p in fotos:
    f = p["fields"]
    lezing = {"name": f.get("card_name"), "label_name": f.get("label_name"), "set_name": f.get("set_name"),
              "number": f.get("number"), "set_code": f.get("set_code"), "grade": f.get("grade"), "cert": f.get("cert")}
    o, n = OUD.opschonen(dict(lezing)), NIEUW.opschonen(dict(lezing))
    if (o["set_code"], o["number"]) != (f.get("set_code"), f.get("number")):
        afwijk_opgeslagen += 1
    andere = {k for k in set(o) | set(n) if k not in ("set_code", "number") and o.get(k) != n.get(k)}
    assert not andere, (a["item_id"], andere)
    if (o["set_code"], o["number"]) == (n["set_code"], n["number"]):
        continue
    rec = {"analysis_id": a["analysis_id"], "item_id": a["item_id"], "created_at": a["created_at"],
           "source": (a["result_json"] or {}).get("_source"), "photo_idx": p.get("photo_idx"),
           "set_code": [o["set_code"], n["set_code"]], "number": [o["number"], n["number"]],
           "label_name": f.get("label_name"), "set_name": f.get("set_name")}
    assert o["set_code"] is None or n["set_code"] in (o["set_code"], o["set_code"][:-2]), rec
    geraakt.append(rec)
    per_code[f"{o['set_code']} -> {n['set_code']}"] += 1

promo_geraakt = [g for g in geraakt if g["set_code"][0] and (FAMILIE_PROMO.match(g["set_code"][0]) or g["set_code"][0] in PCP_PROMO)]
json.dump({"analyses": len(rijen), "qwen_fotos": len(fotos), "geraakt": geraakt}, open(HIER / "replay_resultaat.json", "w"),
          ensure_ascii=False, indent=1)

print(f"slab_ocr-analyses sinds 9-9 16:44: {len(rijen)} · Qwen-foto-lezingen: {len(fotos)}")
print(f"sanity: oude opschoning wijkt af van opgeslagen waarde bij {afwijk_opgeslagen} foto's (verwacht 0)")
print(f"geraakt (set_code en/of nummer anders): {len(geraakt)} foto's · {len({g['item_id'] for g in geraakt})} items")
for k, v in per_code.most_common():
    print(f"  {k}: {v}")
print(f"echte promo-codes geraakt (familie *-P zonder cijfer, S8A-P, S8-P): {len(promo_geraakt)}")
print("\nALLE geraakte items:")
for g in geraakt:
    print(f"  {g['item_id']:<24} {g['set_code'][0]:>7}->{g['set_code'][1]:<5} {str(g['number'][0]):>12}->{str(g['number'][1]):<9}"
          f" label={g['label_name']!r} set_name={g['set_name']!r}")
