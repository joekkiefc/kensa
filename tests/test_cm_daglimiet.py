"""Vaste daglimiet op /api/cm/pending — per dag maximaal CM_DAGLIMIET UNIEKE
Cardmarket-URL's naar de Windows-worker, en een URL die vandaag al uitgedeeld
is gaat niet nog een keer. Puur unit: Supabase-pad gemonkeypatcht, dagstand in
een tmp-bestand.
"""
from __future__ import annotations

import json

import pytest

import cm_queue_api


def _rij(i: int, url: str | None = None) -> dict:
    return {"item_id": f"t{i}", "url": url or f"https://cm/{i}", "grade": "10"}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(cm_queue_api, "CM_DAGTELLER_PAD", tmp_path / "teller.json")
    monkeypatch.setattr(cm_queue_api, "CM_DAGLIMIET", 25)
    monkeypatch.setattr(cm_queue_api, "STORAGE", "supabase")
    monkeypatch.setattr(cm_queue_api, "_limiet_gemeld_voor_dag", None)
    monkeypatch.setattr(cm_queue_api, "_sb_pending_count", lambda: 7)
    return cm_queue_api.app.test_client()


def _wachtrij(monkeypatch, rows: list[dict]) -> None:
    """Nep-wachtrij: rijen blijven pending (de test simuleert geen resultaten)."""
    monkeypatch.setattr(cm_queue_api, "_sb_pending", lambda limit: rows[:limit])


def test_uitdelen_stopt_precies_op_de_limiet(api, tmp_path, monkeypatch):
    _wachtrij(monkeypatch, [_rij(i) for i in range(60)])
    assert len(api.get("/api/cm/pending").get_json()) == 20      # 20/25
    assert len(api.get("/api/cm/pending").get_json()) == 5       # 25/25 — rest afgeknipt
    assert api.get("/api/cm/pending").get_json() == []           # vol → lege lijst
    stand = json.loads((tmp_path / "teller.json").read_text())
    assert stand["uitgedeeld"] == 25 and len(stand["urls"]) == 25
    assert stand["dag"] == cm_queue_api._vandaag()


def test_zelfde_url_gaat_maar_een_keer_per_dag_naar_de_worker(api, monkeypatch):
    # 3 kaarten delen 1 CM-pagina, 2 kaarten een andere, 1 kaart een eigen pagina
    rows = [_rij(1, "https://cm/A"), _rij(2, "https://cm/A"), _rij(3, "https://cm/A"),
            _rij(4, "https://cm/B"), _rij(5, "https://cm/B"), _rij(6, "https://cm/C")]
    _wachtrij(monkeypatch, rows)
    eerste = api.get("/api/cm/pending").get_json()
    assert [r["url"] for r in eerste] == ["https://cm/A", "https://cm/B", "https://cm/C"]
    # dezelfde rijen staan nog pending (worker is bezig / resultaat nog niet terug)
    # → níet opnieuw uitdelen, en de dagstand blijft 3
    assert api.get("/api/cm/pending").get_json() == []
    assert api.get("/health").get_json()["uitgedeeld_vandaag"] == 3


def test_dubbele_urls_tellen_niet_mee_voor_de_limiet(api, monkeypatch):
    # 100 pending rijen die samen maar 10 unieke pagina's zijn → 10 van de 25 gebruikt
    rows = [_rij(i, f"https://cm/{i % 10}") for i in range(100)]
    _wachtrij(monkeypatch, rows)
    assert len(api.get("/api/cm/pending?limit=100").get_json()) == 10
    assert api.get("/health").get_json()["uitgedeeld_vandaag"] == 10
    assert cm_queue_api._ruimte_vandaag() == 15


def test_bekende_urls_trekken_de_batch_niet_leeg(api, monkeypatch):
    # eerst 20 unieke uitdelen; daarna staan die 20 nog vooraan in de wachtrij
    # met 30 nieuwe erachter → de worker krijgt gewoon de 5 die nog mogen
    rows = [_rij(i) for i in range(50)]
    _wachtrij(monkeypatch, rows)
    api.get("/api/cm/pending")
    tweede = api.get("/api/cm/pending").get_json()
    assert [r["item_id"] for r in tweede] == ["t20", "t21", "t22", "t23", "t24"]


def test_teller_overleeft_herstart_en_reset_op_nieuwe_dag(api, tmp_path, monkeypatch):
    _wachtrij(monkeypatch, [_rij(i) for i in range(60)])
    api.get("/api/cm/pending")                                   # 20
    assert len(cm_queue_api._lees_dagteller()[1]) == 20          # alleen het bestand blijft
    (tmp_path / "teller.json").write_text(json.dumps({"dag": "2000-01-01", "urls": ["x"] * 999}))
    assert cm_queue_api._ruimte_vandaag() == 25


def test_zonder_limiet_geen_rem_maar_wel_ontdubbeling(api, monkeypatch):
    monkeypatch.setattr(cm_queue_api, "CM_DAGLIMIET", 0)
    _wachtrij(monkeypatch, [_rij(i) for i in range(100)])
    assert len(api.get("/api/cm/pending?limit=100").get_json()) == 100
    assert api.get("/api/cm/pending?limit=100").get_json() == []  # zelfde rijen, al uitgedeeld


def test_health_toont_dagstand(api, monkeypatch):
    _wachtrij(monkeypatch, [_rij(i) for i in range(60)])
    api.get("/api/cm/pending")
    h = api.get("/health").get_json()
    assert h["daglimiet"] == 25 and h["uitgedeeld_vandaag"] == 20 and h["pending"] == 7
