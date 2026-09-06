#!/usr/bin/env python3
"""Detecteer PayPay-Flea (z*) items die stuck zitten in detail-queue (>6u geen detail)
en zet 1x retry-flag met timestamp. 30 min later markeert detail_dead_marker ze als
'dead' als ze nog steeds geen detail hebben. Run elke uur via cron."""
import datetime, json, sqlite3, urllib.parse, urllib.request
from pathlib import Path

DB = Path('/home/pi/.openclaw/workspace/agents/kensa/kensa.db')
SECRETS = Path('/home/pi/.openclaw/secrets.json')
STUCK_HOURS = 6

def set_retry_flag_pi():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT item_id, extra_json FROM listings "
        "WHERE item_id LIKE 'z%' AND detail_scraped_at IS NULL "
        "AND status='new' "
        "AND datetime(first_seen_at) < datetime('now', ?)",
        (f'-{STUCK_HOURS} hours',),
    )
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    marked = []
    for row in cur.fetchall():
        try:
            data = json.loads(row['extra_json']) if row['extra_json'] else {}
        except Exception:
            data = {}
        if data.get('detail_retry_attempted'):
            continue
        data['detail_retry_attempted'] = 1
        data['detail_retry_at'] = now_iso
        conn.execute(
            "UPDATE listings SET extra_json=? WHERE item_id=?",
            (json.dumps(data), row['item_id']),
        )
        marked.append(row['item_id'])
    conn.commit()
    conn.close()
    return marked

if __name__ == '__main__':
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    marked = set_retry_flag_pi()
    print(f'[{ts}] detail-stuck-detector: {len(marked)} z-items retry-flag gezet')
