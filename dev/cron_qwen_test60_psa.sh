#!/bin/bash
# TIJDELIJK — PSA-waarheid voor test-60 in porties (429-vriendelijk). Verwijderen na 60/60.
set -u
cd /home/pi/.openclaw/workspace/agents/kensa
exec /usr/bin/flock -n /tmp/kensa-test60-psa.lock env PSA_MAX=8 PSA_PAUZE=14 \
  /home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3 dev/qwen_test60.py --psa >> dev/qwen_test60/psa_cron.log 2>&1
