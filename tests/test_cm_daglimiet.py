"""Vaste daglimiet op /api/cm/pending — niet meer Cardmarket-URL's per dag naar
de Windows-worker dan CM_DAGLIMIET. Puur unit: Supabase-pad gemonkeypatcht,
dagteller in een tmp-bestand.
"""
from __future__ import annotations

import json

import pytest

import cm_queue_api


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(cm_queue_api, "CM_DAGTELLER_PAD", tmp_path / "teller.json")
    monkeypatch.setattr(cm_queue_api, "CM_DAGLIMIET", 25)
    monkeypatch.setattr(cm_queue_api, "STORAGE", "supabase")
    monkeypatch.setattr(cm_queue_api, "_limiet_gemeld_voor_dag", None)
    # nep-wachtrij: geeft altijd precies `limit` rijen terug
    monkeypatch.setattr(cm_queue_api, "_sb_pending",
                        lambda limit: [{"item_id": f"t{i}", "url": "u", "grade": "10"} for i in range(limit)])
    return cm_queue_api.app.test_client()


def test_uitdelen_stopt_precies_op_de_limiet(api, tmp_path):
    assert len(api.get("/api/cm/pending").get_json()) == 20      # 20/25
    assert len(api.get("/api/cm/pending").get_json()) == 5       # 25/25 — rest afgeknipt
    assert api.get("/api/cm/pending").get_json() == []           # vol → lege lijst
    assert api.get("/api/cm/pending").get_json() == []
    stand = json.loads((tmp_path / "teller.json").read_text())
    assert stand["uitgedeeld"] == 25
    assert stand["dag"] == cm_queue_api._vandaag()


def test_worker_limit_param_wordt_ook_afgeknipt(api):
    assert len(api.get("/api/cm/pending?limit=100").get_json()) == 25
    assert api.get("/api/cm/pending?limit=100").get_json() == []


def test_teller_overleeft_herstart_en_reset_op_nieuwe_dag(api, tmp_path):
    api.get("/api/cm/pending")                                   # 20
    # "herstart": alleen het bestand blijft
    assert cm_queue_api._lees_dagteller()[1] == 20
    # bestand van gisteren telt niet mee
    (tmp_path / "teller.json").write_text(json.dumps({"dag": "2000-01-01", "uitgedeeld": 999}))
    assert cm_queue_api._ruimte_vandaag() == 25


def test_zonder_limiet_geen_rem(api, monkeypatch):
    monkeypatch.setattr(cm_queue_api, "CM_DAGLIMIET", 0)
    for _ in range(5):
        assert len(api.get("/api/cm/pending?limit=100").get_json()) == 100


def test_health_toont_dagstand(api):
    monkeypatched = cm_queue_api
    monkeypatched._sb_pending_count = lambda: 7
    api.get("/api/cm/pending")
    h = api.get("/health").get_json()
    assert h["daglimiet"] == 25 and h["uitgedeeld_vandaag"] == 20 and h["pending"] == 7
