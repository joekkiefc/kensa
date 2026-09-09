#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Head-to-head op de gouden set (30 kaarten, PSA-register = absolute waarheid).

Per kaart: PSA-waarheid | Gemini | Qwen, verdict per veld. Geen model-calls —
gebruikt de al gedraaide backtest_results.json (Qwen prompt V4, zonder titel)
+ gemini_ref. Doel: één helder eindoordeel + exact welke kaarten wie mist.
"""
import json, re, sys
from pathlib import Path
DEV = Path(__file__).resolve().parent
sys.path.insert(0, str(DEV.parent))
from qwen_backtest import norm_grade, norm_number, cert_digits, norm_ws

d = json.load(open(DEV / "qwen_goldenset/backtest_results.json"))
kaarten = [r for r in d["per_kaart"] if r.get("type") == "kaart"]

SUB = {"VMAX","VSTAR","GX","EX","V","SAR","SR","UR","HR","AR","CHR","BREAK","HOLO","FA","LVX","LV.X"}
def subject(naam):
    """Kern-subject: strip FA/-prefix, subtype-woorden, HOLO-suffix, punctuatie."""
    s = norm_ws(naam).replace("/", " ").replace("-", " ").replace(".", " ")
    toks = [t for t in s.split() if t not in SUB and not re.fullmatch(r"\d+", t)]
    return toks[0] if toks else ""

def score(ans, truth):
    return {
        "cert": cert_digits(ans.get("cert")) == truth["cert"],
        "grade": norm_grade(ans.get("grade")) == norm_grade(truth["grade"]),
        "number": (lambda a,t: (a==t) if "/" in t else (a.split("/")[0]==t))(
            norm_number(ans.get("number")), norm_number(truth["number"])),
        "subject": subject(ans.get("card_name") or ans.get("name") or "") == subject(truth["card_name"]),
    }

rows = []
gem_vol = qw_vol = 0
for r in kaarten:
    t = r["waarheid"]
    g = score(r["gemini_ref"] or {}, t)
    q = score(r["qwen"] or {}, t)
    gem_ok = all(g.values()); qw_ok = all(q.values())
    gem_vol += gem_ok; qw_vol += qw_ok
    rows.append((r["item_id"], t, r["gemini_ref"] or {}, r["qwen"] or {}, g, q, gem_ok, qw_ok))

n = len(kaarten)
print(f"=== HEAD-TO-HEAD — {n} kaarten, waarheid = PSA-register ===\n")
for veld in ("cert","grade","number","subject"):
    gv = sum(1 for _,_,_,_,g,_,_,_ in rows if g[veld])
    qv = sum(1 for _,_,_,_,_,q,_,_ in rows if q[veld])
    print(f"  {veld:8s}  Gemini {gv:2d}/{n}   Qwen {qv:2d}/{n}")
print(f"  {'VOLLEDIG':8s}  Gemini {gem_vol:2d}/{n}   Qwen {qw_vol:2d}/{n}\n")

def toon(r):
    iid,t,g,q,gs,qs,go,qo = r
    fout = lambda s: ",".join(k for k in ("cert","grade","number","subject") if not s[k])
    print(f"  {iid}  waarheid: {t['card_name']} #{t['number']} g{t['grade']} cert{t['cert']}")
    if not go: print(f"     GEMINI fout [{fout(gs)}]: naam={g.get('card_name')} nr={g.get('number')} cert={g.get('cert')}")
    if not qo: print(f"     QWEN   fout [{fout(qs)}]: naam={q.get('card_name')} nr={q.get('number')} cert={q.get('cert')}")

print("--- Kaarten die QWEN mist (en wat hij ervan maakt):")
qw_fout = [r for r in rows if not r[7]]
for r in qw_fout: toon(r)
print(f"\n--- Kaarten die GEMINI mist:")
gem_fout = [r for r in rows if not r[6]]
for r in gem_fout: toon(r)

print(f"\n=== SAMENVATTING: Gemini {gem_vol}/{n} volledig goed · Qwen {qw_vol}/{n} · "
      f"Qwen mist {len(qw_fout)}, Gemini mist {len(gem_fout)} ===")
