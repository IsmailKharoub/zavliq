#!/usr/bin/env python3
"""Expire media by creation time; Synapse's native retention uses last access."""
import json
from pathlib import Path
import time
import urllib.parse
import urllib.request
import psycopg2
import yaml

config = yaml.safe_load(Path('/data/homeserver.yaml').read_text())
token = Path('/bootstrap/admin_token').read_text().strip()
args = {k: v for k, v in config['database']['args'].items() if not k.startswith('cp_')}
cutoff = int(time.time() * 1000) - 30 * 86_400_000
removed = 0
with psycopg2.connect(**args) as conn:
    # No application SQL writes: Synapse performs each deletion and updates metadata.
    with conn.cursor() as cursor:
        cursor.execute('SELECT media_id FROM local_media_repository WHERE created_ts < %s ORDER BY created_ts LIMIT 1000', (cutoff,))
        expired = [row[0] for row in cursor.fetchall()]
for media_id in expired:
    url = 'http://synapse:8008/_synapse/admin/v1/media/' + urllib.parse.quote(config['server_name'], safe='') + '/' + urllib.parse.quote(media_id, safe='')
    request = urllib.request.Request(url, method='DELETE', headers={'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.load(response)
    removed += result.get('total', 0)
print(json.dumps({'operation': 'media_retention', 'expired_deleted': removed, 'batch_size': len(expired)}))
