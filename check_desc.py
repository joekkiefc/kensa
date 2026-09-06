#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa — Check 3: seller-beschrijving ↔ slab match.

Scope: PUUR slab-verificatie. We checken alleen of de beschrijving
consistent is met wat we op de slab lezen. Damage-mentions, return-policy,
proxy/reproduction-flags e.d. horen NIET in deze check (Tommy 2026-07-21).

  - grade in beschrijving (PSA10 / GEM MT / グレード:10) matcht slab-grade
  - pokemon-naam in beschrijving matcht slab-naam

status:
  pass  = grade-match en/of naam-match, geen mismatches
  fail  = grade-mismatch (bv desc PSA10 maar slab PSA9)
  skip  = beschrijving leeg of niks vergelijkbaars
"""

import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def _grade_in_desc(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"PSA\s*(10|9\.5|9|8\.5|8|7|6|5|4|3|2|1)", text, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"グレード\s*[:.：]\s*(?:PSA\s*)?(10|9|8|7|6|5|4|3|2|1)", text)
    if m:
        return m.group(1)
    if re.search(r"GEM\s*MT|GEM\s*MINT", text, re.IGNORECASE):
        return "10"
    return None


def _title_grade_from_check(title_check: dict) -> str | None:
    """Herleid title-grade uit check_title's matches-lijst."""
    for m in title_check.get("matches", []):
        gm = re.search(r"PSA\s*(\d+(?:\.\d+)?)", m, re.IGNORECASE)
        if gm:
            return gm.group(1)
    return None


def _title_pokemon_from_check(title_check: dict) -> str | None:
    """Herleid pokemon-naam uit check_title's matches-lijst."""
    for m in title_check.get("matches", []):
        pm = re.search(r"pokemon-naam '([a-z]+)'", m)
        if pm:
            return pm.group(1)
    return None


# `check_desc` verhuisd naar _split/ (v2). Verwijderd 2026-09-01 (regel 2 cleanup).
# Backup: _legacy_pre_refactor/... — alias-override staat lager in de file.




# Refactor-switch(es) voor if __name__ zodat CLI-run de alias ook heeft.
# Refactor switch (2026-09-01, regel 2 #4): route naar v2. Backup:
# _legacy_pre_refactor/check_desc.py.20260901-regel2-func4
from check_desc_split.desc_match import check_desc_v2 as _check_desc_new  # noqa: E402
check_desc = _check_desc_new

if __name__ == "__main__":
    from check_slab import check_slab
    from check_title import check_title
    import sqlite3
    item_id = sys.argv[1] if len(sys.argv) > 1 else "m10228250509"
    conn = sqlite3.connect(str(SCRIPT_DIR / "kensa.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT title_jp, title_en, description_jp FROM listings WHERE item_id = ?", (item_id,)).fetchone()
    slab = check_slab(item_id)
    tit = check_title(slab, row["title_jp"], row["title_en"])
    r = check_desc(slab, row["description_jp"], tit)
    print(f"--- check_desc → {r['status']} ---")
    for m in r["matches"]: print(f"  MATCH: {m}")
    for m in r["mismatches"]: print(f"  MISMATCH: {m}")
    for m in r["reasons"]: print(f"  note: {m}")


