#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Fase 4 — dagrapport van de live lezer: wie las de slabs (Qwen / Gemini-vangnet / Vision),
hoe vaak was Qwen onbereikbaar, welke certs zijn als rommel afgekeurd, leestijd.

Gebruik:  dev/qwen_live_rapport.py [--sinds 2026-09-09T14:44:00+00:00] [--uren 24]
Read-only (leest kensa.analysis, trap slab_ocr).
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

KENSA = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KENSA))
from storage_supabase import _http  # noqa: E402

FLIP = "2026-09-09T14:44:54+00:00"   # fase 4 aan (16:44:54 CEST)


def haal(sinds: str) -> list[dict]:
    return haal_trap("slab_ocr", sinds)


def haal_trap(trap: str, sinds: str) -> list[dict]:
    out, offset = [], 0
    while True:
        rows = _http.get("analysis", "kensa", [
            ("select", "item_id,result_json,created_at"), ("trap", f"eq.{trap}"),
            ("created_at", f"gt.{sinds}"), ("order", "created_at.asc"),
            ("limit", "1000"), ("offset", str(offset)),
        ])
        out.extend(rows)
        if len(rows) < 1000:
            return out
        offset += 1000


def rapport(sinds: str) -> dict:
    rows = haal(sinds)
    bron = collections.Counter()
    redenen = collections.Counter()
    afgekeurd = []
    tijden = []
    onbereikbaar = 0
    for r in rows:
        rj = r.get("result_json") or {}
        s = rj.get("_source") or "?"
        bron[s] += 1
        reden = rj.get("_reason") or ""
        if s in ("gemini_fallback", "vision_fallback"):
            redenen[reden.split(" (")[0][:40]] += 1
        if "qwen_onbereikbaar" in reden:
            onbereikbaar += 1
        for p in rj.get("per_photo") or rj.get("_multimodal_per_photo") or []:
            if p.get("cert_afgekeurd"):
                afgekeurd.append((r["item_id"], p["cert_afgekeurd"]))
            if p.get("lezer") == "qwen" and p.get("took_ms"):
                tijden.append(p["took_ms"] / 1000)
    # eBay-notitieboek (Tommy 9-9 20:20: omweg via CM-URL / kern): waar kwam de prijs vandaan?
    ebay = haal_trap("ebay_prices", sinds)
    ebay_bron = collections.Counter()
    for r in ebay:
        rj = r.get("result_json") or {}
        if rj.get("from_cache"):
            ebay_bron[rj.get("cache_source") or "cache_zoekzin"] += 1
        elif rj.get("error"):
            ebay_bron["live_ebay_fout"] += 1
        else:
            ebay_bron["live_gelukt"] += 1
    n = len(rows)
    gelezen = n - bron.get("skip", 0)
    vangnet = bron.get("gemini_fallback", 0) + bron.get("vision_fallback", 0)
    return {
        "sinds": sinds, "lezingen": n, "zonder_skip": gelezen,
        "bron": dict(bron),
        "vangnet_pct": round(100 * vangnet / gelezen, 1) if gelezen else None,
        "qwen_onbereikbaar_kaarten": onbereikbaar,
        "vangnet_redenen": dict(redenen),
        "ebay_prijs_bron": dict(ebay_bron),
        "ebay_uit_notitieboek_pct": round(100 * sum(v for k, v in ebay_bron.items() if not k.startswith("live")) / len(ebay), 1) if ebay else None,
        "cert_afgekeurd_rommel": len(afgekeurd),
        "cert_afgekeurd_voorbeelden": afgekeurd[:8],
        "qwen_s": {"gem": round(statistics.mean(tijden), 2), "p95": round(sorted(tijden)[int(len(tijden) * 0.95) - 1], 2)}
        if len(tijden) >= 20 else {"n": len(tijden)},
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sinds", default=None)
    ap.add_argument("--uren", type=float, default=None)
    a = ap.parse_args()
    sinds = a.sinds or ((datetime.now(timezone.utc) - timedelta(hours=a.uren)).isoformat() if a.uren else FLIP)
    print(json.dumps(rapport(sinds), indent=1, ensure_ascii=False))
