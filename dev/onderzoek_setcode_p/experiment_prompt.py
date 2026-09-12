#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Onderzoek set-code '-P' (11-9). ALLEEN LEZEN / meten, niets live wijzigen.

Draait het LIVE codepad qwen_lezer.lees_slab_foto(url) (foto ophalen, zelfde payload,
temperature=0, max_tokens=512, zelfde model, lot-check, cert-sanity, opschonen).
Het enige dat per variant verandert is de module-global qwen_lezer.PROMPT_FIXED
(vraag_qwen leest die bij elke call). parse_model_json wordt ingepakt om het RUWE
modelantwoord (vóór opschonen) vast te leggen; haal_foto om de sha1 van de foto te loggen.

Varianten:
  P1 = PROMPT_FIXED ongewijzigd
  P2 = alleen '/S8a-P' uit het set_code-voorbeeld weg (rest byte-identiek)
  P3 = alle -P-voorbeelden weg ('promo-code S-P/SV-P/SM-P/XY-P of ' + '/S8a-P')

Sequentieel, max MAX_CALLS calls, backlog-check vooraf en elke 15 calls.
Hervatbaar: resultaten.jsonl, klaar = (item_id, variant, herhaling).
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HIER = Path(__file__).resolve().parent
KENSA = HIER.parent.parent
sys.path.insert(0, str(KENSA))

import qwen_lezer as QL  # noqa: E402
from storage_supabase import _http  # noqa: E402

MAX_CALLS = 118
UIT = HIER / "resultaten.jsonl"
STATUS = HIER / "status.log"

ORIG = QL.PROMPT_FIXED
FRAG = "set-code SV10/SV1a/S7R/S8a-P etc."
assert ORIG.count(FRAG) == 1, "voorbeeldzin niet precies 1x gevonden"
P2 = ORIG.replace("SV10/SV1a/S7R/S8a-P etc.", "SV10/SV1a/S7R etc.", 1)
P3 = P2.replace("promo-code S-P/SV-P/SM-P/XY-P of set-code", "set-code", 1)
assert P2 != ORIG and len(ORIG) - len(P2) == len("/S8a-P")
assert P3 != P2 and "-P" not in P3.split('"set_code":')[1].split("\n")[0]
VARIANTEN = {"P1": ORIG, "P2": P2, "P3": P3}
assert QL.MODEL == "qwen2.5-vl-7b-instruct", QL.MODEL


def log(t: str) -> None:
    regel = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {t}"
    print(regel, flush=True)
    with STATUS.open("a") as f:
        f.write(regel + "\n")


def backlog() -> int:
    rows = _http.get("listings", "kensa", {"slab_status": "eq.pending", "select": "item_id", "limit": "1000"})
    return len(rows or [])


# ---- raw-capture hooks (alleen in dit proces) ----
_laatste = {}
_orig_parse = QL.parse_model_json
_orig_haal = QL.haal_foto


def _parse_hook(content):
    _laatste["raw"] = content
    return _orig_parse(content)


def _haal_hook(url):
    img, gebruikt, up = _orig_haal(url)
    _laatste["sha1"] = hashlib.sha1(img).hexdigest() if img else None
    _laatste["kb"] = len(img) // 1024 if img else None
    return img, gebruikt, up


QL.parse_model_json = _parse_hook
QL.haal_foto = _haal_hook


def kies_items() -> list[dict]:
    rows = json.load(open(HIER / "kandidaten_live.json"))
    def lab(r): return "SV8a" if "SV8a JP" in r["lbl"] else "SV2a"
    def fout(r): return (r["sc"] or "").endswith("-P")
    groepen = [("SV8a", True, 15), ("SV2a", True, 5), ("SV8a", False, 5), ("SV2a", False, 5)]
    uit = []
    for l, f, n in groepen:
        # SV8a/SV2a correct = exact 'SV8A'/'SV2A' (niet SV10/SV-P/leeg)
        pool = [r for r in rows if lab(r) == l and fout(r) == f and r["url_original"]
                and (f or r["sc"] in ("SV8A", "SV2A"))]
        uit.append((l, f, n, pool))
    return uit


def main() -> None:
    klaar = set()
    if UIT.exists():
        for regel in UIT.open():
            d = json.loads(regel)
            klaar.add((d["item_id"], d["variant"], d["herhaling"]))
    calls = len(klaar)
    start_backlog = backlog()
    log(f"start: backlog pending={start_backlog}, al klaar={calls}, qwen_bereikbaar={QL.qwen_bereikbaar()}")
    if not QL.qwen_bereikbaar():
        log("STOP: Qwen 7B niet bereikbaar")
        return

    def run(item: dict, variant: str, herhaling: int) -> dict | None:
        nonlocal calls
        sleutel = (item["item_id"], variant, herhaling)
        if sleutel in klaar:
            return "klaar"
        if calls >= MAX_CALLS:
            raise SystemExit("MAX_CALLS bereikt")
        if calls and calls % 15 == 0:
            b = backlog()
            log(f"backlog-check na {calls} calls: pending={b} (start {start_backlog})")
            if b > max(40, start_backlog + 30):
                log("PAUZE/STOP: OCR-backlog loopt op")
                raise SystemExit("backlog")
        QL.PROMPT_FIXED = VARIANTEN[variant]
        _laatste.clear()
        t0 = time.time()
        try:
            lezing = QL.lees_slab_foto(item["url_original"])
        except QL.QwenOnbereikbaar as e:
            log(f"STOP: Qwen onbereikbaar: {e}")
            raise SystemExit("onbereikbaar")
        finally:
            QL.PROMPT_FIXED = ORIG
        if (lezing or {}).get("_error", "").startswith("foto:"):
            return None                      # foto weg → geen call gedaan
        calls += 1
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "item_id": item["item_id"], "variant": variant, "herhaling": herhaling,
            "label_live": item["lbl"], "set_code_live": item["sc"], "live_ts": item["created_at"],
            "foto_url": item["url_original"], "foto_sha1": _laatste.get("sha1"), "foto_kb": _laatste.get("kb"),
            "raw": _laatste.get("raw"),
            "raw_set_code": None, "raw_number": None, "raw_label_name": None,
            "opgeschoond_set_code": lezing.get("set_code"), "opgeschoond_number": lezing.get("number"),
            "opgeschoond_name": lezing.get("name"), "error": lezing.get("_error"),
            "sec": round(time.time() - t0, 2),
        }
        try:
            r = QL.parse_model_json(_laatste.get("raw") or "") if _laatste.get("raw") else {}
            rec["raw_set_code"], rec["raw_number"], rec["raw_label_name"] = r.get("set_code"), r.get("number"), r.get("label_name")
        except Exception:
            pass
        with UIT.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        klaar.add(sleutel)
        log(f"#{calls} {item['item_id']} {variant} h{herhaling} live={item['sc']} raw_set={rec['raw_set_code']!r} raw_nr={rec['raw_number']!r} → {rec['opgeschoond_set_code']}")
        return rec

    # Fase A: 30 foto's x P1,P2,P3 (verweven per foto, zodat tijd/drift geen verschil maakt)
    gekozen: list[dict] = []
    for l, f, n, pool in kies_items():
        genomen = 0
        for item in pool:
            if genomen >= n:
                break
            r1 = run(item, "P1", 1)
            if r1 is None:
                continue                     # foto niet meer te halen → volgende kandidaat
            genomen += 1
            gekozen.append(item)
            run(item, "P2", 1)
            run(item, "P3", 1)
        log(f"groep {l} fout={f}: {genomen}/{n} foto's")
    json.dump([g["item_id"] for g in gekozen], open(HIER / "gekozen_items.json", "w"))

    # Fase B: variantie — 5 foto's (eerste 5 live -P) nog 2x P1 en 1x P2
    for item in gekozen[:5]:
        run(item, "P1", 2)
        run(item, "P1", 3)
        run(item, "P2", 2)
    log(f"KLAAR: {calls} calls, backlog nu {backlog()}")


if __name__ == "__main__":
    main()
