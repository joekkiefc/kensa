"""check_title.check_title (=v2) tegen 500 baseline-fixtures."""
import pytest

from check_title import check_title


@pytest.mark.unit
def test_check_title_all_fixtures(check_title_fixtures):
    """Elke baseline-fixture moet byte-identiek reproduceerbaar zijn."""
    for fx in check_title_fixtures:
        got = check_title(
            fx["slab"],
            fx["title_jp"] or "",
            fx["title_en"] or "",
        )
        assert got == fx["expected"], (
            f"item {fx['item_id']}\n  got:      {got}\n  expected: {fx['expected']}"
        )
