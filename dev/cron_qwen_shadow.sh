#!/bin/bash
# Fase 3 — Qwen-schaduwdraai: elke 10 min een batch naast de live stroom.
# 100% read-only op live (leest Supabase, schrijft alleen dev/qwen_shadow/).
# PC uit => script logt offline en stopt; flock voorkomt overlappende runs.
set -u
KENSA_DIR="/home/pi/.openclaw/workspace/agents/kensa"
VENV_PY="/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3"
LOCK="/tmp/kensa-qwen-shadow.lock"
cd "$KENSA_DIR"
exec /usr/bin/flock -n "$LOCK" "$VENV_PY" dev/qwen_shadow.py >> dev/qwen_shadow/cron.log 2>&1
