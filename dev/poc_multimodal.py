#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""PoC — vergelijk oude flow (Vision + Flash-Lite interpret_slab) vs nieuwe flow
(directe multimodal Gemini Flash op de foto). Meet accuratesse + kosten/latency.

Draai:  ./poc_multimodal.py [N]     # N = aantal slabs (default 10)

Output: per-item side-by-side veld-vergelijking + samenvatting met
JSON-field match rate en geschatte $/1k items voor beide paden.
"""
import json, random, sqlite3, sys, time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from vision import ocr_image
from llm_client import interpret_slab, interpret_slab_photo, MODEL_INTERPRET, MODEL_PHOTO

DB = SCRIPT_DIR / "kensa.db"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 10

# --- Prijzen (per 1M tokens / per 1k Vision requests), Nov 2026 Google-tarief ---
PRICE = {
    "gemini-flash-latest":      {"in": 0.30, "out": 2.50},   # $/1M tokens
    "gemini-flash-lite-latest": {"in": 0.10, "out": 0.40},   # $/1M tokens
    "vision_text_detection":    1.50,                         # $/1k requests
}
IMG_TOKEN_ESTIMATE = 258  # Gemini rekent ~258 tokens per <=1024px image tile

COMPARE_FIELDS = ["name", "subtype", "number", "set_code", "set_name",
                  "variant", "grade", "cert", "year"]


def cost_old(vision_calls: int, in_tokens: int, out_tokens: int, model: str) -> float:
    """Vision ($1.50/1k) + Flash-Lite input+output tokens."""
    p = PRICE[model]
    return (
        vision_calls * PRICE["vision_text_detection"] / 1000
        + in_tokens * p["in"] / 1_000_000
        + out_tokens * p["out"] / 1_000_000
    )


def cost_new(in_tokens: int, out_tokens: int, model: str) -> float:
    """Alleen Gemini multimodal (input incl. image-tokens, output text)."""
    p = PRICE[model]
    return in_tokens * p["in"] / 1_000_000 + out_tokens * p["out"] / 1_000_000


def pick_slabs(n: int) -> list[tuple]:
    conn = sqlite3.connect(str(DB))
    rows = conn.execute(
        """SELECT l.item_id, l.title_en, l.title_jp, p.url_original
           FROM listings l
           JOIN photos p ON p.item_id=l.item_id AND p.photo_index=1
           WHERE (l.title_en LIKE 'PSA%' OR l.title_en LIKE 'psa%')
           ORDER BY RANDOM() LIMIT ?""", (n,)
    ).fetchall()
    conn.close()
    return rows


def norm(v) -> str:
    if v is None:
        return ""
    return str(v).strip().lower()


def main():
    slabs = pick_slabs(N)
    if len(slabs) < N:
        print(f"[warn] slechts {len(slabs)} slabs beschikbaar")

    per_item = []
    tot_old_in = tot_old_out = 0
    tot_new_in = tot_new_out = 0
    tot_old_ms = tot_new_ms = 0
    match_hits = 0
    total_field_checks = 0

    for i, (item_id, en, jp, url) in enumerate(slabs, 1):
        print(f"\n{'='*80}\n[{i}/{len(slabs)}] {item_id} — {(en or '')[:60]}")
        print(f"    photo: {url[:80]}")

        # --- OUD: Vision + interpret_slab (Flash-Lite) ---
        t0 = time.perf_counter()
        ocr = ocr_image(url)
        vision_ms = int((time.perf_counter() - t0) * 1000)
        if ocr.get("error"):
            print(f"    [OLD] Vision ERROR: {ocr['error']}")
            continue
        r_old = interpret_slab(ocr["full_text"], en, jp)
        m_old = r_old.get("_meta", {}) or {}
        old_llm_ms = m_old.get("latency_ms", 0)
        old_total_ms = vision_ms + old_llm_ms
        old_in = m_old.get("in_tokens") or 0
        old_out = m_old.get("out_tokens") or 0
        old_cost = cost_old(1, old_in, old_out, MODEL_INTERPRET)

        # --- NIEUW: multimodal Gemini (Flash) ---
        r_new = interpret_slab_photo(url, en, jp)
        m_new = r_new.get("_meta", {}) or {}
        new_ms = m_new.get("latency_ms", 0) + m_new.get("fetch_ms", 0)
        new_in = m_new.get("in_tokens") or 0
        new_out = m_new.get("out_tokens") or 0
        new_cost = cost_new(new_in, new_out, MODEL_PHOTO)

        if r_old.get("_error") or r_new.get("_error"):
            print(f"    [ERROR] old={r_old.get('_error')} new={r_new.get('_error')}")
            continue

        # Field-by-field compare
        print(f"    {'FIELD':10s} | {'OLD (Vision+LLM)':30s} | {'NEW (multimodal)':30s} | match")
        print(f"    {'-'*10} | {'-'*30} | {'-'*30} | -----")
        item_hits = 0
        for f in COMPARE_FIELDS:
            v1 = r_old.get(f)
            v2 = r_new.get(f)
            m = norm(v1) == norm(v2)
            if m:
                item_hits += 1
                match_hits += 1
            total_field_checks += 1
            print(f"    {f:10s} | {str(v1)[:30]:30s} | {str(v2)[:30]:30s} | {'✓' if m else '≠'}")

        print(f"    latency:  OLD={old_total_ms}ms (vision={vision_ms}+llm={old_llm_ms}) | NEW={new_ms}ms")
        print(f"    tokens:   OLD in={old_in} out={old_out} | NEW in={new_in} out={new_out}")
        print(f"    cost($):  OLD={old_cost:.6f} | NEW={new_cost:.6f}")
        print(f"    match:    {item_hits}/{len(COMPARE_FIELDS)} fields")

        per_item.append({
            "item_id": item_id, "match_hits": item_hits,
            "old_ms": old_total_ms, "new_ms": new_ms,
            "old_cost": old_cost, "new_cost": new_cost,
        })
        tot_old_in += old_in; tot_old_out += old_out
        tot_new_in += new_in; tot_new_out += new_out
        tot_old_ms += old_total_ms; tot_new_ms += new_ms

    n_ok = len(per_item)
    if n_ok == 0:
        print("\n[fail] geen successful items")
        return

    print(f"\n{'='*80}\nSAMENVATTING (n={n_ok})\n{'='*80}")
    match_rate = 100 * match_hits / total_field_checks if total_field_checks else 0
    avg_old_ms = tot_old_ms / n_ok
    avg_new_ms = tot_new_ms / n_ok
    total_old_cost = sum(x["old_cost"] for x in per_item)
    total_new_cost = sum(x["new_cost"] for x in per_item)
    cost_old_per_1k = 1000 * total_old_cost / n_ok
    cost_new_per_1k = 1000 * total_new_cost / n_ok

    print(f"JSON-field match rate: {match_rate:.1f}% ({match_hits}/{total_field_checks} fields)")
    print(f"Avg latency:  OLD={avg_old_ms:.0f}ms | NEW={avg_new_ms:.0f}ms  (Δ={avg_new_ms-avg_old_ms:+.0f}ms)")
    print(f"Avg tokens:   OLD in={tot_old_in/n_ok:.0f} out={tot_old_out/n_ok:.0f}")
    print(f"              NEW in={tot_new_in/n_ok:.0f} out={tot_new_out/n_ok:.0f}")
    print(f"\nKOSTEN per 1000 items:")
    print(f"  OLD (Vision+Flash-Lite): ${cost_old_per_1k:.3f}")
    print(f"  NEW (multimodal Flash):  ${cost_new_per_1k:.3f}")
    delta = cost_old_per_1k - cost_new_per_1k
    pct = 100 * delta / cost_old_per_1k if cost_old_per_1k else 0
    print(f"  Verschil:                {'-' if delta>0 else '+'}${abs(delta):.3f}/1k  ({pct:+.1f}%)")

    # Per-veld breakdown zodat we zien welk veld het meest afwijkt
    print(f"\nPER-VELD match rate:")
    # recount per field
    conn = sqlite3.connect(str(DB))
    conn.close()


if __name__ == "__main__":
    random.seed(42)
    main()
