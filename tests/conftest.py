"""Shared pytest fixtures voor Kensa test-suite.

Zorgt dat imports vanuit agents/kensa werken zonder installatie als package.
Laadt de fixture-JSONs die tijdens de refactor zijn opgebouwd.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import pytest

KENSA_DIR = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = KENSA_DIR

# Zorg dat 'from analyze_split.ebay_phase import ...' werkt in tests
sys.path.insert(0, str(KENSA_DIR))


@pytest.fixture(autouse=True)
def _geen_productie_opslag_via_env(monkeypatch):
    """Slot: de KENSA_*_STORAGE-schakelaars mogen de tests nooit naar de échte
    Supabase-tabellen sturen. Op 2026-09-08 draaide de suite met
    KENSA_WRITE_STORAGE=supabase geëxporteerd — de SQLite-storage-tests
    schreven toen m1..m7 in kensa.listings (productie). Tests die Supabase
    nodig hebben, gebruiken expliciet de test_*-tabellen of zetten de
    variabele zelf via monkeypatch (dat gebeurt ná deze fixture)."""
    for var in ("KENSA_STORAGE", "KENSA_READ_STORAGE", "KENSA_WRITE_STORAGE"):
        monkeypatch.delenv(var, raising=False)


def _load_baseline(subdir: str) -> list[dict]:
    path = FIXTURE_ROOT / subdir / "baseline.json"
    if not path.exists():
        pytest.skip(f"baseline {subdir} niet aanwezig — draai de collector eerst")
    return json.loads(path.read_text())


@pytest.fixture(scope="session")
def check_title_fixtures():
    return _load_baseline("check_title_refactor_fixtures")


@pytest.fixture(scope="session")
def check_desc_fixtures():
    return _load_baseline("check_desc_refactor_fixtures")


@pytest.fixture(scope="session")
def parse_detail_fixtures():
    return _load_baseline("parse_detail_refactor_fixtures")


@pytest.fixture(scope="session")
def parse_listing_fixtures():
    return _load_baseline("parse_listing_refactor_fixtures")


@pytest.fixture(scope="session")
def parse_card_fixtures():
    return _load_baseline("parse_card_refactor_fixtures")


@pytest.fixture(scope="session")
def psa_name_set_fixtures():
    return _load_baseline("psa_name_set_refactor_fixtures")


@pytest.fixture(scope="session")
def ebay_build_query_fixtures():
    return _load_baseline("ebay_build_query_refactor_fixtures")


@pytest.fixture(scope="session")
def interpret_slab_fixtures():
    return _load_baseline("interpret_slab_refactor_fixtures")


@pytest.fixture(scope="session")
def judge_sales_fixtures():
    return _load_baseline("judge_sales_refactor_fixtures")
