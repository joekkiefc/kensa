"""Kensa interpret_slab v2 — CoC-gesplitste implementatie.

Semantiek IDENTIEK aan llm_client.py:interpret_slab (regel 171, CoC 38).
Doel: elke sub-functie CoC < 15, orkestrator CoC < 10.
Wordt aliased vanaf llm_client.py na verifier + replay green.

Circuit-breaker state (_QUOTA_EXHAUSTED) blijft in llm_client — daar delen
we hem met interpret_slab_photo en judge_sales.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request

import llm_client
from llm_client import (
    MODEL_INTERPRET,
    SYSTEM_PROMPT,
    URL,
    _api_key,
    _hash_ocr,
    _slab_cache_get,
    _slab_cache_set,
)


def _prepare_request(ocr_text: str, title_en: str | None, title_jp: str | None) -> urllib.request.Request:
    """Bouw de Gemini HTTP-request. Puur transformatie, geen I/O."""
    user_msg = (
        f"OCR text van PSA slab-label + kaart:\n---\n{(ocr_text or '')[:2500]}\n---\n"
        f"Listing titel EN: {title_en or '(geen)'}\n"
        f"Listing titel JP: {title_jp or '(geen)'}\n\nGeef JSON."
    )
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.1,
            "maxOutputTokens": 512,
        },
    }
    return urllib.request.Request(
        URL.format(model=MODEL_INTERPRET, key=_api_key()),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )


def _wait_from_429(body_err: str) -> float:
    """Extract retry-wait uit een 429-body, capped op 15s."""
    m = re.search(r"retry in ([\d\.]+)s", body_err)
    return min(float(m.group(1)) + 1 if m else 10.0, 15.0)


def _call_gemini_with_retry(req: urllib.request.Request, max_retries: int,
                            verbose: bool) -> tuple[dict | None, dict | None]:
    """HTTP-call met 429 retry + circuit breaker. Return (data, error_dict)."""
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return (json.loads(resp.read()), None)
        except urllib.error.HTTPError as e:
            body_err = e.read().decode()
            if e.code == 429 and attempt < max_retries:
                wait = _wait_from_429(body_err)
                if verbose:
                    print(f"    [llm] 429 — wacht {wait:.0f}s (retry {attempt+1}/{max_retries})",
                          file=sys.stderr)
                time.sleep(wait)
                continue
            if e.code == 429:
                llm_client._mark_quota_exhausted()
                if verbose:
                    print("    [llm] 429 na retry — circuit breaker ON, LLM skip voor rest van run",
                          file=sys.stderr)
            return (None, {"_error": f"HTTP {e.code}: {body_err[:200]}"})
        except Exception as e:
            return (None, {"_error": f"{type(e).__name__}: {e}"})
    return (None, {"_error": "geen response"})


def _parse_gemini_response(data: dict) -> dict:
    """Extract text + parse JSON + handle bundle-list. Return dict of {_error, _raw}."""
    try:
        cand = data["candidates"][0]
        text = cand["content"]["parts"][0]["text"]
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
        parsed = json.loads(text)
    except Exception as e:
        return {"_error": f"parse: {e}", "_raw": json.dumps(data)[:400]}

    # LLM geeft soms een list terug voor bundel-listings — pak eerste dict of error.
    if isinstance(parsed, list):
        if not parsed:
            return {"_error": "LLM gaf lege list"}
        first = parsed[0]
        if not isinstance(first, dict):
            return {"_error": f"LLM list-item type={type(first).__name__}"}
        return first
    return parsed


def _add_meta(parsed: dict, data: dict, latency_ms: int) -> dict:
    """Voeg _meta toe (model, latency, tokens) aan het parsed antwoord."""
    usage = data.get("usageMetadata", {})
    parsed["_meta"] = {
        "model": MODEL_INTERPRET,
        "latency_ms": latency_ms,
        "in_tokens": usage.get("promptTokenCount"),
        "out_tokens": usage.get("candidatesTokenCount"),
    }
    return parsed


def interpret_slab_v2(ocr_text: str, title_en: str | None, title_jp: str | None,
                      verbose: bool = False, max_retries: int = 1) -> dict:
    """Zie llm_client.interpret_slab. Orkestrator: cache → circuit → prep → call → parse → meta → cache_set."""
    ocr_hash = _hash_ocr(ocr_text, title_en, title_jp)
    cached = _slab_cache_get(ocr_hash)
    if cached is not None:
        if verbose:
            print(f"    [llm] slab-cache hit (hash={ocr_hash[:12]}...)", file=sys.stderr)
        cached["_meta"] = {"model": "cache", "latency_ms": 0, "from_cache": True}
        return cached

    if llm_client._quota_exhausted():
        return {"_error": "circuit_breaker: eerder 429 in deze run — LLM overgeslagen"}

    req = _prepare_request(ocr_text, title_en, title_jp)
    t0 = time.perf_counter()
    data, err = _call_gemini_with_retry(req, max_retries, verbose)
    if err is not None:
        return err
    latency = int((time.perf_counter() - t0) * 1000)

    parsed = _parse_gemini_response(data)
    if "_error" in parsed:
        return parsed

    parsed = _add_meta(parsed, data, latency)
    _slab_cache_set(ocr_hash, parsed, model=MODEL_INTERPRET)
    return parsed
