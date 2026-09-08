#!/usr/bin/env python3
"""kensa_bewaker — dé ene Kensa-monitor (post-migratie, Supabase-only).

Vervangt drie losse bewakers:
  - scripts/sync-drift-check.py   (Pi<->Supabase drift + backfill; na de cutover is de
                                   Pi bevroren -> drift is verwacht én backfill = risico)
  - scripts/monitor-cutover-cache.py (transitie-monitor batch 2; klaar + kapot)
  - de losse 6u-cron van report_supabase_writes.py (logica hergebruikt, niet weggegooid)

Vier secties, één verdict (slechtste sectie wint), alleen afwijkingen uitgelicht:
  1. SCHRIJVEN   — Supabase write-gezondheid (report_supabase_writes.build_report,
                   duplicaten 409/23505 tellen niet als fout)
  2. DOORSTROOM  — nieuwe listings + verdicts per uur, backlogs (uit Supabase)
  3. WORKERS     — laatste run per worker uit de logs: leeft 'ie nog?
  4. CARDMARKET  — pending in de wachtrij + laatste ophaling door de Windows-worker

Cron: elke 6u met --discord naar #algemeen. Handmatig: kensa_bewaker.py (stdout).
Meet Supabase, niet de Pi (memory: kensa-measure-supabase-not-pi).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from report_supabase_writes import build_report, post_discord  # noqa: E402

SECRETS = Path("/home/pi/.openclaw/secrets.json")

GROEN, ORANJE, ROOD = 0, 1, 2
KLEUR = {GROEN: "🟢 GROEN", ORANJE: "🟠 ORANJE", ROOD: "🔴 ROOD"}


# ---------------------------------------------------------------- Supabase
def _sb():
    s = json.loads(SECRETS.read_text())["supabase"]
    return s["url"], (s.get("service_role_key") or s.get("service_key") or s.get("key"))


def _get(url: str, headers: dict, params: list, attempts: int = 3):
    """GET met korte retry: de Supabase-route geeft incidenteel TLS-resets/timeouts;
    één hik mag de monitor niet oranje kleuren."""
    import time
    for i in range(attempts):
        try:
            return requests.get(url, headers=headers, params=params, timeout=25)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if i == attempts - 1:
                return None
            time.sleep(2)


def sb_count(table: str, params: list[tuple[str, str]]) -> int | None:
    """Exact aantal rijen. None bij fout (nooit stil 0 — les van 7 sept)."""
    url, key = _sb()
    h = {"apikey": key, "Authorization": f"Bearer {key}", "Accept-Profile": "kensa",
         "Prefer": "count=exact", "Range": "0-0"}
    r = _get(f"{url}/rest/v1/{table}", h, [("select", "item_id")] + params)
    if r is None or r.status_code >= 400:
        return None
    try:
        return int(r.headers.get("content-range", "*/0").split("/")[-1])
    except Exception:
        return None


def sb_max(table: str, col: str, params: list[tuple[str, str]]) -> datetime | None:
    url, key = _sb()
    h = {"apikey": key, "Authorization": f"Bearer {key}", "Accept-Profile": "kensa"}
    r = _get(f"{url}/rest/v1/{table}", h,
             [("select", col), ("order", f"{col}.desc.nullslast"), ("limit", "1")] + params)
    try:
        if r is None or r.status_code >= 400 or not r.json():
            return None
        return _ts(r.json()[0].get(col))
    except Exception:
        return None


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _min_ago(d: datetime | None) -> int | None:
    return None if d is None else int((datetime.now(timezone.utc) - d).total_seconds() // 60)


# ---------------------------------------------------------------- 2. doorstroom
def sectie_doorstroom(hours: int) -> tuple[int, list[str]]:
    now = datetime.now(timezone.utc)
    u1 = (now - timedelta(hours=1)).isoformat()
    uN = (now - timedelta(hours=hours)).isoformat()
    nieuw1 = sb_count("listings", [("first_seen_at", f"gte.{u1}")])
    nieuwN = sb_count("listings", [("first_seen_at", f"gte.{uN}")])
    verdict1 = sb_count("analysis", [("trap", "eq.summary"), ("created_at", f"gte.{u1}")])
    verdictN = sb_count("analysis", [("trap", "eq.summary"), ("created_at", f"gte.{uN}")])
    detail_bl = sb_count("listings", [("detail_scraped_at", "is.null"), ("status", "eq.new")])
    ocr_bl = sb_count("listings", [("detail_scraped_at", "not.is.null"),
                                   ("slab_status", "is.null"), ("status", "eq.new")])

    st, regels = GROEN, []
    if None in (nieuw1, nieuwN, verdict1, verdictN):
        return ORANJE, ["doorstroom: Supabase niet bereikbaar voor tellingen (tijdelijk?)"]
    regels.append(f"nieuw: {nieuw1}/u · {nieuwN}/{hours}u — verdicts: {verdict1}/u · {verdictN}/{hours}u"
                  f" — backlog detail {detail_bl} · OCR {ocr_bl}")
    # 's nachts (01-07) is 0 instroom normaal; overdag is 0 in 2u verdacht
    uur = datetime.now().hour
    overdag = 7 <= uur <= 23
    if overdag and nieuwN == 0:
        st = max(st, ROOD); regels.append(f"⚠️ al {hours}u géén nieuwe listing — scraper stil?")
    elif overdag and nieuw1 == 0:
        st = max(st, ORANJE); regels.append("⚠️ laatste uur 0 nieuwe listings")
    if nieuwN and verdictN == 0:
        st = max(st, ROOD); regels.append(f"⚠️ wél instroom maar 0 verdicts in {hours}u — pijplijn stokt")
    if detail_bl is not None and detail_bl > 300:
        st = max(st, ROOD if detail_bl > 800 else ORANJE); regels.append(f"⚠️ detail-backlog loopt op: {detail_bl}")
    if ocr_bl is not None and ocr_bl > 300:
        st = max(st, ROOD if ocr_bl > 800 else ORANJE); regels.append(f"⚠️ OCR-backlog loopt op: {ocr_bl}")
    return st, regels


# ---------------------------------------------------------------- 3. workers
WORKERS = [
    # naam,        logbestand,                     header-regex (groep 1 = ISO ts),               interval (min)
    ("scrape",      "pipeline_scrape.log",          r"^=+\s+(\S+)\s+kensa scrape start",           15),
    ("detail",      "pipeline_detail_mercapi.log",  r"^=+\s+(\S+)\s+kensa detail-mercapi start",   3),
    ("ocr",         "pipeline_worker_ocr.log",      r"^=+\s+(\S+)\s+worker-ocr start",             2),
    ("cache",       "pipeline_worker_cache.log",    r"^=+\s+(\S+)\s+worker-cache start",           1),
    ("ebay",        "pipeline_worker_ebay.log",     r"^=+\s+(\S+)\s+worker-ebay start",            4),
    ("bevoorrader", "cm_bevoorrader.log",           r"^\[(\S+)\]\s+\[bevoorrader\]\s+(?:klaar|recent=|wachtrij vol)", 3),
]


def _laatste_ts(log: Path, rx: re.Pattern, tail_bytes: int = 200_000) -> datetime | None:
    if not log.exists():
        return None
    with log.open("rb") as f:
        f.seek(0, 2); size = f.tell(); f.seek(max(0, size - tail_bytes))
        chunk = f.read().decode("utf-8", errors="replace")
    laatste = None
    for line in chunk.splitlines():
        m = rx.match(line)
        if m:
            t = _ts(m.group(1))
            if t:
                laatste = t
    return laatste


def _tracebacks(log: Path, since: datetime, tail_bytes: int = 400_000) -> int:
    """Tel Traceback-regels ná `since` (op de dichtstbijzijnde voorafgaande ISO-tijd)."""
    if not log.exists():
        return 0
    with log.open("rb") as f:
        f.seek(0, 2); size = f.tell(); f.seek(max(0, size - tail_bytes))
        chunk = f.read().decode("utf-8", errors="replace")
    n, cur = 0, None
    iso = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^\s\]]*)")
    for line in chunk.splitlines():
        m = iso.search(line)
        if m:
            t = _ts(m.group(1))
            if t:
                cur = t
        if "Traceback (most recent call last)" in line and cur and cur >= since:
            n += 1
    return n


def sectie_workers(hours: int) -> tuple[int, list[str]]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    st, regels, stil, tb_tot = GROEN, [], [], 0
    for naam, logf, rx, interval in WORKERS:
        log = HERE / logf
        t = _laatste_ts(log, re.compile(rx))
        ago = _min_ago(t)
        tb = _tracebacks(log, since)
        tb_tot += tb
        if ago is None:
            st = max(st, ROOD); stil.append(f"{naam}: geen run gevonden")
        elif ago > interval * 4:
            st = max(st, ROOD); stil.append(f"{naam}: laatste run {ago} min geleden (elke {interval})")
        elif ago > interval * 2:
            st = max(st, ORANJE); stil.append(f"{naam}: laatste run {ago} min geleden (elke {interval})")
    regels.append(f"{len(WORKERS)} workers gecheckt — {len(WORKERS)-len(stil)} op schema"
                  + (f" · tracebacks {tb_tot}/{hours}u" if tb_tot else ""))
    for s in stil:
        regels.append(f"⚠️ {s}")
    if tb_tot > 10:
        st = max(st, ORANJE); regels.append(f"⚠️ {tb_tot} tracebacks in {hours}u — logs bekijken")
    return st, regels


# ---------------------------------------------------------------- 4. cardmarket
def sectie_cardmarket() -> tuple[int, list[str]]:
    pending = sb_count("cardmarket_queue", [("fetched_at", "is.null"), ("grade", "not.is.null")])
    laatste = sb_max("cardmarket_queue", "fetched_at", [("fetched_at", "not.is.null")])
    ago = _min_ago(laatste)
    st, regels = GROEN, []
    if pending is None:
        return ORANJE, ["cardmarket: Supabase niet bereikbaar"]
    regels.append(f"pending {pending} · laatste ophaling Windows-worker "
                  + (f"{ago} min geleden" if ago is not None else "onbekend"))
    if pending > 0 and (ago is None or ago > 30):
        st = max(st, ROOD); regels.append("⚠️ er staat werk maar de Windows-worker haalt niets op (>30 min)")
    elif pending > 150:
        st = max(st, ORANJE); regels.append("⚠️ wachtrij loopt vol (>150) — worker houdt het niet bij")
    return st, regels


# ---------------------------------------------------------------- samenstellen
def _verdict_van(tekst: str) -> int:
    return ROOD if "🔴" in tekst else ORANJE if "🟠" in tekst else GROEN


def bouw_rapport(hours: int) -> tuple[int, str]:
    v_schr, txt_schr = build_report(hours)
    s_schr = _verdict_van(v_schr)
    s_flow, r_flow = sectie_doorstroom(hours)
    s_work, r_work = sectie_workers(hours)
    s_cm, r_cm = sectie_cardmarket()
    totaal = max(s_schr, s_flow, s_work, s_cm)

    # write-sectie: alleen de kern-regels (writes/latency/retry/lees-fouten), niet de uurtabel
    kern = [l for l in txt_schr.splitlines()[1:]
            if l.startswith("• writes") or l.startswith("• latency") or l.startswith("• retry-queue")
            or l.startswith("• lees-fouten") or l.startswith("• nog geen")]

    L = [f"**Kensa bewaker** — laatste {hours}u — {KLEUR[totaal]}"]
    L.append(f"**1. Schrijven** {KLEUR[s_schr]}")
    L += [f"  {l[2:]}" for l in kern]
    L.append(f"**2. Doorstroom** {KLEUR[s_flow]}")
    L += [f"  {r}" for r in r_flow]
    L.append(f"**3. Workers** {KLEUR[s_work]}")
    L += [f"  {r}" for r in r_work]
    L.append(f"**4. Cardmarket** {KLEUR[s_cm]}")
    L += [f"  {r}" for r in r_cm]
    if totaal == GROEN:
        L.append("Alles loopt. Geen actie nodig.")
    return totaal, "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=6)
    ap.add_argument("--discord", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    verdict, tekst = bouw_rapport(a.hours)
    if not a.quiet:
        print(tekst)
    if a.discord:
        ok = post_discord(tekst)
        print(f"[bewaker] discord: {'verstuurd' if ok else 'MISLUKT'}", file=sys.stderr)
        if not ok:
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
