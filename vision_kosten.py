#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa — rapport over betaalde Google Vision API-calls.

Leest agents/kensa/vision_calls.csv (geschreven door vision._log_vision_call)
en toont per dag: aantal calls, geschat bedrag, en de laatste 24u apart.

AANNAME KOSTEN (expliciet, pas aan als Google-tarief wijzigt):
  Google Vision TEXT_DETECTION = $1.50 per 1000 calls, NA de eerste 1000
  gratis calls per maand. We rekenen met een ruwe €-omrekening van
  ~€1,40 per 1000 calls (≈ $1.50 bij koers ~1,07). De gratis-1000/maand
  wordt NIET per dag verrekend hier — we tonen het bruto-bedrag en melden
  de aanname erbij. Zie ook analyse: memory/kensa-vision-lokaal-analyse.md

BEPERKING: deze CSV weet NIET of een call kwam doordat de lokale OCR
onbereikbaar was (local_ocr_fail) of door een echte leesfout
(local_invalid_missing). Die reden leeft alleen in ocr_router.py en wordt
hier NIET gelogd. Het aandeel "PC uit" is dus niet uit dit rapport af te lezen.
"""

import csv
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

CSV_PATH = Path(__file__).resolve().parent / "vision_calls.csv"

EUR_PER_1000 = 1.40  # aanname, zie docstring
# Statussen die een daadwerkelijke (potentieel facturabele) call bij Google zijn.
# Transport-fouten (timeout/DNS) bereikten Google mogelijk niet → niet meegeteld in kosten.
BILLABLE_STATUSES = {"ok", "api_error"}


def _is_billable(status: str) -> bool:
    return status in BILLABLE_STATUSES or status.startswith("http_")


def _load_rows() -> list[dict]:
    if not CSV_PATH.exists():
        return []
    rows = []
    with open(CSV_PATH, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                r["_dt"] = datetime.fromisoformat(r["timestamp_iso"])
            except (ValueError, KeyError):
                continue
            rows.append(r)
    return rows


def _cost_eur(n_calls: int) -> float:
    return n_calls / 1000.0 * EUR_PER_1000


def main() -> None:
    rows = _load_rows()
    print(f"=== Vision API kosten-rapport ===")
    print(f"Bron: {CSV_PATH}")
    print(f"Aanname: €{EUR_PER_1000:.2f} per 1000 calls (Google $1.50/1000, eerste 1000/maand gratis — NIET verrekend)")
    print()

    if not rows:
        print("Nog geen data — vision_calls.csv is leeg of bestaat niet.")
        print("(Er is sinds het inbouwen van de teller nog geen Vision-call gelogd.)")
        return

    # Per dag groeperen (op kalenderdag in de ISO-timestamp, UTC-basis).
    per_day_total = defaultdict(int)
    per_day_billable = defaultdict(int)
    per_day_status = defaultdict(lambda: defaultdict(int))
    for r in rows:
        day = r["_dt"].date().isoformat()
        status = r.get("status", "?")
        per_day_total[day] += 1
        per_day_status[day][status] += 1
        if _is_billable(status):
            per_day_billable[day] += 1

    print("--- Per dag ---")
    print(f"{'datum':<12} {'calls':>6} {'facturabel':>11} {'~kosten':>9}  statussen")
    total_billable = 0
    for day in sorted(per_day_total):
        b = per_day_billable[day]
        total_billable += b
        statuses = ", ".join(f"{k}:{v}" for k, v in sorted(per_day_status[day].items()))
        print(f"{day:<12} {per_day_total[day]:>6} {b:>11} {'€'+format(_cost_eur(b), '.3f'):>9}  {statuses}")

    print()
    print(f"TOTAAL facturabele calls (alle dagen): {total_billable}  →  ~€{_cost_eur(total_billable):.2f}")

    # Laatste 24u apart
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    last24 = [r for r in rows if r["_dt"] >= cutoff]
    last24_billable = sum(1 for r in last24 if _is_billable(r.get("status", "")))
    print()
    print("--- Laatste 24 uur ---")
    print(f"Calls totaal:      {len(last24)}")
    print(f"Facturabel:        {last24_billable}  →  ~€{_cost_eur(last24_billable):.2f}")
    if last24:
        st = defaultdict(int)
        for r in last24:
            st[r.get("status", "?")] += 1
        print("Statussen:         " + ", ".join(f"{k}:{v}" for k, v in sorted(st.items())))

    print()
    print("BEPERKING: dit rapport kan NIET zien welk aandeel van de calls kwam")
    print("doordat de lokale OCR/PC onbereikbaar was (local_ocr_fail) versus een")
    print("echte leesfout (local_invalid_missing). Die reden leeft in ocr_router.py,")
    print("niet in vision.py. Zie analyse: memory/kensa-vision-lokaal-analyse.md")


if __name__ == "__main__":
    main()
