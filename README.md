# Kensa

Kensa is de deal-hunting / pricing-pipeline van House of Sneijder: scrapet Buyee/Mercari-listings,
verrijkt ze met detail- en OCR-data, prijst kaarten via eBay last-sold + Cardmarket, en synct
alles naar Supabase. Draait als losse cron-services op de Raspberry Pi.

## Live locatie
De code draait live vanuit `/home/pi/.openclaw/workspace/agents/kensa/` op de Pi.
Crons staan in de crontab (`cron_scrape.sh`, `cron_detail*.sh`, `cron_worker_*.sh`, drain, etc.).

## Pipeline (grote lijnen)
- **scrape** → nieuwe listings ophalen (`scrape_buyee.py`)
- **detail** → per listing detailpagina + velden (`fetch_detail*.py`)
- **ocr / cache / ebay** workers → verrijking + prijsbronnen
- **analyze** → ROI-berekening + Cardmarket-queue (`analyze_split/`)
- **supabase sync** → Pi ↔ Supabase, met retry-drainer (`supabase_sync.py`, `sync_retry_drain.py`)

## Belangrijk
- **Secrets** worden centraal geladen uit `/home/pi/.openclaw/secrets.json` — staan NOOIT in deze repo.
- **Data/logs/fixtures/db** worden bewust niet geversioneerd (zie `.gitignore`); dit is een code-repo.
- Netwerk-/timeout-strategie richting Supabase: zie `RCA-SUPABASE-TIMEOUTS.md`.
