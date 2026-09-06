# Kensa Cardmarket Worker — build spec voor LLM

## Wat moet dit systeem doen

Dit is een **achtergrond-worker die draait op een Windows PC**. De worker
haalt Cardmarket-URLs op bij een Raspberry Pi (Kensa server, over Tailscale),
scraped elke URL, extraheert **de 3 goedkoopste listings die "PSA10" of
"PSA 10" in de product-description hebben**, en pusht het resultaat terug
naar de Pi.

Er is al een werkend voorbeeld-script bijgesloten (`cm_worker.py`).

**Belangrijkste eis: geen popups, geen console-windows, geen GUI. Volledig
silent in de achtergrond, 24/7.**

## Architectuur

```
┌──────────────────┐          ┌───────────────────────────┐
│  Raspberry Pi    │          │  Windows PC (deze worker) │
│  (Kensa server)  │          │                           │
│                  │  poll →  │  1. GET pending URLs      │
│  Flask endpoints │          │  2. Scrape elke URL       │
│  op poort 8899   │  ← post  │  3. POST resultaat terug  │
│                  │          │                           │
│  DB: kensa.db    │          │  Draait als Task          │
│  (SQLite)        │          │  Scheduler-taak, silent   │
└──────────────────┘          └───────────────────────────┘
```

**Pi Tailscale-IP:** `100.74.4.23` (poort 8899)

## Pi-endpoints (contract)

### `GET http://100.74.4.23:8899/api/cm/pending`

Retourneert tot 20 URLs die nog gescraped moeten worden.

Response (JSON):
```json
[
  {"item_id": "m10228250509", "url": "https://www.cardmarket.com/en/Pokemon/Products/Singles/MEGA-Dream-ex/Mega-Lucario-ex-V2-m2a228?language=7&minCondition=1"},
  {"item_id": "m37388919187", "url": "https://www.cardmarket.com/en/Pokemon/Products/Singles/Battle-Partners/Nidoking-ex-V3-sv10126?language=7&minCondition=1"}
]
```

Als de queue leeg is: `[]` — wacht dan 60s en poll opnieuw.

### `POST http://100.74.4.23:8899/api/cm/result`

Post het scrape-resultaat per item. Body (JSON):

```json
{
  "item_id": "m10228250509",
  "url": "https://www.cardmarket.com/en/Pokemon/Products/Singles/MEGA-Dream-ex/Mega-Lucario-ex-V2-m2a228?language=7&minCondition=1",
  "listings": [
    {"seller": "Nneman54",       "price_eur": 80.00, "description": "PSA10"},
    {"seller": "LethalBulletz",  "price_eur": 89.90, "description": "PSA 10 GEM MINT"},
    {"seller": "DinoHut",        "price_eur": 89.95, "description": "PSA 10"}
  ]
}
```

Bij fout (bv. Cloudflare-block ondanks solve):
```json
{"item_id": "m10228250509", "url": "...", "error": "korte beschrijving"}
```

Pi antwoordt: `{"ok": true, "item_id": "...", "n_listings": 3}`.

## Scrape-logica (dit is de kern)

Per URL:

1. Fetch met **Scrapling `StealthyFetcher.fetch`** — **`solve_cloudflare=True`
   is verplicht** (Cardmarket zit achter Cloudflare Turnstile). Op Windows
   met Chrome geïnstalleerd werkt dit; op Linux headless niet — daarom draait
   deze worker op Windows.

   Getest werkende config:
   ```python
   page = StealthyFetcher.fetch(
       url,
       headless=True,          # geen zichtbaar venster
       network_idle=True,      # wacht tot netwerk stil is
       solve_cloudflare=True,  # verplicht voor CM
   )
   ```

2. Parse HTML met BeautifulSoup — listings zitten in `div.article-row`.

3. **Filter:** houd alleen rijen waar de tekst in de rij het patroon
   `PSA\s*10` bevat (dus `PSA10`, `PSA 10`, `psa 10`, hoofdletter-ongevoelig).
   Skip alles met andere graders (BGS/CGC/SGC/etc) of PSA 9 e.d.

4. **Prijs extractie:** binnen elke rij zit `span.color-primary` met de prijs
   in `€`-format. Cardmarket gebruikt Europees formaat (`1.234,56 €`). Parse
   ook `1,234.56` en `123,45` correct.

5. **Sorteer op prijs oplopend** en **pak de 3 goedkoopste**.

6. **Per behouden listing:**
   - `seller` — naam uit `a.seller-name` / `.seller-info a` in dezelfde rij
   - `price_eur` — float
   - `description` — korte tekst uit `.product-attributes` / `.article-info`
     (max ~200 chars), zodat de gebruiker in het dashboard kan zien wat de
     verkoper zegt over grading / conditie / variant

7. Post terug naar `/api/cm/result`.

## Poll-schema

- Continue draaien (Task Scheduler taak die start bij Windows-login, geen
  console, `pythonw.exe`).
- Loop: poll `/api/cm/pending` → verwerk → wacht 4s tussen scrapes (respectvolle
  pauze) → als queue leeg, wacht 60s.

## Config / defaults

- `PI_URL`  = `http://100.74.4.23:8899`
- `POLL_INTERVAL_SEC` = 60 (bij lege queue)
- `BETWEEN_SCRAPES_SEC` = 4
- `MAX_LISTINGS` = 3

## Silent draaien op Windows (achtergrond, geen console-window)

**Gebruik `pythonw.exe`** (zonder console) en configureer via Task Scheduler:

1. Task Scheduler → Create Task (niet Basic Task)
2. General tab:
   - Naam: `Kensa CM Worker`
   - Run whether user is logged on or not ✓
   - Hidden ✓
3. Triggers → New → At log on
4. Actions → New:
   - Program: `pythonw.exe`  (het venster-loze Python)
   - Arguments: `C:\kensa\cm_worker.py`
   - Start in: `C:\kensa\`
5. Conditions → uncheck "Start only if on AC power"
6. Settings → "If the task is already running, do not start a new instance"

Log wordt naar `C:\kensa\worker.log` geschreven (rotating, max 5 MB).

## Installatie (eenmalig)

```powershell
# 1. Python 3.10+ zorgen dat aanwezig is (bij twijfel: python --version)
# 2. Dependencies
pip install scrapling beautifulsoup4 requests
scrapling install       # installeert patchright browser

# 3. Kopieer cm_worker.py naar C:\kensa\
# 4. Test 1x zichtbaar:
python cm_worker.py
# → zie output "Kensa CM worker — polling ..." en scrape-resultaten

# 5. Als test werkt: activeer Task Scheduler entry (hierboven)
# 6. Reboot Windows, verifieer via C:\kensa\worker.log dat hij draait
```

## Testen zonder wachten op cron

Vanaf de Pi (of via curl vanaf Windows) kun je handmatig een URL op de queue
zetten:

```bash
curl -X POST http://100.74.4.23:8899/api/cm/enqueue \
  -H 'Content-Type: application/json' \
  -d '{"item_id":"test-lucario","url":"https://www.cardmarket.com/en/Pokemon/Products/Singles/MEGA-Dream-ex/Mega-Lucario-ex-V2-m2a228?language=7&minCondition=1"}'
```

Binnen 60s picket de worker hem op. Verifieer op de Pi:
```bash
sqlite3 /home/pi/.openclaw/workspace/agents/kensa/kensa.db \
  "SELECT item_id, fetched_at, error, substr(listings_json,1,200) FROM cardmarket_queue;"
```

## Foutafhandeling

- Netwerkfout naar Pi → wacht 60s, opnieuw polls (geen exit)
- Cloudflare failure na `solve_cloudflare=True` → post `{error: "cloudflare block after solve"}` terug zodat Kensa de fout kan tonen
- Scrapling-crash op één URL → catch, log, ga door naar volgende URL
- Onparseerbare listing → skip, ga door met volgende

## Referenties

- Bijgesloten voorbeeld-script: `cm_worker.py` (werkende basis)
- Scrapling docs: <https://github.com/D4Vinci/Scrapling>
- Cardmarket product-page voorbeeld:
  <https://www.cardmarket.com/en/Pokemon/Products/Singles/MEGA-Dream-ex/Mega-Lucario-ex-V2-m2a228?language=7&minCondition=1>
