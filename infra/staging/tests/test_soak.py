import importlib.util
from contextlib import closing, redirect_stdout
from datetime import datetime
import fcntl
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import probe
from begin_soak import snapshot
from probe import summarize, upload_failure, bound_images_healthy, Observation


class SoakEvidence(unittest.TestCase):
    def setUp(self):
        self.marker = {'started_at': 1000, 'expected_revision': 'a' * 40, 'final_service_set': True}
        self.rows = [{'checked_at': 1000 + index * 60, 'ok': True} for index in range(1441)]

    def test_exact_full_day_of_healthy_samples_passes(self):
        result = summarize(self.rows, self.marker, 87400)
        self.assertTrue(result['complete'])
        self.assertTrue(result['final_service_soak_passed'])

    def test_early_run_failure_and_missing_minute_do_not_pass(self):
        self.assertFalse(summarize(self.rows[:-1], self.marker, 87399)['complete'])
        failed = [dict(row) for row in self.rows]
        failed[100]['ok'] = False
        self.assertFalse(summarize(failed, self.marker, 87400)['complete'])
        self.assertFalse(summarize(self.rows[:100] + self.rows[101:], self.marker, 87400)['continuity_passed'])

    def test_diagnostic_scope_cannot_claim_final_service_soak(self):
        result = summarize(self.rows, {**self.marker, 'final_service_set': False}, 87400)
        self.assertTrue(result['complete'])
        self.assertFalse(result['final_service_soak_passed'])

    def test_old_samples_cannot_fill_a_new_soak(self):
        result = summarize(self.rows, {**self.marker, 'started_at': 87400}, 87400)
        self.assertEqual(result['samples'], 1)
        self.assertFalse(result['complete'])

    def test_paused_or_unchecked_echo_cannot_count_as_healthy(self):
        expected = {'echo': {'image_id': 'sha256:echo'}, 'gateway': {'image_id': 'sha256:web'}}
        actual = {name: {**image, 'running': True, 'paused': False, 'health': 'healthy' if name == 'echo' else ''}
                  for name, image in expected.items()}
        self.assertTrue(bound_images_healthy(actual, expected))
        actual['echo']['paused'] = True
        self.assertFalse(bound_images_healthy(actual, expected))
        actual['echo']['paused'] = False
        actual['echo']['health'] = ''
        self.assertFalse(bound_images_healthy(actual, expected))
        actual['echo']['health'] = 'healthy'
        del actual['echo']['paused']
        self.assertFalse(bound_images_healthy(actual, expected))

    def test_health_journal_logs_only_known_upload_error_and_static_action(self):
        result = upload_failure('{"code":"CAPABILITY_EXPIRED_OR_INVALID","action":"private-value"}')
        self.assertEqual(result['code'], 'CAPABILITY_EXPIRED_OR_INVALID')
        self.assertNotIn('private-value', str(result))
        self.assertEqual(upload_failure('https://example.invalid/?signature=private-value')['code'], 'INVALID_LOCAL_STATE')


class ObservationEvidence(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.output = self.directory / 'health.json'
        self.health = self.directory / 'healthcheck.py'
        self.images = {name: {'image_id': 'sha256:' + str(index) * 64}
                       for index, name in enumerate(['synapse', 'control', 'gateway', 'postgres', 'echo'], 1)}
        self.marker = {'started_at': int(time.time()) - 1, 'expected_revision': 'a' * 40,
                       'final_service_set': True, 'project': 'zavliq-test', 'expected_images': self.images}
        (self.directory / 'marker.json').write_text(json.dumps(self.marker))
        self.health_payload = {'ok': True, 'checks': [
            {'check': '/health', 'ok': True, 'latency_ms': 17},
            {'check': '/_matrix/client/versions', 'ok': True, 'latency_ms': 3},
            {'check': '/.well-known/matrix/client', 'ok': True, 'latency_ms': 4},
            {'check': 'disk', 'ok': True, 'used_percent': 15.1},
            {'check': 'backup_age', 'ok': True, 'age_hours': 2.3},
        ]}
        self.set_health(self.health_payload)
        self.argv = ['probe.py', '--directory', str(self.directory), '--output', str(self.output),
                     '--healthcheck', str(self.health), '--capability', str(self.directory / 'unused-capability.json')]

    def set_health(self, payload, prefix=''):
        self.health.write_text(prefix + '\nimport json\nprint(' + repr(json.dumps(payload)) + ')\n')

    def rows(self):
        with closing(sqlite3.connect(self.directory / 'health.sqlite3')) as db:
            return [(row[0], bool(row[1]), json.loads(row[2])) for row in
                    db.execute('SELECT id,ok,details FROM samples ORDER BY id')]

    def inspect(self, args, **kwargs):
        self.assertIn('{{with index .State "Health"}}', args[3])
        self.assertIn('{{.Id}}', args[3])
        self.assertLessEqual(kwargs['timeout'], 5)
        self.assertIs(kwargs['stderr'], subprocess.DEVNULL)
        name = args[-1].removeprefix('zavliq-test-').removesuffix('-1')
        image = self.images[name]['image_id']
        return '|'.join(['f' * 64, image, 'zavliq-' + name + ':' + 'a' * 40, 'true', 'false',
                         '' if name == 'gateway' else 'healthy']) + '\n'

    def run_probe(self, inspect=None):
        real_run = subprocess.run

        def run(args, **kwargs):
            if args[1] == str(self.health):
                # Execute the real fixture child: it has no network or Docker access.
                return real_run(args, **kwargs)
            self.assertEqual(Path(args[1]), BASE / 'upload.py')
            # The upload must see the final row/report, not an unfinished checkpoint.
            self.assertEqual(self.rows()[-1][2]['observation']['status'], 'completed')
            return subprocess.CompletedProcess(args, 0, '{}', '')

        with patch.object(sys, 'argv', self.argv), patch('subprocess.run', side_effect=run), \
                patch('subprocess.check_output', side_effect=inspect or self.inspect), redirect_stdout(io.StringIO()):
            return probe.main()

    def assert_brackets(self, value):
        self.assertLessEqual(datetime.fromisoformat(value['started_at']), datetime.fromisoformat(value['finished_at']))
        self.assertGreaterEqual(value['elapsed_ms'], 0)

    def test_reservation_precedes_health_child_and_all_service_evidence_is_durable(self):
        before = """import sqlite3,json
db=sqlite3.connect(%r)
rows=db.execute('SELECT ok,details FROM samples').fetchall()
assert len(rows)==1 and rows[0][0]==0
assert json.loads(rows[0][1])['ok'] is False
assert json.loads(rows[0][1])['observation']['status']=='incomplete'
db.close()
""" % str(self.directory / 'health.sqlite3')
        self.set_health(self.health_payload, before)
        seen = []

        def inspect(args, **kwargs):
            row = self.rows()[0]
            self.assertFalse(row[1])
            self.assertFalse(row[2]['ok'])
            self.assertEqual(len(row[2]['service_observations']), len(seen))
            self.assertEqual(row[2]['checks'][0]['latency_ms'], 17)
            seen.append(args[-1])
            return self.inspect(args, **kwargs)

        self.assertEqual(self.run_probe(inspect), 0)
        row = self.rows()[0]
        self.assertTrue(row[1])
        self.assertEqual(len(row[2]['service_observations']), 5)
        self.assert_brackets(row[2]['observation'])
        self.assert_brackets(row[2]['health_observation'])
        for value in row[2]['service_observations']:
            self.assertTrue(value['ok'])
            self.assertEqual(value['container_id'], 'f' * 64)
            self.assert_brackets(value)
        self.assertLess(self.output.stat().st_size, 58000)
        self.assertNotIn('scheduled_at', row[2]['observation'])

    def test_partial_inspection_failure_keeps_prior_results_and_redacts_exception(self):
        def inspect(args, **kwargs):
            if args[-1].endswith('-control-1'):
                raise subprocess.CalledProcessError(1, args, output='private-output', stderr='private-stderr')
            return self.inspect(args, **kwargs)

        self.assertEqual(self.run_probe(inspect), 1)
        value = self.rows()[0][2]
        self.assertFalse(value['ok'])
        self.assertEqual(value['checks'][0]['latency_ms'], 17)
        self.assertEqual(len(value['service_observations']), 5)
        self.assertEqual(value['service_observations'][1]['error_code'], 'INSPECTION_FAILED')
        self.assertTrue(value['service_observations'][-1]['ok'])
        self.assertNotIn('private-', json.dumps(value))

    def test_http_failure_remains_failed_after_healthy_image_inspections(self):
        self.health_payload['ok'] = False
        self.health_payload['checks'][0] = {'check': '/health', 'ok': False, 'error': 'HTTPError', 'latency_ms': 100}
        self.set_health(self.health_payload)
        self.assertEqual(self.run_probe(), 1)
        report = self.rows()[0][2]
        self.assertFalse(report['checks'][0]['ok'])
        self.assertEqual(report['checks'][0]['error'], 'HTTPError')
        self.assertEqual(report['checks'][-1], {'check': 'bound_service_images', 'ok': True})
        self.assertEqual(len(report['service_observations']), 5)

    def test_later_success_has_distinct_attempt_and_does_not_repair_failed_row(self):
        self.health_payload['ok'] = False
        self.set_health(self.health_payload)
        self.assertEqual(self.run_probe(), 1)
        earlier = self.rows()[0]
        self.health_payload['ok'] = True
        self.set_health(self.health_payload)
        self.assertEqual(self.run_probe(), 1)  # Current sample passes; its soak still fails.
        rows = self.rows()
        self.assertEqual(rows[0], earlier)
        self.assertTrue(rows[1][1])
        self.assertNotEqual(rows[0][2]['observation']['attempt_id'], rows[1][2]['observation']['attempt_id'])
        report = json.loads(self.output.read_text())
        self.assertEqual(report['soak']['failed_samples'], 1)
        self.assertFalse(report['ok'])

    def test_concurrent_attempt_is_recorded_without_checks_or_overwriting_output(self):
        self.output.write_text('preserved-active-output')
        with (self.directory / 'probe.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(sys, 'argv', self.argv), patch('subprocess.run') as run, redirect_stdout(io.StringIO()):
                self.assertEqual(probe.main(), 1)
            run.assert_not_called()
        self.assertEqual(self.output.read_text(), 'preserved-active-output')
        self.assertEqual(self.rows()[0][2]['error_code'], 'OBSERVATION_OVERLAP')

    def test_killed_probe_leaves_original_failed_reservation(self):
        ready = self.directory / 'health-started'
        self.health.write_text('from pathlib import Path\nimport time\nPath(' + repr(str(ready)) + ").write_text('ready')\ntime.sleep(30)\n")
        process = subprocess.Popen([sys.executable, '-B', str(BASE / 'probe.py'), *self.argv[1:]],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), 'Offline health fixture did not start')
            before = self.rows()[0]
            self.assertFalse(before[1])
            self.assertFalse(before[2]['ok'])
            self.assertEqual(before[2]['observation']['status'], 'incomplete')
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        self.assertEqual(self.rows()[0], before)
        self.assertFalse(self.output.exists())

    def test_checkpoints_cannot_modify_replaced_or_finalized_record(self):
        report = {'checked_at': 100, 'ok': False, 'observation': {'attempt_id': 'test', 'status': 'incomplete'}}
        path = self.directory / 'separate.sqlite3'
        observation = Observation(path, report)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('UPDATE samples SET details=? WHERE id=?', ('preserved-other-record', observation.id))
        with self.assertRaisesRegex(ValueError, 'OBSERVATION_CHANGED'):
            observation.write({**report, 'ok': True}, final=True)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('SELECT details FROM samples').fetchone()[0], 'preserved-other-record')
        another = Observation(path, report)
        another.write(report, final=True)
        with self.assertRaisesRegex(ValueError, 'OBSERVATION_ALREADY_FINALIZED'):
            another.write(report, final=True)

    def test_snapshot_mapping_compatibility_and_deadline(self):
        with patch('subprocess.check_output', side_effect=self.inspect):
            values = snapshot('zavliq-test', self.images)
        self.assertEqual(set(values['gateway']), {'image_id', 'image_ref', 'running', 'paused', 'health'})
        self.assertEqual(values['gateway']['health'], '')
        evidence = []
        with patch('subprocess.check_output') as run:
            with self.assertRaisesRegex(ValueError, 'SERVICE_INSPECTION_INCOMPLETE'):
                snapshot('zavliq-test', self.images, deadline=time.monotonic() - 1, record_observation=evidence.append)
        run.assert_not_called()
        self.assertEqual(len(evidence), 5)
        self.assertTrue(all(value['error_code'] == 'INSPECTION_TIMEOUT' for value in evidence))

    def test_health_report_ignores_debug_and_bounds_untrusted_fields(self):
        self.health_payload['debug'] = 'private-top-level'
        self.health_payload['ok'] = False
        self.health_payload['checks'][0].update(ok=False, debug='private-check', error='private-exception')
        self.set_health(self.health_payload)
        self.assertEqual(self.run_probe(), 1)
        for value in ('private-top-level', 'private-check', 'private-exception'):
            self.assertNotIn(value, self.output.read_text())
        huge = subprocess.CompletedProcess([], 0, 'x' * 16385, '')
        with self.assertRaisesRegex(ValueError, 'INVALID_HEALTH_RESULT'):
            probe.health_result(huge)
        self.health.write_text("print('invalid-json-private-data')\n")
        self.assertEqual(self.run_probe(), 1)
        self.assertNotIn('private-data', self.output.read_text())
        self.assertEqual(self.rows()[-1][2]['error_code'], 'HEALTH_OBSERVATION_INCOMPLETE')


if __name__ == '__main__':
    unittest.main()
