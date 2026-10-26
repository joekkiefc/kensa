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
import threading
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request

DB_PATH = Path(os.environ.get("KENSA_DB", "/home/pi/.openclaw/workspace/agents/kensa/kensa.db"))
PORT = int(os.environ.get("KENSA_CM_API_PORT", "8898"))
STORAGE = os.environ.get("KENSA_STORAGE", "dual")  # "supabase" | "dual"
SB_SCHEMA = "kensa"
SB_TABLE = "cardmarket_queue"

# ---------------------------------------------------------------------------
# Vaste daglimiet Cardmarket (herstart 2026-10-26)
# ---------------------------------------------------------------------------
# Kensa stond uit van 23 t/m 26 okt; de price_cache (3 dagen vers) is daardoor
# leeg en zou bij herstart in één dag honderden kaarten tegelijk willen
# ophalen. Dat is een ban-risico bij Cardmarket. Daarom: NIET meer URL's aan de
# Windows-worker uitdelen dan we vóór de stop per dag deden, plus 10%.
#
# Meting (cm_queue_api.log, POST /api/cm/result = één echte CM-pagina door de
# worker), 38 volle dagen 14 sep t/m 21 okt 2026: gemiddeld 221,7 per dag,
# piek 319. Tommy (26 okt): tel op UNIEKE Cardmarket-URL's, 250 nieuwe per dag.
#
# Werking: per Amsterdam-dag onthouden we welke URL's naar de worker zijn gegaan
# ("urls") en welke daarvan een resultaat terug hebben ("klaar").
#   - nieuwe URL = telt mee, stopt bij CM_DAGLIMIET
#   - al uitgedeeld maar nog geen resultaat = gewoon opnieuw aanbieden, telt niet
#     nog eens (de Windows-worker pakt per poll maar één of enkele van de batch,
#     gebleken 26 okt: 18 van 20 bleven liggen)
#   - al resultaat vandaag = NIET nog eens (dubbele kaarten op dezelfde CM-pagina)
#   - binnen één antwoord elke URL één keer
#
# De limiet is BEWUST vast (geen berekening op het moment zelf). Zodra de cache
# weer bijgewerkt is en het risico weg is, mag hij eraf: constante op 0 zetten
# (= geen limiet) en de service herstarten. De dagstand staat in een bestandje
# zodat een herstart van de service hem niet wist.
CM_DAGLIMIET = 250
CM_DAG_TZ = ZoneInfo("Europe/Amsterdam")
CM_DAGTELLER_PAD = Path(os.environ.get(
    "KENSA_CM_DAGTELLER",
    "/home/pi/.openclaw/workspace/agents/kensa/cm_dagteller.json"))
_dagteller_slot = threading.Lock()
_limiet_gemeld_voor_dag: str | None = None
_vorige_gelogde_stand: int | None = None

app = Flask(__name__)


def _vandaag() -> str:
    return datetime.now(CM_DAG_TZ).date().isoformat()


def _lees_dagteller() -> tuple[str, list[str], list[str]]:
    """(dag, vandaag uitgedeelde unieke URL's, URL's met resultaat vandaag).
    Andere/ontbrekende dag = leeg."""
    try:
        d = json.loads(CM_DAGTELLER_PAD.read_text())
        if d.get("dag") == _vandaag():
            return d["dag"], list(d.get("urls", [])), list(d.get("klaar", []))
    except Exception:
        pass
    return _vandaag(), [], []


def _schrijf_dagteller(dag: str, urls: list[str], klaar: list[str]) -> None:
    tmp = CM_DAGTELLER_PAD.with_suffix(".tmp")
    tmp.write_text(json.dumps({"dag": dag, "uitgedeeld": len(urls), "limiet": CM_DAGLIMIET,
                               "klaar_aantal": len(klaar), "urls": urls, "klaar": klaar}))
    tmp.replace(CM_DAGTELLER_PAD)


def _markeer_klaar(url: str | None) -> None:
    """Resultaat binnen voor deze URL: vandaag niet nog een keer naar de worker."""
    if not url:
        return
    with _dagteller_slot:
        dag, urls, klaar = _lees_dagteller()
        if url in klaar:
            return
        klaar.append(url)
        if url not in urls:      # resultaat van vóór een herstart/omzetting: alsnog tellen
            urls.append(url)
        _schrijf_dagteller(dag, urls, klaar)


def _ruimte_vandaag() -> int:
    """Hoeveel NIEUWE unieke URL's mogen er vandaag nog naar de worker. 0 = limiet
    bereikt. Zonder limiet (CM_DAGLIMIET <= 0) altijd 'onbeperkt' (groot getal)."""
    if CM_DAGLIMIET <= 0:
        return 10**9
    _, urls, _ = _lees_dagteller()
    return max(0, CM_DAGLIMIET - len(urls))


def _kies_uit_te_delen(rows: list[dict], limit: int) -> tuple[list[dict], int, int]:
    """Filtert pending rijen tot wat nu naar de worker mag. Geeft
    (rijen, dagstand, aantal nieuwe URL's dat wegens de limiet bleef liggen) terug.
    - URL met resultaat vandaag → overslaan (dubbele kaart, zelfde CM-pagina)
    - URL vandaag al uitgedeeld, nog geen resultaat → opnieuw aanbieden, telt niet nog eens
    - nieuwe URL → telt mee, alleen zolang er ruimte is
    - binnen dit antwoord elke URL één keer; maximaal `limit` rijen
    Geteld wordt bij UITDELEN (niet bij het resultaat): elke uitgedeelde URL
    wordt door de worker gescraped, ook als hij fout gaat. Dat is de veilige kant."""
    dag, urls, klaar = _lees_dagteller()
    uitgedeeld, al_klaar = set(urls), set(klaar)
    ruimte = 10**9 if CM_DAGLIMIET <= 0 else max(0, CM_DAGLIMIET - len(urls))
    in_antwoord: set[str] = set()
    keuze: list[dict] = []
    nieuw = 0
    blijft_liggen = 0
    for r in rows:
        url = r.get("url")
        if not url or url in al_klaar or url in in_antwoord:
            continue
        if len(keuze) >= limit:
            break
        if url not in uitgedeeld:
            if nieuw >= ruimte:
                blijft_liggen += 1
                continue
            nieuw += 1
            urls.append(url)
            uitgedeeld.add(url)
        in_antwoord.add(url)
        keuze.append(r)
    if nieuw:
        _schrijf_dagteller(dag, urls, klaar)
    return keuze, len(urls), blijft_liggen


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
    # Zelfde CM-pagina = zelfde resultaat: wachtende rijen met dezelfde URL
    # meteen invullen, dan haalt de Windows-worker ze niet nóg een keer op.
    try:
        n_filled = _sb_fill_same_url(cm_url, item_id, listings, now)
    except Exception as e:
        n_filled = 0
        _log(f"same-url-vulling voor {item_id} mislukt (worker haalt ze zelf op): {e!r}")
    if n_filled:
        _log(f"queue-result {item_id}: {n_filled} wachtende rij(en) met dezelfde URL direct ingevuld")
    if not card_key:
        return
    try:
        from storage_supabase.price_cache import upsert_cm
        upsert_cm(card_key, cm_url, listings, now)
    except Exception as e:
        _log(f"price_cache {card_key} BLIJVEND mislukt: {e!r}")


def _sb_fill_same_url(url: str | None, done_item_id: str, listings: list, now: str,
                      schema: str = SB_SCHEMA, table: str = SB_TABLE) -> int:
    """Vul alle nog-wachtende queue-rijen (fetched_at leeg) met dezelfde
    Cardmarket-URL direct in met dit resultaat, en werk price_cache bij voor
    hun card_keys — precies wat er gebeurd zou zijn als de worker ze zelf had
    opgehaald. Alleen bij een écht resultaat (niet-lege listings): een lege
    lijst kan een mislukte scrape zijn en mag niet 'plakken' op andere kaarten.

    Retourneert het aantal ingevulde rijen (0 bij niks te doen of storing;
    de worker haalt ze dan gewoon zelf op — nooit erger dan vroeger).
    """
    from storage_supabase import _http
    if not url or not listings:
        return 0
    r = _http.patch(table, schema,
                    {"url": f"eq.{url}", "fetched_at": "is.null", "item_id": f"neq.{done_item_id}"},
                    {"fetched_at": now, "listings_json": listings, "error": None})
    if r.status_code != 200:
        return 0
    try:
        rows = r.json() or []
    except Exception:
        return 0
    keys = sorted({row.get("card_key") for row in rows if row.get("card_key")})
    if keys:
        from storage_supabase.price_cache import upsert_cm
        for key in keys:
            try:
                upsert_cm(key, url, listings, now, schema=schema)
            except Exception as e:
                _log(f"price_cache {key} (same-url-vulling) mislukt: {e!r}")
    return len(rows)


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
    global _limiet_gemeld_voor_dag, _vorige_gelogde_stand
    limit = min(int(request.args.get("limit", 20)), 100)
    with _dagteller_slot:
        # Ruimer ophalen dan we uitdelen: rijen die afvallen (al klaar vandaag)
        # mogen de batch niet leegtrekken.
        try:
            rows = _sb_pending(100) if _supabase_only() else _pi_pending(100)
        except Exception as e:
            _log(f"pending mislukt ({STORAGE}): {e!r}")
            return jsonify({"error": "storage unavailable"}), 503
        keuze, stand, blijft_liggen = _kies_uit_te_delen(rows, limit)
        if blijft_liggen and _limiet_gemeld_voor_dag != _vandaag():
            # Limiet bereikt: nieuwe URL's blijven in de wachtrij tot morgen
            # (Amsterdam-dag); al uitgedeelde rijen worden nog wel afgemaakt.
            _limiet_gemeld_voor_dag = _vandaag()
            _log(f"daglimiet {CM_DAGLIMIET} bereikt voor {_vandaag()} — "
                 f"{blijft_liggen} nieuwe URL's blijven liggen tot morgen")
        if keuze and stand != _vorige_gelogde_stand:   # alleen loggen als de stand verandert
            _vorige_gelogde_stand = stand
            _log(f"pending: {len(keuze)} rijen naar de worker, dagstand {stand}/{CM_DAGLIMIET} unieke URL's")
    return jsonify(keuze)


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
    _markeer_klaar(payload.get("url"))

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
    dag, urls, klaar = _lees_dagteller()
    return jsonify({"ok": True, "pending": pending, "storage": STORAGE, "port": PORT,
                    "daglimiet": CM_DAGLIMIET, "dag": dag, "uitgedeeld_vandaag": len(urls),
                    "klaar_vandaag": len(klaar)})


if __name__ == "__main__":
    _log(f"start op poort {PORT}, storage={STORAGE}, daglimiet={CM_DAGLIMIET} unieke URL's/dag "
         f"(vandaag al uitgedeeld: {len(_lees_dagteller()[1])}, klaar: {len(_lees_dagteller()[2])})")
    app.run(host="0.0.0.0", port=PORT, debug=False)
