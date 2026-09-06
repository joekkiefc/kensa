"""Vangnet in storage_supabase._http (cutover-voorbereiding, 2026-09-06).

Hermetisch: geen netwerk, geen SQLite. De Session en sync_retry.enqueue_* worden
gemockt. Test de classificatie (tijdelijk vs blijvend), het pad-formaat voor de
drainer en de synthetische 201/204.
"""
from __future__ import annotations
import sys
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from storage_supabase import _http  # noqa: E402


class _Resp(requests.Response):
    def __init__(self, status: int, body: bytes = b""):
        super().__init__()
        self.status_code = status
        self._content = body


class _FakeSession:
    """post/patch/delete geven een vooraf bepaalde Response of raise'en een exception."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    def _do(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    def post(self, url, **kw):
        return self._do("POST", url, **kw)

    def patch(self, url, **kw):
        return self._do("PATCH", url, **kw)

    def delete(self, url, **kw):
        return self._do("DELETE", url, **kw)


@pytest.fixture(autouse=True)
def _geen_echte_schrijflog(monkeypatch, tmp_path):
    """Tests mogen NOOIT de productie-log pipeline_supabase_writes.log raken."""
    monkeypatch.setattr(_http, "WRITE_LOG_PATH", tmp_path / "writes.log")


@pytest.fixture
def queue(monkeypatch):
    """Vangt enqueue-calls op i.p.v. SQLite te raken."""
    import sync_retry
    recorded = []
    monkeypatch.setattr(sync_retry, "ensure_table", lambda: None)
    monkeypatch.setattr(sync_retry, "enqueue_post",
                        lambda path, rows, prefer, err: recorded.append(("POST", path, rows, prefer, err)) or 41)
    monkeypatch.setattr(sync_retry, "enqueue_patch",
                        lambda path, query, patch, err: recorded.append(("PATCH", path, query, patch, err)) or 42)
    monkeypatch.setattr(_http, "_creds", lambda: ("https://x.supabase.co", "k"))
    return recorded


def _use(monkeypatch, outcome) -> _FakeSession:
    s = _FakeSession(outcome)
    monkeypatch.setattr(_http, "_SESSION", s)
    return s


# --- POST -------------------------------------------------------------------

def test_post_netwerkfout_wordt_geparkeerd_met_synthetische_201(monkeypatch, queue):
    _use(monkeypatch, requests.ConnectionError("edge dood"))
    r = _http.post("analysis", "kensa", {"item_id": "m1"}, prefer="return=minimal")
    assert r.status_code == 201 and r.queued_retry_id == 41
    assert queue == [("POST", "analysis", [{"item_id": "m1"}], "return=minimal", queue[0][4])]
    assert queue[0][4].startswith("conn_error: ")  # foutklasse voorop, dan de exception


def test_post_on_conflict_blijft_in_pad_voor_drainer(monkeypatch, queue):
    _use(monkeypatch, _Resp(503, b"down"))
    r = _http.post("analysis", "kensa", [{"a": 1}], params={"on_conflict": "item_id,trap,created_at"})
    assert r.status_code == 201
    assert queue[0][1] == "analysis?on_conflict=item_id%2Ctrap%2Ccreated_at"


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_post_tijdelijke_status_wordt_geparkeerd(monkeypatch, queue, status):
    _use(monkeypatch, _Resp(status))
    r = _http.post("photos", "kensa", {"x": 1})
    assert r.status_code == 201 and len(queue) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_post_blijvende_fout_wordt_niet_geparkeerd(monkeypatch, queue, status):
    _use(monkeypatch, _Resp(status, b"permanent"))
    r = _http.post("photos", "kensa", {"x": 1})
    assert r.status_code == status and queue == []


def test_post_succes_raakt_queue_niet(monkeypatch, queue):
    _use(monkeypatch, _Resp(201))
    assert _http.post("photos", "kensa", {"x": 1}).status_code == 201
    assert queue == []


def test_post_ander_schema_wordt_niet_geparkeerd(monkeypatch, queue):
    _use(monkeypatch, _Resp(503))
    r = _http.post("test_listings", "kensa_test", {"x": 1})
    assert r.status_code == 503 and queue == []


def test_post_netwerkfout_zonder_parkeermogelijkheid_raiset(monkeypatch, queue):
    import sync_retry
    monkeypatch.setattr(sync_retry, "enqueue_post", lambda *a: (_ for _ in ()).throw(RuntimeError("db dicht")))
    _use(monkeypatch, requests.ConnectionError("edge dood"))
    with pytest.raises(requests.ConnectionError):
        _http.post("analysis", "kensa", {"x": 1})


# --- PATCH ------------------------------------------------------------------

def test_patch_timeout_wordt_geparkeerd_met_synthetische_204(monkeypatch, queue):
    _use(monkeypatch, requests.ReadTimeout("traag"))
    r = _http.patch("listings", "kensa", {"item_id": "eq.m1"}, {"slab_status": "ocr_done"})
    assert r.status_code == 204 and r.queued_retry_id == 42
    assert queue == [("PATCH", "listings", {"item_id": "eq.m1"}, {"slab_status": "ocr_done"}, queue[0][4])]


def test_patch_blijvende_fout_wordt_niet_geparkeerd(monkeypatch, queue):
    _use(monkeypatch, _Resp(400, b"bad column"))
    r = _http.patch("listings", "kensa", {"item_id": "eq.m1"}, {"nope": 1})
    assert r.status_code == 400 and queue == []


# --- DELETE (bewust geen vangnet) -------------------------------------------

def test_delete_heeft_geen_vangnet_en_raiset_bij_netwerkfout(monkeypatch, queue):
    _use(monkeypatch, requests.ConnectionError("edge dood"))
    with pytest.raises(requests.ConnectionError):
        _http.delete("listings", "kensa", {"item_id": "eq.m1"})
    assert queue == []


# --- Schrijf-log + foutklasse (cutover-monitoring) ---------------------------

def test_classify_error_herkent_dns_timeouts_tls_en_statussen():
    import urllib3
    c = _http.classify_error
    assert c(requests.ConnectionError("HTTPSConnectionPool: Max retries ... NameResolutionError: Failed to resolve 'x.supabase.co' ([Errno -3] Temporary failure in name resolution)")) == "dns"
    assert c(requests.ConnectTimeout("connect timed out")) == "connect_timeout"
    assert c(requests.ReadTimeout("Read timed out. (read timeout=25)")) == "read_timeout"
    assert c(requests.exceptions.SSLError("SSL: HANDSHAKE_FAILURE")) == "tls"
    assert c(requests.ConnectionError("('Connection aborted.', ConnectionResetError(104, 'Connection reset by peer'))")) == "conn_reset"
    assert c(requests.ConnectionError("iets anders")) == "conn_error"
    assert c(status=503) == "http_5xx" and c(status=429) == "http_429" and c(status=400) == "http_4xx"
    assert c(status=201) == "ok" and c() == "ok"


def _read_log(path):
    import json as _j
    return [_j.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_schrijflog_registreert_ok_queued_en_failed(monkeypatch, queue, tmp_path):
    log = tmp_path / "writes.log"
    monkeypatch.setattr(_http, "WRITE_LOG_PATH", log)
    _use(monkeypatch, _Resp(201));                       _http.post("analysis", "kensa", {"a": 1})
    _use(monkeypatch, requests.ReadTimeout("traag"));     _http.post("analysis", "kensa", {"a": 1})
    _use(monkeypatch, _Resp(400, b"kolom bestaat niet")); _http.post("photos", "kensa", {"a": 1})
    _use(monkeypatch, _Resp(503));                       _http.patch("listings", "kensa", {"item_id": "eq.m1"}, {"x": 1})
    recs = _read_log(log)
    assert [(r["method"], r["table"], r["outcome"], r["err_class"]) for r in recs] == [
        ("POST", "analysis", "ok", "ok"),
        ("POST", "analysis", "queued", "read_timeout"),
        ("POST", "photos", "failed", "http_4xx"),
        ("PATCH", "listings", "queued", "http_5xx"),
    ]
    assert recs[1]["retry_id"] == 41 and recs[3]["retry_id"] == 42
    assert all("ts" in r and "ms" in r and "worker" in r for r in recs)
    assert "kolom bestaat niet" in recs[2]["err"]


def test_schrijflog_fout_laat_write_nooit_falen(monkeypatch, queue):
    monkeypatch.setattr(_http, "WRITE_LOG_PATH", Path("/proc/onmogelijk/writes.log"))
    _use(monkeypatch, _Resp(201))
    assert _http.post("analysis", "kensa", {"a": 1}).status_code == 201
