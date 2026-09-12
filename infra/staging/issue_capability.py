#!/usr/bin/env python3
"""Operator-only issuance of fixed, expiring S3 POST forms; never print forms."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import time

import boto3
from botocore.config import Config

HEALTH_BYTES = 64 * 1024
BACKUP_BYTES = 256 * 1024 * 1024
WINDOW_SECONDS = 48 * 3600


def issue(client, bucket, now):
    expiry = now + WINDOW_SECONDS

    def form(key, maximum, content_type, extra=False):
        fields = {'Content-Type': content_type, 'x-amz-server-side-encryption': 'AES256'}
        conditions = [{'Content-Type': content_type}, {'x-amz-server-side-encryption': 'AES256'}, ['content-length-range', 1, maximum]]
        if extra:
            fields.update({'x-amz-meta-sha256': '', 'x-amz-meta-created-at': ''})
            conditions.extend([['starts-with', '$x-amz-meta-sha256', ''], ['starts-with', '$x-amz-meta-created-at', '']])
        signed = client.generate_presigned_post(Bucket=bucket, Key=key, Fields=fields, Conditions=conditions, ExpiresIn=WINDOW_SECONDS)
        return {'key': key, 'maximum_bytes': maximum, **signed}

    slots = []
    start = now // 43200 * 43200
    while start < expiry:
        stamp = dt.datetime.fromtimestamp(start, dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        slots.append({'not_before': start, 'not_after': min(start + 43200, expiry), **form(f'daily/zavliq-{stamp}.tar.age', BACKUP_BYTES, 'application/octet-stream', True)})
        start += 43200
    return {'version': 1, 'bucket': bucket, 'issued_at': now, 'expires_at': expiry,
            'health': form('status/health.json', HEALTH_BYTES, 'application/json'), 'backups': slots}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bucket', required=True)
    parser.add_argument('--directory', required=True, type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r'zavliq-staging-backups-[0-9]{12}', args.bucket):
        parser.error('Only this project’s private staging backup bucket is supported.')
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = args.directory / 'capability.json'
    if output.exists():
        parser.error('Choose a new private directory; preserve the currently issued capability.')
    session = boto3.Session(region_name='us-east-1')
    credentials = session.get_credentials()
    if credentials is None or credentials.get_frozen_credentials().token or getattr(credentials, '_expiry_time', None):
        parser.error('The 48-hour window requires operator signing credentials that do not expire earlier.')
    client = session.client('s3', config=Config(signature_version='s3v4', s3={'us_east_1_regional_endpoint': 'regional'}))
    now = int(time.time())
    capability = issue(client, args.bucket, now)
    output.write_text(json.dumps(capability, separators=(',', ':')))
    output.chmod(0o600)
    summary = {'issued_at': now, 'expires_at': capability['expires_at'], 'window_hours': 48,
               'health_key': capability['health']['key'], 'health_maximum_bytes': HEALTH_BYTES,
               'backup_keys': [slot['key'] for slot in capability['backups']],
               'backup_maximum_bytes_each': BACKUP_BYTES,
               'maximum_distinct_backup_bytes': BACKUP_BYTES * len(capability['backups']),
               'host_can_list_or_read_bucket': False}
    (args.directory / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary))


if __name__ == '__main__':
    main()
