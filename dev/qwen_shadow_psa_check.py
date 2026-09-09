#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Fase 3 — absolute-waarheid-arm: het PSA-register als scheidsrechter.

Tommy (9 sept): niet alleen Qwen-vs-Gemini vergelijken, maar ook tegen de
absolute waarheid die we écht kunnen verifiëren. Dit script pakt uit het
schaduwlog de gevallen die een oordeel nodig hebben en kijkt ze na bij PSA
(psacard.com, via scrape_psa_cert — alleen de functie, nooit main()).

Selectie per run (budget PSA_MAX lookups, PSA-429-les: chunks klein, pauze lang):
  1. cert-verschillen  — Qwen en Gemini lazen een ander cert: eerst Qwens cert,
     bij budget ook Gemini's cert. Wie z'n cert bestaat én bij de kaart past, had gelijk.
  2. qwen-extra        — Gemini las niks, Qwen een plausibel 8-10-cijferig cert.
  3. controle          — steekproef op rijen waar beide het EENS zijn (vangt
     'allebei fout' — het hele punt van absolute waarheid), max 3 per run.

Schrijft dev/qwen_shadow/psa_checks.jsonl (append). --rapport telt de oordelen.
Read-only op live; raakt alleen dev/qwen_shadow/.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEV = Path(__file__).resolve().parent
KENSA = DEV.parent
sys.path.insert(0, str(KENSA))
sys.path.insert(0, str(DEV))

from check_title import _extract_slab_pokemon_en  # noqa: E402
from qwen_backtest import norm_grade, norm_number  # noqa: E402

OUT = DEV / "qwen_shadow"
LOG_F = OUT / "shadow_log.jsonl"
PSA_F = OUT / "psa_checks.jsonl"

PSA_MAX = int(os.environ.get("PSA_MAX", "10"))
PSA_PAUZE_S = float(os.environ.get("PSA_PAUZE", "20"))
CONTROLE_PER_RUN = 3


def load_psa_scraper():
    spec = importlib.util.spec_from_file_location(
        "psa_slab", "/home/pi/.openclaw/workspace/agents/webshop/psa-slab.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.scrape_psa_cert


def parse_grade(item_grade: str) -> str | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*$", (item_grade or "").strip())
    return m.group(1) if m else None


def is_cert(s) -> bool:
    return bool(re.fullmatch(r"\d{8,10}", str(s or "")))


def match_kant(psa: dict, velden: dict) -> dict:
    """Past de PSA-kaart bij wat deze kant (Qwen of Gemini) las?"""
    psa_nr, kant_nr = norm_number(psa.get("number")), norm_number(velden.get("number"))
    nr_ok = bool(psa_nr) and (psa_nr == kant_nr or psa_nr.split("/")[0] == kant_nr.split("/")[0])
    gr_ok = norm_grade(psa.get("grade")) == norm_grade(velden.get("grade"))
    psa_pk = _extract_slab_pokemon_en(psa.get("card_name") or "")
    kant_pk = _extract_slab_pokemon_en(velden.get("naam") or "")
    pk_ok = bool(psa_pk) and psa_pk == kant_pk
    return {"nummer": nr_ok, "grade": gr_ok, "pokemon": pk_ok,
            "past": nr_ok and gr_ok}


def psa_lookup(scrape, cert: str) -> tuple[dict | None, str | None]:
    try:
        info, err = scrape(cert)
    except Exception as e:
        info, err = None, repr(e)
    time.sleep(PSA_PAUZE_S)
    if err or not info:
        return None, str(err)[:120] if err else "leeg"
    if info.get("Cert Number") != cert:
        return None, "cert-mismatch in PSA-antwoord"
    return {
        "card_name": (info.get("Subject") or "").strip(),
        "number": (info.get("Card Number") or "").strip(),
        "grade": parse_grade(info.get("Item Grade", "")),
        "year": (info.get("Year") or "").strip(),
        "set": (info.get("Brand/Title") or "").strip(),
    }, None


def gedaan_certs() -> set[str]:
    if not PSA_F.exists():
        return set()
    out = set()
    for l in PSA_F.read_text().splitlines():
        if l.strip():
            out.add(json.loads(l).get("cert"))
    return out


def gedaan_url_items() -> set[str]:
    """item_ids die al een URL-verschil-oordeel hebben (1 rij per kaart)."""
    if not PSA_F.exists():
        return set()
    out = set()
    for l in PSA_F.read_text().splitlines():
        if l.strip():
            r = json.loads(l)
            if r.get("categorie") == "url_verschil":
                out.add(r.get("item_id"))
    return out


def url_soort(r: dict) -> str | None:
    """Fase 3b: Cardmarket-uitkomst per kant staat in r['cm']. Verschil = de hoofdlat."""
    cm = r.get("cm") or {}
    if not cm or "fout" in cm:
        return None
    q, g = cm.get("qwen_url"), cm.get("gemini_url")
    if q and g:
        return None if q == g else "anders"
    if q:
        return "alleen_qwen"
    if g:
        return "alleen_gemini"
    return None


def kandidaten() -> tuple[list, list, list, list]:
    rows = [json.loads(l) for l in LOG_F.read_text().splitlines() if l.strip()]
    ok = [r for r in rows if r.get("status") == "ok"]
    differ, extra, agree = [], [], []
    for r in ok:
        v = r["vergelijk"]
        g_c, q_c = str(r["gemini"].get("cert") or ""), str(r["qwen"].get("cert") or "")
        if v["gemini_cert"] and is_cert(q_c) and g_c != q_c:
            differ.append(r)
        elif not v["gemini_cert"] and is_cert(q_c):
            extra.append(r)
        elif v["cert_agree"]:
            agree.append(r)
    # URL-verschillen (Tommy 9-9 15:50: "hoe zit het met de url verschil?") — ook de
    # vs_vision-rijen tellen mee, daar is de CM-uitkomst net zo goed bepaald.
    url_diff = [r for r in rows if r.get("status") in ("ok", "vs_vision") and url_soort(r)]
    volgorde = {"anders": 0, "alleen_qwen": 1, "alleen_gemini": 2}
    url_diff.sort(key=lambda r: volgorde[url_soort(r)])
    return url_diff, differ, extra, agree


def kies_cert(r: dict) -> str | None:
    """Eén PSA-lookup per kaart: cert waar beide het over eens zijn, anders Qwen, anders Gemini.
    Komma-lijst (multi-slab) → eerste cert."""
    q_c = str(r["qwen"].get("cert") or "").split(",")[0].strip()
    g_c = str(r["gemini"].get("cert") or "").strip()
    if is_cert(q_c) and q_c == g_c:
        return q_c
    if is_cert(q_c):
        return q_c
    if is_cert(g_c):
        return g_c
    return None


def waarheid_url(psa: dict) -> tuple[str | None, str]:
    """PSA-velden → Cardmarket-URL via dezelfde trechter als test-60 (uitkomst5, set-check)."""
    from qwen_test60 import uitkomst5
    _, url, status, _, _ = uitkomst5(psa["card_name"], psa["card_name"], psa["number"], psa["grade"],
                                     None, None, psa.get("year"), psa_set=psa.get("set"))
    return url, status


def url_oordeel(kant_url: str | None, w_url: str | None, w_status: str) -> str:
    if w_url and w_status == "ok":
        if not kant_url:
            return "gemist"
        return "goed" if kant_url == w_url else "FOUT"
    if w_url:                       # waarheid-URL zonder set-bevestiging: niet hard genoeg om FOUT te roepen
        if not kant_url:
            return "gemist?"
        return "goed" if kant_url == w_url else "onbepaald"
    return "geen_url_beide" if not kant_url else "onbepaald"


def velden_van(r: dict, kant: str) -> dict:
    b = r[kant]
    return {"cert": b.get("cert"), "grade": b.get("grade"),
            "number": b.get("number"),
            "naam": b.get("name") if kant == "qwen" else b.get("card_name")}


def run() -> int:
    if not LOG_F.exists():
        print("nog geen schaduwlog")
        return 1
    url_diff, differ, extra, agree = kandidaten()
    klaar = gedaan_certs()
    klaar_url = gedaan_url_items()
    scrape = load_psa_scraper()
    budget = PSA_MAX
    nieuw = 0

    def check_url(r):
        """Hoofdlat: wie z'n Cardmarket-URL klopt volgens PSA. 1 lookup per kaart."""
        nonlocal budget, nieuw
        if r["item_id"] in klaar_url or budget <= 0:
            return
        soort = url_soort(r)
        cm = r["cm"]
        cert = kies_cert(r)
        rij = {"ts": datetime.now(timezone.utc).isoformat(), "item_id": r["item_id"],
               "categorie": "url_verschil", "kant": soort, "soort": soort, "cert": cert,
               "qwen_url": cm.get("qwen_url"), "gemini_url": cm.get("gemini_url"),
               "qwen_status": cm.get("qwen_status"), "gemini_status": cm.get("gemini_status")}
        if not cert:
            rij["psa"] = None
            rij["oordeel"] = "geen_cert_om_op_te_zoeken"
            rij["oordeel_qwen"] = rij["oordeel_gemini"] = "onbepaald"
        else:
            budget -= 1
            psa, err = psa_lookup(scrape, cert)
            if psa is None:
                rij["psa"] = None
                rij["oordeel"] = f"psa_fail: {err}" if "404" not in str(err) else "cert_bestaat_niet"
                rij["oordeel_qwen"] = rij["oordeel_gemini"] = "onbepaald"
            else:
                w_url, w_status = waarheid_url(psa)
                rij["psa"] = psa
                rij["waarheid_url"] = w_url
                rij["waarheid_status"] = w_status
                rij["oordeel_qwen"] = url_oordeel(cm.get("qwen_url"), w_url, w_status)
                rij["oordeel_gemini"] = url_oordeel(cm.get("gemini_url"), w_url, w_status)
                rij["oordeel"] = f"qwen={rij['oordeel_qwen']} gemini={rij['oordeel_gemini']}"
        klaar_url.add(r["item_id"])
        if cert:
            klaar.add(cert)
        nieuw += 1
        with PSA_F.open("a") as f:
            f.write(json.dumps(rij, ensure_ascii=False) + "\n")
        w = (rij.get("waarheid_url") or "").split("Singles/")[-1][:50]
        print(f"  [url/{soort}] {r['item_id']} cert={cert} -> {rij['oordeel']}"
              + (f" (PSA: {rij['psa']['card_name']} #{rij['psa']['number']} → {w or 'geen URL'})" if rij.get("psa") else ""))

    def check(r, kant, categorie):
        nonlocal budget, nieuw
        velden = velden_van(r, kant)
        cert = str(velden["cert"])
        if not is_cert(cert) or cert in klaar or budget <= 0:
            return
        budget -= 1
        psa, err = psa_lookup(scrape, cert)
        rij = {"ts": datetime.now(timezone.utc).isoformat(), "item_id": r["item_id"],
               "categorie": categorie, "kant": kant, "cert": cert}
        if psa is None:
            rij["psa"] = None
            rij["oordeel"] = f"psa_fail: {err}" if "404" not in str(err) else "cert_bestaat_niet"
        else:
            m = match_kant(psa, velden)
            rij["psa"] = psa
            rij["match"] = m
            rij["oordeel"] = "past_bij_deze_kant" if m["past"] else "bestaat_maar_ANDERE_kaart"
        klaar.add(cert)
        nieuw += 1
        with PSA_F.open("a") as f:
            f.write(json.dumps(rij, ensure_ascii=False) + "\n")
        print(f"  [{categorie}/{kant}] {r['item_id']} cert={cert} -> {rij['oordeel']}"
              + (f" (PSA: {psa['card_name']} #{psa['number']} g{psa['grade']})" if psa else ""))

    # 0. URL-verschillen eerst (hoofdlat): 'anders' → 'alleen Qwen' (risico ná de flip)
    #    → 'alleen Gemini' (gemiste deals ná de flip). 1 lookup per kaart.
    for r in url_diff:
        check_url(r)
    # 1. verschillen: per geval BEIDE kanten na elkaar, zodat elk gecheckt
    # verschil meteen een compleet oordeel heeft (Tommy 9-9: absolute waarheid).
    for r in differ:
        check(r, "qwen", "verschil")
        check(r, "gemini", "verschil")
    # 2. qwen-extra
    for r in extra:
        check(r, "qwen", "qwen_extra")
    # 3. controle-steekproef op agree (random, reproduceerbaar per dag)
    random.seed(datetime.now().strftime("%Y%m%d"))
    for r in random.sample(agree, min(CONTROLE_PER_RUN, len(agree))):
        check(r, "qwen", "controle_agree")

    print(f"klaar: {nieuw} PSA-checks gedaan (budget over: {budget}) — totaal in log: {len(klaar)}")
    return 0


def rapport() -> int:
    if not PSA_F.exists():
        print("nog geen psa_checks")
        return 1
    rows = [json.loads(l) for l in PSA_F.read_text().splitlines() if l.strip()]
    urls = [r for r in rows if r["categorie"] == "url_verschil"]
    rows = [r for r in rows if r["categorie"] != "url_verschil"]
    if urls:
        print(f"URL-VERSCHILLEN tegen PSA ({len(urls)} kaarten):")
        print(f"  {'':14s}{'Qwen':>8s}{'Gemini':>8s}")
        for o in ("goed", "FOUT", "gemist", "gemist?", "onbepaald", "geen_url_beide"):
            q = sum(1 for r in urls if r.get("oordeel_qwen") == o)
            g = sum(1 for r in urls if r.get("oordeel_gemini") == o)
            if q or g:
                print(f"  {o:14s}{q:>8d}{g:>8d}")
        for s in ("anders", "alleen_qwen", "alleen_gemini"):
            sub = [r for r in urls if r.get("soort") == s]
            if sub:
                print(f"  · {s}: {len(sub)} — Qwen goed {sum(1 for r in sub if r.get('oordeel_qwen') == 'goed')}"
                      f" / FOUT {sum(1 for r in sub if r.get('oordeel_qwen') == 'FOUT')}"
                      f" · Gemini goed {sum(1 for r in sub if r.get('oordeel_gemini') == 'goed')}"
                      f" / FOUT {sum(1 for r in sub if r.get('oordeel_gemini') == 'FOUT')}")
        fout = [r for r in urls if "FOUT" in (r.get("oordeel_qwen"), r.get("oordeel_gemini"))]
        for r in fout:
            print(f"    FOUT: {r['item_id']} qwen={r.get('oordeel_qwen')} gemini={r.get('oordeel_gemini')}"
                  f" PSA={r['psa']['card_name']} #{r['psa']['number']} → {(r.get('waarheid_url') or '').split('Singles/')[-1][:45]}")
        print()
    per = {}
    for r in rows:
        k = (r["categorie"], r["kant"], r["oordeel"].split(":")[0])
        per[k] = per.get(k, 0) + 1
    for (c, k, o), n in sorted(per.items()):
        print(f"  {c:14s} {k:7s} {o}: {n}")
    # kern-oordeel voor het dagrapport
    q_ok = sum(1 for r in rows if r["categorie"] == "verschil" and r["kant"] == "qwen"
               and r["oordeel"] == "past_bij_deze_kant")
    g_ok = sum(1 for r in rows if r["categorie"] == "verschil" and r["kant"] == "gemini"
               and r["oordeel"] == "past_bij_deze_kant")
    q_tot = sum(1 for r in rows if r["categorie"] == "verschil" and r["kant"] == "qwen")
    g_tot = sum(1 for r in rows if r["categorie"] == "verschil" and r["kant"] == "gemini")
    print(f"\nVERSCHILLEN-OORDEEL (absolute waarheid): Qwen gelijk {q_ok}/{q_tot} · Gemini gelijk {g_ok}/{g_tot}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rapport", action="store_true")
    sys.exit(rapport() if ap.parse_args().rapport else run())
