"""vision_split.psa_parser._psa_name_and_set (=v2) tegen 500 synthetische heads."""
import pytest

from vision_split.psa_parser import _psa_name_and_set


@pytest.mark.unit
def test_psa_name_and_set_all_fixtures(psa_name_set_fixtures):
    for fx in psa_name_set_fixtures:
        got = list(_psa_name_and_set(fx["head"]))
        assert got == fx["expected"]
