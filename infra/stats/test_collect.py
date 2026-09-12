from contextlib import closing
import datetime as dt
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('stats_collect', HERE / 'collect.py')
stats = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stats)
NOW = dt.datetime(2026, 9, 12, 12, 34, 56, tzinfo=dt.timezone.utc)
GROUPS = {'service': ['@echo:zavliq.com'], 'test': ['@arden-fixture:zavliq.com']}


class FakeRunner:
    def __init__(self, now=NOW):
        self.calls = []
        self.enrollment = {'registered_total': 7, 'registered_service': 1, 'registered_test': 1}
        self.database = {'retained_conversations': 4, 'windows': {
            name: {'plaintext_messages': 2, 'encrypted_events': 3,
                   'message_activity': 5, 'participating_identities': 2}
            for name, _, _ in stats.windows(now)}}
        self.fail_at = None

    def run(self, args, data=None):
        self.calls.append((args, data))
        if self.fail_at == len(self.calls):
            raise stats.CollectionError('COLLECTION_TIMEOUT')
        if args[1] == 'inspect':
            service = args[-1].removeprefix('zavliq-production-').removesuffix('-1')
            return f'zavliq-production|{service}|true|false\n'.encode()
        return json.dumps(self.enrollment if 'node' in args else self.database).encode()


class StatsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / 'classifier.json'
        self.config.write_text(json.dumps(GROUPS))
        self.config.chmod(0o600)

    def tearDown(self):
        self.tmp.cleanup()

    def test_complete_snapshot_has_exact_public_shape(self):
        runner = FakeRunner()
        result = stats.execute(self.config, self.root / 'stats.json', runner, NOW)
        self.assertEqual(set(result), {'schema', 'as_of', 'refresh_seconds', 'counts', 'activity_24h', 'daily_utc'})
        self.assertEqual(result['schema'], 'zavliq-public-stats-v1')
        self.assertEqual(result['counts'], {'registered_total': 7, 'registered_service': 1,
            'registered_test': 1, 'registered_other': 5, 'retained_conversations': 4})
        self.assertEqual(result['as_of'], '2026-09-12T12:34:56Z')
        self.assertEqual(result['activity_24h']['from'], '2026-09-11T12:34:56Z')
        self.assertEqual(result['activity_24h']['to'], result['as_of'])
        self.assertEqual([day['date'] for day in result['daily_utc']],
                         [f'2026-09-{day:02}' for day in range(6, 13)])
        self.assertEqual([day['complete'] for day in result['daily_utc']], [True] * 6 + [False])
        raw = (self.root / 'stats.json').read_text()
        self.assertNotIn('@', raw)
        self.assertNotIn('token', raw)
        self.assertNotIn('fixture', raw)
        self.assertEqual(json.loads(raw), result)
        self.assertEqual(len(runner.calls), 4)
        self.assertEqual(runner.calls[-1][0][-4:], ['-U', 'zavliq', '-d', 'synapse'])
        self.assertIn('ON_ERROR_STOP=1', runner.calls[-1][0])

    def test_utc_windows_cross_year_and_leap_day(self):
        for stamp, first in [('2027-01-01T00:00:00+00:00', '2026-12-26'),
                             ('2028-03-01T01:00:00+00:00', '2028-02-24')]:
            now = dt.datetime.fromisoformat(stamp)
            values = stats.windows(now)
            self.assertEqual(len(values), 8)
            self.assertEqual(values[1][0], first)
            self.assertEqual(values[0][2] - values[0][1], dt.timedelta(days=1))
            self.assertEqual(values[-1][2], now)
            for left, right in zip(values[1:], values[2:]):
                self.assertEqual(left[2], right[1])
        midnight = stats.windows(dt.datetime(2027, 1, 1, tzinfo=dt.timezone.utc))[-1]
        self.assertEqual(midnight[1], midnight[2])

    def test_classifier_requires_exact_disjoint_private_id_lists(self):
        invalid = [
            {'service': [], 'test': []},
            {'service': GROUPS['service'], 'test': GROUPS['service']},
            {'service': GROUPS['service'], 'test': ['arden-*']},
            {'service': GROUPS['service'], 'test': ['@arden:localhost']},
            {'service': GROUPS['service'], 'test': ['@arden:zavliq.com'] * 2},
            {**GROUPS, 'private_note': 'not allowed'},
        ]
        for value in invalid:
            self.config.write_text(json.dumps(value))
            with self.subTest(value=value), self.assertRaises(stats.CollectionError):
                stats.classifier(self.config)
        self.config.write_text(json.dumps(GROUPS))
        self.config.chmod(0o644)
        with self.assertRaisesRegex(stats.CollectionError, 'PRIVATE_CLASSIFIER_REQUIRED'):
            stats.classifier(self.config)
        self.config.chmod(0o600)
        alias = self.root / 'alias.json'
        alias.symlink_to(self.config)
        with self.assertRaisesRegex(stats.CollectionError, 'PRIVATE_CLASSIFIER_REQUIRED'):
            stats.classifier(alias)

    def test_oversized_classifier_not_read(self):
        self.config.write_bytes(b' ' * (stats.MAX_JSON + 1))
        with patch.object(Path, 'read_bytes', side_effect=AssertionError('must not read')):
            with self.assertRaisesRegex(stats.CollectionError, 'INVALID_CLASSIFIER'):
                stats.classifier(self.config)

    def test_no_snapshot_change_when_source_or_validation_fails(self):
        output = self.root / 'stats.json'
        old = b'{"historical_snapshot":true}\n'
        output.write_bytes(old)
        for position in [1, 2, 3, 4]:
            runner = FakeRunner()
            runner.fail_at = position
            with self.assertRaises(stats.CollectionError):
                stats.execute(self.config, output, runner, NOW)
            self.assertEqual(output.read_bytes(), old)
        runner = FakeRunner()
        runner.database['windows']['24h']['sender'] = '@must-not-leak:zavliq.com'
        with self.assertRaisesRegex(stats.CollectionError, 'INVALID_AGGREGATE'):
            stats.execute(self.config, output, runner, NOW)
        self.assertEqual(output.read_bytes(), old)

    def test_missing_snapshot_not_fabricated_on_failure(self):
        runner = FakeRunner()
        runner.fail_at = 4
        output = self.root / 'stats.json'
        with self.assertRaises(stats.CollectionError):
            stats.execute(self.config, output, runner, NOW)
        self.assertFalse(output.exists())

    def test_paused_wrong_project_or_wrong_service_refused_before_sql(self):
        for value in ['zavliq-load|control|true|false',
                      'zavliq-production|synapse|true|false',
                      'zavliq-production|control|false|false',
                      'zavliq-production|control|true|true']:
            runner = FakeRunner()
            with patch.object(runner, 'run', return_value=value.encode()) as mocked:
                with self.assertRaisesRegex(stats.CollectionError, 'PRODUCTION_CONTAINER_REQUIRED'):
                    stats.collect(GROUPS, NOW, runner)
                self.assertEqual(mocked.call_count, 1)

    def test_invalid_counts_unknown_fields_and_window_gaps_rejected(self):
        changes = [
            lambda r: r.enrollment.update(registered_total=True),
            lambda r: r.enrollment.update(registered_total=2**53),
            lambda r: r.enrollment.update(registered_total=1),
            lambda r: r.enrollment.update(credentials='must-not-leak'),
            lambda r: r.database.update(retained_conversations=-1),
            lambda r: r.database['windows'].pop('2026-09-06'),
            lambda r: r.database['windows']['24h'].update(message_activity=6),
            lambda r: r.database['windows']['24h'].update(participating_identities=6),
        ]
        for change in changes:
            runner = FakeRunner()
            change(runner)
            with self.subTest(change=change), self.assertRaises(stats.CollectionError):
                stats.collect(GROUPS, NOW, runner)
        with self.assertRaisesRegex(stats.CollectionError, 'INVALID_AGGREGATE'):
            stats.parse_json(b'{"registered_total":1,"registered_total":2}')

    def test_atomic_snapshot_under_restrictive_umask_and_replace_failure(self):
        output = self.root / 'stats.json'
        output.write_bytes(b'old')
        previous = os.umask(0o077)
        try:
            with patch.object(os, 'replace', side_effect=OSError('simulated private failure')):
                with self.assertRaises(OSError):
                    stats.publish(output, {'checked': 1})
            self.assertEqual(output.read_bytes(), b'old')
            self.assertEqual(list(self.root.glob('.stats-*')), [])
            stats.publish(output, {'checked': 2})
            self.assertEqual(output.stat().st_mode & 0o777, 0o644)
        finally:
            os.umask(previous)

    def test_postgres_query_is_read_only_aggregate_and_server_time_bound(self):
        sql = stats.postgres_query(NOW)
        self.assertIn('REPEATABLE READ READ ONLY', sql)
        self.assertIn("statement_timeout='3000ms'", sql)
        self.assertIn("lock_timeout='500ms'", sql)
        self.assertIn('rejection_reason IS NULL AND state_key IS NULL', sql)
        self.assertIn('count(DISTINCT e.sender)', sql)
        self.assertIn('count(*) FROM rooms r WHERE EXISTS', sql)
        for excluded in ['event_json', 'content', 'access_token', 'origin_server_ts',
                         'INSERT ', 'UPDATE ', 'DELETE ', 'CREATE ', 'COPY ']:
            self.assertNotIn(excluded, sql)
        self.assertIn(str(int(NOW.timestamp()) * 1000), sql)

    def test_runner_timeout_nonzero_and_oversized_output_are_classified(self):
        with patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired(['private'], 1)):
            with self.assertRaisesRegex(stats.CollectionError, '^COLLECTION_TIMEOUT$'):
                stats.Runner().run(['not-executed'])
        with patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)):
            with self.assertRaisesRegex(stats.CollectionError, '^COLLECTION_COMMAND_FAILED$'):
                stats.Runner().run(['not-executed'])
        def oversized(*_, **kwargs):
            kwargs['stdout'].write(b'x' * (stats.MAX_JSON + 1))
            return subprocess.CompletedProcess([], 0)
        with patch.object(subprocess, 'run', side_effect=oversized):
            with self.assertRaisesRegex(stats.CollectionError, '^OVERSIZED_AGGREGATE$'):
                stats.Runner().run(['not-executed'])
        with self.assertRaisesRegex(stats.CollectionError, '^COLLECTION_TIMEOUT$'):
            stats.Runner(timeout=-1).run(['not-executed'])

    def test_cli_failure_logs_fixed_code_without_private_exception_details(self):
        output = io.StringIO()
        before = os.umask(0o077)
        try:
            with patch.object(sys, 'argv', ['collect.py']), patch.object(sys, 'stdout', output), \
                 patch.object(stats, 'execute', side_effect=OSError('private-path @identity:zavliq.com secret-token')):
                self.assertEqual(stats.main(), 1)
        finally:
            os.umask(before)
        self.assertEqual(json.loads(output.getvalue()), {'ok': False, 'error': 'COLLECTION_FAILED'})

    @unittest.skipUnless(shutil.which('node'), 'Node 24 required for the real SQLite reader fixture')
    def test_real_read_only_sqlite_uses_exact_classification_without_secret_output(self):
        database = self.root / 'control.sqlite'
        with closing(sqlite3.connect(database)) as connection:
            with connection:
                connection.execute('CREATE TABLE enrollments (handle TEXT PRIMARY KEY,phase TEXT,credentials TEXT)')
                for handle, phase in [('echo', 'complete'), ('arden-fixture', 'complete'),
                                      ('arden-other', 'complete'), ('mira', 'complete'),
                                      ('pending', 'reserved'), ('collision', 'collision')]:
                    connection.execute('INSERT INTO enrollments VALUES (?,?,?)', (handle, phase, 'private-fixture-token'))
        before = database.read_bytes()
        result = subprocess.run(['node', '--no-warnings', '--input-type=module', '-e', stats.control_script(database)],
            input=json.dumps(GROUPS), text=True, capture_output=True, timeout=5,
            env={**os.environ, 'SERVER_NAME': 'zavliq.com'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'registered_total': 4, 'registered_service': 1, 'registered_test': 1})
        self.assertNotIn('private-fixture-token', result.stdout + result.stderr)
        self.assertNotIn('arden', result.stdout + result.stderr)
        self.assertEqual(database.read_bytes(), before)
        # Wrong namespace must exit before even opening a missing database.
        missing = self.root / 'must-not-create.sqlite'
        result = subprocess.run(['node', '--no-warnings', '--input-type=module', '-e', stats.control_script(missing)],
            input=json.dumps(GROUPS), text=True, capture_output=True, timeout=5,
            env={**os.environ, 'SERVER_NAME': 'localhost'})
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
