#!/usr/bin/env python3
"""Publish bounded, content-free aggregates from the existing production stores."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time

SCHEMA = 'zavliq-public-stats-v1'
PROJECT = 'zavliq-production'
MAX_JSON = 16 * 1024
MAX_INTEGER = 2**53 - 1
IDENTITY = re.compile(r'@[a-z][a-z0-9_-]{2,31}:zavliq\.com\Z')
UTC = dt.timezone.utc


class CollectionError(Exception):
    pass


def require(value, code):
    if not value:
        raise CollectionError(code)


def exact(value, keys):
    require(type(value) is dict and set(value) == set(keys), 'INVALID_AGGREGATE')


def number(value):
    require(type(value) is int and 0 <= value <= MAX_INTEGER, 'INVALID_AGGREGATE')
    return value


def parse_json(raw, limit=MAX_JSON):
    require(len(raw) <= limit, 'OVERSIZED_AGGREGATE')
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'INVALID_AGGREGATE')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, UnicodeError) as error:
        raise CollectionError('INVALID_AGGREGATE') from error


def classifier(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and not info.st_mode & 0o077,
            'PRIVATE_CLASSIFIER_REQUIRED')
    require(info.st_size <= MAX_JSON, 'INVALID_CLASSIFIER')
    value = parse_json(path.read_bytes())
    exact(value, ['service', 'test'])
    for group in value.values():
        require(type(group) is list and len(group) <= 100 and
                all(type(item) is str and IDENTITY.fullmatch(item) for item in group),
                'INVALID_CLASSIFIER')
        require(len(group) == len(set(group)), 'INVALID_CLASSIFIER')
    require(not set(value['service']) & set(value['test']), 'INVALID_CLASSIFIER')
    require('@echo:zavliq.com' in value['service'], 'ECHO_CLASSIFICATION_REQUIRED')
    return value


def timestamp(value):
    return value.isoformat(timespec='seconds').replace('+00:00', 'Z')


def windows(as_of):
    today = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
    result = [('24h', as_of - dt.timedelta(days=1), as_of)]
    for ago in range(6, -1, -1):
        start = today - dt.timedelta(days=ago)
        result.append((start.date().isoformat(), start, min(start + dt.timedelta(days=1), as_of)))
    return result


def control_script(path='/data/control.sqlite'):
    # JSON parameters are bound to SQLite. Only aggregates leave the database.
    return '''
import {DatabaseSync} from 'node:sqlite';
import {readFileSync} from 'node:fs';
if(process.env.SERVER_NAME !== 'zavliq.com') process.exit(2);
const groups = JSON.parse(readFileSync(0, 'utf8'));
const db = new DatabaseSync(DATABASE, {readOnly:true});
try {
  db.exec('PRAGMA query_only=ON; PRAGMA busy_timeout=1000; BEGIN');
  const result = db.prepare(`SELECT count(*) AS registered_total,
    coalesce(sum(('@' || handle || ':zavliq.com') IN (SELECT value FROM json_each(?))),0) AS registered_service,
    coalesce(sum(('@' || handle || ':zavliq.com') IN (SELECT value FROM json_each(?))),0) AS registered_test
    FROM enrollments WHERE phase='complete'`).get(JSON.stringify(groups.service),JSON.stringify(groups.test));
  db.exec('ROLLBACK');
  process.stdout.write(JSON.stringify(result));
} finally { db.close(); }
'''.replace('DATABASE', json.dumps(str(path)))


def postgres_query(as_of):
    intervals = windows(as_of)
    rows = ','.join("('%s',%d::bigint,%d::bigint)" %
                    (name, int(start.timestamp()) * 1000, int(end.timestamp()) * 1000)
                    for name, start, end in intervals)
    lower = int(intervals[1][1].timestamp()) * 1000
    upper = int(as_of.timestamp()) * 1000
    return f'''BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout='3000ms';
SET LOCAL lock_timeout='500ms';
WITH windows(key,start_ms,end_ms) AS (VALUES {rows}),
matching AS (
  SELECT received_ts, sender, type FROM events
  WHERE processed AND NOT outlier AND rejection_reason IS NULL AND state_key IS NULL
    AND type IN ('m.room.message','m.room.encrypted')
    AND received_ts >= {lower} AND received_ts < {upper}
), aggregates AS (
  SELECT w.key,
    count(*) FILTER (WHERE e.type='m.room.message') AS plaintext_messages,
    count(*) FILTER (WHERE e.type='m.room.encrypted') AS encrypted_events,
    count(e.type) AS message_activity,
    count(DISTINCT e.sender) AS participating_identities
  FROM windows w LEFT JOIN matching e ON e.received_ts >= w.start_ms AND e.received_ts < w.end_ms
  GROUP BY w.key
)
SELECT json_build_object(
  'retained_conversations', (SELECT count(*) FROM rooms r WHERE EXISTS (
    SELECT 1 FROM events e WHERE e.room_id=r.room_id AND e.type='com.zavliq.conversation'
      AND e.state_key='' AND e.processed AND NOT e.outlier AND e.rejection_reason IS NULL)),
  'windows', (SELECT json_object_agg(key,json_build_object(
    'plaintext_messages',plaintext_messages,'encrypted_events',encrypted_events,
    'message_activity',message_activity,'participating_identities',participating_identities)) FROM aggregates)
);
ROLLBACK;
'''


class Runner:
    def __init__(self, timeout=20):
        self.deadline = time.monotonic() + timeout

    def run(self, args, data=None):
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, 'COLLECTION_TIMEOUT')
        # Temporary files bound memory even if a failed tool emits unexpected output.
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                result = subprocess.run(args, input=data, stdout=out, stderr=err,
                                        timeout=min(remaining, 6), check=False)
            except subprocess.TimeoutExpired as error:
                raise CollectionError('COLLECTION_TIMEOUT') from error
            except OSError as error:
                raise CollectionError('COLLECTION_COMMAND_FAILED') from error
            require(result.returncode == 0, 'COLLECTION_COMMAND_FAILED')
            require(out.tell() <= MAX_JSON, 'OVERSIZED_AGGREGATE')
            out.seek(0)
            return out.read(MAX_JSON + 1)


def collect(groups, as_of, runner):
    containers = {}
    for service in ['control', 'postgres']:
        name = PROJECT + '-' + service + '-1'
        expected = PROJECT + '|' + service + '|true|false'
        proof = runner.run(['docker', 'inspect', '--format',
            '{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}|{{.State.Running}}|{{.State.Paused}}', name])
        require(proof.decode('utf8').strip() == expected, 'PRODUCTION_CONTAINER_REQUIRED')
        containers[service] = name
    enrollment = parse_json(runner.run(['docker', 'exec', '-i', containers['control'],
        'node', '--no-warnings', '--input-type=module', '-e', control_script()],
        json.dumps(groups, separators=(',', ':')).encode()))
    database = parse_json(runner.run(['docker', 'exec', '-i', containers['postgres'],
        'psql', '-X', '-q', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-U', 'zavliq', '-d', 'synapse'],
        postgres_query(as_of).encode()))
    exact(enrollment, ['registered_total', 'registered_service', 'registered_test'])
    for value in enrollment.values():
        number(value)
    other = enrollment['registered_total'] - enrollment['registered_service'] - enrollment['registered_test']
    number(other)
    exact(database, ['retained_conversations', 'windows'])
    number(database['retained_conversations'])
    intervals = windows(as_of)
    exact(database['windows'], [item[0] for item in intervals])
    for activity in database['windows'].values():
        exact(activity, ['plaintext_messages', 'encrypted_events', 'message_activity', 'participating_identities'])
        for value in activity.values():
            number(value)
        require(activity['message_activity'] == activity['plaintext_messages'] + activity['encrypted_events'] and
                activity['participating_identities'] <= activity['message_activity'], 'INVALID_AGGREGATE')
    result = {
        'schema': SCHEMA, 'as_of': timestamp(as_of), 'refresh_seconds': 300,
        'counts': {**enrollment, 'registered_other': other,
                   'retained_conversations': database['retained_conversations']},
        'activity_24h': {'from': timestamp(intervals[0][1]), 'to': timestamp(as_of),
                        **database['windows']['24h']},
        'daily_utc': [{'date': name, 'complete': name != as_of.date().isoformat(),
                       **database['windows'][name]} for name, _, _ in intervals[1:]],
    }
    require(len(json.dumps(result).encode()) <= MAX_JSON, 'OVERSIZED_AGGREGATE')
    return result


def publish(path, snapshot):
    require(path.parent.is_dir() and not path.is_symlink(), 'PUBLIC_OUTPUT_REQUIRED')
    data = (json.dumps(snapshot, separators=(',', ':'), allow_nan=False) + '\n').encode()
    require(len(data) <= MAX_JSON, 'OVERSIZED_AGGREGATE')
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix='.stats-', dir=path.parent)
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
            output.flush()
            os.fchmod(output.fileno(), 0o644)
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def execute(config, output, runner=None, now=None):
    as_of = (now or dt.datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    snapshot = collect(classifier(config), as_of, runner or Runner())
    publish(output, snapshot)
    return snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--classifier', type=Path, default=Path('/etc/zavliq/stats-classifier.json'))
    parser.add_argument('--output', type=Path, default=Path('/var/lib/zavliq/stats.json'))
    args = parser.parse_args()
    os.umask(0o077)
    try:
        value = execute(args.classifier, args.output)
        print(json.dumps({'ok': True, 'schema': SCHEMA, 'as_of': value['as_of']}))
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, CollectionError) else 'COLLECTION_FAILED'
        print(json.dumps({'ok': False, 'error': code}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
