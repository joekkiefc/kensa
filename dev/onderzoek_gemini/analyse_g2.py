#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""G1/G2 — replay over ALLE Qwen-items sinds 9-9 16:44 met de échte (pure) live-functies.
Leest data/supabase_pull.json + kensa.db read-only. Schrijft alleen data/g2_*.json.
CM-lookups (alleen GET op kensa cards-catalogus) alleen waar de invoer met/zonder Gemini verschilt.
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
KENSA = HERE.parents[1]
sys.path.insert(0, str(HERE))
import veilig  # noqa: E402  (vóór Kensa-imports)
sys.path.insert(0, str(KENSA))

import json  # noqa: E402
import re  # noqa: E402
import sqlite3  # noqa: E402
from collections import Counter, defaultdict  # noqa: E402

import analyze  # noqa: E402  (voor _needs_llm)
from analyze_split import ebay_phase  # noqa: E402
from analyze_split.ebay_query_split import _ebay_build_query_v2  # noqa: E402
from analyze_split.enqueue_cardmarket import _enq_extract_identity, _looks_like_bundle  # noqa: E402
from check_title import _extract_slab_pokemon_en  # noqa: E402
from llm_client import _hash_ocr  # noqa: E402
import supabase_client  # noqa: E402

veilig.sluit_writers()
DO_CM = "--cm" in sys.argv

D = json.loads((HERE / "data" / "supabase_pull.json").read_text())
T = D["traps"]
L = D["listings"]


def rj(trap, iid):
    r = T[trap].get(iid)
    return (r or {}).get("result_json"), (r or {}).get("created_at")


def norm_nr(x):
    s = str(x or "").strip().lstrip("#").split("/")[0].strip()
    m = re.match(r"\d+", s)
    return (m.group(0).lstrip("0") or "0") if m else ""


def norm_grade(x):
    if x in (None, "", "null"):
        return ""
    try:
        f = float(str(x).strip())
        return str(int(f)) if f == int(f) else str(f)
    except Exception:
        return str(x).strip()


def eff_setcode_qwen(slab):
    sc = supabase_client.geldige_setcode(slab.get("set_code")) if slab.get("set_code") else None
    nr = str(slab.get("number") or "")
    if not sc and "/" in nr:
        sc = supabase_client.geldige_setcode(nr.split("/", 1)[1])
    return sc or ""


def gem_pokemon(llm):
    words = (llm.get("name") or "").split()
    pdx = ebay_phase._pokemon_en_lookup()
    return next((w.lower() for w in words if len(w) >= 4 and w.lower() in pdx), None)


# --- read-only sqlite (bevroren mirror + live llm_slab_cache) ---
con = sqlite3.connect(veilig.KENSA_DB)
sqlite_photo_items = set()
items = sorted(set(D["qwen_items"]) | set(D["llm_items"]))
for i in range(0, len(items), 500):
    ch = items[i:i + 500]
    q = "SELECT DISTINCT item_id FROM photos WHERE item_id IN (%s)" % ",".join("?" * len(ch))
    sqlite_photo_items |= {r[0] for r in con.execute(q, ch)}


def cache_has(h):
    return con.execute("SELECT 1 FROM llm_slab_cache WHERE ocr_hash=?", (h,)).fetchone() is not None


cm_cache = {}
n_lookup_calls = 0


def cm(pokemon, number, set_hint, set_code, soft_hint):
    global n_lookup_calls
    key = (pokemon, str(number), set_hint, set_code, soft_hint)
    if key not in cm_cache:
        n_lookup_calls += 1
        try:
            hit = supabase_client.lookup(pokemon, str(number), set_hint=set_hint, set_code=set_code, soft_hint=soft_hint)
        except Exception as e:
            hit = {"_err": str(e)}
        cm_cache[key] = (hit or {}).get("url") if hit else None
    return cm_cache[key]


def cm_inputs(slab, llm):
    pokemon, number = _enq_extract_identity(slab, llm)
    ld = llm or {}
    return (pokemon, number, ld.get("set_name") or slab.get("set_name"),
            ld.get("set_code") or slab.get("set_code"), slab.get("label_name"))


if DO_CM:
    # Voor-ophalen: alle unieke lookup-invoeren parallel (alleen GET), daarna leest cm() uit cm_cache.
    from concurrent.futures import ThreadPoolExecutor
    keys = set()
    for iid in D["llm_items"]:
        slab, _ = rj("slab_ocr", iid); llm, _ = rj("llm_slab", iid)
        if not slab:
            continue
        for x in (cm_inputs(slab, llm), cm_inputs(slab, None)):
            if x[0] and x[1]:
                keys.add((x[0], str(x[1]), x[2], x[3], x[4]))

    def _one(key):
        try:
            hit = supabase_client.lookup(key[0], key[1], set_hint=key[2], set_code=key[3], soft_hint=key[4])
        except Exception as e:
            return key, "ERR:" + str(e)[:60]
        return key, (hit or {}).get("url") if hit else None

    with ThreadPoolExecutor(8) as ex:
        for key, url in ex.map(_one, sorted(keys, key=str)):
            cm_cache[key] = url
    n_lookup_calls = len(keys)
    print("cm-lookups klaar:", len(keys), file=sys.stderr)

rows = []
gate = Counter()
for iid in D["qwen_items"]:
    slab, s_at = rj("slab_ocr", iid)
    llm, l_at = rj("llm_slab", iid)
    summ, _ = rj("summary", iid)
    listing = L.get(iid) or {}
    needs, reason = analyze._needs_llm(slab)
    reason_k = re.sub(r"'.*?'", "'…'", re.sub(r"\(\d+ tokens\)", "(n tokens)", reason))
    pad = None
    if llm:
        pad = "ocr" if (l_at <= s_at) else "score_retry"
    gate[(needs, reason_k, pad or "geen_llm")] += 1
    rec = {"item_id": iid, "gate_open": needs, "gate_reason": reason, "pad": pad,
           "slab_status": slab.get("status"), "live_card_key": listing.get("card_key"),
           "live_query": (summ or {}).get("ebay_query"), "live_query_source": (summ or {}).get("query_source")}
    if llm:
        meta = llm.get("_meta") or {}
        ocr_fields = "\n".join(str(slab.get(k)) for k in ("year", "set_name", "card_name", "number", "grade_text", "grade", "cert") if slab.get(k))
        h = _hash_ocr(ocr_fields, listing.get("title_en"), listing.get("title_jp"))
        rec.update({
            "meta_model": meta.get("model"), "in_tokens": meta.get("in_tokens"), "out_tokens": meta.get("out_tokens"),
            "latency_ms": meta.get("latency_ms"),
            "sqlite_heeft_foto": iid in sqlite_photo_items,
            "hash_velden_in_llm_cache": cache_has(h),
            "q_pokemon": _extract_slab_pokemon_en(slab.get("card_name") or ""), "g_pokemon": gem_pokemon(llm),
            "q_nr": norm_nr(slab.get("number")), "g_nr": norm_nr(llm.get("number")),
            "q_nr_raw": slab.get("number"), "g_nr_raw": llm.get("number"),
            "q_grade": norm_grade(slab.get("grade")), "g_grade": norm_grade(llm.get("grade")),
            "q_set": eff_setcode_qwen(slab), "g_set_raw": llm.get("set_code"),
            "g_set": supabase_client.geldige_setcode(llm.get("set_code")) or "",
            "q_setname": slab.get("set_name"), "g_setname": llm.get("set_name"),
            "label_name": slab.get("label_name"), "g_conf": llm.get("confidence"),
        })
        ck_llm = ebay_phase._build_card_key(slab, llm)
        ck_none = ebay_phase._build_card_key(slab, None)
        q_llm = _ebay_build_query_v2(slab, listing, llm, False)
        q_none = _ebay_build_query_v2(slab, listing, None, False)
        rec.update({"ck_llm": ck_llm, "ck_none": ck_none, "q_llm": q_llm[0], "q_llm_skip": (q_llm[2] or {}).get("skipped"),
                    "q_none": q_none[0], "q_none_skip": (q_none[2] or {}).get("skipped")})
        cm_ok = slab.get("status") == "pass" and not _looks_like_bundle(listing.get("title_jp"), listing.get("title_en"))
        in_llm, in_none = cm_inputs(slab, llm), cm_inputs(slab, None)
        rec["cm_in_llm"], rec["cm_in_none"] = in_llm, in_none
        rec["cm_guard_ok"] = cm_ok
        if DO_CM and cm_ok:
            u_llm = cm(*in_llm) if in_llm[0] and in_llm[1] else None
            u_none = cm(*in_none) if in_none[0] and in_none[1] else None
            rec["cm_url_llm"], rec["cm_url_none"] = u_llm, u_none
    rows.append(rec)

(HERE / "data").mkdir(exist_ok=True)
(HERE / "data" / ("g2_items_cm.json" if DO_CM else "g2_items.json")).write_text(json.dumps(rows, ensure_ascii=False, indent=0))

# ------------------------------------------------------------------ tellingen
out = []
P = out.append
llm_rows = [r for r in rows if r["pad"]]
P(f"Qwen-items: {len(rows)}   met llm_slab: {len(llm_rows)}")
P("\nGATE (_needs_llm) × pad:")
for (needs, reason, pad), n in sorted(gate.items(), key=lambda x: -x[1]):
    P(f"  gate_open={needs!s:5} pad={pad:11} n={n:5}  reden={reason}")
P("\nPAD × meta_model:")
for k, n in Counter((r["pad"], r["meta_model"]) for r in llm_rows).most_common():
    P(f"  {k}: {n}")
P("\nFOTO-bewijs (alle llm-items):")
for k, n in Counter((r["sqlite_heeft_foto"], r["hash_velden_in_llm_cache"]) for r in llm_rows).most_common():
    P(f"  sqlite_heeft_foto={k[0]} hash(alleen-velden) in llm_slab_cache={k[1]}: {n}")
gem = [r for r in llm_rows if (r["meta_model"] or "").startswith("gemini")]
P(f"\nGemini-calls (niet cache): {len(gem)}; in_tokens sum={sum(r['in_tokens'] or 0 for r in gem)} out sum={sum(r['out_tokens'] or 0 for r in gem)}; "
  f"latency sum={sum(r['latency_ms'] or 0 for r in gem)/1000:.0f}s gem={sum(r['latency_ms'] or 0 for r in gem)/max(1,len(gem)):.0f}ms")
for pad in ("ocr", "score_retry"):
    g = [r for r in gem if r["pad"] == pad]
    P(f"  pad {pad}: {len(g)} calls, latency sum {sum(r['latency_ms'] or 0 for r in g)/1000:.0f}s")


def cmp(a, b):
    if (a or "") == (b or ""):
        return "gelijk"
    if not b:
        return "gemini_leeg"
    if not a:
        return "qwen_leeg"
    return "anders"


P("\nVELD-VERSCHIL Qwen vs Gemini (llm-items):")
for veld in ("pokemon", "nr", "grade", "set"):
    c = Counter(cmp(r[f"q_{veld}"], r[f"g_{veld}"]) for r in llm_rows)
    P(f"  {veld:8}: {dict(c)}")
c = Counter()
for r in llm_rows:
    q, g = r["q_set"].upper(), r["g_set"].upper()
    if q == g:
        c["gelijk"] += 1
    elif g == q + "-P":
        c["gemini voegt -P toe"] += 1
    elif q == g + "-P":
        c["gemini haalt -P weg"] += 1
    else:
        c["anders"] += 1
P(f"  set -P detail: {dict(c)}")
sn = Counter(cmp(supabase_client._norm_set(r["q_setname"]), supabase_client._norm_set(r["g_setname"])) for r in llm_rows)
P(f"  set_name (genormaliseerd): {dict(sn)}")

P("\nUITKOMST card_key (met Gemini vs zonder):")
P(f"  {dict(Counter('gelijk' if r['ck_llm']==r['ck_none'] else ('alleen_met_gemini' if not r['ck_none'] else ('alleen_zonder' if not r['ck_llm'] else 'anders')) for r in llm_rows))}")
ocr_rows = [r for r in llm_rows if r["pad"] == "ocr"]
P(f"  replay-trouw (OCR-pad): ck_llm == live listings.card_key: {sum(r['ck_llm']==r['live_card_key'] for r in ocr_rows)}/{len(ocr_rows)}")
sr = [r for r in llm_rows if r["pad"] == "score_retry"]
P(f"  replay-trouw (score-pad): ck_none == live listings.card_key: {sum(r['ck_none']==r['live_card_key'] for r in sr)}/{len(sr)}; ck_llm==live {sum(r['ck_llm']==r['live_card_key'] for r in sr)}")
P("\nUITKOMST eBay-zoekzin:")
P(f"  {dict(Counter('gelijk' if r['q_llm']==r['q_none'] else 'anders' for r in llm_rows))}")
P(f"  skip met gemini={sum(bool(r['q_llm_skip']) for r in llm_rows)}  skip zonder={sum(bool(r['q_none_skip']) for r in llm_rows)}")
P(f"  alleen zonder-gemini skip (Gemini redt zoekzin): {sum(bool(r['q_none_skip']) and not r['q_llm_skip'] for r in llm_rows)}")
P(f"  replay-trouw: q_llm == summary.ebay_query: {sum(r['q_llm']==r['live_query'] for r in llm_rows)}/{len(llm_rows)}")
if DO_CM:
    P("\nUITKOMST CM-link (lookup met Gemini-invoer vs Qwen-invoer), alleen items die CM-guards passeren:")
    g = [r for r in llm_rows if r["cm_guard_ok"]]
    cc = Counter()
    for r in g:
        a, b = r.get("cm_url_llm"), r.get("cm_url_none")
        cc["gelijk (beide geen)" if not a and not b else "gelijk url" if a == b else "alleen met Gemini" if a and not b else "alleen zonder Gemini" if b and not a else "andere url"] += 1
    P(f"  {dict(cc)}  (lookup-calls: {n_lookup_calls})")
    P(f"  cm_invoer identiek: {sum(r['cm_in_llm']==r['cm_in_none'] for r in g)}/{len(g)}")
P(f"\nSCHRIJF-SLOT: {json.dumps({k: len(v) for k, v in veilig.BLOCKED.items() if k!='sqlite_rw_open'})}  (sqlite opens ro-geforceerd: {len(veilig.BLOCKED['sqlite_rw_open'])})")
txt = "\n".join(out)
print(txt)
(HERE / "data" / ("g2_tellingen_cm.txt" if DO_CM else "g2_tellingen.txt")).write_text(txt)
