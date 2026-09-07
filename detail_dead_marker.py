#!/usr/bin/env python3
"""Markeer PayPay-Flea (z*) items als 'dead' als retry-1x-flag gezet is en 30 min
zonder detail. Voorkomt eeuwige energie op stukke items. Run elke 15 min via cron.

Sinds cutover #4 (2026-09-07) is Supabase de leesbron (Pi-mirror krijgt geen
detail-writes meer). Status 'dead' schrijven we op beide: Supabase is leidend,
de Pi-rij wordt meegezet zodat de bevroren mirror netjes blijft."""
import datetime
import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DB = Path('/home/pi/.openclaw/workspace/agents/kensa/kensa.db')
SECRETS = Path('/home/pi/.openclaw/secrets.json')
RETRY_GRACE_MIN = 30


def _sb():
    sb = json.loads(SECRETS.read_text())['supabase']
    key = sb.get('service_role_key') or sb.get('service_key') or sb.get('key')
    return sb['url'], key


def _req(url, key, path_qs, method='GET', body=None, attempts=3):
    req = urllib.request.Request(
        f'{url}/rest/v1/{path_qs}',
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            'apikey': key, 'Authorization': f'Bearer {key}',
            'Content-Profile': 'kensa', 'Accept-Profile': 'kensa',
            'Content-Type': 'application/json', 'Prefer': 'return=representation',
        },
    )
    # Transiente netwerk-hikjes naar Supabase (TLS-reset/timeout) komen dagelijks
    # incidenteel voor; kort hertesten i.p.v. de hele run laten crashen.
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                b = resp.read().decode()
            return json.loads(b) if b else []
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if i == attempts - 1:
                raise
            time.sleep(2)


def find_dead_supabase():
    url, key = _sb()
    # extra_json is jsonb: filter op de sleutel zelf (like werkt niet op jsonb).
    qs = urllib.parse.urlencode({
        'select': 'item_id,extra_json',
        'item_id': 'like.z*',
        'detail_scraped_at': 'is.null',
        'status': 'eq.new',
        'extra_json->>detail_retry_attempted': 'not.is.null',
        'limit': '500',
    })
    rows = _req(url, key, f'listings?{qs}')
    dead = []
    now = datetime.datetime.now(datetime.timezone.utc)
    grace = datetime.timedelta(minutes=RETRY_GRACE_MIN)
    for row in rows:
        raw = row.get('extra_json')
        if isinstance(raw, str):
            try:
                data = json.loads(raw) if raw else {}
            except Exception:
                data = {}
        else:
            data = raw or {}
        if not data.get('detail_retry_attempted'):
            continue
        retry_at = data.get('detail_retry_at')
        if retry_at:
            try:
                if (now - datetime.datetime.fromisoformat(retry_at)) < grace:
                    continue
            except Exception:
                pass
        dead.append(row['item_id'])
    return dead


def mark_dead_supabase(item_ids):
    if not item_ids:
        return 0
    url, key = _sb()
    # status=eq.new + detail_scraped_at=is.null als condities: een item dat
    # ondertussen tóch detail kreeg wordt server-side overgeslagen.
    qs = urllib.parse.urlencode({
        'item_id': 'in.(' + ','.join(item_ids) + ')',
        'status': 'eq.new',
        'detail_scraped_at': 'is.null',
    })
    res = _req(url, key, f'listings?{qs}', method='PATCH', body={'status': 'dead'})
    return len(res)


def mark_dead_pi(item_ids):
    if not item_ids:
        return 0
    conn = sqlite3.connect(str(DB))
    try:
        q = ','.join('?' for _ in item_ids)
        cur = conn.execute(
            f"UPDATE listings SET status='dead' WHERE item_id IN ({q})", item_ids)
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


if __name__ == '__main__':
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    dead = find_dead_supabase()
    n_sb = mark_dead_supabase(dead)
    try:
        n_pi = mark_dead_pi(dead)
    except Exception as e:
        n_pi = f'ERROR: {e}'
    print(f'[{ts}] detail-dead-marker: kandidaten={len(dead)} supabase={n_sb} pi={n_pi}')
