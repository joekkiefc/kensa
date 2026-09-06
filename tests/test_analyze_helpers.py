"""Unit-tests voor de kleine helpers in analyze.py + analyze_split submodules."""
import pytest

from analyze import (
    _build_card_key,
    _looks_like_bundle,
    _needs_llm,
    _pokemon_en_lookup,
)


@pytest.mark.unit
def test_pokemon_en_lookup_cached_and_lowercased():
    s = _pokemon_en_lookup()
    assert isinstance(s, set)
    assert "pikachu" in s
    assert "charizard" in s
    # Case-insensitive: alle entries zijn lowercase
    assert all(name == name.lower() for name in s)


@pytest.mark.unit
class TestBuildCardKey:
    def test_full_key_with_set(self):
        slab = {"grade": "10", "number": "089", "set_code": "CRI"}
        llm = {"name": "Frogadier", "set_code": "CRI"}
        assert _build_card_key(slab, llm) == "frogadier:089:10:cri"

    def test_key_without_set(self):
        slab = {"grade": "10", "number": "089"}
        llm = {"name": "Frogadier"}
        assert _build_card_key(slab, llm) == "frogadier:089:10"

    def test_missing_grade_returns_none(self):
        slab = {"number": "089"}
        llm = {"name": "Frogadier"}
        assert _build_card_key(slab, llm) is None

    def test_missing_pokemon_returns_none(self):
        slab = {"grade": "10", "number": "089"}
        llm = {"name": "SomeUnknownWord"}  # niet in pokedex
        assert _build_card_key(slab, llm) is None

    def test_missing_number_returns_none(self):
        slab = {"grade": "10"}
        llm = {"name": "Frogadier"}
        assert _build_card_key(slab, llm) is None

    def test_llm_none_falls_back_to_slab(self):
        slab = {"grade": "10", "number": "089", "card_name": "FA/FROGADIER"}
        assert _build_card_key(slab, None) is not None


@pytest.mark.unit
class TestLooksLikeBundle:
    def test_no_bundle_words(self):
        assert _looks_like_bundle("PSA10 Charizard", "PSA10 Charizard") is None

    def test_japanese_bundle_keyword(self):
        assert _looks_like_bundle("まとめ売り Pokemon PSA10", None) is not None

    def test_double_psa10_in_jp_only(self):
        # 2× PSA10 in JP titel = bundle
        assert _looks_like_bundle("PSA10 リザードン PSA10 ピカチュウ", None) is not None

    def test_double_psa10_in_en_only_no_bundle(self):
        # EN-only double PSA10 komt vaak door translate — geen bundle
        assert _looks_like_bundle(None, "PSA10 Charizard [PSA10]") is None

    def test_empty_titles_no_bundle(self):
        assert _looks_like_bundle("", "") is None
        assert _looks_like_bundle(None, None) is None


@pytest.mark.unit
class TestNeedsLLM:
    def test_slab_status_not_pass_needs_llm(self):
        needs, reason = _needs_llm({"status": "skip"})
        assert needs is True
        assert "slab-status niet pass" in reason

    def test_slab_with_all_fields_and_known_pokemon_no_llm(self):
        # pass + single-word card_name=known + grade + number + set_name → geen LLM
        needs, _ = _needs_llm({
            "status": "pass",
            "card_name": "CHARIZARD",
            "grade": "10",
            "number": "011",
            "set_name": "2024 POKEMON SV1a JP",
        })
        assert needs is False

    def test_missing_set_name_needs_llm(self):
        needs, reason = _needs_llm({
            "status": "pass", "card_name": "CHARIZARD", "grade": "10", "number": "011",
        })
        assert needs is True
        assert "set_name" in reason

    def test_multi_word_name_needs_llm(self):
        # multi-word (bv 'Rocket's Charizard' of 'Galarian Zapdos') → LLM
        needs, reason = _needs_llm({
            "status": "pass",
            "card_name": "GALARIAN ZAPDOS",
            "grade": "10",
            "number": "011",
            "set_name": "X",
        })
        assert needs is True

    def test_missing_grade_needs_llm(self):
        needs, reason = _needs_llm({"status": "pass", "card_name": "CHARIZARD", "number": "011"})
        assert needs is True

    def test_unknown_pokemon_needs_llm(self):
        needs, reason = _needs_llm({
            "status": "pass",
            "card_name": "SOMEUNKNOWN",
            "grade": "10",
            "number": "011",
        })
        assert needs is True
