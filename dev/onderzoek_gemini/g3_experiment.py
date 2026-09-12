#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""G3 — "Gemini mét foto": EXACT de live-functie analyze._llm_enrich_slab (ongewijzigd), met als enige
verschil dat zijn foto-opzoeking (sqlite `photos`, analyze.py:208-213) een tijdelijke dev-database leest
die gevuld is met de Supabase kensa.photos-rijen i.p.v. de bevroren Pi-mirror.

- Zelfde prompt/model/temperature: interpret_slab_split/gemini_interpret.py (niet aangeraakt).
- Live llm_slab_cache: NIET gelezen voor het antwoord (anders krijg je het oude no-foto-antwoord terug
  als Vision faalt) en NIET beschreven (_slab_cache_set = no-op die alleen registreert).
  Wel wordt read-only genoteerd of de live cache een hit zou hebben gegeven.
- Vision (vision.ocr_image) en de Gemini-invoer worden ruw vastgelegd.
- Sequentieel, max MAX_CALLS Gemini-calls. Schrijft alleen data/g3_*.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import veilig  # noqa: E402
sys.path.insert(0, str(HERE.parents[1]))

import json  # noqa: E402
import sqlite3  # noqa: E402
import time  # noqa: E402

import analyze  # noqa: E402
import vision  # noqa: E402
from analyze_split import ebay_phase  # noqa: E402
from analyze_split.ebay_query_split import _ebay_build_query_v2  # noqa: E402
from analyze_split.enqueue_cardmarket import _enq_extract_identity  # noqa: E402
from interpret_slab_split import gemini_interpret  # noqa: E402
import llm_client  # noqa: E402
import supabase_client  # noqa: E402

veilig.sluit_writers()
MAX_CALLS = 50
veilig.ALLOW_URLLIB_HOSTS.update({"generativelanguage.googleapis.com", "vision.googleapis.com"})

D = json.loads((HERE / "data" / "supabase_pull.json").read_text())
T = D["traps"]
K = json.loads((HERE / "data" / "g3_kandidaten.json").read_text())

# --- tijdelijke foto-database (dev), zelfde kolommen als analyze.py:211 leest ---
PDB = HERE / "data" / "g3_photos_supabase.db"
if PDB.exists():
    PDB.unlink()
c = sqlite3.connect(str(PDB))
c.execute("CREATE TABLE photos (item_id TEXT, photo_index INTEGER, url_original TEXT)")
for k in K:
    for idx, url in D["photos"][k["item_id"]]:
        c.execute("INSERT INTO photos VALUES (?,?,?)", (k["item_id"], idx, url))
c.commit(); c.close()
analyze.DB_PATH = PDB   # ENIGE wijziging t.o.v. live: waar _llm_enrich_slab de foto-URL zoekt

# --- registratie ---
REC = {}
_orig_ocr = vision.ocr_image
_orig_interp = analyze.llm_interpret_slab
_orig_cache_get = llm_client._slab_cache_get   # leest live cache read-only (veilig.py forceert mode=ro)
CALLS = {"gemini": 0, "vision": 0, "cache_set_genegeerd": 0}


def ocr_rec(url, *a, **kw):
    CALLS["vision"] += 1
    r = _orig_ocr(url, *a, **kw)
    REC["vision"] = {"url": url, "chars": r.get("chars"), "error": r.get("error"), "full_text": r.get("full_text")}
    return r


def interp_rec(ocr_text, title_en, title_jp, verbose=False, **kw):
    h = llm_client._hash_ocr(ocr_text, title_en, title_jp)
    REC["gemini_input"] = {"ocr_text": ocr_text, "title_en": title_en, "title_jp": title_jp,
                           "live_cache_zou_hitten": _orig_cache_get(h) is not None}
    CALLS["gemini"] += 1
    return _orig_interp(ocr_text, title_en, title_jp, verbose=verbose, **kw)


def cache_set_noop(h, result, model=None):
    CALLS["cache_set_genegeerd"] += 1


vision.ocr_image = ocr_rec
analyze.llm_interpret_slab = interp_rec
gemini_interpret._slab_cache_get = lambda h: None
gemini_interpret._slab_cache_set = cache_set_noop


def uitkomsten(slab, listing, llm):
    pokemon, number = _enq_extract_identity(slab, llm)
    ld = llm or {}
    inp = (pokemon, number, ld.get("set_name") or slab.get("set_name"), ld.get("set_code") or slab.get("set_code"), slab.get("label_name"))
    url = None
    if pokemon and number:
        hit = supabase_client.lookup(pokemon, str(number), set_hint=inp[2], set_code=inp[3], soft_hint=inp[4])
        url = (hit or {}).get("url")
    q = _ebay_build_query_v2(slab, listing, llm, False)
    return {"card_key": ebay_phase._build_card_key(slab, llm), "ebay_query": q[0], "ebay_skip": (q[2] or {}).get("skipped"),
            "cm_invoer": inp, "cm_url": url}


out_path = HERE / "data" / "g3_resultaten.jsonl"
done = set()
if out_path.exists():
    done = {json.loads(l)["item_id"] for l in out_path.read_text().splitlines() if l.strip()}
with out_path.open("a") as fh:
    for k in K:
        iid = k["item_id"]
        if iid in done:
            continue
        if CALLS["gemini"] >= MAX_CALLS:
            print("MAX_CALLS bereikt"); break
        slab = T["slab_ocr"][iid]["result_json"]
        listing = D["listings"][iid]
        stored = T["llm_slab"][iid]["result_json"]
        REC.clear()
        t0 = time.time()
        try:
            new = analyze._llm_enrich_slab(iid, slab, listing, verbose=True)
            err = None
        except Exception as e:
            new, err = None, f"{type(e).__name__}: {e}"
        row = {"item_id": iid, "categorie": k["categorie"], "foto": k.get("foto"), "took_s": round(time.time() - t0, 2),
               "error": err, "vision": REC.get("vision"), "gemini_input": REC.get("gemini_input"),
               "qwen": {x: slab.get(x) for x in ("card_name", "number", "set_code", "set_name", "grade", "cert", "label_name", "source_photo_idx")},
               "gemini_zonder_foto": stored, "gemini_met_foto": new,
               "uit_live_opgeslagen": uitkomsten(slab, listing, stored),
               "uit_met_foto": uitkomsten(slab, listing, new) if new else None,
               "uit_zonder_gemini": uitkomsten(slab, listing, None)}
        fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n"); fh.flush()
        v = REC.get("vision") or {}
        print(f"{iid} vision_chars={v.get('chars')} err={v.get('error')} → {(new or {}).get('name')} {(new or {}).get('number')} {(new or {}).get('set_code')}")
        time.sleep(1.0)
print("CALLS", CALLS)
print("SCHRIJF-SLOT", {k: len(v) for k, v in veilig.BLOCKED.items()})
