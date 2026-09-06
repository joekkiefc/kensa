"""Gedeelde HTTP-helpers voor Supabase REST calls binnen storage_supabase.

Klein en zelfstandig — geen dependency op supabase_sync (die is voor de
oude sync-laag). Deze helpers zijn schema-parametrisch: elke call kiest
`kensa` of `kensa_test` via Accept-Profile / Content-Profile headers.

Timeout- en pool-strategie identiek aan supabase_sync (RCA §9 fix #1+#2):
- (connect=5, read=25) tuple → geen zinnige TLS-handshake wordt gekilld
- 1 gedeelde requests.Session met HTTPAdapter + urllib3 Retry

VANGNET (2026-09-06, cutover-voorbereiding):
Deze route is de doel-route voor Supabase-only. Zonder SQLite-kopie is een
mislukte write hier definitief verloren — daarom parkeren post()/patch() bij
een TIJDELIJKE fout (netwerk/timeout na de urllib3-retries, of HTTP 408/429/5xx)
de call in dezelfde `sync_retry`-wachtrij die de dual-route al gebruikt; de
drainer (sync_retry_drain.py) speelt 'm later af. De aanroeper krijgt dan een
synthetische 201/204 terug (attribuut `queued_retry_id`), zodat workers de
write als "gelukt" behandelen — precies de semantiek van de dual-route.
BLIJVENDE fouten (overige 4xx: verkeerde kolom, RLS, 409-conflict) worden
NIET geparkeerd (herhalen helpt niet) maar wél luid gelogd; de aanroeper
raise't zoals voorheen. Alleen schema `kensa` wordt geparkeerd — de drainer
speelt in dat schema af. DELETE kent de drainer niet → niet geparkeerd.
"""
from __future__ import annotations
import json
import sys
import urllib.parse
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SECRETS_PATH = "/home/pi/.openclaw/secrets.json"
_CREDS_CACHE: tuple[str, str] | None = None

TIMEOUT: tuple[float, float] = (5.0, 25.0)

_SESSION: requests.Session | None = None


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        s = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=1.0,
            status_forcelist=[502, 503, 504],
            allowed_methods=frozenset(["GET", "HEAD", "POST", "PATCH", "DELETE", "PUT"]),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20, max_retries=retry)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _SESSION = s
    return _SESSION


def _creds() -> tuple[str, str]:
    global _CREDS_CACHE
    if _CREDS_CACHE is None:
        s = json.loads(Path(SECRETS_PATH).read_text())["supabase"]
        _CREDS_CACHE = (s["url"], s["service_role_key"])
    return _CREDS_CACHE


def _headers(schema: str, prefer: str = "return=representation") -> dict:
    _, key = _creds()
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept-Profile": schema,
        "Content-Profile": schema,
        "Content-Type": "application/json",
        "Prefer": prefer,
    }


def get(table: str, schema: str, params: dict, timeout=TIMEOUT) -> list[dict]:
    url = f"{_creds()[0]}/rest/v1/{table}"
    r = _session().get(url, params=params, headers=_headers(schema, prefer="count=none"), timeout=timeout)
    if r.status_code == 200:
        return r.json()
    if r.status_code == 404:
        return []
    r.raise_for_status()
    return []


# ---------------------------------------------------------------------------
# Vangnet: tijdelijke fouten parkeren in sync_retry (zie module-docstring)
# ---------------------------------------------------------------------------
_TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_QUEUE_SCHEMA = "kensa"  # drainer speelt uitsluitend in dit schema af


def _warn(msg: str) -> None:
    print(f"[storage_supabase] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Schrijf-log (cutover-monitoring): één JSON-regel per write, ALLE uitkomsten.
# Bestand: pipeline_supabase_writes.log  — gelezen door report_supabase_writes.py
# Velden: ts, worker, method, table, outcome (ok|queued|failed), status,
#         err_class (dns|connect_timeout|read_timeout|tls|conn_reset|conn_error|
#                    http_5xx|http_429|http_4xx|ok), ms, retry_id, err
# ---------------------------------------------------------------------------
WRITE_LOG_PATH = Path("/home/pi/.openclaw/workspace/agents/kensa/pipeline_supabase_writes.log")
_WORKER = Path(sys.argv[0]).stem if sys.argv and sys.argv[0] else "unknown"


def classify_error(exc: BaseException | None = None, status: int | None = None) -> str:
    """Vertaal exception/status naar een korte foutklasse voor de monitoring.

    Bewust grof: het doel is trends zien (DNS? timeouts? Cloudflare 5xx?), niet
    elke exception apart. Kijkt door de requests-wrapper heen naar de tekst,
    want urllib3 verpakt de echte oorzaak (gaierror, ConnectTimeoutError, ...).
    """
    if exc is not None:
        name = type(exc).__name__
        text = f"{name}: {exc}".lower()
        if "nameresolution" in text or "gaierror" in text or "name or service not known" in text \
                or "temporary failure in name resolution" in text:
            return "dns"
        if isinstance(exc, requests.exceptions.SSLError) or "sslerror" in text or "ssl" in text and "handshake" in text:
            return "tls"
        if isinstance(exc, requests.exceptions.ConnectTimeout) or "connecttimeout" in text:
            return "connect_timeout"
        if isinstance(exc, requests.exceptions.ReadTimeout) or "readtimeout" in text or "read timed out" in text:
            return "read_timeout"
        if "reset by peer" in text or "remotedisconnected" in text or "connection aborted" in text:
            return "conn_reset"
        if isinstance(exc, requests.exceptions.ConnectionError) or "connection" in text:
            return "conn_error"
        return f"exc_{name.lower()}"
    if status is None:
        return "ok"
    if status == 429:
        return "http_429"
    if status >= 500:
        return "http_5xx"
    if status >= 400:
        return "http_4xx"
    return "ok"


def _log_write(method: str, table: str, outcome: str, status: int | None, err_class: str,
               ms: int, retry_id: int | None = None, err: str = "") -> None:
    try:
        from datetime import datetime, timezone
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "worker": _WORKER, "method": method, "table": table, "outcome": outcome,
            "status": status, "err_class": err_class, "ms": ms,
        }
        if retry_id is not None:
            rec["retry_id"] = retry_id
        if err:
            rec["err"] = err[:200]
        WRITE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with WRITE_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # logging mag nooit een write laten falen


class _QueuedResponse(requests.Response):
    """Synthetische Response voor een write die in sync_retry geparkeerd is.

    status_code 201 (POST) / 204 (PATCH) zodat bestaande aanroepers 'm als
    succes zien; `queued_retry_id` maakt het onderscheid zichtbaar voor wie
    het wil weten.
    """

    def __init__(self, status: int, retry_id: int, table: str) -> None:
        super().__init__()
        self.status_code = status
        self.reason = "Queued"
        self.url = table
        self.queued_retry_id = retry_id
        self._content = json.dumps({"queued_retry_id": retry_id, "table": table}).encode()


def _path_with_params(table: str, params: dict | None) -> str:
    """on_conflict e.d. in het pad opnemen — de drainer geeft geen params door,
    maar plakt het pad letterlijk achter /rest/v1/, dus een query-string blijft werken."""
    return f"{table}?{urllib.parse.urlencode(params)}" if params else table


def _is_transient_status(status: int) -> bool:
    return status in _TRANSIENT_STATUS


def _queue_post(table: str, schema: str, payload, prefer: str,
                params: dict | None, error: str) -> int | None:
    if schema != _QUEUE_SCHEMA:
        _warn(f"POST {schema}.{table} mislukt en NIET geparkeerd (drainer kent alleen {_QUEUE_SCHEMA}): {error[:160]}")
        return None
    try:
        import sync_retry  # lazy: top-level kensa-module, voorkomt circulaire import
        sync_retry.ensure_table()
        rows = payload if isinstance(payload, list) else [payload]
        rid = sync_retry.enqueue_post(_path_with_params(table, params), rows, prefer, error)
        _warn(f"POST {table} tijdelijk mislukt → geparkeerd als sync_retry #{rid} ({error[:120]})")
        return rid
    except Exception as e:  # parkeren zelf mislukt: niets verzwijgen
        _warn(f"POST {table} mislukt ÉN parkeren mislukt: {e!r} (oorspronkelijk: {error[:120]})")
        return None


def _queue_patch(table: str, schema: str, query: dict, patch_body: dict, error: str) -> int | None:
    if schema != _QUEUE_SCHEMA:
        _warn(f"PATCH {schema}.{table} mislukt en NIET geparkeerd (drainer kent alleen {_QUEUE_SCHEMA}): {error[:160]}")
        return None
    try:
        import sync_retry
        sync_retry.ensure_table()
        rid = sync_retry.enqueue_patch(table, query, patch_body, error)
        _warn(f"PATCH {table} tijdelijk mislukt → geparkeerd als sync_retry #{rid} ({error[:120]})")
        return rid
    except Exception as e:
        _warn(f"PATCH {table} mislukt ÉN parkeren mislukt: {e!r} (oorspronkelijk: {error[:120]})")
        return None


def post(table: str, schema: str, payload: dict | list, prefer: str = "return=minimal",
         timeout=TIMEOUT, params: dict | None = None) -> requests.Response:
    import time as _t
    url = f"{_creds()[0]}/rest/v1/{table}"
    t0 = _t.perf_counter()
    try:
        r = _session().post(url, json=payload, headers=_headers(schema, prefer=prefer),
                             params=params, timeout=timeout)
    except requests.RequestException as e:  # netwerk/timeout ná de urllib3-retries
        ms = int((_t.perf_counter() - t0) * 1000)
        cls = classify_error(e)
        rid = _queue_post(table, schema, payload, prefer, params, f"{cls}: {e!r}")
        if rid is None:
            _log_write("POST", table, "failed", None, cls, ms, err=repr(e))
            raise
        _log_write("POST", table, "queued", None, cls, ms, retry_id=rid, err=repr(e))
        return _QueuedResponse(201, rid, table)
    ms = int((_t.perf_counter() - t0) * 1000)
    if _is_transient_status(r.status_code):
        cls = classify_error(status=r.status_code)
        rid = _queue_post(table, schema, payload, prefer, params, f"HTTP {r.status_code}: {r.text[:200]}")
        if rid is not None:
            _log_write("POST", table, "queued", r.status_code, cls, ms, retry_id=rid, err=r.text)
            return _QueuedResponse(201, rid, table)
        _log_write("POST", table, "failed", r.status_code, cls, ms, err=r.text)
    elif r.status_code >= 400:
        _warn(f"POST {table} BLIJVENDE fout HTTP {r.status_code} — niet geparkeerd: {r.text[:200]}")
        _log_write("POST", table, "failed", r.status_code, classify_error(status=r.status_code), ms, err=r.text)
    else:
        _log_write("POST", table, "ok", r.status_code, "ok", ms)
    return r


def patch(table: str, schema: str, query: dict, patch_body: dict, timeout=TIMEOUT) -> requests.Response:
    import time as _t
    url = f"{_creds()[0]}/rest/v1/{table}"
    t0 = _t.perf_counter()
    try:
        r = _session().patch(url, params=query, json=patch_body, headers=_headers(schema), timeout=timeout)
    except requests.RequestException as e:
        ms = int((_t.perf_counter() - t0) * 1000)
        cls = classify_error(e)
        rid = _queue_patch(table, schema, query, patch_body, f"{cls}: {e!r}")
        if rid is None:
            _log_write("PATCH", table, "failed", None, cls, ms, err=repr(e))
            raise
        _log_write("PATCH", table, "queued", None, cls, ms, retry_id=rid, err=repr(e))
        return _QueuedResponse(204, rid, table)
    ms = int((_t.perf_counter() - t0) * 1000)
    if _is_transient_status(r.status_code):
        cls = classify_error(status=r.status_code)
        rid = _queue_patch(table, schema, query, patch_body, f"HTTP {r.status_code}: {r.text[:200]}")
        if rid is not None:
            _log_write("PATCH", table, "queued", r.status_code, cls, ms, retry_id=rid, err=r.text)
            return _QueuedResponse(204, rid, table)
        _log_write("PATCH", table, "failed", r.status_code, cls, ms, err=r.text)
    elif r.status_code >= 400:
        _warn(f"PATCH {table} BLIJVENDE fout HTTP {r.status_code} — niet geparkeerd: {r.text[:200]}")
        _log_write("PATCH", table, "failed", r.status_code, classify_error(status=r.status_code), ms, err=r.text)
    else:
        _log_write("PATCH", table, "ok", r.status_code, "ok", ms)
    return r


def delete(table: str, schema: str, query: dict, timeout=TIMEOUT) -> requests.Response:
    """Geen vangnet: de drainer kent geen DELETE-replay. Tijdelijke fouten
    worden luid gelogd; de aanroeper beslist (deletes zijn zeldzaam en idempotent)."""
    import time as _t
    url = f"{_creds()[0]}/rest/v1/{table}"
    t0 = _t.perf_counter()
    try:
        r = _session().delete(url, params=query, headers=_headers(schema, prefer="return=minimal"), timeout=timeout)
    except requests.RequestException as e:
        ms = int((_t.perf_counter() - t0) * 1000)
        _warn(f"DELETE {table} netwerkfout, NIET geparkeerd (geen DELETE-replay): {e!r}")
        _log_write("DELETE", table, "failed", None, classify_error(e), ms, err=repr(e))
        raise
    ms = int((_t.perf_counter() - t0) * 1000)
    if r.status_code >= 400:
        _warn(f"DELETE {table} HTTP {r.status_code}, NIET geparkeerd (geen DELETE-replay): {r.text[:200]}")
        _log_write("DELETE", table, "failed", r.status_code, classify_error(status=r.status_code), ms, err=r.text)
    else:
        _log_write("DELETE", table, "ok", r.status_code, "ok", ms)
    return r
