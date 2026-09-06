#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa — confidence score engine.

Formule (V1.2, afgesproken met Tommy 2026-07-21):
    check1 = pass  → base 80, check2/3 match +10 / mismatch -15 / skip 0
    check1 ≠ pass  → base 0,  check2/3 match +25 / mismatch  0  / skip 0
    clamp [0, 100]

Rationale: zonder leesbare slab als anker kunnen we mismatches niet betrouwbaar
scoren (we weten niet wie de waarheid vertelt), dus mismatches tellen 0. Titel
en desc die onderling matchen leveren dan 25+25 = 50%.

Elke check-result is een dict met tenminste `status` ∈ {"pass","fail","skip"}.
"pass"  = de check kon uitgevoerd worden EN kwam positief uit
"fail"  = de check kon uitgevoerd worden EN kwam negatief uit
"skip"  = de check kon niet uitgevoerd worden
"""

BASE_SLAB_PASS = 80
BASE_SLAB_FAIL = 0
BONUS_MATCH_WITH_SLAB = 10
PENALTY_MISMATCH_WITH_SLAB = -15
BONUS_MATCH_NO_SLAB = 25
PENALTY_MISMATCH_NO_SLAB = 0


def compute_score(check1: dict, check2: dict, check3: dict) -> dict:
    """Combineer de 3 checks tot 1 confidence score + uitleg."""
    reasons: list[str] = []
    slab_ok = check1.get("status") == "pass"

    if slab_ok:
        base = BASE_SLAB_PASS
        reasons.append(f"slab OCR gelukt (+{BASE_SLAB_PASS})")
    else:
        base = BASE_SLAB_FAIL
        reasons.append(f"slab OCR niet gelukt (base {BASE_SLAB_FAIL})")

    delta2 = _delta_for(check2, slab_ok)
    if delta2 > 0:
        reasons.append(f"titel matcht (+{delta2})")
    elif delta2 < 0:
        reasons.append(f"titel mismatcht ({delta2})")
    else:
        reasons.append("titel-check overgeslagen / neutraal")

    delta3 = _delta_for(check3, slab_ok)
    if delta3 > 0:
        reasons.append(f"beschrijving matcht (+{delta3})")
    elif delta3 < 0:
        reasons.append(f"beschrijving mismatcht ({delta3})")
    else:
        reasons.append("beschrijving-check overgeslagen / neutraal")

    score = max(0, min(100, base + delta2 + delta3))
    return {
        "score": score,
        "base": base,
        "delta_title": delta2,
        "delta_desc": delta3,
        "reasons": reasons,
        "verdict": _verdict(score),
    }


def _delta_for(check: dict, slab_ok: bool) -> int:
    status = (check or {}).get("status")
    if status == "pass":
        return BONUS_MATCH_WITH_SLAB if slab_ok else BONUS_MATCH_NO_SLAB
    if status == "fail":
        return PENALTY_MISMATCH_WITH_SLAB if slab_ok else PENALTY_MISMATCH_NO_SLAB
    return 0


def _verdict(score: int) -> str:
    """Verificatie-verdict (NIET koopadvies). Zegt: hoe zeker weten we dat dit
    de kaart is die de seller beweert?"""
    if score >= 90:
        return "geverifieerd"
    if score >= 50:
        return "twijfel"
    return "onbetrouwbaar"


if __name__ == "__main__":
    import json
    cases = [
        ("alles pass",     {"status": "pass"}, {"status": "pass"}, {"status": "pass"}),
        ("slab fail, rest pass", {"status": "skip"}, {"status": "pass"}, {"status": "pass"}),
        ("slab pass, rest mismatch", {"status": "pass"}, {"status": "fail"}, {"status": "fail"}),
        ("alleen slab pass", {"status": "pass"}, {"status": "skip"}, {"status": "skip"}),
        ("alles fout", {"status": "skip"}, {"status": "fail"}, {"status": "fail"}),
    ]
    for label, c1, c2, c3 in cases:
        r = compute_score(c1, c2, c3)
        print(f"{label:30} → {r['score']:3}% ({r['verdict']})")
