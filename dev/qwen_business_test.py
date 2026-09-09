#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Business-test — Tommy 9-9: test tegen de EXACTE uitkomsten die tellen.

Gouden set (30 kaarten, PSA = waarheid). Qwen krijgt IDENTIEKE input als Gemini:
  - zelfde foto, verkleind naar 768px precies zoals _ip_fetch_photo dat doet
  - zelfde prompt (PHOTO_INTERPRET_PROMPT)
  - zelfde listing-titel (--zonder-titel om de deploy-config te meten)
Daarna beide door de ECHTE pijplijnfuncties:
  - eBay:       query_builder.build_query(naam, nummer, grade, set, jaar) -> zoekzin
  - Cardmarket: supabase_client.lookup(pokemon, nummer)                 -> CM-URL
en vergeleken met wat de PSA-waarheid door diezelfde functies oplevert.
Read-only (cm_lookup = Supabase read). Raakt schaduw/live niet.
"""
from __future__ import annotations
import base64, io, json, re, sys, urllib.request
from pathlib import Path
from PIL import Image

DEV = Path(__file__).resolve().parent
KENSA = DEV.parent
sys.path.insert(0, str(KENSA)); sys.path.insert(0, str(DEV))
from llm_client import PHOTO_INTERPRET_PROMPT
from query_builder import build_query
from supabase_client import lookup as cm_lookup
from check_title import _extract_slab_pokemon_en
from storage_supabase import _http

ENDPOINT = "http://100.125.116.37:1234/v1/chat/completions"
MODEL = "qwen2.5-vl-7b-instruct"
ZONDER_TITEL = "--zonder-titel" in sys.argv
FULLRES = "--fullres" in sys.argv   # deploy-config: lokale GPU heeft geen token-kosten
GOLD = DEV / "qwen_goldenset"


def downscale_768(img_bytes: bytes) -> bytes:
    """Exact de live Gemini-verkleining (_ip_fetch_photo)."""
    im = Image.open(io.BytesIO(img_bytes))
    w, h = im.size
    if max(w, h) > 768:
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        s = 768 / max(w, h)
        im = im.resize((int(w * s), int(h * s)), Image.LANCZOS)
        buf = io.BytesIO(); im.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue()
    return img_bytes


def qwen(img_bytes: bytes, title_en, title_jp) -> dict:
    user = (f"Analyseer deze PSA-slab foto.\nListing titel EN: {title_en or '(geen)'}\n"
            f"Listing titel JP: {title_jp or '(geen)'}\n\nGeef JSON.")
    b64 = base64.b64encode(img_bytes).decode()
    payload = {"model": MODEL, "messages": [
        {"role": "system", "content": PHOTO_INTERPRET_PROMPT},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            {"type": "text", "text": user}]}],
        "temperature": 0, "max_tokens": 512}
    req = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=75) as r:
        raw = json.loads(r.read())["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M), re.DOTALL)
    try:
        return json.loads(m.group(0)) if m else {}
    except Exception:
        return {}


def uitkomst(naam, nummer, grade, set_name, jaar) -> tuple[str | None, str | None]:
    """Wat de pijplijn met deze velden DOET: (eBay-zoekzin, CM-URL)."""
    try:
        jaar_i = int(jaar) if jaar not in (None, "", "null") else None
    except Exception:
        jaar_i = None
    q = build_query(naam, nummer, str(grade) if grade else None, set_name, jaar_i)
    pokemon = _extract_slab_pokemon_en(naam or "")
    hit = None
    if pokemon and nummer:
        try:
            hit = cm_lookup(pokemon, str(nummer))
        except Exception as e:
            hit = {"url": f"ERR {type(e).__name__}"}
    return q, (hit or {}).get("url")


def main():
    truth_all = json.loads((GOLD / "answers.json").read_text())["kaarten"]
    bt = {r["item_id"]: r for r in json.loads((GOLD / "backtest_results.json").read_text())["per_kaart"]
          if r.get("type") == "kaart"}
    ids = [k["item_id"] for k in truth_all]
    titels = {}
    for i in range(0, len(ids), 50):
        for row in _http.get("listings", "kensa", [("select", "item_id,title_en,title_jp"),
                             ("item_id", f"in.({','.join(ids[i:i+50])})"), ("limit", "80")]):
            titels[row["item_id"]] = (row.get("title_en"), row.get("title_jp"))

    print(f"=== BUSINESS-TEST gouden set — Qwen op {'FULL-RES' if FULLRES else 'IDENTIEKE 768px'}-foto, "
          f"{'ZONDER' if ZONDER_TITEL else 'MET'} titel (= {'deploy-config' if ZONDER_TITEL else 'exact Gemini-input'}) ===\n")
    tot = {"cm_g": 0, "cm_q": 0, "eb_g": 0, "eb_q": 0, "cm_truth": 0}
    rijen = []
    for k in truth_all:
        iid, t = k["item_id"], k["waarheid"]
        g = (bt.get(iid) or {}).get("gemini_ref") or {}
        img = (GOLD / k["photo"]).read_bytes()
        te, tj = titels.get(iid, (None, None))
        q = qwen(img if FULLRES else downscale_768(img), None if ZONDER_TITEL else te, None if ZONDER_TITEL else tj)

        eb_t, cm_t = uitkomst(t["card_name"], t["number"], t["grade"], t.get("set"), t.get("year"))
        eb_g, cm_g = uitkomst(g.get("card_name"), g.get("number"), g.get("grade"), g.get("set_name"), g.get("year"))
        eb_q, cm_q = uitkomst(q.get("name"), q.get("number"), q.get("grade"), q.get("set_name"), q.get("year"))

        if cm_t: tot["cm_truth"] += 1
        cm_g_ok = bool(cm_t) and cm_g == cm_t
        cm_q_ok = bool(cm_t) and cm_q == cm_t
        eb_g_ok = eb_g == eb_t
        eb_q_ok = eb_q == eb_t
        tot["cm_g"] += cm_g_ok; tot["cm_q"] += cm_q_ok; tot["eb_g"] += eb_g_ok; tot["eb_q"] += eb_q_ok
        rijen.append((iid, t, g, q, eb_t, eb_g, eb_q, cm_t, cm_g, cm_q, cm_g_ok, cm_q_ok, eb_g_ok, eb_q_ok))
        print(f"  CM  G{'✓' if cm_g_ok else '✗'} Q{'✓' if cm_q_ok else '✗'} | eBay G{'✓' if eb_g_ok else '✗'} Q{'✓' if eb_q_ok else '✗'}  "
              f"{iid}  waarheid={t['card_name']} #{t['number']}")
        if not (cm_g_ok and cm_q_ok and eb_g_ok and eb_q_ok):
            print(f"        waarheid-eBay: {eb_t!r}  CM: {cm_t}")
            if not (cm_g_ok and eb_g_ok): print(f"        GEMINI  eBay: {eb_g!r}  CM: {cm_g}")
            if not (cm_q_ok and eb_q_ok): print(f"        QWEN    eBay: {eb_q!r}  CM: {cm_q}   (naam={q.get('name')!r} nr={q.get('number')!r})")

    n = len(rijen)
    print(f"\n=== CARDMARKET-URL goed (van {tot['cm_truth']} kaarten met een CM-match op de waarheid): "
          f"Gemini {tot['cm_g']} · Qwen {tot['cm_q']}")
    print(f"=== eBay-zoekzin exact gelijk aan waarheid ({n}): Gemini {tot['eb_g']} · Qwen {tot['eb_q']}")
    (GOLD / f"business_test_{'fullres' if FULLRES else '768'}_{'zonder' if ZONDER_TITEL else 'met'}_titel.json").write_text(
        json.dumps([{"item": r[0], "eb_t": r[4], "eb_g": r[5], "eb_q": r[6], "cm_t": r[7], "cm_g": r[8], "cm_q": r[9]}
                    for r in rijen], ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
