"""Vaste daglimiet op /api/cm/pending — per dag maximaal CM_DAGLIMIET UNIEKE
Cardmarket-URL's naar de Windows-worker. Al uitgedeelde rijen zonder resultaat
worden opnieuw aangeboden (de worker pakt per poll maar een deel van de batch);
een URL met resultaat vandaag komt niet meer terug. Puur unit: Supabase-pad
gemonkeypatcht, dagstand in een tmp-bestand.
"""
from __future__ import annotations

import json

import pytest

import cm_queue_api


def _rij(i: int, url: str | None = None) -> dict:
    return {"item_id": f"t{i}", "url": url or f"https://cm/{i}", "grade": "10"}


class NepWachtrij:
    """Pending rijen; een resultaat haalt de rij (en alle rijen met dezelfde URL) weg,
    net als de echte same-url-vulling."""

    def __init__(self, rows: list[dict]):
        self.rows = list(rows)

    def pending(self, limit: int) -> list[dict]:
        return self.rows[:limit]

    def resultaat(self, item_id: str) -> None:
        url = next(r["url"] for r in self.rows if r["item_id"] == item_id)
        self.rows = [r for r in self.rows if r["url"] != url]


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(cm_queue_api, "CM_DAGTELLER_PAD", tmp_path / "teller.json")
    monkeypatch.setattr(cm_queue_api, "CM_DAGLIMIET", 25)
    monkeypatch.setattr(cm_queue_api, "STORAGE", "supabase")
    monkeypatch.setattr(cm_queue_api, "_limiet_gemeld_voor_dag", None)
    monkeypatch.setattr(cm_queue_api, "_sb_pending_count", lambda: 7)
    monkeypatch.setattr(cm_queue_api, "_sb_result", lambda *a, **k: None)
    return cm_queue_api.app.test_client()


def _wachtrij(monkeypatch, rows: list[dict]) -> NepWachtrij:
    q = NepWachtrij(rows)
    monkeypatch.setattr(cm_queue_api, "_sb_pending", q.pending)
    return q


def _resultaat(api, q: NepWachtrij, item_id: str, listings=None) -> None:
    url = next(r["url"] for r in q.rows if r["item_id"] == item_id)
    q.resultaat(item_id)
    api.post("/api/cm/result", json={"item_id": item_id, "url": url,
                                     "listings": listings if listings is not None else [{"price_eur": 1}]})


def test_nieuwe_urls_stoppen_op_de_limiet_maar_uitgedeelde_rijen_blijven_komen(api, tmp_path, monkeypatch):
    q = _wachtrij(monkeypatch, [_rij(i) for i in range(60)])
    assert len(api.get("/api/cm/pending").get_json()) == 20           # 20 nieuw
    # worker heeft nog niks afgemaakt → zelfde 20 opnieuw + 5 nieuwe (25 = limiet)
    tweede = api.get("/api/cm/pending?limit=100").get_json()
    assert [r["item_id"] for r in tweede] == [f"t{i}" for i in range(25)]
    # derde poll: nog steeds die 25 (niks nieuws meer), geen lege lijst
    assert len(api.get("/api/cm/pending?limit=100").get_json()) == 25
    stand = json.loads((tmp_path / "teller.json").read_text())
    assert stand["uitgedeeld"] == 25 and len(stand["urls"]) == 25
    assert stand["dag"] == cm_queue_api._vandaag()


def test_worker_die_per_poll_een_rij_doet_krijgt_de_rest_steeds_terug(api, monkeypatch):
    q = _wachtrij(monkeypatch, [_rij(i) for i in range(30)])
    batch = api.get("/api/cm/pending").get_json()
    assert len(batch) == 20
    _resultaat(api, q, batch[-1]["item_id"])                         # worker doet alleen de laatste
    volgende = api.get("/api/cm/pending").get_json()
    ids = [r["item_id"] for r in volgende]
    assert "t19" not in ids                                           # klaar → niet meer
    assert ids[:19] == [f"t{i}" for i in range(19)]                   # de 19 resterende opnieuw
    assert ids[19] == "t20"                                           # + 1 nieuwe tot limit 20
    assert api.get("/health").get_json()["uitgedeeld_vandaag"] == 21
    assert api.get("/health").get_json()["klaar_vandaag"] == 1


def test_zelfde_url_gaat_maar_een_keer_per_antwoord_en_nooit_meer_na_resultaat(api, monkeypatch):
    rows = [_rij(1, "https://cm/A"), _rij(2, "https://cm/A"), _rij(3, "https://cm/A"),
            _rij(4, "https://cm/B"), _rij(5, "https://cm/B"), _rij(6, "https://cm/C")]
    q = _wachtrij(monkeypatch, rows)
    eerste = api.get("/api/cm/pending").get_json()
    assert [r["url"] for r in eerste] == ["https://cm/A", "https://cm/B", "https://cm/C"]
    assert api.get("/health").get_json()["uitgedeeld_vandaag"] == 3
    # resultaat voor A met LEGE listings: same-url-vulling slaat t2/t3 over, die
    # blijven pending — maar A is vandaag klaar, dus ze gaan niet nog eens naar CM
    q.rows = [r for r in q.rows if r["item_id"] != "t1"]
    api.post("/api/cm/result", json={"item_id": "t1", "url": "https://cm/A", "listings": []})
    daarna = api.get("/api/cm/pending").get_json()
    assert [r["url"] for r in daarna] == ["https://cm/B", "https://cm/C"]
    assert api.get("/health").get_json()["uitgedeeld_vandaag"] == 3


def test_dubbele_urls_tellen_niet_mee_voor_de_limiet(api, monkeypatch):
    rows = [_rij(i, f"https://cm/{i % 10}") for i in range(100)]
    _wachtrij(monkeypatch, rows)
    assert len(api.get("/api/cm/pending?limit=100").get_json()) == 10
    assert api.get("/health").get_json()["uitgedeeld_vandaag"] == 10
    assert cm_queue_api._ruimte_vandaag() == 15


def test_limiet_vol_nieuwe_blijven_liggen_tot_morgen(api, monkeypatch):
    q = _wachtrij(monkeypatch, [_rij(i) for i in range(40)])
    api.get("/api/cm/pending?limit=100")                              # 25 uitgedeeld = vol
    for i in range(25):
        _resultaat(api, q, f"t{i}")                                   # worker maakt ze af
    assert api.get("/api/cm/pending?limit=100").get_json() == []      # t25..t39 wachten op morgen
    assert api.get("/health").get_json()["uitgedeeld_vandaag"] == 25


def test_teller_overleeft_herstart_en_reset_op_nieuwe_dag(api, tmp_path, monkeypatch):
    _wachtrij(monkeypatch, [_rij(i) for i in range(60)])
    api.get("/api/cm/pending")
    assert len(cm_queue_api._lees_dagteller()[1]) == 20
    (tmp_path / "teller.json").write_text(json.dumps({"dag": "2000-01-01", "urls": ["x"] * 999}))
    assert cm_queue_api._ruimte_vandaag() == 25


def test_resultaat_van_voor_de_omzetting_telt_alsnog_mee(api, monkeypatch):
    _wachtrij(monkeypatch, [])
    api.post("/api/cm/result", json={"item_id": "oud", "url": "https://cm/oud", "listings": []})
    h = api.get("/health").get_json()
    assert h["uitgedeeld_vandaag"] == 1 and h["klaar_vandaag"] == 1


def test_zonder_limiet_geen_rem(api, monkeypatch):
    monkeypatch.setattr(cm_queue_api, "CM_DAGLIMIET", 0)
    _wachtrij(monkeypatch, [_rij(i) for i in range(100)])
    assert len(api.get("/api/cm/pending?limit=100").get_json()) == 100
    assert len(api.get("/api/cm/pending?limit=100").get_json()) == 100


def test_health_toont_dagstand(api, monkeypatch):
    _wachtrij(monkeypatch, [_rij(i) for i in range(60)])
    api.get("/api/cm/pending")
    h = api.get("/health").get_json()
    assert h["daglimiet"] == 25 and h["uitgedeeld_vandaag"] == 20 and h["pending"] == 7
