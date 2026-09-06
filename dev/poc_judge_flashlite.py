#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""PoC — vergelijk judge_sales met Flash vs Flash-Lite.

Draai:  ./poc_judge_flashlite.py [N]     # N = aantal cases (default 10)

Aanpak:
- Pak N recente 'llm-judge'-cases uit price_cache (dat zijn de queries waar de
  huidige pipeline ook echt judge_sales voor aanriep, dus representatief).
- Fetch raw eBay-sales opnieuw (15 stuks) — dezelfde input als productie.
- Run judge_sales twee keer: één met Flash (baseline) en één met Flash-Lite.
- Meet keep-overlap, latency, kosten. Print reasons voor visuele check.
"""
import json, sqlite3, sys, time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from ebay_lastsold import search as ebay_search
import llm_client

DB = SCRIPT_DIR / "kensa.db"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 10

PRICE = {
    "gemini-flash-latest":      {"in": 0.30, "out": 2.50},   # $/1M tokens
    "gemini-flash-lite-latest": {"in": 0.10, "out": 0.40},
}


def cost(model: str, in_tok: int, out_tok: int) -> float:
    p = PRICE[model]
    return in_tok * p["in"] / 1_000_000 + out_tok * p["out"] / 1_000_000


def pick_queries(n: int) -> list[str]:
    """Query's waar judge_sales in productie ook echt op aansloeg."""
    conn = sqlite3.connect(str(DB))
    rows = conn.execute(
        """SELECT DISTINCT ebay_query FROM price_cache
           WHERE ebay_result_json LIKE '%"filter_source": "llm"%'
             AND ebay_query IS NOT NULL AND ebay_query != ''
           ORDER BY ebay_fetched_at DESC LIMIT ?""", (n * 3,)
    ).fetchall()
    conn.close()
    seen, out = set(), []
    for (q,) in rows:
        if q in seen:
            continue
        seen.add(q)
        out.append(q)
        if len(out) >= n:
            break
    return out


def run_judge(model: str, query: str, sales: list[dict]) -> dict:
    """Force een specifiek model via monkey-patch van MODEL_JUDGE."""
    orig = llm_client.MODEL_JUDGE
    llm_client.MODEL_JUDGE = model
    try:
        return llm_client.judge_sales(query, sales, context=None, verbose=False)
    finally:
        llm_client.MODEL_JUDGE = orig


def keep_set(res: dict) -> set[int]:
    if not res or "decisions" not in res:
        return set()
    return {d["i"] for d in res["decisions"] if d.get("keep")}


def main():
    queries = pick_queries(N)
    if not queries:
        print("[fail] geen queries gevonden in price_cache")
        return
    print(f"[info] {len(queries)} query's opgehaald uit price_cache")

    tot_flash_in = tot_flash_out = 0
    tot_lite_in = tot_lite_out = 0
    tot_flash_ms = tot_lite_ms = 0
    tot_overlap_num = tot_overlap_den = 0
    per_case = []

    for i, q in enumerate(queries, 1):
        print(f"\n{'='*80}\n[{i}/{len(queries)}] {q}")
        t0 = time.perf_counter()
        r = ebay_search(q, limit=15, verbose=False)
        raw = r.get("sales") or []
        if not raw:
            print(f"    [skip] geen sales (err={r.get('error')})")
            continue
        print(f"    fetched {len(raw)} raw sales in {int((time.perf_counter()-t0)*1000)}ms")

        j_flash = run_judge("gemini-flash-latest", q, raw)
        if j_flash.get("_error"):
            print(f"    [flash err] {j_flash['_error']}")
            continue
        j_lite = run_judge("gemini-flash-lite-latest", q, raw)
        if j_lite.get("_error"):
            print(f"    [lite err] {j_lite['_error']}")
            continue

        m_f, m_l = j_flash["_meta"], j_lite["_meta"]
        keep_f, keep_l = keep_set(j_flash), keep_set(j_lite)

        # Jaccard-overlap op index-set: agreement over welke sales gehouden worden
        union = keep_f | keep_l
        inter = keep_f & keep_l
        # Exact-agreement: fractie sales waar beide dezelfde keep-beslissing nemen
        n = len(raw)
        agree = sum(1 for idx in range(n) if (idx in keep_f) == (idx in keep_l))
        agree_pct = 100 * agree / n if n else 0
        jaccard = 100 * len(inter) / len(union) if union else (100 if not keep_f and not keep_l else 0)

        print(f"    Flash keeps: {sorted(keep_f)}  ({len(keep_f)}/{n})")
        print(f"    Lite  keeps: {sorted(keep_l)}  ({len(keep_l)}/{n})")
        print(f"    agree: {agree}/{n} ({agree_pct:.0f}%)   jaccard: {jaccard:.0f}%")
        # Toon diff met reasons
        for idx in sorted((keep_f ^ keep_l)):
            in_flash = idx in keep_f
            r_f = next((d for d in j_flash["decisions"] if d.get("i") == idx), {})
            r_l = next((d for d in j_lite["decisions"] if d.get("i") == idx), {})
            title = (raw[idx].get("title") or "")[:70]
            marker_f = "keep" if in_flash else "REJECT"
            marker_l = "REJECT" if in_flash else "keep"
            print(f"      Δ [{idx}] {title}")
            print(f"          Flash={marker_f}: {(r_f.get('reason') or '')[:100]}")
            print(f"          Lite ={marker_l}: {(r_l.get('reason') or '')[:100]}")

        print(f"    latency: Flash={m_f['latency_ms']}ms  Lite={m_l['latency_ms']}ms")
        print(f"    tokens:  Flash in={m_f['in_tokens']} out={m_f['out_tokens']}  "
              f"Lite in={m_l['in_tokens']} out={m_l['out_tokens']}")

        tot_flash_in += m_f["in_tokens"] or 0
        tot_flash_out += m_f["out_tokens"] or 0
        tot_lite_in += m_l["in_tokens"] or 0
        tot_lite_out += m_l["out_tokens"] or 0
        tot_flash_ms += m_f["latency_ms"] or 0
        tot_lite_ms += m_l["latency_ms"] or 0
        tot_overlap_num += agree
        tot_overlap_den += n
        per_case.append({
            "query": q, "n": n, "keep_flash": sorted(keep_f), "keep_lite": sorted(keep_l),
            "agree": agree, "jaccard": jaccard,
            "flash_meta": m_f, "lite_meta": m_l,
            "diffs": [{
                "i": idx,
                "title": (raw[idx].get("title") or "")[:120],
                "flash_keep": idx in keep_f,
                "flash_reason": next((d.get("reason") for d in j_flash["decisions"] if d.get("i") == idx), None),
                "lite_reason": next((d.get("reason") for d in j_lite["decisions"] if d.get("i") == idx), None),
            } for idx in sorted(keep_f ^ keep_l)],
        })

    n_ok = len(per_case)
    if n_ok == 0:
        print("\n[fail] geen successful cases")
        return

    print(f"\n{'='*80}\nSAMENVATTING (n={n_ok})\n{'='*80}")
    overall_agree = 100 * tot_overlap_num / tot_overlap_den if tot_overlap_den else 0
    avg_jaccard = sum(x["jaccard"] for x in per_case) / n_ok
    avg_flash_ms = tot_flash_ms / n_ok
    avg_lite_ms = tot_lite_ms / n_ok
    cost_flash = cost("gemini-flash-latest", tot_flash_in, tot_flash_out)
    cost_lite = cost("gemini-flash-lite-latest", tot_lite_in, tot_lite_out)
    cost_flash_1k = 1000 * cost_flash / n_ok
    cost_lite_1k = 1000 * cost_lite / n_ok

    print(f"Overall keep-agreement:  {overall_agree:.1f}%  ({tot_overlap_num}/{tot_overlap_den} sale-beslissingen gelijk)")
    print(f"Avg Jaccard (per case):  {avg_jaccard:.1f}%")
    print(f"Avg latency: Flash={avg_flash_ms:.0f}ms  Lite={avg_lite_ms:.0f}ms  (Δ={avg_lite_ms-avg_flash_ms:+.0f}ms)")
    print(f"Kosten per 1000 judge_sales-calls:")
    print(f"  Flash:      ${cost_flash_1k:.4f}")
    print(f"  Flash-Lite: ${cost_lite_1k:.4f}")
    save_pct = 100 * (cost_flash_1k - cost_lite_1k) / cost_flash_1k if cost_flash_1k else 0
    print(f"  Besparing:  {save_pct:.1f}%")

    # Rapport-JSON
    out_path = SCRIPT_DIR.parent.parent / "memory" / "kensa-monitor" / "judge-flashlite-cases.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "n": n_ok,
        "overall_agree_pct": overall_agree,
        "avg_jaccard_pct": avg_jaccard,
        "avg_flash_ms": avg_flash_ms,
        "avg_lite_ms": avg_lite_ms,
        "cost_flash_per_1k": cost_flash_1k,
        "cost_lite_per_1k": cost_lite_1k,
        "cases": per_case,
    }, indent=2, ensure_ascii=False))
    print(f"\n[write] {out_path}")


if __name__ == "__main__":
    main()
