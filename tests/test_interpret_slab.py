"""llm_client.interpret_slab (=v2) tegen 500 mocked-Gemini fixtures."""
import json
import sqlite3
import urllib.error
from unittest.mock import patch

import pytest

import llm_client


class FakeResp:
    def __init__(self, body: bytes):
        self._b = body
    def read(self):
        return self._b
    def __enter__(self):
        return self
    def __exit__(self, *_a):
        return False


class FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code: int, body: str):
        super().__init__(url="https://x", code=code, msg="", hdrs=None, fp=None)
        self._body_bytes = body.encode()
    def read(self):
        return self._body_bytes


def _rebuild(mock_list):
    out = []
    for m in mock_list:
        k = m["kind"]
        if k == "response": out.append(m["data"])
        elif k == "HTTPError": out.append(FakeHTTPError(m["code"], m["body"]))
        elif k == "URLError": out.append(urllib.error.URLError(m["reason"]))
        else: out.append(RuntimeError(m.get("msg") or "x"))
    return out


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
def test_interpret_slab_all_fixtures(interpret_slab_fixtures):
    # Cache-clear voor test-hashes zodat we altijd via de mocked-Gemini flow gaan
    conn = sqlite3.connect(str(llm_client.DB_PATH))
    cur = conn.cursor()
    for fx in interpret_slab_fixtures:
        h = llm_client._hash_ocr(
            fx["input"]["ocr_text"], fx["input"]["title_en"], fx["input"]["title_jp"],
        )
        cur.execute("DELETE FROM llm_slab_cache WHERE ocr_hash=?", (h,))
    conn.commit()
    conn.close()

    for fx in interpret_slab_fixtures:
        llm_client._QUOTA_EXHAUSTED = False
        responses = _rebuild(fx["mock_responses"])
        with patch(
            "interpret_slab_split.gemini_interpret.urllib.request.urlopen",
            side_effect=_se(responses),
        ), patch(
            "interpret_slab_split.gemini_interpret.time.sleep",
            lambda *_a, **_k: None,
        ):
            got = llm_client.interpret_slab(
                fx["input"]["ocr_text"],
                fx["input"]["title_en"],
                fx["input"]["title_jp"],
                verbose=False,
                max_retries=1,
            )
        if isinstance(got, dict) and "_meta" in got and isinstance(got["_meta"], dict):
            got["_meta"].pop("latency_ms", None)
        assert got == fx["expected"], f"idx {fx['index']} scen {fx['scenario']}"
