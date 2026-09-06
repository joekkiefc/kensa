"""Kensa judge_sales v2 — CoC-gesplitste implementatie.

Semantiek IDENTIEK aan llm_client.py:judge_sales (regel 315, CoC 24).
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
    MODEL_JUDGE,
    SALES_JUDGE_PROMPT,
    URL,
    _api_key,
)


def _shorten_sales(sales: list[dict]) -> list[dict]:
    return [{
        "i": i,
        "title": (s.get("title") or "")[:200],
        "price_usd": s.get("price_usd"),
        "date": s.get("sold_date"),
    } for i, s in enumerate(sales)]


def _prepare_judge_request(query: str, sales: list[dict], context: dict | None) -> urllib.request.Request:
    variant_hint = ""
    if context and context.get("variant"):
        variant_hint = f"\nBelangrijke variant om te matchen: {context['variant']}"
    user_msg = (
        f"Query: {query}{variant_hint}\n\n"
        f"Sales om te beoordelen:\n{json.dumps(_shorten_sales(sales), ensure_ascii=False)}\n\n"
        f"Geef JSON array met keep/reject per sale (behoud dezelfde 'i' index)."
    )
    body = {
        "system_instruction": {"parts": [{"text": SALES_JUDGE_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0.1, "maxOutputTokens": 512},
    }
    return urllib.request.Request(
        URL.format(model=MODEL_JUDGE, key=_api_key()),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )


def _wait_from_429(body_err: str) -> float:
    m = re.search(r"retry in ([\d\.]+)s", body_err)
    return min(float(m.group(1)) + 1 if m else 10.0, 15.0)


def _call_judge_with_retry(req, max_retries: int, verbose: bool) -> tuple[dict | None, dict | None]:
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return (json.loads(resp.read()), None)
        except urllib.error.HTTPError as e:
            body_err = e.read().decode()
            if e.code == 429 and attempt < max_retries:
                wait = _wait_from_429(body_err)
                if verbose:
                    print(f"    [llm-judge] 429 — wacht {wait:.0f}s", file=sys.stderr)
                time.sleep(wait)
                continue
            return (None, {"_error": f"HTTP {e.code}: {body_err[:200]}"})
        except Exception as e:
            return (None, {"_error": f"{type(e).__name__}: {e}"})
    return (None, {"_error": "geen response"})


def _parse_judge_response(data: dict) -> list | dict:
    """Return decisions-lijst, of error-dict."""
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
        decisions = json.loads(text)
    except Exception as e:
        return {"_error": f"parse: {e}"}
    if not isinstance(decisions, list):
        return {"_error": f"expected list, got {type(decisions).__name__}"}
    return decisions


def judge_sales_v2(query: str, sales: list[dict], context: dict | None = None,
                   verbose: bool = False, max_retries: int = 1) -> dict:
    """Zie llm_client.judge_sales."""
    if llm_client._quota_exhausted():
        return {"_error": "circuit_breaker: eerder 429 in deze run — LLM overgeslagen"}

    req = _prepare_judge_request(query, sales, context)
    t0 = time.perf_counter()
    data, err = _call_judge_with_retry(req, max_retries, verbose)
    if err is not None:
        return err

    decisions_or_err = _parse_judge_response(data)
    if isinstance(decisions_or_err, dict):
        return decisions_or_err

    latency = int((time.perf_counter() - t0) * 1000)
    usage = data.get("usageMetadata", {})
    return {
        "decisions": decisions_or_err,
        "_meta": {"model": MODEL_JUDGE, "latency_ms": latency,
                   "in_tokens": usage.get("promptTokenCount"),
                   "out_tokens": usage.get("candidatesTokenCount")},
    }
