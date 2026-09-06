"""llm_client.judge_sales (=v2) tegen 500 mocked-Gemini fixtures."""
import json
import urllib.error
from unittest.mock import patch

import pytest

import llm_client


class FakeResp:
    def __init__(self, b): self._b = b
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *_a): return False


class FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code, body):
        super().__init__(url="https://x", code=code, msg="", hdrs=None, fp=None)
        self._body_bytes = body.encode() if isinstance(body, str) else body
    def read(self): return self._body_bytes


def _rebuild(m):
    k = m["kind"]
    if k == "response": return m["data"]
    if k == "HTTPError": return FakeHTTPError(m["code"], m["body"])
    if k == "URLError": return urllib.error.URLError(m["reason"])
    return RuntimeError()


def _se(items):
    it = iter(items)
    def side_effect(*_a, **_kw):
        v = next(it)
        if isinstance(v, Exception): raise v
        if isinstance(v, dict): return FakeResp(json.dumps(v).encode())
        return FakeResp(str(v).encode())
    return side_effect


@pytest.mark.slow
@pytest.mark.integration
def test_judge_sales_all_fixtures(judge_sales_fixtures):
    for fx in judge_sales_fixtures:
        llm_client._QUOTA_EXHAUSTED = False
        responses = [_rebuild(m) for m in fx["mock"]]
        with patch(
            "judge_sales_split.gemini_judge.urllib.request.urlopen",
            side_effect=_se(responses),
        ), patch(
            "judge_sales_split.gemini_judge.time.sleep",
            lambda *_a, **_k: None,
        ):
            got = llm_client.judge_sales(
                fx["input"]["query"],
                fx["input"]["sales"],
                context=fx["input"]["context"],
                verbose=False,
                max_retries=1,
            )
        if isinstance(got, dict) and "_meta" in got and isinstance(got["_meta"], dict):
            got["_meta"].pop("latency_ms", None)
        assert got == fx["expected"], f"idx {fx['index']} scen {fx['scenario']}"
