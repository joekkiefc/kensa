"""Gedeelde logging voor storage_supabase fetch/read operaties.

Alle nieuwe Supabase-native lees-functies loggen hun call naar één centrale
file (`pipeline_supabase_reads.log`) zodat we kunnen monitoren of migratie
werkt zonder crash + wat de performance is.

Format per regel:
  [ISO-8601] [module.func] status=N rows=N ms=N key=... err=...

Wordt door een cron elke 15 min gelezen en samengevat naar Discord.
"""
from __future__ import annotations
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = Path("/home/pi/.openclaw/workspace/agents/kensa/pipeline_supabase_reads.log")


def _write(line: str) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def log_call(func: str, status: str = "ok", rows: int = 0, ms: int = 0,
             key: str = "", err: str = "") -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    parts = [f"[{ts}]", f"[{func}]", f"status={status}", f"rows={rows}", f"ms={ms}"]
    if key:
        parts.append(f"key={key}")
    if err:
        parts.append(f"err={err[:200]}")
    _write(" ".join(parts))


@contextmanager
def timed(func: str, key: str = ""):
    """Context manager: meet duur en log automatisch. Vang exceptions af (logt err)."""
    t0 = time.perf_counter()
    rows = {"n": 0}
    try:
        yield rows
        ms = int((time.perf_counter() - t0) * 1000)
        log_call(func, status="ok", rows=rows.get("n", 0), ms=ms, key=key)
    except Exception as e:
        ms = int((time.perf_counter() - t0) * 1000)
        log_call(func, status="err", rows=0, ms=ms, key=key, err=f"{type(e).__name__}: {e}")
        raise
