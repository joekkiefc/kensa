#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa LLM POC — Gemini 2.5 Flash slab-interpretatie + eBay-query bouwen.

Draait op N al-gescande items in de DB, vergelijkt LLM-output met de bestaande
regex/heuristiek output side-by-side. Bouwt niks in de pipeline — alleen rapport.

Vereist secrets.json → google.gemini_api_key.

Usage:
  ./llm_analyze.py                # 10 random items
  ./llm_analyze.py --limit 20     # meer items
  ./llm_analyze.py --item <id>    # specifieke listing
"""

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from vision import ocr_image, parse_psa_fields  # bestaande regex-baseline
from query_builder import build_query  # bestaande query-builder

DB_PATH = SCRIPT_DIR / "kensa.db"
SECRETS = Path("/home/pi/.openclaw/secrets.json")

GEMINI_MODEL = "gemini-flash-latest"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

SYSTEM_PROMPT = """Je bent een Pokemon-kaart PSA slab OCR-interpreter.

INPUT: ruwe OCR-text van een PSA-label + optionele listing-titel (EN/JP).

OUTPUT: strict JSON met deze velden:
{
  "name": "<Pokemon of trainer naam zoals op de PSA label, in Engels — bv 'Pikachu', 'Rocket's Moltres', 'Professor Juniper'>",
  "subtype": "<VMAX/VSTAR/GX/EX/V/SAR/SR/UR/HR/AR/CHR, of null>",
  "number": "<kaart-nummer, bv '123' of '068/187'>",
  "set_code": "<promo/set-code, bv 'S-P', 'SV-P', 'SM-P', 'XY-P', of null>",
  "set_name": "<volledige set-naam in Engels, bv 'Terastal Festival', 'Pikapika Campaign', of null>",
  "variant": "<optionele variant-info: 'Master Ball', 'Reverse Holo', 'Alt Art', 'Full Art', of null>",
  "grade": "<10 / 9.5 / 9 / etc>",
  "cert": "<8-10 cijfer cert nr, of null>",
  "year": <jaar als integer, of null>,
  "ebay_query": "<optimale eBay-zoekterm voor deze specifieke kaart+grade — moet varianten scherp scheiden>",
  "confidence": <0.0-1.0 hoe zeker je bent van de interpretatie>
}

REGELS:
- Behoud multi-word namen: 'Rocket\\'s Moltres', 'Galarian Zapdos', 'Alolan Vulpix'
- Bij Master Ball / Reverse Holo / Alt Art variant: neem dat mee in ebay_query
- ebay_query MOET altijd 'PSA <grade>' bevatten
- Als OCR corrupt is (bv 'MYLE' bij Sylveon in de titel), corrigeer via titel
- Antwoord PUUR met JSON, geen markdown, geen prose"""


def _api_key() -> str:
    s = json.loads(SECRETS.read_text())
    key = s.get("google", {}).get("gemini_api_key")
    if not key:
        raise SystemExit("secrets.json → google.gemini_api_key ontbreekt. Haal een key bij https://aistudio.google.com/apikey")
    return key


def _load_item(item_id: str) -> dict | None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        listing = conn.execute("SELECT * FROM listings WHERE item_id=?", (item_id,)).fetchone()
        if not listing:
            return None
        photos = conn.execute(
            "SELECT url_original FROM photos WHERE item_id=? ORDER BY photo_index",
            (item_id,),
        ).fetchall()
        slab = conn.execute(
            "SELECT result_json FROM analysis WHERE item_id=? AND trap='slab_ocr' ORDER BY analysis_id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return {
            "item_id": item_id,
            "title_en": listing["title_en"],
            "title_jp": listing["title_jp"],
            "photos": [p["url_original"] for p in photos],
            "slab_regex": json.loads(slab["result_json"]) if slab else None,
        }
    finally:
        conn.close()


def _pick_items(limit: int) -> list[str]:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute(
            "SELECT DISTINCT item_id FROM analysis WHERE trap='slab_ocr' ORDER BY RANDOM() LIMIT ?",
            (limit,),
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def _ocr_first_useful(photos: list[str], max_photos: int = 3) -> tuple[str, int | None]:
    """Return (raw_ocr_text, photo_idx_used)."""
    orig = [(i, u) for i, u in enumerate(photos) if "/orig/" in u or "/detail/orig/" in u]
    candidates = orig or list(enumerate(photos))
    for idx, url in candidates[:max_photos]:
        r = ocr_image(url)
        if r.get("full_text") and len(r["full_text"]) > 30:
            return r["full_text"], idx
    return "", None


def _gemini_call(ocr_text: str, title_en: str | None, title_jp: str | None, key: str) -> tuple[dict, dict]:
    """Return (parsed_json, meta) where meta = {latency_ms, token_est}."""
    user_msg = (
        f"OCR text van PSA slab-label + kaart:\n---\n{ocr_text[:2500]}\n---\n"
        f"Listing titel EN: {title_en or '(geen)'}\n"
        f"Listing titel JP: {title_jp or '(geen)'}\n"
        f"\nGeef JSON."
    )
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0.1, "maxOutputTokens": 4096},
    }
    req = urllib.request.Request(
        GEMINI_URL.format(model=GEMINI_MODEL, key=key),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    # Retry op 429 (free tier: 20 req/min voor gemini-2.5-flash)
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
            break
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 4:
                raise
            body = e.read().decode()
            m = re.search(r"retry in ([\d\.]+)s", body)
            wait = float(m.group(1)) if m else 30.0
            print(f"    [rate-limit] wachten {wait:.0f}s (attempt {attempt+1})", file=sys.stderr)
            time.sleep(min(wait + 1, 60))
    latency = int((time.perf_counter() - t0) * 1000)
    try:
        cand = data["candidates"][0]
        finish = cand.get("finishReason")
        text = cand["content"]["parts"][0]["text"]
        # Strip markdown code-fences if present
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
        parsed = json.loads(text)
    except Exception as e:
        raw_text = ""
        try: raw_text = data["candidates"][0]["content"]["parts"][0]["text"][:400]
        except Exception: raw_text = json.dumps(data)[:400]
        return {"_error": f"parse: {e}", "_finish": finish if 'finish' in dir() else None, "_raw_text": raw_text}, {"latency_ms": latency}
    usage = data.get("usageMetadata", {})
    return parsed, {"latency_ms": latency, "in_tokens": usage.get("promptTokenCount"), "out_tokens": usage.get("candidatesTokenCount")}


def _regex_baseline(slab: dict) -> dict:
    """Ontleedt bestaande slab_ocr JSON tot vergelijkbare fields + regex-built query."""
    if not slab:
        return {}
    # Geen default naar '10' — laat query leeg als grade ontbreekt zodat compare
    # toont dat de baseline geen bruikbare query kon bouwen.
    slab_grade = slab.get("grade")
    if not slab_grade or not str(slab_grade).strip():
        q = None
    else:
        q = build_query(slab.get("card_name"), slab.get("number"), str(slab_grade).strip(),
                        set_name=slab.get("set_name"), year=slab.get("year"))
    return {
        "name": slab.get("card_name"),
        "number": slab.get("number"),
        "set_name": slab.get("set_name"),
        "grade": slab.get("grade"),
        "cert": slab.get("cert"),
        "year": slab.get("year"),
        "ebay_query": q,
    }


def compare(item_id: str, key: str) -> None:
    item = _load_item(item_id)
    if not item:
        print(f"[{item_id}] niet gevonden")
        return

    ocr_text, photo_idx = _ocr_first_useful(item["photos"])
    if not ocr_text:
        print(f"[{item_id}] geen bruikbare OCR")
        return

    baseline = _regex_baseline(item["slab_regex"])
    llm, meta = _gemini_call(ocr_text, item["title_en"], item["title_jp"], key)

    print(f"\n{'='*80}\n{item_id}  (photo idx {photo_idx})")
    print(f"  title_en: {item['title_en']!r}")
    print(f"  title_jp: {item['title_jp']!r}")
    print(f"  latency: {meta.get('latency_ms')}ms  tokens: in={meta.get('in_tokens')} out={meta.get('out_tokens')}")
    print(f"\n  --- REGEX (huidig) ---")
    print(f"    name  = {baseline.get('name')!r}")
    print(f"    num   = {baseline.get('number')!r}")
    print(f"    set   = {baseline.get('set_name')!r}")
    print(f"    grade = {baseline.get('grade')!r}")
    print(f"    query = {baseline.get('ebay_query')!r}")
    print(f"\n  --- GEMINI ---")
    if "_error" in llm:
        print(f"    FOUT: {llm['_error']}")
        print(f"    finish: {llm.get('_finish')}")
        print(f"    raw: {(llm.get('_raw_text') or '')[:300]!r}")
        return
    for k in ("name", "subtype", "number", "set_code", "set_name", "variant", "grade", "cert", "year", "ebay_query", "confidence"):
        v = llm.get(k)
        if v not in (None, ""):
            print(f"    {k:12} = {v!r}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--item", type=str, default=None)
    args = ap.parse_args()

    key = _api_key()

    if args.item:
        compare(args.item, key)
    else:
        ids = _pick_items(args.limit)
        print(f"Vergelijking op {len(ids)} random items (regex vs Gemini 2.5 Flash):\n")
        for iid in ids:
            try:
                compare(iid, key)
                time.sleep(4)  # free tier = 20 req/min, sleep 4s houdt marge
            except Exception as e:
                print(f"\n[{iid}] FOUT: {e}")
                time.sleep(4)


if __name__ == "__main__":
    main()
