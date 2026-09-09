"""Fase 4 — Qwen als slab-lezer: opschoning, cert-sanity, onbereikbaar-gedrag en de route
in check_slab_hybrid (Qwen eerst; Gemini ALLEEN als Qwen niet bereikbaar is; onvolledig →
Vision-vangnet). Geen netwerk: alles gemockt."""
from __future__ import annotations

import urllib.error

import pytest

import lezing_opschonen as LO
import qwen_lezer as QL
import check_slab_hybrid as CSH


@pytest.fixture(autouse=True)
def _onderbreker_dicht(monkeypatch):
    """Elke test begint met een gesloten stroomonderbreker (module-state)."""
    monkeypatch.setattr(QL, "_onbereikbaar_tot", 0.0)


# ---------------------------------------------------------------- lezing_opschonen
@pytest.mark.parametrize("ruw, naam, nummer, setcode", [
    ({"name": "Venusaur-Holo", "label_name": "#003 VENUSAUR-HOLO", "number": "#003/071", "set_code": "SV-P"},
     "Venusaur", "003/SV-P", "SV-P"),
    ({"name": "FA/MEW V", "label_name": "FA/MEW V", "number": "#105", "set_code": "2023 POKEMON SVGJP"},
     "Mew V", "105", None),                                    # rommel-setcode eruit, subtype blijft
    ({"name": "Mega Gardevoir ex", "label_name": "MEGA GARDEVOIR ex", "number": "087/063", "set_code": "M1S"},
     "Mega Gardevoir EX", "087/M1S", "M1S"),                   # prefix Mega blijft
    ({"name": "Kairiki", "label_name": "#121 KAIRIKI", "number": "121/138", "set_code": None},
     "Machamp", "121", None),                                  # JP romaji → officiële Engelse naam
    ({"name": "Charizard ex", "label_name": None, "number": "331/190 SSR", "set_code": "CSR"},
     "Charizard EX", "331", None),                             # rarity-code is geen set-code
])
def test_opschonen_naam_nummer_setcode(ruw, naam, nummer, setcode):
    out = LO.opschonen({**ruw, "grade": "10", "cert": "12345678"})
    assert out["name"] == naam
    assert out["number"] == nummer
    assert out["set_code"] == setcode
    assert out["name_raw"] == ruw["name"]


def test_opschonen_onbekende_naam_blijft_ruw():
    out = LO.opschonen({"name": "Xyzzy Foo", "label_name": "XYZZY", "number": "#12"})
    assert out["name"] == "Xyzzy Foo" and out["pokemon"] is None and out["_naam_bron"] == "onbekend"
    assert out["number"] == "12"


def test_opschonen_laat_fouten_met_rust():
    assert LO.opschonen({"_error": "parse: x"}) == {"_error": "parse: x"}


# ---------------------------------------------------------------- cert-sanity
@pytest.mark.parametrize("cert, ok", [
    ("162382822", True), ("85079037", True), ("1234567890", False),
    ("77777777", False), ("2025000000", False), ("12345678", False),
    ("1234567", False), ("abc", False), (None, False), ("162382822,162382823", False),
])
def test_cert_plausibel(cert, ok):
    assert QL.cert_plausibel(cert) is ok


# ---------------------------------------------------------------- parse
def test_parse_model_json_varianten():
    assert QL.parse_model_json('```json\n{"name": "Pikachu"}\n```') == {"name": "Pikachu"}
    assert QL.parse_model_json('blabla {"cert": "1"} nawoord') == {"cert": "1"}
    assert "_error" in QL.parse_model_json("geen json hier")
    assert "_error" in QL.parse_model_json("[1,2]")


# ---------------------------------------------------------------- onbereikbaar
def test_vraag_qwen_onbereikbaar_bij_verbindingsfout(monkeypatch):
    def kapot(*a, **k):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(QL.urllib.request, "urlopen", kapot)
    with pytest.raises(QL.QwenOnbereikbaar):
        QL.vraag_qwen(b"x" * 10, pogingen=1)


def test_vraag_qwen_4xx_is_geen_onbereikbaar(monkeypatch):
    import io
    def vierhonderd(*a, **k):
        raise urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b"te groot"))
    monkeypatch.setattr(QL.urllib.request, "urlopen", vierhonderd)
    d, _ = QL.vraag_qwen(b"x" * 10, pogingen=1)
    assert d["_error"].startswith("HTTP 400")


def test_lees_slab_foto_keurt_verzonnen_cert_af(monkeypatch):
    monkeypatch.setattr(QL, "haal_foto", lambda url: (b"x" * 9000, url, False))
    monkeypatch.setattr(QL, "vraag_qwen", lambda img, pogingen=2: (
        {"name": "Pikachu", "label_name": "#001 PIKACHU", "number": "001", "grade": "10", "cert": "77777777"}, 1.2))
    d = QL.lees_slab_foto("http://x/foto.jpg")
    assert d["cert"] is None and d["_cert_afgekeurd"] == "77777777"
    assert d["name"] == "Pikachu" and d["_lezer"] == "qwen"


def test_lees_slab_foto_zonder_foto_is_geen_qwen_probleem(monkeypatch):
    monkeypatch.setattr(QL, "haal_foto", lambda url: (None, url, False))
    d = QL.lees_slab_foto("http://x/foto.jpg")
    assert d["_error"].startswith("foto:")


# ---------------------------------------------------------------- route in check_slab_hybrid
@pytest.fixture
def route(monkeypatch):
    """Alles rond de lezer gemockt: 2 foto's, geen lot, Vision-vangnet telt aanroepen."""
    monkeypatch.setattr(CSH, "_fetch_titles", lambda iid, db: ("タイトル", "title"))
    monkeypatch.setattr(CSH, "is_multi_slab_lot", lambda jp, en: (False, None))
    monkeypatch.setattr(CSH, "_load_photo_urls", lambda iid, db: [(0, "http://p/orig/1.jpg"), (1, "http://p/orig/2.jpg")])
    calls = {"gemini": 0, "vision": 0, "qwen": 0}

    def vision(iid, max_photos=3, db_path=None):
        calls["vision"] += 1
        return {"status": "fail", "card_name": None, "grade": None}
    monkeypatch.setattr(CSH, "_classic_check_slab", vision)

    def gemini(url, en, jp):
        calls["gemini"] += 1
        return {"name": "Pikachu", "number": "025", "grade": "10", "cert": "11112222", "set_name": "Base"}
    monkeypatch.setattr(CSH, "interpret_slab_photo", gemini)
    return calls


def test_qwen_leest_compleet_geen_gemini(route, monkeypatch):
    monkeypatch.setattr(CSH, "LEZER", "qwen")
    def qwen(url):
        route["qwen"] += 1
        return {"name": "Pikachu V", "label_name": "#001 PIKACHU V", "number": "001/SV-P", "set_code": "SV-P",
                "grade": "10", "cert": "16238282", "_lezer": "qwen"}
    monkeypatch.setattr(CSH, "_qwen_lees", qwen)
    r = CSH.check_slab_hybrid("m1", db_path="x")
    assert r["status"] == "pass" and r["_source"] == "qwen" and r["_reason"] == "ok"
    assert r["card_name"] == "Pikachu V" and r["set_code"] == "SV-P" and r["label_name"] == "#001 PIKACHU V"
    assert route == {"gemini": 0, "vision": 0, "qwen": 1}


def test_qwen_onbereikbaar_dan_gemini(route, monkeypatch):
    monkeypatch.setattr(CSH, "LEZER", "qwen")
    def qwen(url):
        route["qwen"] += 1
        raise CSH.QwenOnbereikbaar("URLError: refused")
    monkeypatch.setattr(CSH, "_qwen_lees", qwen)
    r = CSH.check_slab_hybrid("m1", db_path="x")
    assert r["status"] == "pass" and r["_source"] == "gemini_fallback"
    assert r["_reason"].startswith("qwen_onbereikbaar")
    assert r["card_name"] == "Pikachu"
    assert route == {"gemini": 1, "vision": 0, "qwen": 1}


def test_qwen_onvolledig_dan_vision_niet_gemini(route, monkeypatch):
    monkeypatch.setattr(CSH, "LEZER", "qwen")
    def qwen(url):
        route["qwen"] += 1
        return {"_error": "parse: geen JSON"} if route["qwen"] == 1 else {"name": None, "grade": "10"}
    monkeypatch.setattr(CSH, "_qwen_lees", qwen)
    r = CSH.check_slab_hybrid("m1", db_path="x")
    assert r["_source"] == "vision_fallback" and r["_reason"] == "qwen_incomplete"
    assert route == {"gemini": 0, "vision": 1, "qwen": 2}
    assert [p["lezer"] for p in r["_multimodal_per_photo"]] == ["qwen", "qwen"]


def test_zonder_schakelaar_blijft_gemini(route, monkeypatch):
    monkeypatch.setattr(CSH, "LEZER", "gemini")
    monkeypatch.setattr(CSH, "_qwen_lees", lambda url: pytest.fail("Qwen mag hier niet aangeroepen worden"))
    r = CSH.check_slab_hybrid("m1", db_path="x")
    assert r["status"] == "pass" and r["_source"] == "multimodal" and r["_reason"] == "ok"
    assert route == {"gemini": 1, "vision": 0, "qwen": 0}


def test_onderbreker_slaat_qwen_even_over(monkeypatch):
    def kapot(*a, **k):
        raise urllib.error.URLError("refused")
    monkeypatch.setattr(QL.urllib.request, "urlopen", kapot)
    monkeypatch.setattr(QL, "_onbereikbaar_tot", 0.0)
    with pytest.raises(QL.QwenOnbereikbaar):
        QL.vraag_qwen(b"x" * 10, pogingen=1)
    # tweede kaart in dezelfde run: geen foto ophalen, geen call, meteen door naar Gemini
    monkeypatch.setattr(QL, "haal_foto", lambda url: pytest.fail("mag niet meer ophalen"))
    with pytest.raises(QL.QwenOnbereikbaar, match="recent onbereikbaar"):
        QL.lees_slab_foto("http://x/foto.jpg")
    monkeypatch.setattr(QL, "_onbereikbaar_tot", 0.0)   # opruimen voor andere tests
