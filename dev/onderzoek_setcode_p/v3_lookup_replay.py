#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""V3: live lookup-pad naspelen (alleen lezen). Zelfde functies als cm_bevoorrader→enqueue:
_load_stored_slab_llm → _enq_extract_identity → supabase_client.lookup(set_hint, set_code, soft_hint).
Controle: identieke call met alleen '-P' van set_code gestript."""
import os, sys, json, re
os.environ["KENSA_READ_STORAGE"] = "supabase"
sys.path.insert(0, "/home/pi/.openclaw/workspace/agents/kensa")
from analyze_split.score_only import _load_stored_slab_llm
from analyze_split.enqueue_cardmarket import _enq_extract_identity
from supabase_client import lookup
ids = sys.argv[1].split(",")
out = []
for iid in ids:
    slab, llm, _ = _load_stored_slab_llm(iid)
    pokemon, number = _enq_extract_identity(slab, llm)
    ld = llm or {}
    set_code = ld.get("set_code") or slab.get("set_code")
    set_hint = ld.get("set_name") or slab.get("set_name")
    soft = slab.get("label_name")
    live = lookup(pokemon, number, set_hint=set_hint, set_code=set_code, soft_hint=soft) if pokemon and number else None
    sc2 = re.sub(r"-P$", "", set_code or "", flags=re.I) or None
    nr2 = re.sub(r"/[A-Za-z0-9]+-P$", "", str(number or ""), flags=re.I)
    ctrl = lookup(pokemon, nr2, set_hint=set_hint, set_code=sc2, soft_hint=soft) if pokemon and number else None
    rec = dict(item_id=iid, llm=bool(llm), pokemon=pokemon, number=number, set_code=set_code, set_hint=set_hint,
               label=soft, live_url=(live or {}).get("url"), ctrl_set_code=sc2, ctrl_url=(ctrl or {}).get("url"))
    out.append(rec)
    print(json.dumps(rec, ensure_ascii=False))
json.dump(out, open(sys.argv[2] if len(sys.argv) > 2 else "v3_lookup_replay.json", "w"), ensure_ascii=False, indent=1)
