"""Blok B3 — dispatcher-consistency tests voor analyze_split/analyze_full.

Verifieert dat _af_load_and_guard identieke output geeft onder READ=pi en
READ=supabase.
"""
from __future__ import annotations
import os

BASELINE_ITEM = "m24912557313"


def _run_with_env(env_val, callable_):
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


def test_af_load_and_guard_pi_vs_supabase_identical():
    from analyze_split.analyze_full import _af_load_and_guard
    pi = _run_with_env(None, lambda: _af_load_and_guard(BASELINE_ITEM))
    sb = _run_with_env("supabase", lambda: _af_load_and_guard(BASELINE_ITEM))
    # Beide moeten listing + prev_retry_count teruggeven
    assert "listing" in pi and "listing" in sb
    assert "prev_retry_count" in pi and "prev_retry_count" in sb
    # prev_retry_count moet identiek zijn (uit summary-trap)
    assert pi["prev_retry_count"] == sb["prev_retry_count"], (
        f"prev_retry_count mismatch: pi={pi['prev_retry_count']}, sb={sb['prev_retry_count']}"
    )
    # Listing item_id moet matchen
    assert pi["listing"]["item_id"] == sb["listing"]["item_id"] == BASELINE_ITEM


def test_af_load_and_guard_returns_error_for_unknown():
    from analyze_split.analyze_full import _af_load_and_guard
    for env in (None, "supabase"):
        result = _run_with_env(env, lambda: _af_load_and_guard("m00000000000"))
        assert "error" in result, f"env={env}: verwachtte error-dict, kreeg {result!r}"
        assert "not found" in result["error"]
