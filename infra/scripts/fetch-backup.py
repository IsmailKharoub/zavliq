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


def matching_object(bucket, key, digest):
    # A prefix-scoped ListBucket grant can yield403 for HEAD on a missing key.
    # Explicitly list the allowed prefix before requesting object metadata.
    listing = subprocess.run(['aws', 's3api', 'list-objects-v2', '--bucket', bucket, '--prefix', key, '--max-keys', '1', '--output', 'json'], capture_output=True, text=True)
    if listing.returncode:
        raise RuntimeError('Cannot inspect backup object; verify the deployment role and bucket')
    if not any(item['Key'] == key for item in json.loads(listing.stdout).get('Contents', [])):
        return False
    result = subprocess.run(['aws', 's3api', 'head-object', '--bucket', bucket, '--key', key, '--output', 'json'], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('Cannot inspect backup object; verify the deployment role and bucket')
    return json.loads(result.stdout).get('Metadata', {}).get('sha256') == digest


def main(args):
    os.umask(0o077)
    directory = args.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest = subprocess.run(['ssh', '-F', str(args.ssh_config), 'zavliq', 'sudo python3 -'], input=MANIFEST_PROGRAM, capture_output=True, text=True, check=True)
    info = validate_manifest(json.loads(manifest.stdout))
    name, digest = info['name'], info['sha256']
    key = 'daily/' + name
    existing = matching_object(args.bucket, key, digest)
    if not existing:
        if shutil.disk_usage(directory).free < info['bytes'] + 1024 ** 3:
            raise RuntimeError('Runner has insufficient free disk for this encrypted backup')
        destination = directory / name
        with destination.open('xb') as output:
            # The filename is a validated timestamp, never shell syntax.
            subprocess.run(['ssh', '-F', str(args.ssh_config), 'zavliq', 'sudo cat /var/backups/zavliq/' + name], stdout=output, check=True)
        calculated = hashlib.sha256()
        with destination.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                calculated.update(chunk)
        if destination.stat().st_size != info['bytes'] or calculated.hexdigest() != digest:
            raise RuntimeError('Encrypted backup size/checksum mismatch; no upload performed')
        subprocess.run(['aws', 's3', 'cp', str(destination), 's3://' + args.bucket + '/' + key, '--sse', 'AES256', '--metadata', 'sha256=' + digest, '--only-show-errors'], check=True)
        if not matching_object(args.bucket, key, digest):
            raise RuntimeError('Uploaded backup metadata could not be verified')
    checksum = directory / (name + '.sha256')
    checksum.write_text(digest + '  ' + name + '\n')
    subprocess.run(['aws', 's3', 'cp', str(checksum), 's3://' + args.bucket + '/' + key + '.sha256', '--sse', 'AES256', '--only-show-errors'], check=True)
    print(json.dumps({'status': 'already_off_host' if existing else 'uploaded', 'backup': name, 'archive_bytes_transferred': 0 if existing else info['bytes'], 'checksum_verified': True}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh-config', type=Path, required=True)
    parser.add_argument('--bucket', required=True)
    parser.add_argument('--directory', type=Path, required=True)
    main(parser.parse_args())
