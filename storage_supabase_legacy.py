"""Kensa — Supabase-native read helpers.

Doel: workers laten lezen uit Supabase in plaats van de lokale SQLite.
Schrijven gaat al via supabase_sync.py (dual-write). Deze module is de eerste
stap richting Supabase-first.

Interface bewust identiek aan de SQLite-varianten in analyze.py en check_slab.py
zodat we call-sites 1:1 kunnen swappen zonder verdere refactor.
"""
from __future__ import annotations

import json
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_SECRETS_PATH = Path("/home/pi/.openclaw/secrets.json")
_CACHED = {"url": None, "key": None, "loaded": False}

# RCA fix #1+#2: gedeelde (connect=5, read=25) timeout + Session/pool.
# Vervangt de per-functie 6-8s float default (te kort voor de p99 TLS-
# handshake-stalls tegen Cloudflare edge, kilde herstellende calls onnodig).
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
            allowed_methods=frozenset(["GET", "HEAD"]),
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


def _headers():
    _load_secrets()
    return {
        "apikey": _CACHED["key"],
        "Authorization": f"Bearer {_CACHED['key']}",
        "Content-Profile": "kensa",
        "Accept-Profile": "kensa",
    }


def load_listing_supabase(item_id: str, timeout=TIMEOUT) -> dict | None:
    """Supabase-equivalent van analyze._load_listing()."""
    _load_secrets()
    r = _session().get(
        f"{_CACHED['url']}/rest/v1/listings",
        headers=_headers(),
        params={"item_id": f"eq.{item_id}", "select": "*", "limit": "1"},
        timeout=timeout,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"load_listing {item_id}: HTTP {r.status_code} {r.text[:200]}")
    rows = r.json()
    return rows[0] if rows else None


def load_photo_urls_supabase(item_id: str, timeout=TIMEOUT) -> list[tuple[int, str]]:
    """Supabase-equivalent van check_slab._load_photo_urls()."""
    _load_secrets()
    r = _session().get(
        f"{_CACHED['url']}/rest/v1/photos",
        headers=_headers(),
        params={
            "item_id": f"eq.{item_id}",
            "select": "photo_index,url_original",
            "order": "photo_index.asc",
        },
        timeout=timeout,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"load_photos {item_id}: HTTP {r.status_code} {r.text[:200]}")
    rows = r.json()
    return [(row["photo_index"], row["url_original"]) for row in rows]


def load_analysis_trap_latest_supabase(item_id: str, trap: str, timeout=TIMEOUT) -> dict | None:
    """Supabase-equivalent van score_only._load_stored_slab_llm() per trap.

    SQLite: SELECT result_json FROM analysis WHERE item_id=? AND trap=?
            ORDER BY analysis_id DESC LIMIT 1
    """
    _load_secrets()
    r = _session().get(
        f"{_CACHED['url']}/rest/v1/analysis",
        headers=_headers(),
        params={
            "item_id": f"eq.{item_id}",
            "trap": f"eq.{trap}",
            "select": "result_json",
            "order": "analysis_id.desc",
            "limit": "1",
        },
        timeout=timeout,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"load_analysis_trap {item_id}/{trap}: HTTP {r.status_code} {r.text[:200]}")
    rows = r.json()
    return rows[0] if rows else None


def load_price_cache_by_key_supabase(card_key: str, timeout=TIMEOUT) -> dict | None:
    """Supabase-equivalent van ebay_phase._price_cache_get()."""
    _load_secrets()
    r = _session().get(
        f"{_CACHED['url']}/rest/v1/price_cache",
        headers=_headers(),
        params={"card_key": f"eq.{card_key}", "select": "*", "limit": "1"},
        timeout=timeout,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"load_price_cache {card_key}: HTTP {r.status_code} {r.text[:200]}")
    rows = r.json()
    return rows[0] if rows else None


def load_analysis_by_trap_since_supabase(trap: str, cutoff_iso: str,
                                          limit: int = 20, timeout=TIMEOUT) -> list[dict]:
    """Supabase-equivalent van ebay_phase._cached_ebay_for_query() (zonder query-filter, dat doet caller).

    SQLite: SELECT result_json FROM analysis
            WHERE trap = ? AND created_at >= ?
            ORDER BY analysis_id DESC LIMIT ?
    """
    _load_secrets()
    r = _session().get(
        f"{_CACHED['url']}/rest/v1/analysis",
        headers=_headers(),
        params={
            "trap": f"eq.{trap}",
            "created_at": f"gte.{cutoff_iso}",
            "select": "result_json",
            "order": "analysis_id.desc",
            "limit": str(limit),
        },
        timeout=timeout,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"load_analysis_since {trap}: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


def pick_cache_batch_supabase(limit: int, cache_days: int, timeout=TIMEOUT) -> list[str]:
    """Supabase-equivalent van worker_cache.pick_batch().

    SQLite: JOIN listings + price_cache WHERE slab_status='ocr_done'
            AND card_key IS NOT NULL AND ebay data fresh.

    STRATEGIE (herzien 2026-09-05):
      Eerst listings ophalen (kleine pool: ~100 items met slab_status=ocr_done),
      dan per unieke card_key checken of price_cache verse eBay-data heeft.
      Dit vermijdt Supabase's default 1000-row cap op de price_cache-query
      (die 1958+ fresh keys terug zou moeten geven maar er slechts 1000 leverde).
    """
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=cache_days)).isoformat()
    _load_secrets()

    # Stap 1: haal candidate listings — slab_status='ocr_done' + card_key aanwezig
    # (relatief kleine pool, meestal < 500 items). Ruim genoeg vragen om filter-shrinkage.
    fetch_limit = max(limit * 3, 300)
    r = _session().get(
        f"{_CACHED['url']}/rest/v1/listings",
        headers=_headers(),
        params={
            "select": "item_id,card_key,first_seen_at",
            "slab_status": "eq.ocr_done",
            "card_key": "not.is.null",
            "order": "first_seen_at.desc",
            "limit": str(fetch_limit),
        },
        timeout=timeout,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"pick_cache stap1: HTTP {r.status_code} {r.text[:200]}")
    candidates = r.json()
    if not candidates:
        return []

    # Stap 2: unieke card_keys → check price_cache voor verse ebay_fetched_at
    # (chunked + quoted om special chars in keys veilig te encoderen).
    unique_keys = list({c["card_key"] for c in candidates if c.get("card_key")})
    fresh_keys: set[str] = set()
    chunk_size = 50
    for i in range(0, len(unique_keys), chunk_size):
        chunk = unique_keys[i:i + chunk_size]
        quoted = ",".join('"' + k.replace('"', '""') + '"' for k in chunk)
        r = _session().get(
            f"{_CACHED['url']}/rest/v1/price_cache",
            headers=_headers(),
            params={
                "select": "card_key",
                "card_key": f"in.({quoted})",
                "ebay_fetched_at": f"gte.{cutoff}",
                "ebay_result_json": "not.is.null",
                "limit": str(len(chunk)),
            },
            timeout=timeout,
        )
        if r.status_code >= 300:
            raise RuntimeError(f"pick_cache stap2: HTTP {r.status_code} {r.text[:200]}")
        for row in r.json():
            if row.get("card_key"):
                fresh_keys.add(row["card_key"])

    # Stap 3: filter kandidaten op fresh_keys, behoud oorspronkelijke DESC-order
    result = [c["item_id"] for c in candidates if c.get("card_key") in fresh_keys]
    return result[:limit]


def pick_ebay_batch_supabase(limit: int, cache_days: int, timeout=TIMEOUT) -> list[str]:
    """Supabase-equivalent van worker_claim.claim_ebay_batch() — READ-only.

    Selecteert items met slab_status='ocr_done' waar GEEN verse ebay-cache is
    (card_key mist óf price_cache-rij ontbreekt/verlopen).

    LOCKING (locked_by/locked_at) blijft in Pi SQLite — deze functie doet
    alleen de SELECT-fase. Worker roept daarna claim_ebay_batch(Pi-lock)
    aan op basis van deze item_ids.

    Strategie omgedraaid net als pick_cache: eerst listings (ocr_done, klein),
    dan check per card_key of price_cache VERS is. Alles zonder verse cache
    komt in de return.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=cache_days)).isoformat()
    _load_secrets()
    # Stap 1: kandidaten — slab_status='ocr_done'
    fetch_limit = max(limit * 5, 500)
    r = _session().get(
        f"{_CACHED['url']}/rest/v1/listings",
        headers=_headers(),
        params={
            "select": "item_id,card_key,first_seen_at",
            "slab_status": "eq.ocr_done",
            "order": "first_seen_at.desc",
            "limit": str(fetch_limit),
        },
        timeout=timeout,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"pick_ebay stap1: HTTP {r.status_code} {r.text[:200]}")
    candidates = r.json()
    if not candidates:
        return []
    # Stap 2: check welke card_keys VERSE ebay-cache hebben
    unique_keys = [c["card_key"] for c in candidates if c.get("card_key")]
    fresh_keys: set[str] = set()
    chunk_size = 50
    for i in range(0, len(unique_keys), chunk_size):
        chunk = unique_keys[i:i + chunk_size]
        quoted = ",".join('"' + k.replace('"', '""') + '"' for k in chunk)
        r = _session().get(
            f"{_CACHED['url']}/rest/v1/price_cache",
            headers=_headers(),
            params={
                "select": "card_key",
                "card_key": f"in.({quoted})",
                "ebay_fetched_at": f"gte.{cutoff}",
                "ebay_result_json": "not.is.null",
                "limit": str(len(chunk)),
            },
            timeout=timeout,
        )
        if r.status_code >= 300:
            raise RuntimeError(f"pick_ebay stap2: HTTP {r.status_code} {r.text[:200]}")
        for row in r.json():
            if row.get("card_key"):
                fresh_keys.add(row["card_key"])
    # Stap 3: filter — items ZONDER card_key OR met card_key niet in fresh_keys
    result = [
        c["item_id"] for c in candidates
        if not c.get("card_key") or c["card_key"] not in fresh_keys
    ]
    return result[:limit]


def pick_detail_batch_supabase(limit: int | None = None, timeout=TIMEOUT) -> list[tuple[str, str | None]]:
    """Supabase-equivalent van fetch_detail._pending_items().

    SQLite: SELECT item_id, detail_url FROM listings
            WHERE detail_scraped_at IS NULL
              AND (detail_url IS NULL OR detail_url NOT LIKE '%/mercari/%')
            ORDER BY first_seen_at DESC [LIMIT ?]

    Mercari-items (buyee.jp/mercari/...) uitgesloten want die worden door
    fetch_detail_mercapi.py opgepakt (voorkomt race + quota-verspilling).
    """
    _load_secrets()
    params = {
        "select": "item_id,detail_url",
        "detail_scraped_at": "is.null",
        "or": "(detail_url.is.null,detail_url.not.like.*/mercari/*)",
        "order": "first_seen_at.desc",
    }
    if limit:
        params["limit"] = str(int(limit))
    r = _session().get(
        f"{_CACHED['url']}/rest/v1/listings",
        headers=_headers(),
        params=params,
        timeout=timeout,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"pick_detail_batch: HTTP {r.status_code} {r.text[:200]}")
    return [(row["item_id"], row.get("detail_url")) for row in r.json()]


def pick_mercapi_batch_supabase(limit: int | None = None, timeout=TIMEOUT) -> list[str]:
    """Supabase-equivalent van fetch_detail_mercapi._pending_mercari_ids().

    SQLite: SELECT item_id FROM listings
            WHERE detail_scraped_at IS NULL AND detail_url LIKE '%/mercari/%'
            ORDER BY first_seen_at DESC [LIMIT ?]
    """
    _load_secrets()
    params = {
        "select": "item_id",
        "detail_scraped_at": "is.null",
        "detail_url": "like.*/mercari/*",
        "order": "first_seen_at.desc",
    }
    if limit:
        params["limit"] = str(int(limit))
    r = _session().get(
        f"{_CACHED['url']}/rest/v1/listings",
        headers=_headers(),
        params=params,
        timeout=timeout,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"pick_mercapi_batch: HTTP {r.status_code} {r.text[:200]}")
    return [row["item_id"] for row in r.json()]


def pick_mercapi_recheck_batch_supabase(limit: int = 100, min_age_hours: int = 4, timeout=TIMEOUT) -> list[str]:
    """Supabase-equivalent van fetch_detail_mercapi._deal_recheck_ids().

    Mercari-items die als deal zichtbaar zijn (roi>0, niet onbetrouwbaar), nog niet sold,
    en langer dan min_age_hours niet gezien. Voor sold-status refresh.

    SQLite JOIN met json_extract → Supabase 2-fase (kan geen server-side JOIN + JSON filter):
      Stap 1: listings-kandidaten (mercari, niet sold, last_seen_at oud genoeg)
      Stap 2: laatste 'summary' trap per item, filter op roi_avg3_pct in-range en verdict!='onbetrouwbaar'
    """
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=int(min_age_hours))).isoformat()
    _load_secrets()

    # Stap 1: mercari-items niet sold, last_seen oud genoeg. Vraag ruime pool (5×limit) om
    # filter-shrinkage van stap 2 te absorberen.
    fetch_limit = max(int(limit) * 5, 500)
    r = _session().get(
        f"{_CACHED['url']}/rest/v1/listings",
        headers=_headers(),
        params={
            "select": "item_id,last_seen_at",
            "detail_url": "like.*/mercari/*",
            "or": "(sold.is.null,sold.eq.false)",
            "last_seen_at": f"lt.{cutoff}",
            "order": "last_seen_at.asc",
            "limit": str(fetch_limit),
        },
        timeout=timeout,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"pick_mercapi_recheck stap1: HTTP {r.status_code} {r.text[:200]}")
    candidates = r.json()
    if not candidates:
        return []

    # Stap 2: haal per chunk de laatste 'summary'-trap op en filter op JSON-velden client-side.
    order_index = {c["item_id"]: i for i, c in enumerate(candidates)}
    kept: list[tuple[int, str]] = []
    chunk_size = 50
    ids = [c["item_id"] for c in candidates]
    for i in range(0, len(ids), chunk_size):
        chunk = ids[i:i + chunk_size]
        quoted = ",".join('"' + iid.replace('"', '""') + '"' for iid in chunk)
        r2 = _session().get(
            f"{_CACHED['url']}/rest/v1/analysis",
            headers=_headers(),
            params={
                "select": "item_id,result_json",
                "trap": "eq.summary",
                "item_id": f"in.({quoted})",
                "limit": str(len(chunk)),
            },
            timeout=timeout,
        )
        if r2.status_code >= 300:
            raise RuntimeError(f"pick_mercapi_recheck stap2: HTTP {r2.status_code} {r2.text[:200]}")
        for row in r2.json():
            rj = row.get("result_json") or {}
            if isinstance(rj, str):
                try:
                    rj = json.loads(rj)
                except Exception:
                    continue
            roi = rj.get("roi_avg3_pct")
            verdict = rj.get("verdict")
            if roi is None or verdict == "onbetrouwbaar":
                continue
            try:
                roi_f = float(roi)
            except (TypeError, ValueError):
                continue
            if roi_f <= 0 or roi_f > 100:
                continue
            iid = row["item_id"]
            kept.append((order_index[iid], iid))
        if len(kept) >= int(limit):
            break

    # Sorteer op oorspronkelijke ORDER BY last_seen_at ASC (stap 1's volgorde)
    kept.sort(key=lambda t: t[0])
    return [iid for _, iid in kept[:int(limit)]]


def load_detail_status_supabase(item_id: str, timeout=TIMEOUT) -> str | None:
    """Supabase-equivalent van SELECT detail_scraped_at FROM listings WHERE item_id=?.
    Retourneert de timestamp-string of None (item niet gevonden of nog pending)."""
    _load_secrets()
    r = _session().get(
        f"{_CACHED['url']}/rest/v1/listings",
        headers=_headers(),
        params={"item_id": f"eq.{item_id}", "select": "detail_scraped_at", "limit": "1"},
        timeout=timeout,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"load_detail_status: HTTP {r.status_code} {r.text[:200]}")
    rows = r.json()
    if not rows:
        return None
    return rows[0].get("detail_scraped_at")


def pick_detail_proxy_batch_supabase(limit: int, timeout=TIMEOUT) -> list[str]:
    """Supabase-equivalent van fetch_detail_proxy._pending_items().

    SQLite: SELECT item_id FROM listings
            WHERE detail_scraped_at IS NULL
            ORDER BY first_seen_at DESC LIMIT ?
    """
    _load_secrets()
    r = _session().get(
        f"{_CACHED['url']}/rest/v1/listings",
        headers=_headers(),
        params={
            "select": "item_id",
            "detail_scraped_at": "is.null",
            "order": "first_seen_at.desc",
            "limit": str(int(limit)),
        },
        timeout=timeout,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"pick_detail_proxy_batch: HTTP {r.status_code} {r.text[:200]}")
    return [row["item_id"] for row in r.json()]


def pick_ocr_batch_supabase(limit: int, timeout=TIMEOUT) -> list[str]:
    """Supabase-equivalent van worker_ocr.pick_batch().

    SQLite: SELECT item_id FROM listings
            WHERE detail_scraped_at IS NOT NULL
              AND (slab_status='pending' OR slab_status IS NULL)
            ORDER BY first_seen_at DESC LIMIT ?
    """
    _load_secrets()
    r = _session().get(
        f"{_CACHED['url']}/rest/v1/listings",
        headers=_headers(),
        params={
            "select": "item_id",
            "detail_scraped_at": "not.is.null",
            "or": "(slab_status.eq.pending,slab_status.is.null)",
            "order": "first_seen_at.desc",
            "limit": str(limit),
        },
        timeout=timeout,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"pick_ocr_batch: HTTP {r.status_code} {r.text[:200]}")
    return [row["item_id"] for row in r.json()]
