"""Unit-tests voor supabase_sync — HTTP-layer + retry-enqueue integratie."""
import json
import sqlite3
import urllib.error
from unittest.mock import patch

import pytest

import supabase_sync as sbs


class FakeResp:
    def __init__(self, code=200, body=b""):
        self.status_code = code
        self._body = body
    def read(self):
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return False


def _fake_requests_ok():
    """requests.post/patch die 200 returnt."""
    class R:
        status_code = 200
        text = ""
        def __init__(self, *_a, **_kw):
            pass
    return R


@pytest.mark.unit
def test_mode_defaults_sqlite(monkeypatch):
    monkeypatch.delenv("KENSA_STORAGE", raising=False)
    assert sbs.mode() == "sqlite"
    assert sbs.enabled() is False


@pytest.mark.unit
def test_mode_dual_enabled(monkeypatch):
    monkeypatch.setenv("KENSA_STORAGE", "dual")
    assert sbs.mode() == "dual"
    assert sbs.enabled() is True


@pytest.mark.unit
def test_mode_supabase_enabled(monkeypatch):
    monkeypatch.setenv("KENSA_STORAGE", "supabase")
    assert sbs.enabled() is True


@pytest.mark.unit
def test_maybe_json_none_and_empty():
    assert sbs._maybe_json(None) is None
    assert sbs._maybe_json("") is None


@pytest.mark.unit
def test_maybe_json_parses_string():
    assert sbs._maybe_json('[{"x":1}]') == [{"x": 1}]


@pytest.mark.unit
def test_maybe_json_returns_dict_unchanged():
    d = {"a": 1}
    assert sbs._maybe_json(d) is d


@pytest.mark.unit
def test_maybe_json_invalid_string_returns_none():
    assert sbs._maybe_json("not json {{{") is None


@pytest.mark.unit
def test_post_no_rows_returns_true():
    """Lege rows-list = niks te posten, moet True zijn (semantiek 'geen fout')."""
    assert sbs._post("listings", []) is True


class _FakeSession:
    """Test-double voor de shared requests.Session: elke HTTP-methode
    returned een fake response of raist een exceptie. Wordt via
    monkeypatch.setattr('supabase_sync._session', lambda: _FakeSession(...))
    ingezet."""
    def __init__(self, status=200, text="", raises: Exception | None = None):
        class R:
            status_code = status
            def __init__(self):
                self.text = text
        self._R = R
        self._raises = raises
    def _resp(self, *_a, **_kw):
        if self._raises is not None:
            raise self._raises
        return self._R()
    post = _resp
    patch = _resp
    delete = _resp
    get = _resp


@pytest.mark.unit
def test_post_success(monkeypatch):
    """Mock succesvolle HTTP → _post returned True, geen enqueue in retry."""
    monkeypatch.setattr("supabase_sync._session", lambda: _FakeSession(status=200))
    with patch("supabase_sync._enqueue_post_retry") as enq:
        ok = sbs._post("listings", [{"item_id": "test"}])
        assert ok is True
        enq.assert_not_called()


@pytest.mark.unit
def test_post_failure_enqueues_retry(monkeypatch):
    """Mock 500-response → _post returned False EN roept retry-enqueue aan."""
    monkeypatch.setattr("supabase_sync._session",
                        lambda: _FakeSession(status=500, text="server error"))
    with patch("supabase_sync._enqueue_post_retry") as enq:
        ok = sbs._post("listings", [{"item_id": "test"}])
        assert ok is False
        enq.assert_called_once()


@pytest.mark.unit
def test_post_exception_enqueues_retry(monkeypatch):
    monkeypatch.setattr("supabase_sync._session",
                        lambda: _FakeSession(raises=ConnectionError("net down")))
    with patch("supabase_sync._enqueue_post_retry") as enq:
        ok = sbs._post("listings", [{"item_id": "x"}])
        assert ok is False
        enq.assert_called_once()


@pytest.mark.unit
def test_patch_success(monkeypatch):
    monkeypatch.setattr("supabase_sync._session", lambda: _FakeSession(status=204))
    with patch("supabase_sync._enqueue_patch_retry") as enq:
        ok = sbs._patch("listings", {"item_id": "eq.m1"}, {"foo": "bar"})
        assert ok is True
        enq.assert_not_called()


@pytest.mark.unit
def test_patch_failure_enqueues_retry(monkeypatch):
    monkeypatch.setattr("supabase_sync._session",
                        lambda: _FakeSession(status=400, text="bad request"))
    with patch("supabase_sync._enqueue_patch_retry") as enq:
        ok = sbs._patch("listings", {"item_id": "eq.m1"}, {"foo": "bar"})
        assert ok is False
        enq.assert_called_once()


@pytest.mark.unit
def test_sync_functions_noop_when_disabled(monkeypatch):
    """Bij KENSA_STORAGE!=dual/supabase mag sync_* niks doen (geen HTTP-call)."""
    monkeypatch.delenv("KENSA_STORAGE", raising=False)
    with patch("supabase_sync._post") as post_mock, patch("supabase_sync._patch") as patch_mock:
        sbs.sync_slab_status("m1", "ok", "card:1:10")
        sbs.sync_cm_queue_upsert("m1", "https://cm", "10", "2026-01-01T00:00:00Z")
        sbs.sync_price_cache_ebay("k", "q", {}, "2026-01-01T00:00:00Z")
    post_mock.assert_not_called()
    patch_mock.assert_not_called()


@pytest.mark.unit
def test_sync_cm_queue_upsert_enabled(monkeypatch):
    monkeypatch.setenv("KENSA_STORAGE", "dual")
    with patch("supabase_sync._post") as post_mock:
        sbs.sync_cm_queue_upsert("m1", "https://cm", "10", "2026-01-01T00:00:00Z",
                                 card_key="charizard:11:10")
    post_mock.assert_called_once()


@pytest.mark.unit
def test_sync_upsert_listing_calls_post(monkeypatch):
    monkeypatch.setenv("KENSA_STORAGE", "dual")
    with patch("supabase_sync._post") as post_mock:
        sbs.sync_upsert_listing(
            {"item_id": "m1", "title_jp": "x"},
            "2026-01-01T00:00:00Z",
            "2026-01-02T00:00:00Z",
        )
    post_mock.assert_called_once()


@pytest.mark.unit
def test_enqueue_post_retry_writes_to_sync_retry_table(tmp_path, monkeypatch):
    """Lazy-import van sync_retry moet werken zonder crash."""
    monkeypatch.setattr("sync_retry.DB_PATH", tmp_path / "test_kensa.db")
    sbs._enqueue_post_retry("listings", [{"x": 1}], "return=minimal", "500 err")
    import sync_retry
    stats = sync_retry.stats()
    assert stats["pending"] == 1
