#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa — Qwen als slab-lezer (fase 4, GO Tommy 2026-09-09 16:35).

Leest de PSA-slab-foto met de lokale Qwen2.5-VL-7B op Bionic (Tommy's PC, via
Tailscale). Geeft dezelfde velden terug als de Gemini-lezer (interpret_slab_photo),
zodat check_slab_hybrid en alles daarachter geen verschil merken.

Regels (Tommy 9-9):
  - Qwen is DE lezer. Gemini ALLEEN als Qwen niet bereikbaar is → QwenOnbereikbaar.
  - Een onvolledige of onleesbare Qwen-lezing is GEEN reden voor Gemini: die komt
    gewoon als onvolledige lezing terug (check_slab_hybrid valt dan terug op het
    Vision-vangnet, precies zoals bij een onvolledige Gemini-lezing).
  - Verzonnen certs (77777777, 2025000000) komen er niet door → cert_plausibel().

Deploy-config = de config uit de schaduw (fase 3b, gemeten): volle-resolutie foto,
GEEN listing-titel (bewezen 9-9: JP-titel duwt Qwen naar romanisatie, 0/24 → 9/24),
PROMPT_FIXED (label_name letterlijk, FA/SA/RR niet uitspellen). Daarna opschonen
naar Kensa-velden via lezing_opschonen (zelfde trechter als het test-harnas).

Schakelaar: KENSA_SLAB_LEZER=qwen in cron_worker_ocr.sh (zie check_slab_hybrid).
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import time
import urllib.error
import urllib.request

from llm_client import PHOTO_INTERPRET_PROMPT
from lezing_opschonen import opschonen

BASIS = os.environ.get("KENSA_QWEN_URL", "http://100.125.116.37:1234").rstrip("/")
ENDPOINT = f"{BASIS}/v1/chat/completions"
MODELS_URL = f"{BASIS}/v1/models"
MODEL = os.environ.get("KENSA_QWEN_MODEL", "qwen2.5-vl-7b-instruct")
CALL_TIMEOUT_S = float(os.environ.get("KENSA_QWEN_TIMEOUT", "75"))
UA = {"User-Agent": "Mozilla/5.0"}

# Eén plek voor de opdracht: schaduw (dev/qwen_prompt_fixed.py) importeert deze.
PROMPT_FIXED = PHOTO_INTERPRET_PROMPT.replace(
    '  "subtype":',
    '  "label_name": "<de NAAM-regel van het PSA-label LETTERLIJK zoals gedrukt, bv \'FA/JOLTEON V\' of \'TM.MAG.GROUDON-HOLO\' — kopieer, interpreteer niet>",\n  "subtype":',
).replace(
    "REGELS:",
    "REGELS:\n- FA/, SA/, RR/ vooraan op het label zijn AFKORTINGEN (Full Art, Special Art). NOOIT uitspellen tot een woord (dus nooit \'Fairy\'); laat staan of weglaten.",
)
assert PROMPT_FIXED != PHOTO_INTERPRET_PROMPT

USER_MSG = "Analyseer deze PSA-slab foto.\nListing titel EN: (geen)\nListing titel JP: (geen)\n\nGeef JSON."


class QwenOnbereikbaar(Exception):
    """PC uit, Tailscale weg, LM Studio niet geladen, timeout, server-fout: Gemini mag overnemen."""


# Stroomonderbreker: na een 'onbereikbaar' slaan we Qwen even over, anders wacht elke
# kaart in dezelfde worker-run 2× de call-timeout voordat Gemini aan de beurt is.
ONDERBREKER_S = float(os.environ.get("KENSA_QWEN_ONDERBREKER", "120"))
_onbereikbaar_tot = 0.0


def _meld_onbereikbaar(reden: str) -> QwenOnbereikbaar:
    global _onbereikbaar_tot
    _onbereikbaar_tot = time.time() + ONDERBREKER_S
    return QwenOnbereikbaar(reden)


def _onderbreker_open() -> str | None:
    rest = _onbereikbaar_tot - time.time()
    return f"recent onbereikbaar, {int(rest)}s overgeslagen" if rest > 0 else None


# ------------------------------------------------------------------ bereikbaarheid
def qwen_bereikbaar(timeout: float = 4.0) -> bool:
    """Staat het model klaar? (GET /v1/models, kort.)"""
    try:
        with urllib.request.urlopen(MODELS_URL, timeout=timeout) as r:
            data = json.loads(r.read())
        return any(m.get("id") == MODEL for m in data.get("data", []))
    except Exception:
        return False


# ------------------------------------------------------------------ foto (volle resolutie)
def verbeter_foto_url(url: str) -> str:
    """Bekende thumbnail-vormen → volle resolutie (fase-2-recept, bevestigd 9-9).
    Mercari: /thumb/item/jpeg/X → /item/detail/orig/photos/X (810x1080)
    Shops:   /-/small/plain/X   → /-/large/plain/X          (952x1600)
    Buyee/Yahoo: al groot genoeg → ongemoeid."""
    if "static.mercdn.net/thumb/item/jpeg/" in url:
        return url.replace("/thumb/item/jpeg/", "/item/detail/orig/photos/")
    if "mercari-shops-static.com/-/small/plain/" in url:
        return url.replace("/-/small/plain/", "/-/large/plain/")
    return url


def _download(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = resp.read()
        return data if len(data) >= 8000 else None
    except Exception:
        return None


def haal_foto(url: str) -> tuple[bytes | None, str, bool]:
    """(bytes, gebruikte_url, upgraded) — eerst de grote variant, anders het origineel."""
    beter = verbeter_foto_url(url)
    if beter != url:
        img = _download(beter)
        if img is not None:
            return img, beter, True
    return _download(url), url, False


# ------------------------------------------------------------------ de call
def parse_model_json(content: str) -> dict:
    s = (content or "").strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.MULTILINE).strip()
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if not m:
        return {"_error": f"parse: geen JSON in antwoord: {content[:120]!r}"}
    try:
        d = json.loads(m.group(0))
    except Exception as e:
        return {"_error": f"parse: {type(e).__name__}: {content[:120]!r}"}
    if not isinstance(d, dict):
        return {"_error": f"parse: geen object maar {type(d).__name__}"}
    return d


def vraag_qwen(img_bytes: bytes, pogingen: int = 2) -> tuple[dict, float]:
    """Eén foto → ruwe JSON-lezing. Onbereikbaar (na `pogingen`) → QwenOnbereikbaar."""
    b64 = base64.b64encode(img_bytes).decode()
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": PROMPT_FIXED},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                {"type": "text", "text": USER_MSG},
            ]},
        ],
        "temperature": 0,
        "max_tokens": 512,
    }
    body = json.dumps(payload).encode()
    laatste = "?"
    for poging in range(1, pogingen + 1):
        t0 = time.time()
        req = urllib.request.Request(ENDPOINT, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=CALL_TIMEOUT_S) as r:
                resp = json.loads(r.read())
            raw = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
            return parse_model_json(raw), time.time() - t0
        except urllib.error.HTTPError as e:
            if e.code < 500:                      # 4xx = ons verzoek deugt niet → geen 'onbereikbaar'
                return {"_error": f"HTTP {e.code}: {e.read()[:160]!r}"}, time.time() - t0
            laatste = f"HTTP {e.code}"
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as e:
            laatste = f"{type(e).__name__}: {e}"
        except Exception as e:                    # rare server-antwoorden (geen JSON) → ook 'onbereikbaar'
            laatste = f"{type(e).__name__}: {e}"
        if poging < pogingen:
            time.sleep(3)
    raise _meld_onbereikbaar(laatste)


# ------------------------------------------------------------------ cert-sanity
def cert_plausibel(cert) -> bool:
    """PSA-cert: 8-10 cijfers, geen verzinsel. 77777777 en 2025000000 zijn echt gezien (9-9)."""
    s = str(cert or "").strip()
    if not re.fullmatch(r"\d{8,10}", s):
        return False
    if len(set(s)) == 1:                          # 77777777
        return False
    if s.endswith("000000"):                      # 2025000000
        return False
    if s in ("12345678", "123456789", "1234567890", "10000000", "100000000"):
        return False
    return True


# ------------------------------------------------------------------ hoofdfunctie
def lees_slab_foto(photo_url: str) -> dict:
    """Eén foto → lezing in het Gemini-contract (name/number/set_code/set_name/grade/cert/year/…).

    - foto niet te halen → {"_error": "foto: ..."}  (geen Gemini: dat is geen Qwen-probleem)
    - Qwen onbereikbaar → QwenOnbereikbaar (caller schakelt naar Gemini)
    - onleesbaar/parse-fout → {"_error": "parse: ..."}
    - anders: opgeschoonde lezing + '_lezer': 'qwen', '_qwen_s', '_foto'.
    """
    open_ = _onderbreker_open()
    if open_:
        raise QwenOnbereikbaar(open_)
    img, gebruikt, upgraded = haal_foto(photo_url) if photo_url else (None, photo_url, False)
    if img is None:
        return {"_error": f"foto: niet te halen ({photo_url})"}
    ruw, dt = vraag_qwen(img)
    if ruw.get("_error"):
        ruw["_lezer"] = "qwen"
        ruw["_qwen_s"] = round(dt, 2)
        return ruw
    if ruw.get("cert") is not None and not cert_plausibel(ruw.get("cert")):
        ruw["_cert_afgekeurd"] = str(ruw.get("cert"))
        ruw["cert"] = None
    lezing = opschonen(ruw)
    lezing["_lezer"] = "qwen"
    lezing["_qwen_s"] = round(dt, 2)
    lezing["_foto"] = {"url": gebruikt, "kb": len(img) // 1024, "upgraded": upgraded}
    return lezing


if __name__ == "__main__":
    import sys
    print("bereikbaar:", qwen_bereikbaar())
    if len(sys.argv) > 1:
        print(json.dumps(lees_slab_foto(sys.argv[1]), indent=2, ensure_ascii=False))
