#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa — filter v4: query = single source of truth.

Tokeniseert de query en vergelijkt met verkoop-titels via match-percentage.

Pijplijn:
  1. LAAG 1 — query_is_specific(): naam-token + minstens 1 andere token vereist.
     Zo niet → skip eBay-fetch (query is te vaag, OCR heeft gefaald).
  2. LAAG 2 — filter_sales(sales, query): score elke sale op query-token match,
     cutoff ≥ 75%.
  3. LAAG 3 — instant-fails: PSA-grade mismatch, andere graders (BGS/CGC).
  4. LAAG 4 — prijs-outlier: > 3× median of < median÷3 (bij ≥3 kepts).

Terug: (kept_sales, meta) — kept is max 5 nieuwste, meta bevat kept/removed/reasons.
"""

import re
import unicodedata

OTHER_GRADERS = re.compile(r"\b(BGS|CGC|SGC|CGS|HGA|MPG)\b", re.IGNORECASE)
TITLE_GRADE_RE = re.compile(r"psa\s*(10|9\.5|9|8\.5|8|7|6|5|4|3|2|1)")
STOP_TOKENS = {
    "psa", "the", "and", "for", "with", "japanese", "english", "japan",
    "pokemon", "pokémon", "gem", "mint", "card", "trading", "holo",
}
# Subtypes/rarity/set-codes zijn geen "namen"
SUBTYPE_TOKENS = {
    "gx", "ex", "vmax", "vstar", "sar", "sr", "ur", "hr", "ar", "chr",
    "fa", "sa", "rr", "v", "sp", "svp", "smp", "xyp", "bwp", "dpp",
}

MATCH_CUTOFF_PCT = 75.0
MAX_KEEP = 5


def normalize(s: str) -> str:
    """lowercase + accent-strip + 'psa 10' → 'psa10' + 's-p' → 'sp'."""
    if not s:
        return ""
    n = unicodedata.normalize("NFKD", s)
    n = "".join(c for c in n if not unicodedata.combining(c)).lower()
    n = re.sub(r"psa\s*(\d+\.?\d*)", r"psa\1", n)
    n = re.sub(r"\b([a-z]{1,3})-([a-z0-9]{1,3})\b", r"\1\2", n)
    return n


def _tokenize(s: str) -> list[str]:
    n = normalize(s)
    raw = re.split(r"[^a-z0-9]+", n)
    return [t for t in raw if t and len(t) >= 2 and t not in STOP_TOKENS]


def query_tokens(query: str) -> list[str]:
    """Return unieke query-tokens. PSA-grade wordt gemarkeerd als '_grade_X'."""
    toks = _tokenize(query)
    out, seen = [], set()
    for t in toks:
        gm = re.fullmatch(r"psa(\d+\.?\d*)", t)
        key = "_grade_" + gm.group(1) if gm else t
        if key not in seen:
            out.append(key)
            seen.add(key)
    return out


def query_is_specific(query: str) -> tuple[bool, str]:
    """LAAG 1 — is de query specifiek genoeg om eBay te bevragen?

    Vereist:
      - minstens één naam-token: alfa ≥ 3 tekens, geen subtype/stopwoord/digit
      - minstens één andere token: nummer, set-code, of tweede naam-token
    """
    toks = query_tokens(query)
    name_tokens = [
        t for t in toks
        if not t.startswith("_grade_")
        and t.isalpha()
        and len(t) >= 3
        and t not in SUBTYPE_TOKENS
    ]
    other_tokens = [
        t for t in toks
        if not t.startswith("_grade_") and t not in name_tokens
    ]
    if not name_tokens:
        return False, "geen echte naam-token in query (OCR-fout of te vage input)"
    if not other_tokens:
        return False, "alleen naam — geen nummer/set/subtype om op te ankeren"
    return True, "ok"


def query_is_very_specific(query: str) -> tuple[bool, str]:
    """LAAG 2 — is de query zó scherp dat regex-filter voldoende is en LLM-judge overbodig?

    Vereist (ALLE):
      - minstens één naam-token (≥3 tekens, geen subtype)
      - minstens één nummer-token (bv '025' of '068/187')
      - minstens één subtype- of set-code-token (VMAX/GX/SAR/S8a-P/etc)
    Als deze 3 aspecten aanwezig zijn dan filtert de regex sold-titels betrouwbaar
    (naam + nummer + subtype = zeer unieke fingerprint), en hoeven we geen dure
    LLM-judge te doen om varianten te scheiden.
    """
    toks = query_tokens(query)
    name_tokens = [t for t in toks if not t.startswith("_grade_") and t.isalpha() and len(t) >= 3 and t not in SUBTYPE_TOKENS]
    number_tokens = [t for t in toks if any(ch.isdigit() for ch in t) and not t.startswith("_grade_")]
    subtype_tokens = [t for t in toks if t in SUBTYPE_TOKENS]
    if not name_tokens:
        return False, "geen naam-token"
    if not number_tokens:
        return False, "geen nummer-token"
    if not subtype_tokens:
        return False, "geen subtype/setcode-token"
    return True, f"name+number+subtype OK ({len(name_tokens)}+{len(number_tokens)}+{len(subtype_tokens)})"


def score_sale(sale_title: str, q_toks: list[str]) -> tuple[float, dict]:
    """Return (match_percentage, meta). meta bevat 'fail' bij instant-reject."""
    if not sale_title:
        return 0.0, {"fail": "lege titel"}
    if OTHER_GRADERS.search(sale_title):
        return 0.0, {"fail": "andere grader (niet PSA)"}

    title_norm = normalize(sale_title)
    title_toks = set(_tokenize(sale_title))

    tg = TITLE_GRADE_RE.search(title_norm)
    for qt in q_toks:
        if qt.startswith("_grade_") and tg:
            wanted = qt.split("_grade_")[1]
            if tg.group(1) != wanted:
                return 0.0, {"fail": f"grade psa{tg.group(1)} != query psa{wanted}"}

    if not q_toks:
        return 0.0, {"fail": "geen query-tokens"}

    matched, missed = [], []
    for qt in q_toks:
        if qt.startswith("_grade_"):
            wanted = qt.split("_grade_")[1]
            if re.search(rf"\bpsa{re.escape(wanted)}\b", title_norm):
                matched.append(qt)
            else:
                missed.append(qt)
        else:
            if qt in title_toks:
                matched.append(qt)
            else:
                missed.append(qt)
    pct = 100.0 * len(matched) / len(q_toks)
    return pct, {"matched": matched, "missed": missed, "total": len(q_toks), "pct": pct}


def filter_sales(sales: list[dict], query: str) -> tuple[list[dict], dict]:
    """LAAG 2+3+4 — filter en trim tot MAX_KEEP nieuwste."""
    if not sales:
        return [], {"kept": 0, "removed": 0, "removed_reasons": []}

    q_toks = query_tokens(query)
    removed_reasons: list[str] = []
    survivors: list[dict] = []

    for s in sales:
        pct, meta = score_sale(s.get("title") or "", q_toks)
        if "fail" in meta:
            removed_reasons.append(f"'{(s.get('title') or '')[:60]}' → {meta['fail']}")
            continue
        if pct < MATCH_CUTOFF_PCT:
            missed = meta.get("missed") or []
            removed_reasons.append(
                f"'{(s.get('title') or '')[:60]}' → match {pct:.0f}% (mist {missed})"
            )
            continue
        survivors.append(s)

    # LAAG 4 — prijs-outlier
    if len(survivors) >= 3:
        prices = sorted([s["price_usd"] for s in survivors if s.get("price_usd")])
        median = prices[len(prices) // 2]
        clean: list[dict] = []
        for s in survivors:
            p = s.get("price_usd") or 0
            if median and (p > median * 3 or p < median / 3):
                removed_reasons.append(
                    f"'{(s.get('title') or '')[:60]}' → prijs ${p:.2f} outlier (median ${median:.2f})"
                )
                continue
            clean.append(s)
        survivors = clean

    kept = survivors[:MAX_KEEP]
    trimmed = max(0, len(survivors) - MAX_KEEP)
    return kept, {
        "kept": len(kept),
        "removed": len(sales) - len(survivors),
        "removed_reasons": removed_reasons,
        "trimmed": trimmed,
    }


if __name__ == "__main__":
    # Gate-tests
    for q in [
        "Rayquaza PSA 10",
        "GX 190 PSA 10",
        "Muk 034 PSA 10",
        "Pikachu VMAX 123 S-P PSA 10",
        "PSA 10",
    ]:
        ok, why = query_is_specific(q)
        print(f"gate {q!r}: {'OK' if ok else 'BLOCK'}  {why}")
    print()
    # Filter-test
    demo_query = "Pikachu 073 PSA 10"
    demo_sales = [
        {"price_usd": 841.0,  "title": "PSA 10 Ash Pikachu 073 SM-P Red Promo Pokemon Card Japanese 2017"},
        {"price_usd": 133.6,  "title": "2022 POKEMON JAPANESE SWORD & SHIELD DARK PHANTASMA #073 FULL ART/PIKACHU PSA 10"},
        {"price_usd": 185.0,  "title": "PIKACHU 2022 POKEMON JPN DARK PHANTASMA FA CHARACTER RARE #073/071 PSA 10"},
        {"price_usd": 155.5,  "title": "2022 POKEMON JAPANESE SWORD & SHIELD DARK PHANTASMA #073 FULL ART/PIKACHU PSA 10"},
        {"price_usd": 239.7,  "title": "FA/PIKACHU DARK PHANTASMA POKEMON JAP SWSH DARK PHANTASMA 2022 PSA 10"},
    ]
    kept, meta = filter_sales(demo_sales, demo_query)
    print(f"query={demo_query!r}  kept {meta['kept']}/{len(demo_sales)}")
    for s in kept:
        print(f"  ${s['price_usd']}: {s['title']}")
    for r in meta["removed_reasons"]:
        print(f"  ✗ {r}")
