"""Test voor de URL-fallback in _enq_try_cache_hit (bugfix 2026-09-16).

Situatie: de eigen card_key (variant, bv 'groudon:069:10:sv3a') heeft géén
verse price_cache, maar een andere sleutel met dezelfde Cardmarket-URL wél.
Vóór de fix kreeg alleen de queue-rij die listings; price_cache voor de
eigen sleutel bleef leeg → HoS wachtkamer/dashboard zagen "geen prijs".

Verwacht:
  1. fallback-geval  → upsert_cm(eigen_key, url, listings, ts) wordt aangeroepen
  2. eigen verse hit → upsert_cm wordt NIET aangeroepen (geen extra write)
  3. fallback met lege listings → geen upsert (niks laten plakken)

Alles gemonkeypatcht: geen Supabase-verkeer.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

KENSA_DIR = Path(__file__).resolve().parent.parent
if str(KENSA_DIR) not in sys.path:
    sys.path.insert(0, str(KENSA_DIR))

from analyze_split import enqueue_cardmarket as enq  # noqa: E402

URL = "https://www.cardmarket.com/en/Pokemon/Products/Singles/Raging-Surf/Groudon-V2-sv3a069?language=7&minCondition=1"
OWN_KEY = "groudon:069:10:sv3a"
ALT_KEY = "groudon:069:10"
LISTINGS = [{"price_eur": 109.95}, {"price_eur": 119.0}]


def _fresh_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def harness(monkeypatch):
    """Zet alle I/O-randen vast; geeft een dict met de gevangen calls terug."""
    calls: dict[str, list] = {"upsert_cm": [], "enqueue_from_cache": []}
    monkeypatch.setenv("KENSA_WRITE_STORAGE", "supabase")
    monkeypatch.setattr(enq, "_build_card_key", lambda slab, llm: OWN_KEY)
    monkeypatch.setattr(
        "storage_supabase.cardmarket_queue.enqueue_from_cache",
        lambda *a, **k: calls["enqueue_from_cache"].append(a),
    )
    monkeypatch.setattr(
        "storage_supabase.price_cache.upsert_cm",
        lambda key, url, listings, ts=None, schema="kensa": calls["upsert_cm"].append((key, url, listings)),
    )
    return calls


def _run(monkeypatch, own_row, alt_row):
    monkeypatch.setattr(enq, "_price_cache_get", lambda key: own_row)
    monkeypatch.setattr(enq, "_price_cache_get_by_cm_url", lambda url: alt_row)
    return enq._enq_try_cache_hit("m_test", {"url": URL}, "10", {}, None, verbose=False)


def test_fallback_vult_price_cache_voor_eigen_sleutel(harness, monkeypatch):
    own = {"card_key": OWN_KEY, "cm_fetched_at": None, "cm_listings_json": None}
    alt = {"card_key": ALT_KEY, "cm_fetched_at": _fresh_ts(), "cm_listings_json": LISTINGS}
    assert _run(monkeypatch, own, alt) is True
    assert len(harness["enqueue_from_cache"]) == 1
    assert harness["upsert_cm"] == [(OWN_KEY, URL, LISTINGS)]


def test_eigen_verse_hit_schrijft_niet_opnieuw(harness, monkeypatch):
    own = {"card_key": OWN_KEY, "cm_fetched_at": _fresh_ts(), "cm_listings_json": LISTINGS}
    assert _run(monkeypatch, own, None) is True
    assert len(harness["enqueue_from_cache"]) == 1
    assert harness["upsert_cm"] == []


def test_fallback_met_lege_listings_plakt_niet(harness, monkeypatch):
    own = None
    alt = {"card_key": ALT_KEY, "cm_fetched_at": _fresh_ts(), "cm_listings_json": []}
    assert _run(monkeypatch, own, alt) is True
    assert harness["upsert_cm"] == []
