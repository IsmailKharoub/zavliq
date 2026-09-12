#!/usr/bin/env python3
"""Minute health evidence; a soak starts only through the explicit image-bound marker."""
import argparse
import fcntl
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from begin_soak import snapshot
from upload import SAFE_CODES, safe_error


def upload_failure(stderr):
    """Retain a known code, never a subprocess's arbitrary text or action."""
    try:
        code = json.loads(stderr).get('code')
    except (ValueError, AttributeError):
        code = None
    if isinstance(code, str) and code in SAFE_CODES:
        return safe_error(ValueError(code))
    if code == 'NETWORK_FAILURE':
        return safe_error(OSError())
    return safe_error(ValueError())


def summarize(rows, marker, now):
    started = marker['started_at']
    samples = [row for row in rows if row['checked_at'] >= started]
    failures = sum(not row['ok'] for row in samples)
    times = [started] + [row['checked_at'] for row in samples] + [now]
    gaps = [b - a for a, b in zip(times, times[1:])]
    maximum_gap = max(gaps, default=0)
    continuity = all(0 <= gap <= 90 for gap in gaps)
    elapsed = max(0, now - started)
    complete = elapsed >= 86400 and len(samples) >= 1440 and not failures and continuity
    return {'scope': 'private-backend', 'started_at': started, 'checked_at': now,
            'expected_revision': marker['expected_revision'], 'elapsed_seconds': elapsed,
            'samples': len(samples), 'failed_samples': failures, 'maximum_gap_seconds': maximum_gap,
            'continuity_passed': continuity, 'complete': complete,
            'final_service_set': marker['final_service_set'],
            'final_service_soak_passed': complete and marker['final_service_set']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--origin', default='http://localhost:19180', choices=['http://localhost:19180', 'http://127.0.0.1:19180'])
    parser.add_argument('--directory', default='/var/lib/zavliq/staging-soak', type=Path)
    parser.add_argument('--capability', default='/etc/zavliq/staging-capability.json', type=Path)
    parser.add_argument('--healthcheck', default='/opt/zavliq/current/infra/scripts/healthcheck.py', type=Path)
    parser.add_argument('--output', default='/var/lib/zavliq/health.json', type=Path)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = (args.directory / 'probe.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    now = int(time.time())
    report = {'ok': False, 'checked_at': now, 'checks': []}
    marker = None
    try:
        result = subprocess.run(['python3', str(args.healthcheck), '--origin', args.origin, '--backup-directory', '/var/backups/zavliq'], capture_output=True, text=True, timeout=40)
        report = json.loads(result.stdout)
        report['ok'] = report.get('ok') is True and result.returncode == 0
        report['checked_at'] = now
        marker_path = args.directory / 'marker.json'
        marker = json.loads(marker_path.read_text()) if marker_path.exists() else None
        if marker:
            actual = snapshot(marker['project'], marker['expected_images'])
            same = all(actual[name]['image_id'] == expected['image_id'] and actual[name]['running'] and actual[name]['health'] in ('', 'healthy') for name, expected in marker['expected_images'].items())
            report['checks'].append({'check': 'bound_service_images', 'ok': same})
            report['ok'] = report['ok'] and same
    except Exception as error:
        report.update(ok=False, error_class=type(error).__name__)
    # Health and image-check failures must enter the durable journal too.
    try:
        with sqlite3.connect(args.directory / 'health.sqlite3') as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS samples (id INTEGER PRIMARY KEY, checked_at INTEGER NOT NULL, ok INTEGER NOT NULL, details TEXT NOT NULL)')
            db.execute('INSERT INTO samples(checked_at,ok,details) VALUES (?,?,?)', (now, int(report['ok']), json.dumps(report, separators=(',', ':'))))
            db.commit()
            if marker:
                rows = [{'checked_at': row[0], 'ok': bool(row[1])} for row in db.execute('SELECT checked_at,ok FROM samples WHERE checked_at >= ? ORDER BY id', (marker['started_at'],))]
                summary = summarize(rows, marker, now)
                report['soak'] = summary
                # A gap or failed sample remains visible until an explicit new soak.
                report['ok'] = report['ok'] and not summary['failed_samples'] and summary['continuity_passed']
                temporary_summary = args.directory / 'summary.tmp'
                temporary_summary.write_text(json.dumps(summary, indent=2) + '\n')
                temporary_summary.replace(args.directory / 'summary.json')
    except Exception as error:
        report.update(ok=False, error_class=type(error).__name__)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix('.tmp')
    temporary.write_text(json.dumps(report, separators=(',', ':')))
    temporary.chmod(0o644)
    temporary.replace(args.output)
    uploader = Path(__file__).with_name('upload.py')
    uploaded = subprocess.run(['python3', str(uploader), '--config', str(args.capability), 'health', str(args.output)], capture_output=True, text=True, timeout=185)
    result = {'operation': 'stage_health', 'checked_at': now, 'ok': report['ok'], 'uploaded': uploaded.returncode == 0, 'soak_started': (args.directory / 'marker.json').exists()}
    if uploaded.returncode:
        result['upload_error'] = upload_failure(uploaded.stderr)
    print(json.dumps(result))
    return 0 if report['ok'] and uploaded.returncode == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
