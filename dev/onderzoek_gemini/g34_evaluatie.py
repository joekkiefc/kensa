#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""G3 + G4 — beoordeling tegen de handmatig vastgestelde waarheid (data/g3_waarheid.json).
Varianten per item (alle drie berekend door g3_experiment.py met de live pure functies):
  live  = opgeslagen Gemini-antwoord (zonder foto)  → wat live gebeurde
  foto  = Gemini mét foto (Vision-tekst van Supabase-foto)
  geen  = zonder Gemini (llm_data=None) → alleen Qwen
Score per onderdeel: goed / ontbreekt / fout. CM-waarheid: lookup met de ware waarden (alleen GET).
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import veilig  # noqa: E402
sys.path.insert(0, str(HERE.parents[1]))

import json  # noqa: E402
import re  # noqa: E402
from collections import Counter, defaultdict  # noqa: E402

import supabase_client  # noqa: E402

veilig.sluit_writers()
W = json.loads((HERE / "data" / "g3_waarheid.json").read_text())
RES = [json.loads(l) for l in (HERE / "data" / "g3_resultaten.jsonl").read_text().splitlines() if l.strip()]

RAR = {"SAR", "SR", "UR", "AR", "MA", "MUR", "SSR", "BWR", "CHR", "HR", "RR", "RRR", "SIR", "IR", "PR", "ACE"}
SET_RE = re.compile(r"^(SV|SM|SMP|S|M|XY|BW|MC|SWSH)\d{0,2}[A-Z]?(-[PG])?$")


def n_set(s):
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def n_nr(s):
    m = re.match(r"#?0*(\d+)", str(s or "").strip())
    return m.group(1) if m else ""


def score_key(ck, w):
    if not ck:
        return {"naam": "ontbreekt", "nummer": "ontbreekt", "set": "ontbreekt", "grade": "ontbreekt", "sleutel": "geen"}
    p = ck.split(":")
    naam, nr, gr = p[0], p[1], p[2]
    st = p[3] if len(p) > 3 else ""
    out = {"sleutel": ck}
    out["naam"] = "goed" if any(t in naam for t in w["tokens"]) else "fout"
    out["nummer"] = "goed" if (w["nr"] and n_nr(nr.split("/")[0]) == w["nr"]) else ("n.v.t." if not w["nr"] else "fout")
    if w["set"] is None:
        out["set"] = "n.v.t."
    else:
        out["set"] = "ontbreekt" if not st else ("goed" if n_set(st) == n_set(w["set"]) else "fout")
    out["grade"] = "n.v.t." if w["grade"] is None else ("goed" if gr == w["grade"] else "fout")
    return out


def score_query(q, w):
    if not q:
        return {"z_naam": "ontbreekt", "z_nummer": "ontbreekt", "z_set": "ontbreekt"}
    ql = q.lower()
    toks = re.findall(r"[A-Za-z0-9#\-']+(?:/[A-Za-z0-9\-]+)?", q)
    nrs = {n_nr(t.lstrip("#").split("/")[0]) for t in toks if re.match(r"#?\d", t)}
    sets = set()
    for t in toks:
        for part in t.split("/"):
            u = part.upper().strip("#")
            if SET_RE.match(u) and u not in RAR and not u.isdigit():
                sets.add(n_set(u))
    out = {"z_naam": "goed" if any(tk.split("-")[0] in ql for tk in w["tokens"]) else "fout",
           "z_nummer": "n.v.t." if not w["nr"] else ("goed" if w["nr"] in nrs else "fout")}
    if w["set"] is None:
        out["z_set"] = "n.v.t."
    else:
        out["z_set"] = "goed" if n_set(w["set"]) in sets else ("fout" if sets else "ontbreekt")
    return out


truth_cm = {}
for iid, w in W.items():
    if iid.startswith("_") or w["soort"] not in ("ok", "stockfoto") or not w["nr"]:
        continue
    url = None
    for tok in w["tokens"]:
        sc = supabase_client.geldige_setcode(w["set"]) if w["set"] else None
        hit = supabase_client.lookup(tok.split("-")[0] if tok != "ho-oh" else "ho-oh", w["nr"], set_code=sc,
                                     set_hint=None if sc else w["set"])
        if hit and hit.get("url"):
            url = hit["url"]; break
    truth_cm[iid] = url


def score_cm(url, iid):
    t = truth_cm.get(iid)
    if t is None:
        return "geen_waarheid_link" if not url else "link_terwijl_catalogus_geen_ware_link"
    if not url:
        return "ontbreekt"
    return "goed" if url == t else "fout"


RANG = {"goed": 2, "ontbreekt": 1, "fout": 0}
rows = []
for r in RES:
    iid = r["item_id"]; w = W[iid]
    row = {"item_id": iid, "categorie": r["categorie"], "soort": w["soort"], "waarheid": w["naam"], "cm_waarheid": truth_cm.get(iid)}
    for v, key in (("live", "uit_live_opgeslagen"), ("foto", "uit_met_foto"), ("geen", "uit_zonder_gemini")):
        u = r[key] or {}
        row[v] = {**score_key(u.get("card_key"), w), **score_query(u.get("ebay_query"), w),
                  "cm": score_cm(u.get("cm_url"), iid), "cm_url": u.get("cm_url"), "zoekzin": u.get("ebay_query")}
    rows.append(row)
(HERE / "data" / "g34_beoordeling.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))

out = []
P = out.append
beoordeeld = [x for x in rows if x["soort"] in ("ok", "stockfoto")]
P(f"Items: {len(rows)}; beoordeeld (ok+stockfoto): {len(beoordeeld)}; lots: {sum(x['soort']=='lot' for x in rows)}; niet-PSA: {sum(x['soort']=='niet_psa' for x in rows)}")
P(f"CM-waarheid gevonden in catalogus: {sum(1 for x in beoordeeld if x['cm_waarheid'])}/{len(beoordeeld)}")
DIM = ["naam", "nummer", "set", "grade", "z_naam", "z_nummer", "z_set", "cm"]
P("\nSTAND per variant (goed/ontbreekt/fout):")
for v in ("live", "foto", "geen"):
    P(f"  {v:5} " + "  ".join(f"{d}={dict(Counter(x[v][d] for x in beoordeeld))}" for d in DIM))


def vergelijk(a, b, groep, label):
    P(f"\n{label}  (per onderdeel: beter / gelijk / slechter)")
    for d in DIM:
        c = Counter()
        voorbeelden = defaultdict(list)
        for x in groep:
            ra, rb = x[a][d], x[b][d]
            if d == "cm":
                ma = {"goed": 2, "ontbreekt": 1, "geen_waarheid_link": 2, "fout": 0, "link_terwijl_catalogus_geen_ware_link": 0}
                sa, sb = ma[ra], ma[rb]
            else:
                if ra == "n.v.t." or rb == "n.v.t.":
                    continue
                sa, sb = RANG[ra], RANG[rb]
            k = "beter" if sb > sa else "slechter" if sb < sa else "gelijk"
            c[k] += 1
            if k != "gelijk":
                voorbeelden[k].append(x["item_id"])
        P(f"  {d:9} beter={c['beter']:2} gelijk={c['gelijk']:2} slechter={c['slechter']:2}   beter:{voorbeelden['beter'][:6]} slechter:{voorbeelden['slechter'][:6]}")


vergelijk("live", "foto", beoordeeld, "G3: Gemini MÉT foto t.o.v. live (Gemini zonder foto)")
vergelijk("live", "geen", beoordeeld, "G4: ZONDER Gemini t.o.v. live")
vergelijk("foto", "geen", beoordeeld, "extra: zonder Gemini t.o.v. Gemini-met-foto")


def kaart_klopt(s):
    return s["naam"] == "goed" and s["nummer"] in ("goed", "n.v.t.") and s["set"] in ("goed", "n.v.t.")


P("\nKAART-IDENTITEIT volledig goed (naam+nummer+set in card_key):")
for v in ("live", "foto", "geen"):
    P(f"  {v}: {sum(kaart_klopt(x[v]) for x in beoordeeld)}/{len(beoordeeld)}")
P("\nVERANDERINGEN in uitkomst (ongeacht goed/fout):")
for a, b in (("live", "foto"), ("live", "geen")):
    P(f"  {a}→{b}: card_key anders={sum(x[a]['sleutel']!=x[b]['sleutel'] for x in rows)}/{len(rows)}  "
      f"zoekzin anders={sum(x[a]['zoekzin']!=x[b]['zoekzin'] for x in rows)}  cm-link anders={sum(x[a]['cm_url']!=x[b]['cm_url'] for x in rows)}")
P("\nPER CATEGORIE kaart-identiteit goed (live / foto / geen):")
for cat in sorted({x["categorie"] for x in beoordeeld}):
    g = [x for x in beoordeeld if x["categorie"] == cat]
    P(f"  {cat:24} n={len(g):2}  live={sum(kaart_klopt(x['live']) for x in g)} foto={sum(kaart_klopt(x['foto']) for x in g)} geen={sum(kaart_klopt(x['geen']) for x in g)}")
P("\nDETAIL per item (sleutel | cm):")
for x in rows:
    P(f"  {x['item_id']} [{x['soort']}/{x['categorie']}] waarheid={x['waarheid']!r} cm_waar={'ja' if x['cm_waarheid'] else 'nee'}")
    for v in ("live", "foto", "geen"):
        s = x[v]
        P(f"     {v:4} key={s['sleutel']} n/nr/set/gr={s['naam']}/{s['nummer']}/{s['set']}/{s['grade']} zoekzin={s['zoekzin']!r} z={s['z_naam']}/{s['z_nummer']}/{s['z_set']} cm={s['cm']}")
P(f"\nSCHRIJF-SLOT: {json.dumps({k: len(v) for k, v in veilig.BLOCKED.items() if k != 'sqlite_rw_open'})}")
txt = "\n".join(out)
print(txt)
(HERE / "data" / "g34_tellingen.txt").write_text(txt)
