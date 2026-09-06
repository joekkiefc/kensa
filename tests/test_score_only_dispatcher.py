"""Blok B1 — dispatcher-consistency tests voor analyze_split/score_only.

Verifieert dat _load_listing en _load_stored_slab_llm identieke output geven
onder READ=pi (default) en READ=supabase.

Als deze tests slagen, kan de worker-cutover naar KENSA_READ_STORAGE=supabase
plaatsvinden zonder gedragsverandering — dat is de kern van de migratie.

Baseline item: m24912557313 (bekend uit Blok A).
"""
from __future__ import annotations
import os
import importlib

BASELINE_ITEM = "m24912557313"


def _run_with_env(env_val: str, callable_):
    """Zet env-var tijdelijk, roep callable aan, herstel env-var."""
    old = os.environ.get("KENSA_READ_STORAGE")
    if env_val is None:
        os.environ.pop("KENSA_READ_STORAGE", None)
    else:
        os.environ["KENSA_READ_STORAGE"] = env_val
    try:
        return callable_()
    finally:
        if old is None:
            os.environ.pop("KENSA_READ_STORAGE", None)
        else:
            os.environ["KENSA_READ_STORAGE"] = old


def test_load_listing_pi_vs_supabase_identical():
    from analyze_split.score_only import _load_listing
    pi = _run_with_env(None, lambda: _load_listing(BASELINE_ITEM))
    sb = _run_with_env("supabase", lambda: _load_listing(BASELINE_ITEM))
    assert pi is not None
    assert sb is not None
    # Kernvelden voor score-flow moeten identiek zijn
    for col in ("item_id", "title_jp", "title_en", "price_eur", "price_jpy",
                "slab_status", "card_key", "seller_id", "description_jp"):
        assert pi.get(col) == sb.get(col), f"mismatch op kolom {col}"


def test_load_stored_slab_llm_pi_vs_supabase_identical():
    from analyze_split.score_only import _load_stored_slab_llm
    pi_slab, pi_llm, pi_retry = _run_with_env(None, lambda: _load_stored_slab_llm(BASELINE_ITEM))
    sb_slab, sb_llm, sb_retry = _run_with_env("supabase", lambda: _load_stored_slab_llm(BASELINE_ITEM))
    # slab-dict identiek
    assert pi_slab == sb_slab, "slab_ocr result_json mismatch"
    # llm-dict identiek
    assert pi_llm == sb_llm, "llm_slab result_json mismatch"
    # prev_retry_count identiek
    assert pi_retry == sb_retry, f"prev_retry mismatch: pi={pi_retry}, sb={sb_retry}"


def test_load_listing_returns_none_for_unknown():
    from analyze_split.score_only import _load_listing
    for env in (None, "supabase"):
        result = _run_with_env(env, lambda: _load_listing("m00000000000"))
        assert result is None, f"env={env}: verwachtte None, kreeg {result!r}"


def test_load_stored_slab_llm_returns_nones_for_unknown():
    from analyze_split.score_only import _load_stored_slab_llm
    for env in (None, "supabase"):
        slab, llm, retry = _run_with_env(env, lambda: _load_stored_slab_llm("m00000000000"))
        assert slab is None
        assert llm is None
        assert retry == 0
