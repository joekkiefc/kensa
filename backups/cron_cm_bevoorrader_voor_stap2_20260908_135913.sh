#!/bin/bash
# CM-bevoorrader — dé plek die beslist welke kaarten een Cardmarket-prijs nodig hebben.
# Zie cm_bevoorrader.py. Cron: */3 * * * *
# Lezen uit Supabase (bron van waarheid); wachtrij dual (Pi + Supabase) want de
# Windows-CM-worker leest de Pi tot de CM-migratie als laatste cutover-stap.
export KENSA_READ_STORAGE=supabase
export KENSA_WRITE_STORAGE=dual
export KENSA_STORAGE=dual

cd /home/pi/.openclaw/workspace/agents/kensa || exit 1
exec 9>/tmp/cm_bevoorrader.lock
if ! /usr/bin/flock -n 9; then
  echo "$(date -Iseconds) vorige bevoorrader-ronde loopt nog — skip"
  exit 0
fi
/usr/bin/timeout --kill-after=30s 150s ./cm_bevoorrader.py
