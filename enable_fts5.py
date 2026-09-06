#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""One-shot: FTS5-index over listings.title_jp/title_en aanmaken + triggers
zodat insert/update/delete op listings automatisch worden gesynct.

Draai 1x: `python3 enable_fts5.py`. Idempotent — mag opnieuw draaien.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

DB = Path("/home/pi/.openclaw/workspace/agents/kensa/kensa.db")


def main() -> int:
    if not DB.exists():
        print(f"[err] db niet gevonden: {DB}", file=sys.stderr)
        return 1
    conn = sqlite3.connect(str(DB))
    try:
        # Check of FTS5 überhaupt beschikbaar is
        try:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)")
            conn.execute("DROP TABLE _fts5_probe")
        except sqlite3.OperationalError as e:
            print(f"[err] SQLite build heeft geen FTS5: {e}", file=sys.stderr)
            return 2

        print("[info] FTS5-tabel + triggers aanmaken…")
        conn.executescript(
            """
            -- Contentless FTS5 gekoppeld aan listings.rowid.
            CREATE VIRTUAL TABLE IF NOT EXISTS listings_fts USING fts5(
              title_jp,
              title_en,
              content='listings',
              content_rowid='rowid',
              tokenize='unicode61 remove_diacritics 2'
            );

            -- Triggers: sync bij INSERT / UPDATE / DELETE.
            DROP TRIGGER IF EXISTS listings_ai;
            CREATE TRIGGER listings_ai AFTER INSERT ON listings BEGIN
              INSERT INTO listings_fts(rowid, title_jp, title_en)
              VALUES (NEW.rowid,
                      COALESCE(NEW.title_jp, ''),
                      COALESCE(NEW.title_en, ''));
            END;

            DROP TRIGGER IF EXISTS listings_ad;
            CREATE TRIGGER listings_ad AFTER DELETE ON listings BEGIN
              INSERT INTO listings_fts(listings_fts, rowid, title_jp, title_en)
              VALUES ('delete', OLD.rowid,
                      COALESCE(OLD.title_jp, ''),
                      COALESCE(OLD.title_en, ''));
            END;

            DROP TRIGGER IF EXISTS listings_au;
            CREATE TRIGGER listings_au AFTER UPDATE OF title_jp, title_en ON listings BEGIN
              INSERT INTO listings_fts(listings_fts, rowid, title_jp, title_en)
              VALUES ('delete', OLD.rowid,
                      COALESCE(OLD.title_jp, ''),
                      COALESCE(OLD.title_en, ''));
              INSERT INTO listings_fts(rowid, title_jp, title_en)
              VALUES (NEW.rowid,
                      COALESCE(NEW.title_jp, ''),
                      COALESCE(NEW.title_en, ''));
            END;
            """
        )
        conn.commit()

        # Vul FTS5 met bestaande data (alleen als leeg).
        cur = conn.execute("SELECT COUNT(*) FROM listings_fts")
        already = cur.fetchone()[0]
        cur = conn.execute("SELECT COUNT(*) FROM listings")
        total = cur.fetchone()[0]
        if already >= total:
            print(f"[info] FTS5 al gevuld ({already} rows) — skip populate.")
        else:
            print(f"[info] FTS5 populate: {total - already} rows te indexeren…")
            t0 = time.time()
            conn.execute(
                """
                INSERT INTO listings_fts(rowid, title_jp, title_en)
                SELECT rowid, COALESCE(title_jp, ''), COALESCE(title_en, '')
                FROM listings
                WHERE rowid NOT IN (SELECT rowid FROM listings_fts)
                """
            )
            conn.commit()
            print(f"[info] klaar in {time.time() - t0:.1f}s")

        # Optimize
        try:
            conn.execute("INSERT INTO listings_fts(listings_fts) VALUES ('optimize')")
            conn.commit()
            print("[info] FTS5 geoptimaliseerd.")
        except Exception as e:
            print(f"[warn] optimize failed (niet kritiek): {e}")

        # Sanity check
        cur = conn.execute("SELECT COUNT(*) FROM listings_fts")
        print(f"[ok] FTS5-tabel bevat nu {cur.fetchone()[0]} rows.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
