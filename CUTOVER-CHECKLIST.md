# Kensa — Cutover-checklist SQLite → Supabase-only

_Doel: alle bedrijfsdata van Kensa alleen nog in Supabase. De Pi houdt alleen
transport-buffers (retry-wachtrij), tijdelijke caches en bestanden._
_Levend document. Bijwerken bij elke stap. Laatste update: 2026-09-07._

## Hoe we omzetten (de regels)

1. **Eén systeem tegelijk.** Per cron-script `KENSA_WRITE_STORAGE=dual` → `=supabase`.
   Rollback = die ene regel terugzetten. Reads staan al overal op Supabase.
2. **24 uur kijken vóór de volgende.** Bron van waarheid: `report_supabase_writes.py`
   (elke 6u in #algemeen, of handmatig). Pas door bij 🟢 GROEN over het hele venster.
3. **Bewijs dat het echt Supabase-only is:** de Pi-tellingen van de tabellen die het
   systeem schrijft stoppen met groeien; de Supabase-tellingen groeien door.
   Meetmethode: per-dag `COUNT` aan beide kanten, nooit id-lijsten vergelijken
   (Supabase geeft max 1000 rijen per request — zie memory `kensa-supabase-reconciliation-method`).
4. **Vangnet is voorwaarde.** Elke Supabase-only write loopt via `storage_supabase/_http.py`;
   tijdelijke fouten (netwerk/DNS/timeout/5xx/429) parkeren in `sync_retry`, de drainer
   speelt ze af. Blijvende fouten (4xx) worden gelogd, niet geparkeerd. DELETE heeft geen vangnet.
5. **Niet omzetten zolang iets op de Pi nog naar de output van dat systeem kijkt.**
   Zie de afhankelijkheden-tabel hieronder.

## Volgorde en status

| # | Systeem (cron) | Status | Voorwaarden vóór omzetten | Omgezet op | 24u-oordeel |
|---|---|---|---|---|---|
| 1 | **OCR-worker** (`cron_worker_ocr.sh`) | 🔄 **LIVE Supabase-only sinds 2026-09-06 21:13** | geen — alle afnemers lezen Supabase | 2026-09-06 21:13 | _open — beoordelen 2026-09-07 ~21:15_ |
| 2 | **Cache-worker** (`cron_worker_cache.sh`) | 🔄 **LIVE Supabase-only sinds 2026-09-07 08:21** | ✔ CM-blokkade opgelost: enqueue verhuisd naar `cm_bevoorrader.py` (schrijft dual); score_only raakt CM niet meer aan. Dispatchers in score_only gelijkgetrokken met analyze.py | 2026-09-07 08:21 | _beoordelen 2026-09-08 ~08:30_ |
| 3 | **eBay-worker** (`cron_worker_ebay.sh`) | ⏳ ONTBLOKKEERD | CM-blokkade weg (bevoorrader). Pick/claim al Supabase ✔. Flippen ná 24u groen op #2 | | |
| 4 | **Detail** (`cron_detail.sh`, `cron_detail_mercapi.sh`) | ⏳ wacht | `detail_stuck_detector.py` + `detail_dead_marker.py` lezen `detail_scraped_at` op de Pi. Zonder Pi-writes lijken ALLE z-items "nooit gedetaild" → dead-marker zet ze **dood in Supabase**. Beide scripts éérst op Supabase laten lezen. | | |
| 5 | **Scraper** (`cron_scrape.sh`) | ⏳ wacht | `alerts.py` (Discord nieuwe-listing-alerts = thermometer) leest Pi; `dedup_listings.py` beslist op Pi-data. Beide éérst op Supabase. | | |
| 6 | **Cardmarket-wachtrij + Windows-worker** | ⏳ LAATSTE (Tommy) | `cm_queue_api.py` serveert uit Pi-SQLite; 16k oude Pi-only rijen bewust niet gesynct (niet backfillen). Apart project. | | |

## Afhankelijkheden op de Pi (wie leest nog SQLite)

| Script | Leest op Pi | Schrijft | Actie |
|---|---|---|---|
| `alerts.py` | listings + alerts | alerts (Pi-only) | ompunten naar Supabase (alerts-tabel bestaat al) — vóór #5 |
| `dedup_listings.py` | listings (ranking per card_key) | delete op Pi én Supabase | ranking uit Supabase halen — vóór #5. NB: sinds #1 krijgen nieuwe items op de Pi geen card_key meer → dedup neemt ze op de Pi niet mee (veilige kant: minder verwijderen) |
| `detail_stuck_detector.py` | listings (z-items zonder detail) | retry-vlag Pi-only | Supabase lezen + schrijven — vóór #4 |
| `detail_dead_marker.py` | listings + retry-vlag | status=dead op beide | Supabase lezen — vóór #4 |
| `stale_lock_cleanup.py` | — | locks op beide | Pi-deel weg bij einde |
| `cm_queue_api.py` | cardmarket_queue | dual | Supabase serveren — #6 |
| `cm_bevoorrader.py` | wachtrij-pending (Pi, CM-domein) | CM-wachtrij dual | bij #6: schrijfadres omzetten — dé enige plek |
| `worker_claim.py` | listings (lock) | Pi-only | alleen terugval; verwijderen bij einde |
| `llm_client.py` | llm_slab_cache | Pi-only (Gemini-antwoorden, 30d) | beslissing: tabel in Supabase of accepteren (kostencache, geen correctheid) |

## Per omzetting: wat je de eerste 24u controleert

- [ ] `report_supabase_writes.py` → 🟢: geen blijvende fouten, geen nieuwe dead-letters, <2% geparkeerd
- [ ] foutklassen: geen `dns`/`tls`; `read_timeout`/`conn_reset` hooguit incidenteel (< 2%)
- [ ] latency p95 ok-writes < 1500 ms
- [ ] retry-queue "nieuw in venster" loopt leeg (drainer gelukt ≈ geparkeerd)
- [ ] Pi-tellingen van de geschreven tabellen stoppen met groeien, Supabase groeit door
- [ ] de rest van de pipeline merkt niks: cache/eBay-workers vinden `ocr_done`-items, webapp toont nieuwe slabs
- [ ] `pipeline_worker_*.log`: geen tracebacks, geen `[storage_supabase] ... BLIJVENDE fout`

## Vóór de laatste stap (SQLite echt uit)

- [ ] alle 6 systemen 🟢 over 24u
- [x] 82 oude `sync_retry`-rijen opgeruimd op 2026-09-06 21:24 (55× photos `photo_url` stale, 25× analysis 409-dup, 2× photos dup — backup in /home/pi/kensa-backups/)
- [ ] ~350 historische `analysis[roi]`-misses (17 aug–2 sep) gebackfilld, andere traps met dezelfde per-dag-methode gecheckt
- [ ] drift-monitor `scripts/sync-drift-check.py` verbreed naar álle tabellen (per-dag count) — nu alleen listings
- [ ] `scripts/monitor-cutover-cache.py` gerepareerd (valt sinds update 2026.9.2 over `openclaw`-ownership) of verwijderd
- [ ] beslissing Gemini-cache (`llm_slab_cache`) en `alerts` genomen en uitgevoerd
- [ ] Windows CM-worker op Supabase (#6)
- [ ] `raw_pages` (HTML-cache 3d) en `sync_retry` (wachtrij) mogen op de Pi blijven — dat is geen bedrijfsdata

## Opruimen ná de cutover (code)

- [ ] dual/sqlite-takken uit `storage.py`, `analyze.py`, `analyze_split/*`, `storage.py` dispatchers
- [ ] `supabase_sync.py` sync_* helpers (dual-route) → alleen retry/drain-deel bewaren
- [ ] `worker_claim.py`, SQLite-fallbacks in workers, `_ensure_*_table` import-bijwerkingen
- [ ] `storage_supabase_legacy.py` hernoemen (is NIET legacy — bevat de Supabase-reads)
- [ ] crontab: globale `KENSA_STORAGE=dual` en per-cron `KENSA_*_STORAGE` weg (of vast op supabase)
- [ ] `ANALYSE-KENSA.md` sectie "Na de cutover" afvinken

## Log

- 2026-09-06 07:36 — netwerk-fixes (timeout-split, Session-pool, upsert merge-dup, backoff). Lees-fouten: 198 in de nacht, 2 daarna.
- 2026-09-06 21:05 — vangnet op Supabase-only route (`_http.py`), end-to-end bewezen, 10 tests.
- 2026-09-06 21:13 — **#1 OCR-worker → Supabase-only.** Schrijf-log + rapport live.
- 2026-09-07 08:15 — **`cm_bevoorrader.py` live** (cron */3): één beslisser voor CM-prijzen (Tommy's architectuur-wens, simpele NL naam). Leest Supabase, wachtrij dual. Dempers: 40/ronde nieuwste-eerst, skip bij >150 pending, 24u herprobeer-administratie. CM-enqueue **verwijderd** uit score_only (workers scoren alleen nog).
- 2026-09-07 08:21 — **#2 Cache-worker → Supabase-only.** Vooraf: `_save_trap`/`_mark_slab_status` in score_only kregen dezelfde KENSA_WRITE_STORAGE-dispatcher als analyze.py (hadden die niet — flip zou anders stil dual blijven voor traps).
