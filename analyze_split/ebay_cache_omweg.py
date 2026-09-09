"""eBay-notitieboek (price_cache): twee extra ingangen als de exacte sleutel niet raakt.

Tommy 9-9 20:25: "Implementeren". Bewijs vooraf (dev/ebay_cache_test.py, avonddata
16:45-20:05): 181 van 343 eBay-opzoekingen (53%) gingen live terwijl er een vers
antwoord in price_cache lag onder een ANDERE spelling van dezelfde kaart
('gengar:094:10:sv2a' vs 'gengar:094/165:10'). In de Gemini-tijd was dat al 29%.

Ingangen, in deze volgorde:
  1. via de Cardmarket-URL van de kaart (uit cardmarket_queue) — zeker dezelfde kaart
  2. via de kern pokemon:nummer:grade — alleen als de set van beide sleutels niet botst

Geeft de gevonden price_cache-rij terug (jsonb als string, zoals _price_cache_get).
Kill-switch: KENSA_EBAY_CACHE_OMWEG=0 (default aan). Read-only op de tabellen.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from supabase_client import _SET_ALIAS, _norm_set  # noqa: E402

DB_PATH = Path(__file__).resolve().parent.parent / "kensa.db"
DAGEN = 3            # zelfde venster als PRICE_CACHE_DAYS in ebay_phase
SELECT = "card_key,ebay_query,ebay_result_json,ebay_fetched_at,cm_url"


def aan() -> bool:
    return os.environ.get("KENSA_EBAY_CACHE_OMWEG", "1").strip() != "0"


def _supabase() -> bool:
    return os.environ.get("KENSA_READ_STORAGE", "").lower() == "supabase"


def _cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=DAGEN)).isoformat()


# ------------------------------------------------------------------ sleutel-logica
def kern(card_key: str | None) -> str | None:
    """'gengar:094/165:10:sv2a' → 'gengar:94:10' (nummer vóór de slash, zonder voorloopnullen)."""
    if not card_key:
        return None
    d = card_key.split(":")
    if len(d) < 3 or not d[0] or not d[2]:
        return None
    nr = d[1].split("/")[0].lstrip("#").lstrip("0") or "0"
    return f"{d[0]}:{nr}:{d[2]}"


def set_van(card_key: str | None) -> str | None:
    d = (card_key or "").split(":")
    return d[3] if len(d) > 3 and d[3] else None


def sets_botsen(a: str | None, b: str | None) -> bool:
    """Alleen botsing als BEIDE een set hebben en die na normalisatie/alias verschillen."""
    if not a or not b:
        return False
    na, nb = _norm_set(a), _norm_set(b)
    na, nb = _SET_ALIAS.get(na, na), _SET_ALIAS.get(nb, nb)
    return na != nb


def _nummer_varianten(card_key: str) -> list[str]:
    raw = card_key.split(":")[1].split("/")[0].lstrip("#")
    kaal = raw.lstrip("0") or "0"
    out = []
    for v in (raw, kaal, kaal.zfill(3)):
        if v and v not in out:
            out.append(v)
    return out


def _rij(row: dict) -> dict:
    out = dict(row)
    if out.get("ebay_result_json") is not None and not isinstance(out["ebay_result_json"], str):
        out["ebay_result_json"] = json.dumps(out["ebay_result_json"], ensure_ascii=False)
    return out


def _nieuwste(rows: list[dict]) -> dict | None:
    rows = [r for r in rows if r.get("ebay_fetched_at") and r.get("ebay_result_json")]
    if not rows:
        return None
    return _rij(max(rows, key=lambda r: r["ebay_fetched_at"]))


# ------------------------------------------------------------------ data-toegang
def cm_url_van_item(item_id: str | None) -> str | None:
    if not item_id:
        return None
    if _supabase():
        from storage_supabase import _http
        rows = _http.get("cardmarket_queue", "kensa", [
            ("select", "url,queued_at"), ("item_id", f"eq.{item_id}"),
            ("order", "queued_at.desc"), ("limit", "1"),
        ])
        return rows[0].get("url") if rows else None
    conn = sqlite3.connect(str(DB_PATH))
    try:
        row = conn.execute("SELECT url FROM cardmarket_queue WHERE item_id=? ORDER BY queued_at DESC LIMIT 1",
                           (item_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _rijen_via_url(cm_url: str) -> list[dict]:
    if _supabase():
        from storage_supabase import _http
        return _http.get("price_cache", "kensa", [
            ("select", SELECT), ("cm_url", f"eq.{cm_url}"),
            ("ebay_fetched_at", f"gte.{_cutoff()}"),
            ("order", "ebay_fetched_at.desc"), ("limit", "5"),
        ])
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT card_key, ebay_query, ebay_result_json, ebay_fetched_at, cm_url FROM price_cache "
            "WHERE cm_url=? AND ebay_fetched_at >= ? ORDER BY ebay_fetched_at DESC LIMIT 5",
            (cm_url, _cutoff())).fetchall()]
    finally:
        conn.close()


def _rijen_via_prefix(pokemon: str, nr: str) -> list[dict]:
    if _supabase():
        from storage_supabase import _http
        return _http.get("price_cache", "kensa", [
            ("select", SELECT), ("card_key", f"like.{pokemon}:{nr}*"),
            ("ebay_fetched_at", f"gte.{_cutoff()}"),
            ("order", "ebay_fetched_at.desc"), ("limit", "20"),
        ])
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT card_key, ebay_query, ebay_result_json, ebay_fetched_at, cm_url FROM price_cache "
            "WHERE card_key LIKE ? AND ebay_fetched_at >= ? ORDER BY ebay_fetched_at DESC LIMIT 20",
            (f"{pokemon}:{nr}%", _cutoff())).fetchall()]
    finally:
        conn.close()


# ------------------------------------------------------------------ de omweg
def zoek_via_url(cm_url: str | None, eigen_key: str) -> dict | None:
    if not cm_url:
        return None
    return _nieuwste([r for r in _rijen_via_url(cm_url) if r.get("card_key") != eigen_key])


def zoek_via_kern(eigen_key: str) -> dict | None:
    k = kern(eigen_key)
    if not k:
        return None
    pokemon = eigen_key.split(":")[0]
    eigen_set = set_van(eigen_key)
    kandidaten: dict[str, dict] = {}
    for nr in _nummer_varianten(eigen_key):
        for r in _rijen_via_prefix(pokemon, nr):
            ck = r.get("card_key")
            if not ck or ck == eigen_key or kern(ck) != k or sets_botsen(eigen_set, set_van(ck)):
                continue
            kandidaten[ck] = r
    return _nieuwste(list(kandidaten.values()))


def zoek(eigen_key: str | None, item_id: str | None) -> tuple[dict | None, str | None]:
    """→ (price_cache-rij, bron) met bron 'price_cache_url' of 'price_cache_kern'; anders (None, None)."""
    if not eigen_key or not aan():
        return None, None
    try:
        rij = zoek_via_url(cm_url_van_item(item_id), eigen_key)
        if rij:
            return rij, "price_cache_url"
        rij = zoek_via_kern(eigen_key)
        if rij:
            return rij, "price_cache_kern"
    except Exception as e:                     # de omweg mag de eBay-fase nooit breken
        print(f"  [ebay] cache-omweg overgeslagen: {type(e).__name__}: {e}", file=sys.stderr)
    return None, None
