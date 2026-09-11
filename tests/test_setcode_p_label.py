"""11-9: valse '-P' opruimen als het PSA-label de gewone set-code letterlijk toont
('2024 POKEMON SV8a JP' → SV8A, niet SV8A-P). Echte promo's blijven ongemoeid. Geen netwerk."""
from __future__ import annotations

import pytest

import lezing_opschonen as LO


def _lees(**velden):
    return LO.opschonen({"name": "Vaporeon ex", "grade": "10", "cert": "12345678", **velden})


def test_sv8a_jp_label_verliest_p():
    out = _lees(label_name="2024 POKEMON SV8a JP #205 VAPOREON ex SPECIAL ART RARE",
                number="#205", set_code="SV8a-P")
    assert out["set_code"] == "SV8A"
    assert out["number"] == "205/SV8A"


def test_sv2a_jp_label_verliest_p():
    out = _lees(name="Charmander", label_name="2023 POKEMON SV2a JP #004 CHARMANDER MASTER BALL REVERSE HOLO",
                number="#004", set_code="SV2a-P")
    assert out["set_code"] == "SV2A"
    assert out["number"] == "004/SV2A"


def test_nummer_suffix_verliest_p():
    out = _lees(label_name="2024 POKEMON SV8a JP #205 VAPOREON ex SPECIAL ART RARE",
                number="205/SV8a-P", set_code="SV8a-P")
    assert out["number"] == "205/SV8A"
    assert out["set_code"] == "SV8A"


def test_setregel_in_set_name_telt_mee():
    """Qwen splitst de labelregel soms: label_name '#008', set_name '2024 POKEMON SV8a JP'."""
    out = _lees(name="Eevee", label_name="#008", set_name="2024 POKEMON SV8a JP", number="#008", set_code="SV8a-P")
    assert out["set_code"] == "SV8A"
    assert out["number"] == "008/SV8A"


def test_echte_promo_zonder_setregel_blijft():
    out = _lees(name="Pikachu V", label_name="#001 PIKACHU V SD.100 COROCORO COMIC VER.",
                number="#001", set_code="S8a-P")
    assert out["set_code"] == "S8A-P"
    assert out["number"] == "001/S8A-P"


@pytest.mark.parametrize("label", [
    "2023 POKEMON SV-P JP #001 PIKACHU",          # familie-promo: code zonder cijfer
    "2024 POKEMON SV8a-P JP #205 VAPOREON",       # label noemt de -P-code zelf
    "2024 POKEMON SV8a - P JP #205 VAPOREON",
    "2024 POKEMON SV8a JP PROMO #205 VAPOREON",   # label noemt PROMO
    "2020 POKEMON S8A #021",                      # geen '<code> JP'-vorm → twijfel → laten staan
    "",
    None,
])
def test_twijfel_of_promo_blijft(label):
    code = "SV-P" if label and "SV-P" in label else ("S8a-P" if label and "S8A" in label else "SV8a-P")
    out = _lees(label_name=label, number="#205", set_code=code)
    assert out["set_code"] == code.upper()
    assert out["number"] == f"205/{code.upper()}"


@pytest.mark.parametrize("code, label", [
    ("SV-P", "2023 POKEMON SV2A JPN OKIDOGI | POKEMON SV"),   # 'SV' is een tijdperk, geen set
    ("XY-P", "#003 | 2016 P.M. JAPANESE XY"),
    ("SWSH-P", "#221 GEM MT 10 | 2020 POKEMON JPN.SWSH"),
])
def test_familie_promo_met_tijdperk_op_label_blijft(code, label):
    labelnaam, _, setnaam = label.partition(" | ")
    out = _lees(label_name=labelnaam, set_name=setnaam or None, number="#003", set_code=code)
    assert out["set_code"] == code


def test_zonder_valse_p_direct():
    assert LO.zonder_valse_p("SV8A-P", "2024 POKEMON SV8a JP") == "SV8A"
    assert LO.zonder_valse_p("SV8A", "2024 POKEMON SV8a JP") == "SV8A"
    assert LO.zonder_valse_p(None, "2024 POKEMON SV8a JP") is None
    assert LO.zonder_valse_p("S8A-P", "2024 POKEMON SV8a JP") == "S8A-P"   # S8A is geen los woord in SV8A
