#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Diagnose 2 — schaadt de Japanse listing-titel Qwen? (Tommy 9-9)

Hergebruikt de naam-fails uit het schaduwlog (foto's die we al hebben).
Per kaart: Qwen MÉT titel (zoals live) vs Qwen ZONDER titel — beide vers,
zelfde foto, zodat het een schone gecontroleerde vergelijking is.

Vergelijkt het EERSTE woord (= de kaart-subject: pokémon of trainer) tegen
Gemini's naam (proxy-waarheid; op dit segment 3× PSA-bevestigd). Eerste woord,
want dát draagt de kaart-identiteit — 'Charizard VSTAR' vs 'Charizard' telt als
gelijk (segmentatie is geen identiteitsfout).

Read-only; raakt schaduw/live niet.
"""
from __future__ import annotations
import base64, json, sys, urllib.request, re
from pathlib import Path

DEV = Path(__file__).resolve().parent
KENSA = DEV.parent
sys.path.insert(0, str(KENSA)); sys.path.insert(0, str(DEV))
from llm_client import PHOTO_INTERPRET_PROMPT
from storage_supabase import _http
from qwen_backtest import norm_ws

ENDPOINT = "http://100.125.116.37:1234/v1/chat/completions"
MODEL = "qwen2.5-vl-7b-instruct"
UA = {"User-Agent": "Mozilla/5.0"}
N = int(sys.argv[1]) if len(sys.argv) > 1 else 24


def eerste_woord(s: str) -> str:
    return norm_ws(s).split("/")[-1].split()[0] if norm_ws(s).split() else ""


def call(img_b64, title_jp):
    user = f"Analyseer deze PSA-slab foto.\nListing titel EN: (geen)\nListing titel JP: {title_jp or '(geen)'}\n\nGeef JSON."
    payload = {"model": MODEL, "messages": [
        {"role": "system", "content": PHOTO_INTERPRET_PROMPT},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
            {"type": "text", "text": user}]}],
        "temperature": 0, "max_tokens": 512}
    req = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=75) as r:
        raw = json.loads(r.read())["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M), re.DOTALL)
    try:
        return (json.loads(m.group(0)).get("name") if m else None) or "(geen)"
    except Exception:
        return "(parse)"


def main():
    rows = [json.loads(l) for l in (DEV / "qwen_shadow/shadow_log.jsonl").read_text().splitlines() if l.strip()]
    fails = [r for r in rows if r.get("status") == "ok" and not r["vergelijk"]["name_agree"]
             and r["gemini"].get("card_name") and r["foto"].get("url")
             and eerste_woord(r["qwen"].get("name") or "") != eerste_woord(r["gemini"]["card_name"])]
    step = max(1, len(fails) // N)
    steek = fails[::step][:N]

    ids = [r["item_id"] for r in steek]
    titels = {}
    for i in range(0, len(ids), 50):
        for row in _http.get("listings", "kensa", [("select", "item_id,title_jp"),
                             ("item_id", f"in.({','.join(ids[i:i+50])})"), ("limit", "80")]):
            titels[row["item_id"]] = row.get("title_jp") or ""

    print(f"eerste-woord naam-fails: {len(fails)} — test {len(steek)}\n")
    met = zonder = 0
    uit = []
    for r in steek:
        try:
            req = urllib.request.Request(r["foto"]["url"], headers=UA)
            img_b64 = base64.b64encode(urllib.request.urlopen(req, timeout=25).read()).decode()
        except Exception as e:
            print(f"  fetch-fail {r['item_id']}"); continue
        truth = r["gemini"]["card_name"]
        tw = eerste_woord(truth)
        a = call(img_b64, titels.get(r["item_id"]))   # MÉT titel (live-config)
        b = call(img_b64, None)                        # ZONDER titel
        a_ok = eerste_woord(a) == tw
        b_ok = eerste_woord(b) == tw
        met += a_ok; zonder += b_ok
        flip = "→FIX" if (b_ok and not a_ok) else ("→BREAK" if a_ok and not b_ok else "")
        uit.append((r["item_id"], truth, a, b, a_ok, b_ok, flip))
        print(f"  met={'✓' if a_ok else '✗'} zonder={'✓' if b_ok else '✗'} truth={truth!r:24s} "
              f"MET={a!r:24s} ZONDER={b!r:24s} {flip}")
    n = len(uit)
    print(f"\n=== eerste-woord goed — MÉT titel: {met}/{n} · ZONDER titel: {zonder}/{n} ===")
    (DEV / "qwen_shadow/title_probe.json").write_text(json.dumps(uit, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
