#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Diagnose — waar komt de naam-fail vandaan? (Tommy 9-9)

A/B op de bekende naam-fails uit het schaduwlog:
  A = huidige live-prompt (PHOTO_INTERPRET_PROMPT, 'lees label EN kaart, combineer')
  B = label-only naam ('kopieer de Engelse naam die op het PSA-label staat;
      romaniseer NOOIT de grote Japanse artwork-naam')

Hypothese: de Engelse naam staat ALTIJD op het PSA-label. De fail is een
KEUZE-fout (Qwen romaniseert het grote Japanse woord op de kaart) — geen
lees-fout. B maakt van 'naam' een pure kopieertaak (waar Qwen sterk in is).

Proxy-waarheid = Gemini's naam (voor dit trainer-segment 3× PSA-geverifieerd:
DAWN/IONO/FA-CYNTHIA). Read-only; raakt schaduw/live niet.
"""
from __future__ import annotations
import base64, json, sys, urllib.request
from pathlib import Path

DEV = Path(__file__).resolve().parent
KENSA = DEV.parent
sys.path.insert(0, str(KENSA)); sys.path.insert(0, str(DEV))
from llm_client import PHOTO_INTERPRET_PROMPT
from qwen_backtest import norm_ws

ENDPOINT = "http://100.125.116.37:1234/v1/chat/completions"
MODEL = "qwen2.5-vl-7b-instruct"
UA = {"User-Agent": "Mozilla/5.0"}
N = int(sys.argv[1]) if len(sys.argv) > 1 else 15

# Prompt B: alleen het name-veld anders — label-only, anti-romanisatie.
PROMPT_B = PHOTO_INTERPRET_PROMPT.replace(
    '"name": "<Pokemon/trainer/kaart-naam zoals op de PSA label, in Engels; behoud multi-word namen: \'Rocket\'s Moltres\', \'Galarian Zapdos\', \'Alolan Vulpix\'>"',
    '"name": "<KOPIEER de Engelse naam die op het PSA-LABEL staat (de tekstregel op het rood-witte label), letter voor letter. De Engelse naam staat ALTIJD op het label. Romaniseer/transcribeer NOOIT de grote Japanse naam op de kaart-artwork — schrijf dus DAWN (van het label), niet Hikari (van de kaart)>"',
)
assert PROMPT_B != PHOTO_INTERPRET_PROMPT, "replace faalde — prompt-tekst gewijzigd?"


def call(system, img_b64, title_jp):
    user = f"Analyseer deze PSA-slab foto.\nListing titel EN: (geen)\nListing titel JP: {title_jp or '(geen)'}\n\nGeef JSON."
    payload = {"model": MODEL, "messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
            {"type": "text", "text": user}]}],
        "temperature": 0, "max_tokens": 512}
    req = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=75) as r:
        raw = json.loads(r.read())["choices"][0]["message"]["content"]
    import re
    m = re.search(r"\{.*\}", re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M), re.DOTALL)
    try:
        return (json.loads(m.group(0)).get("name") if m else None) or "(geen)"
    except Exception:
        return "(parse-fout)"


def main():
    rows = [json.loads(l) for l in (DEV / "qwen_shadow/shadow_log.jsonl").read_text().splitlines() if l.strip()]
    # naam-fails met bruikbare gemini-naam (= proxy-waarheid = label-Engels)
    fails = [r for r in rows if r.get("status") == "ok"
             and not r["vergelijk"]["name_agree"]
             and r["gemini"].get("card_name") and r["foto"].get("url")]
    # spreiding: neem elke k-de zodat het geen 15× dezelfde Jolteon is
    step = max(1, len(fails) // N)
    steek = fails[::step][:N]
    print(f"naam-fails totaal: {len(fails)} — test {len(steek)}\n")

    a_goed = b_goed = 0
    resultaten = []
    for r in steek:
        try:
            req = urllib.request.Request(r["foto"]["url"], headers=UA)
            img_b64 = base64.b64encode(urllib.request.urlopen(req, timeout=25).read()).decode()
        except Exception as e:
            print(f"  fetch-fail {r['item_id']}: {e}"); continue
        truth = r["gemini"]["card_name"]
        a = call(PHOTO_INTERPRET_PROMPT, img_b64, None)
        b = call(PROMPT_B, img_b64, None)
        a_ok, b_ok = norm_ws(a) == norm_ws(truth), norm_ws(b) == norm_ws(truth)
        a_goed += a_ok; b_goed += b_ok
        flip = "→FIXED" if (b_ok and not a_ok) else ("→BROKE" if (a_ok and not b_ok) else "")
        resultaten.append((r["item_id"], truth, a, b, a_ok, b_ok, flip))
        print(f"  {'B✓' if b_ok else 'B✗'}{'A✓' if a_ok else 'A✗'} truth={truth!r:26s} A={a!r:26s} B={b!r:26s} {flip}")

    n = len(resultaten)
    print(f"\n=== A (huidig): {a_goed}/{n} · B (label-only): {b_goed}/{n} ===")
    (DEV / "qwen_shadow/name_probe.json").write_text(json.dumps(resultaten, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
