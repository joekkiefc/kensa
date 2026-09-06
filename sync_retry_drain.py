#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa — sync-retry drain worker.

Pakt tot LIMIT rijen uit `sync_retry` waarvan `next_retry_at` verstreken is,
en probeert de originele Supabase-call opnieuw uit te voeren. Succes → row weg.
Fail → attempts++ en volgende retry gepland (of dead-letter na max).

Wordt gedraaid door cron (elke 2 min) en logt naar sync_retry_drain.log.

Exit codes:
  0 — niks te doen of alles gelukt
  1 — 1+ retries gefaald (nog niet dead-letter)
  2 — 1+ items in dead-letter beland tijdens deze run (opereer met alert)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import supabase_sync as sbs  # noqa: E402
import sync_retry as sr      # noqa: E402

LOG_PATH = Path(__file__).resolve().parent / "sync_retry_drain.log"
DEFAULT_LIMIT = 100
DELAY_BETWEEN_CALLS = 0.05  # 50ms adempauze voorkomt burst-throttling
# Fix #4 — exponential backoff bij opeenvolgende fails ipv door hammeren.
# Formule: min(BACKOFF_MAX, BACKOFF_BASE * 2 ** (consecutive_fails - 1))
# fail#1 → 1s, fail#2 → 2s, fail#3 → 4s, fail#4 → 8s, ceil BACKOFF_MAX.
# Reset counter zodra 1 call succesvol is. Voorkomt dat de drain juist
# druk toevoegt precies wanneer het netwerk stottert (zie RCA §9 fix #4).
BACKOFF_BASE = 1.0
BACKOFF_MAX = 30.0
ALERT_CHANNEL_ID = "1379489498835976424"  # #algemeen — Tommy leest hier mee
ALERT_MENTION = "<@375411895580098570>"   # Tommy


def _log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat()} {msg}\n"
    try:
        with LOG_PATH.open("a") as f:
            f.write(line)
    except Exception:
        pass
    print(msg)


def _replay_post(row: dict) -> tuple[bool, str]:
    """Herbouw POST-call uit payload en voer uit. Gebruikt de nieuwe
    (5, 25)-timeout via _do_post_http default."""
    payload = json.loads(row["payload_json"])
    return sbs._do_post_http(row["path"], payload["rows"], payload.get("prefer"))


def _replay_patch(row: dict) -> tuple[bool, str]:
    """Herbouw PATCH-call uit payload en voer uit."""
    payload = json.loads(row["payload_json"])
    return sbs._do_patch_http(row["path"], payload["query"], payload["patch"])


def _replay_one(row: dict) -> tuple[bool, str]:
    if row["method"] == "POST":
        return _replay_post(row)
    if row["method"] == "PATCH":
        return _replay_patch(row)
    return (False, f"unknown method: {row['method']}")


def drain(limit: int = DEFAULT_LIMIT) -> dict:
    """Drain-cycle. Returns summary dict met counts + dead-letter ids."""
    sr.ensure_table()
    started_at = sr._now_iso()
    due = sr.fetch_due(limit)
    if not due:
        return {"processed": 0, "success": 0, "failed": 0, "dead": 0, "dead_ids": [], "started_at": started_at}

    success = failed = 0
    dead_ids: list[int] = []
    consecutive_fails = 0
    for row in due:
        ok, err = _replay_one(row)
        if ok:
            sr.mark_success(row["id"])
            success += 1
            consecutive_fails = 0
            time.sleep(DELAY_BETWEEN_CALLS)
        else:
            became_dead = sr.bump_retry(row["id"], err)
            failed += 1
            if became_dead:
                dead_ids.append(row["id"])
            consecutive_fails += 1
            # Exponentiële backoff: fail#1 → 1s, fail#2 → 2s, fail#3 → 4s, ...
            # cap op BACKOFF_MAX (30s). Bij succes reset naar 50ms.
            wait_s = min(BACKOFF_MAX, BACKOFF_BASE * (2 ** (consecutive_fails - 1)))
            _log(f"backoff: fail#{consecutive_fails} → sleep {wait_s:.1f}s (err={err[:80]})")
            time.sleep(wait_s)

    return {
        "processed": len(due),
        "success": success,
        "failed": failed,
        "dead": len(dead_ids),
        "dead_ids": dead_ids,
        "started_at": started_at,
    }


def _exit_code(summary: dict) -> int:
    if summary["dead"] > 0:
        return 2
    if summary["failed"] > 0:
        return 1
    return 0


def _alert_dead_letter(dead_ids: list[int]) -> None:
    """Post Discord alert bij nieuw-dead-letter items. Kort en actionable."""
    if not dead_ids:
        return
    msg = (
        f"{ALERT_MENTION} **Kensa sync-retry: dead-letter alert**\n"
        f"{len(dead_ids)} write(s) hebben na 6 pogingen nog steeds gefaald.\n"
        f"Retry-ID(s): `{', '.join(str(i) for i in dead_ids)}`\n"
        f"Check: `sqlite3 kensa.db 'SELECT * FROM sync_retry WHERE dead_letter=1'` "
        f"of `sync_retry_drain.py --stats`"
    )
    try:
        subprocess.run(
            ["openclaw", "message", "send",
             "--channel", "discord", "--target", ALERT_CHANNEL_ID,
             "-m", msg],
            check=True, timeout=15, capture_output=True,
        )
        _log(f"dead-letter alert sent for ids={dead_ids}")
    except Exception as e:
        _log(f"dead-letter alert FAILED to send: {e!r}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help=f"max rijen per drain-cycle (default {DEFAULT_LIMIT})")
    ap.add_argument("--stats", action="store_true", help="alleen stats printen, geen drain")
    args = ap.parse_args()

    if args.stats:
        print(json.dumps(sr.stats(), indent=2))
        return 0

    summary = drain(limit=args.limit)
    _log(
        f"drain: processed={summary['processed']} success={summary['success']} "
        f"failed={summary['failed']} dead={summary['dead']}"
    )
    if summary["dead_ids"]:
        _log(f"DEAD-LETTER ids: {summary['dead_ids']}")
        _alert_dead_letter(summary["dead_ids"])
    return _exit_code(summary)


if __name__ == "__main__":
    sys.exit(main())
