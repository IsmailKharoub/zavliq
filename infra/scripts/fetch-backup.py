#!/usr/bin/env python3
"""Transfer the latest encrypted backup once, verify it, and keep S3 private."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

MANIFEST_PROGRAM = '''import json,re
from pathlib import Path
root=Path('/var/backups/zavliq')
files=sorted(p for p in root.iterdir() if p.is_file() and not p.is_symlink() and re.fullmatch(r'zavliq-[0-9]{8}T[0-9]{6}Z\\.tar\\.age',p.name))
if not files:raise SystemExit('No encrypted backup available')
p=files[-1]
checksum=p.with_name(p.name+'.sha256')
if checksum.is_symlink():raise SystemExit('Unexpected checksum link')
print(json.dumps({'name':p.name,'bytes':p.stat().st_size,'sha256':checksum.read_text().split()[0]}))
'''


TRANSFER_SECONDS = 540  # Workflow transfer step is ten minutes; cleanup has its own budget.


class Deadline:
    def __init__(self, seconds=TRANSFER_SECONDS):
        self.end = time.monotonic() + seconds

    def remaining(self, maximum=None):
        value = self.end - time.monotonic()
        if value <= 0:
            raise TimeoutError('BACKUP_TRANSFER_TIMEOUT')
        return min(value, maximum) if maximum is not None else value


def command(argv, deadline, maximum, **kwargs):
    if argv[0] == 'aws':
        argv = [*argv, '--cli-connect-timeout', '5', '--cli-read-timeout', '30', '--no-cli-pager']
        kwargs['env'] = {**os.environ, 'AWS_MAX_ATTEMPTS': '2'}
    if not kwargs.get('capture_output'):
        kwargs.setdefault('stderr', subprocess.PIPE)
    return subprocess.run(argv, timeout=deadline.remaining(maximum), **kwargs)


def ssh_command(config):
    return ['ssh', '-F', str(config), '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=4', 'zavliq']


def validate_manifest(value, now=None):
    if not isinstance(value, dict) or set(value) != {'name', 'bytes', 'sha256'}:
        raise ValueError('Invalid backup manifest fields')
    if not isinstance(value['name'], str) or not re.fullmatch(r'zavliq-[0-9]{8}T[0-9]{6}Z\.tar\.age', value['name']):
        raise ValueError('Invalid backup filename')
    if type(value['bytes']) is not int or not 1 <= value['bytes'] <= 80 * 1024 ** 3:
        raise ValueError('Backup size is outside the host storage bounds')
    if not isinstance(value['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', value['sha256']):
        raise ValueError('Invalid backup checksum')
    created = dt.datetime.strptime(value['name'][7:-8], '%Y%m%dT%H%M%SZ').replace(tzinfo=dt.timezone.utc)
    age = ((now or dt.datetime.now(dt.timezone.utc)) - created).total_seconds()
    if not -300 <= age <= 86400:
        raise ValueError('Latest backup is stale or its timestamp is in the future')
    return value


def matching_object(bucket, key, digest, deadline=None):
    deadline = deadline or Deadline()
    # A prefix-scoped ListBucket grant can yield403 for HEAD on a missing key.
    # Explicitly list the allowed prefix before requesting object metadata.
    listing = command(['aws', 's3api', 'list-objects-v2', '--bucket', bucket, '--prefix', key, '--max-keys', '1', '--output', 'json'], deadline, 45, capture_output=True, text=True)
    if listing.returncode:
        raise RuntimeError('Cannot inspect backup object; verify the deployment role and bucket')
    if not any(item['Key'] == key for item in json.loads(listing.stdout).get('Contents', [])):
        return False
    result = command(['aws', 's3api', 'head-object', '--bucket', bucket, '--key', key, '--output', 'json'], deadline, 45, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('Cannot inspect backup object; verify the deployment role and bucket')
    return json.loads(result.stdout).get('Metadata', {}).get('sha256') == digest


def main(args, deadline=None):
    deadline = deadline or Deadline()
    os.umask(0o077)
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest = command(ssh_command(args.ssh_config) + ['sudo python3 -'], deadline, 60,
                       input=MANIFEST_PROGRAM, capture_output=True, text=True, check=True)
    info = validate_manifest(json.loads(manifest.stdout))
    name, digest = info['name'], info['sha256']
    key = 'daily/' + name
    existing = matching_object(args.bucket, key, digest, deadline)
    if not existing:
        if shutil.disk_usage(directory).free < info['bytes'] + 1024 ** 3:
            raise RuntimeError('Runner has insufficient free disk for this encrypted backup')
        destination = directory / name
        with destination.open('xb') as output:
            # The filename is a validated timestamp, never shell syntax.
            command(ssh_command(args.ssh_config) + ['sudo cat /var/backups/zavliq/' + name],
                    deadline, 360, stdout=output, check=True)
        calculated = hashlib.sha256()
        with destination.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                deadline.remaining()
                calculated.update(chunk)
        if destination.stat().st_size != info['bytes'] or calculated.hexdigest() != digest:
            raise RuntimeError('Encrypted backup size/checksum mismatch; no upload performed')
        command(['aws', 's3', 'cp', str(destination), 's3://' + args.bucket + '/' + key, '--sse', 'AES256', '--metadata', 'sha256=' + digest, '--only-show-errors'], deadline, 360, check=True)
        if not matching_object(args.bucket, key, digest, deadline):
            raise RuntimeError('Uploaded backup metadata could not be verified')
    checksum = directory / (name + '.sha256')
    checksum.write_text(digest + '  ' + name + '\n')
    command(['aws', 's3', 'cp', str(checksum), 's3://' + args.bucket + '/' + key + '.sha256', '--sse', 'AES256', '--only-show-errors'], deadline, 60, check=True)
    deadline.remaining()
    print(json.dumps({'status': 'already_off_host' if existing else 'uploaded', 'backup': name, 'archive_bytes_transferred': 0 if existing else info['bytes'], 'checksum_verified': True}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh-config', type=Path, required=True)
    parser.add_argument('--bucket', required=True)
    parser.add_argument('--directory', type=Path, required=True)
    try:
        main(parser.parse_args())
    except Exception as error:
        # Never echo remote output, environment credentials, or a subprocess traceback.
        code = 'BACKUP_TRANSFER_TIMEOUT' if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)) else 'BACKUP_TRANSFER_FAILED'
        print(json.dumps({'ok': False, 'code': code, 'error_class': type(error).__name__,
                          'action': 'Check backup transfer and SSH cleanup status before retrying.'}))
        raise SystemExit(1)
