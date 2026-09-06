#!/usr/bin/env python3
"""
Kensa CM-queue API — standalone, geen dashboard.

Levert de 3 endpoints die de externe Windows CM-scraper nodig heeft:
  GET  /api/cm/pending   → lijst URLs die gescraped moeten
  POST /api/cm/result    → scrape-resultaat wegschrijven
  POST /api/cm/enqueue   → handmatig URL bijzetten (test)

Draait op poort 8898 (was 8899 in oude kensa-webapp).
Losgekoppeld van de Flask-dashboard — dashboard kan permanent uit.

Storage: schrijft naar Pi SQLite + best-effort dual-write naar Supabase
(zelfde pad als de oude webapp, zodat scraper-code onveranderd blijft).
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request

DB_PATH = Path(os.environ.get("KENSA_DB", "/home/pi/.openclaw/workspace/agents/kensa/kensa.db"))
PORT = int(os.environ.get("KENSA_CM_API_PORT", "8898"))

app = Flask(__name__)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


@app.route("/api/cm/pending", methods=["GET"])
def api_cm_pending():
    """Scraper polt hier: welke URLs moet ik nu ophalen?

    Alleen rijen met expliciete grade — nooit defaulten naar 10 (bug: PSA 9
    slabs zouden anders met PSA 10 CM-listings vergeleken worden).
    """
    limit = min(int(request.args.get("limit", 20)), 100)
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
        return jsonify([{"item_id": r["item_id"], "url": r["url"], "grade": r["grade"]} for r in rows])
    finally:
        conn.close()


@app.route("/api/cm/result", methods=["POST"])
def api_cm_result():
    """Scraper pusht scrape-resultaat: {item_id, url, listings: [...]} of {item_id, error}."""
    payload = request.get_json(force=True, silent=True) or {}
    item_id = payload.get("item_id")
    if not item_id:
        return jsonify({"error": "item_id required"}), 400
    listings_json = json.dumps(payload.get("listings") or []) if not payload.get("error") else None
    error = payload.get("error")
    now = datetime.now(timezone.utc).isoformat()

    conn = _db()
    cm_url_for_sync = None
    card_key_for_sync = None
    try:
        conn.execute(
            "UPDATE cardmarket_queue SET fetched_at=?, listings_json=?, error=? WHERE item_id=?",
            (now, listings_json, error, item_id),
        )
        # Vul price_cache zodat volgende scans van dezelfde kaart binnen TTL geen worker-call nodig hebben.
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

    # Dual-write naar Supabase (best-effort, faalt-stil — zelfde pad als oude webapp).
    try:
        import supabase_sync as _sbs  # type: ignore
        if cm_url_for_sync:
            try:
                _sbs.sync_cm_queue_result(item_id, cm_url_for_sync, now,
                                          payload.get("listings") or [], error)
            except Exception:
                pass
        if card_key_for_sync:
            try:
                _sbs.sync_price_cache_cm(card_key_for_sync, cm_url_for_sync,
                                          payload.get("listings") or [], now)
            except Exception:
                pass
    except ImportError:
        pass

    return jsonify({"ok": True, "item_id": item_id, "n_listings": len(payload.get("listings") or [])})


@app.route("/api/cm/enqueue", methods=["POST"])
def api_cm_enqueue():
    """Handmatig een URL op de queue zetten (voor testen). Body: {item_id, url}."""
    p = request.get_json(force=True, silent=True) or {}
    if not p.get("item_id") or not p.get("url"):
        return jsonify({"error": "item_id + url required"}), 400
    now = datetime.now(timezone.utc).isoformat()
    conn = _db()
    try:
        conn.execute(
            """INSERT INTO cardmarket_queue (item_id, url, queued_at)
               VALUES (?, ?, ?)
               ON CONFLICT(item_id) DO UPDATE SET url=excluded.url, queued_at=excluded.queued_at,
                                                   fetched_at=NULL, listings_json=NULL, error=NULL""",
            (p["item_id"], p["url"], now),
        )
        conn.commit()
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.route("/health", methods=["GET"])
def health():
    conn = _db()
    try:
        pending = conn.execute(
            "SELECT COUNT(*) FROM cardmarket_queue WHERE fetched_at IS NULL"
        ).fetchone()[0]
        return jsonify({"ok": True, "pending": pending, "port": PORT})
    finally:
        conn.close()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
