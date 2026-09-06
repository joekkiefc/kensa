"""check_desc.check_desc (=v2) tegen 500 baseline-fixtures."""
import pytest

from check_desc import check_desc


@pytest.mark.unit
def test_check_desc_all_fixtures(check_desc_fixtures):
    for fx in check_desc_fixtures:
        got = check_desc(
            fx["input"]["slab"],
            fx["input"]["description_jp"],
            fx["input"]["title_check"],
        )
        assert got == fx["expected"], (
            f"item {fx['item_id']}\n  got:      {got}\n  expected: {fx['expected']}"
        )
