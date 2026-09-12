#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""G2 verdieping — alleen lokale bestanden (geen netwerk). Schrijft data/g2_detail.txt."""
import json
import re
import statistics as st
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
D = json.loads((HERE / "data" / "supabase_pull.json").read_text())
R = [r for r in json.loads((HERE / "data" / "g2_items.json").read_text()) if r["pad"]]
T = D["traps"]
out = []
P = out.append

P("in_tokens per foto-groep (alleen echte Gemini-calls):")
for k in sorted({(r["sqlite_heeft_foto"], r["hash_velden_in_llm_cache"]) for r in R}):
    xs = [r["in_tokens"] for r in R if (r["sqlite_heeft_foto"], r["hash_velden_in_llm_cache"]) == k and r["in_tokens"]]
    if xs:
        P(f"  sqlite_foto={k[0]!s:5} hash_velden={k[1]!s:5} n={len(xs):5} min={min(xs)} med={st.median(xs)} max={max(xs)}")

# 54 zonder foto en zonder hash-match: waarom?
odd = [r for r in R if not r["sqlite_heeft_foto"] and not r["hash_velden_in_llm_cache"]]
c = Counter()
for r in odd:
    s_at = T["slab_ocr"][r["item_id"]]["created_at"]; l_at = T["llm_slab"][r["item_id"]]["created_at"]
    c[(r["pad"], r["meta_model"])] += 1
P(f"\n54-groep (geen sqlite-foto, geen hash): {dict(c)}; in_tokens max={max([r['in_tokens'] or 0 for r in odd] or [0])}")


def parts(ck):
    if not ck:
        return None
    p = ck.split(":")
    return {"pokemon": p[0], "nr": p[1], "grade": p[2], "set": p[3] if len(p) > 3 else ""}


def nnr(x):
    m = re.match(r"#?0*(\d+)", x or "")
    return m.group(1) if m else x


cc = Counter(); ex = {}
for r in R:
    a, b = parts(r["ck_llm"]), parts(r["ck_none"])
    if r["ck_llm"] == r["ck_none"]:
        cc["gelijk"] += 1; continue
    if not a or not b:
        cc["één van beide None"] += 1; continue
    diff = []
    if a["pokemon"] != b["pokemon"]: diff.append("pokemon")
    if nnr(a["nr"]) != nnr(b["nr"]): diff.append("nummer")
    elif a["nr"] != b["nr"]: diff.append("nummer-schrijfwijze")
    if a["grade"] != b["grade"]: diff.append("grade")
    if a["set"] != b["set"]: diff.append("set")
    k = "+".join(diff)
    cc[k] += 1
    ex.setdefault(k, []).append((r["item_id"], r["ck_none"], r["ck_llm"], r["label_name"]))
P("\ncard_key-verschil per onderdeel (zonder Gemini → met Gemini):")
for k, n in cc.most_common():
    P(f"  {k:35} {n}")
    for e in ex.get(k, [])[:3]:
        P(f"      {e[0]}: {e[1]}  →  {e[2]}   label={e[3]!r}")

kern_anders = [r for r in R if parts(r["ck_llm"]) and parts(r["ck_none"]) and (
    parts(r["ck_llm"])["pokemon"] != parts(r["ck_none"])["pokemon"] or nnr(parts(r["ck_llm"])["nr"]) != nnr(parts(r["ck_none"])["nr"])
    or parts(r["ck_llm"])["grade"] != parts(r["ck_none"])["grade"])]
P(f"\nkaart-KERN (pokemon/nummer/grade) anders: {len(kern_anders)}")

P("\npokemon 'anders' voorbeelden (Qwen card_name → Gemini name):")
for r in [r for r in R if r["q_pokemon"] and r["g_pokemon"] and r["q_pokemon"] != r["g_pokemon"]][:12]:
    P(f"  {r['item_id']}: q={T['slab_ocr'][r['item_id']]['result_json'].get('card_name')!r} ({r['q_pokemon']}) g={T['llm_slab'][r['item_id']]['result_json'].get('name')!r} ({r['g_pokemon']})")
P("pokemon: q_pokemon zit in pokedex?")
sys.path.insert(0, str(HERE.parents[1]))
from analyze_split.ebay_phase import _pokemon_en_lookup  # noqa: E402
pdx = _pokemon_en_lookup()
P(f"  {dict(Counter((r['q_pokemon'] in pdx if r['q_pokemon'] else None, bool(r['g_pokemon'])) for r in R))}  (sleutel: (qwen-naam in pokedex, gemini-naam bevat pokedex-woord))")
P("  Gemini-pokemon ≠ Qwen-pokemon terwijl BEIDE in pokedex:")
bp = [r for r in R if r["q_pokemon"] in pdx and r["g_pokemon"] and r["q_pokemon"] != r["g_pokemon"]]
P(f"  n={len(bp)}")
for r in bp[:15]:
    P(f"    {r['item_id']}: q={T['slab_ocr'][r['item_id']]['result_json'].get('card_name')!r} g={T['llm_slab'][r['item_id']]['result_json'].get('name')!r} label={r['label_name']!r}")

P("\nNUMMER-verschillen (alle):")
for r in R:
    if r["q_nr"] and r["g_nr"] and r["q_nr"] != r["g_nr"]:
        P(f"  {r['item_id']}: q={r['q_nr_raw']!r} g={r['g_nr_raw']!r} label={r['label_name']!r}")
P("\nGRADE-verschillen (alle):")
for r in R:
    if r["q_grade"] != r["g_grade"]:
        P(f"  {r['item_id']}: q={r['q_grade']!r} g={r['g_grade']!r} label={r['label_name']!r} conf={r['g_conf']}")

P("\nSET 'anders' categorieën:")
c = Counter(); exs = {}
for r in R:
    q, g = r["q_set"].upper(), r["g_set"].upper()
    if q == g: continue
    if not q and g: k = "qwen geen set, gemini wel"
    elif q and not g: k = "gemini geen (geldige) set"
    elif g == q + "-P": k = "gemini voegt -P toe"
    elif q == g + "-P": k = "gemini haalt -P weg"
    else: k = "echt andere set"
    c[k] += 1; exs.setdefault(k, []).append((r["item_id"], r["q_set"], r["g_set_raw"], r["label_name"]))
for k, n in c.most_common():
    P(f"  {k:30} {n}")
    for e in exs[k][:4]:
        P(f"      {e}")

# -P op label: label bevat 'SVxx JP' zonder -P, maar qwen-set eindigt op -P
lblP = [r for r in R if r["q_set"].upper().endswith("-P") and r["label_name"] and "-P" not in r["label_name"].upper()
        and re.search(r"\b(SV|S|SM)\d+[A-Za-z]?\s+JP\b", r["label_name"], re.I)]
P(f"\nQwen -P terwijl labelregel een gewone set-code zonder -P toont: {len(lblP)}; gemini_set daarbij: {dict(Counter(r['g_set'].upper().endswith('-P') for r in lblP))} (True = Gemini houdt -P)")

# eBay: trouw van query-replay op verse (niet-cache) fetches
E = T["ebay_prices"]
fresh_llm = [r for r in R if r["item_id"] in E and not E[r["item_id"]]["result_json"].get("from_cache") and E[r["item_id"]]["result_json"].get("query_source") == "llm"]
P(f"\neBay verse fetch met LLM-zoekzin: n={len(fresh_llm)}; q_llm == ebay_prices.query: {sum(r['q_llm'] == E[r['item_id']]['result_json'].get('query') for r in fresh_llm)}")
P(f"eBay-resultaat per llm-item: {dict(Counter((E[r['item_id']]['result_json'].get('query_source'), bool(E[r['item_id']]['result_json'].get('from_cache')), E[r['item_id']]['result_json'].get('cache_source')) for r in R if r['item_id'] in E))}")
sr = [r for r in R if r["pad"] == "score_retry"]
P(f"score-retry: ebay na retry query_source={dict(Counter(E[r['item_id']]['result_json'].get('query_source') for r in sr if r['item_id'] in E))} "
  f"from_cache={dict(Counter(bool(E[r['item_id']]['result_json'].get('from_cache')) for r in sr if r['item_id'] in E))}; ck_llm==ck_none: {sum(r['ck_llm']==r['ck_none'] for r in sr)}")
P(f"judge (filter_source=llm) bij llm-items: {dict(Counter((bool(E[r['item_id']]['result_json'].get('from_cache')), E[r['item_id']]['result_json'].get('filter_source')) for r in R if r['item_id'] in E))}")

txt = "\n".join(out)
print(txt)
(HERE / "data" / "g2_detail.txt").write_text(txt)
