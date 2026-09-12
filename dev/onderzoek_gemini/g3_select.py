#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""G3 steekproef-kandidaten + foto-download (alleen HTTP GET naar de foto-CDN).
Pool: OCR-pad, echte Gemini-call, BEWEZEN zonder foto (geen sqlite-foto én velden-hash in llm_slab_cache),
slab pass, Supabase-foto aanwezig. Schrijft data/g3_kandidaten.json + fotos/<item>.jpg
"""
import json
import random
import re
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
from qwen_lezer import verbeter_foto_url  # noqa: E402  (alleen voor een scherpere kijk-foto)

D = json.loads((HERE / "data" / "supabase_pull.json").read_text())
R = json.loads((HERE / "data" / "g2_items.json").read_text())
T = D["traps"]
FOTO = HERE / "fotos"
FOTO.mkdir(exist_ok=True)

pool = [r for r in R if r["pad"] == "ocr" and (r["meta_model"] or "").startswith("gemini")
        and not r["sqlite_heeft_foto"] and r["hash_velden_in_llm_cache"] and r["slab_status"] == "pass"
        and D["photos"].get(r["item_id"])]
print("pool", len(pool))


def src_url(iid):
    slab = T["slab_ocr"][iid]["result_json"]
    ph = D["photos"][iid]
    idx = slab.get("source_photo_idx")
    for i, u in ph:
        if i == idx or (idx is None and "/orig/" in u):
            return u
    return None


valsP = [r for r in pool if r["q_set"].upper().endswith("-P") and r["label_name"] and "-P" not in r["label_name"].upper()
         and re.search(r"\b(SV|S|SM)\d+[A-Za-z]?\s+JP\b", r["label_name"], re.I)]
nr_diff = [r for r in pool if r["q_nr"] and r["g_nr"] and r["q_nr"] != r["g_nr"]]
from analyze_split.ebay_phase import _pokemon_en_lookup  # noqa: E402
pdx = _pokemon_en_lookup()
naam = [r for r in pool if r["g_pokemon"] and r["q_pokemon"] != r["g_pokemon"]]
grade = [r for r in pool if r["q_grade"] != r["g_grade"]]
print("valsP", len(valsP), "nr_diff", len(nr_diff), "naam", len(naam), "grade", len(grade))

random.seed(20260911)
kand = []
seen = set()


def add(rs, cat, n):
    rs = sorted(rs, key=lambda r: T["llm_slab"][r["item_id"]]["created_at"], reverse=True)
    k = 0
    for r in rs:
        if k >= n:
            break
        if r["item_id"] in seen:
            continue
        seen.add(r["item_id"]); k += 1
        kand.append({"item_id": r["item_id"], "categorie": cat})


add(valsP, "qwen_verdacht_valsP", 8)
add(nr_diff, "qwen_verdacht_nummer", 6)
add(grade, "qwen_verdacht_grade", 3)
add(naam, "qwen_verdacht_naam", 5)
rest = [r for r in pool if r["item_id"] not in seen]
for r in random.sample(rest, 25):
    seen.add(r["item_id"])
    kand.append({"item_id": r["item_id"], "categorie": "willekeurig"})

ses = requests.Session()
for k in kand:
    u = src_url(k["item_id"])
    k["source_url"] = u
    kijk = verbeter_foto_url(u) if u else None
    k["kijk_url"] = kijk
    p = FOTO / f"{k['item_id']}.jpg"
    if kijk and not p.exists():
        try:
            r = ses.get(kijk, timeout=20)
            if r.status_code == 200:
                p.write_bytes(r.content)
            else:
                r = ses.get(u, timeout=20)
                if r.status_code == 200:
                    p.write_bytes(r.content)
        except Exception as e:
            k["download_err"] = str(e)
    k["foto"] = str(p) if p.exists() else None
(HERE / "data" / "g3_kandidaten.json").write_text(json.dumps(kand, ensure_ascii=False, indent=1))
print(len(kand), "kandidaten;", sum(1 for k in kand if k["foto"]), "foto's")
