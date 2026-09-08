#!/usr/bin/env python3
"""
Kensa CM-queue API — standalone, geen dashboard.

Levert de 3 endpoints die de externe Windows CM-scraper nodig heeft:
  GET  /api/cm/pending   → lijst URLs die gescraped moeten
  POST /api/cm/result    → scrape-resultaat wegschrijven
  POST /api/cm/enqueue   → handmatig URL bijzetten (test)

Draait op poort 8898 (was 8899 in oude kensa-webapp).
Losgekoppeld van de Flask-dashboard — dashboard kan permanent uit.

Storage-schakelaar (env KENSA_STORAGE, gezet in de systemd-unit):
  supabase  → #6 stap 2 (2026-09-08): wachtrij lezen én resultaten schrijven
              ALLEEN in Supabase. De Pi-SQLite wordt niet meer aangeraakt.
              Writes lopen via storage_supabase._http (tijdelijke fouten ->
              sync_retry-vangnet, drainer speelt af).
  dual      → #6 stap 1 (rollback-pad): Pi SQLite is de bron voor /pending,
              resultaten naar Pi + robuuste dual-write naar Supabase.
De Windows-worker praat in beide standen onveranderd met deze Pi-API; de
JSON-vorm van de endpoints is identiek.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request

DB_PATH = Path(os.environ.get("KENSA_DB", "/home/pi/.openclaw/workspace/agents/kensa/kensa.db"))
PORT = int(os.environ.get("KENSA_CM_API_PORT", "8898"))
STORAGE = os.environ.get("KENSA_STORAGE", "dual")  # "supabase" | "dual"
SB_SCHEMA = "kensa"
SB_TABLE = "cardmarket_queue"

app = Flask(__name__)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _supabase_only() -> bool:
    return STORAGE == "supabase"


def _log(msg: str) -> None:
    print(f"[cm-api] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Supabase-pad (#6 stap 2)
# ---------------------------------------------------------------------------

def _sb_pending(limit: int) -> list[dict]:
    """Oudste pending rijen mét grade, zelfde vorm als het Pi-pad.

    `grade=neq.` sluit de lege string uit (equivalent van TRIM(grade)!='' op
    de Pi; grades worden bij enqueue altijd gestript). Lijst van tuples omdat
    `grade` twee filters krijgt.
    """
    from storage_supabase import _http
    rows = _http.get(SB_TABLE, SB_SCHEMA, [
        ("select", "item_id,url,grade"),
        ("fetched_at", "is.null"),
        ("grade", "not.is.null"),
        ("grade", "neq."),
        ("order", "queued_at.asc"),
        ("limit", str(limit)),
    ])
    return [{"item_id": r["item_id"], "url": r["url"], "grade": r["grade"]} for r in rows]


def _sb_pending_count() -> int:
    from storage_supabase import _http
    url, _ = _http._creds()
    headers = _http._headers(SB_SCHEMA, prefer="count=exact")
    headers["Range"] = "0-0"
    r = _http._session().get(f"{url}/rest/v1/{SB_TABLE}",
                             params={"select": "item_id", "fetched_at": "is.null"},
                             headers=headers, timeout=_http.TIMEOUT)
    r.raise_for_status()
    return int(r.headers["Content-Range"].split("/")[1])


def _sb_result(item_id: str, error: str | None, listings: list, now: str) -> None:
    """Resultaat alleen in Supabase: PATCH de queue-rij (met vangnet), daarna
    price_cache vullen met de card_key/url die de PATCH teruggeeft
    (Prefer: return=representation → geen extra rondje)."""
    from storage_supabase import _http
    r = _http.patch(SB_TABLE, SB_SCHEMA, {"item_id": f"eq.{item_id}"},
                    {"fetched_at": now, "listings_json": None if error else listings, "error": error})
    rows: list = []
    if r.status_code == 200:
        try:
            rows = r.json() or []
        except Exception:
            rows = []
    elif getattr(r, "queued_retry_id", None) is not None:
        _log(f"queue-result {item_id} tijdelijk mislukt → geparkeerd als sync_retry #{r.queued_retry_id}")
    elif r.status_code >= 400:
        _log(f"queue-result {item_id} BLIJVEND mislukt HTTP {r.status_code}: {r.text[:200]}")
    if error:
        return  # geen prijs → geen price_cache
    if not rows:
        # PATCH gaf geen rij terug (geparkeerd of onbekend item): card_key los ophalen.
        try:
            rows = _http.get(SB_TABLE, SB_SCHEMA,
                             {"item_id": f"eq.{item_id}", "select": "card_key,url"})
        except Exception as e:
            _log(f"card_key-lookup {item_id} mislukt, price_cache niet bijgewerkt: {e!r}")
            return
    if not rows:
        _log(f"queue-rij {item_id} niet gevonden in Supabase — price_cache niet bijgewerkt")
        return
    card_key = rows[0].get("card_key")
    cm_url = rows[0].get("url")
    if not card_key:
        return
    try:
        from storage_supabase.price_cache import upsert_cm
        upsert_cm(card_key, cm_url, listings, now)
    except Exception as e:
        _log(f"price_cache {card_key} BLIJVEND mislukt: {e!r}")


def _sb_enqueue(item_id: str, url: str, grade: str | None, card_key: str | None) -> None:
    from storage_supabase.cardmarket_queue import enqueue_fresh
    enqueue_fresh(item_id, url, grade, card_key)


# ---------------------------------------------------------------------------
# Pi-pad (dual, stap 1) — ongewijzigd, alleen nog actief bij KENSA_STORAGE=dual
# ---------------------------------------------------------------------------

def _pi_pending(limit: int) -> list[dict]:
    conn = _db()
    try:
        rows = conn.execute(
            """SELECT item_id, url, grade FROM cardmarket_queue
               WHERE fetched_at IS NULL
                 AND grade IS NOT NULL
                 AND TRIM(grade) != ''
               ORDER BY queued_at ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [{"item_id": r["item_id"], "url": r["url"], "grade": r["grade"]} for r in rows]
    finally:
        conn.close()


def _pi_result_dual(item_id: str, error: str | None, listings: list, now: str) -> None:
    listings_json = json.dumps(listings) if not error else None
    conn = _db()
    cm_url_for_sync = None
    card_key_for_sync = None
    try:
        conn.execute(
            "UPDATE cardmarket_queue SET fetched_at=?, listings_json=?, error=? WHERE item_id=?",
            (now, listings_json, error, item_id),
        )
        if not error and listings_json:
            row = conn.execute(
                "SELECT card_key, url FROM cardmarket_queue WHERE item_id=?",
                (item_id,),
            ).fetchone()
            if row and row["card_key"]:
                card_key_for_sync = row["card_key"]
                cm_url_for_sync = row["url"]
                conn.execute(
                    """INSERT INTO price_cache (card_key, cm_url, cm_listings_json, cm_fetched_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(card_key) DO UPDATE SET
                           cm_url=excluded.cm_url,
                           cm_listings_json=excluded.cm_listings_json,
                           cm_fetched_at=excluded.cm_fetched_at""",
                    (row["card_key"], row["url"], listings_json, now),
                )
        if cm_url_for_sync is None:
            r2 = conn.execute("SELECT url FROM cardmarket_queue WHERE item_id=?", (item_id,)).fetchone()
            if r2:
                cm_url_for_sync = r2["url"]
        conn.commit()
    finally:
        conn.close()

    # Dual-write naar Supabase via het robuuste _http-pad (stap 1).
    try:
        from storage_supabase import _http
        from storage_supabase.price_cache import upsert_cm as _sb_upsert_cm
        try:
            _http.patch(SB_TABLE, SB_SCHEMA, {"item_id": f"eq.{item_id}"},
                        {"fetched_at": now, "listings_json": None if error else listings, "error": error})
        except Exception as e:
            _log(f"supabase queue-result blijvend mislukt {item_id}: {e}")
        if card_key_for_sync and not error:
            try:
                _sb_upsert_cm(card_key_for_sync, cm_url_for_sync, listings, now)
            except Exception as e:
                _log(f"supabase price_cache blijvend mislukt {card_key_for_sync}: {e}")
    except ImportError:
        pass


def _pi_enqueue(item_id: str, url: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = _db()
    try:
        conn.execute(
            """INSERT INTO cardmarket_queue (item_id, url, queued_at)
               VALUES (?, ?, ?)
               ON CONFLICT(item_id) DO UPDATE SET url=excluded.url, queued_at=excluded.queued_at,
                                                   fetched_at=NULL, listings_json=NULL, error=NULL""",
            (item_id, url, now),
        )
        conn.commit()
    finally:
        conn.close()


def _pi_pending_count() -> int:
    conn = _db()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM cardmarket_queue WHERE fetched_at IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.route("/api/cm/pending", methods=["GET"])
def api_cm_pending():
    """Scraper polt hier: welke URLs moet ik nu ophalen?

    Alleen rijen met expliciete grade — nooit defaulten naar 10 (bug: PSA 9
    slabs zouden anders met PSA 10 CM-listings vergeleken worden).
    Bij een storing 503 (geen lege lijst): de worker wacht en pollt opnieuw.
    """
    limit = min(int(request.args.get("limit", 20)), 100)
    try:
        rows = _sb_pending(limit) if _supabase_only() else _pi_pending(limit)
    except Exception as e:
        _log(f"pending mislukt ({STORAGE}): {e!r}")
        return jsonify({"error": "storage unavailable"}), 503
    return jsonify(rows)


@app.route("/api/cm/result", methods=["POST"])
def api_cm_result():
    """Scraper pusht scrape-resultaat: {item_id, url, listings: [...]} of {item_id, error}."""
    payload = request.get_json(force=True, silent=True) or {}
    item_id = payload.get("item_id")
    if not item_id:
        return jsonify({"error": "item_id required"}), 400
    error = payload.get("error")
    listings = payload.get("listings") or []
    now = datetime.now(timezone.utc).isoformat()

    if _supabase_only():
        _sb_result(item_id, error, listings, now)
    else:
        _pi_result_dual(item_id, error, listings, now)

    return jsonify({"ok": True, "item_id": item_id, "n_listings": len(listings)})


@app.route("/api/cm/enqueue", methods=["POST"])
def api_cm_enqueue():
    """Handmatig een URL op de queue zetten (voor testen). Body: {item_id, url, grade?, card_key?}."""
    p = request.get_json(force=True, silent=True) or {}
    if not p.get("item_id") or not p.get("url"):
        return jsonify({"error": "item_id + url required"}), 400
    if _supabase_only():
        _sb_enqueue(p["item_id"], p["url"], p.get("grade"), p.get("card_key"))
    else:
        _pi_enqueue(p["item_id"], p["url"])
    return jsonify({"ok": True})


@app.route("/health", methods=["GET"])
def health():
    try:
        pending = _sb_pending_count() if _supabase_only() else _pi_pending_count()
    except Exception as e:
        _log(f"health mislukt ({STORAGE}): {e!r}")
        return jsonify({"ok": False, "storage": STORAGE, "port": PORT}), 503
    return jsonify({"ok": True, "pending": pending, "storage": STORAGE, "port": PORT})


if __name__ == "__main__":
    _log(f"start op poort {PORT}, storage={STORAGE}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
