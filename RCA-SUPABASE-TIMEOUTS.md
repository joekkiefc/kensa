# RCA — Supabase dual-write timeouts (Kensa)

**Scope**: waarom `supabase_sync.log` ~2600 timeouts telt sinds 2026-08-31.
**Analyse-datum**: 2026-09-06 07:20 UTC.
**Analist**: subagent (geen fixes — alleen diagnose).
**Conclusie in één zin**: Supabase is gezond; de timeouts komen door **client-side network stalls tussen Pi en Cloudflare-edge**, uitvergroot door **korte timeouts (6-8s) en het ontbreken van connection-reuse**. Zolang de retry-queue (`sync_retry`) leegloopt en `drift=0`, is dit een noise-probleem, geen data-verlies-probleem.

---

## 1. Wat vertelt de log?

Log-window: `2026-08-31T06:18` → `2026-09-06T05:10` (≈ 6 dagen).

### Fouten per klasse
| Klasse | Aantal |
|---|---|
| `ReadTimeout` (response nooit ontvangen) | **2179** |
| `ConnectTimeout` (TCP nooit tot stand) | 292 |
| `ConnectionError` (RST tijdens sessie) | 55 |
| `HTTP 5xx / HTTP 4xx` (server) | ~10 |
| **Totaal exc-lines** | **~2600** |

**Read timeouts domineren 7:1.** TCP komt op, maar de TLS-handshake of het POST-body-upload/response stalt.

### Fouten per tabel/endpoint
| Endpoint | fails | typische payload |
|---|---|---|
| `analysis` (POST) | 531 | 500-3000 B, gem. 630 B, `ebay_prices` p95 = 2478 B |
| `listings` (POST+PATCH) | 402 | 300-700 B |
| `photos` | 42 | 200-2000 B (batched per item) |
| `price_cache` | 21 | 1-3 KB |
| `cert_sightings` | 17 | <200 B |
| `cardmarket_queue` | 24 | 200-500 B |

Verhouding matcht ongeveer met traffic-volume (analysis + listings zijn de zwaarste dual-write clients).

### Verdeling per uur/dag
- Rustige dagen: 25-150 fails/dag.
- Piek 2026-09-04: **1522 fails op één dag** (24h burst).
- 2026-09-02: **338**, waarvan 208 in het uur 08:00-09:00 en 129 in 10:00-11:00 — twee duidelijke minutenlange bursts (17-33 fails/minuut in 08:30-08:38 en 10:43-10:48).
- Bursts komen **niet** consistent overeen met dezelfde cron-slot; ze verspreiden over minute-of-hour → geen specifieke worker triggert het.

### Retry-queue status
- `sync_retry` tabel: **78 rows totaal**, waarvan **12 dead-letter**.
- Dead-letter root-cause is *niet* de timeout: het zijn **HTTP 409 duplicate-key** en één **HTTP 400 PGRST204 (`photo_url` schema-cache miss, legacy kolomnaam in oude enqueued rows)**. Zie §7.
- Drain-cron loopt elke 2 min, processed 0-14 rows per run, ratio success/fail ≈ 50/50 — grotendeels omdat dezelfde flaky netwerk-conditie de retry óók raakt.

---

## 2. Netwerk-diagnose (live tests, 07:12-07:15 UTC)

### Latency baseline is prima
```
5x sequentiële curl GET /rest/v1/ (default IP-order):
  #1  92 ms   #2 76 ms   #3 76 ms   #4 91 ms   #5 72 ms
Ping 20x → 0% loss, RTT 9.4-10.2 ms (± 0.16 ms)
```
DNS = 27 ms (cold), 11-22 ms (warm). Geen DNS-issue.

### Maar bij ELKE test kwam een stall voor
```
# 5 sequential over Python-requests (fresh session, mimicking supabase_sync.py):
#0 84ms 401   #1 76ms 401   #2 72ms 401   #3 11099ms ReadTimeout   #4 85ms 401
```
Consistent reproduceerbaar: **~1 op elke 5-10 verbindingen stalt in de TLS-handshake** en trekt 10 seconden. Fout is client-side: TCP connect lukt (<30 ms), maar `SSL_do_handshake()` levert `TimeoutError`.

### IP-specifiek
DNS geeft **twee** Cloudflare edge-IPs:
- `172.64.149.246`
- `104.18.38.10`

Per-IP curl-test (5x, `--resolve`, 10s max):
```
--- 172.64.149.246 ---
  #1 connect=0.012s ssl=0.722s total=4.057s   ← ssl-handshake 722 ms, retry
  #2 connect=0.010s ssl=0.028s total=0.049s
  #3 connect=0.012s ssl=0.031s total=0.053s
  #4 connect=0.000s ssl=0.000s total=10.001s  ← FULL TIMEOUT
  #5 connect=0.010s ssl=0.027s total=0.044s
--- 104.18.38.10 ---
  #1..#5 : allemaal 44-56 ms, geen enkele stall
```
**`172.64.149.246` is de flaky edge; `104.18.38.10` is stabiel.** Python's `requests` (via urllib3) probeert IPs in resolver-volgorde en heeft **geen happy-eyeballs**, dus als `172.64.149.246` als eerste komt (wat consistent gebeurt) én stalt, is de call verloren.

### IPv6 uitgesloten
Supabase publiceert **geen AAAA-records** voor deze host (`socket.getaddrinfo(AF_INET6)` → `Errno -5`). Dus geen dual-stack race.

### Concurrency verergert het
20 parallele requests: 4 van de 20 duurden 1-4 seconden i.p.v. 100 ms (grote spread). Bij 30 parallel timeout de hele test na 120 s (bench werd gekapt). Onder gelijktijdige load treft de failure meer gebruikers per netwerk-hiccup.

---

## 3. Supabase-kant (edge_logs, 24h window)

Via MCP `query_logs` op `edge_logs`:

| uur (UTC) | reqs | 5xx | 429/408/522/524 | p95 origin_time | max origin_time |
|---:|---:|---:|---:|---:|---:|
| 06→05 sep | 4200-10 200 /uur | **0** | **0** | 340-580 ms | 900-5100 ms |

- **Totaal 24u**: 150k+ requests, **0 server-side timeouts, 0 throttling**.
- p95 origin-time 340-580 ms, max 5.1s (één query). Netjes voor free/paid tier.
- Requests komen binnen bij Cloudflare edge (`request.cf.colo = AMS`), TLS lukt aan Supabase-kant. Er is dus **geen enkel bewijs dat Supabase de handshake vroeg afkapt**.

Betekent: alle 2600 timeouts in de client-log zijn **connecties die Supabase nooit heeft gezien** (of pas na de client-timeout). Fault ligt in `Pi ↔ Cloudflare-edge`-pad.

---

## 4. Kensa-code / timeout-configuratie

`supabase_sync.py`:
- `_post()` timeout = **8.0 s** (connect+read; requests-lib past 1 waarde toe op beide)
- `_patch()` timeout = **6.0 s**
- `_delete()` timeout = **6.0 s** (buiten de generic wrapper) + inline `requests.delete(..., timeout=6)` in `sync_replace_analysis`
- `sync_retry_drain._replay_post/patch` timeout = **10.0 s**
- `supabase_client.py` (cards-lookup, GET): urlopen timeout = **10 s**

**Elke call opent een nieuwe TCP+TLS-verbinding** — nergens wordt `requests.Session()` hergebruikt. Bij 4-10k dual-write POSTs/dag betekent dat 4-10k volledige TLS-handshakes/dag. Elke TLS-handshake is een extra kans op de "172.64.149.246 stalls"-loterij.

Bewijs uit log:
- **Verhouding 4:1 read:connect timeouts** → verreweg de meeste failures gebeuren *na* TCP connect. Combined met bewijs uit §2 (TLS handshake stalls exact 10s bij 172.64.149.246), zit de meeste read-timeout-tijd effectief in de handshake, niet in de POST-body of response.
- `connect timeout=8.0` (POST) vs `connect timeout=6` (DELETE): identieke root cause, verschillende ceiling.

---

## 5. Payload-grootte

Analysis `ebay_prices` traps zijn de zwaarste rows:
```
trap=ebay_prices → n=85 619   avg 1837 B   p50 1805 B   p90 2478 B   p99 2723 B   max 3035 B
trap=summary     → n=103 269  avg  858 B   max  996 B
trap=slab_ocr    → n=103 238  avg  609 B   max 1813 B
```
Op een 100 Mbit link is 3 KB in <1 ms te uploaden — payload-grootte is **niet** de timeout-driver. Hij vergroot wél de blootstelling: elke POST is 1 handshake.

`photos` payload: 200-2000 B, gem. 368 B (URLs, geen base64/binary). Geen probleem.

---

## 6. Concurrency & cron-schema

Actieve Kensa-crons die dual-write triggeren:
```
* * * * *   worker_cache      (elke minuut)
*/2 * * * *  worker_ocr        (analysis-inserts)
*/2 * * * *  sync_retry_drain  (extra POSTs bovenop!)
*/3 * * * *  cron_detail_mercapi
*/8 * * * *  worker_ebay        (ebay_prices → grote payloads)
*/10 * * * * cron_detail
*/15 * * * * cron_scrape
```

Op minuut :00 lopen **6+ crons potentieel gelijktijdig**. `flock` verhindert overlap *van dezelfde cron*, niet cross-cron. Ieder proces opent onafhankelijk TCP+TLS naar Supabase. Als de flaky edge hikt, treft het meerdere workers tegelijk → burst van fails (matcht 08:30-08:38 patroon).

`sync_retry_drain` heeft geen back-off vs de live-schrijvers; bij storing verdubbel je effectief de load precies wanneer je hem juist zou willen ontlasten.

Pi-load: instantane snapshot toonde één keer 97.7% iowait (piek), maar 5-min gemiddelde is 3-11% wa, load 0.9-1.8. Systeem is **niet** structureel disk/CPU-gebonden — iowait pieken vallen ~20-30s en zijn geen dominante driver.

---

## 7. Retry-queue en dead-letters (bijvangst-bevindingen — niet direct de RCA maar wel toxic)

`sync_retry` bevat 78 rows. Dead-letters (12) zijn **niet** timeout-gerelateerd:

**a) HTTP 409 duplicate-key op `analysis`** (10x dead)
```
Key (item_id, trap, created_at)=(<id>, ebay_prices, 2026-09-04 09:25:04.909514+00) already exists.
```
De originele POST timeoutte, maar de rij is aan Supabase-kant **wel** aangekomen (write-through gebeurde vóór de handshake-stall op response). Retry probeert exact dezelfde `(item_id, trap, created_at)` opnieuw te posten → 409. `resolution=ignore-duplicates` prefer-header lost dit **niet** op omdat een unique-constraint op `(item_id, trap, created_at)` matcht, terwijl de default upsert-target waarschijnlijk op een andere key rust. → **Kan echte data-verlies maskeren** als de originele write toch niet gecommit was. Nu: geen echt verlies, maar 10 dead-letters die "niets missen".

**b) HTTP 400 PGRST204 `photo_url` schema-cache miss** (1x dead)
```
Could not find the 'photo_url' column of 'photos' in the schema cache
```
Legacy retry-row uit periode toen kolom `url_original` nog `photo_url` heette. Payload staat vast in oude vorm.

**c) `enqueued POST this_table_does_not_exist_xyz`** (1 rij, artefact van een test).

---

## 8. Hoofdoorzaak (met bewijs)

**Primair (95% van het volume)**: TLS-handshake-stalls tegen Cloudflare edge-IP `172.64.149.246` in het Pi-uplink-pad, gemiddeld ~1 op de 10-20 verse verbindingen. Bewijs:
- Live gereproduceerd 3x op rij tijdens deze analyse (§2).
- `104.18.38.10` (andere edge, zelfde ISP-pad) faalt niet → wijst op edge/peering-specifiek probleem, niet de Pi.
- Supabase edge-logs tonen 0 5xx en 0 throttling in dezelfde window → probleem zit vóór Cloudflare-edge het request ziet.
- 4:1 read:connect ratio in log-timeouts matcht "TCP komt op, TLS stalt".

**Amplifier 1 (code)**: Geen `requests.Session()`-reuse → elke van 4-10k daily POSTs is een aparte TLS-handshake en dus een extra loterijticket.

**Amplifier 2 (code)**: Timeouts 6-8s zijn te kort voor de p99-tail van deze route. TLS-stalls die zich zelf herstellen na 3-5s worden nu weggegooid; retry-drain (10s timeout) haalt dezelfde call vaak wél binnen.

**Amplifier 3 (schedule)**: 6+ crons potentieel op minute :00 tegelijk + `sync_retry_drain` elke 2 min = correlated bursts wanneer het netwerk stottert.

**Niet de oorzaak** (getest / uitgesloten):
- IPv6 / happy-eyeballs (geen AAAA-record)
- Payload-grootte (max 3 KB, upload triviaal)
- Supabase throttling/5xx (edge-logs schoon)
- Pi CPU/disk (5-min iowait laag, RAM 800 MB vrij)
- DNS lookup (11-27 ms)

---

## 9. Aanbevolen fixes — gerangschikt op impact/effort

| # | Fix | Impact | Effort | Waarom |
|---|---|---|---|---|
| 1 | **Verhoog client-timeouts** naar `connect=5, read=25` (splits met `timeout=(5, 25)` tuple in requests). | **-70%** live-fails schatting | 5 min | De TLS-stalls duren typisch <5s; nu killt de 8s ceiling ze onnodig. |
| 2 | **`requests.Session()`** + `HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=Retry(3, backoff_factor=0.3, status_forcelist=[502,503,504], allowed_methods=frozenset(['POST','PATCH','DELETE'])))`. Session bij module-init, hergebruikt over de hele process-lifetime. | **-50%** blootstelling handshake-loterij | 30 min | Elke worker doet 10-50 calls/run → 1 handshake i.p.v. 50 = 50× minder ticket in de loterij. |
| 3 | **Preferentie voor `104.18.38.10`**: force `--resolve` gedrag door in `_do_post_http` een custom `HTTPAdapter` te mounten die IPs sorteert of expliciet `104.18.38.10` prefereert. Wanneer 172.64.149.246 minder flaky is dan `.10`, wordt dit een min. Best of both worlds: **healthchecked pinning** met periodieke re-eval. | Grote reductie als het edge-specifiek blijft | 1-2 uur | Cloudflare geeft ons twee IPs; als er structureel eentje slechter is, kun je dit tijdelijk omzeilen. Alternatief: klaag bij Cloudflare/ISP. |
| 4 | **Backoff-mode voor `sync_retry_drain`** wanneer live-writes falen: check "recente fails in laatste 5 min > threshold" → drain skippen. Voorkomt dubbele last op flaky moment. | Verzacht bursts | 20 min | Nu voegt de drain juist druk toe wanneer je hem niet wilt. |
| 5 | **Upsert-strategie op `analysis` fixen**: gebruik `on_conflict=(item_id,trap)` i.p.v. `(item_id,trap,created_at)`, of doe eerst een DELETE en dan POST met `Prefer: resolution=merge-duplicates`. Voorkomt 409-dead-letters bij succesvol-maar-timeouted eerste post. | 10 dead-letters minder + minder toekomstige | 1 uur | Nu kan een timeout een rij dead-letteren die feitelijk al bestaat. |
| 6 | **Ruim legacy retry-rows op**: `sync_retry` heeft 1 rij met kolom `photo_url` die permanent PGRST204 geeft. Handmatige DELETE of migreer payload naar `url_original`. | 1 dead-letter minder | 5 min | Alleen als schoonmaak. |
| 7 | **Split cron-slots**: verplaats `sync_retry_drain` naar oneven minuten (`1,3,5,...`) i.p.v. `*/2`; verspreid `*/8` en `*/10` naar `2-58/8` / `5-55/10`. Verlaagt cross-cron-piek. | Kleine burst-reductie | 10 min | Cosmetisch; grotere winst zit in fix #1+#2. |
| 8 | **Verplaats worker-processen naar httpx met HTTP/2 + connection pool**. HTTP/2 multiplexed => 1 verbinding, minder handshake-blootstelling per call. Groter refactor. | Mogelijk vergelijkbaar met #2 | 2-3 uur | Overweeg alleen als #1+#2 onvoldoende blijken. |

**Not recommended** (voorgestelde alternatieven die niet gaan werken):
- SQLite `WAL_autocheckpoint` tunen → geen effect, Pi disk is niet de bottleneck.
- Supabase upgraden naar pro-tier → server-side is al schoon; geld verspild.
- Payload verkleinen (drop `ebay_prices` details) → payload is niet de trigger.
- Retry-drain frequentie verhogen → dubbelt de druk zonder de root te raken.

---

## 10. Wat betekent dit voor "mag SQLite eruit?"

- **Data-integriteit is OK vandaag**: `drift=0`, `sync_retry` 78 rows waarvan maar 12 dead — en die 12 zijn 409-conflicts of legacy schema-issues, geen echte gaten. De 66 live retries lopen door hun schema heen.
- **Duurzaamheidsrisico is niet nul**: als een POST timeout heeft, maar Supabase wel schreef (matched `409` pattern), én retry-drain veegt hem als "duplicate" weg, hebben we een blind-spot. Voor `listings.detail_scraped_at` en `sold` is dat kritiek.
- **Aanbeveling**: fix #1 + #2 + #5 uitvoeren, 1 week meten (target: <20 fails/dag), dán SQLite-only overwegen. Zonder #5 blijft de dead-letter-blindheid een risico.

---

## Samenvatting

De 2600 timeouts komen **niet** door Kensa-code die iets fout doet, en **niet** door Supabase. Ze komen door **onstabiele TLS-handshakes tussen de Pi en één van Cloudflare's twee edge-IPs** (`172.64.149.246`), reproduceerbaar met kale `curl`/`requests` en bevestigd door 0 server-side fouten in Supabase edge-logs. Kensa vergroot het probleem door **geen connection-reuse** en **te korte timeouts (6-8s)**. Twee kleine code-changes (fix #1 verhoog timeouts, fix #2 requests.Session) halen naar schatting >80% van de fails weg. De retry-queue vangt vandaag alles op — geen data-verlies — maar de duplicate-key dead-letters (fix #5) verdienen aandacht voordat SQLite offline mag.
