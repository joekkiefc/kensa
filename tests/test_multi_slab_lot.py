"""Multi-slab lots afbreken (Tommy 11-9): het systeem is voor 1 kaart per listing.
Lot = herkennen (titel óf lezing), netjes afsluiten (skip), nooit meer behandelen.
Geen netwerk: alles gemockt."""
from __future__ import annotations

import pytest

import lezing_opschonen as LO
import qwen_lezer as QL
import check_slab_hybrid as CSH
from ocr_router import is_multi_slab_lot

ENKEL = {"name": "Pikachu V", "label_name": "#001 PIKACHU V", "number": "001/SV-P",
         "set_code": "SV-P", "grade": "10", "cert": "16238282"}


# ---------------------------------------------------------------- is_lot_lezing
@pytest.mark.parametrize("lezing, reden", [
    ({**ENKEL, "number": ["#260", "#261", "#289"]}, "number_lijst"),
    ({**ENKEL, "grade": [10, 10, 9]}, "grade_lijst"),
    ({**ENKEL, "cert": ["164173497", "164173498"]}, "cert_lijst"),
    ({**ENKEL, "name": ["Pikachu", "Pikachu"]}, "name_lijst"),
    ({**ENKEL, "cert": "164173497 164173498 164173499"}, "meerdere_certs"),
    ({**ENKEL, "cert": "164173497,164173498"}, "meerdere_certs"),
    ({**ENKEL, "grade": "10/9"}, "meerdere_grades"),
    ({**ENKEL, "grade": "10, 9, 10"}, "meerdere_grades"),
    ({**ENKEL, "multi_slab": True}, "multi_slab_flag"),
])
def test_is_lot_lezing_herkent_lot(lezing, reden):
    assert LO.is_lot_lezing(lezing) == (True, reden)


@pytest.mark.parametrize("lezing", [
    ENKEL,
    {**ENKEL, "grade": 10, "cert": 162382822},                 # ints zijn gewoon scalars
    {**ENKEL, "grade": "9.5", "number": "068/187"},            # '/' in nummer is geen lot
    {**ENKEL, "cert": None, "grade": None},
    {**ENKEL, "multi_slab": False},
    {"_error": "parse: x"},
    None,
])
def test_is_lot_lezing_negatief_enkel_slab(lezing):
    assert LO.is_lot_lezing(lezing) == (False, "")


# ---------------------------------------------------------------- titel-regex
@pytest.mark.parametrize("titel", [
    "トウホクのピカチュウ ヒロシマのピカチュウ フクオカのピカチュウ PSA10/9",
    "Pikachu promo PSA 10/9",
    "PSA9.5/10 set",
    "【連番】トウホク ヒロシマ フクオカ ピカチュウ PSA10 プロモ",
])
def test_titel_regex_lot(titel):
    is_lot, pat = is_multi_slab_lot(titel, None)
    assert is_lot and pat


@pytest.mark.parametrize("titel", [
    "ピカチュウ PSA10 プロモ 001/SV-P",
    "Pikachu V PSA 10 068/187",
    "Charizard PSA9 2023",
])
def test_titel_regex_geen_lot(titel):
    assert is_multi_slab_lot(titel, None) == (False, "")


# ---------------------------------------------------------------- contract-hardening
def test_opschonen_laat_geen_lijst_door():
    out = LO.opschonen({**ENKEL, "number": ["#260", "#261"], "grade": [10, 9], "cert": ["1", "2"]})
    assert out["number"] == ""
    assert out["grade"] is None
    assert out["cert"] is None
    assert out["name"] == "Pikachu V"          # rest van de opschoning ongemoeid


def test_opschonen_scalars_ongewijzigd():
    out = LO.opschonen(dict(ENKEL))
    assert out["number"] == "001/SV-P" and out["grade"] == "10" and out["cert"] == "16238282"


# ---------------------------------------------------------------- qwen_lezer: lot-vlag uit de ruwe lezing
def test_lees_slab_foto_markeert_lot(monkeypatch):
    monkeypatch.setattr(QL, "haal_foto", lambda url: (b"x" * 9000, url, False))
    ruw = {"name": "Pikachu", "number": ["#260", "#261", "#289"], "grade": [10, 9, 10],
           "cert": "164173497 164173498 164173499", "set_code": "SV-P"}
    monkeypatch.setattr(QL, "vraag_qwen", lambda img: (ruw, 1.0))
    d = QL.lees_slab_foto("http://p/1.jpg")
    assert d["multi_slab"] is True and d["_lot_reden"] == "number_lijst"
    assert d["number"] == "" and d["grade"] is None      # hardening blijft ook gelden
    assert LO.is_lot_lezing(d) == (True, "multi_slab_flag")


def test_prompt_fixed_heeft_multi_slab_zin():
    assert "multi_slab" in QL.PROMPT_FIXED
    assert "multi_slab" not in QL.PHOTO_INTERPRET_PROMPT   # basisprompt (Gemini) ongemoeid


# ---------------------------------------------------------------- route: lezing-lot → skip, geen Vision
@pytest.fixture
def route(monkeypatch):
    monkeypatch.setattr(CSH, "_fetch_titles", lambda iid, db: ("タイトル", "title"))
    monkeypatch.setattr(CSH, "_load_photo_urls", lambda iid, db: [(0, "http://p/orig/1.jpg"), (1, "http://p/orig/2.jpg")])
    calls = {"vision": 0, "qwen": 0, "gemini": 0}

    def vision(iid, max_photos=3, db_path=None):
        calls["vision"] += 1
        return {"status": "pass", "card_name": "Pikachu", "grade": "10"}
    monkeypatch.setattr(CSH, "_classic_check_slab", vision)

    def gemini(url, en, jp):
        calls["gemini"] += 1
        return {"name": "Pikachu", "number": ["260", "261"], "grade": "10", "cert": "11112222"}
    monkeypatch.setattr(CSH, "interpret_slab_photo", gemini)
    return calls


def test_lezing_lot_wordt_skip_zonder_vision(route, monkeypatch):
    monkeypatch.setattr(CSH, "LEZER", "qwen")
    def qwen(url):
        route["qwen"] += 1
        return {**ENKEL, "multi_slab": True, "_lot_reden": "number_lijst", "number": "", "grade": None}
    monkeypatch.setattr(CSH, "_qwen_lees", qwen)
    r = CSH.check_slab_hybrid("m1", db_path="x")
    assert r["status"] == "skip" and r["_source"] == "skip"
    assert r["_reason"] == "multi_slab_lot_lezing:multi_slab_flag"
    assert r["card_name"] is None and r["grade"] is None
    assert route == {"vision": 0, "qwen": 1, "gemini": 0}     # 1 foto, dan stoppen; geen vangnet


def test_gemini_lezing_lot_wordt_ook_skip(route, monkeypatch):
    monkeypatch.setattr(CSH, "LEZER", "gemini")
    r = CSH.check_slab_hybrid("m1", db_path="x")
    assert r["status"] == "skip" and r["_reason"] == "multi_slab_lot_lezing:number_lijst"
    assert route == {"vision": 0, "qwen": 0, "gemini": 1}


def test_titel_lot_skip_en_lezing_lot_skip_zijn_gelijk(route, monkeypatch):
    monkeypatch.setattr(CSH, "_fetch_titles", lambda iid, db: ("PSA10/9 lot", None))
    r = CSH.check_slab_hybrid("m1", db_path="x")
    assert r["status"] == "skip" and r["_reason"].startswith("multi_slab_lot:")
    assert route == {"vision": 0, "qwen": 0, "gemini": 0}
    verwacht = {k for k in CSH._lot_skip("x") if k != "_reason"}
    assert verwacht == {k for k in r if k != "_reason"}


def test_enkel_slab_blijft_pass(route, monkeypatch):
    monkeypatch.setattr(CSH, "LEZER", "qwen")
    monkeypatch.setattr(CSH, "_qwen_lees", lambda url: dict(ENKEL))
    r = CSH.check_slab_hybrid("m1", db_path="x")
    assert r["status"] == "pass" and r["_source"] == "qwen" and r["card_name"] == "Pikachu V"


def test_fetch_titles_leest_supabase_bij_schakelaar(monkeypatch):
    monkeypatch.setenv("KENSA_READ_STORAGE", "supabase")
    import storage_supabase
    monkeypatch.setattr(storage_supabase, "load_listing_supabase",
                        lambda iid: {"title_jp": "【連番】x", "title_en": None})
    assert CSH._fetch_titles("m1", "geen.db") == ("【連番】x", None)


# ---------------------------------------------------------------- worker_ebay: crash sluit item af
def test_worker_ebay_crash_zet_item_op_fout(monkeypatch):
    import worker_ebay as WE
    import analyze_split.score_only as SO
    gezien = []
    monkeypatch.setattr(SO, "_mark_slab_status", lambda iid, status, card_key=None: gezien.append((iid, status)))
    WE._sluit_af_na_crash("m1", AttributeError("'list' object has no attribute 'split'"))
    assert gezien == [("m1", "ebay_error")]
    assert WE.CRASH_STATUS != "ocr_done"
