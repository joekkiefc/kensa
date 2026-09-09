#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Fase 2 — backwards test: Qwen VL moet de gouden set exact reproduceren.

Gebruik:
  qwen_backtest.py --selftest   -> test-de-test: scheidsrechter zelf bewijzen (laag 6)
  qwen_backtest.py              -> de echte run (PROMPT_V1 bevroren), resume-baar

Vergelijkregels (vastgelegd vóór de run, zie qwen_ocr_testplan.md):
  cert   : cijfer-voor-cijfer stringgelijk
  grade  : numeriek gelijk na normalisatie ("10"=="10", 9.5=="9.5")
  number : na strip van '#', hoofdletters: exact gelijk aan PSA; heeft PSA géén slash,
           dan moet het deel vóór de slash van Qwen gelijk zijn aan PSA
  naam   : gelijk na normalisatie (hoofdletters, witruimte samengevouwen, spaties
           rond '-' en '/' weg) — opmaak is geen inhoud, tekens wel
  valstrik: alleen {"no_label": true} is goed; cert-achtige cijfers = VERZONNEN -> directe fail
"""
from __future__ import annotations

import base64
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

DEV = Path(__file__).resolve().parent
GOLD = DEV / "qwen_goldenset"
ANSWERS = GOLD / "answers.json"
RESULTS = GOLD / "backtest_results.json"

ENDPOINT = "http://100.125.116.37:1234/v1/chat/completions"
MODEL = "qwen2.5-vl-7b-instruct"

PROMPT_VERSIE = "V2"
PROMPT_V1 = (
    "Photo of a trading card, possibly in a grading slab. Your ONLY task is to copy text from the "
    "red-and-white PSA LABEL at the top of a PSA slab (the label that says 'PSA').\n"
    "Rules:\n"
    "- COPY characters EXACTLY as printed on the label. Never translate, never substitute names you "
    "think are correct, never guess characters you cannot read.\n"
    "- cert must be an 8-10 digit number you can CLEARLY read on the PSA label. If any digit is "
    "unclear, do not guess.\n"
    "- If there is no PSA slab (raw card, sealed product, card in a plain toploader), or the slab is "
    "from another grading company (ARS, AXCI, BGS, CGC, TAG, ...), or the photo is a screenshot of an "
    'app, return exactly {"no_label": true}.\n'
    "Return ONLY a JSON object with keys:\n"
    "cert: the certification number\n"
    "grade: the NUMERIC grade on the label (e.g. 10, 9.5, 9)\n"
    "card_name: the card NAME line in capital letters, including prefix words (e.g. DETECTIVE "
    "PIKACHU, FA/PIKACHU — never shorten), WITHOUT rarity/variety text such as SPECIAL ART RARE\n"
    "number: the COMPLETE card number after #, exactly as printed including any suffix after a slash "
    "(e.g. 205/172, 339/SM-P, 083)\n"
    'Or {"no_label": true}. No extra text.'
)


# ---------------------------------------------------------------- normalisatie
def norm_ws(s: str) -> str:
    s = re.sub(r"\s+", " ", (s or "").strip().upper())
    s = re.sub(r"\s*([-/])\s*", r"\1", s)
    return s


def norm_grade(g) -> str:
    s = str(g or "").strip()
    m = re.search(r"(\d+(?:\.\d+)?)", s)
    if not m:
        return ""
    v = m.group(1)
    return v[:-2] if v.endswith(".0") else v


def norm_number(n) -> str:
    return norm_ws(str(n or "")).lstrip("#").strip()


def cert_digits(c) -> str:
    return re.sub(r"\D", "", str(c or ""))


# ---------------------------------------------------------------- vergelijker
def compare_card(waarheid: dict, antwoord: dict) -> dict:
    """Return per veld True/False + geheel."""
    r = {}
    r["cert"] = cert_digits(antwoord.get("cert")) == waarheid["cert"]
    r["grade"] = norm_grade(antwoord.get("grade")) == norm_grade(waarheid["grade"])
    qn, tn = norm_number(antwoord.get("number")), norm_number(waarheid["number"])
    r["number"] = (qn == tn) if "/" in tn else (qn.split("/")[0] == tn)
    r["card_name"] = norm_ws(antwoord.get("card_name", "")) == norm_ws(waarheid["card_name"])
    r["alles"] = all(r.values())
    return r


def compare_trap(antwoord: dict) -> dict:
    no_label = antwoord.get("no_label") is True
    verzonnen = bool(re.fullmatch(r"\d{7,11}", cert_digits(antwoord.get("cert"))))
    return {"no_label_ok": no_label and not verzonnen, "verzonnen_cert": verzonnen}


def parse_model_json(content: str) -> dict:
    s = content.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.MULTILINE).strip()
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if not m:
        return {"_parse_error": content[:120]}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {"_parse_error": content[:120]}


# ---------------------------------------------------------------- test-de-test
def selftest() -> bool:
    data = json.loads(ANSWERS.read_text())
    fails = []
    # 1) waarheid tegen zichzelf: alles moet slagen
    for k in data["kaarten"]:
        r = compare_card(k["waarheid"], dict(k["waarheid"]))
        if not r["alles"]:
            fails.append(f"waarheid-vs-waarheid faalde: {k['item_id']} {r}")
    for t in data["valstrikken"]:
        r = compare_trap({"no_label": True})
        if not r["no_label_ok"]:
            fails.append(f"valstrik-vs-waarheid faalde: {t['item_id']}")
    # 2) expres fout: alles moet afgekeurd worden
    proef = data["kaarten"][0]["waarheid"]
    corrupties = [
        ("cert 1 cijfer anders", {**proef, "cert": proef["cert"][:-1] + ("1" if proef["cert"][-1] != "1" else "2")}),
        ("grade anders", {**proef, "grade": "9" if norm_grade(proef["grade"]) != "9" else "8"}),
        ("naam anders", {**proef, "card_name": proef["card_name"] + " X"}),
        ("nummer anders", {**proef, "number": (proef["number"] or "0") + "9"}),
    ]
    for naam, corrupt in corrupties:
        if compare_card(proef, corrupt)["alles"]:
            fails.append(f"corruptie NIET gevangen: {naam}")
    if compare_trap({"cert": "12345678", "grade": "10"})["no_label_ok"]:
        fails.append("valstrik met verzonnen cert NIET gevangen")
    if not compare_trap({"cert": "12345678"})["verzonnen_cert"]:
        fails.append("verzonnen-cert-detectie faalde")
    # nummer-regel: PSA zonder slash accepteert label-met-slash (deel vóór slash gelijk)
    if not compare_card({**proef, "number": "339"}, {**proef, "number": "339/SM-P"})["number"]:
        fails.append("nummer-regel (voor-de-slash) faalde")
    if compare_card({**proef, "number": "339/SM-P"}, {**proef, "number": "339"})["number"]:
        fails.append("nummer-regel te soepel: volledige PSA-vorm niet afgedwongen")
    for f in fails:
        print("SELFTEST FAIL:", f)
    n = len(data["kaarten"]) + len(data["valstrikken"])
    print(f"SELFTEST: {'GESLAAGD' if not fails else 'GEFAALD'} — {n} waarheid-checks + {len(corrupties)+3} corruptie-checks")
    return not fails


# ---------------------------------------------------------------- de echte run
def vraag_qwen(img_bytes: bytes) -> tuple[dict, float, str]:
    b64 = base64.b64encode(img_bytes).decode()
    payload = {"model": MODEL, "messages": [{"role": "user", "content": [
        {"type": "text", "text": PROMPT_V1},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}],
        "temperature": 0, "max_tokens": 250}
    t0 = time.time()
    req = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        resp = json.loads(r.read())
    raw = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
    return parse_model_json(raw), time.time() - t0, raw


def run() -> int:
    data = json.loads(ANSWERS.read_text())
    eerder = {}
    if RESULTS.exists():
        eerder = {r["item_id"]: r for r in json.loads(RESULTS.read_text()).get("per_kaart", [])}
    per_kaart, tijden = [], []
    try:
        for k in data["kaarten"]:
            iid = k["item_id"]
            if iid in eerder:
                per_kaart.append(eerder[iid]); continue
            img = (GOLD / k["photo"]).read_bytes()
            for poging in (1, 2):
                try:
                    antwoord, dt, raw = vraag_qwen(img); break
                except Exception as e:
                    if poging == 2:
                        raise
                    time.sleep(3)
            cmp_ = compare_card(k["waarheid"], antwoord)
            tijden.append(dt)
            per_kaart.append({"item_id": iid, "type": "kaart", "tijd_s": round(dt, 2),
                              "waarheid": k["waarheid"], "qwen": antwoord,
                              "gemini_ref": k.get("gemini_ref"), "vergelijk": cmp_})
            print(f"  {'✓' if cmp_['alles'] else '✗'} {iid} {dt:.1f}s "
                  f"{'' if cmp_['alles'] else '-> fout in: ' + ','.join(f for f in ('cert','grade','number','card_name') if not cmp_[f])}")
            (GOLD / "backtest_results.json").write_text(json.dumps(
                {"status": "bezig", "per_kaart": per_kaart}, ensure_ascii=False, indent=1))
        for t in data["valstrikken"]:
            iid = t["item_id"]
            key = f"trap_{iid}"
            if key in eerder:
                per_kaart.append(eerder[key]); continue
            img = (GOLD / t["photo"]).read_bytes()
            antwoord, dt, raw = vraag_qwen(img)
            cmp_ = compare_trap(antwoord)
            tijden.append(dt)
            per_kaart.append({"item_id": key, "type": "valstrik", "tijd_s": round(dt, 2),
                              "qwen": antwoord, "vergelijk": cmp_})
            print(f"  {'✓' if cmp_['no_label_ok'] else '✗'} VALSTRIK {iid} {dt:.1f}s"
                  f"{' !! VERZONNEN CERT' if cmp_['verzonnen_cert'] else ''}")
            (GOLD / "backtest_results.json").write_text(json.dumps(
                {"status": "bezig", "per_kaart": per_kaart}, ensure_ascii=False, indent=1))
    except Exception as e:
        print(f"AFGEBROKEN (PC uit / netwerk?): {type(e).__name__}: {e} — tussenstand bewaard, herstart hervat")
        return 2

    kaarten = [r for r in per_kaart if r["type"] == "kaart"]
    traps = [r for r in per_kaart if r["type"] == "valstrik"]
    velden = {f: sum(1 for r in kaarten if r["vergelijk"][f]) for f in ("cert", "grade", "number", "card_name")}
    alles = sum(1 for r in kaarten if r["vergelijk"]["alles"])
    trap_ok = sum(1 for r in traps if r["vergelijk"]["no_label_ok"])
    verzonnen = sum(1 for r in traps if r["vergelijk"]["verzonnen_cert"])
    geslaagd = (alles == len(kaarten) and trap_ok == len(traps) and verzonnen == 0)
    samenvatting = {
        "status": "GESLAAGD" if geslaagd else "GEFAALD",
        "prompt": PROMPT_VERSIE, "model": MODEL,
        "kaarten_alles_exact": f"{alles}/{len(kaarten)}", "per_veld": velden,
        "valstrikken_ok": f"{trap_ok}/{len(traps)}", "verzonnen_certs": verzonnen,
        "tijd_gemiddeld_s": round(sum(tijden) / max(len(tijden), 1), 2) if tijden else None,
        "gedraaid_op": time.strftime("%Y-%m-%d %H:%M:%S"),
        "per_kaart": per_kaart,
    }
    RESULTS.write_text(json.dumps(samenvatting, ensure_ascii=False, indent=1))
    print(json.dumps({k: v for k, v in samenvatting.items() if k != "per_kaart"}, ensure_ascii=False, indent=1))
    return 0 if geslaagd else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    sys.exit(run())
