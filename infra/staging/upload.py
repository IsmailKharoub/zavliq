#!/usr/bin/env python3
"""Host-side bounded S3 upload. Signed forms stay private; no AWS SDK/key on host."""
import argparse
import copy
import datetime as dt
import hashlib
import http.client
import io
import json
from pathlib import Path
import re
import secrets
import sys
import time
from urllib.parse import urlsplit

SAFE_CODES = {
    'PRIVATE_CAPABILITY_REQUIRED', 'INVALID_CAPABILITY', 'CAPABILITY_EXPIRED_OR_INVALID',
    'INVALID_BUCKET', 'UPLOAD_SIZE_EXCEEDED', 'UNEXPECTED_UPLOAD_ENDPOINT',
    'UNEXPECTED_UPLOAD_POLICY', 'INVALID_UPLOAD_FIELD', 'UPLOAD_SOURCE_CHANGED',
    'UPLOAD_REJECTED', 'REGULAR_SOURCE_REQUIRED', 'HEALTH_REPORT_TOO_LARGE',
    'INVALID_HEALTH_REPORT', 'NO_CURRENT_BACKUP_SLOT', 'ENCRYPTED_SNAPSHOT_NAME_REQUIRED',
    'FRESH_SNAPSHOT_REQUIRED', 'AGE_ENCRYPTED_SNAPSHOT_REQUIRED', 'UPLOAD_DEADLINE_EXCEEDED',
}


def safe_error(error):
    value = error.args[0] if error.args else None
    code = value if isinstance(value, str) and value in SAFE_CODES else 'NETWORK_FAILURE' if isinstance(error, (OSError, http.client.HTTPException)) else 'INVALID_LOCAL_STATE'
    if code in {'CAPABILITY_EXPIRED_OR_INVALID', 'NO_CURRENT_BACKUP_SLOT'}:
        action = 'Install a newly issued bounded capability; keep the local encrypted snapshots.'
    elif code in {'UPLOAD_SIZE_EXCEEDED', 'HEALTH_REPORT_TOO_LARGE'}:
        action = 'Keep the source file; use the operator transfer path for an encrypted archive above this staging limit.'
    elif code in {'NETWORK_FAILURE', 'UPLOAD_REJECTED', 'UPLOAD_DEADLINE_EXCEEDED'}:
        action = 'Keep the local snapshot; verify S3 reachability and capability expiry, then retry within its lifetime.'
    else:
        action = 'Verify the private staging configuration and completed source file before retrying.'
    return {'code': code, 'action': action}


def load_capability(path, now):
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError('PRIVATE_CAPABILITY_REQUIRED')
    value = json.loads(path.read_text())
    if value.get('version') != 1 or not isinstance(value.get('issued_at'), int) or not isinstance(value.get('expires_at'), int):
        raise ValueError('INVALID_CAPABILITY')
    if not 0 < value['expires_at'] - value['issued_at'] <= 172800 or not value['issued_at'] <= now < value['expires_at']:
        raise ValueError('CAPABILITY_EXPIRED_OR_INVALID')
    if not re.fullmatch(r'zavliq-staging-backups-[0-9]{12}', value.get('bucket', '')):
        raise ValueError('INVALID_BUCKET')
    return value


def upload(capability, form, stream, size):
    deadline = time.monotonic() + 180
    if not 0 < size <= form['maximum_bytes']:
        raise ValueError('UPLOAD_SIZE_EXCEEDED')
    parsed = urlsplit(form['url'])
    bucket = capability['bucket']
    if parsed.scheme != 'https' or parsed.hostname not in (f'{bucket}.s3.amazonaws.com', f'{bucket}.s3.us-east-1.amazonaws.com') or parsed.port or parsed.username or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('UNEXPECTED_UPLOAD_ENDPOINT')
    fields = form['fields']
    if fields.get('key') != form['key'] or fields.get('x-amz-server-side-encryption') != 'AES256':
        raise ValueError('UNEXPECTED_UPLOAD_POLICY')
    boundary = 'zavliq-' + secrets.token_hex(16)
    prefix = bytearray()
    for name, value in fields.items():
        if not isinstance(value, str) or re.search(r'[\r\n"]', name) or re.search(r'[\r\n]', value):
            raise ValueError('INVALID_UPLOAD_FIELD')
        prefix.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    prefix.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="payload"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode())
    suffix = f'\r\n--{boundary}--\r\n'.encode()
    connection = http.client.HTTPSConnection(parsed.hostname, timeout=30)

    def remaining_timeout():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('UPLOAD_DEADLINE_EXCEEDED')
        connection.timeout = min(30, remaining)
        if connection.sock:
            connection.sock.settimeout(connection.timeout)

    try:
        remaining_timeout()
        connection.putrequest('POST', '/')
        connection.putheader('Content-Type', f'multipart/form-data; boundary={boundary}')
        connection.putheader('Content-Length', str(len(prefix) + size + len(suffix)))
        connection.endheaders()
        connection.send(prefix)
        remaining = size
        while remaining:
            remaining_timeout()
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError('UPLOAD_SOURCE_CHANGED')
            connection.send(chunk)
            remaining -= len(chunk)
        connection.send(suffix)
        remaining_timeout()
        response = connection.getresponse()
        response.read(65536)
        if response.status not in (200, 201, 204):
            raise ValueError('UPLOAD_REJECTED')
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('kind', choices=['health', 'backup'])
    parser.add_argument('file', type=Path)
    args = parser.parse_args()
    try:
        now = int(time.time())
        capability = load_capability(args.config, now)
        if args.file.is_symlink() or not args.file.is_file():
            raise ValueError('REGULAR_SOURCE_REQUIRED')
        if args.kind == 'health':
            if args.file.stat().st_size > 60000:
                raise ValueError('HEALTH_REPORT_TOO_LARGE')
            report = json.loads(args.file.read_text())
            if not isinstance(report.get('checked_at'), int) or not isinstance(report.get('ok'), bool):
                raise ValueError('INVALID_HEALTH_REPORT')
            report.update(capability_issued_at=capability['issued_at'], capability_expires_at=capability['expires_at'])
            content = json.dumps(report, separators=(',', ':')).encode()
            upload(capability, capability['health'], io.BytesIO(content), len(content))
        else:
            form = next((copy.deepcopy(slot) for slot in capability['backups'] if slot['not_before'] <= now < slot['not_after']), None)
            if form is None:
                raise ValueError('NO_CURRENT_BACKUP_SLOT')
            matched = re.fullmatch(r'zavliq-(\d{8}T\d{6}Z)\.tar\.age', args.file.name)
            if not matched:
                raise ValueError('ENCRYPTED_SNAPSHOT_NAME_REQUIRED')
            created = int(dt.datetime.strptime(matched[1], '%Y%m%dT%H%M%SZ').replace(tzinfo=dt.timezone.utc).timestamp())
            if not 0 <= now - created <= 3600:
                raise ValueError('FRESH_SNAPSHOT_REQUIRED')
            digest = hashlib.sha256()
            with args.file.open('rb') as stream:
                if stream.read(22) != b'age-encryption.org/v1\n':
                    raise ValueError('AGE_ENCRYPTED_SNAPSHOT_REQUIRED')
                stream.seek(0)
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
                form['fields']['x-amz-meta-sha256'] = digest.hexdigest()
                form['fields']['x-amz-meta-created-at'] = str(created)
                stream.seek(0)
                upload(capability, form, stream, args.file.stat().st_size)
        print(json.dumps({'operation': 'stage_' + args.kind + '_upload', 'ok': True, 'capability_expires_at': capability['expires_at']}))
    except Exception as error:
        # Do not print request fields, response bodies, URLs or raw exceptions.
        print(json.dumps({'operation': 'stage_' + args.kind + '_upload', 'ok': False, **safe_error(error)}), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
