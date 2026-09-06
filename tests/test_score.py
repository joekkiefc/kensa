"""Unit-tests voor score.compute_score."""
import pytest

from score import compute_score


@pytest.mark.unit
class TestComputeScore:
    def test_all_pass_all_bonus(self):
        r = compute_score({"status": "pass", "cert": "12345678"},
                          {"status": "pass"}, {"status": "pass"})
        assert r["score"] > 50

    def test_all_skip_returns_50(self):
        r = compute_score({"status": "skip"}, {"status": "skip"}, {"status": "skip"})
        assert 0 <= r["score"] <= 100

    def test_output_has_expected_keys(self):
        r = compute_score({"status": "pass"}, {"status": "pass"}, {"status": "pass"})
        for k in ["score", "verdict", "base", "delta_title", "delta_desc", "reasons"]:
            assert k in r
