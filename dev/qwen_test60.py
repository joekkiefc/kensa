#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Test-60 — Tommy 9-9: fixes 1-4 op 60 ANDERE Pokémon-kaarten (niet de gouden set).

Set:      60 verse Pokémon-kaarten uit het schaduwlog (geen trainers, niet in de
          gouden set, full-res foto, Gemini-lezing aanwezig).
Qwen:     full-res, geen titel, prompt mét fix 2 (label-prefix niet uitspellen) en
          fix 3 (label_name = naamregel van het label letterlijk).
Fix 1:    set_code in het nummer-suffix zodat build_query 'm ziet (Qwen zet 'm apart).
Fix 4:    pokemon-woord uit label-tokens (split op non-letters) tegen de pokédex —
          fixt -HOLO / TM.MAG. / 'Dark Arbok'→Houndoom. Toegepast op Qwen, Gemini
          ÉN de waarheid (zelfde meetlat voor iedereen).
Waarheid: PSA-register (chunked, resumable, ≤PSA_MAX per run).
Uitkomst: eBay-zoekzin (build_query) + Cardmarket-URL (cm_lookup), per kaart wie mist.

  qwen_test60.py --bouw      selecteer set + Qwen-lezingen (cache) + uitkomsten
  qwen_test60.py --psa       volgende PSA-chunk (herhalen tot 60/60)
  qwen_test60.py --rapport   eindtabel
Read-only op live; schrijft alleen dev/qwen_test60/.
"""
from __future__ import annotations
import base64, importlib.util, json, os, re, sys, time, urllib.request
from pathlib import Path

DEV = Path(__file__).resolve().parent
KENSA = DEV.parent
sys.path.insert(0, str(KENSA)); sys.path.insert(0, str(DEV))
from llm_client import PHOTO_INTERPRET_PROMPT
from query_builder import build_query
from supabase_client import lookup as cm_lookup
from qwen_backtest import norm_grade, norm_number, cert_digits

OUT = DEV / "qwen_test60"; OUT.mkdir(exist_ok=True)
SET_F, READS_F, TRUTH_F = OUT / "set.json", OUT / "reads.json", OUT / "truth.json"
ENDPOINT = "http://100.125.116.37:1234/v1/chat/completions"
MODEL = "qwen2.5-vl-7b-instruct"
UA = {"User-Agent": "Mozilla/5.0"}
N = 60
PSA_MAX = int(os.environ.get("PSA_MAX", "10"))
PSA_PAUZE = float(os.environ.get("PSA_PAUZE", "15"))

POKEDEX_EN = {v.lower() for v in json.load(open(KENSA / "pokedex_ja_en.json")).values() if len(v) >= 3}
PREFIX = {"mega", "dark", "light", "shining", "radiant", "galarian", "alolan", "hisuian", "paldean"}
SUBTYPES = ["VMAX", "VSTAR", "GX", "EX", "V", "BREAK"]

# ---- fix 2 + 3 in de prompt: prefix-regel + label_name-veld ----------------
PROMPT_FIXED = PHOTO_INTERPRET_PROMPT.replace(
    '  "subtype":',
    '  "label_name": "<de NAAM-regel van het PSA-label LETTERLIJK zoals gedrukt, bv \'FA/JOLTEON V\' of \'TM.MAG.GROUDON-HOLO\' — kopieer, interpreteer niet>",\n  "subtype":',
).replace(
    "REGELS:",
    "REGELS:\n- FA/, SA/, RR/ vooraan op het label zijn AFKORTINGEN (Full Art, Special Art). NOOIT uitspellen tot een woord (dus nooit 'Fairy'); laat staan of weglaten.",
)
assert PROMPT_FIXED != PHOTO_INTERPRET_PROMPT


# ---- fix 4: pokemon-woord uit label-tokens ----------------------------------
def pokemon_uit(*teksten) -> str | None:
    for t in teksten:
        toks = [x.lower() for x in re.split(r"[^A-Za-z]+", t or "") if x]
        for i, tok in enumerate(toks):
            if tok in POKEDEX_EN and len(tok) >= 3:
                if i > 0 and toks[i - 1] in PREFIX:
                    return f"{toks[i-1]} {tok}"
                return tok
    return None


def subtype_uit(*teksten) -> str | None:
    for t in teksten:
        up = (t or "").upper()
        for s in SUBTYPES:
            if re.search(rf"\b{s}\b", up):
                return s
    return None


def schone_naam(pokemon: str | None, subtype: str | None) -> str | None:
    if not pokemon:
        return None
    naam = " ".join(w.capitalize() for w in pokemon.split())
    return f"{naam} {subtype}" if subtype else naam


SETCODE_RE = re.compile(r"^[A-Z]{1,3}\d{0,2}[A-Z]?(?:-P)?$")   # S-P, SV-P, SV1A, S8A-P, M2A, SV10, PM-P


RARITY = {"CSR", "SSR", "SAR", "SR", "AR", "UR", "HR", "CHR", "RR", "RRR", "SIR", "ACE", "IR", "PR", "MUR", "K", "U", "C", "R"}


def geldige_setcode(s) -> str | None:
    s = re.sub(r"[^A-Za-z0-9\-]", "", str(s or "")).upper()
    if not s or s in RARITY or not SETCODE_RE.match(s):
        return None
    return s


def schoon_nummer(number, set_code) -> str:
    """Fix 1 met filter: cijfers + alleen een ÉCHTE set-code als suffix (rommel eruit)."""
    raw = str(number or "").strip().lstrip("#")
    m = re.match(r"\d{1,4}", raw.split("/")[0])
    if not m:
        return ""
    num = m.group(0)
    suffix = raw.split("/", 1)[1] if "/" in raw else ""
    code = geldige_setcode(suffix) or geldige_setcode(set_code)
    return f"{num}/{code}" if code else num


def uitkomst(label_name, name, number, grade, set_code, set_name, jaar):
    """Fix 1 (gefilterd) + fix 4 toegepast → (eBay-zoekzin, CM-URL, pokemon, nummer)."""
    pokemon = pokemon_uit(name, label_name)
    naam = schone_naam(pokemon, subtype_uit(name, label_name))
    num = schoon_nummer(number, set_code)
    try:
        jaar_i = int(jaar) if jaar not in (None, "", "null") else None
    except Exception:
        jaar_i = None
    q = build_query(naam, num or None, str(grade) if grade else None, set_name, jaar_i) if naam else None
    url = None
    if pokemon and num:
        try:
            url = (cm_lookup(pokemon, num) or {}).get("url")
        except Exception as e:
            url = f"ERR {type(e).__name__}"
    return q, url, pokemon, num


# ---- stap 1: set + Qwen-lezingen ---------------------------------------------
def qwen(img: bytes) -> dict:
    user = "Analyseer deze PSA-slab foto.\nListing titel EN: (geen)\nListing titel JP: (geen)\n\nGeef JSON."
    b64 = base64.b64encode(img).decode()
    payload = {"model": MODEL, "messages": [
        {"role": "system", "content": PROMPT_FIXED},
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


def bouw():
    gold = {k["item_id"] for k in json.load(open(DEV / "qwen_goldenset/answers.json"))["kaarten"]}
    rows = [json.loads(l) for l in (DEV / "qwen_shadow/shadow_log.jsonl").read_text().splitlines() if l.strip()]
    gezien, kand = set(), []
    for r in reversed(rows):                                # nieuwste eerst
        iid = r.get("item_id")
        if r.get("status") != "ok" or iid in gold or iid in gezien:
            continue
        if not r["foto"].get("url") or not r["gemini"].get("card_name"):
            continue
        if not pokemon_uit(r["gemini"]["card_name"]):       # geen trainer/onbekend
            continue
        gezien.add(iid); kand.append(r)
    stap = max(1, len(kand) // N)
    gekozen = kand[::stap][:N]
    SET_F.write_text(json.dumps(gekozen, ensure_ascii=False, indent=1))
    print(f"set: {len(gekozen)} kaarten uit {len(kand)} kandidaten")

    reads = json.loads(READS_F.read_text()) if READS_F.exists() else {}
    for i, r in enumerate(gekozen, 1):
        iid = r["item_id"]
        if iid in reads:
            continue
        try:
            img = urllib.request.urlopen(urllib.request.Request(r["foto"]["url"], headers=UA), timeout=25).read()
            q = qwen(img)
        except Exception as e:
            q = {"_error": f"{type(e).__name__}: {e}"}
        reads[iid] = q
        READS_F.write_text(json.dumps(reads, ensure_ascii=False, indent=1))
        print(f"  {i:2d}/{len(gekozen)} {iid}: {q.get('label_name')!r} / {q.get('name')!r} #{q.get('number')} cert={q.get('cert')}")
    print("Qwen-lezingen klaar")


# ---- stap 2: PSA-waarheid (chunked) -----------------------------------------
def psa_chunk():
    spec = importlib.util.spec_from_file_location("psa_slab", "/home/pi/.openclaw/workspace/agents/webshop/psa-slab.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    gekozen = json.loads(SET_F.read_text()); reads = json.loads(READS_F.read_text())
    truth = json.loads(TRUTH_F.read_text()) if TRUTH_F.exists() else {}
    budget = PSA_MAX
    for r in gekozen:
        iid = r["item_id"]
        if iid in truth or budget <= 0:
            continue
        q, g = reads.get(iid, {}), r["gemini"]
        certs = []
        for c in (q.get("cert"), g.get("cert")):
            c = cert_digits(c)
            if re.fullmatch(r"\d{8,10}", c) and c not in certs:
                certs.append(c)
        res = None
        geblokkeerd = False
        for c in certs:
            if budget <= 0:
                break
            budget -= 1
            try:
                info, err = mod.scrape_psa_cert(c)
            except Exception as e:
                info, err = None, repr(e)
            time.sleep(PSA_PAUZE)
            if err and "429" in str(err):
                geblokkeerd = True            # PSA remt af: niks vastleggen, later opnieuw
                break
            if info and info.get("Cert Number") == c:
                nr = (info.get("Card Number") or "").strip()
                res = {"cert": c, "card_name": (info.get("Subject") or "").strip(), "number": nr,
                       "grade": re.sub(r"^.*?(\d+(?:\.\d+)?)\s*$", r"\1", info.get("Item Grade", "")),
                       "set": (info.get("Brand/Title") or "").strip(), "year": (info.get("Year") or "").strip(),
                       "bron": "qwen-cert" if c == cert_digits(q.get("cert")) else "gemini-cert"}
                break
            print(f"  PSA {c}: {str(err)[:60] if err else 'geen match'}")
        if geblokkeerd:
            print("PSA 429 — chunk gestopt, deze kaart komt volgende run terug")
            break
        truth[iid] = res or {"_fail": True}
        TRUTH_F.write_text(json.dumps(truth, ensure_ascii=False, indent=1))
        print(f"  {iid}: {res['card_name'] + ' #' + res['number'] if res else 'GEEN PSA-WAARHEID'}")
    n_ok = sum(1 for v in truth.values() if not v.get("_fail"))
    print(f"waarheid: {n_ok} ok / {len(truth)} geprobeerd / {len(gekozen)} totaal")


# ---- stap 3: rapport ----------------------------------------------------------
def rapport():
    gekozen = json.loads(SET_F.read_text()); reads = json.loads(READS_F.read_text())
    truth = json.loads(TRUTH_F.read_text()) if TRUTH_F.exists() else {}
    tot = dict(n=0, cm_t=0, cm_g=0, cm_q=0, eb_g=0, eb_q=0, cert_g=0, cert_q=0, nr_g=0, nr_q=0)
    missers = []
    for r in gekozen:
        iid = r["item_id"]; t = truth.get(iid)
        if not t or t.get("_fail"):
            continue
        q, g = reads.get(iid, {}), r["gemini"]
        tot["n"] += 1
        eb_t, cm_t, st_, _, _ = uitkomst5(t["card_name"], t["card_name"], t["number"], t["grade"], None, None, t.get("year"), psa_set=t.get("set"))
        eb_q, cm_q, sq, pq, nq = uitkomst5(q.get("label_name"), q.get("name"), q.get("number"), q.get("grade"), q.get("set_code"), q.get("set_name"), q.get("year"))
        eb_g, cm_g, sg, pg, ng = uitkomst5(None, g.get("card_name"), g.get("number"), g.get("grade"), None, g.get("set_name"), None)
        if cm_t: tot["cm_t"] += 1
        # eBay op KERN-inhoud (pokemon+subtype, nummer, grade) — de set-code is een
        # optioneel extra; PSA-waarheid heeft die nooit, dus letterlijk vergelijken is oneerlijk.
        def kern(ln, nm, nr, gr):
            return (pokemon_uit(ln, nm), subtype_uit(ln, nm), schoon_nummer(nr, None).split("/")[0], norm_grade(gr))
        k_t = kern(t["card_name"], t["card_name"], t["number"], t["grade"])
        k_q = kern(q.get("label_name"), q.get("name"), q.get("number"), q.get("grade"))
        k_g = kern(None, g.get("card_name"), g.get("number"), g.get("grade"))
        ok = {"cm_g": bool(cm_t) and cm_g == cm_t, "cm_q": bool(cm_t) and cm_q == cm_t,
              "eb_g": k_g == k_t, "eb_q": k_q == k_t,
              "cert_g": cert_digits(g.get("cert")) == t["cert"], "cert_q": cert_digits(q.get("cert")) == t["cert"],
              "nr_g": norm_number(ng).split("/")[0] == norm_number(t["number"]).split("/")[0],
              "nr_q": norm_number(nq).split("/")[0] == norm_number(t["number"]).split("/")[0]}
        for k, v in ok.items(): tot[k] += v
        if not (ok["cm_q"] and ok["eb_q"]) or not (ok["cm_g"] and ok["eb_g"]):
            missers.append((iid, t, ok, eb_t, cm_t, eb_g, cm_g, eb_q, cm_q, q, g))

    n = tot["n"]
    print(f"=== TEST-60 — {n} kaarten met PSA-waarheid (Pokémon only, fixes 1-4 op iedereen) ===")
    print(f"  {'':16s} {'Gemini':>8s} {'Qwen':>8s}")
    print(f"  {'Cardmarket-URL':16s} {tot['cm_g']:>5d}/{tot['cm_t']:<3d}{tot['cm_q']:>5d}/{tot['cm_t']}")
    print(f"  {'eBay-zoekzin':16s} {tot['eb_g']:>5d}/{n:<3d}{tot['eb_q']:>5d}/{n}")
    print(f"  {'nummer':16s} {tot['nr_g']:>5d}/{n:<3d}{tot['nr_q']:>5d}/{n}")
    print(f"  {'cert':16s} {tot['cert_g']:>5d}/{n:<3d}{tot['cert_q']:>5d}/{n}")
    print("\n--- kaarten waar iemand mist:")
    for iid, t, ok, eb_t, cm_t, eb_g, cm_g, eb_q, cm_q, q, g in missers:
        print(f"  {iid}  waarheid: {t['card_name']} #{t['number']}  → eBay {eb_t!r} · CM {'ja' if cm_t else 'geen'}")
        if not (ok["cm_g"] and ok["eb_g"]):
            print(f"      GEMINI {'CM✓' if ok['cm_g'] else 'CM✗'} {'eBay✓' if ok['eb_g'] else 'eBay✗'}  naam={g.get('card_name')!r} nr={g.get('number')!r} → {eb_g!r}")
        if not (ok["cm_q"] and ok["eb_q"]):
            print(f"      QWEN   {'CM✓' if ok['cm_q'] else 'CM✗'} {'eBay✓' if ok['eb_q'] else 'eBay✗'}  label={q.get('label_name')!r} naam={q.get('name')!r} nr={q.get('number')!r} → {eb_q!r}")




# ---- stap 4: visueel overzicht voor Tommy (foto + Qwen + Gemini + uitkomst) ----
def html():
    import io, html as H
    from PIL import Image
    gekozen = json.loads(SET_F.read_text()); reads = json.loads(READS_F.read_text())
    truth = json.loads(TRUTH_F.read_text()) if TRUTH_F.exists() else {}
    FOTOS = OUT / "fotos"; FOTOS.mkdir(exist_ok=True)

    def thumb_b64(url, iid):
        p = FOTOS / f"{iid}.jpg"
        try:
            if not p.exists():
                p.write_bytes(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25).read())
            im = Image.open(p); im.thumbnail((520, 520))
            if im.mode != "RGB": im = im.convert("RGB")
            buf = io.BytesIO(); im.save(buf, "JPEG", quality=80)
            return base64.b64encode(buf.getvalue()).decode()
        except Exception:
            return None

    def e(x): return H.escape("" if x is None else str(x))
    rows_html, agree_cm = [], 0
    for i, r in enumerate(gekozen, 1):
        iid = r["item_id"]; q = reads.get(iid, {}); g = r["gemini"]; t = truth.get(iid)
        eb_q, cm_q, sq, pq, nq = uitkomst5(q.get("label_name"), q.get("name"), q.get("number"), q.get("grade"), q.get("set_code"), q.get("set_name"), q.get("year"))
        eb_g, cm_g, sg, pg, ng = uitkomst5(None, g.get("card_name"), g.get("number"), g.get("grade"), None, g.get("set_name"), None)
        same_cm = (cm_q == cm_g) and cm_q is not None
        agree_cm += same_cm
        kleur = "#1f6f3f" if same_cm else ("#8a6d1a" if (cm_q is None and cm_g is None) else "#8a2b2b")
        b64 = thumb_b64(r["foto"]["url"], iid)
        img = f'<img src="data:image/jpeg;base64,{b64}" style="max-width:260px;max-height:340px;border-radius:6px">' if b64 else "<i>geen foto</i>"
        def kolom(titel, naam, extra, nr, gr, cert, eb, cm, pk):
            cm_txt = f'<a href="{e(cm)}" target="_blank" rel="noopener noreferrer">{e(cm.split("/Singles/")[-1][:60]) if cm and "Singles" in cm else e(cm)}</a>' if cm else "<b style='color:#c33'>geen match</b>"
            return (f"<div style='flex:1;min-width:230px'><b>{titel}</b><br>"
                    f"naam: <b>{e(naam)}</b>{('<br><small>label: ' + e(extra) + '</small>') if extra else ''}<br>"
                    f"nr: {e(nr)} · grade: {e(gr)} · cert: {e(cert)}<br>"
                    f"→ pokémon: <b>{e(pk)}</b><br>→ eBay: <code>{e(eb)}</code><br>→ CM: {cm_txt}</div>")
        truth_html = ""
        if t and not t.get("_fail"):
            truth_html = f"<div style='margin-top:6px;padding:6px;background:#eef;border-radius:6px'><b>PSA-waarheid:</b> {e(t['card_name'])} #{e(t['number'])} grade {e(t['grade'])} cert {e(t['cert'])}</div>"
        rows_html.append(
            f"<div style='display:flex;gap:16px;padding:12px;border-left:6px solid {kleur};margin:10px 0;background:#fff;border-radius:8px;flex-wrap:wrap'>"
            f"<div style='min-width:270px'><small>#{i} · {e(iid)} · {e(r['foto'].get('px'))}</small><br>{img}</div>"
            f"<div style='flex:2;display:flex;gap:16px;flex-wrap:wrap'>"
            + kolom("QWEN (fixes 1-4)", q.get("name"), q.get("label_name"), q.get("number"), q.get("grade"), q.get("cert"), eb_q, cm_q, pq)
            + kolom("GEMINI (live)", g.get("card_name"), None, g.get("number"), g.get("grade"), g.get("cert"), eb_g, cm_g, pg)
            + f"</div><div style='width:100%'>{truth_html}</div></div>")
    n = len(gekozen)
    doc = (f"<!doctype html><meta charset='utf-8'><title>Test-60 Qwen vs Gemini</title>"
           f"<body style='font-family:system-ui;background:#f3f3f3;margin:0;padding:16px;color:#111'>"
           f"<h2>Test-60 — Qwen (fixes 1-4, full-res, geen titel) vs Gemini (live) — {n} Pokémon-kaarten</h2>"
           f"<p>Groene rand = zelfde Cardmarket-URL · rood = verschillende URL · geel = beide geen match. "
           f"Zelfde CM-URL: <b>{agree_cm}/{n}</b>. PSA-waarheid bij {sum(1 for v in truth.values() if not v.get('_fail'))} kaarten.</p>"
           + "".join(rows_html) + "</body>")
    (OUT / "rapport.html").write_text(doc)
    print(f"rapport.html: {n} kaarten, {len(doc)//1024} KB, zelfde CM-URL {agree_cm}/{n}")




# ---- stap 5: contact-sheets (PNG) — fallback als portal niet werkt --------------
def sheets():
    from PIL import Image, ImageDraw, ImageFont
    gekozen = json.loads(SET_F.read_text()); reads = json.loads(READS_F.read_text())
    truth = json.loads(TRUTH_F.read_text()) if TRUTH_F.exists() else {}
    FOTOS = OUT / "fotos"; FOTOS.mkdir(exist_ok=True)
    try:
        F = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 17)
        FB = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 19)
    except Exception:
        F = FB = ImageFont.load_default()
    PER, W, RH, TH = 10, 1500, 330, 300
    paden = []
    for s in range(0, len(gekozen), PER):
        chunk = gekozen[s:s + PER]
        im = Image.new("RGB", (W, RH * len(chunk) + 50), "#f3f3f3"); d = ImageDraw.Draw(im)
        d.text((16, 12), f"Test-60 — kaarten {s+1}-{s+len(chunk)} — QWEN (fixes 1-4, full-res) vs GEMINI (live)", fill="#111", font=FB)
        for j, r in enumerate(chunk):
            y0 = 50 + j * RH; iid = r["item_id"]; q = reads.get(iid, {}); g = r["gemini"]; t = truth.get(iid)
            eb_q, cm_q, sq, pq, nq = uitkomst5(q.get("label_name"), q.get("name"), q.get("number"), q.get("grade"), q.get("set_code"), q.get("set_name"), q.get("year"))
            eb_g, cm_g, sg, pg, ng = uitkomst5(None, g.get("card_name"), g.get("number"), g.get("grade"), None, g.get("set_name"), None)
            same = cm_q == cm_g and cm_q is not None
            kleur = "#2e8b57" if same else ("#c9a227" if (cm_q is None and cm_g is None) else "#c0392b")
            d.rectangle([8, y0 + 4, W - 8, y0 + RH - 6], fill="#ffffff", outline=kleur, width=5)
            p = FOTOS / f"{iid}.jpg"
            try:
                if not p.exists():
                    p.write_bytes(urllib.request.urlopen(urllib.request.Request(r["foto"]["url"], headers=UA), timeout=25).read())
                ph = Image.open(p).convert("RGB"); ph.thumbnail((240, TH)); im.paste(ph, (20, y0 + 14))
            except Exception:
                d.text((30, y0 + 120), "geen foto", fill="#900", font=F)
            def slug(cm, st="geen"): return cm.split("/Singles/")[-1].split("?")[0][:42] if cm and "Singles" in cm else ({"afgekeurd": "AFGEKEURD: set klopt niet", "geen": "geen kandidaat", "ongecontroleerd": "?"}.get(st, "geen match") if not cm else cm[:42])
            def blok(x, titel, naam, label, nr, gr, cert, pk, eb, cm, st="geen"):
                d.text((x, y0 + 14), titel, fill="#111", font=FB)
                regels = [f"naam: {naam}", f"label: {label}" if label else None, f"nr {nr} · grade {gr} · cert {cert}",
                          f"→ pokémon: {pk}", f"→ eBay: {eb}", f"→ CM: {slug(cm, st)}" + (" ✓set" if st == "ok" else "")]
                yy = y0 + 44
                for rg in regels:
                    if rg is None: continue
                    d.text((x, yy), str(rg)[:58], fill="#c0392b" if ("CM: geen" in rg or "AFGEKEURD" in rg) else "#222", font=F); yy += 26
            blok(285, f"#{s+j+1} QWEN", q.get("name"), q.get("label_name"), q.get("number"), q.get("grade"), q.get("cert"), pq, eb_q, cm_q, sq)
            blok(900, "GEMINI", g.get("card_name"), None, g.get("number"), g.get("grade"), g.get("cert"), pg, eb_g, cm_g, sg)
            if t and not t.get("_fail"):
                d.text((285, y0 + RH - 34), f"PSA-waarheid: {t['card_name']} #{t['number']} grade {t['grade']}", fill="#1a3d8f", font=FB)
        out = OUT / f"sheet_{s//PER+1}.png"; im.save(out, optimize=True); paden.append(out)
        print(out)
    return paden




# ---- fix 5 (Tommy): set moet KLOPPEN, geen tiebreak maar harde check -----------
import supabase_client as _sc

_GENERIEK = {"pokemon", "japanese", "jp", "japan", "en", "english", "card", "game", "the", "of", "and",
             "2019", "2020", "2021", "2022", "2023", "2024", "2025", "2026"}


# label-code → catalogus set_number (catalogus schrijft S-P als 'swshp', SM-P als 'smp')
_ALIAS = {"sp": "swshp", "svp": "svp", "smp": "smp", "xyp": "xyp", "mp": "mp", "bwp": "bwp", "dpp": "dpp"}


def norm_set(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def setcode_uit_tekst(tekst) -> str | None:
    """PSA-tekst zoals 'POKEMON JAPANESE SV PROMO' → 'svp'; anders None."""
    m = re.search(r"\b(SV|SM|XY|BW|DP|S|M)\s*PROMO", str(tekst or "").upper())
    return f"{m.group(1).lower()}p" if m else None


def cm_strict(pokemon, num, set_code=None, set_tekst=None):
    """Zelfde kandidaten als cm_lookup, maar de set MOET passen.
    Return (url, status): status ∈ ok / afgekeurd (kandidaat maar set klopt niet) / geen / ongecontroleerd."""
    if not pokemon or not num:
        return None, "geen"
    base, headers = _sc._config()
    raw = str(num).split("/")[0].lstrip("#")
    varianten = [raw] + ([raw.lstrip("0") or "0"] if raw.lstrip("0") != raw else [])
    rows = []
    for v in varianten:
        rows = _sc._fetch(base, headers, pokemon.lower(), v)
        if rows: break
    picks = [r for r in rows if (r.get("language") or "").lower() == "japanese" and r.get("mint")]
    if not picks:
        return None, "geen"
    code = norm_set(set_code) if set_code else None
    toks = {t for t in re.split(r"[^a-z0-9]+", str(set_tekst or "").lower()) if t} - _GENERIEK
    if not code and not toks:
        return picks[0]["url"], "ongecontroleerd"
    aanvaard = set()
    if code:
        aanvaard.add(code)
        aanvaard.add(_ALIAS.get(code, code))
        m = re.match(r"^(sv|sm|xy|bw|dp|s|m)\d*[a-z]?p$", code)   # promo-subset → familie
        if m:
            aanvaard.add(_ALIAS.get(m.group(1) + "p", m.group(1) + "p"))
    ok = [r for r in picks if norm_set(r.get("set_number")) in aanvaard
          or (toks and _sc._score_pick(r, toks) > 0)]
    return (ok[0]["url"], "ok") if ok else (None, "afgekeurd")


def uitkomst5(label_name, name, number, grade, set_code, set_name, jaar, psa_set=None):
    """Als uitkomst(), maar CM via cm_strict (fix 5). → (eBay, CM-url, status, pokemon, num)."""
    pokemon = pokemon_uit(name, label_name)
    naam = schone_naam(pokemon, subtype_uit(name, label_name))
    num = schoon_nummer(number, set_code)
    try: jaar_i = int(jaar) if jaar not in (None, "", "null") else None
    except Exception: jaar_i = None
    q = build_query(naam, num or None, str(grade) if grade else None, set_name, jaar_i) if naam else None
    code = geldige_setcode(num.split("/", 1)[1]) if "/" in num else geldige_setcode(set_code)
    if not code and psa_set:
        code = setcode_uit_tekst(psa_set)
    url, status = cm_strict(pokemon, num, code, set_name or psa_set)
    return q, url, status, pokemon, num


def rapport5():
    gekozen = json.loads(SET_F.read_text()); reads = json.loads(READS_F.read_text())
    truth = json.loads(TRUTH_F.read_text()) if TRUTH_F.exists() else {}
    tel = {"q_ok": 0, "g_ok": 0, "q_afgekeurd": 0, "g_afgekeurd": 0, "q_geen": 0, "g_geen": 0, "q_onc": 0, "g_onc": 0,
           "same": 0, "q_oud_maar_nu_weg": 0, "g_oud_maar_nu_weg": 0}
    pik = {"q_afgekeurd": 0, "g_afgekeurd": 0, "n": 0}
    regels = []
    for r in gekozen:
        iid = r["item_id"]; q = reads.get(iid, {}); g = r["gemini"]
        _, cm_q_oud, _, _ = uitkomst(q.get("label_name"), q.get("name"), q.get("number"), q.get("grade"), q.get("set_code"), q.get("set_name"), q.get("year"))
        _, cm_g_oud, _, _ = uitkomst(None, g.get("card_name"), g.get("number"), g.get("grade"), None, g.get("set_name"), None)
        eb_q, cm_q, sq, pq, nq = uitkomst5(q.get("label_name"), q.get("name"), q.get("number"), q.get("grade"), q.get("set_code"), q.get("set_name"), q.get("year"))
        eb_g, cm_g, sg, pg, ng = uitkomst5(None, g.get("card_name"), g.get("number"), g.get("grade"), None, g.get("set_name"), None)
        tel[f"q_{ {'ok':'ok','afgekeurd':'afgekeurd','geen':'geen','ongecontroleerd':'onc'}[sq] }"] += 1
        tel[f"g_{ {'ok':'ok','afgekeurd':'afgekeurd','geen':'geen','ongecontroleerd':'onc'}[sg] }"] += 1
        tel["same"] += (cm_q == cm_g and cm_q is not None)
        tel["q_oud_maar_nu_weg"] += (cm_q_oud is not None and cm_q is None)
        tel["g_oud_maar_nu_weg"] += (cm_g_oud is not None and cm_g is None)
        if pq == "pikachu" or pg == "pikachu":
            pik["n"] += 1; pik["q_afgekeurd"] += (sq == "afgekeurd"); pik["g_afgekeurd"] += (sg == "afgekeurd")
        if sq == "afgekeurd" or sg == "afgekeurd":
            regels.append(f"  {iid}  {pq or pg} #{nq or ng}  Qwen[{sq}] set={q.get('set_code')!r}  Gemini[{sg}] nr={g.get('number')!r} set={g.get('set_name')!r}  oud-URL={ (cm_q_oud or cm_g_oud or '')[45:95] }")
    n = len(gekozen)
    print(f"=== FIX 5 (set moet kloppen) op {n} kaarten ===")
    print(f"  {'':22s}{'Qwen':>8s}{'Gemini':>8s}")
    for k, lab in (("ok", "CM-match, set klopt"), ("afgekeurd", "AFGEKEURD (set fout)"), ("geen", "geen kandidaat"), ("onc", "ongecontroleerd (geen set-info)")):
        print(f"  {lab:22s}{tel['q_'+k]:>8d}{tel['g_'+k]:>8d}")
    print(f"  {'was URL, nu weg':22s}{tel['q_oud_maar_nu_weg']:>8d}{tel['g_oud_maar_nu_weg']:>8d}   ← dit waren VALSE matches")
    print(f"  zelfde URL Qwen=Gemini: {tel['same']}/{n}")
    print(f"  Pikachu ({pik['n']}): afgekeurd Qwen {pik['q_afgekeurd']} · Gemini {pik['g_afgekeurd']}")
    print("\n--- afgekeurde matches (set klopte niet):")
    for l in regels: print(l)


if __name__ == "__main__":
    if "--bouw" in sys.argv: bouw()
    elif "--psa" in sys.argv: psa_chunk()
    elif "--rapport" in sys.argv: rapport()
    elif "--html" in sys.argv: html()
    elif "--sheets" in sys.argv: sheets()
    elif "--fix5" in sys.argv: rapport5()
    else: print(__doc__)
