"""fetch_detail.parse_detail (=v2) tegen 500 opgeslagen HTML-pages."""
from pathlib import Path
from unittest.mock import patch
import pytest

import storage
import fetch_detail

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "parse_detail_refactor_fixtures"


@pytest.mark.integration  # leest lokale HTML files
def test_parse_detail_all_fixtures(parse_detail_fixtures):
    for fx in parse_detail_fixtures:
        html = (FIXTURE_DIR / fx["html_file"]).read_text()
        with patch.object(storage, "now_iso", return_value="2026-09-01T00:00:00+00:00"):
            got = fetch_detail.parse_detail(html, fx["item_id"])
        assert got == fx["expected"], f"idx {fx['index']} mismatch"
