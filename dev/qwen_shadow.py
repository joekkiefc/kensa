#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Fase 3 — schaduwdraai: Qwen VL leest stil mee met elke echte slab-lezing.

Appels-met-appels met de live route (zie qwen_ocr_testplan.md fase 3):
  - ZELFDE uitvoer-contract: llm_client.PHOTO_INTERPRET_PROMPT (geïmporteerd,
    dus altijd identiek aan wat Gemini live krijgt).
  - ZELFDE context: listing-titel EN/JP in exact dezelfde user-message.
  - ZELFDE foto: de foto-index die de pijplijn zelf gebruikte (source_photo_idx).
  - BEWUSTE afwijking (gedocumenteerd): Qwen krijgt de foto op VOLLE resolutie.
    Live verkleint naar 768px puur om Gemini-imagetokens te besparen; een lokale
    GPU heeft die kosten niet. We meten dus de échte vervangings-configuratie
    (fase 2 bewees: volle resolutie = cert foutloos, 768px niet).
  - temperature 0 (bewezen fase-2-stand; Gemini live draait 0.1).

Vergelijking op de velden zoals het systeem ze gebruikt:
  cert / grade / number / card_name (fase-2-normalisatie) + de business-sleutel
  (analyze._build_card_key over Qwens velden vs listings.card_key live).

100% read-only op live: leest Supabase (analysis/listings/photos), schrijft
alleen dev/qwen_shadow/*. PC uit => run stopt zonder checkpoint-opschuif;
rijen ouder dan 24u worden dan als 'missed_offline' geteld (geen backfill).

Gebruik:
  qwen_shadow.py                 -> één batch (cron-modus, max 150 rijen)
  qwen_shadow.py --limit 8       -> kleine smoke-batch
  qwen_shadow.py --rapport       -> samenvatting uit het schaduwlog
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEV = Path(__file__).resolve().parent
KENSA = DEV.parent
sys.path.insert(0, str(KENSA))
sys.path.insert(0, str(DEV))

from storage_supabase import _http  # noqa: E402
from llm_client import PHOTO_INTERPRET_PROMPT  # noqa: E402
from analyze import _build_card_key  # noqa: E402
from qwen_backtest import norm_ws, norm_grade, norm_number, cert_digits, parse_model_json  # noqa: E402

OUT = DEV / "qwen_shadow"
STATE_F = OUT / "shadow_state.json"
LOG_F = OUT / "shadow_log.jsonl"

ENDPOINT = "http://100.125.116.37:1234/v1/chat/completions"
MODELS_URL = "http://100.125.116.37:1234/v1/models"
MODEL = "qwen2.5-vl-7b-instruct"
UA = {"User-Agent": "Mozilla/5.0"}

MAX_PER_RUN = 150
OFFLINE_BACKLOG_H = 24  # ouder dan dit tijdens offline-inhaal => missed_offline


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state() -> dict:
    if STATE_F.exists():
        return json.loads(STATE_F.read_text())
    # Eerste start: alleen de verse stroom vanaf nu (bewust geen backfill).
    return {"last_created_at": now_iso(), "counters": {}, "started": now_iso()}


def save_state(st: dict) -> None:
    st["last_run"] = now_iso()
    STATE_F.write_text(json.dumps(st, indent=1))


def tel(st: dict, key: str, n: int = 1) -> None:
    st["counters"][key] = st["counters"].get(key, 0) + n


def log_row(row: dict) -> None:
    with LOG_F.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ bionic
def bionic_online() -> bool:
    try:
        with urllib.request.urlopen(MODELS_URL, timeout=4) as r:
            data = json.loads(r.read())
        return any(m.get("id") == MODEL for m in data.get("data", []))
    except Exception:
        return False


def vraag_qwen(img_bytes: bytes, title_en: str | None, title_jp: str | None) -> tuple[dict, float]:
    """Zelfde contract als live: PHOTO_INTERPRET_PROMPT als system, titels in user-msg."""
    user_msg = (
        f"Analyseer deze PSA-slab foto.\n"
        f"Listing titel EN: {title_en or '(geen)'}\n"
        f"Listing titel JP: {title_jp or '(geen)'}\n\nGeef JSON."
    )
    b64 = base64.b64encode(img_bytes).decode()
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": PHOTO_INTERPRET_PROMPT},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": user_msg},
            ]},
        ],
        "temperature": 0,
        "max_tokens": 512,
    }
    t0 = time.time()
    req = urllib.request.Request(ENDPOINT, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=75) as r:
        resp = json.loads(r.read())
    raw = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
    return parse_model_json(raw), time.time() - t0


# ------------------------------------------------------------------ supabase (read-only)
def fetch_new_rows(since: str, limit: int) -> list[dict]:
    return _http.get("analysis", "kensa", [
        ("select", "item_id,result_json,created_at"),
        ("trap", "eq.slab_ocr"),
        ("created_at", f"gt.{since}"),
        ("order", "created_at.asc"),
        ("limit", str(limit)),
    ])


def fetch_listing(item_id: str) -> dict:
    rows = _http.get("listings", "kensa", [
        ("select", "item_id,title_en,title_jp,card_key"),
        ("item_id", f"eq.{item_id}"),
        ("limit", "1"),
    ])
    return rows[0] if rows else {}


def fetch_photo_url(item_id: str, wanted_idx) -> str | None:
    rows = _http.get("photos", "kensa", [
        ("select", "photo_index,url_original"),
        ("item_id", f"eq.{item_id}"),
        ("order", "photo_index.asc"),
        ("limit", "10"),
    ])
    if not rows:
        return None
    if wanted_idx is not None:
        for r in rows:
            if r["photo_index"] == wanted_idx:
                return r["url_original"]
    return rows[0]["url_original"]


def verbeter_foto_url(url: str) -> str:
    """Upgrade bekende thumbnail-vormen naar volle resolutie (fase-2-recept).

    Mercari:  static.mercdn.net/thumb/item/jpeg/X  -> /item/detail/orig/photos/X
    Shops:    assets.mercari-shops-static.com/-/small/plain/X -> -/large/plain/X
    Empirisch bevestigd 2026-09-09: orig=810x1080, large=952x1600. Overige
    bronnen (Buyee/Yahoo) zijn al groot genoeg -> ongemoeid.
    """
    if "static.mercdn.net/thumb/item/jpeg/" in url:
        return url.replace("/thumb/item/jpeg/", "/item/detail/orig/photos/")
    if "mercari-shops-static.com/-/small/plain/" in url:
        return url.replace("/-/small/plain/", "/-/large/plain/")
    return url


def download(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = resp.read()
        return data if len(data) >= 8000 else None
    except Exception:
        return None


def haal_foto(url: str) -> tuple[bytes | None, str, bool]:
    """(bytes, gebruikte_url, upgraded) — upgrade eerst, val terug op origineel."""
    beter = verbeter_foto_url(url)
    if beter != url:
        img = download(beter)
        if img is not None:
            return img, beter, True
    return download(url), url, False


def img_px(data: bytes) -> str | None:
    try:
        import io
        from PIL import Image
        return "x".join(map(str, Image.open(io.BytesIO(data)).size))
    except Exception:
        return None


# ------------------------------------------------------------------ vergelijking
def qwen_card_key(q: dict) -> str | None:
    slab_like = {"card_name": q.get("name"), "number": q.get("number"),
                 "grade": q.get("grade"), "set_code": q.get("set_code")}
    llm_like = {"name": q.get("name"), "number": q.get("number"),
                "grade": q.get("grade"), "set_code": q.get("set_code")}
    try:
        return _build_card_key(slab_like, llm_like)
    except Exception:
        return None


def vergelijk(gem: dict, q: dict, live_key: str | None) -> dict:
    g_cert, q_cert = cert_digits(gem.get("cert")), cert_digits(q.get("cert"))
    qn, gn = norm_number(q.get("number")), norm_number(gem.get("number"))
    qk = qwen_card_key(q)
    lk = live_key or None
    v = {
        "cert_agree": bool(g_cert) and g_cert == q_cert,
        "gemini_cert": bool(g_cert), "qwen_cert": bool(q_cert),
        "grade_agree": norm_grade(q.get("grade")) == norm_grade(gem.get("grade")),
        "number_agree": qn == gn,
        "number_prefix_agree": bool(qn) and bool(gn) and qn.split("/")[0] == gn.split("/")[0],
        "name_agree": norm_ws(q.get("name", "")) == norm_ws(gem.get("card_name", "")),
        "key_qwen": qk, "key_live": lk,
        # Kern = pokemon:nummer:grade. Live-keys missen structureel de set_code
        # (multimodal-pad heeft geen llm_data), Qwen levert 'm wel — dat is
        # geen identiteitsverschil. Exacte vorm apart bewaard.
        "key_agree": _kern(qk) is not None and _kern(qk) == _kern(lk),
        "key_exact": qk is not None and qk == lk,
        "pokemon_agree": (qk or ":").split(":")[0] == (lk or ":").split(":")[0] and qk is not None and lk is not None,
    }
    return v


def _kern(key: str | None) -> str | None:
    return ":".join(key.split(":")[:3]) if key else None


# ------------------------------------------------------------------ batch
def run_batch(limit: int) -> int:
    OUT.mkdir(exist_ok=True)
    st = load_state()

    if not bionic_online():
        tel(st, "offline_runs")
        st["last_offline"] = now_iso()
        save_state(st)
        print("PC/Bionic offline — checkpoint blijft staan, niks verwerkt (stekker-gedrag ok)")
        return 0

    rows = fetch_new_rows(st["last_created_at"], limit)
    if not rows:
        save_state(st)
        print("geen nieuwe slab-lezingen")
        return 0

    grens_oud = datetime.now(timezone.utc) - timedelta(hours=OFFLINE_BACKLOG_H)
    verwerkt = 0
    for r in rows:
        iid, created = r["item_id"], r["created_at"]
        rj = r.get("result_json") or {}
        source = rj.get("_source") or "?"

        # Te oude achterstand (PC was lang uit): niet inhalen, wel eerlijk tellen.
        try:
            ts = datetime.fromisoformat(created)
        except ValueError:
            ts = datetime.now(timezone.utc)
        if ts < grens_oud:
            tel(st, "missed_offline")
            st["last_created_at"] = created
            continue

        if source == "skip":
            tel(st, "gemini_skip")
            st["last_created_at"] = created
            continue

        listing = fetch_listing(iid)
        url = fetch_photo_url(iid, rj.get("source_photo_idx"))
        img, gebruikt_url, upgraded = haal_foto(url) if url else (None, url, False)
        if img is None:
            tel(st, "fetch_error")
            log_row({"ts": now_iso(), "item_id": iid, "created_at": created,
                     "status": "fetch_error", "source": source, "photo_url": url})
            st["last_created_at"] = created
            continue

        # 2 pogingen op DEZELFDE rij; faalt allebei -> PC weg: stop zonder
        # checkpoint-opschuif, zodat deze rij bij de volgende run terugkomt.
        q = None
        for poging in (1, 2):
            try:
                q, dt = vraag_qwen(img, listing.get("title_en"), listing.get("title_jp"))
                break
            except Exception as e:
                print(f"  qwen-call faalde ({type(e).__name__}) — poging {poging}")
                if poging == 1:
                    time.sleep(3)
        if q is None:
            print("PC lijkt uitgevallen midden in de run — stop, checkpoint bij laatste succes")
            tel(st, "offline_runs")
            st["last_offline"] = now_iso()
            break

        if "_parse_error" in q:
            tel(st, "qwen_parse_error")
            log_row({"ts": now_iso(), "item_id": iid, "created_at": created,
                     "status": "qwen_parse_error", "source": source, "raw": q["_parse_error"]})
            st["last_created_at"] = created
            continue

        gem_took = None
        for p in rj.get("per_photo") or []:
            if p.get("photo_idx") == rj.get("source_photo_idx"):
                gem_took = p.get("took_ms")
        v = vergelijk(rj, q, listing.get("card_key"))
        status = "ok" if source == "multimodal" else "vs_vision"
        tel(st, status)
        log_row({
            "ts": now_iso(), "item_id": iid, "created_at": created, "status": status,
            "source": source, "qwen_s": round(dt, 2), "gemini_ms": gem_took,
            "foto": {"url": gebruikt_url, "px": img_px(img), "kb": len(img) // 1024,
                      "upgraded": upgraded},
            "gemini": {k: rj.get(k) for k in ("cert", "grade", "card_name", "number", "set_name")},
            "qwen": {k: q.get(k) for k in ("cert", "grade", "name", "number", "set_code", "set_name",
                                            "subtype", "variant", "confidence")},
            "vergelijk": v,
        })
        st["last_created_at"] = created
        verwerkt += 1
        if verwerkt % 20 == 0:
            save_state(st)
        time.sleep(0.3)

    save_state(st)
    print(f"batch klaar: {verwerkt} vergeleken · counters={st['counters']}")
    return 0


# ------------------------------------------------------------------ rapport
def rapport() -> int:
    if not LOG_F.exists():
        print("nog geen schaduwlog")
        return 1
    st = load_state()
    rows = [json.loads(l) for l in LOG_F.read_text().splitlines() if l.strip()]
    ok = [r for r in rows if r["status"] == "ok"]
    vsv = [r for r in rows if r["status"] == "vs_vision"]

    def pct(n, d):
        return f"{100.0 * n / d:.1f}%" if d else "-"

    cert_basis = [r for r in ok if r["vergelijk"]["gemini_cert"]]
    cert_agree = sum(1 for r in cert_basis if r["vergelijk"]["cert_agree"])
    cert_qwen_miss = sum(1 for r in cert_basis if not r["vergelijk"]["qwen_cert"])
    cert_differ = [r for r in cert_basis if r["vergelijk"]["qwen_cert"] and not r["vergelijk"]["cert_agree"]]
    # Gemini las géén cert maar Qwen wel een plausibele (8-10 cijfers):
    # niet automatisch fout — fase 2 bewees dat Qwen hier gelijk kan hebben. Steekproef.
    import re as _re
    cert_extra = [r for r in ok if not r["vergelijk"]["gemini_cert"]
                  and _re.fullmatch(r"\d{8,10}", str(r["qwen"].get("cert") or ""))]

    velden = {}
    for f in ("grade_agree", "number_agree", "number_prefix_agree", "name_agree", "pokemon_agree"):
        velden[f] = f"{sum(1 for r in ok if r['vergelijk'][f])}/{len(ok)} ({pct(sum(1 for r in ok if r['vergelijk'][f]), len(ok))})"
    # key-kern hier uit de strings berekend (werkt ook voor oudere logrijen)
    kk = sum(1 for r in ok if _kern(r["vergelijk"].get("key_qwen"))
             and _kern(r["vergelijk"].get("key_qwen")) == _kern(r["vergelijk"].get("key_live")))
    velden["key_kern_agree"] = f"{kk}/{len(ok)} ({pct(kk, len(ok))})"

    tijden = sorted(r["qwen_s"] for r in ok if r.get("qwen_s"))
    gtijden = sorted(r["gemini_ms"] / 1000.0 for r in ok if r.get("gemini_ms"))
    dagen = sorted({r["created_at"][:10] for r in rows})

    out = {
        "sinds": st.get("started"), "dagen": dagen,
        "vergeleken_multimodal": len(ok), "vs_vision_fallback": len(vsv),
        "counters": st.get("counters", {}),
        "cert": {
            "basis (Gemini las cert)": len(cert_basis),
            "overeenstemming": f"{cert_agree}/{len(cert_basis)} ({pct(cert_agree, len(cert_basis))})",
            "qwen_las_niks": cert_qwen_miss,
            "verschillende_certs (steekproef!)": len(cert_differ),
            "qwen_extra_cert (Gemini niks, steekproef)": len(cert_extra),
        },
        "velden_multimodal": velden,
        "qwen_s": {"avg": round(sum(tijden) / len(tijden), 2) if tijden else None,
                    "p95": round(tijden[int(0.95 * len(tijden))], 2) if tijden else None},
        "gemini_s": {"avg": round(sum(gtijden) / len(gtijden), 2) if gtijden else None},
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if cert_differ:
        print("\nCERT-VERSCHILLEN (voor handmatige PSA-steekproef):")
        for r in cert_differ[:15]:
            print(f"  {r['item_id']}: gemini={r['gemini'].get('cert')} qwen={r['qwen'].get('cert')}")
    afwijkend = [r for r in ok if _kern(r["vergelijk"].get("key_qwen")) != _kern(r["vergelijk"].get("key_live"))][:12]
    if afwijkend:
        print("\nBUSINESS-SLEUTEL AFWIJKEND (laatste 12):")
        for r in afwijkend:
            print(f"  {r['item_id']}: live={r['vergelijk']['key_live']} qwen={r['vergelijk']['key_qwen']}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=MAX_PER_RUN)
    ap.add_argument("--rapport", action="store_true")
    a = ap.parse_args()
    sys.exit(rapport() if a.rapport else run_batch(a.limit))
