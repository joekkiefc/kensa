# ANALYSE-KENSA.md — Volledige read-only analyse van het Kensa-project

**v2 — 2026-09-05, met cutover-context**

**Doel:** wie Kensa nooit heeft gezien, snapt na dit document (a) wat het doet,
(b) hoe het werkt, (c) waar de structurele complexiteit zit en (d) hoe je het
stap voor stap opruimt zonder iets kapot te maken.

**Scope-uitgangspunt:** dit is een read-only analyse. Er is niets veranderd,
verwijderd of hernoemd. Alle voorstellen zijn suggesties.

**Belangrijke context — cutover Supabase (Tommy, 5 sep 2026):**
- De dual-storage (SQLite + Supabase) is BEWUST en TIJDELIJK. De migratie naar
  Supabase is net afgerond en dual mode draait ter verificatie.
- **Morgen (6 sep) wordt SQLite verwijderd** en draait alles alleen op Supabase.
- Extra logging (`pipeline_supabase_reads.log`, `cutover_cache_state.json`, e.d.)
  en de cutover-monitor-scripts horen bij deze verificatie en zijn tijdelijk.
- Doelarchitectuur (fases 5-8 én de aparte sectie **"Na de cutover"**) gaat
  uit van de toekomstige situatie: **Supabase-only**.

**Wat is er veranderd t.o.v. v1 (2026-09-05 19:38):**
1. Storage-chaos herschreven als "cutover-fase, niet structureel". Aparte
   sectie "Na de cutover" telt op wat morgen concreet overbodig wordt.
2. Doelarchitectuur (fases 5, 7, 8) uitgaat van Supabase-only i.p.v.
   dual-write als eindstation.
3. Env-vars: crontab gebruikt de globale `KENSA_STORAGE=dual` als aanhoudend
   signaal, plus per-cron `KENSA_READ_STORAGE=supabase` en
   `KENSA_WRITE_STORAGE=dual` — dat is nu correct beschreven (v1 had het
   als per-cron-verschillen).
4. `storage_supabase_legacy.py` is per de cutover NIET legacy — het bevat
   álle Supabase-read-functies die de package re-exporteert. Naam is
   misleidend maar functie is centraal.
5. Migratiestappenplan begint nu met "SQLite-verwijderen en dispatch-code
   opruimen" als natuurlijke P0-stap, ná Tommy's cutover.
6. Verscherpte cijfers en referenties (regelnummers, functie-lijsten).

---

## Samenvatting in 10 zinnen

1. Kensa scant Japanse Pokémon-slabs op Buyee (Mercari + PayPay Fleamarket)
   op onder-de-marktprijs koopjes: scrape → detail → OCR + AI → prijs-check
   (eBay + Cardmarket) → confidence-score + winst-berekening.
2. De pipeline draait autonoom via **17 aparte cron-taken** (elk eigen flock)
   met 3 workers (OCR, cache-hit, live-eBay) die parallel lopen.
3. De data staat nu tijdelijk **in twee databases**: SQLite (`kensa.db`, 1.0 GB)
   én Supabase — dual mode ter verificatie. **Vanaf morgen (6 sep):
   Supabase-only, SQLite wordt verwijderd.**
4. Cron-scripts sturen elk hun eigen `KENSA_READ_STORAGE=supabase` en
   `KENSA_WRITE_STORAGE=dual` — de reads staan al op Supabase, writes gaan
   nog naar allebei. Dat is de laatste stap vóór cutover.
5. De pipeline zelf (scrape → detail → OCR → prijs → beslissing) is logisch;
   de code is fragmentatief door een refactor-golf van 31 aug – 1 sep die
   bijna elk groot bestand splitste in een `<naam>_split/`-package.
6. Grootste structurele complexiteit blijft ná de cutover: **8 refactor-splits
   + 15 fixture-mappen + 3 legacy-archieven + verspreide helpers** — dat is
   niet-cutover-gerelateerd en verdient opruiming.
7. Er zijn drie parallelle detail-fetchers (mercapi-API, Buyee-scraper via
   Chromium, Buyee-via-NL-proxy) — snelheids-optimalisatie, wél lastig te
   volgen omdat de router in een shellscript zit.
8. Discord-integratie is minimaal: `alerts.py` voor nieuwe listings die
   matchen met user-alerts, en `sync_retry_drain.py` voor dead-letter-alerts.
9. HTTP: één minimale Flask-service op poort 8898 (`cm_queue_api.py`) waar
   de Windows Cardmarket-worker mee praat.
10. Grote schoonmaakwinst zit in **drie plaatsen tegelijk**:
    (a) SQLite-verwijdering + dispatch-branches opruimen (post-cutover),
    (b) alias-stubs samenvoegen met hun `_split/`-packages,
    (c) fixtures/logs/archieven onder één dak.

---

## FASE 1 — Volledige kaart

### 1.1 Wat Kensa doet (in 1 zin)

Vind Japanse PSA-gegradeerde Pokémon-slabs op Buyee die goedkoper zijn dan hun
eBay/Cardmarket-verkoopprijs, en presenteer die als "deals" met een
confidence-score per kaart.

### 1.2 Entrypoints — wat draait er automatisch

Uit `crontab -l` (Kensa-blok):

| Interval | Script | Wat het doet |
|---|---|---|
| `*/15` | `cron_scrape.sh` | Buyee-zoekpagina's scrapen (Mercari + PayPay), nieuwe listings + Discord-alert |
| `*/10` | `cron_detail.sh` | Detail: mercapi → mercapi-recheck → Buyee-scrape → proxy (bij backlog>150) |
| `*/3`  | `cron_detail_mercapi.sh` | Alleen mercapi (Mercari-API, snel — parallel aan cron_detail) |
| `*/2`  | `cron_worker_ocr.sh` | Slab-OCR + AI-lezing (worker_ocr, 6 threads) |
| `* * * * *` | `cron_worker_cache.sh` | Score-only voor items met verse eBay-cache (worker_cache, 4 threads) |
| `*/8`  | `cron_worker_ebay.sh` | Live eBay-fetch + score voor cache-misses (worker_ebay, 1 thread) |
| `0 4`  | `cron_cleanup_raw_pages.sh` | Retentie: raw_pages ouder dan 3 dagen weg |
| `*/10` | `scripts/monitor-cm-queue.py` | Alert #algemeen als CM-queue >15 pending |
| `*/2`  | `sync_retry_drain.py` | Gefaalde Supabase-writes opnieuw (backoff, dead-letter) |
| `5 * *` | `scripts/sync-drift-check.py` | Detecteert Pi↔Supabase-drift binnen 1 uur — **cutover-only** |
| `30 3` | `dedup_listings.py --apply` | Dagelijkse dedup: max 20 per card_key, 28d retentie |
| `*/30` | `stale_lock_cleanup.py` | Worker-locks >20 min lossmaken (Pi + Supabase) |
| `*/15` | `detail_dead_marker.py` | Kapotte PayPay-listings markeren als dead |
| `11 *` | `detail_stuck_detector.py` | 6u zonder detail → retry-flag zetten |
| `0 */12` | `scripts/report-supabase-reads.py` | Rapport Supabase read-volume — **cutover-only** |
| `0 */12` | `scripts/monitor-cutover-cache.py` | Monitort cache-worker cutover — **cutover-only** |

Bovenaan de crontab staat één globale `KENSA_STORAGE=dual`; die wordt door
`supabase_sync.py` gelezen als "geef sync-fouten door aan de retry-queue".
De cron-scripts zetten daarnaast per-cron hun eigen
`KENSA_READ_STORAGE=supabase` en `KENSA_WRITE_STORAGE=dual`.

**Uitgeschakeld** (comment in crontab): `cron_analyze.sh` — vervangen door de
drie workers (OCR / cache / eBay).

**Geen webapp meer.** De oude Flask dashboard staat in `_legacy_removed_2026-09-05/`.
Enige HTTP-eindpunt is `cm_queue_api.py` (poort 8898), waar de Windows
Cardmarket-scraper URLs bij ophaalt en resultaten postet. Discord-alerts gaan
via `alerts.py` (nieuwe listing) en `sync_retry_drain.py` (dead-letter).

### 1.3 Logische groepen — 8 groepen op basis van wat de code echt doet

**A. Binnenhalen (scraping/fetching)**
- `scrape_buyee.py` — zoekpagina scrapen (Chromium/Scrapling stealth)
- `parse_listing_split.py` — parse-helper (v2, refactor-fixture-ready)
- `fetch_detail.py` — Buyee-detail via Chromium
- `parse_detail_split/mercari_parser.py` — parse-helper voor detail
- `fetch_detail_mercapi.py` — Mercari-API-fetch (mercapi lib, ~1.5s/item)
- `fetch_detail_proxy.py` — Buyee via NL residential proxy (BoilingProxies)
- `migrate_from_json.py` — `last_scrape.json` → DB + Discord-alert-trigger
- `socks5_bridge.py` — HTTP-CONNECT → NordVPN SOCKS5-auth bridge (handmatig)

**B. Slab lezen (OCR + AI)**
- `vision.py` + `vision_split/psa_parser.py` + `psa_name_set_split.py` — Vision-OCR + PSA-label parse
- `check_slab.py` — orchestreert 2-3 foto's → OCR → slab-velden
- `check_slab_hybrid.py` — Gemini multimodal → Vision fallback (opt-in `KENSA_USE_MULTIMODAL=1`)
- `crop_psa_label.py` — OpenCV rode-rand crop
- `ocr_router.py` — hybride lokale-OCR pipeline (Tailscale-endpoint); alleen `is_multi_slab_lot` wordt echt gebruikt door `check_slab_hybrid.py`
- `llm_client.py` + `interpret_slab_split/gemini_interpret.py` + `llm_client_split/interp_photo.py` + `judge_sales_split/gemini_judge.py` — Gemini-client + interpret/judge/multimodal
- `translate.py` — JP → EN titel-vertaling

**C. Prijzen ophalen**
- `ebay_lastsold.py` + `parse_card_split.py` — eBay sold-listings scrape
- `ebay_login.py` — eenmalige eBay-login voor cookies
- `ebay_filter.py` — query-match + PSA-grade + outlier-filter
- `query_builder.py` — eBay-query bouw uit slab-velden
- `price_stats.py` — last/avg3/avg5 statistieken
- `supabase_client.py` — Cardmarket-URL-lookup in Supabase `cards` tabel

**D. Vergelijken & beslissen**
- `check_title.py` + `check_title_split/title_match.py` — titel ↔ slab
- `check_desc.py` + `check_desc_split/desc_match.py` — beschrijving ↔ slab
- `score.py` — confidence-score-formule (base 80 + adjustments)
- `roi.py` — Buyee-prijs → landed cost → verkoop → winst_eur / winst_pct
- `analyze.py` — orchestrator-stub met aliases naar `analyze_split/`
- `analyze_split/analyze_full.py` — echte "analyze()" (8 fases F1-F8)
- `analyze_split/score_only.py` — score-only mode (voor cache/eBay-workers)
- `analyze_split/ebay_phase.py` — eBay-guard/query/cache/fetch/persist
- `analyze_split/ebay_query_split.py` — query-bouw uit slab of LLM
- `analyze_split/enqueue_cardmarket.py` — CM-URL-lookup + enqueue

**E. Workers (parallel)**
- `worker_ocr.py` — batch OCR+LLM (6 threads, elke 2 min)
- `worker_cache.py` — batch score-only voor cache-hits (4 threads, elke min)
- `worker_ebay.py` — batch score-only met live eBay (1 thread, elke 8 min)
- `worker_claim.py` — atomair lock-mechanisme (voorkomt dubbele claim)

**F. Storage / data-laag**
- `storage.py` — SQLite-schema + upserts + **3-weg dispatch** (sqlite / dual / supabase)
- `storage_supabase/` — package: `__init__.py` (facade), `_http.py` (REST-wrapper),
  `_logging.py`, `analysis.py`, `listings.py`, `photos.py`, `listings_seen.py`,
  `cert_sightings.py`, `cardmarket_queue.py`, `price_cache.py`
- `storage_supabase_legacy.py` — **álle read-functies** die de package
  re-exporteert (naam is misleidend — zie 4.1.B)
- `supabase_sync.py` — best-effort dual-write helper (sync_upsert_listing,
  sync_replace_analysis, sync_slab_status, sync_photo_urls, ...)
- `sync_retry.py` — SQLite-queue voor gefaalde Supabase-calls
- `sync_retry_drain.py` — cron-worker die de retry-queue leegt (exp. backoff,
  dead-letter na 6 pogingen + Discord-alert)
- `enable_fts5.py` — one-shot: FTS5 full-text index op titels
- `migrate_to_supabase.py` — one-shot bulk-migratie
- `backfill_cm_queue.py` — one-shot backfill CM-queue

**G. Onderhoud / monitors**
- `stale_lock_cleanup.py` — reset locks >20 min (Pi + Supabase)
- `detail_dead_marker.py` — PayPay-items met retry-flag + 30 min zonder detail → dead
- `detail_stuck_detector.py` — 6 u zonder detail → 1× retry-flag in `extra_json`
- `dedup_listings.py` — dagelijkse dedup (max 20 per card_key, 28d retentie)
- `scripts/monitor-cm-queue.py` — externe monitor (Discord bij >15 pending)
- `scripts/sync-drift-check.py` — Pi↔Supabase drift-detectie + auto-backfill
- `scripts/report-supabase-reads.py` — 15-min Supabase-read-rapport
- `scripts/monitor-cutover-cache.py` — cutover-verificatie cache-worker

**H. Randgevallen / externe integratie**
- `alerts.py` — Discord-alert bij nieuwe listing met matching user-alert
- `cm_queue_api.py` — Flask op :8898 (GET pending / POST result / POST enqueue)
- `windows-worker/` — losse Windows-scraper voor Cardmarket (draait niet op Pi)
- `dev/kensa_discord_commands.py` — Discord slash-commands (via symlink vanuit root)
- `spike_crop_gallery.py`, `llm_analyze.py`, `dev/*` — POCs & experimenten

### 1.4 Externe services die Kensa aanroept

| Service | Waar | Waarvoor |
|---|---|---|
| Buyee.jp (Mercari + PayPay Flea) | `scrape_buyee.py`, `fetch_detail.py`, `fetch_detail_proxy.py` | Zoeken + detail |
| Mercari API (mercapi) | `fetch_detail_mercapi.py` | Snelle Mercari-detail |
| Google Vision API | `vision.py` | OCR op foto's |
| Google Gemini | `llm_client.py` + splits | Slab-interpretatie / eBay-judge / multimodal |
| Google Translate (gratis endpoint) | `translate.py` | JP→EN titels |
| eBay.com (ingelogd) | `ebay_lastsold.py`, `ebay_login.py` | Sold-listings scrape |
| Cardmarket (via Windows-worker) | `cm_queue_api.py` + `windows-worker/` | 3 goedkoopste PSA10 |
| Supabase (PostgREST, schema `kensa`) | `supabase_client.py`, `supabase_sync.py`, `storage_supabase/*`, `storage_supabase_legacy.py` | Data-laag |
| Frankfurter API | `roi.py` | USD/EUR wisselkoers |
| BoilingProxies (NL residential) | `fetch_detail_proxy.py`, `ebay_lastsold.py` | IP-diversificatie |
| NordVPN SOCKS5 | `socks5_bridge.py` | Extra proxy-optie |
| Lokale GOT-OCR (Tailscale 100.125.116.37:8898) | `ocr_router.py` | Lokale OCR-fallback |
| Discord webhooks / message-tool | `alerts.py`, `sync_retry_drain.py`, `scripts/monitor-*.py` | Alerts |

### 1.5 Datastroom in het kort

**In:** Buyee zoek → listings + photos tabel (Pi + Supabase). Buyee detail →
photos-URLs + `detail_scraped_at`. Foto-bytes worden pas gedownload als
`check_slab` ze nodig heeft (content-addressable in `photos/by_hash/`).

**Verwerkt:** foto's → Vision/Gemini → `analysis` tabel als "trap"-rijen
(`slab_ocr`, `llm_slab`, `title_match`, `desc_match`, `ebay_prices`, `roi`,
`summary`). Cache in `llm_slab_cache` (hash, 30d) en `price_cache` (card_key,
3d).

**Extern verrijkt:** slab-data → eBay-query → live sales → stats → cache.
Slab-data → CM-URL-lookup → CM-queue → Windows-worker → CM-result → cache.

**Beslissing:** `roi.compute()` + `score.compute_score()` → `summary`-trap
met `verdict` (`geverifieerd` ≥90, `twijfel` 50-89, `onbetrouwbaar` <50) en
`roi_avg3_pct`.

**Uit:** Supabase (dashboards lezen hier); Discord-alerts bij nieuwe matches
en dead-letter-fouten.

### 1.6 Waar zit "de beslissing"?

Drie geconcentreerde plaatsen:
- **eBay-fase** — `analyze_split/ebay_phase.py`: query, cache-check, live
  fetch, filter, stats (last/avg3/avg5).
- **ROI** — `roi.py`: Buyee-prijs → landed cost (2.7% + €4.55) → verkoop
  (avg × USD/EUR) → winst_eur + winst_pct.
- **Score** — `score.py`: base 80 (slab-OCR pass) + adjustments voor
  titel-match / desc-match; verdict-label.

### 1.7 Helpers vs businesslogica vs infrastructuur

- **Pure helpers**: `translate`, `crop_psa_label`, `socks5_bridge`,
  `ebay_login`, `enable_fts5`, `migrate_from_json`, `migrate_to_supabase`,
  `backfill_cm_queue`, `stale_lock_cleanup`, `dedup_listings`, `sync_retry*`
- **Businesslogica**: `check_slab*`, `check_title`, `check_desc`, `score`,
  `roi`, `query_builder`, `ebay_filter`, `alerts`, `enqueue_cardmarket`,
  alle `analyze_split/*`
- **Infrastructuur (mag niet weten wat "een kaart" is)**: `storage*`,
  `storage_supabase/*`, `storage_supabase_legacy`, `supabase_sync`,
  `worker_claim`, `cm_queue_api`

### 1.8 Circulaire imports

`analyze_split/*` lost circulaire imports op met **lazy-imports** binnen
functies. Voorbeeld: `_af_load_and_guard` doet `from analyze_split.score_only
import _read_via_supabase` binnen de functie. Dat is een symptoom dat de
aliases in `analyze.py` en de code in `analyze_split/` een lastige knoop
vormen. Na het samenvoegen (P2.2) verdwijnt dit vanzelf.

---

## FASE 2 — Datastroom

### 2.1 In gewone taal (16 stappen)

1. **Zoeken** (elke 15 min) — script kijkt op Buyee's zoekpagina (Mercari +
   PayPay Fleamarket) naar de nieuwste PSA10 Pokémon-listings.
2. **Vertalen** — Japanse titel → Engels (Pokémon-namen via pokedex, rest
   via Google Translate).
3. **Opslaan als nieuw item** — nog niet in DB → status "new" → Discord-alert
   als het matcht met een user-alert.
4. **Detail-pagina ophalen** (elke 3–10 min) — Mercari-items via mercapi-API
   (snel, ~1.5s), PayPay-items via Buyee-scraper met Chromium. Bij backlog
   >150 springt de NL-proxy-versie bij.
5. **Foto's downloaden** (impliciet) — URLs staan in DB, bytes pas als OCR ze
   nodig heeft. Content-addressable opslag in `photos/by_hash/`.
6. **Slab lezen** (elke 2 min, `worker_ocr`) — 2-3 foto's per item →
   Vision-OCR (of Gemini multimodal als `KENSA_USE_MULTIMODAL=1`) → PSA-velden
   parsen (cert, grade, naam, nummer, set, jaar). Als OCR te weinig oplevert:
   Gemini erbij voor interpretatie.
7. **Card-key bouwen** — `pokemon:nummer:grade[:set_code]` als stabiele
   cache-sleutel: dezelfde fysieke kaart bij verschillende sellers krijgt
   dezelfde key.
8. **Cache-check** (elke minuut, `worker_cache`) — items met card-key + verse
   eBay-cache (<3 dagen) direct scoren zonder nieuwe eBay-fetch.
9. **eBay live** (elke 8 min, `worker_ebay`) — cache-miss → live scrape (~30-80s
   per item, 1 thread wegens Chromium-profile-lock) → filter → stats.
10. **Titel-match** — Engelse + Japanse titel vergelijken met slab-velden
    (pass/fail/skip).
11. **Beschrijving-match** — idem voor Japanse listing-beschrijving.
12. **Confidence-score** — base 80 (bij slab-OCR-pass) + adjustments →
    verdict (`geverifieerd` / `twijfel` / `onbetrouwbaar`).
13. **ROI** — landed cost + FX → winst_eur en winst_pct voor last/avg3/avg5.
14. **Cardmarket-lookup** — pokemon+nummer → Supabase `cards`-tabel → CM-URL
    in queue. Windows-worker pakt de queue op en post 3 goedkoopste PSA10
    terug via `cm_queue_api.py`.
15. **Summary opslaan** — score/verdict/ROI/slab in `analysis`-tabel
    (`trap='summary'`), `slab_status='analyzed'`. Dashboard leest dit.
16. **Discord-alert** — bij nieuw item dat matcht met user-alert → ping in
    #algemeen (stap 3 kan al vóór detail-fetch gebeuren als de zoekpagina
    voldoende info geeft).

### 2.2 ASCII-diagram

```
                     BUYEE (Mercari + PayPay Flea)
                                │
                                │ cron_scrape.sh (*/15)
                                ▼
                         scrape_buyee.py ──► last_scrape.json
                                                    │
                                       migrate_from_json.py
                                                    │
                                                    ▼
                            ┌─── SQLite (Pi kensa.db) ───┐
                            │                            │
                            │  <── dual-write ──►        │
                            │                            │
                            └── Supabase (schema kensa) ─┘
                                                    │
              cron_detail.sh (*/10)  cron_detail_mercapi.sh (*/3)
                                                    │
                       fetch_detail_mercapi (Mercari-API, snel)
                       fetch_detail        (Buyee via Chromium)
                       fetch_detail_proxy  (backlog>150 → NL proxy)
                                                    │
                       listings.detail_scraped_at + photos-URLs
                                                    │
                                     cron_worker_ocr.sh (*/2)
                                                    ▼
                     worker_ocr → analyze_ocr_only()
                        ├─ check_slab.py           (Vision)
                        │   of check_slab_hybrid   (Gemini multimodal, opt-in)
                        ├─ llm_client.interpret    (bij ruis)
                        └─ slab_status='ocr_done', card_key gezet
                                                    │
                        ┌───────────────────────────┴──────────────┐
                        │                                          │
              card_key + verse                             geen card_key of
              price_cache                                  verlopen cache
                        │                                          │
         cron_worker_cache.sh (*/1)                     cron_worker_ebay.sh (*/8)
                        │                                          │
                        ▼                                          ▼
              analyze_score_only                        analyze_score_only
              (mode='cache_only')                       (mode='ebay_only')
                        │                        ebay_lastsold (live)
                        │                        → ebay_filter (regex+LLM judge)
                        │                        → price_stats
                        └─────────────────┬───────────────┘
                                          ▼
                              score.compute_score
                              roi.compute
                              supabase_client.lookup (CM-URL)
                                          │
                                          ▼
                        cardmarket_queue ──► Windows-worker (Tailscale)
                                                    │
                                        cm_queue_api.py (POST result)
                                                    │
                                                    ▼
                                        price_cache (CM-data)
                                                    │
                                                    ▼
                              analysis-tabel trap='summary'
                              (score, verdict, roi, cert, ...)
                                                    │
                                                    ▼
                              Supabase (dashboards lezen hier)
                                                    │
                                                    ▼
                                      (Tommy ziet de deals)
```

---

## FASE 3 — Tabel per belangrijk bestand

Belangrijkheid: **K** = kritiek, **B** = belangrijk, **S** = support, **E** = experimenteel/POC.

### 3.1 Root-modules

| Bestand | Wat doet het (mensentaal) | Aangeroepen door | Roept aan | Belang |
|---|---|---|---|---|
| `scrape_buyee.py` (201) | Buyee zoekpagina scrapen (Chromium + stealth), basisinfo van alle listings | `cron_scrape.sh` | `parse_listing_split`, Scrapling | K |
| `parse_listing_split.py` (82) | v2 van parse_listing (post-refactor) | `scrape_buyee` (alias) | — | S |
| `migrate_from_json.py` (55) | `last_scrape.json` → DB-upserts + Discord-alerts voor nieuwe items | `cron_scrape.sh` | `storage.upsert_listing`, `alerts.check_and_post` | K |
| `alerts.py` (131) | User-alerts matchen op titel/prijs, Discord-post via `openclaw message send` | `migrate_from_json`, Discord commands | subprocess | B |
| `fetch_detail.py` (287) | Buyee-detail via Chromium (JSON-LD) — voor niet-Mercari (z-ids) én mercapi-fails | `cron_detail.sh` | `parse_detail_split`, `storage.upsert_listing`, `translate` | K |
| `fetch_detail_mercapi.py` (280) | Mercari-API-fetch via `mercapi` lib — snelste path | `cron_detail.sh`, `cron_detail_mercapi.sh` | mercapi, storage, translate | K |
| `fetch_detail_proxy.py` (218) | `fetch_detail` via NL residential proxy — springt bij als backlog >150 | `cron_detail.sh` | `fetch_detail.parse_detail`, storage | B |
| `translate.py` (84) | JP→EN titel (pokedex + Google Translate free) | fetch_detail*, migrate | urlopen | S |
| `check_slab.py` (128) | Voor item: pak 2-3 foto's, Vision-OCR, parse tot PSA-velden | `analyze_split/*` | `vision.ocr_and_parse` | K |
| `check_slab_hybrid.py` (146) | Gemini multimodal → Vision-fallback (opt-in `KENSA_USE_MULTIMODAL=1`) | `analyze.py` (env-gate) | `llm_client.interpret_slab_photo`, `check_slab`, `ocr_router.is_multi_slab_lot` | B |
| `vision.py` (194) | Vision REST-client + PSA-label parser (via `vision_split/`) | check_slab, ocr_router, llm_analyze | Vision REST | K |
| `vision_split/psa_parser.py` | Regex-parse van OCR-tekst → PSA-velden | vision.py (alias) | — | K |
| `vision_split/psa_name_set_split.py` | Naam+set uit gecombineerde regel splitsen | psa_parser | — | S |
| `ocr_router.py` (232) | Hybride lokale-OCR pipeline (Tailscale) — **alleen `is_multi_slab_lot` wordt in productie gebruikt**; hybrid_pipeline is dead-ish | `check_slab_hybrid` (deels) | crop, lokaal OCR, vision | E |
| `crop_psa_label.py` (192) | OpenCV: rode PSA-rand uitknippen | ocr_router, spikes | OpenCV, PIL | S |
| `check_title.py` (137) | Titel (JP+EN) ↔ slab-velden — pass/fail/skip | analyze_split/*, check_desc | `check_title_split.title_match`, pokedex | K |
| `check_title_split/title_match.py` | v2 (regex + token-check) | check_title (alias) | — | K |
| `check_desc.py` (85) | JP-beschrijving ↔ slab (grade + pokemon-naam) | analyze_split/* | `check_desc_split.desc_match` | B |
| `check_desc_split/desc_match.py` | v2 | check_desc (alias) | — | B |
| `llm_client.py` (278) | Gemini REST-client + slab-cache (hash) + 3 aliases naar splits | analyze, hybrid, splits | Gemini REST, SQLite `llm_slab_cache` | K |
| `interpret_slab_split/gemini_interpret.py` | Gemini interpret-call | llm_client (alias) | — | K |
| `judge_sales_split/gemini_judge.py` | Gemini eBay-sale-judge | llm_client (alias) | — | K |
| `llm_client_split/interp_photo.py` | Gemini multimodal photo-interp | llm_client (alias) | — | B |
| `score.py` (96) | 3 checks → confidence-score (0-100) + verdict | analyze_split/* | — | K |
| `roi.py` (107) | Buyee-prijs → landed → verkoop → winst | analyze_split/* | Frankfurter + cache | K |
| `query_builder.py` (127) | eBay-zoekterm uit slab-velden ("Pokemon Charizard 4 PSA 10") | ebay_phase | — | K |
| `ebay_lastsold.py` (235) | eBay sold-listings scrape (ingelogd Chromium) | ebay_phase | Scrapling, `parse_card_split` | K |
| `parse_card_split.py` (67) | v2 van parse_card | ebay_lastsold (alias) | — | S |
| `ebay_login.py` (109) | Eenmalige headed login → cookies opslaan | handmatig | Scrapling | S |
| `ebay_filter.py` (226) | Query-token-match (75% cutoff), grade-fail, prijs-outlier | ebay_phase | — | K |
| `price_stats.py` (55) | last / avg3 / avg5 uit sales | ebay_phase | — | K |
| `supabase_client.py` (142) | CM-URL lookup in Supabase `cards`-tabel | enqueue_cardmarket | Supabase REST | K |
| `analyze.py` (521) | **Alleen nog helpers + constanten + aliases** naar `analyze_split/*` | workers | analyze_split/*, check_slab*, llm_client, storage | K |
| `analyze_split/analyze_full.py` (453) | Echte analyze() — 8 fases F1-F8 | analyze.analyze | veel modules | K |
| `analyze_split/score_only.py` (477) | Score-only orchestrator (cache_only / ebay_only modes) | worker_cache, worker_ebay via `analyze.analyze_score_only` | ebay_phase, enqueue_cardmarket | K |
| `analyze_split/ebay_phase.py` (482) | 5 sub-functies: guard/query/cache/fetch+filter/persist | analyze_full, score_only | ebay_lastsold, ebay_filter, price_stats, price_cache | K |
| `analyze_split/ebay_query_split.py` (78) | Query-bouw uit slab of LLM | ebay_phase | check_title, query_builder | K |
| `analyze_split/enqueue_cardmarket.py` (387) | CM-URL-lookup + enqueue (bundle-guard, grade-fallback) | analyze_full, score_only | supabase_client, price_cache | K |
| `worker_ocr.py` (80) | Batch: pending → analyze_ocr_only, 6 threads | `cron_worker_ocr.sh` | analyze | K |
| `worker_cache.py` (88) | Batch: ocr_done + verse cache → score_only cache_only | `cron_worker_cache.sh` | analyze | K |
| `worker_ebay.py` (83) | Batch: ocr_done zonder cache → live eBay + score, 1 thread | `cron_worker_ebay.sh` | analyze, worker_claim | K |
| `worker_claim.py` (106) | Atomair lock (locked_by/locked_at), stale locks >10 min override | worker_ebay | SQLite | K |
| `storage.py` (584) | SQLite-schema + upserts + **3-weg dispatch naar Supabase** (env-gate) | overal | supabase_sync, storage_supabase/* | K |
| `storage_supabase/__init__.py` (25) | Package-facade — re-exporteert reads uit legacy, exposeert write-namespaces | analyze, workers, storage | submodules, legacy | K |
| `storage_supabase/_http.py` (62) | REST-wrapper (get/post/patch/delete) met kensa-schema headers | alle submodules | requests | K |
| `storage_supabase/_logging.py` (53) | `timed()` context manager voor Supabase-request tracing | alle submodules | — | S |
| `storage_supabase/analysis.py` (103) | save_trap (DELETE+INSERT) + fetch_latest_trap voor Supabase | analyze_split, workers | _http | K |
| `storage_supabase/listings.py` (165) | upsert_listing + fetch_listing + mark_slab_status | storage.py, analyze | _http | K |
| `storage_supabase/photos.py` (100) | upsert_photos + fetch_photos + update_metadata | storage.py, check_slab | _http | K |
| `storage_supabase/listings_seen.py` (34) | Batch UPDATE listings.last_seen_at (chunk 200) | storage.mark_seen_in_search | _http | S |
| `storage_supabase/cert_sightings.py` (49) | Supabase-native record_sighting + aggregate | storage.record_cert_sighting | _http | S |
| `storage_supabase/cardmarket_queue.py` (93) | Twee enqueue-paths (from_cache met listings, fresh zonder) | enqueue_cardmarket | _http | K |
| `storage_supabase/price_cache.py` (108) | fetch by card_key + cm_url, upsert_ebay + upsert_cm, is_fresh helpers | ebay_phase, enqueue_cardmarket | _http | K |
| `storage_supabase_legacy.py` (482) | **Álle Supabase-read-functies** (14×): load_listing, load_photo_urls, load_analysis_trap_latest, load_price_cache_by_key, load_analysis_by_trap_since, pick_cache_batch, pick_ebay_batch, pick_detail_batch, pick_detail_proxy_batch, pick_mercapi_batch, pick_mercapi_recheck_batch, pick_ocr_batch, load_detail_status. Naam is misleidend | via package `__init__` | requests | K |
| `supabase_sync.py` (372) | Best-effort dual-write helper (26 sync_* functies) + retry-enqueue helpers | storage.py + oude paden | requests, sync_retry | K |
| `sync_retry.py` (204) | SQLite-queue voor gefaalde Supabase-calls, exp. backoff (2m→10m→1u→4u→24u, max 6×) | supabase_sync, drain | SQLite | K |
| `sync_retry_drain.py` (153) | Cron-worker (*/2): pak due items, replay POST/PATCH/DELETE, dead-letter na 6 fails + Discord-alert | `crontab */2` | sync_retry, subprocess Discord | K |
| `cm_queue_api.py` (163) | Flask op :8898 (GET pending / POST result / POST enqueue) voor Windows CM-worker | Windows via Tailscale | SQLite + supabase_sync | K |
| `stale_lock_cleanup.py` (50) | Reset `locked_by=NULL` voor locks >20 min (Pi + Supabase) | `crontab */30` | SQLite + PostgREST | S |
| `detail_dead_marker.py` (72) | PayPay-items met retry-flag én 30 min zonder detail → dead | `crontab */15` | SQLite + PostgREST | S |
| `detail_stuck_detector.py` (45) | 6 u zonder detail → 1× retry-flag in `extra_json` | `crontab 11 *` | SQLite | S |
| `dedup_listings.py` (276) | Dedup-cron: max 20 rijen per card_key, 28d retentie (dry-run default, `--apply` echt) | `crontab 30 3` | SQLite + PostgREST | K |
| `enable_fts5.py` (113) | One-shot: FTS5-index op titels (met sync-triggers) | handmatig | SQLite | S |
| `migrate_to_supabase.py` (261) | One-shot bulk-migratie met dedup (max 20 per card_key + 28d unmatched) | handmatig | SQLite + requests | S |
| `backfill_cm_queue.py` (189) | One-shot: CM-queue rows die op Pi maar niet in Supabase staan bijposten | handmatig | SQLite + requests | S |
| `llm_analyze.py` (252) | POC: side-by-side regex vs Gemini voor N random items | handmatig | Gemini + SQLite | E |
| `socks5_bridge.py` (150) | HTTP-CONNECT → NordVPN SOCKS5-auth (Playwright kan geen SOCKS-auth) | handmatig | — | S |
| `spike_crop_gallery.py` (135) | POC: pak N recente items, run crop op alle foto's | handmatig | crop_psa_label | E |

### 3.2 Split-packages (naast root-versies)

Root-bestand houdt alleen helpers + `<naam> = <naam>_v2` als alias.

| Split-package | Wat zit erin | Root-stub |
|---|---|---|
| `analyze_split/` | analyze_full, score_only, ebay_phase, ebay_query_split, enqueue_cardmarket | `analyze.py` (4 aliases) |
| `check_title_split/` | title_match | `check_title.py` |
| `check_desc_split/` | desc_match | `check_desc.py` |
| `interpret_slab_split/` | gemini_interpret | `llm_client.py` |
| `judge_sales_split/` | gemini_judge | `llm_client.py` |
| `llm_client_split/` | interp_photo | `llm_client.py` |
| `parse_detail_split/` | mercari_parser | `fetch_detail.py` |
| `vision_split/` | psa_parser, psa_name_set_split | `vision.py` |

### 3.3 Support-directories

| Directory | Wat zit erin | Belang |
|---|---|---|
| `_legacy_pre_refactor/` (13 files) | Backups van pre-refactor originelen (31 aug/1 sep) | Kan weg als refactor stabiel is |
| `_legacy_removed_2026-09-05/` | Oude `webapp.py` + logs | Kan weg |
| `_removed_ebay_2_20260905-170611/` | Uitgezette 2e eBay-worker | Kan weg |
| `analyze_refactor_fixtures*/` (× 15) | Test-fixtures per refactor-stap + BASELINE_ANALYSIS.md | Actief in tests; verhuizen naar `tests/fixtures/` |
| `spike_crop_media/` (17 MB), `spike_multimodal_media/` | POC-experiment-foto's | Kan weg |
| `dev/` (10 scripts) | POCs, shadow-tests, `kensa_discord_commands.py` (via symlink live) | `kensa_discord_commands.py` blijft; rest weg |
| `windows-worker/` | Standalone Windows-scraper voor Cardmarket | Live — hoort elders |
| `tests/` (30+ tests) | pytest incl. dispatcher-tests + fixture-writers | Actief |
| `data/` (4 files) | proxy_usage, kensa_status_last, ... | Live |
| `photos/by_hash/` | Foto-bytes content-addressable | Live (7d retentie) |

### 3.4 Belangrijkste tabellen (SQLite-schema; wordt 1-op-1 gespiegeld op Supabase)

| Tabel | Bevat |
|---|---|
| `listings` | Alle scraped items (item_id, titels, prijzen, seller, detail_scraped_at, slab_status, card_key, locked_by/at) |
| `photos` | Foto-URLs per item (photo_index, url_original, file_hash, file_path, size, downloaded_at) |
| `analysis` | Alle checks per item als "traps": slab_ocr, llm_slab, title_match, desc_match, ebay_prices, roi, summary |
| `raw_pages` | Gzipped HTML (retentie 3 dagen) |
| `cert_sightings` | Elke keer dat cert-nummer voorkomt (fraude-detectie op relist) |
| `cardmarket_queue` | CM-URLs die de Windows-worker moet ophalen |
| `alerts` | User-alerts (query + max/min yen) |
| `price_cache` | Kaart-brede eBay + CM prijzen (3d TTL) — key=card_key |
| `user_verdicts` | Handmatige verdict-overrides |
| `llm_slab_cache` | Hash-cache Gemini-interpretaties (30d TTL) |
| `sync_retry` | Queue van gefaalde Supabase-writes (drain-worker) |

---

## FASE 4 — Complexiteit en problemen

Ik markeer **[T]** = tijdelijk (verdwijnt na cutover, geen structureel
probleem), **[S]** = structureel (blijft ook ná cutover een probleem).

### 4.1 ZEKER (bewijs uit code)

**A. [S] Dubbele bestandsstructuur door "alias-refactor"** — grootste bron
van leesbaarheids-verwarring. Elke `<naam>.py` in de root houdt alleen
helpers + `<naam> = <naam>_v2` als alias; de echte code zit in
`<naam>_split/`. Voorbeeld uit `analyze.py:518-521`:

```python
analyze_score_only = _analyze_score_only_new
analyze = analyze_v2
_run_ebay_phase = run_ebay_phase_v2
_enqueue_cardmarket_if_possible = enqueue_cardmarket_if_possible_v2
```

Dit patroon zit óók in: `scrape_buyee`, `fetch_detail`, `check_title`,
`check_desc`, `llm_client` (3 aliases), `vision`, `ebay_lastsold`. Om te
weten wat `analyze.analyze()` doet, moet je 3 files openen (analyze →
analyze_full → sub-modules per fase). **Objectief moeilijker leesbaar** dan
één bestand met sub-functies.

**B. [S, maar wordt kleiner] Storage-cutover-branching.** Elke storage-functie
in `storage.py`, `analyze.py` en `analyze_split/*` heeft dit patroon:

```python
mode = os.environ.get("KENSA_WRITE_STORAGE", "sqlite")
if mode == "supabase":  return _sb_native_call(...)
# SQLite-path
...
if mode == "dual":      _sb_native_call(...); return
_sync(_sbs.sync_..., ...)  # legacy sync-path
```

Dat is 3-weg branching in élk kritiek pad, gestuurd door env-vars die
cron-scripts per worker instellen. **Waarom dit nu OK is:** dit is de
laatste week van de cutover; morgen wordt de branching gewoon opgeruimd
(zie "Na de cutover"). Wél zeker: **de code is nu op zijn moeilijkst
leesbaar** — deze fase moet niet langer duren dan nodig.

**C. [T] Verspreide sync-monitoring en verificatie-loops.** Vier scripts
draaien puur voor de cutover: `sync-drift-check.py`, `monitor-cutover-cache.py`,
`report-supabase-reads.py`, plus `sync_retry_drain.py` als dead-letter-vangnet.
Ze schrijven naar 5 aparte logs (`sync_drift.log`, `monitor_cutover_cache.log`,
`report_supabase_reads.log`, `sync_retry_drain.log`, `supabase_sync.log`).
Na cutover: 3 van de 4 kunnen weg; retry-drain blijft nuttig voor
netwerk-storingen.

**D. [S] `storage_supabase_legacy.py` heet "legacy" maar is centraal.**
Bevat álle Supabase-read-functies (14 functies, 482 regels). De package
`storage_supabase/__init__.py` re-exporteert ze allemaal. **Naam is de
enige "legacy" — de code is de leeslaag.** Blijft ook na cutover fout-benaamd.

**E. [S] Drie parallelle detail-fetchers met router in shell.**
`fetch_detail.py` (Buyee-scrape) + `fetch_detail_mercapi.py` (Mercari-API) +
`fetch_detail_proxy.py` (Buyee via NL-proxy). De router-logica (mercapi voor
m*-ids, Buyee voor z*-ids, proxy als backlog>150) staat **in `cron_detail.sh`,
niet in Python**. Wie de logica wil begrijpen moet zowel de shell- als de
Python-code lezen.

**F. [S] Mercapi loopt via twee crons.** `cron_detail.sh` (`*/10`, mercapi
+ Buyee + proxy) en `cron_detail_mercapi.sh` (`*/3`, alleen mercapi). Aparte
flocks (`/tmp/kensa-detail.lock` vs `/tmp/kensa-detail-mercapi.lock`) — kunnen
dus tegelijk lopen. Beide zetten `KENSA_READ_STORAGE=supabase` /
`KENSA_WRITE_STORAGE=dual`. Bedoeld om mercapi vaker te draaien dan Buyee,
maar er staat geen comment die uitlegt dat overlap OK is. **Vermoedelijk
correct** (Tommy heeft mercapi bewust versneld), maar niet gedocumenteerd.

**G. [S] Duplicate helpers in split-modules.** Om circulaire imports te
vermijden zijn helpers gekopieerd:
- `_build_card_key` — `analyze.py` én `analyze_split/ebay_phase.py`
- `_looks_like_bundle` — `analyze.py` én `analyze_split/enqueue_cardmarket.py`
- `_pokemon_en_lookup` — beide
- `_load_listing`, `_save_trap`, `_mark_slab_status` — in `analyze.py` én
  `analyze_split/score_only.py` (analyze_full importeert vervolgens uit
  score_only, wat de knoop nog vergroot)
- Regex-constanten (`SUBTYPE_RE`, `CERT_RE`, `GRADE_RE`) — `vision.py` én
  `vision_split/psa_parser.py`

Elke duplicatie is een risico: fix op één plek → drift op de ander.

**H. [S] Legacy-archieven zonder opruim-datum.**
- `_legacy_pre_refactor/` (13 pre-refactor backups)
- `_legacy_removed_2026-09-05/` (oude webapp)
- `_removed_ebay_2_20260905-170611/` (2e eBay-worker)
- 15 `*_refactor_fixtures/`-mappen (test-fixtures voor elke refactor-stap)

Eten diskruimte + maken `ls` in de root onleesbaar (**29 subdirs in root**,
inclusief 15 fixture-mappen).

**I. [T, maar krimpt niet meteen] Grote SQLite-DB.** `kensa.db` is 1.02 GB
(verdwijnt morgen). Backup in `/home/pi/kensa-refactor-backup-20260831-195849/`
van 527 MB (kan ook opgeruimd worden).

**J. [S] Zeer veel logs in de repo-root.** `pipeline_worker_ebay.log` 30 MB,
`pipeline_worker_ocr.log` 18 MB, `pipeline_worker_cache.log` 17 MB,
`pipeline_detail.log` 12 MB, `pipeline_scrape.log` 6 MB — géén logrotate
zichtbaar. Groeit door tot filesystem vol raakt.

**K. [S] Dev-scripts en spikes in productie-root.** `spike_crop_gallery.py`,
`llm_analyze.py`, `spike_crop_media/` (17 MB), `spike_multimodal_media/` —
en tegelijk staan er in `dev/` nog 10 andere POC-scripts. Twee "dev-plekken"
naast elkaar.

**L. [S] `analyze.py` is nu vooral commentaar en aliases.** Van de 521
regels zijn ~150 aliases, placeholder-comments ("NB: X verhuisd naar Y")
en dispatcher-branches (die na cutover ook wegvallen). De echte inhoud is
klein. **Prima kandidaat om samen te voegen met `analyze_split/` → package
`analyze/`.**

**M. [S] Wisselende naming.** `_split/` vs `_refactor_fixtures/` (met
enkele variaties: `analyze_refactor_fixtures_analyze/`, `_enqueue`, `_interp`,
`_psa`, `_score_only`). Geen consistente conventie welke suffix bij wat hoort.

**N. [S] Twee "loop-mechanismen" voor detail-retry naast elkaar.** 
`detail_stuck_detector.py` zet retry-flags; `detail_dead_marker.py` markeert
op basis van diezelfde flag als dead. Werkt, maar de "waarom 6 uur" en
"waarom 30 min" staat verspreid over twee scripts + comments.

### 4.2 VERMOEDENS (niet 100% zeker)

- **`ocr_router.py`** — de hybride lokale-OCR pipeline (`ocr_and_parse_hybrid`)
  wordt door geen enkele actieve module aangeroepen; **alleen
  `is_multi_slab_lot` wordt gebruikt** door `check_slab_hybrid.py`. Rest kan
  vermoedelijk weg of naar `dev/`.
- **`socks5_bridge.py`** — geen systemd/cron-referentie, log is in-repo. Draait
  vermoedelijk als handmatige daemon of niet meer.
- **`llm_analyze.py`** (POC) — geen imports vanaf productie-code; is een
  handmatig te draaien vergelijk-tool.
- **15 `*_refactor_fixtures/` mappen** — vermoedelijk alleen actief nog nodig
  zolang de tests ze importeren. Na de merge in P2.2/P2.3 wordt de meeste
  overbodig. **Verificatie**: `grep -r refactor_fixtures tests/` vóór
  weggooien.
- **`windows-worker/`** in deze repo is een reference-kopie — de echte draait
  op Tommy's PC. Kan naar aparte repo.

### 4.3 Wat WEL goed werkt (behouden!)

- **Worker-splitsing** (OCR / cache / eBay). Snelle en langzame paden zijn
  gescheiden; parallellisme is per worker afgesteld (6 / 4 / 1 threads).
- **Elke cron heeft eigen flock** — geen dubbele runs.
- **Retry-queue met exp. backoff + dead-letter alert** — nette
  best-effort-sync pattern die ook na cutover nuttig blijft.
- **`price_cache` met card_key als key** — echte cross-seller cache; scheelt
  eBay-hits (kernwinst).
- **Tests-map** met dispatcher-tests én fixtures — solide basis om refactors
  te doen zonder ongeluk.
- **Env-var-toggles zijn 1-regel-rollback** — bewuste "rollback-in-1-min"
  strategie tijdens cutover.
- **Cutover-monitoring** (`sync-drift-check`, `monitor-cutover-cache`,
  `report-supabase-reads`) is verstandig ontworpen: kort-cyclische Discord-
  rapportages met anti-spam en state-file. Puur cutover-only, maar goed
  gedaan.

---

## Na de cutover — wat wordt morgen overbodig

Wanneer SQLite verdwijnt en Supabase de enige data-laag is, wordt dit
concreet opruimbaar:

### Verdwijnende code / paden

**Storage-dispatcher:**
- Alle `os.environ.get("KENSA_WRITE_STORAGE", ...)`-branches in:
  `storage.py` (5×: upsert_listing, upsert_photos, download_photo_if_needed,
  mark_seen_in_search, record_cert_sighting), `analyze.py` (2× in
  `_save_trap` en `_mark_slab_status`), `analyze_split/enqueue_cardmarket.py`
  (2×) → **9 branches weg**.
- Alle `os.environ.get("KENSA_READ_STORAGE", ...)`-branches in:
  `analyze.py`, `check_slab.py`, `fetch_detail.py`, `fetch_detail_mercapi.py`
  (3×), `fetch_detail_proxy.py`, `worker_ocr.py`, `worker_cache.py`,
  `worker_ebay.py`, `analyze_split/score_only.py` (via `_read_via_supabase()`),
  `analyze_split/ebay_phase.py` (via `_read_via_supabase()`),
  `analyze_split/analyze_full.py` (via score_only) → **12+ branches weg**.

**Dual-write / sync helpers:**
- `supabase_sync.py` (372 regels) — alle 26 `sync_*` functies + retry-enqueue
  helpers zijn niet meer nodig als er geen "secondary" is om naar te syncen.
  Wat blijft: eventueel `_do_post_http/_do_patch_http/_do_delete_http` als
  utility, maar dan verplaats naar `storage_supabase/_http.py` (die heeft
  al een REST-wrapper).
- `sync_retry.py` + `sync_retry_drain.py` — **behouden**, maar herbenoemen
  ("supabase-write-retry"). Rationale verandert: was "houd dual-write
  betrouwbaar", wordt "vang netwerk-glitches naar de enige data-store op".
- Log `supabase_sync.log` (556 KB) — stopt met groeien.

**Storage-modules:**
- `storage.py` — SQLite-schema + upserts verdwijnen. Wat blijft:
  photo-download-logica (bytes op disk in `photos/by_hash/`), retentie-
  helpers (`cleanup_expired_photos`, `cleanup_raw_pages`), en `save_raw_page`
  → maar `raw_pages` staat in SQLite; **beslissing nodig**: gaat `raw_pages`
  mee naar Supabase, of blijft dat een lokale mini-DB op Pi? Bepaalt of
  `storage.py` helemaal weg kan of afslankt tot een pure "photo bytes +
  raw_pages op disk"-module.
- `kensa.db` (1.02 GB) — dropbaar zodra alle 30d cache-hits in Supabase
  ingelopen zijn.
- Backup `/home/pi/kensa-refactor-backup-20260831-195849/` (527 MB) — kan
  ook weg.

**Verificatie- en drift-scripts:**
- `scripts/sync-drift-check.py` + `sync_drift.log` + `sync_drift.cron.log` +
  `data/sync_drift_state.json` — geen 2 databases = geen drift = script
  overbodig. Cron-entry `5 * * * *` weg.
- `scripts/monitor-cutover-cache.py` + `monitor_cutover_cache.log` +
  `cutover_cache_state.json` — pure cutover-verificatie. Cron-entry
  `0 */12 * * *` weg.
- `scripts/report-supabase-reads.py` + `report_supabase_reads.log` +
  `pipeline_supabase_reads.log` (1.4 MB, groeit) — verificatie-tool.
  Cron-entry weg, `pipeline_supabase_reads.log` verdwijnt (`_logging.timed()`
  in `storage_supabase/_logging.py` schrijft ernaar).
- `backfill_cm_queue.py` — one-shot cutover-hulp, kan naar `archive/oneoffs/`.
- `migrate_to_supabase.py` — idem.

**Crontab-simplificatie:**
- Global env-var `KENSA_STORAGE=dual` bovenaan crontab — weg.
- Per-cron `export KENSA_READ_STORAGE=supabase` / `KENSA_WRITE_STORAGE=dual`
  regels in álle 6 kensa-cron-scripts — weg.
- Cutover-tijdstempel-comments (`# CUTOVER 2026-09-05 17:07`) — weg.
- Backlog-check in `cron_detail.sh` die zowel Supabase- als Pi-fallback doet
  — Pi-fallback kan weg; alleen Supabase-count blijft.

**Refactoring-oppervlak dat verdwijnt vanzelf:**
- `_read_via_supabase()`-helpers in `analyze_split/score_only.py:57` en
  `analyze_split/ebay_phase.py:77` — beide return altijd `True`, dus weg.
- Duplicate `_load_listing` / `_save_trap` / `_mark_slab_status` in
  `analyze.py` én `analyze_split/score_only.py` — de SQLite-versies kunnen
  weg, alleen de Supabase-varianten blijven. Vervolgens: één canonicale
  versie in `analyze/__init__.py` (P2.2).

### Rough count
- **~21+ env-var-branches** verdwijnen uit business-logica.
- **1 modulen** (`supabase_sync.py`, 372 regels) grotendeels weg.
- **3 cron-jobs + 5 log-files + 3 state-files** verdwijnen.
- **~1.5 GB disk** vrij (kensa.db + backup + supabase_sync.log +
  pipeline_supabase_reads.log).
- **`storage.py` van 584 → geschat 100-150 regels** (alleen photo-bytes-
  helpers + eventueel raw_pages als die op Pi blijft).

Dit maakt het perfecte startpunt voor P2.2/P2.3 (alias-stubs merge): eerst
de dispatcher weg, dán merge — anders raak je in de knoop met branches die
je later toch weer moet aanraken.

---

## FASE 5 — Voorstel logischere architectuur (Supabase-only)

**Uitgangspunt:** geen enterprise-lagen. Zeven concentrische ringen op basis
van wat de code echt doet. Storage is één publieke API.

### 5.1 Ring 1 — INBRENGEN (extern → onze DB)

Alles wat een externe bron aanraakt om data binnen te halen. Weet niks over
"scoring" of "verdict".

Modules: `scrape_buyee`, `parse_listing`, `fetch_detail_mercapi`,
`fetch_detail`, `fetch_detail_proxy`, `parse_detail`,
`ingest_scrape_json` (was migrate_from_json), `translate`, `socks5_bridge`.

Gemeen: hebben Chromium/mercapi/proxy nodig, produceren "listings-rijen +
photo-URLs".

### 5.2 Ring 2 — LEZEN (slab OCR + AI)

Kaart-identiteit uit de foto's halen.

Modules: `vision` + `vision_split/*` (samengevoegd tot 1), `check_slab_ocr`
(was check_slab), `check_slab_multimodal` (was check_slab_hybrid),
`crop_psa_label`, `llm_client` (met interpret + judge + photo splits
samengevoegd), `pokedex_ja_en.json`.

Gemeen: photo-URL → structured slab-fields.

### 5.3 Ring 3 — PRIJSDATA

Voor bekende kaart marktprijs zoeken.

Modules: `ebay_lastsold` + `parse_card` (samen), `ebay_login`, `ebay_filter`,
`query_builder`, `price_stats`, `cardmarket_lookup` (was supabase_client),
`cm_queue_api`.

### 5.4 Ring 4 — BESLISSING

Alles samenvoegen tot een oordeel.

Modules: `check_title` (met title_match_split samen), `check_description`
(was check_desc), `score`, `roi`, `analyze/` (package: was `analyze_split/`).

### 5.5 Ring 5 — WORKERS + CRON

Modules: `worker_slab_read` (was worker_ocr), `worker_score_from_cache`
(was worker_cache), `worker_score_with_ebay` (was worker_ebay),
`worker_lock` (was worker_claim), plus `maintenance/` submap voor
`stale_lock_cleanup`, `detail_stuck_detector`, `detail_dead_marker`,
`dedup_listings`. Cron-scripts in `cron/`.

### 5.6 Ring 6 — STORAGE (Supabase-only)

**Eén publieke API.** Business-code roept `storage.upsert_listing(row)` aan,
niet meer PostgREST direct.

Modules:
- `storage/__init__.py` — publieke API (upsert_listing, upsert_photos,
  save_trap, mark_slab_status, ...)
- `storage/http.py` — REST-wrapper (was `storage_supabase/_http.py`)
- `storage/logging.py` — request-tracing (was `_logging.py`)
- `storage/listings.py`, `photos.py`, `analysis.py`, `cert_sightings.py`,
  `listings_seen.py`, `cardmarket_queue.py`, `price_cache.py` — huidige
  submodules ongewijzigd
- `storage/reads.py` — de leeslaag (was `storage_supabase_legacy.py`, 14
  functies)
- `storage/write_retry/` (was `sync_retry*.py`) — netwerk-glitch vangnet
  voor writes
- `storage/local.py` — kleine set voor lokale bytes: `photos/by_hash/`
  content-addressable + retentie. Als `raw_pages` op Pi blijft: ook hier.

### 5.7 Ring 7 — DISCORD + BEHEER

Modules: `discord/alerts_matcher` (was alerts.py), `discord/slash_commands`
(was dev/kensa_discord_commands.py). One-shots in `oneoffs/`.

### 5.8 Wat niet in de ringen thuishoort

- POC's (`llm_analyze.py`, `spike_crop_gallery.py`, `dev/*`) →
  `experiments/`
- Legacy-archieven → één `archive/`
- Refactor-fixtures → `tests/fixtures/`
- `windows-worker/` → aparte repo

---

## FASE 6 — Naamvoorstellen (per onduidelijk bestand)

Niets hernoemd — dit is alleen een voorstel per bestand met korte reden.
Sorteer op impact.

| Huidig | Voorstel | Reden |
|---|---|---|
| `storage_supabase_legacy.py` | `storage/reads.py` | "Legacy" is misleidend; het is dé leeslaag |
| `storage_supabase/` package | `storage/` package | Post-cutover is dat ALLE storage — voorvoegsel overbodig |
| `supabase_sync.py` | (weg, of gedeeltelijk → `storage/http.py`) | Post-cutover: geen sync meer |
| `sync_retry.py`, `sync_retry_drain.py` | `storage/write_retry/queue.py`, `storage/write_retry/worker.py` | Duidelijker dat het een write-retry vangnet is, geen dual-sync-loop |
| `analyze.py` | `analyze/__init__.py` (of `pipeline.py`) | Nu bijna leeg (aliases + helpers) — verhuis naar package |
| `analyze_split/` | `analyze/` | "split" is refactor-detail dat niet hoort te blijven |
| `check_slab_hybrid.py` | `check_slab_multimodal.py` | "hybrid" zegt niks; "multimodal" beschrijft wat het is |
| `check_slab.py` | `check_slab_ocr.py` | Verduidelijkt tegenover multimodal-variant |
| `analyze_refactor_fixtures*/` | `tests/fixtures/<naam>/` | Zeg wat het is (test-fixtures), niet waarvoor gemaakt |
| `migrate_from_json.py` | `ingest_scrape_json.py` | "migrate" suggereert one-shot; dit draait elke 15 min |
| `check_desc.py` | `check_description.py` | Volledige woord leest sneller |
| `dev/kensa_discord_commands.py` | `discord/slash_commands.py` | Aparte `discord/` submap |
| `alerts.py` | `discord/alerts_matcher.py` | Reserveert ruimte voor alerts_service etc. |
| `worker_ocr.py` | `worker_slab_read.py` | OCR = middel, slab_read = doel |
| `worker_cache.py` | `worker_score_from_cache.py` | Zegt wat het doet |
| `worker_ebay.py` | `worker_score_with_ebay.py` | Idem |
| `worker_claim.py` | `worker_lock.py` | "claim" is jargon |
| `supabase_client.py` | `cardmarket_lookup.py` | Wat het doet (CM-URL lookup), niet welke DB (Supabase) |
| `backfill_cm_queue.py` | `oneoffs/cm_queue_backfill.py` | Prefix maakt one-shot expliciet |
| `migrate_to_supabase.py` | `oneoffs/bulk_migrate_to_supabase.py` | Idem — na cutover naar `archive/` |
| `spike_crop_gallery.py` | `experiments/spike_crop_gallery.py` | Weg uit productie-root |
| `llm_analyze.py` | `experiments/compare_regex_vs_gemini.py` | Verduidelijkt POC-doel |
| `pokedex_ja_en.json` | `data/pokedex_ja_en.json` | Statische data in `data/` |
| `.fx_cache.json` | `data/fx_cache.json` | Dotfile onnodig |
| `_legacy_pre_refactor/` | `archive/legacy_pre_refactor_20260901/` | Datum expliciet |
| `_legacy_removed_2026-09-05/` | `archive/webapp_removed_20260905/` | Zeg WAT weg is |
| `_removed_ebay_2_20260905-170611/` | `archive/second_ebay_worker_removed_20260905/` | Idem |

Voor de `*_split/` packages die alleen "v2 van X" bevatten (`check_title_split/`,
`check_desc_split/`, `parse_detail_split/`, `interpret_slab_split/`,
`judge_sales_split/`, `llm_client_split/`, `vision_split/`): merge terug in
het root-bestand. De cyclomatische complexiteit die tot splitsing leidde is
even goed op te lossen met sub-functies binnen één file.

---

## FASE 7 — Voorstel mappenstructuur (Supabase-only, doel-situatie)

```
agents/kensa/
├── README.md
├── ANALYSE-KENSA.md
├── AGENTS.md
├── pytest.ini
├── photos/by_hash/                  # foto-bytes content-addressable
│
├── ingest/                          # RING 1 — DATA BINNENHALEN
│   ├── scrape_buyee.py
│   ├── fetch_detail_mercapi.py
│   ├── fetch_detail.py
│   ├── fetch_detail_proxy.py
│   ├── ingest_scrape_json.py        # was migrate_from_json.py
│   └── translate.py
│
├── slab_read/                       # RING 2 — SLAB LEZEN
│   ├── check_slab_ocr.py            # was check_slab.py
│   ├── check_slab_multimodal.py     # was check_slab_hybrid.py
│   ├── crop_psa_label.py
│   ├── vision.py                    # met vision_split terug erin
│   ├── llm_client.py                # met 3 splits terug erin
│   └── data/pokedex_ja_en.json
│
├── pricing/                         # RING 3 — PRIJSDATA
│   ├── ebay_lastsold.py             # met parse_card terug erin
│   ├── ebay_login.py
│   ├── ebay_filter.py
│   ├── query_builder.py
│   ├── price_stats.py
│   ├── cardmarket_lookup.py         # was supabase_client.py
│   └── cm_queue_api.py
│
├── decide/                          # RING 4 — BESLISSING
│   ├── check_title.py               # met check_title_split terug erin
│   ├── check_description.py         # was check_desc.py
│   ├── score.py
│   ├── roi.py
│   └── analyze/
│       ├── __init__.py              # analyze() + analyze_ocr_only()
│       ├── full.py                  # was analyze_full.py
│       ├── score_only.py
│       ├── ebay_phase.py
│       └── enqueue_cardmarket.py
│
├── workers/                         # RING 5 — ORKESTRATIE
│   ├── worker_slab_read.py          # was worker_ocr.py
│   ├── worker_score_from_cache.py   # was worker_cache.py
│   ├── worker_score_with_ebay.py    # was worker_ebay.py
│   ├── worker_lock.py               # was worker_claim.py
│   └── maintenance/
│       ├── stale_lock_cleanup.py
│       ├── detail_stuck_detector.py
│       ├── detail_dead_marker.py
│       └── dedup_listings.py
│
├── cron/                            # RING 5 — cron shell-scripts
│   ├── cron_scrape.sh
│   ├── cron_detail.sh
│   ├── cron_detail_mercapi.sh
│   ├── cron_worker_slab_read.sh
│   ├── cron_worker_score_from_cache.sh
│   ├── cron_worker_score_with_ebay.sh
│   ├── cron_cleanup_raw_pages.sh
│   └── kensa.env                    # centraal (na cutover: bijna leeg)
│
├── storage/                         # RING 6 — STORAGE (Supabase-only)
│   ├── __init__.py                  # publieke API
│   ├── http.py                      # was _http.py
│   ├── logging.py                   # was _logging.py
│   ├── reads.py                     # was storage_supabase_legacy.py
│   ├── listings.py
│   ├── photos.py
│   ├── analysis.py
│   ├── cert_sightings.py
│   ├── listings_seen.py
│   ├── cardmarket_queue.py
│   ├── price_cache.py
│   ├── local.py                     # photos/by_hash bytes + evt. raw_pages
│   └── write_retry/
│       ├── queue.py                 # was sync_retry.py
│       └── worker.py                # was sync_retry_drain.py
│
├── discord/                         # RING 7 — DISCORD
│   ├── alerts_matcher.py            # was alerts.py
│   └── slash_commands.py            # was dev/kensa_discord_commands.py
│
├── oneoffs/                         # eenmalige scripts (geen cron)
│   └── enable_fts5.py               # (indien nog nodig na Supabase-only)
│
├── experiments/                     # POCs & spikes
│   ├── spike_crop_gallery.py
│   ├── compare_regex_vs_gemini.py   # was llm_analyze.py
│   ├── poc_multimodal.py
│   ├── shadow_test_easyocr.py
│   └── test_hybrid_ocr.py
│
├── tests/                           # (bestaat al)
│   ├── fixtures/                    # ← alle *_refactor_fixtures/ hierheen
│   └── ...
│
├── data/                            # runtime state + statische data
│   ├── proxy_usage.json
│   ├── kensa_status_last.json
│   ├── fx_cache.json                # was .fx_cache.json
│   └── monitor_state/
│
├── logs/                            # ← alle pipeline_*.log hierheen
│   └── (logrotate config)
│
├── archive/                         # oude versies + one-shots
│   ├── legacy_pre_refactor_20260901/
│   ├── webapp_removed_20260905/
│   ├── second_ebay_worker_removed_20260905/
│   ├── bulk_migrate_to_supabase.py  # was migrate_to_supabase.py
│   ├── cm_queue_backfill.py         # was backfill_cm_queue.py
│   └── cutover_scripts/             # sync-drift-check, monitor-cutover, report-reads
│
└── windows-worker/                  # (in principe naar aparte repo)
```

---

## FASE 8 — Veilig migratiestappenplan

Elke stap: klein, terug-draaibaar, na afloop testbaar. Wacht na elke stap
minstens één volledige cron-cyclus (max 30 min) om te zien of er iets in de
logs ontploft.

Legenda: **P0** = eerste natuurlijke stap ná Tommy's cutover morgen. **P1**
= duidelijk voordeel, laag risico. **P2** = nuttig, meer werk. **P3** =
alleen bij behoefte.

### P0 — Meteen ná de Supabase-cutover (6 sep)

**P0.1 — Verificatie-scripts uit crontab halen.** Zet `sync-drift-check`,
`monitor-cutover-cache`, `report-supabase-reads` uit (commentaar in crontab).
Wacht 1 dag: geen alerts = veilig. Verplaats de scripts naar
`archive/cutover_scripts/`.

**P0.2 — Env-vars uit cron-scripts halen.** Verwijder alle
`export KENSA_READ_STORAGE=supabase` en `export KENSA_WRITE_STORAGE=dual`
uit `cron_scrape.sh`, `cron_detail.sh`, `cron_detail_mercapi.sh`,
`cron_worker_ocr.sh`, `cron_worker_cache.sh`, `cron_worker_ebay.sh`. Ook
`KENSA_STORAGE=dual` bovenaan crontab weg. Doe dit ná P0.3 (anders volgen
de scripts oud gedrag).

**P0.3 — Dispatch-branches uit business-code weghalen.** Volgorde:
1. `analyze_split/score_only.py:57` — `_read_via_supabase()` altijd `True`.
2. `analyze_split/ebay_phase.py:77` — idem.
3. Duplicate `_load_listing` / `_save_trap` / `_mark_slab_status` in
   `analyze.py` en `analyze_split/score_only.py`: SQLite-paden weg, alleen
   Supabase-versies houden.
4. `storage.py` — alle 5 dispatch-branches vervangen door één regel per functie:
   `return _sb_upsert(row)` etc.
5. `analyze_split/enqueue_cardmarket.py` (2 branches) — idem.
6. `worker_ocr.py`, `worker_cache.py`, `worker_ebay.py` — reads via
   `storage_supabase.pick_*_batch_supabase` als enige pad.

Per stap: run tests (`pytest tests/`), draai één cron handmatig, check logs.

**P0.4 — `supabase_sync.py` uitfaseren.** De `sync_*` functies zijn nergens
meer nodig na P0.3 (dispatcher-branches waren de enige callers). Migreer
zeer klein deel dat wél nog nodig blijkt (bijv. retry-enqueue helpers) naar
`storage_supabase/_http.py`. Verwijder `supabase_sync.py`.

**P0.5 — SQLite-cleanup.** Bevestig `kensa.db` heeft geen actieve readers
(alle env-vars weg + tests groen). `mv kensa.db kensa.db.PRE_CUTOVER_20260906`
(niet meteen weggooien; laat 7 dagen staan als paniek-vangnet). Backup-dir
`/home/pi/kensa-refactor-backup-20260831-195849/` idem.

**P0.6 — Log-schoonmaak.** `supabase_sync.log`, `pipeline_supabase_reads.log`,
`sync_drift.log`, `monitor_cutover_cache.log`, `report_supabase_reads.log`
truncaten en de bijbehorende `cron.log`-varianten weghalen. Zet
`raw_pages`-retentie op `KENSA_RAW_RETENTION_DAYS=3` als daadwerkelijk
retention nodig (nu 3d hard-coded).

### P1 — Nu / snel (grotendeels los van de cutover)

**P1.1 — Logs naar `logs/` + logrotate.** Verhuis `pipeline_*.log`,
`*.cron.log`, `cm_monitor.log`. Update cron-scripts (`>> logs/...`). Configureer
`logrotate` voor 7 dagen / max 100 MB per file. Snelle winst — directe
disk-rust.

**P1.2 — Statische data in `data/`.** Verhuis `pokedex_ja_en.json`,
`.fx_cache.json`, `proxy_usage.json`, `kensa_status_last.json`,
`data/kensa_status_last.json` — update paths.

**P1.3 — POCs en spikes naar `experiments/`.** `llm_analyze.py`,
`spike_crop_gallery.py`, `spike_crop_media/`, `spike_multimodal_media/` weg
uit de root. `dev/*` inhoud ook (behalve `kensa_discord_commands.py` waarnaar
gesymlinkt wordt — die move los behandelen).

**P1.4 — Fixtures naar `tests/fixtures/`.** 15 `*_refactor_fixtures/`-mappen
naar `tests/fixtures/<naam>/`. Update test-imports. Winst: 15 items minder
in root. **Verificatie:** `grep -r <fixture_naam> tests/` bevestigt gebruik.

**P1.5 — Archief consolideren.** `_legacy_pre_refactor/`,
`_legacy_removed_2026-09-05/`, `_removed_ebay_2_*` samen onder `archive/`.

**P1.6 — Documenteer de mercapi-dubbele-cron.** Vraag Tommy: bewust dat
`cron_detail.sh` én `cron_detail_mercapi.sh` allebei mercapi draaien? Zo ja:
header-comment in beide bestanden ("beide crons zetten `KENSA_READ_STORAGE`,
mercapi loopt zowel elke 3 als elke 10 min voor extra dekking op Mercari").
Zo nee: mercapi-call uit `cron_detail.sh` weghalen — draait dan alleen nog
via de */3-cron.

### P2 — Nuttig, iets groter werk

**P2.1 — Mappenstructuur invoeren (grote sprong).** Verhuis root-files in
één zitting naar de 7 ring-mappen. Elke file `git mv` + imports fixen. Doe
per groep van 5-10 files + testrun. Risico: hoog — één missende import en
pipeline staat stil. **Vereist aparte branch met `--dry-run` op alle
scripts.**

**P2.2 — `analyze.py` opgaan in package `analyze/`.** Zet aliases-stub uit
`analyze.py` in `analyze/__init__.py`. Verwijder `analyze.py`. Duplicaat-
helpers (`_load_listing`, `_save_trap`, `_looks_like_bundle`,
`_build_card_key`, `_pokemon_en_lookup`) samenvoegen tot 1 canonicale
versie in `analyze/__init__.py`. Sub-modules importeren die (lazy-import
kan dan weg).

**P2.3 — Andere alias-stubs samenvoegen.** `scrape_buyee.py`,
`fetch_detail.py`, `check_title.py`, `check_desc.py`, `llm_client.py`,
`vision.py`, `ebay_lastsold.py`. Merge `_split/`-inhoud terug in root-file
(sub-functies binnen één file). Winst: elk concept → één file. Doe één file
per keer + testrun.

**P2.4 — Storage-package hernoemen.** `storage_supabase/` → `storage/`.
`storage_supabase_legacy.py` → `storage/reads.py`. Update alle imports.

**P2.5 — Cron-scripts naar `cron/` + centraal `kensa.env`.** Verhuis alle
`cron_*.sh` naar `cron/`. Update crontab. Enige overgebleven env-vars
(`KENSA_USE_MULTIMODAL=1`, `KENSA_RAW_RETENTION_DAYS`, limits) centraal in
`cron/kensa.env` die de scripts `source`en.

**P2.6 — Windows-worker naar aparte repo.** `windows-worker/` naar
`~/.openclaw/github-repos/kensa-cardmarket-worker/`. In kensa-repo alleen
`README.md`-verwijzing.

### P3 — Alleen bij behoefte

**P3.1 — `ocr_router.py` opruimen.** Alleen `is_multi_slab_lot` blijft in
gebruik door `check_slab_hybrid.py`. Twee opties: (a) helper terug in
`check_slab_hybrid.py` en `ocr_router.py` naar `experiments/`, of (b)
router valideren als productie-optie en documenteren wanneer/hoe je 'm
inschakelt.

**P3.2 — Discord slash-commands verhuizen.** Nu geïmporteerd via symlink in
`webshop-bot`. Als HoS geen webshop-bot meer draait: eigen minimale service
in `discord/`.

**P3.3 — Refactor-fixtures uitdunnen.** Na P2.2/P2.3 (splits gemerged, tests
groen) kunnen veel `tests/fixtures/*_refactor_fixtures/` weg — houd alleen
wat nog actief geïmporteerd wordt.

**P3.4 — Verdere hernoemingen** (`worker_ocr` → `worker_slab_read` etc.) —
puur cosmetisch. Alleen doen als iemand leest en verward raakt.

**P3.5 — `raw_pages`-beslissing.** Blijft dat een lokale mini-DB (`storage/local.py`
met SQLite) of gaat het naar Supabase? Kleine tabel (retentie 3d), dus of/of
mag. Beslissing bepaalt of `storage.py` helemaal weg kan.

---

## Bijlage — Concrete cijfers (2026-09-05)

- Aantal `.py`-bestanden in root: **~50**
- Aantal subdirs in root: **29** (waarvan 15 refactor-fixtures + 3 legacy-archieven + 8 splits + 3 diversen: `data/`, `dev/`, `tests/`, `photos/`, `windows-worker/`, `__pycache__/`, `.pytest_cache/`)
- Totaal LoC in Python-files (root + storage_supabase + analyze_split): **11 023**
- Grootste files: `storage.py` 584, `analyze.py` 521, `storage_supabase_legacy.py` 482, `analyze_split/ebay_phase.py` 482, `analyze_split/score_only.py` 477, `analyze_split/analyze_full.py` 453, `supabase_sync.py` 372
- Refactor-fixture-mappen: **15**
- Legacy-archief-mappen: **3**
- Split-packages: **8** (`analyze_split`, `check_desc_split`, `check_title_split`, `interpret_slab_split`, `judge_sales_split`, `llm_client_split`, `parse_detail_split`, `vision_split`)
- Storage-modules totaal: **12** (`storage.py` + 8× `storage_supabase/*` + `storage_supabase_legacy.py` + `supabase_sync.py` + `sync_retry.py`)
- Cron-taken die Kensa raken: **17** (waarvan **3 puur cutover-tijdelijk**)
- SQLite-DB: **1.02 GB** (`kensa.db`, verdwijnt morgen)
- Backup-DB: **527 MB** (`/home/pi/kensa-refactor-backup-20260831-195849`, kan ook weg)
- Log-files in repo-root ≥ 1 MB: **6** (`pipeline_worker_ebay.log` 30 MB, `pipeline_worker_ocr.log` 18 MB, `pipeline_worker_cache.log` 17 MB, `pipeline_detail.log` 12 MB, `pipeline_scrape.log` 6 MB, `pipeline_supabase_reads.log` 1.4 MB)
- Env-var-branches in business-code: **21+** (verdwijnen na P0.3)

---

**Einde analyse.** Alle voorstellen zijn suggesties — niets is uitgevoerd.
