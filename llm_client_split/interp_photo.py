"""interp_photo.py — refactor-split van `interpret_slab_photo` uit `../llm_client.py`.

Vijf sub-functies (één per baseline-fase F1-F5) plus een orkestrator
`interpret_slab_photo_v2` die exact hetzelfde gedrag oplevert als
`llm_client.interpret_slab_photo` (regel 282-394, CC 24).

**Module-globale state (KRITIEK):**
`_QUOTA_EXHAUSTED` is een module-level variable in `llm_client.py`, gedeeld
met `interpret_slab` en `judge_sales`. Deze split IMPORTEERT `llm_client` en
leest/muteert die staat via `llm_client._quota_exhausted()` /
`llm_client._mark_quota_exhausted()` — NIET gedupliceerd hier, anders zou de
circuit-breaker per-module divergeren.

Overige constants/helpers ook via `llm_client.*` referentie:
- `llm_client.MODEL_PHOTO` — default multimodal model
- `llm_client.URL` — Gemini `generateContent` endpoint-template
- `llm_client.PHOTO_INTERPRET_PROMPT` — system-instruction voor photo-variant
- `llm_client._api_key()` — leest `secrets.json` → gemini_api_key

Zie `agents/kensa/analyze_refactor_fixtures_interp/BASELINE_ANALYSIS.md` voor
de F1-F5 fase-split en volledige branch-inventaris (25 branches).
"""

import base64
import io
import json
import re
import sys
import time
import urllib.error
import urllib.request

import llm_client


# ---------------------------------------------------------------------------
# Sub-functies (F1-F5) — één per baseline-fase
# ---------------------------------------------------------------------------


def _ip_guard_and_setup(model: str | None) -> str | None:
    """F1 — Guard + setup.

    Circuit-breaker check: als een eerdere call in deze process-run een 429
    kreeg is `llm_client._QUOTA_EXHAUSTED` gezet en skippen we de LLM-call.
    Returns het te gebruiken model (`model` override of `MODEL_PHOTO`
    default), of `None` als de quota exhausted is — caller mapt None naar
    de originele error-dict.
    """
    if llm_client._quota_exhausted():
        return None
    return model or llm_client.MODEL_PHOTO


def _ip_fetch_photo(photo_url: str, verbose: bool = False):
    """F2 — Photo-fetch + PIL downscale + MIME.

    Download photo via urllib (Mercari-CDN eist User-Agent header, anders
    403). Downscale naar max 768px (~65% minder image-tokens — kost-cut
    2026-08-06 stap 4/4). MIME uit URL-suffix afgeleid (default JPEG).

    Returns:
      - Success: tuple `(image_bytes, mime, fetch_ms)`
      - Error: dict `{"_error": "photo fetch: ..."}` — caller returned direct
    """
    t_fetch = time.perf_counter()
    try:
        req = urllib.request.Request(photo_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            img_bytes = resp.read()
    except Exception as e:
        return {"_error": f"photo fetch: {type(e).__name__}: {e}"}
    fetch_ms = int((time.perf_counter() - t_fetch) * 1000)

    # 2026-08-06 stap 4/4 cost-cut: downscale >768px → ~65% minder image tokens
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(img_bytes))
        max_side = 768
        w, h = im.size
        if max(w, h) > max_side:
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            scale = max_side / max(w, h)
            im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85, optimize=True)
            img_bytes = buf.getvalue()
    except Exception:
        pass  # fallback: verstuur originele bytes

    mime = "image/jpeg"
    if photo_url.lower().endswith(".png"):
        mime = "image/png"
    elif photo_url.lower().endswith(".webp"):
        mime = "image/webp"

    return img_bytes, mime, fetch_ms


def _ip_build_payload(image_bytes: bytes, mime: str,
                       title_en: str | None, title_jp: str | None) -> tuple[dict, str]:
    """F3 — Prompt/payload build.

    Base64-encodeert de foto-bytes en bouwt het Gemini `generateContent`
    payload-body met system-instruction (`PHOTO_INTERPRET_PROMPT`),
    inline image + user-msg met listing-titels, en generationConfig
    (temperatuur 0.1, max 512 tokens, JSON-mime).

    Returns `(body, user_msg)`.
    """
    b64 = base64.b64encode(image_bytes).decode()
    user_msg = (
        f"Analyseer deze PSA-slab foto.\n"
        f"Listing titel EN: {title_en or '(geen)'}\n"
        f"Listing titel JP: {title_jp or '(geen)'}\n\nGeef JSON."
    )
    body = {
        "system_instruction": {"parts": [{"text": llm_client.PHOTO_INTERPRET_PROMPT}]},
        "contents": [{"role": "user", "parts": [
            {"inline_data": {"mime_type": mime, "data": b64}},
            {"text": user_msg},
        ]}],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0.1, "maxOutputTokens": 512},
    }
    return body, user_msg


def _ip_call_llm_with_retry(payload: dict, model: str,
                             max_retries: int = 1, verbose: bool = False) -> tuple[dict, dict]:
    """F4 — LLM-call + 429-retry loop.

    Doet de POST naar Gemini met 60s timeout (bigger multimodal payload).
    Retry-strategie identiek aan origineel:
      * 429 mét retry-budget: parse "retry in Ns" uit body, sleep min(N+1, 15),
        volgende attempt.
      * 429 zonder retry-budget: `_mark_quota_exhausted()` (side-effect op
        gedeelde module-globale) → error-dict retour.
      * Andere HTTPError of Exception: direct error-dict.

    Returns:
      - Success: tuple `(response_json, meta)` waarbij meta `{"latency_ms": N}`.
      - Error: tuple `(error_dict, {})` — caller checkt `"_error" in first`.
    """
    key = llm_client._api_key()
    req = urllib.request.Request(
        llm_client.URL.format(model=model, key=key),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    data = None
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read())
            break
        except urllib.error.HTTPError as e:
            body_err = e.read().decode()
            if e.code == 429 and attempt < max_retries:
                m = re.search(r"retry in ([\d\.]+)s", body_err)
                wait = min(float(m.group(1)) + 1 if m else 10.0, 15.0)
                if verbose:
                    print(f"    [llm-photo] 429 — wacht {wait:.0f}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if e.code == 429:
                llm_client._mark_quota_exhausted()
            return {"_error": f"HTTP {e.code}: {body_err[:200]}"}, {}
        except Exception as e:
            return {"_error": f"{type(e).__name__}: {e}"}, {}
    if data is None:
        return {"_error": "geen response"}, {}
    latency = int((time.perf_counter() - t0) * 1000)
    return data, {"latency_ms": latency}


def _ip_parse_response(response: dict, meta: dict) -> dict:
    """F5 — Response-parse + list-unwrap + `_meta` injectie.

    Parseert `candidates[0].content.parts[0].text`, strips ```json-fence,
    doet `json.loads`. Gemini geeft soms een list terug (bundel-listings —
    zelfde edge-case als text-variant): pakken [0]. Attacht `_meta` met
    model / latency_ms / fetch_ms / in_tokens / out_tokens / image_bytes
    (identiek shape als origineel regel 385-393).

    Meta-dict input MOET bevatten: `model`, `latency_ms`, `fetch_ms`,
    `image_bytes`. Token-counts worden hier uit `usageMetadata` gehaald.
    """
    try:
        text = response["candidates"][0]["content"]["parts"][0]["text"]
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
        parsed = json.loads(text)
    except Exception as e:
        return {"_error": f"parse: {e}", "_raw": json.dumps(response)[:400]}
    if isinstance(parsed, list):
        if not parsed:
            return {"_error": "LLM gaf lege list"}
        parsed = parsed[0]
        if not isinstance(parsed, dict):
            return {"_error": f"LLM list-item type={type(parsed).__name__}"}
    usage = response.get("usageMetadata", {})
    parsed["_meta"] = {
        "model": meta.get("model"),
        "latency_ms": meta.get("latency_ms"),
        "fetch_ms": meta.get("fetch_ms"),
        "in_tokens": usage.get("promptTokenCount"),
        "out_tokens": usage.get("candidatesTokenCount"),
        "image_bytes": meta.get("image_bytes"),
    }
    return parsed


# ---------------------------------------------------------------------------
# Orkestrator — identieke signature + gedrag als llm_client.interpret_slab_photo
# ---------------------------------------------------------------------------


def interpret_slab_photo_v2(photo_url: str, title_en: str | None, title_jp: str | None,
                             verbose: bool = False, max_retries: int = 1,
                             model: str | None = None) -> dict:
    """Multimodal: stuurt slab-foto DIRECT naar Gemini, skip Vision OCR-stap.

    Retourneert JSON dict of `{'_error': ...}` bij falen. Bevat `_meta` met
    latency, tokens, model, image_bytes (voor cost-analyse).

    Identiek in signature en gedrag aan `llm_client.interpret_slab_photo`.
    Zie `agents/kensa/analyze_refactor_fixtures_interp/BASELINE_ANALYSIS.md`.
    """
    used_model = _ip_guard_and_setup(model)
    if used_model is None:
        return {"_error": "circuit_breaker: eerder 429 in deze run — LLM overgeslagen"}

    fetched = _ip_fetch_photo(photo_url, verbose)
    if isinstance(fetched, dict):
        return fetched
    img_bytes, mime, fetch_ms = fetched

    payload, _user_msg = _ip_build_payload(img_bytes, mime, title_en, title_jp)

    data, call_meta = _ip_call_llm_with_retry(payload, used_model, max_retries, verbose)
    if "_error" in data:
        return data

    meta = {
        "model": used_model,
        "latency_ms": call_meta["latency_ms"],
        "fetch_ms": fetch_ms,
        "image_bytes": len(img_bytes),
    }
    return _ip_parse_response(data, meta)
