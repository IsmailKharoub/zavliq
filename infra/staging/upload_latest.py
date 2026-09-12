#!/usr/bin/env python3
"""Upload the latest complete encrypted snapshot, without printing its contents."""
import argparse
from pathlib import Path
import subprocess
import sys

parser = argparse.ArgumentParser()
parser.add_argument('--config', default='/etc/zavliq/staging-capability.json')
parser.add_argument('--directory', default='/var/backups/zavliq', type=Path)
args = parser.parse_args()
files = [p for p in args.directory.glob('zavliq-*.tar.age') if p.is_file() and not p.is_symlink()]
if not files:
    raise SystemExit('No complete encrypted snapshot exists.')
latest = max(files, key=lambda p: p.stat().st_mtime)
sys.exit(subprocess.call(['python3', str(Path(__file__).with_name('upload.py')), '--config', args.config, 'backup', str(latest)]))
