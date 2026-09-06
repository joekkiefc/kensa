#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa — Gemini API client (Google AI Studio, free tier).

Één functie: interpret_slab(ocr_text, title_en, title_jp) → gestructureerde JSON.
Retry-with-backoff op 429. Log-vriendelijke output bij parse-fout.
"""

import hashlib
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
# Refactored slab-photo interpreter — CC 24 → max 10 per sub-functie.
# Zie ook alias-override aan het EIND van deze file zodat externe modules
# (check_slab_hybrid, POC/spike scripts) via `from llm_client import interpret_slab_photo`
# automatisch de v2-implementatie krijgen zonder hun code te wijzigen.
# NB: v2 hergebruikt `_quota_exhausted`/`_mark_quota_exhausted` uit deze module
# — circuit-breaker state blijft gedeeld met interpret_slab + judge_sales.

SECRETS = Path("/home/pi/.openclaw/secrets.json")
# Beide text-taken op Flash-Lite: PoC's toonden 100% match op interpret_slab en
# 100% keep-agreement op judge_sales (150/150 sale-beslissingen gelijk),
# en Flash-Lite is ~4x sneller. MODEL_PHOTO staat apart zodat multimodal-
# beslissingen (interpret_slab_photo) niet onbedoeld mee-downgraden.
MODEL_INTERPRET = "gemini-flash-lite-latest"
MODEL_JUDGE = "gemini-flash-lite-latest"
MODEL_PHOTO = "gemini-flash-lite-latest"  # 2026-08-06 stap 1/4 cost-cut: swap naar flash-lite (4x goedkoper input). Terug naar flash-latest als _reason=multimodal_incomplete stijgt boven ~10%.
URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

# OCR-based slab-cache: hash van OCR-tekst + titles → LLM-antwoord.
# Bespaart Gemini-calls bij relists (identieke slab-foto → identieke OCR).
DB_PATH = Path(__file__).resolve().parent / "kensa.db"
LLM_CACHE_DAYS = 30   # slab-interpretatie is deterministisch, mag lang cachen

# Circuit breaker: bij 429 wordt de rest van deze process-run overgeslagen.
# Volgende cron-run start met een verse Python-process → breaker reset vanzelf.
_QUOTA_EXHAUSTED = False


def _quota_exhausted() -> bool:
    return _QUOTA_EXHAUSTED


def _mark_quota_exhausted():
    global _QUOTA_EXHAUSTED
    _QUOTA_EXHAUSTED = True


def _ensure_slab_cache_table() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS llm_slab_cache (
                ocr_hash    TEXT PRIMARY KEY,
                result_json TEXT NOT NULL,
                cached_at   TEXT NOT NULL
            )"""
        )
        # Additive: track which model produced elke cached row (voor A/B analyse).
        cols = {r[1] for r in conn.execute("PRAGMA table_info(llm_slab_cache)").fetchall()}
        if "model" not in cols:
            conn.execute("ALTER TABLE llm_slab_cache ADD COLUMN model TEXT")
        conn.commit()
    finally:
        conn.close()


def _hash_ocr(ocr_text: str, title_en: str | None, title_jp: str | None) -> str:
    """SHA256-hash van de exacte input die naar LLM zou gaan. Deterministisch."""
    payload = f"{(ocr_text or '').strip()}|{(title_en or '').strip()}|{(title_jp or '').strip()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _slab_cache_get(h: str) -> dict | None:
    """Return gecacht LLM-resultaat of None als niet in cache / verlopen."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        row = conn.execute(
            "SELECT result_json, cached_at FROM llm_slab_cache WHERE ocr_hash=?", (h,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    # TTL check
    from datetime import datetime, timedelta, timezone
    try:
        ts = datetime.fromisoformat(row[1])
    except Exception:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if ts < datetime.now(timezone.utc) - timedelta(days=LLM_CACHE_DAYS):
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def _slab_cache_set(h: str, result: dict, model: str | None = None) -> None:
    """Sla LLM-resultaat op onder OCR-hash. Alleen valide antwoorden (geen _error)."""
    if not result or result.get("_error"):
        return
    # Strip _meta zodat we een schone gecachte versie hebben (metadata is per-call)
    clean = {k: v for k, v in result.items() if k != "_meta"}
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            """INSERT INTO llm_slab_cache (ocr_hash, result_json, cached_at, model)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(ocr_hash) DO UPDATE SET
                   result_json=excluded.result_json,
                   cached_at=excluded.cached_at,
                   model=excluded.model""",
            (h, json.dumps(clean, ensure_ascii=False), now, model),
        )
        conn.commit()
    finally:
        conn.close()


_ensure_slab_cache_table()

SYSTEM_PROMPT = """Je bent een Pokemon-kaart PSA slab OCR-interpreter.

INPUT: ruwe OCR-text van een PSA-label + optionele listing-titel (EN/JP).

OUTPUT: strict JSON:
{
  "name": "<Pokemon/trainer/kaart-naam zoals op de PSA label, in Engels; behoud multi-word namen: 'Rocket's Moltres', 'Galarian Zapdos', 'Alolan Vulpix'>",
  "subtype": "<VMAX/VSTAR/GX/EX/V/SAR/SR/UR/HR/AR/CHR of null>",
  "number": "<'123' of '068/187' — behoud volledige vorm>",
  "set_code": "<promo-code S-P/SV-P/SM-P/XY-P of set-code SV10/SV1a/S7R/S8a-P etc. of null>",
  "set_name": "<volledige set-naam in Engels, of null>",
  "variant": "<'Master Ball' / 'Reverse Holo' / 'Alt Art' / 'Full Art' / 'Rainbow Rare' etc. of null>",
  "grade": "<PSA grade als getal: 10/9.5/9/8.5/8/7/6/5/4/3/2/1 — of null als grade NIET duidelijk leesbaar is op het label>",
  "cert": "<8-10 cijfers of null>",
  "year": <integer of null>,
  "ebay_query": "<optimale zoekterm voor eBay voor DEZE specifieke kaart+grade. Moet varianten scherp scheiden. Format: '<name> <subtype?> <number> <set_code?> <variant?> PSA <grade>'>",
  "confidence": <0.0-1.0>
}

REGELS:
- Corrigeer OCR-fouten via de listing-titel (bv. 'MYLE' → 'Sylveon', 'DOPAT' → 'Rayquaza')
- GRADE-EXTRACTIE (verplicht exact):
  * Grade komt ALTIJD van het PSA-label zelf (het grote getal op de slab)
  * 'GEM MT 10' of 'GEM MINT 10' → grade = '10'; 'MINT 9' → grade = '9'; 'EX-MT 6' → grade = '6'
  * Als het grade-getal NIET zichtbaar/leesbaar is → grade = null. NOOIT defaulten naar '10'
  * Leid grade NIET af uit conditiebeschrijvingen als 'Near Mint', 'Pack Fresh', 'Looks like 10'
  * Antwoord met grade = null liever dan een gok — een verkeerde grade is erger dan geen grade
- ebay_query MOET altijd 'PSA <grade>' bevatten (weglaten als grade = null)
- Bij Master Ball / Reverse Holo / Alt Art variant: neem dat mee in ebay_query
- Antwoord PUUR JSON, geen markdown, geen prose"""


def _api_key() -> str:
    s = json.loads(SECRETS.read_text())
    key = s.get("google", {}).get("gemini_api_key")
    if not key:
        raise RuntimeError("secrets.json → google.gemini_api_key ontbreekt")
    return key


# `interpret_slab` verhuisd naar _split/ (v2). Verwijderd 2026-09-01 (regel 2 cleanup).
# Backup: _legacy_pre_refactor/... — alias-override staat lager in de file.



PHOTO_INTERPRET_PROMPT = """Je bent een Pokemon-kaart PSA slab visual-interpreter.

INPUT: foto van een PSA-gegradede kaart-slab (met label bovenaan) + optionele listing-titel (EN/JP).

Lees zelf ALLE tekst van het PSA-label EN de kaart. Combineer met de listing-titel om correcte kaart-info te bepalen.

OUTPUT: strict JSON (identieke velden als tekst-versie):
{
  "name": "<Pokemon/trainer/kaart-naam zoals op de PSA label, in Engels; behoud multi-word namen: 'Rocket's Moltres', 'Galarian Zapdos', 'Alolan Vulpix'>",
  "subtype": "<VMAX/VSTAR/GX/EX/V/SAR/SR/UR/HR/AR/CHR of null>",
  "number": "<'123' of '068/187' — behoud volledige vorm>",
  "set_code": "<promo-code S-P/SV-P/SM-P/XY-P of set-code SV10/SV1a/S7R/S8a-P etc. of null>",
  "set_name": "<volledige set-naam in Engels, of null>",
  "variant": "<'Master Ball' / 'Reverse Holo' / 'Alt Art' / 'Full Art' / 'Rainbow Rare' etc. of null>",
  "grade": "<PSA grade als getal: 10/9.5/9/8.5/8/7/6/5/4/3/2/1 — of null als grade NIET duidelijk leesbaar is op het label>",
  "cert": "<8-10 cijfers of null>",
  "year": <integer of null>,
  "ebay_query": "<optimale zoekterm voor eBay voor DEZE specifieke kaart+grade. Format: '<name> <subtype?> <number> <set_code?> <variant?> PSA <grade>'>",
  "confidence": <0.0-1.0>
}

REGELS:
- GRADE-EXTRACTIE: grade komt ALTIJD van het grote getal op het PSA-label
  * 'GEM MT 10' → grade='10'; 'MINT 9' → grade='9'
  * Als grade-getal niet leesbaar → grade=null. NOOIT defaulten naar '10'
- ebay_query MOET 'PSA <grade>' bevatten (weglaten als grade=null)
- Bij variant zichtbaar op kaart (Master Ball reverse pattern, Alt Art artwork): neem mee
- Antwoord PUUR JSON, geen markdown, geen prose"""


# NB: `interpret_slab_photo` is verhuisd naar `llm_client_split/interp_photo.py`.
#     De oude def (CC 24) is verwijderd na refactor 2026-09-01.
#     Backup: agents/kensa/_legacy_pre_refactor/llm_client.py.20260901
#     De naam wordt aan het EIND van deze file gealiast (zoek `_interpret_slab_photo_new`).


SALES_JUDGE_PROMPT = """Je beoordeelt of eBay sold-listings dezelfde kaart zijn als de query.

INPUT: query (Pokemon-kaart+grade+set) + array van sold-listings (title + price + date).

OUTPUT: JSON array — voor elke sale in dezelfde volgorde:
[
  {"i": 0, "keep": true/false, "reason": "<1-zin uitleg>"},
  ...
]

BEOORDEEL PER SALE:
- keep=true als de sale-titel exact dezelfde kaart is (naam, subtype, nummer, set, PSA-grade)
- keep=false als: ander nummer, andere set, andere pokemon, verkeerde grade, andere grader (BGS/CGC/SGC),
  bulk-listing/accessoire (keychain/sticker/lot), verkeerde variant
- Wees streng op VARIANTEN: 'Master Ball' ≠ 'Reverse Holo' ≠ 'Regular Full Art', 'Alt Art Secret' ≠ standaard,
  'Rainbow Rare' ≠ 'Full Art'. Prijzen kunnen 3-5× verschillen.
- PSA 10 slab wil ALLEEN PSA 10 sales; PSA 9 slabs alleen PSA 9.

Antwoord PUUR JSON array, geen markdown, geen prose."""


# `judge_sales` verhuisd naar _split/ (v2). Verwijderd 2026-09-01 (regel 2 cleanup).
# Backup: _legacy_pre_refactor/... — alias-override staat lager in de file.




# Refactor-switch(es) voor if __name__ zodat CLI-run de alias ook heeft.
# ---------------------------------------------------------------------------
# Refactor switch (2026-09-01): route `interpret_slab_photo` naar de v2 in
# llm_client_split/interp_photo.py. De originele def hierboven (regel ~282)
# blijft in de file staan voor snelle rollback (verwijder regel eronder).
# Externe modules (check_slab_hybrid.py + POC/spike) blijven ongewijzigd —
# hun `from llm_client import interpret_slab_photo` krijgt automatisch de v2.
# ---------------------------------------------------------------------------
from llm_client_split.interp_photo import interpret_slab_photo_v2 as _interpret_slab_photo_new  # noqa: E402
interpret_slab_photo = _interpret_slab_photo_new


# ---------------------------------------------------------------------------
# Refactor switch (2026-09-01, regel 2 #2): route `interpret_slab` naar de v2
# in interpret_slab_split/gemini_interpret.py. Originele def hierboven (regel 171,
# CoC 38) blijft staan voor snelle rollback (verwijder de regel eronder).
# Externe modules (analyze.py, poc_multimodal.py) blijven ongewijzigd —
# hun `from llm_client import interpret_slab` krijgt automatisch de v2.
# Backup: _legacy_pre_refactor/llm_client.py.20260901-regel2-func2
# ---------------------------------------------------------------------------
from interpret_slab_split.gemini_interpret import interpret_slab_v2 as _interpret_slab_new  # noqa: E402
interpret_slab = _interpret_slab_new


# Refactor switch (2026-09-01, regel 2 #5): route judge_sales -> v2.
# Backup: _legacy_pre_refactor/llm_client.py.20260901-regel2-func5
from judge_sales_split.gemini_judge import judge_sales_v2 as _judge_sales_new  # noqa: E402
judge_sales = _judge_sales_new

if __name__ == "__main__":
    # quick sanity test
    r = interpret_slab(
        "2024 POKEMON SV8a JP\nSYLVEON\nMASTER BALL REVERSE HOLO\n#068\nGEM MT\n10\n118117195",
        "[PSA10] Sylveon Master Ball Mirror",
        None,
        verbose=True,
    )
    print(json.dumps(r, indent=2))


