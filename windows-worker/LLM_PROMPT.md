# Instructie voor LLM — Kensa Cardmarket Worker deployen

## Wat jij moet doen

Deploy een Python worker op deze Windows PC die Cardmarket-URLs scraped
in de achtergrond en resultaten naar een Raspberry Pi (Kensa server)
pusht via Tailscale.

**Vereisten van de gebruiker:**
- Volledig **silent**: geen console-window, geen popup, geen tray-icon
- Draait 24/7 in de achtergrond
- Start automatisch na Windows-login
- Log naar file zodat de gebruiker later kan checken wat er gebeurd is
- Overleeft reboots

## Wat je krijgt

De gebruiker levert 2 bestanden aan:
- `cm_worker.py` — het worker-script (draait als daemon-loop)
- `README.md` — technische spec + architectuur

De code werkt al. Jouw taak is uitsluitend: **installeren + inrichten**.

## Stappen die je moet uitvoeren

### 1. Verifieer omgeving
```powershell
python --version         # moet 3.10+ zijn
tailscale status         # moet actieve verbinding tonen
ping 100.74.4.23         # Pi moet bereikbaar zijn
```

Als Python < 3.10: installeer via `winget install Python.Python.3.12`.
Als Tailscale niet actief: log de gebruiker in en run `tailscale up`.

### 2. Installeer dependencies
```powershell
pip install scrapling beautifulsoup4 requests
scrapling install
```

Als `scrapling install` mist: `python -m patchright install chromium`.

### 3. Zet worker op vaste plek
Kopieer `cm_worker.py` naar `C:\kensa\cm_worker.py`. Maak de map als hij
niet bestaat.

### 4. Test 1× zichtbaar (foreground, met console)
```powershell
cd C:\kensa
python cm_worker.py
```

Verwacht: geen output op scherm (log gaat naar file), maar `C:\kensa\worker.log`
begint volgeschreven te worden binnen 60s met regels als:
```
2026-XX-XX HH:MM:SS  INFO   Kensa CM worker start — polling http://100.74.4.23:8899
```

Om te testen dat scraping werkt, zet handmatig een URL op de queue van
de Pi (kan vanuit Windows via curl):
```powershell
curl -X POST http://100.74.4.23:8899/api/cm/enqueue `
  -H "Content-Type: application/json" `
  -d "{\"item_id\":\"test-lucario\",\"url\":\"https://www.cardmarket.com/en/Pokemon/Products/Singles/MEGA-Dream-ex/Mega-Lucario-ex-V2-m2a228?language=7&minCondition=1\"}"
```

Binnen 1-2 min: log toont "scrape test-lucario" + "→ 3 listings".
Als je 3 listings ziet in worker.log: **de scrape werkt**. Ctrl+C om te
stoppen.

### 5. Zet als silent background-task (Task Scheduler)

Open Task Scheduler → **Create Task** (niet Basic Task, want we hebben
Hidden-flag nodig).

**General:**
- Name: `Kensa CM Worker`
- Description: `Cardmarket scraper worker voor Kensa (Pi via Tailscale)`
- Security: `Run whether user is logged on or not` ✓
- `Hidden` ✓
- Configure for: Windows 10/11

**Triggers → New:**
- Begin the task: `At log on`
- Any user
- Repeat task every 5 minutes for a duration of Indefinitely (safety, mocht process crashen)
- Delay task for 30 seconds (wacht tot Tailscale up is)

**Actions → New:**
- Action: `Start a program`
- Program/script: `pythonw.exe`  ← **belangrijk: pythonw NIET python** (geen console)
- Add arguments: `C:\kensa\cm_worker.py`
- Start in: `C:\kensa\`

**Conditions:**
- Uncheck: "Start the task only if the computer is on AC power"
- Uncheck: "Stop if the computer switches to battery power"

**Settings:**
- If the task is already running: `Do not start a new instance`
- Allow task to be run on demand ✓

Save. Enter Windows-wachtwoord als gevraagd (voor "run whether logged on").

### 6. Verifieer
```powershell
# Task moet enabled staan
Get-ScheduledTask -TaskName "Kensa CM Worker"

# Trigger 'm handmatig voor test
Start-ScheduledTask -TaskName "Kensa CM Worker"

# Check dat er een pythonw.exe proces loopt
Get-Process pythonw

# Check log
Get-Content C:\kensa\worker.log -Tail 20
```

Als er `pythonw.exe` in de proceslijst staat + je ziet activiteit in
worker.log: **klaar**. Reboot Windows één keer om te verifiëren dat hij
automatisch start.

### 7. Rapporteer aan gebruiker
- Bevestig: worker draait silent op de achtergrond
- Geef pad naar log-file: `C:\kensa\worker.log`
- Uitleg: worker polt elke 60s bij lege queue, verwerkt jobs direct als
  ze binnenkomen, respectvolle 4s pauze tussen scrapes
- Config aanpassen: PI_URL / intervals bovenaan `cm_worker.py`

## Wat je NIET moet doen
- Geen console-window openlaten
- Geen tray-icon of GUI toevoegen
- Geen wijzigingen aan cm_worker.py's scrape-logica (die is al getest en werkt)
- Geen notificaties naar de gebruiker sturen tijdens normaal draaien
- Geen wachtwoorden of API-keys in nieuwe files hardcoden

## Fallback als iets faalt
- Cloudflare-block ondanks `solve_cloudflare=True`: `scrapling install` opnieuw
  (browser-versie updaten)
- Tailscale-verbinding weg: worker retryt vanzelf elke 60s
- `pythonw.exe` niet gevonden: Python installatie deed niet "Add to PATH" — voeg toe
  of gebruik volledige pad `C:\Users\<user>\AppData\Local\Programs\Python\Python312\pythonw.exe`
- Task Scheduler weigert Hidden-flag: run PowerShell als admin, of gebruik
  `schtasks /create` command-line (in de README staat de shell-variant)
