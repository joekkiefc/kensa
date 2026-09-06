"""scrape_buyee.parse_listing (=v2) tegen 500 synthetische HTMLs."""
import pytest
from scrapling.parser import Selector

import scrape_buyee


@pytest.mark.integration  # gebruikt scrapling parser
def test_parse_listing_all_fixtures(parse_listing_fixtures):
    for fx in parse_listing_fixtures:
        doc = Selector(content=f"<ul>{fx['li_html']}</ul>")
        li_els = doc.css("li")
        assert li_els, "geen <li> in fixture"
        got = scrape_buyee.parse_listing(li_els[0])
        assert got == fx["expected"]
