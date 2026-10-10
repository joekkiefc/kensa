#!/bin/bash
# Kensa noodklep — bij hoog Pi-geheugengebruik OCR-parallel omlaag + chromium-detail
# overslaan, ZONDER Kensa te stoppen. Draait elke minuut, zelfde cadans als
# cpu-profile.sh (losse regel direct erna in de crontab).
#
# Normaal (mem < 75%):  WORKER_OCR_PARALLEL=6, fetch_detail.py/proxy draaien gewoon.
# Noodklep (mem >= 85%): WORKER_OCR_PARALLEL=2 (via override-bestand dat
#   cron_worker_ocr.sh leest), fetch_detail.py + fetch_detail_proxy.py in
#   cron_detail.sh worden overgeslagen (zwaarste chromium-load van de 7 pipelines).
# Hysteresis (85% aan / 75% uit) voorkomt flapperen rond de drempel.
# Logt alleen bij statuswissel, niet elke minuut-run.

set -u

KENSA_DIR="/home/pi/.openclaw/workspace/agents/kensa"
LOG="$KENSA_DIR/noodklep.log"
FLAG="$KENSA_DIR/.noodklep_active"
PARALLEL_OVERRIDE="$KENSA_DIR/.worker_ocr_parallel_override"
SKIP_CHROMIUM="$KENSA_DIR/.noodklep_skip_chromium"

HIGH=85
LOW=75

MEM=$(free | awk '/Mem:/ {printf "%d", $3/$2*100}')
TS=$(date '+%Y-%m-%d %H:%M:%S')

if [ -f "$FLAG" ]; then
  if [ "$MEM" -lt "$LOW" ]; then
    rm -f "$FLAG" "$PARALLEL_OVERRIDE" "$SKIP_CHROMIUM"
    echo "[$TS] NORMAAL — mem=${MEM}% < ${LOW}%, noodklep uit (WORKER_OCR_PARALLEL=6, chromium-detail weer aan)" >> "$LOG"
  fi
else
  if [ "$MEM" -ge "$HIGH" ]; then
    echo "2" > "$PARALLEL_OVERRIDE"
    touch "$SKIP_CHROMIUM"
    touch "$FLAG"
    echo "[$TS] NOODKLEP AAN — mem=${MEM}% >= ${HIGH}%, WORKER_OCR_PARALLEL=2, fetch_detail.py/proxy (chromium) overgeslagen" >> "$LOG"
  fi
fi
