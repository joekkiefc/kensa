#!/usr/bin/env python3
"""Kensa cutover-monitor — rapport over Supabase-writes van de Supabase-only route.

Leest:
  - pipeline_supabase_writes.log   (JSONL, geschreven door storage_supabase/_http.py)
  - pipeline_supabase_reads.log    (tekst, storage_supabase/_logging.py)  -> alleen fouten
  - sync_retry_drain.log           ("drain: processed=.. success=.. failed=.. dead=..")
  - kensa.db / sync_retry          (queue-diepte, dead-letters, oudste wachtende)

Gebruik:
  report_supabase_writes.py                 # laatste 24u, naar stdout
  report_supabase_writes.py --hours 6       # ander venster
  report_supabase_writes.py --discord       # ook posten in #algemeen (via openclaw)

Verdict:
  GROEN  = geen blijvende fouten, geen dead-letters, <2% geparkeerd, geen DNS/TLS
  ORANJE = 2-10% geparkeerd, of DNS/TLS gezien, of queue >20 wachtend
  ROOD   = blijvende fouten, dead-letters, >10% geparkeerd of queue >100 wachtend
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
WRITES_LOG = HERE / "pipeline_supabase_writes.log"
READS_LOG = HERE / "pipeline_supabase_reads.log"
DRAIN_LOG = HERE / "sync_retry_drain.log"
DB = HERE / "kensa.db"
ALERT_CHANNEL_ID = "1379489498835976424"  # #algemeen


def _parse_ts(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _pct(vals: list[int], p: float) -> int:
    if not vals:
        return 0
    v = sorted(vals)
    return v[min(len(v) - 1, int(round((len(v) - 1) * p)))]


def load_writes(since: datetime) -> list[dict]:
    if not WRITES_LOG.exists():
        return []
    out = []
    with WRITES_LOG.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            ts = _parse_ts(rec.get("ts", ""))
            if ts and ts >= since:
                rec["_ts"] = ts
                out.append(rec)
    return out


_READ_ERR = re.compile(r"^\[(?P<ts>[^\]]+)\] \[(?P<func>[^\]]+)\] status=err .*?err=(?P<err>.*)$")


def load_read_errors(since: datetime) -> list[tuple[str, str]]:
    if not READS_LOG.exists():
        return []
    out = []
    with READS_LOG.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _READ_ERR.match(line.strip())
            if not m:
                continue
            ts = _parse_ts(m.group("ts"))
            if ts and ts >= since:
                out.append((m.group("func"), m.group("err")[:120]))
    return out


_DRAIN = re.compile(r"^(?P<ts>\S+) drain: processed=(?P<p>\d+) success=(?P<s>\d+) failed=(?P<f>\d+) dead=(?P<d>\d+)")


def load_drain(since: datetime) -> dict:
    tot = Counter()
    runs = 0
    if DRAIN_LOG.exists():
        with DRAIN_LOG.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _DRAIN.match(line.strip())
                if not m:
                    continue
                ts = _parse_ts(m.group("ts"))
                if ts and ts >= since:
                    runs += 1
                    tot["processed"] += int(m.group("p"))
                    tot["success"] += int(m.group("s"))
                    tot["failed"] += int(m.group("f"))
                    tot["dead"] += int(m.group("d"))
    tot["runs"] = runs
    return tot


def queue_state(since: datetime) -> dict:
    """Totalen én 'nieuw in venster' (created_at >= since) — oude rommel van vóór
    de test telt niet mee in het verdict, maar blijft wel zichtbaar."""
    st = {"pending": 0, "dead": 0, "pending_new": 0, "dead_new": 0, "oldest_pending_min": 0, "by_path": {}}
    if not DB.exists():
        return st
    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
        s_iso = since.isoformat()
        st["pending"] = c.execute("SELECT COUNT(*) FROM sync_retry WHERE dead_letter=0").fetchone()[0]
        st["dead"] = c.execute("SELECT COUNT(*) FROM sync_retry WHERE dead_letter=1").fetchone()[0]
        st["pending_new"] = c.execute("SELECT COUNT(*) FROM sync_retry WHERE dead_letter=0 AND created_at>=?", (s_iso,)).fetchone()[0]
        st["dead_new"] = c.execute("SELECT COUNT(*) FROM sync_retry WHERE dead_letter=1 AND created_at>=?", (s_iso,)).fetchone()[0]
        row = c.execute("SELECT MIN(created_at) FROM sync_retry WHERE dead_letter=0").fetchone()
        if row and row[0]:
            ts = _parse_ts(row[0])
            if ts:
                st["oldest_pending_min"] = int((datetime.now(timezone.utc) - ts).total_seconds() // 60)
        st["by_path"] = dict(c.execute(
            "SELECT substr(path,1,instr(path||'?','?')-1), COUNT(*) FROM sync_retry WHERE dead_letter=0 GROUP BY 1"
        ).fetchall())
    except Exception as e:
        st["error"] = repr(e)
    return st


def build_report(hours: int) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    writes = load_writes(since)
    read_errs = load_read_errors(since)
    drain = load_drain(since)
    q = queue_state(since)

    n = len(writes)
    outcome = Counter(w.get("outcome") for w in writes)
    err_cls = Counter(w.get("err_class") for w in writes if w.get("outcome") != "ok")
    tables = Counter(w.get("table") for w in writes)
    workers = Counter(w.get("worker") for w in writes)
    lat = [int(w.get("ms", 0)) for w in writes if w.get("outcome") == "ok"]
    per_hour: dict[str, Counter] = defaultdict(Counter)
    per_hour_lat: dict[str, list[int]] = defaultdict(list)
    for w in writes:
        h = w["_ts"].astimezone().strftime("%d %H:00")
        per_hour[h][w.get("outcome")] += 1
        if w.get("outcome") == "ok":
            per_hour_lat[h].append(int(w.get("ms", 0)))

    queued = outcome.get("queued", 0)
    failed = outcome.get("failed", 0)
    queued_pct = (queued / n * 100) if n else 0.0
    bad_classes = {k: v for k, v in err_cls.items() if k in ("dns", "tls")}

    # verdict
    if failed > 0 or q["dead_new"] > 0 or queued_pct > 10 or q["pending_new"] > 100:
        verdict = "🔴 ROOD"
    elif queued_pct >= 2 or bad_classes or q["pending_new"] > 20 or read_errs:
        verdict = "🟠 ORANJE"
    else:
        verdict = "🟢 GROEN"

    L = []
    L.append(f"**Kensa Supabase-only monitor** — laatste {hours}u — {verdict}")
    if n == 0:
        L.append("• nog geen writes gelogd op de Supabase-only route in dit venster")
    else:
        L.append(f"• writes: **{n}** → ok {outcome.get('ok',0)} · geparkeerd {queued} ({queued_pct:.1f}%) · blijvend mislukt {failed}")
        L.append(f"• latency ok-writes: p50 {_pct(lat,0.5)} ms · p95 {_pct(lat,0.95)} ms · max {max(lat) if lat else 0} ms")
        if err_cls:
            L.append("• foutklassen: " + ", ".join(f"{k} {v}" for k, v in err_cls.most_common()))
        L.append("• per tabel: " + ", ".join(f"{k} {v}" for k, v in tables.most_common(6)))
        L.append("• per worker: " + ", ".join(f"{k} {v}" for k, v in workers.most_common(6)))
    old_p, old_d = q['pending'] - q['pending_new'], q['dead'] - q['dead_new']
    L.append(f"• retry-queue: nieuw in venster {q['pending_new']} wachtend · {q['dead_new']} dead-letter"
             + (f"  (daarnaast nog {old_p} wachtend / {old_d} dead van vóór het venster)" if (old_p or old_d) else "")
             + (f" · per tabel {q['by_path']}" if q['by_path'] else ""))
    L.append(f"• drainer ({drain.get('runs',0)} runs): afgespeeld {drain.get('processed',0)} · gelukt {drain.get('success',0)} · mislukt {drain.get('failed',0)} · dead {drain.get('dead',0)}")
    if read_errs:
        rc = Counter(e for _, e in read_errs)
        L.append(f"• lees-fouten (Supabase reads): {len(read_errs)} — " + "; ".join(f"{v}x {k[:60]}" for k, v in rc.most_common(3)))
    else:
        L.append("• lees-fouten (Supabase reads): 0")
    if per_hour:
        L.append("• per uur (ok/geparkeerd/mislukt · p95 ms):")
        for h in sorted(per_hour)[-24:]:
            c = per_hour[h]
            L.append(f"    {h}  {c.get('ok',0)}/{c.get('queued',0)}/{c.get('failed',0)} · {_pct(per_hour_lat[h],0.95)}")
    return verdict, "\n".join(L)


def _openclaw_bin() -> str:
    # Cron heeft een kale PATH zonder nvm-bin; na een node-upgrade wijzigt dat pad ook nog.
    from shutil import which
    p = which("openclaw")
    if p:
        return p
    import glob
    import re

    def _ver(path: str):
        m = re.search(r"/node/v(\d+)\.(\d+)\.(\d+)/", path)
        return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)

    cands = sorted(glob.glob("/home/pi/.nvm/versions/node/*/bin/openclaw"), key=_ver)
    return cands[-1] if cands else "openclaw"


def post_discord(text: str) -> bool:
    # openclaw routeert Discord; expliciete agent i.v.m. agents.ownership=explicit sinds 2026.9.2
    _bin = _openclaw_bin()
    # openclaw is een node-CLI: diens bin-dir (met `node`) voorop het PATH, cron mist nvm.
    _env = dict(os.environ)
    # NB: bewust géén realpath — de symlink-dir is de nvm bin-dir waar ook `node` staat.
    _env["PATH"] = os.path.dirname(_bin) + os.pathsep + _env.get("PATH", "")
    for extra in (["--agent", "main"], []):
        try:
            r = subprocess.run([_bin, "message", "send", "--channel", "discord",
                                "--target", ALERT_CHANNEL_ID, *extra, "-m", text],
                               capture_output=True, text=True, timeout=60, env=_env)
            if r.returncode == 0:
                return True
            sys.stderr.write(f"[report] openclaw send rc={r.returncode}: {(r.stderr or r.stdout)[:200]}\n")
        except Exception as e:
            sys.stderr.write(f"[report] openclaw send exc: {e!r}\n")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--discord", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    verdict, text = build_report(a.hours)
    if not a.quiet:
        print(text)
    if a.discord:
        ok = post_discord(text)
        print(f"[report] discord: {'verstuurd' if ok else 'MISLUKT'}", file=sys.stderr)
        return 0 if ok else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
