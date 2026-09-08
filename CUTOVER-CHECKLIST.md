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
| 3 | **eBay-worker** (`cron_worker_ebay.sh`) | 🔄 **LIVE Supabase-only sinds 2026-09-07 09:09** | ✔ CM-blokkade weg (bevoorrader). Pick/claim al Supabase. Verstopte valkuil gefixt: `_price_cache_upsert_ebay` kreeg de write-dispatcher (schreef altijd Pi, negeerde schakelaar) — 3 regressietests | 2026-09-07 09:09 | _beoordelen 2026-09-08 ~09:15_ |
| 4 | **Detail** (`cron_detail.sh`, `cron_detail_mercapi.sh`) | 🔄 **LIVE Supabase-only sinds 2026-09-07 20:00** | ✔ `detail_stuck_detector.py` + `detail_dead_marker.py` geport naar Supabase (lezen+schrijven; extra_json is daar **jsonb**, dus key-filter i.p.v. LIKE en dicts i.p.v. strings; dead-PATCH server-side geconditioneerd op status=new + detail=null; retry op transiente netwerkfouten). 24u-gate bewust overgeslagen op besluit Tommy (batches 1-3 al 11-23u schoon). | 2026-09-07 20:00 | _beoordelen 2026-09-08 ~20:00_ |
| 5 | **Scraper** (`cron_scrape.sh`) | 🔄 **LIVE Supabase-only sinds 2026-09-08 13:03** | ✔ voorwaarden bleken al veilig: upsert_listing/mark_seen_in_search/record_cert_sighting/upsert_photos hebben supabase-pad; `is_new` komt correct uit Supabase (alerts vuren goed); `dedup_listings.py` is al Supabase-native (`sb_ids_to_delete`); `alerts.py` leest de `alerts`-watches-tabel (blijft Pi, migreert niet). save_raw_page blijft Pi. | 2026-09-08 13:03 | _beoordelen 2026-09-09 ~13:00_ |
| 6 | **Cardmarket-wachtrij + Windows-worker** (`cm_queue_api.py`, `cron_cm_bevoorrader.sh`) | 🔄 **LIVE Supabase-only sinds 2026-09-08 13:59** | ✔ Stap 1 (13:19): resultaat-write robuust (vangnet). ✔ Stap 2 (13:59): `/api/cm/pending` + `/health` uit Supabase (`KENSA_STORAGE=supabase` in de systemd-unit), `/result` alleen Supabase (PATCH met return=representation → card_key voor price_cache), bevoorrader leest wachtrij-stand uit Supabase en schrijft alleen Supabase; 5u-cleanup van verlopen bestellingen geport naar de bevoorrader (Supabase). Pre-check: Pi en Supabase pending identiek (83/83, zelfde ids, 968/968 fetched-24u). **Windows-worker onaangeraakt** — praat met dezelfde Pi-API, JSON-vorm byte-voor-byte gelijk (getest op poort 8897 naast live). 16k oude Pi-only rijen blijven bewust ongesynct. | 2026-09-08 13:59 | _beoordelen 2026-09-09 14:00 (automation)_ |

## Afhankelijkheden op de Pi (wie leest nog SQLite)

| Script | Leest op Pi | Schrijft | Actie |
|---|---|---|---|
| `alerts.py` | ~~listings~~ + `alerts`-watches | Discord-melding | ✔ klaar: leest géén listings (item komt van caller), alleen de `alerts`-watches-tabel (blijft Pi, migreert niet). is_new uit Supabase. |
| `dedup_listings.py` | `listings` (ranking per card_key) — **Pi én Supabase apart** | delete op Pi én Supabase | ✔ klaar: `sb_ids_to_delete()` leest+rankt Supabase-native; Pi-tak raakt alleen de bevroren mirror. NB: nieuwe Pi-items hebben geen card_key (veilige kant: minder verwijderen) |
| `detail_stuck_detector.py` | ~~Pi~~ → **Supabase** (sinds 2026-09-07) | retry-vlag in Supabase extra_json | ✔ klaar |
| `detail_dead_marker.py` | ~~Pi~~ → **Supabase** (sinds 2026-09-07) | status=dead op beide (Supabase leidend) | ✔ klaar |
| `stale_lock_cleanup.py` | — | locks op beide | Pi-deel weg bij einde |
| `cm_queue_api.py` | ~~cardmarket_queue~~ → **Supabase** (sinds 2026-09-08 13:59) | Supabase-only | ✔ klaar (#6 stap 2); dual-pad blijft in de code als rollback (`KENSA_STORAGE=dual`) |
| `cm_bevoorrader.py` | ~~wachtrij-pending (Pi)~~ → **Supabase** | CM-wachtrij Supabase-only | ✔ klaar (#6 stap 2); Pi-pad blijft als rollback (`KENSA_*_STORAGE=dual`) |
| `cron_scrape.sh` cm-cleanup | cardmarket_queue (Pi) | delete pending >5u op Pi | doet sinds #6 stap 2 niets meer (Pi bevroren); Supabase-equivalent zit in `cm_bevoorrader.ruim_verlopen_bestellingen_op`. Regel weghalen bij code-opruiming. |
| `scripts/monitor-cm-queue.py` | cardmarket_queue (Pi) | Discord-alarm | **UIT** (crontab 2026-09-08): las bevroren Pi, drempel 15 was permanent alarm, send kapot; `kensa_bewaker` dekt CM |
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
- [x] ~~historische `analysis[roi]`-misses backfillen~~ — **VERVALT** (Tommy 2026-09-08: Kensa is snel-en-vluchtig, augustus-listings zijn verkocht/oninteressant; nooit meer voorstellen)
- [x] **Monitoring geconsolideerd → `kensa_bewaker.py`** (2026-09-08, cron `0 */6` naar #algemeen). Vervangt: `sync-drift-check.py` (UIT — Pi bevroren na cutover, drift verwacht + backfill = risico), `monitor-cutover-cache.py` (UIT — transitie batch 2, klaar + kapot) en de losse 6u-cron van `report_supabase_writes.py` (logica hergebruikt). Vier secties: schrijven / doorstroom / workers / cardmarket, één verdict. Fixes: 409/23505-duplicaten tellen niet als fout; lees-hikjes pas ORANJE bij >20 per venster.
- [x] beslissing Gemini-cache (`llm_slab_cache`) en `alerts`-watches: **blijven bewust op de Pi** (Tommy 2026-09-08) — kostencache resp. eigen watch-regels, geen bedrijfsdata
- [x] Windows CM-worker op Supabase (#6): stap 1 klaar 2026-09-08 13:19 (CM-resultaat robuust naar Supabase via `cm_queue_api`); **stap 2 klaar 2026-09-08 13:59** (`/pending` uit Supabase, bevoorrader Supabase-only, Pi-writes uit). Windows-worker zelf onaangeraakt (praat met de Pi-API = Supabase-brug). **Daarmee stroomt er geen live bedrijfsdata meer via de Pi-SQLite: migratie functioneel af.** 24u-oordeel: 2026-09-09 14:00.
- [ ] `raw_pages` (HTML-cache 3d) en `sync_retry` (wachtrij) mogen op de Pi blijven — dat is geen bedrijfsdata

## Opruimen ná de cutover (code)

_**GEPARKEERD** (Tommy 2026-09-08): pas oppakken als alles aantoonbaar een tijd stabiel loopt. Geen functionele wijziging, alleen vereenvoudiging._

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
- 2026-09-07 09:09 — **#3 eBay-worker → Supabase-only.**
- 2026-09-07 ~19:45 — Doorlichting vóór #4: retry-vangnet photos bouwde rows met oude kolom `photo_url` i.p.v. `url_original` (bron van de PGRST204-poison-rijen, zelfde klasse als de 55 van 6 sept) → gefixt in `storage.py`. 6-uurs Discord-rapport bleek stil kapot sinds node-upgrade (cron-PATH zonder nvm: `openclaw` én `node` onvindbaar) → binary-resolver + PATH-fix, live bewezen. eBay-worker cadans */8 → */4 (zelfde volume, halve wachttijd).
- 2026-09-07 20:00 — **#4 Detail → Supabase-only.** Guards geport (zie tabel): let op, Supabase `extra_json` is **jsonb** — LIKE-filters werken daar niet (42883) en lezen/schrijven gaat als dict, niet als JSON-string. `save_raw_page` heeft bewust geen dispatcher en blijft Pi-lokaal. Baseline freeze-bewijs: Pi 73.438 details / max 17:49:28Z om 19:59:41 CEST.
- 2026-09-08 13:03 — **#5 Scraper → Supabase-only.** Voorwaarden bleken al veilig (geen code-port nodig). Freeze-bewijs: Pi 75.275 listings, Supabase +9 in de testronde.
- 2026-09-08 13:19 — **#6 stap 1:** `cm_queue_api` /result via `_http.patch` + `price_cache.upsert_cm` (vangnet) i.p.v. fire-and-forget. Live bewezen (darkrai:114/081).
- 2026-09-08 13:59 — **#6 stap 2 → Supabase-only (LAATSTE CUTOVER).** Pre-check Pi↔Supabase pending identiek (83/83). API-vorm gelijk getest op testpoort 8897 (`diff` op limit=100: identiek). `grade=neq.`-filter bewezen op `test_cardmarket_queue` ('' en NULL komen niet door). Bewijs na flip: 5 worker-uitslagen + 5 price_cache-updates + 6 bestellingen alleen in Supabase (16/16 writes ok), Pi bevroren op 56.061 / 11:59:07Z. Bijvangst: `scripts/monitor-cm-queue.py` uit crontab (las Pi, permanent alarm, send kapot). Backups: `backups/*_voor_stap2_20260908_135913*`.
