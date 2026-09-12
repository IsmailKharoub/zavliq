#!/usr/bin/env python3
"""Content-free operational checks. Never retrieves messages or credentials."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument('--origin', required=True)
parser.add_argument('--disk', default='/')
parser.add_argument('--backup-directory')
parser.add_argument('--output')
args = parser.parse_args()
checks = []
for path in ('/health', '/_matrix/client/versions', '/.well-known/matrix/client'):
    started = time.monotonic()
    try:
        with urllib.request.urlopen(args.origin.rstrip('/') + path, timeout=10) as r:
            payload = json.load(r)
            if not isinstance(payload, dict): raise ValueError('invalid response type')
        checks.append({'check': path, 'ok': True, 'latency_ms': round((time.monotonic()-started)*1000)})
    except Exception as exc:
        checks.append({'check': path, 'ok': False, 'error': type(exc).__name__})
disk = shutil.disk_usage(args.disk)
used = round(100 * disk.used / disk.total, 1)
checks.append({'check': 'disk', 'ok': used < 80, 'used_percent': used})
if args.backup_directory:
    directory = Path(args.backup_directory)
    backups = list(directory.glob('zavliq-*.tar.age'))
    age = time.time() - max((p.stat().st_mtime for p in backups), default=0)
    checks.append({'check': 'backup_age', 'ok': age <= 86400, 'age_hours': round(age / 3600, 1)})
report = json.dumps({'ok': all(c['ok'] for c in checks), 'checked_at': int(time.time()), 'checks': checks}, separators=(',', ':'))
if args.output:
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.tmp')
    temporary.write_text(report)
    temporary.chmod(0o644)
    temporary.replace(target)
print(report)
sys.exit(0 if all(c['ok'] for c in checks) else 1)
