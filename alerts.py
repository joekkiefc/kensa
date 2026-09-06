"""Kensa alerts — matcht nieuwe listings tegen door de user gedefinieerde watches.

- CRUD via SQLite (`alerts` tabel)
- match_new(item): geeft lijst van getriggerde alerts terug
- post_alert(item, alert): stuurt Discord-melding via openclaw CLI

Aangeroepen door migrate_from_json.py na elke fresh insert.
"""

import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "kensa.db"
OPENCLAW_BIN = "/home/pi/.nvm/versions/node/v24.18.0/bin/openclaw"
CHANNEL_ID = "channel:1379489498835976424"  # #algemeen


def _conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def list_alerts(active_only: bool = True) -> list[dict]:
    conn = _conn()
    try:
        sql = "SELECT * FROM alerts"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY alert_id"
        return [dict(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def add_alert(query: str, max_yen: int, min_yen: int | None = None,
               all_grades: bool = False, created_by: str | None = None) -> int:
    now = datetime.now(timezone.utc).isoformat()
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO alerts (query, max_yen, min_yen, all_grades, created_by, created_at) VALUES (?,?,?,?,?,?)",
            (query.strip(), int(max_yen), int(min_yen) if min_yen else None,
             1 if all_grades else 0, created_by, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def delete_alert(alert_id: int) -> bool:
    conn = _conn()
    try:
        cur = conn.execute("UPDATE alerts SET active = 0 WHERE alert_id = ?", (alert_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


_PSA10_RE = re.compile(r"psa\s*10\b", re.IGNORECASE)
_PSA_ANY_RE = re.compile(r"psa\s*\d", re.IGNORECASE)


def _matches(item: dict, alert: dict) -> bool:
    """AND-match op title-tokens (lowercase, JP+EN combined) + prijsrange + PSA10 default."""
    price = item.get("price_jpy") or 0
    if price <= 0:
        return False
    if price > alert["max_yen"]:
        return False
    if alert.get("min_yen") and price < alert["min_yen"]:
        return False
    hay_raw = f"{item.get('title_jp') or ''}  {item.get('title_en') or ''}"
    hay = hay_raw.lower()
    # Default (all_grades=0): titel MOET "PSA10" of "PSA 10" bevatten, tenzij gebruiker
    # zelf al een PSA-grade in de query heeft gezet (dan respect user's keuze).
    if not alert.get("all_grades"):
        if not _PSA_ANY_RE.search(alert.get("query") or ""):
            if not _PSA10_RE.search(hay_raw):
                return False
    tokens = [t for t in re.split(r"\s+", (alert["query"] or "").lower()) if t]
    return all(t in hay for t in tokens)


def match_new(item: dict) -> list[dict]:
    """Return alle alerts die matchen voor dit item."""
    return [a for a in list_alerts(active_only=True) if _matches(item, a)]


def post_alert(item: dict, alert: dict) -> None:
    """Stuur Discord melding voor een matchende listing."""
    src = "PayPay" if (item.get("item_id") or "").startswith("z") else "Mercari"
    title = item.get("title_en") or item.get("title_jp") or "?"
    yen = item.get("price_jpy") or 0
    eur = item.get("price_eur") or 0
    detail = item.get("detail_url") or f"https://buyee.jp/mercari/item/{item['item_id']}"
    text = (
        f"🔔 **Alert #{alert['alert_id']} match** — query: `{alert['query']}`\n"
        f"**{title[:120]}**\n"
        f"¥{yen:,} · €{eur:.2f} · {src}\n"
        f"<{detail}>"
    )
    subprocess.run(
        [OPENCLAW_BIN, "message", "send", "--channel", "discord",
         "--target", CHANNEL_ID, "-m", text],
        check=False, timeout=30,
    )


def check_and_post(item: dict) -> int:
    """Match + post. Return aantal getriggerde alerts."""
    hits = match_new(item)
    for a in hits:
        try:
            post_alert(item, a)
        except Exception as e:
            print(f"  [alert] post fout: {e}")
    return len(hits)


if __name__ == "__main__":
    # Sanity: lijst alle alerts
    for a in list_alerts(active_only=False):
        active = "✓" if a["active"] else "✗"
        mn = f" (min ¥{a['min_yen']:,})" if a['min_yen'] else ""
        print(f"  #{a['alert_id']} [{active}] '{a['query']}' max ¥{a['max_yen']:,}{mn}")
