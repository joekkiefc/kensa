"""analyze_split.ebay_phase._ebay_build_query (=v2) tegen 500 productie-slabs."""
import pytest

from analyze_split.ebay_phase import _ebay_build_query


@pytest.mark.unit
def test_ebay_build_query_all_fixtures(ebay_build_query_fixtures):
    for fx in ebay_build_query_fixtures:
        got = list(_ebay_build_query(
            fx["input"]["slab"],
            fx["input"]["listing"],
            fx["input"]["llm_data"],
            False,
        ))
        assert got == fx["expected"]
