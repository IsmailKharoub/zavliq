#!/usr/bin/env python3
"""Minute health evidence; a soak starts only through the explicit image-bound marker."""
import argparse
from contextlib import closing
import fcntl
import json
import math
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import uuid

from begin_soak import snapshot, timestamp, SERVICES
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


def bound_images_healthy(actual, expected):
    return all(actual[name]['image_id'] == image['image_id'] and actual[name]['running']
               and actual[name].get('paused') is False
               and actual[name]['health'] in ({'', 'healthy'} if name == 'gateway' else {'healthy'})
               for name, image in expected.items())


def encode_report(report):
    value = json.dumps(report, separators=(',', ':'), allow_nan=False)
    # upload.py adds only capability timestamps below the 64 KiB POST limit.
    if len(value.encode()) > 58000:
        raise ValueError('REPORT_TOO_LARGE')
    return value


def health_result(result):
    """Retain only the five content-free checks emitted by healthcheck.py."""
    if len(result.stdout.encode()) > 16384:
        raise ValueError('INVALID_HEALTH_RESULT')
    source = json.loads(result.stdout)
    expected = {'/health', '/_matrix/client/versions', '/.well-known/matrix/client', 'disk', 'backup_age'}
    if not isinstance(source, dict) or type(source.get('ok')) is not bool or not isinstance(source.get('checks'), list) or len(source['checks']) != 5:
        raise ValueError('INVALID_HEALTH_RESULT')
    checks = []
    for check in source['checks']:
        if not isinstance(check, dict) or check.get('check') not in expected or type(check.get('ok')) is not bool:
            raise ValueError('INVALID_HEALTH_RESULT')
        expected.remove(check['check'])
        safe = {'check': check['check'], 'ok': check['ok']}
        for field in ('latency_ms', 'used_percent', 'age_hours'):
            if field in check:
                value = check[field]
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 10**12:
                    raise ValueError('INVALID_HEALTH_RESULT')
                safe[field] = value
        if 'error' in check:
            safe['error'] = check['error'] if check['error'] in {
                'HTTPError', 'URLError', 'TimeoutError', 'JSONDecodeError', 'ValueError', 'OSError',
                'ConnectionError', 'ConnectionResetError', 'RemoteDisconnected', 'IncompleteRead',
            } else 'HEALTH_CHECK_FAILED'
        checks.append(safe)
    return checks, source['ok'] and result.returncode == 0 and all(check['ok'] for check in checks)


def read_marker(path):
    if not path.exists():
        return None
    if path.stat().st_size > 16384:
        raise ValueError('INVALID_MARKER')
    marker = json.loads(path.read_text())
    if (not isinstance(marker, dict) or type(marker.get('started_at')) is not int
            or type(marker.get('final_service_set')) is not bool
            or not isinstance(marker.get('expected_revision'), str)
            or not re.fullmatch(r'[0-9a-f]{40}', marker['expected_revision'])
            or not isinstance(marker.get('project'), str)
            or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', marker['project'])
            or not isinstance(marker.get('expected_images'), dict)
            or not 0 < len(marker['expected_images']) <= 5
            or not set(marker['expected_images']) <= SERVICES):
        raise ValueError('INVALID_MARKER')
    for image in marker['expected_images'].values():
        if not isinstance(image, dict) or not isinstance(image.get('image_id'), str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image['image_id']):
            raise ValueError('INVALID_MARKER')
    return marker


class Observation:
    """One durable failed reservation, then checkpoints and one finalization.

    A killed process leaves its failed reservation/last checkpoint intact. The
    compare-and-swap touches only this attempt; it cannot repair an earlier row.
    """
    def __init__(self, path, report):
        self.path = path
        self.finished = False
        self.encoded = encode_report(report)
        with closing(sqlite3.connect(path, timeout=5)) as db, db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('CREATE TABLE IF NOT EXISTS samples (id INTEGER PRIMARY KEY, checked_at INTEGER NOT NULL, ok INTEGER NOT NULL, details TEXT NOT NULL)')
            self.id = db.execute('INSERT INTO samples(checked_at,ok,details) VALUES (?,0,?)', (report['checked_at'], self.encoded)).lastrowid

    def write(self, report, *, final=False):
        if self.finished:
            raise ValueError('OBSERVATION_ALREADY_FINALIZED')
        encoded = encode_report(report if final else {**report, 'ok': False})
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute('PRAGMA synchronous=FULL')
            changed = db.execute('UPDATE samples SET ok=?,details=? WHERE id=? AND ok=0 AND details=?',
                                 (int(report['ok']) if final else 0, encoded, self.id, self.encoded)).rowcount
            if changed != 1:
                raise ValueError('OBSERVATION_CHANGED')
        self.encoded = encoded
        self.finished = final


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--origin', default='http://localhost:19180', choices=['http://localhost:19180', 'http://127.0.0.1:19180'])
    parser.add_argument('--directory', default='/var/lib/zavliq/staging-soak', type=Path)
    parser.add_argument('--capability', default='/etc/zavliq/staging-capability.json', type=Path)
    parser.add_argument('--healthcheck', default='/opt/zavliq/current/infra/scripts/healthcheck.py', type=Path)
    parser.add_argument('--output', default='/var/lib/zavliq/health.json', type=Path)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    now = int(time.time())
    started = time.monotonic()
    attempt_id = uuid.uuid4().hex
    report = {'ok': False, 'checked_at': now, 'checks': [], 'service_observations': [],
              'observation': {'attempt_id': attempt_id, 'started_at': timestamp(), 'status': 'incomplete'}}
    marker = None
    observation = None
    lock = None
    locked = False
    stage = 'journal'
    try:
        observation = Observation(args.directory / 'health.sqlite3', report)
        stage = 'lock'
        lock = (args.directory / 'probe.lock').open('a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        stage = 'marker'
        marker = read_marker(args.directory / 'marker.json')
        stage = 'health'
        health_started = time.monotonic()
        report['health_observation'] = {'started_at': timestamp(), 'status': 'incomplete'}
        observation.write(report)
        try:
            result = subprocess.run(['python3', str(args.healthcheck), '--origin', args.origin, '--backup-directory', '/var/backups/zavliq'],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=40)
            report['checks'], report['ok'] = health_result(result)
            report['health_observation']['status'] = 'completed'
        finally:
            report['health_observation'].update(finished_at=timestamp(), elapsed_ms=round((time.monotonic() - health_started) * 1000, 3))
        observation.write(report)
        if marker:
            stage = 'inspection'

            def record_service(value):
                report['service_observations'].append(value)
                observation.write(report)

            actual = snapshot(marker['project'], marker['expected_images'], record_observation=record_service)
            same = bound_images_healthy(actual, marker['expected_images'])
            report['checks'].append({'check': 'bound_service_images', 'ok': same})
            report['ok'] = report['ok'] and same
    except Exception:
        report.update(ok=False, error_code={
            'journal': 'OBSERVATION_JOURNAL_FAILED', 'lock': 'OBSERVATION_OVERLAP',
            'marker': 'INVALID_SOAK_MARKER', 'health': 'HEALTH_OBSERVATION_INCOMPLETE',
            'inspection': 'SERVICE_INSPECTION_INCOMPLETE',
        }[stage])
    report['observation'].update(status='completed', finished_at=timestamp(), elapsed_ms=round((time.monotonic() - started) * 1000, 3))
    # Finalize exactly this reservation. Earlier failed/interrupted rows remain.
    try:
        if observation is None:
            raise ValueError('NO_DURABLE_OBSERVATION')
        observation.write(report, final=True)
        with closing(sqlite3.connect(args.directory / 'health.sqlite3', timeout=5)) as db:
            if marker:
                rows = [{'checked_at': row[0], 'ok': bool(row[1])} for row in db.execute('SELECT checked_at,ok FROM samples WHERE checked_at >= ? ORDER BY id', (marker['started_at'],))]
                summary = summarize(rows, marker, now)
                report['soak'] = summary
                # A gap or failed sample remains visible until an explicit new soak.
                report['ok'] = report['ok'] and not summary['failed_samples'] and summary['continuity_passed']
                temporary_summary = args.directory / ('summary.' + attempt_id + '.tmp')
                temporary_summary.write_text(json.dumps(summary, indent=2) + '\n')
                temporary_summary.replace(args.directory / 'summary.json')
    except Exception:
        report.update(ok=False, error_code='OBSERVATION_JOURNAL_FAILED')
    try:
        if not locked:
            # A competing invocation gets a failed row, never the active probe's
            # shared output or upload. The active invocation will see its failure.
            print(json.dumps({'operation': 'stage_health', 'checked_at': now, 'ok': False,
                              'uploaded': False, 'error_code': report['error_code']}))
            return 1
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(args.output.name + '.' + attempt_id + '.tmp')
        temporary.write_text(encode_report(report))
        temporary.chmod(0o644)
        temporary.replace(args.output)
        uploader = Path(__file__).with_name('upload.py')
        try:
            uploaded = subprocess.run(['python3', str(uploader), '--config', str(args.capability), 'health', str(args.output)], capture_output=True, text=True, timeout=185)
        except Exception:
            uploaded = subprocess.CompletedProcess([], 1, '', '')
        result = {'operation': 'stage_health', 'checked_at': now, 'ok': report['ok'], 'uploaded': uploaded.returncode == 0, 'soak_started': (args.directory / 'marker.json').exists()}
        if uploaded.returncode:
            result['upload_error'] = upload_failure(uploaded.stderr)
        print(json.dumps(result))
        return 0 if report['ok'] and uploaded.returncode == 0 else 1
    finally:
        if lock is not None:
            lock.close()


if __name__ == '__main__':
    sys.exit(main())
