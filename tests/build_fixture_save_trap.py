#!/usr/bin/env python3
"""Golden fixture voor analyze._save_trap (analysis-writes).

Pakt 20 real historische analysis-rijen uit Pi en maakt daarvan
insert-cases. Ook 5 update-cases: dezelfde (item_id, trap) twee keer
schrijven → tweede write moet de eerste vervangen (DELETE+INSERT).
"""
from __future__ import annotations
import json, sqlite3
from pathlib import Path

DB = "/home/pi/.openclaw/workspace/agents/kensa/kensa.db"
OUT = Path("/home/pi/.openclaw/workspace/agents/kensa/tests/write_fixtures/save_trap.json")


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT item_id, trap, result_json, confidence, card_id
        FROM analysis
        WHERE trap IN ('slab_ocr', 'ebay_prices', 'summary')
        ORDER BY analysis_id DESC LIMIT 20
    """).fetchall()

    fixtures = []
    for r in rows:
        try:
            result = json.loads(r["result_json"]) if r["result_json"] else {}
        except Exception:
            result = {"_raw": r["result_json"]}
        fixtures.append({
            "case": "insert_new",
            "item_id": r["item_id"] + "_saveTrapTest",
            "trap": r["trap"],
            "result": result,
            "confidence": r["confidence"],
            "card_id": r["card_id"],
        })

    # Update-cases: eerste 5 → schrijf twee keer, tweede winst
    for base in fixtures[:5]:
        fixtures.append({
            "case": "update_replace",
            "item_id": base["item_id"],
            "trap": base["trap"],
            "first_result": {"stub": "first"},
            "first_confidence": 50.0,
            "second_result": base["result"],
            "second_confidence": base["confidence"],
            "second_card_id": base["card_id"],
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fixtures, indent=2, ensure_ascii=False, default=str))
    print(f"Wrote {len(fixtures)} fixtures -> {OUT}")


if __name__ == "__main__":
    main()
