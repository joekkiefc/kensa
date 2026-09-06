#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa storage layer — SQLite schema + photo store.

- `kensa.db` in this directory (SQLite).
- Photo bytes onder `photos/by_hash/{h[:2]}/{h[2:4]}/{h}.jpg` (content-addressable).
- URLs altijd bewaard, bytes on-demand gedownload.
- Retentie: 7 dagen voor photo bytes én raw_pages.
"""

import gzip
import hashlib
import io
import json
import os
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH = SCRIPT_DIR / "kensa.db"
PHOTO_ROOT = SCRIPT_DIR / "photos" / "by_hash"

UA = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

# Dual-write naar Supabase (best-effort). Enabled via KENSA_STORAGE=dual.
try:
    import supabase_sync as _sbs
except Exception:
    _sbs = None


def _sync(fn, *args, **kwargs):
    """Dual-write helper. Roept de sync-functie aan, of logt de mislukking.
    Silent-fail is GEEN optie meer — anders bouw je onzichtbare drift op
    (zie incident 2026-09-01, 151 stale Mercari-pending rows). Wat er ook
    gebeurt: de fout komt in supabase_sync.log terecht zodat de drift-check
    hem later kan oppikken."""
    if _sbs is None:
        return
    try:
        fn(*args, **kwargs)
    except Exception as e:
        try:
            _sbs._log(f"sync-exception {fn.__name__} {type(e).__name__}: {e!r}")
        except Exception:
            pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path=None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # Perf-tuning voor read-heavy queries op 837MB db:
    # - cache_size = -65536  → 64 MB page cache per connection (was 2 MB default)
    # - mmap_size  = 256 MB → memory-mapped I/O, veel minder read()-calls
    # - temp_store = MEMORY → CTE / window-funcs blijven in RAM ipv disk
    # - synchronous NORMAL → OK onder WAL, minder fsync-overhead voor writers
    conn.execute("PRAGMA cache_size = -65536")
    conn.execute("PRAGMA mmap_size = 268435456")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    item_id             TEXT PRIMARY KEY,
    title_jp            TEXT,
    title_en            TEXT,
    description_jp      TEXT,
    price_jpy           INTEGER,
    price_eur           REAL,
    shipping_jpy        INTEGER,
    seller_id           TEXT,
    seller_name         TEXT,
    seller_rating       TEXT,
    category_path       TEXT,
    condition           TEXT,
    authenticated       INTEGER DEFAULT 0,
    sold                INTEGER DEFAULT 0,
    detail_url          TEXT,
    first_seen_at       TEXT NOT NULL,
    last_seen_at        TEXT NOT NULL,
    detail_scraped_at   TEXT,
    status              TEXT NOT NULL DEFAULT 'new',
    extra_json          TEXT
);
CREATE INDEX IF NOT EXISTS idx_listings_last_seen ON listings(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status);

CREATE TABLE IF NOT EXISTS photos (
    photo_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id             TEXT NOT NULL REFERENCES listings(item_id) ON DELETE CASCADE,
    photo_index         INTEGER NOT NULL,
    url_original        TEXT NOT NULL,
    file_hash           TEXT,
    file_path           TEXT,
    downloaded_at       TEXT,
    size_bytes          INTEGER,
    width               INTEGER,
    height              INTEGER,
    UNIQUE(item_id, photo_index),
    UNIQUE(item_id, url_original)
);
CREATE INDEX IF NOT EXISTS idx_photos_hash ON photos(file_hash);
CREATE INDEX IF NOT EXISTS idx_photos_item ON photos(item_id);

CREATE TABLE IF NOT EXISTS user_verdicts (
    item_id             TEXT PRIMARY KEY REFERENCES listings(item_id) ON DELETE CASCADE,
    seen_at             TEXT,
    verdict             TEXT,
    notes               TEXT,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analysis (
    analysis_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id             TEXT NOT NULL REFERENCES listings(item_id) ON DELETE CASCADE,
    trap                TEXT NOT NULL,
    result_json         TEXT,
    confidence          REAL,
    card_id             TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analysis_item_trap ON analysis(item_id, trap);

CREATE TABLE IF NOT EXISTS cert_sightings (
    sighting_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    cert                TEXT NOT NULL,
    item_id             TEXT NOT NULL REFERENCES listings(item_id) ON DELETE CASCADE,
    seller_hint         TEXT,
    seen_at             TEXT NOT NULL,
    UNIQUE(cert, item_id)
);
CREATE INDEX IF NOT EXISTS idx_cert_sightings_cert ON cert_sightings(cert);

CREATE TABLE IF NOT EXISTS raw_pages (
    page_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    url                 TEXT NOT NULL,
    page_type           TEXT NOT NULL,
    html_gz             BLOB NOT NULL,
    fetched_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_raw_fetched ON raw_pages(fetched_at);

-- Cardmarket queue + results (Windows worker via Tailscale doet de scrape)
CREATE TABLE IF NOT EXISTS cardmarket_queue (
    item_id             TEXT PRIMARY KEY REFERENCES listings(item_id) ON DELETE CASCADE,
    url                 TEXT NOT NULL,
    grade               TEXT,          -- '10' / '9.5' / '9' etc — worker filtert hierop
    queued_at           TEXT NOT NULL,
    fetched_at          TEXT,
    listings_json       TEXT,
    error               TEXT
);
CREATE INDEX IF NOT EXISTS idx_cm_queue_pending ON cardmarket_queue(fetched_at) WHERE fetched_at IS NULL;

-- Alerts: gebruiker zet zoekterm + max/min prijs, matcht op elke nieuwe listing
CREATE TABLE IF NOT EXISTS alerts (
    alert_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    query       TEXT NOT NULL,     -- 'charizard 110' — split op whitespace, AND-match op title
    max_yen     INTEGER NOT NULL,  -- alleen items goedkoper dan dit
    min_yen     INTEGER,           -- optioneel, sluit rommel-prijs uit
    all_grades  INTEGER NOT NULL DEFAULT 0,  -- 0=alleen PSA10, 1=alle grades doorlaten
    created_by  TEXT,              -- discord user id (optioneel)
    created_at  TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts(active);
"""


def init_db(db_path=None) -> None:
    conn = _connect(db_path)
    try:
        conn.executescript(SCHEMA)
        # Idempotent migration for existing DBs.
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(listings)").fetchall()}
        if "title_en" not in existing:
            conn.execute("ALTER TABLE listings ADD COLUMN title_en TEXT")
        # Migrate: alerts.all_grades kolom toevoegen als hij ontbreekt
        alert_cols = {r["name"] for r in conn.execute("PRAGMA table_info(alerts)").fetchall()}
        if alert_cols and "all_grades" not in alert_cols:
            conn.execute("ALTER TABLE alerts ADD COLUMN all_grades INTEGER NOT NULL DEFAULT 0")
        # Migrate: worker-claim lock kolommen (voor 2 parallelle eBay-workers).
        if "locked_by" not in existing:
            conn.execute("ALTER TABLE listings ADD COLUMN locked_by TEXT")
        if "locked_at" not in existing:
            conn.execute("ALTER TABLE listings ADD COLUMN locked_at TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_listings_locked ON listings(locked_by, locked_at)"
        )
        # Perf: partial indexes voor het hete /api/items pad. De EXISTS-
        # subqueries daar deden json_extract per row → duur op 481k analyses.
        # Deze indexes materialiseren de "ROI-hit" en "CM-heeft-listings"
        # sets zodat de EXISTS puur een index-lookup wordt.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_analysis_roi_hit "
            "ON analysis(item_id) "
            "WHERE trap = 'roi' "
            "AND CAST(json_extract(result_json, '$.scenarios.avg3.winst_pct') AS REAL) >= 5"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_cm_queue_has_listings "
            "ON cardmarket_queue(item_id) "
            "WHERE listings_json IS NOT NULL AND listings_json != '[]'"
        )
        conn.commit()
    finally:
        conn.close()


LISTING_COLS = (
    "item_id", "title_jp", "title_en", "description_jp", "price_jpy", "price_eur",
    "shipping_jpy", "seller_id", "seller_name", "seller_rating",
    "category_path", "condition", "authenticated", "sold", "detail_url",
    "detail_scraped_at", "status", "extra_json",
)


def upsert_listing(row: dict, db_path=None) -> bool:
    """Insert of update. Return True als het een NIEUW item was (INSERT), False bij UPDATE."""
    if not row.get("item_id"):
        raise ValueError("item_id is required")
    _mode = os.environ.get("KENSA_WRITE_STORAGE", "sqlite")
    if _mode == "supabase":
        from storage_supabase.listings import upsert_listing as _sb_upsert
        return _sb_upsert(row)
    ts = now_iso()
    payload = {c: row.get(c) for c in LISTING_COLS}
    if payload.get("authenticated") is not None:
        payload["authenticated"] = int(bool(payload["authenticated"]))
    if payload.get("sold") is not None:
        payload["sold"] = int(bool(payload["sold"]))
    if payload.get("status") is None:
        payload["status"] = "new"
    extra = payload.get("extra_json")
    if extra is not None and not isinstance(extra, str):
        payload["extra_json"] = json.dumps(extra, ensure_ascii=False)

    conn = _connect(db_path)
    try:
        exists = conn.execute(
            "SELECT 1 FROM listings WHERE item_id = ?", (payload["item_id"],)
        ).fetchone()
        if exists:
            sets = [f"{c} = COALESCE(:{c}, {c})" for c in LISTING_COLS if c != "item_id"]
            sets.append("last_seen_at = :last_seen_at")
            payload["last_seen_at"] = ts
            conn.execute(
                f"UPDATE listings SET {', '.join(sets)} WHERE item_id = :item_id",
                payload,
            )
        else:
            payload["first_seen_at"] = ts
            payload["last_seen_at"] = ts
            cols = list(LISTING_COLS) + ["first_seen_at", "last_seen_at"]
            placeholders = ", ".join(f":{c}" for c in cols)
            conn.execute(
                f"INSERT INTO listings ({', '.join(cols)}) VALUES ({placeholders})",
                payload,
            )
        conn.commit()
        first_seen = payload.get("first_seen_at") or ts
        # Dual mode: SQLite-path net uitgevoerd, nu ook Supabase-native (skip oude sync).
        if _mode == "dual":
            try:
                from storage_supabase.listings import upsert_listing as _sb_upsert
                _sb_upsert(row)
            except Exception as e:
                iid = payload.get("item_id")
                print(f"[dual] upsert_listing supabase-write faalt voor {iid}: {e}", file=__import__('sys').stderr)
                # Duurzame recovery: enqueue idempotent upsert-POST in sync_retry queue.
                # Drain-cron (elke 2 min) probeert opnieuw met exponential backoff.
                if _sbs is not None:
                    _sb_row = {c: payload.get(c) for c in LISTING_COLS if payload.get(c) is not None}
                    _sb_row["first_seen_at"] = payload.get("first_seen_at") or first_seen
                    _sb_row["last_seen_at"] = payload["last_seen_at"]
                    _sbs._enqueue_post_retry(
                        "listings", [_sb_row],
                        "return=minimal,resolution=merge-duplicates",
                        f"{type(e).__name__}: {e}",
                    )
        else:
            _sync(_sbs.sync_upsert_listing, payload, first_seen, payload["last_seen_at"]) if _sbs else None
        return not bool(exists)  # True = nieuw item, False = update
    finally:
        conn.close()


def upsert_photos(item_id: str, photo_urls, db_path=None) -> int:
    """Insert photo URLs for a listing. Returns count of newly inserted rows."""
    if not photo_urls:
        return 0
    _mode = os.environ.get("KENSA_WRITE_STORAGE", "sqlite")
    if _mode == "supabase":
        from storage_supabase.photos import upsert_photos as _sb_upsert
        return _sb_upsert(item_id, photo_urls)
    conn = _connect(db_path)
    inserted = 0
    new_pairs: list[tuple[str, int]] = []
    try:
        max_idx = conn.execute(
            "SELECT COALESCE(MAX(photo_index), -1) AS m FROM photos WHERE item_id = ?",
            (item_id,),
        ).fetchone()["m"]
        next_idx = max_idx + 1
        for url in photo_urls:
            if not url:
                continue
            existing = conn.execute(
                "SELECT 1 FROM photos WHERE item_id = ? AND url_original = ?",
                (item_id, url),
            ).fetchone()
            if existing:
                continue
            conn.execute(
                "INSERT INTO photos (item_id, photo_index, url_original) VALUES (?, ?, ?)",
                (item_id, next_idx, url),
            )
            new_pairs.append((url, next_idx))
            next_idx += 1
            inserted += 1
        conn.commit()
        if new_pairs:
            if _mode == "dual":
                try:
                    from storage_supabase.photos import upsert_photos as _sb_upsert
                    _sb_upsert(item_id, photo_urls)
                except Exception as e:
                    print(f"[dual] upsert_photos supabase-write faalt voor {item_id}: {e}", file=__import__('sys').stderr)
                    # Duurzame recovery: enqueue elke (url, index)-paar als upsert-POST.
                    if _sbs is not None:
                        _rows = [{"item_id": item_id, "photo_url": u, "photo_index": idx}
                                 for (u, idx) in new_pairs]
                        _sbs._enqueue_post_retry(
                            "photos", _rows,
                            "return=minimal,resolution=merge-duplicates",
                            f"{type(e).__name__}: {e}",
                        )
            elif _sbs:
                _sync(_sbs.sync_photo_urls, item_id, new_pairs)
    finally:
        conn.close()
    return inserted


def _hash_path(h: str) -> Path:
    return PHOTO_ROOT / h[:2] / h[2:4] / f"{h}.jpg"


def download_photo_if_needed(photo_id: int, db_path=None) -> str | None:
    """Download bytes if not yet downloaded. Returns file_path or None on failure."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT photo_id, url_original, file_hash, file_path FROM photos WHERE photo_id = ?",
            (photo_id,),
        ).fetchone()
        if not row:
            return None
        if row["file_path"] and Path(row["file_path"]).exists():
            return row["file_path"]

        req = urllib.request.Request(row["url_original"], headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
        except Exception as e:
            print(f"download failed for {row['url_original']}: {e}")
            return None

        h = hashlib.sha256(data).hexdigest()
        path = _hash_path(h)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(data)

        width = height = None
        try:
            with Image.open(io.BytesIO(data)) as img:
                width, height = img.size
        except Exception:
            pass

        ts = now_iso()
        _mode = os.environ.get("KENSA_WRITE_STORAGE", "sqlite")
        if _mode == "supabase":
            from storage_supabase.photos import update_metadata as _sb_update_meta
            _sb_update_meta(photo_id, h, str(path), len(data), width, height, ts)
        else:
            conn.execute(
                """UPDATE photos SET file_hash = ?, file_path = ?, downloaded_at = ?,
                   size_bytes = ?, width = ?, height = ? WHERE photo_id = ?""",
                (h, str(path), ts, len(data), width, height, photo_id),
            )
            conn.commit()
        return str(path)
    finally:
        conn.close()


def mark_seen_in_search(item_ids, db_path=None) -> int:
    if not item_ids:
        return 0
    _mode = os.environ.get("KENSA_WRITE_STORAGE", "sqlite")
    if _mode == "supabase":
        from storage_supabase.listings_seen import mark_seen as _sb_mark_seen
        return _sb_mark_seen(item_ids)
    ts = now_iso()
    conn = _connect(db_path)
    try:
        cur = conn.executemany(
            "UPDATE listings SET last_seen_at = ? WHERE item_id = ?",
            [(ts, iid) for iid in item_ids],
        )
        conn.commit()
        if _mode == "dual":
            try:
                from storage_supabase.listings_seen import mark_seen as _sb_mark_seen
                _sb_mark_seen(item_ids)
            except Exception as e:
                print(f"[dual] mark_seen supabase-write faalt: {e}", file=__import__('sys').stderr)
                # Duurzame recovery: enqueue last_seen_at upsert per item.
                if _sbs is not None:
                    _rows = [{"item_id": iid, "last_seen_at": ts} for iid in item_ids]
                    _sbs._enqueue_post_retry(
                        "listings", _rows,
                        "return=minimal,resolution=merge-duplicates",
                        f"{type(e).__name__}: {e}",
                    )
        else:
            _sync(_sbs.sync_mark_seen, list(item_ids), ts) if _sbs else None
        return cur.rowcount
    finally:
        conn.close()


def save_raw_page(url: str, page_type: str, html: str, db_path=None) -> int:
    if page_type not in ("search", "detail"):
        raise ValueError("page_type must be 'search' or 'detail'")
    blob = gzip.compress(html.encode("utf-8"))
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO raw_pages (url, page_type, html_gz, fetched_at) VALUES (?, ?, ?, ?)",
            (url, page_type, blob, now_iso()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def record_cert_sighting(cert: str, item_id: str, seller_hint: str = None, db_path=None) -> dict:
    """Log dat cert `cert` gezien is bij item `item_id`. Returns {times_seen, first_seen_at}."""
    if not cert or not item_id:
        return {"times_seen": 0, "first_seen_at": None}
    _mode = os.environ.get("KENSA_WRITE_STORAGE", "sqlite")
    if _mode == "supabase":
        from storage_supabase.cert_sightings import record_sighting as _sb_record
        return _sb_record(cert, item_id, seller_hint)
    ts = now_iso()
    conn = _connect(db_path)
    try:
        conn.execute(
            """INSERT OR IGNORE INTO cert_sightings (cert, item_id, seller_hint, seen_at)
               VALUES (?, ?, ?, ?)""",
            (cert, item_id, seller_hint, ts),
        )
        conn.commit()
        if _mode == "dual":
            try:
                from storage_supabase.cert_sightings import record_sighting as _sb_record
                _sb_record(cert, item_id, seller_hint)
            except Exception as e:
                print(f"[dual] cert_sighting supabase-write faalt: {e}", file=__import__('sys').stderr)
                # Duurzame recovery: enqueue upsert-POST voor cert_sighting.
                if _sbs is not None:
                    _row = {"cert": cert, "item_id": item_id, "seen_at": ts}
                    if seller_hint is not None:
                        _row["seller_hint"] = seller_hint
                    _sbs._enqueue_post_retry(
                        "cert_sightings", [_row],
                        "return=minimal,resolution=merge-duplicates",
                        f"{type(e).__name__}: {e}",
                    )
        else:
            _sync(_sbs.sync_cert_sighting, cert, item_id, seller_hint, ts) if _sbs else None
        row = conn.execute(
            """SELECT COUNT(*) AS c, MIN(seen_at) AS first_seen_at
               FROM cert_sightings WHERE cert = ?""",
            (cert,),
        ).fetchone()
        return {"times_seen": row["c"], "first_seen_at": row["first_seen_at"]}
    finally:
        conn.close()


def cleanup_expired_photos(days: int = 7, db_path=None) -> dict:
    """Delete photo files whose listing hasn't been seen in `days` days.
    URLs + metadata stay; only bytes get deleted."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = _connect(db_path)
    deleted_files = 0
    freed_bytes = 0
    try:
        rows = conn.execute(
            """SELECT p.photo_id, p.file_path, p.file_hash, p.size_bytes
               FROM photos p JOIN listings l ON p.item_id = l.item_id
               WHERE l.last_seen_at < ? AND p.file_path IS NOT NULL""",
            (cutoff,),
        ).fetchall()
        for row in rows:
            path = Path(row["file_path"]) if row["file_path"] else None
            still_referenced = conn.execute(
                """SELECT 1 FROM photos p JOIN listings l ON p.item_id = l.item_id
                   WHERE p.file_hash = ? AND l.last_seen_at >= ? LIMIT 1""",
                (row["file_hash"], cutoff),
            ).fetchone()
            if not still_referenced and path and path.exists():
                try:
                    freed_bytes += path.stat().st_size
                    path.unlink()
                    deleted_files += 1
                except Exception as e:
                    print(f"cleanup could not delete {path}: {e}")
            conn.execute(
                """UPDATE photos SET file_path = NULL, downloaded_at = NULL,
                   size_bytes = NULL, width = NULL, height = NULL WHERE photo_id = ?""",
                (row["photo_id"],),
            )
        conn.commit()
    finally:
        conn.close()
    return {"deleted_files": deleted_files, "freed_bytes": freed_bytes}


def cleanup_raw_pages(days: int = 7, db_path=None) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = _connect(db_path)
    try:
        cur = conn.execute("DELETE FROM raw_pages WHERE fetched_at < ?", (cutoff,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def db_stats(db_path=None) -> dict:
    conn = _connect(db_path)
    try:
        stats = {}
        for table in ("listings", "photos", "user_verdicts", "analysis", "raw_pages"):
            stats[table] = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
        stats["photos_downloaded"] = conn.execute(
            "SELECT COUNT(*) AS c FROM photos WHERE file_path IS NOT NULL"
        ).fetchone()["c"]
        stats["photos_dir_bytes"] = _dir_size(PHOTO_ROOT)
        db_file = Path(db_path or DB_PATH)
        stats["db_file_bytes"] = db_file.stat().st_size if db_file.exists() else 0
        return stats
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(json.dumps(db_stats(), indent=2))
