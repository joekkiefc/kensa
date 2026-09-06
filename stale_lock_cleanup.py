#!/usr/bin/env python3
"""Release locks ouder dan 20 min op Pi + Supabase. Run elke 30 min via cron."""
import datetime, json, sqlite3, sys, urllib.parse, urllib.request
from pathlib import Path

DB = Path('/home/pi/.openclaw/workspace/agents/kensa/kensa.db')
SECRETS = Path('/home/pi/.openclaw/secrets.json')
STALE_MIN = 20

def release_pi():
    conn = sqlite3.connect(str(DB))
    cur = conn.execute(
        "UPDATE listings SET locked_by=NULL, locked_at=NULL "
        "WHERE locked_by IS NOT NULL AND datetime(locked_at) < datetime('now', ?)",
        (f'-{STALE_MIN} minutes',),
    )
    n = cur.rowcount
    conn.commit()
    conn.close()
    return n

def release_supabase():
    sb = json.loads(SECRETS.read_text())['supabase']
    url = sb['url']
    key = sb.get('service_role_key') or sb.get('service_key') or sb.get('key')
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(minutes=STALE_MIN)).isoformat()
    req = urllib.request.Request(
        f'{url}/rest/v1/listings?locked_at=lt.{urllib.parse.quote(cutoff)}'
        f'&locked_by=not.is.null',
        data=b'{"locked_by":null,"locked_at":null}',
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
    n_pi = release_pi()
    try:
        n_sb = release_supabase()
    except Exception as e:
        n_sb = f'ERROR: {e}'
    print(f'[{ts}] stale-lock cleanup: pi={n_pi} supabase={n_sb}')
