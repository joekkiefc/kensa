"""ebay_lastsold.parse_card (=v2) tegen 500 synthetische eBay cards."""
import pytest
from scrapling.parser import Selector

import ebay_lastsold


@pytest.mark.integration
def test_parse_card_all_fixtures(parse_card_fixtures):
    for fx in parse_card_fixtures:
        doc = Selector(content=fx["card_html"])
        els = doc.css("div.s-card")
        assert els, "geen s-card div"
        got = ebay_lastsold.parse_card(els[0])
        assert got == fx["expected"]
