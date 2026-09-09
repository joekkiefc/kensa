#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Fase 1 — antwoordenboekje (gouden set) voor de Qwen-OCR backwards test.

Bouwt dev/qwen_goldenset/answers.json:
  - 30 kaarten: foto lokaal + waarheid uit het OFFICIELE PSA-register
    (scrape_psa_cert uit agents/webshop/psa-slab.py — alleen de functie, nooit main()).
  - 5 valstrikken: foto's waar de pijplijn geen PSA-label vond (found_psa=false);
    juiste antwoord daar = {"no_label": true}. Worden vóór bevriezing handmatig geschouwd.
  - gemini_ref per kaart: wat de pijplijn (Gemini) er destijds van maakte, als referentie.

Read-only t.o.v. live: leest alleen Supabase + PSA, schrijft alleen in dev/qwen_goldenset/.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

KENSA = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KENSA))
from storage_supabase import _http  # noqa: E402

OUTDIR = Path(__file__).resolve().parent / "qwen_goldenset"
PHOTOS = OUTDIR / "photos"
LOG = OUTDIR / "build.log"
UA = {"User-Agent": "Mozilla/5.0"}

DOEL_KAARTEN = 30
DOEL_VALSTRIK = 5
# PSA remt af na ~10 vlotte opvragingen (429). Dus: kleine chunks, lange pauzes,
# afkoeltijd tussen chunks; dit script is hervat-baar via answers.partial.json.
MAX_PSA_POGINGEN = int(os.environ.get("GS_MAX_PSA", "10"))
PSA_PAUZE_S = float(os.environ.get("GS_PAUZE", "20"))


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    with LOG.open("a") as f:
        f.write(line + "\n")


def load_psa_scraper():
    spec = importlib.util.spec_from_file_location(
        "psa_slab", "/home/pi/.openclaw/workspace/agents/webshop/psa-slab.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.scrape_psa_cert


def parse_grade(item_grade: str) -> str | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*$", (item_grade or "").strip())
    return m.group(1) if m else None


def fetch_photo_urls(item_ids: list[str]) -> dict[str, str]:
    """item_id -> url_original van de eerste foto (photo_index laagst)."""
    out: dict[str, str] = {}
    for i in range(0, len(item_ids), 60):
        chunk = item_ids[i:i + 60]
        rows = _http.get("photos", "kensa", [
            ("select", "item_id,photo_index,url_original"),
            ("item_id", f"in.({','.join(chunk)})"),
            ("order", "item_id.asc,photo_index.asc"),
            ("limit", "500"),
        ])
        for r in rows:
            out.setdefault(r["item_id"], r["url_original"])
    return out


def download(url: str, dest: Path) -> bool:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = resp.read()
        if len(data) < 8000:
            return False
        dest.write_bytes(data)
        return True
    except Exception as e:
        log(f"  download fail {type(e).__name__}: {url[:80]}")
        return False


def gemini_refs(item_ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(item_ids), 40):
        chunk = item_ids[i:i + 40]
        rows = _http.get("analysis", "kensa", [
            ("select", "item_id,result_json,created_at"),
            ("trap", "eq.slab_ocr"),
            ("item_id", f"in.({','.join(chunk)})"),
            ("order", "created_at.desc"),
            ("limit", "200"),
        ])
        for r in rows:
            if r["item_id"] not in out:
                rj = r.get("result_json") or {}
                out[r["item_id"]] = {k: rj.get(k) for k in
                                     ("cert", "grade", "card_name", "number", "set_name", "year", "_source")}
    return out


def main() -> int:
    OUTDIR.mkdir(exist_ok=True)
    PHOTOS.mkdir(exist_ok=True)
    log(f"=== gouden-set build start (max {MAX_PSA_POGINGEN} PSA-pogingen, pauze {PSA_PAUZE_S:.0f}s) ===")

    kaarten: list[dict] = []
    partial = OUTDIR / "answers.partial.json"
    if partial.exists():
        kaarten = json.loads(partial.read_text())
        log(f"resume: {len(kaarten)} eerder geverifieerde kaarten ingeladen")
    klaar_certs = {k["waarheid"]["cert"] for k in kaarten}
    klaar_items = {k["item_id"] for k in kaarten}

    # --- kandidaten: recente cert-waarnemingen (dedupe op cert), mix m*/z* ---
    rows = _http.get("cert_sightings", "kensa", [
        ("select", "cert,item_id,seen_at"),
        ("order", "seen_at.desc"),
        ("limit", "400"),
    ])
    seen_cert: set[str] = set()
    m_cands, z_cands = [], []
    for r in rows:
        cert = (r.get("cert") or "").strip()
        iid = r.get("item_id") or ""
        if not re.fullmatch(r"\d{8,10}", cert) or cert in seen_cert:
            continue
        seen_cert.add(cert)
        (m_cands if iid.startswith("m") else z_cands).append({"cert": cert, "item_id": iid, "moeilijk": False})
    log(f"kandidaten: {len(m_cands)} mercari, {len(z_cands)} buyee/yahoo (uniek cert)")

    # --- extra: 'moeilijke' foto's = waar de pijplijn naar het Vision-vangnet moest ---
    hard_rows = _http.get("analysis", "kensa", [
        ("select", "item_id,result_json,created_at"),
        ("trap", "eq.slab_ocr"),
        ("result_json->>_source", "eq.vision_fallback"),
        ("order", "created_at.desc"),
        ("limit", "20"),
    ])
    hard = []
    for r in hard_rows:
        cert = ((r.get("result_json") or {}).get("cert") or "")
        if re.fullmatch(r"\d{8,10}", str(cert)) and cert not in seen_cert:
            seen_cert.add(str(cert))
            hard.append({"cert": str(cert), "item_id": r["item_id"], "moeilijk": True})
    log(f"moeilijke kandidaten (vision_fallback): {len(hard)}")

    # volgorde: eerst 6 moeilijke, daarna om-en-om mercari/buyee
    volgorde = hard[:6]
    mi = zi = 0
    while len(volgorde) < MAX_PSA_POGINGEN + 10 and (mi < len(m_cands) or zi < len(z_cands)):
        if mi < len(m_cands):
            volgorde.append(m_cands[mi]); mi += 1
        if zi < len(z_cands):
            volgorde.append(z_cands[zi]); zi += 1

    foto_urls = fetch_photo_urls([c["item_id"] for c in volgorde])

    scrape = load_psa_scraper()
    pogingen = 0
    for cand in volgorde:
        if len(kaarten) >= DOEL_KAARTEN or pogingen >= MAX_PSA_POGINGEN:
            break
        iid, cert = cand["item_id"], cand["cert"]
        if cert in klaar_certs or iid in klaar_items:
            continue
        url = foto_urls.get(iid)
        if not url:
            continue
        dest = PHOTOS / f"{iid}.jpg"
        if not dest.exists() and not download(url, dest):
            continue
        pogingen += 1
        try:
            info, err = scrape(cert)
        except Exception as e:
            info, err = None, repr(e)
        time.sleep(PSA_PAUZE_S)
        if err or not info:
            log(f"  PSA fail {cert}: {str(err)[:80]}")
            continue
        grade = parse_grade(info.get("Item Grade", ""))
        subject = (info.get("Subject") or "").strip()
        number = (info.get("Card Number") or "").strip()
        if info.get("Cert Number") != cert or not (grade and subject and number):
            log(f"  PSA onvolledig/mismatch {cert} -> overslaan")
            continue
        kaarten.append({
            "item_id": iid, "photo": f"photos/{iid}.jpg", "photo_url": url,
            "moeilijk": cand["moeilijk"],
            "waarheid": {
                "cert": cert, "grade": grade, "card_name": subject, "number": number,
                "year": (info.get("Year") or "").strip(),
                "set": (info.get("Brand/Title") or "").strip(),
                "variety": (info.get("Variety/Pedigree") or "").strip(),
            },
        })
        log(f"  OK [{len(kaarten)}/{DOEL_KAARTEN}] {cert} = {subject} #{number} g{grade} ({'moeilijk' if cand['moeilijk'] else iid[0]})")
        (OUTDIR / "answers.partial.json").write_text(json.dumps(kaarten, ensure_ascii=False))

    # --- valstrikken: pijplijn vond geen PSA-label (found_psa=false) ---
    trap_rows = _http.get("analysis", "kensa", [
        ("select", "item_id,result_json,created_at"),
        ("trap", "eq.slab_ocr"),
        ("result_json->>found_psa", "eq.false"),
        ("order", "created_at.desc"),
        ("limit", "30"),
    ])
    trap_ids = [r["item_id"] for r in trap_rows]
    trap_urls = fetch_photo_urls(trap_ids)
    valstrikken = []
    for iid in trap_ids:
        if len(valstrikken) >= DOEL_VALSTRIK:
            break
        url = trap_urls.get(iid)
        if not url:
            continue
        dest = PHOTOS / f"trap_{iid}.jpg"
        if not dest.exists() and not download(url, dest):
            continue
        valstrikken.append({"item_id": iid, "photo": f"photos/trap_{iid}.jpg", "photo_url": url,
                            "waarheid": {"no_label": True},
                            "geschouwd": False})
        log(f"  VALSTRIK [{len(valstrikken)}/{DOEL_VALSTRIK}] {iid}")

    refs = gemini_refs([k["item_id"] for k in kaarten])
    for k in kaarten:
        k["gemini_ref"] = refs.get(k["item_id"])

    out = {
        "gebouwd_op": time.strftime("%Y-%m-%d %H:%M:%S"),
        "psa_pogingen": pogingen,
        "kaarten": kaarten,
        "valstrikken": valstrikken,
        "status": ("CONCEPT — valstrikken schouwen, dan bevriezen" if len(kaarten) >= DOEL_KAARTEN
                   else f"DEELS — {len(kaarten)}/{DOEL_KAARTEN} kaarten, vervolg-chunk nodig (PSA-afkoeltijd)"),
    }
    (OUTDIR / "answers.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    n_hard = sum(1 for k in kaarten if k["moeilijk"])
    n_m = sum(1 for k in kaarten if k["item_id"].startswith("m"))
    log(f"=== KLAAR: {len(kaarten)} kaarten ({n_m} mercari, {len(kaarten)-n_m-n_hard if False else len(kaarten)-n_m} overig, {n_hard} moeilijk) + {len(valstrikken)} valstrikken, {pogingen} PSA-pogingen ===")
    return 0 if len(kaarten) >= DOEL_KAARTEN and len(valstrikken) >= DOEL_VALSTRIK else 1


if __name__ == "__main__":
    sys.exit(main())
