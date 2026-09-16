"""16-9: valse '-P' opruimen puur op code-vorm (Tommy).

Een échte Japanse promo-set heeft ALTIJD een letter-only code (S-P, SV-P, SM-P,
XY-P, SWSH-P). Een code met een cijfer erin (SV8A, SV2A, S8E) is een gewone
genummerde set zonder promo-tegenhanger → een '-P' erachter is hallucinatie en
gaat eraf. Uitzondering: 'S8A-P' bestaat WEL (25th Anniversary promo, naast de
gewone 25th-set 'S8A') en blijft staan. Geen netwerk, geen label-afhankelijkheid."""
from __future__ import annotations

import pytest

import lezing_opschonen as LO


def _lees(**velden):
    return LO.opschonen({"name": "Vaporeon ex", "grade": "10", "cert": "12345678", **velden})


def test_sv8a_verliest_p():
    out = _lees(label_name="2024 POKEMON SV8a JP #205 VAPOREON ex SPECIAL ART RARE",
                number="#205", set_code="SV8a-P")
    assert out["set_code"] == "SV8A"
    assert out["number"] == "205/SV8A"


def test_sv2a_verliest_p():
    out = _lees(name="Charmander", label_name="2023 POKEMON SV2a JP #004 CHARMANDER MASTER BALL REVERSE HOLO",
                number="#004", set_code="SV2a-P")
    assert out["set_code"] == "SV2A"
    assert out["number"] == "004/SV2A"


def test_nummer_suffix_verliest_p():
    out = _lees(label_name="2024 POKEMON SV8a JP #205 VAPOREON ex SPECIAL ART RARE",
                number="205/SV8a-P", set_code="SV8a-P")
    assert out["number"] == "205/SV8A"
    assert out["set_code"] == "SV8A"


def test_s8a_p_blijft_echte_set():
    """S8A-P is een echte set (25th Anniversary promo) → blijft ongemoeid."""
    out = _lees(name="Pikachu V", label_name="#001 PIKACHU V SD.100 COROCORO COMIC VER.",
                number="#001", set_code="S8a-P")
    assert out["set_code"] == "S8A-P"
    assert out["number"] == "001/S8A-P"


@pytest.mark.parametrize("code", ["SV-P", "XY-P", "SWSH-P", "SM-P", "S-P", "BW-P", "DP-P"])
def test_letter_only_promo_blijft(code):
    """Familie-promo's (letter-only code) zijn echt → blijven altijd staan."""
    out = _lees(label_name="2023 POKEMON JP #003 PIKACHU", number="#003", set_code=code)
    assert out["set_code"] == code


def test_zonder_valse_p_direct():
    assert LO.zonder_valse_p("SV8A-P") == "SV8A"      # cijfer + -P → strip
    assert LO.zonder_valse_p("SV2A-P") == "SV2A"
    assert LO.zonder_valse_p("SV8A") == "SV8A"        # geen -P → ongewijzigd
    assert LO.zonder_valse_p(None) is None
    assert LO.zonder_valse_p("S8A-P") == "S8A-P"      # echte set → blijft
    assert LO.zonder_valse_p("SV-P") == "SV-P"        # letter-only promo → blijft
    assert LO.zonder_valse_p("SWSH-P") == "SWSH-P"
