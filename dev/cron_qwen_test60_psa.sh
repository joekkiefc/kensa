#!/bin/bash
# PSA-scheidsrechter, elke 20 min, 8 opzoekingen (PSA blokkeert bij meer).
# Eerst test-60 afmaken; zodra alle 60 geprobeerd zijn schakelt hij VANZELF over
# op de schaduw (fase 3b): cert-verschillen + controle-steekproef. (Tommy 9-9: "goed idee")
# 9-9 15:37 Tommy: "de 59 is goed zo, stop die maar" → drempel 59, meteen door op de verschillen.
set -u
KENSA="/home/pi/.openclaw/workspace/agents/kensa"
PY="/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3"
cd "$KENSA"
KLAAR=$("$PY" -c "import json;t=json.load(open('dev/qwen_test60/truth.json'));print(1 if len(t)>=59 else 0)" 2>/dev/null || echo 0)
if [ "$KLAAR" = "1" ]; then
  exec /usr/bin/flock -n /tmp/kensa-psa-check.lock env PSA_MAX=8 PSA_PAUZE=14 "$PY" dev/qwen_shadow_psa_check.py >> dev/qwen_shadow/psa_cron.log 2>&1
else
  exec /usr/bin/flock -n /tmp/kensa-psa-check.lock env PSA_MAX=8 PSA_PAUZE=14 "$PY" dev/qwen_test60.py --psa >> dev/qwen_test60/psa_cron.log 2>&1
fi
