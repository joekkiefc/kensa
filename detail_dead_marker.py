#!/usr/bin/env python3
"""Markeer PayPay-Flea (z*) items als 'dead' als retry-1x-flag gezet is en 30 min
zonder detail. Voorkomt eeuwige energie op stukke items. Run elke 15 min via cron."""
import datetime, json, sqlite3, urllib.parse, urllib.request
from pathlib import Path

DB = Path('/home/pi/.openclaw/workspace/agents/kensa/kensa.db')
SECRETS = Path('/home/pi/.openclaw/secrets.json')
RETRY_GRACE_MIN = 30

def mark_dead_pi():
    conn = sqlite3.connect(str(DB))
    cur = conn.execute(
        "SELECT item_id, extra_json FROM listings "
        "WHERE item_id LIKE 'z%' AND detail_scraped_at IS NULL "
        "AND status='new' "
        "AND extra_json LIKE '%detail_retry_attempted%'"
    )
    dead = []
    now = datetime.datetime.now(datetime.timezone.utc)
    grace = datetime.timedelta(minutes=RETRY_GRACE_MIN)
    for iid, ej in cur.fetchall():
        try:
            data = json.loads(ej) if ej else {}
        except Exception:
            data = {}
        if not data.get('detail_retry_attempted'):
            continue
        retry_at = data.get('detail_retry_at')
        if retry_at:
            try:
                if (now - datetime.datetime.fromisoformat(retry_at)) < grace:
                    continue
            except Exception:
                pass
        dead.append(iid)
    if dead:
        q = ','.join('?' for _ in dead)
        conn.execute(f"UPDATE listings SET status='dead' WHERE item_id IN ({q})", dead)
        conn.commit()
    conn.close()
    return dead

def mark_dead_supabase(item_ids):
    if not item_ids:
        return 0
    sb = json.loads(SECRETS.read_text())['supabase']
    url = sb['url']
    key = sb.get('service_role_key') or sb.get('service_key') or sb.get('key')
    qs = 'item_id=in.(' + ','.join(item_ids) + ')'
    req = urllib.request.Request(
        f'{url}/rest/v1/listings?{qs}',
        data=b'{"status":"dead"}',
        method='PATCH',
        headers={
            'apikey': key, 'Authorization': f'Bearer {key}',
            'Content-Profile': 'kensa', 'Accept-Profile': 'kensa',
            'Content-Type': 'application/json', 'Prefer': 'return=representation',
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read().decode()
    return len(json.loads(body)) if body else 0

if __name__ == '__main__':
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    dead = mark_dead_pi()
    try:
        n_sb = mark_dead_supabase(dead)
    except Exception as e:
        n_sb = f'ERROR: {e}'
    print(f'[{ts}] detail-dead-marker: pi={len(dead)} supabase={n_sb}')
