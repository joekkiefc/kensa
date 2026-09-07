#!/usr/bin/env python3
"""Detecteer PayPay-Flea (z*) items die stuck zitten in detail-queue (>6u geen detail)
en zet 1x retry-flag met timestamp. 30 min later markeert detail_dead_marker ze als
'dead' als ze nog steeds geen detail hebben. Run elk uur via cron.

Sinds cutover #4 (2026-09-07) leest én schrijft dit script Supabase: de Pi-mirror
krijgt geen detail-writes meer, dus op Pi-data zouden ALLE z-items stuck lijken."""
import datetime
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SECRETS = Path('/home/pi/.openclaw/secrets.json')
STUCK_HOURS = 6


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


def set_retry_flag_supabase():
    url, key = _sb()
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(hours=STUCK_HOURS)).isoformat()
    qs = urllib.parse.urlencode({
        'select': 'item_id,extra_json',
        'item_id': 'like.z*',
        'detail_scraped_at': 'is.null',
        'status': 'eq.new',
        'first_seen_at': f'lt.{cutoff}',
        'limit': '500',
    })
    rows = _req(url, key, f'listings?{qs}')
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    marked = []
    for row in rows:
        # extra_json is jsonb in Supabase: lezen geeft direct een dict.
        raw = row.get('extra_json')
        if isinstance(raw, str):
            try:
                data = json.loads(raw) if raw else {}
            except Exception:
                data = {}
        else:
            data = raw or {}
        if data.get('detail_retry_attempted'):
            continue
        data['detail_retry_attempted'] = 1
        data['detail_retry_at'] = now_iso
        # detail_scraped_at=is.null als PATCH-conditie: kreeg het item nét detail,
        # dan matcht de update 0 rijen en overschrijven we niks.
        upd_qs = urllib.parse.urlencode({
            'item_id': f'eq.{row["item_id"]}',
            'detail_scraped_at': 'is.null',
        })
        res = _req(url, key, f'listings?{upd_qs}', method='PATCH',
                   body={'extra_json': data})
        if res:
            marked.append(row['item_id'])
    return marked


if __name__ == '__main__':
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    marked = set_retry_flag_supabase()
    print(f'[{ts}] detail-stuck-detector: {len(marked)} z-items retry-flag gezet (supabase)')
