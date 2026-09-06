# _meta.md — refactor-split van `_run_ebay_phase`

Bron: `agents/kensa/analyze.py::_run_ebay_phase` (regel 464-605, CC = **53**).
Doel-file: `agents/kensa/analyze/ebay_phase.py`.
Alle nieuwe sub-functies < CC 22, orkestrator `run_ebay_phase_v2` is signature-identiek
en levert dezelfde return / DB-writes / HTTP-call-volgorde als het origineel.

## Wat elke functie doet (2 regels)

| Functie | Wat |
|---|---|
| `_ebay_guard(slab, do_ebay)` | Toegangs-check: `do_ebay` waar én `slab.status == 'pass'`. Orkestrator vertaalt False-scenario's naar `None` of skip-dict. |
| `_ebay_build_query(slab, listing, llm_data, verbose)` | Bouwt query via LLM of regex-`build_query`; regelt pokemon-override, grade-vereiste en vaagheid-gate. Returnt `(query, source, skip_result)`. |
| `_ebay_cache_check(slab, llm_data, query, verbose)` | Controleert eerst identiteit-cache (`price_cache`, 3d) en dan query-cache (`analysis`, 24u). Returnt `(card_key, cached_result_or_none)`. |
| `_ebay_fetch_and_filter(query, llm_data, verbose)` | Live `ebay_search`, regex-filter, `query_is_very_specific` fast-path, optionele LLM-judge (via `_apply_llm_judge`). |
| `_apply_llm_judge(...)` | Losgesplitste LLM-judge sub-stap zodat F4's CC laag blijft. Handelt B13 + B14 af. |
| `_ebay_persist_and_stats(...)` | Bouwt output-dict, upsert naar `price_cache` (SQLite + supabase-sync) mits `card_key` én geen fetch-error. |
| `run_ebay_phase_v2(...)` | Orkestrator; roept F1-F5 sequentieel aan en propageert de juiste short-circuits. |

## Baseline branch-mapping (B1..B16 → sub-functie)

| Branch | Regel-orig | Woont in |
|---|---|---|
| B1 (`do_ebay=False`) | 471 | `_ebay_guard` + orkestrator-short-circuit |
| B2 (`slab.status != 'pass'`) | 473 | `_ebay_guard` + orkestrator-short-circuit |
| B3 (pokemon-override uit titel) | 481-490 | `_ebay_build_query` |
| B4 (LLM ebay_query wint) | 495-499 | `_ebay_build_query` |
| B5 (regex build_query + grade-check) | 501-514 | `_ebay_build_query` |
| B6 (build_query leeg) | 515 | `_ebay_build_query` |
| B7 (vaagheid-gate `query_is_specific`) | 520-524 | `_ebay_build_query` |
| B8 (identiteit-cache hit) | 527-537 | `_ebay_cache_check` |
| B9 (query-cache hit `analysis`) | 539-543 | `_ebay_cache_check` |
| B10 (live `ebay_search`) | 548 | `_ebay_fetch_and_filter` |
| B11 (regex `filter_ebay_sales`) | 550 | `_ebay_fetch_and_filter` |
| B12 (very-specific → judge skip) | 559-561 | `_ebay_fetch_and_filter` |
| B13 (LLM-judge override) | 562-579 | `_apply_llm_judge` |
| B14 (LLM-judge error → regex-fallback) | 580-581 | `_apply_llm_judge` |
| B15 (`compute_price_stats`) | 583 | `_ebay_persist_and_stats` |
| B16 (`price_cache` upsert) | 599-604 | `_ebay_persist_and_stats` |

## Radon CC per functie (na split)

```
_ebay_guard                    A (3)
_ebay_build_query              C (17)
_ebay_cache_check              B (10)
_ebay_fetch_and_filter         B (9)
_apply_llm_judge               C (14)   # extra helper voor F4
_ebay_persist_and_stats        A (5)
run_ebay_phase_v2              B (6)
```

Meegekopieerde helpers (uit analyze.py — voor "losstaand werken"):

```
_pokemon_en_lookup             A (4)
_build_card_key                C (19)
_price_cache_get               A (2)
_cache_is_fresh                A (4)
_price_cache_upsert_ebay       A (2)
_cached_ebay_for_query         A (4)
```

Alle functies < CC 22. Origineel was CC 53.

## Wat gekopieerd vs. geïmporteerd

**Direct geïmporteerd (uit dezelfde modules als analyze.py):**
- `_extract_slab_pokemon_en`, `_pokemon_in_text`, `_pokedex` — `check_title`
- `filter_ebay_sales`, `query_is_specific`, `query_is_very_specific` — `ebay_filter`
- `ebay_search` — `ebay_lastsold`
- `llm_judge_sales` — `llm_client`
- `compute_price_stats` — `price_stats`
- `build_query` — `query_builder`
- `now_iso` — `storage`

**Gekopieerd uit `analyze.py` (LOSSTAAND-eis):**
- Constanten `DB_PATH`, `EBAY_CACHE_HOURS`, `PRICE_CACHE_DAYS`
- Helpers `_pokemon_en_lookup`, `_build_card_key`, `_price_cache_get`,
  `_cache_is_fresh`, `_price_cache_upsert_ebay`, `_cached_ebay_for_query`

Geen `from analyze import ...` — zie complicaties.

## Complicaties

1. **`analyze/` shadowt `analyze.py`.** In Python 3 wint een package (dir met
   `__init__.py`) van een gelijknamig `.py`-bestand in dezelfde parent. Zolang
   dit een refactor-DRAFT is en `run_ebay_phase_v2` nergens geïmporteerd wordt,
   is er geen runtime-impact. Wanneer de nieuwe orkestrator effectief gebruikt
   gaat worden, moet ofwel `analyze/__init__.py` alle bestaande exports van
   `analyze.py` herexporteren, ofwel de subdir hernoemd worden (bv. naar
   `analyze_split/`) om shadow-conflict te voorkomen. **Bestaande call-sites
   (`from analyze import DB_PATH, analyze_score_only`) zijn niet aangepast per
   opdracht.**

2. **F4 was net iets te breed voor CC < 22 in één functie.** De LLM-judge sub-stap
   is uitgesplitst naar `_apply_llm_judge` — geen extra fase, geen semantiek-drift,
   puur een read-verbetering. F4 zelf zakt daarmee naar CC 9.

3. **Spec-signature loose interpretation.** De opdracht schreef `F4 -> tuple[list, str]`
   maar F5 heeft ook `result.fetched_at`, `result.error` en `filter_meta` nodig
   om exact het originele output-dict te bouwen. F4 retourneert daarom
   `(result, filtered_sales, filter_meta)` — genoeg data om `sales_raw_count`,
   `fetched_at`, `error` en alle filter-metrics identiek te reproduceren.

4. **F1-signature `bool`.** Omdat de guard alleen True/False geeft, doet de
   orkestrator zelf de skip-return-vorm (`None` vs skip-dict). Dit houdt guard
   simpel (CC 3) en de orkestrator overzichtelijk.

5. **Volgorde-behoud gegarandeerd.** Sub-functies worden in F1→F2→F3→F4→F5 orde
   aangeroepen. `_price_cache_upsert_ebay` doet nog steeds eerst SQLite-INSERT,
   dan `supabase_sync.sync_price_cache_ebay` in try/except — precies zoals in
   analyze.py.

## Verify

```
python3 -m py_compile agents/kensa/analyze/ebay_phase.py    # EXIT 0
python3 -m radon cc -s -a agents/kensa/analyze/ebay_phase.py # alle < 22
```
