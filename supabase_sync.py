"""Kensa → Supabase dual-write laag.

Wordt aangeroepen door storage.py NA elke succesvolle SQLite-commit.
Best-effort: SQLite blijft leidend, Supabase-fouten worden gelogd maar
laten de robot niet crashen.

Env-var KENSA_STORAGE bepaalt gedrag:
  'sqlite' → default, doet niks. 'dual' → SQLite + Supabase.

Log-file: supabase_sync.log
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_LOG_PATH = Path(__file__).resolve().parent / "supabase_sync.log"
_SECRETS_PATH = Path("/home/pi/.openclaw/secrets.json")
_CACHED = {"url": None, "key": None, "loaded": False}

# Fix #1: split connect/read timeout. TLS-handshake stalls tegen 172.64.149.246
# duren typisch <5s; oude 6-8s ceiling killde ze onnodig. Nu 5s connect (fail-fast
# als TCP niet opkomt) + 25s read (ruimte voor lange server-side query of
# TLS-hikje). Zie RCA-SUPABASE-TIMEOUTS.md §9 fix #1.
TIMEOUT: tuple[float, float] = (5.0, 25.0)

# Fix #2: gedeelde requests.Session met connection-pool + auto-retry op
# transient 5xx en connect-errors. 1 TCP+TLS handshake ipv N per worker-run,
# waardoor de "172.64.149.246 flaky edge"-loterij minder tickets krijgt.
# `allowed_methods` mag POST bevatten omdat alle write-endpoints
# `Prefer: resolution=merge-duplicates` of `ignore-duplicates` gebruiken →
# retries zijn idempotent. Zie RCA §9 fix #2.
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


def _load_secrets():
    if _CACHED["loaded"]:
        return
    s = json.loads(_SECRETS_PATH.read_text())["supabase"]
    _CACHED["url"] = s["url"]
    _CACHED["key"] = s["service_role_key"]
    _CACHED["loaded"] = True


def _headers(prefer: str = "return=minimal,resolution=merge-duplicates"):
    _load_secrets()
    return {
        "apikey": _CACHED["key"],
        "Authorization": f"Bearer {_CACHED['key']}",
        "Content-Type": "application/json",
        "Content-Profile": "kensa",
        "Accept-Profile": "kensa",
        "Prefer": prefer,
    }


def _log(msg: str) -> None:
    try:
        with _LOG_PATH.open("a") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")
    except Exception:
        pass


def mode() -> str:
    return (os.environ.get("KENSA_STORAGE") or "sqlite").strip().lower()


def enabled() -> bool:
    return mode() in ("dual", "supabase")


def _do_post_http(path: str, rows, prefer: str | None, timeout=TIMEOUT,
                  params: dict | None = None) -> tuple[bool, str]:
    """Pure HTTP POST — geen logging, geen retry op call-level (session-adapter
    doet transient retries). Return (ok, error_str).

    Wordt zowel door `_post()` (live call) als door de retry-drain aangeroepen.
    Optioneel `params` voor bv. on_conflict=... query-param.
    """
    try:
        _load_secrets()
    except Exception as e:
        return (False, f"secrets-fail: {e!r}")
    try:
        r = _session().post(
            f"{_CACHED['url']}/rest/v1/{path}",
            headers=_headers(prefer or "return=minimal,resolution=merge-duplicates"),
            json=rows if isinstance(rows, list) else [rows],
            params=params,
            timeout=timeout,
        )
        if r.status_code < 300:
            return (True, "")
        return (False, f"HTTP {r.status_code}: {r.text[:300]}")
    except Exception as e:
        return (False, f"{type(e).__name__}: {e}")


def _do_patch_http(path: str, query: dict, patch: dict, timeout=TIMEOUT) -> tuple[bool, str]:
    """Pure HTTP PATCH — geen logging, geen retry. Return (ok, error_str)."""
    try:
        _load_secrets()
    except Exception as e:
        return (False, f"secrets-fail: {e!r}")
    try:
        r = _session().patch(
            f"{_CACHED['url']}/rest/v1/{path}",
            headers=_headers("return=minimal"),
            params=query,
            json=patch,
            timeout=timeout,
        )
        if r.status_code < 300:
            return (True, "")
        return (False, f"HTTP {r.status_code}: {r.text[:300]}")
    except Exception as e:
        return (False, f"{type(e).__name__}: {e}")


def _do_delete_http(path: str, query: dict, timeout=TIMEOUT) -> tuple[bool, str]:
    """Pure HTTP DELETE — geen logging, geen retry. Return (ok, error_str).

    Wordt zowel door `_delete()` (live call) als door de retry-drain aangeroepen.
    """
    try:
        _load_secrets()
    except Exception as e:
        return (False, f"secrets-fail: {e!r}")
    try:
        r = _session().delete(
            f"{_CACHED['url']}/rest/v1/{path}",
            headers=_headers("return=minimal"),
            params=query,
            timeout=timeout,
        )
        if r.status_code < 300:
            return (True, "")
        return (False, f"HTTP {r.status_code}: {r.text[:300]}")
    except Exception as e:
        return (False, f"{type(e).__name__}: {e}")


def _enqueue_post_retry(path: str, rows, prefer: str | None, error: str) -> None:
    """Park een gefaalde POST in sync_retry. Lazy import om circular te vermijden."""
    try:
        import sync_retry
        sync_retry.ensure_table()
        rid = sync_retry.enqueue_post(path, rows, prefer, error)
        _log(f"enqueued POST {path} as retry #{rid} ({error[:120]})")
    except Exception as e:
        _log(f"enqueue-post-fail {path} {e!r}")


def _enqueue_patch_retry(path: str, query: dict, patch: dict, error: str) -> None:
    """Park een gefaalde PATCH in sync_retry."""
    try:
        import sync_retry
        sync_retry.ensure_table()
        rid = sync_retry.enqueue_patch(path, query, patch, error)
        _log(f"enqueued PATCH {path} as retry #{rid} ({error[:120]})")
    except Exception as e:
        _log(f"enqueue-patch-fail {path} {e!r}")


def _enqueue_delete_retry(path: str, query: dict, error: str) -> None:
    """Park een gefaalde DELETE in sync_retry."""
    try:
        import sync_retry
        sync_retry.ensure_table()
        rid = sync_retry.enqueue_delete(path, query, error)
        _log(f"enqueued DELETE {path} as retry #{rid} ({error[:120]})")
    except Exception as e:
        _log(f"enqueue-delete-fail {path} {e!r}")


def _post(path: str, rows, prefer: str = None, timeout=TIMEOUT,
          params: dict | None = None) -> bool:
    if not rows:
        return True
    ok, err = _do_post_http(path, rows, prefer, timeout, params=params)
    if ok:
        return True
    _log(f"post-fail {path} {err}")
    _enqueue_post_retry(path, rows, prefer, err)
    return False


def _patch(path: str, query: dict, patch: dict, timeout=TIMEOUT) -> bool:
    ok, err = _do_patch_http(path, query, patch, timeout)
    if ok:
        return True
    _log(f"patch-fail {path} {err}")
    _enqueue_patch_retry(path, query, patch, err)
    return False


def _delete(path: str, query: dict, timeout=TIMEOUT) -> bool:
    ok, err = _do_delete_http(path, query, timeout)
    if ok:
        return True
    _log(f"delete-fail {path} {err}")
    _enqueue_delete_retry(path, query, err)
    return False


def _maybe_json(v):
    if v is None or v == "":
        return None
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return None
    return v


# ---------------------------------------------------------------------------
# Publieke API — wordt door storage.py + analyze.py + webapp.py aangeroepen.
# ---------------------------------------------------------------------------

def sync_upsert_listing(row: dict, first_seen_at: str, last_seen_at: str) -> None:
    if not enabled():
        return
    full = {
        "item_id": row.get("item_id"),
        "title_jp": row.get("title_jp"),
        "title_en": row.get("title_en"),
        "description_jp": row.get("description_jp"),
        "price_jpy": row.get("price_jpy"),
        "price_eur": row.get("price_eur"),
        "shipping_jpy": row.get("shipping_jpy"),
        "seller_id": row.get("seller_id"),
        "seller_name": row.get("seller_name"),
        "seller_rating": row.get("seller_rating"),
        "category_path": row.get("category_path"),
        "condition": row.get("condition"),
        "authenticated": bool(row["authenticated"]) if row.get("authenticated") is not None else None,
        "sold": bool(row["sold"]) if row.get("sold") is not None else None,
        "detail_url": row.get("detail_url"),
        "status": row.get("status"),
        "extra_json": row.get("extra_json") if isinstance(row.get("extra_json"), (dict, list)) else _maybe_json(row.get("extra_json")),
        "first_seen_at": first_seen_at,
        "last_seen_at": last_seen_at,
        "detail_scraped_at": row.get("detail_scraped_at"),
    }
    # SQLite gebruikt COALESCE(:col, col) — NULL laat oude waarde staan.
    # PostgREST merge-duplicates OVERSCHRIJFT met NULL. Strip NULLs zodat
    # een search-only payload (die alleen item_id+last_seen kent) niet
    # bestaande title/price/detail_scraped_at leegveegt.
    payload = {k: v for k, v in full.items() if v is not None}
    payload["item_id"] = full["item_id"]
    payload["last_seen_at"] = last_seen_at
    _post("listings", payload)


def sync_photo_urls(item_id: str, url_index_pairs) -> None:
    if not enabled() or not url_index_pairs:
        return
    rows = [
        {"item_id": item_id, "photo_index": idx, "url_original": url}
        for url, idx in url_index_pairs
    ]
    _post("photos", rows, prefer="return=minimal,resolution=ignore-duplicates")


def sync_photo_metadata(item_id: str, photo_index: int, file_hash: str,
                        file_path: str, size_bytes: int, width, height,
                        downloaded_at: str) -> None:
    if not enabled():
        return
    _patch(
        "photos",
        {"item_id": f"eq.{item_id}", "photo_index": f"eq.{photo_index}"},
        {"file_hash": file_hash, "file_path": file_path, "downloaded_at": downloaded_at,
         "size_bytes": size_bytes, "width": width, "height": height},
    )


def sync_mark_seen(item_ids, last_seen_at: str) -> None:
    if not enabled() or not item_ids:
        return
    ids = list(item_ids)
    for i in range(0, len(ids), 200):
        chunk = ids[i : i + 200]
        quoted = ",".join(f'"{x}"' for x in chunk)
        _patch("listings", {"item_id": f"in.({quoted})"}, {"last_seen_at": last_seen_at})


def sync_cert_sighting(cert: str, item_id: str, seller_hint, seen_at: str) -> None:
    if not enabled():
        return
    _post(
        "cert_sightings",
        {"cert": cert, "item_id": item_id, "seller_hint": seller_hint, "seen_at": seen_at},
        prefer="return=minimal,resolution=ignore-duplicates",
    )


def sync_replace_analysis(item_id: str, trap: str, result: dict,
                          confidence, card_id, created_at: str) -> None:
    if not enabled():
        return
    try:
        _load_secrets()
    except Exception:
        return
    try:
        _session().delete(
            f"{_CACHED['url']}/rest/v1/analysis",
            headers=_headers("return=minimal"),
            params={"item_id": f"eq.{item_id}", "trap": f"eq.{trap}"},
            timeout=TIMEOUT,
        )
    except Exception as e:
        _log(f"analysis-delete-exc {item_id} {trap} {e!r}")
    # Fix #3: on_conflict=item_id,trap,created_at + merge-duplicates.
    # Als de eerste POST timeoutte MAAR de row werd wel geschreven, komt de
    # retry-drain hier terug met EXACT dezelfde (item_id, trap, created_at)
    # combinatie. Zonder on_conflict-target valt PostgREST terug op de
    # primary-key (analysis_id serial) — die matcht nooit → unique-violation
    # 409 op kensa_analysis_uniq → dead-letter. Met deze on_conflict wordt
    # het een echte upsert die de row met identieke inhoud overschrijft:
    # idempotent en veilig als retry.
    _post(
        "analysis",
        {"item_id": item_id, "trap": trap,
         "result_json": result if isinstance(result, (dict, list)) else _maybe_json(result),
         "confidence": confidence, "card_id": card_id, "created_at": created_at},
        prefer="return=minimal,resolution=merge-duplicates",
        params={"on_conflict": "item_id,trap,created_at"},
    )


def sync_slab_status(item_id: str, status: str, card_key) -> None:
    if not enabled():
        return
    patch = {"slab_status": status}
    if card_key is not None:
        patch["card_key"] = card_key
    _patch("listings", {"item_id": f"eq.{item_id}"}, patch)


def sync_price_cache_ebay(card_key: str, query: str, result: dict, fetched_at: str) -> None:
    if not enabled():
        return
    _post(
        "price_cache",
        {"card_key": card_key, "ebay_query": query,
         "ebay_result_json": result if isinstance(result, (dict, list)) else _maybe_json(result),
         "ebay_fetched_at": fetched_at},
        prefer="return=minimal,resolution=merge-duplicates",
    )


def sync_price_cache_cm(card_key: str, cm_url: str, listings: list, fetched_at: str) -> None:
    if not enabled():
        return
    _post(
        "price_cache",
        {"card_key": card_key, "cm_url": cm_url,
         "cm_listings_json": listings if isinstance(listings, (dict, list)) else _maybe_json(listings),
         "cm_fetched_at": fetched_at},
        prefer="return=minimal,resolution=merge-duplicates",
    )


def sync_cm_queue_upsert(item_id: str, url: str, grade, queued_at: str,
                        fetched_at=None, listings_json=None, card_key=None) -> None:
    if not enabled():
        return
    payload = {
        "item_id": item_id, "url": url, "grade": grade, "queued_at": queued_at,
        "fetched_at": fetched_at,
        "listings_json": listings_json if isinstance(listings_json, (dict, list)) else _maybe_json(listings_json),
        "card_key": card_key,
    }
    _post("cardmarket_queue", payload, prefer="return=minimal,resolution=merge-duplicates")


def sync_cm_queue_result(item_id: str, url: str, fetched_at: str,
                        listings_json, error) -> None:
    if not enabled():
        return
    # Upsert i.p.v. PATCH: als de rij niet bestaat (bv. omdat de enqueue-sync
    # nooit heeft gelopen), zou een PATCH stille no-op zijn en het resultaat
    # gaat verloren. Merge-duplicates behoudt kolommen die niet in payload zitten.
    _post(
        "cardmarket_queue",
        {"item_id": item_id, "url": url,
         "fetched_at": fetched_at,
         "listings_json": listings_json if isinstance(listings_json, (dict, list)) else _maybe_json(listings_json),
         "error": error},
        prefer="return=minimal,resolution=merge-duplicates",
    )
