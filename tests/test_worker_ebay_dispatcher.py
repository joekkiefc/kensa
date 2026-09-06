"""Blok C1 — dispatcher-consistency test voor worker_ebay.pick_batch."""
from __future__ import annotations
import os


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


def test_pick_batch_supabase_returns_list():
    from worker_ebay import pick_batch
    batch = _run_with_env("supabase", lambda: pick_batch(20))
    assert isinstance(batch, list)
    assert len(batch) <= 20
    for iid in batch:
        assert isinstance(iid, str)
        assert len(iid) >= 3


def test_pick_batch_pi_still_works():
    """Zonder env-var moet Pi-lock-pad blijven werken (backwards compat)."""
    from worker_ebay import pick_batch
    batch = _run_with_env(None, lambda: pick_batch(5))
    assert isinstance(batch, list)
    # Pi doet ook lock — items komen mogelijk terug of niet (afhankelijk van staat)
    assert len(batch) <= 5


def test_pick_batch_supabase_zero_limit():
    from worker_ebay import pick_batch
    batch = _run_with_env("supabase", lambda: pick_batch(0))
    assert batch == []
