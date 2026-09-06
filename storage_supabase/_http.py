"""Gedeelde HTTP-helpers voor Supabase REST calls binnen storage_supabase.

Klein en zelfstandig — geen dependency op supabase_sync (die is voor de
oude sync-laag). Deze helpers zijn schema-parametrisch: elke call kiest
`kensa` of `kensa_test` via Accept-Profile / Content-Profile headers.

Timeout- en pool-strategie identiek aan supabase_sync (RCA §9 fix #1+#2):
- (connect=5, read=25) tuple → geen zinnige TLS-handshake wordt gekilld
- 1 gedeelde requests.Session met HTTPAdapter + urllib3 Retry
"""
from __future__ import annotations
import json
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


def post(table: str, schema: str, payload: dict | list, prefer: str = "return=minimal",
         timeout=TIMEOUT, params: dict | None = None) -> requests.Response:
    url = f"{_creds()[0]}/rest/v1/{table}"
    r = _session().post(url, json=payload, headers=_headers(schema, prefer=prefer),
                         params=params, timeout=timeout)
    return r


def patch(table: str, schema: str, query: dict, patch_body: dict, timeout=TIMEOUT) -> requests.Response:
    url = f"{_creds()[0]}/rest/v1/{table}"
    r = _session().patch(url, params=query, json=patch_body, headers=_headers(schema), timeout=timeout)
    return r


def delete(table: str, schema: str, query: dict, timeout=TIMEOUT) -> requests.Response:
    url = f"{_creds()[0]}/rest/v1/{table}"
    r = _session().delete(url, params=query, headers=_headers(schema, prefer="return=minimal"), timeout=timeout)
    return r
