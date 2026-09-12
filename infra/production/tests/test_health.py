"""Content-free production health with exact image/Echo checks and real deadlines."""
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import stat
from subprocess import CompletedProcess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import activate


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = activate.Paths(Path(self.temporary.name))
        self.paths.backups.mkdir(parents=True)
        self.manifest = {'images': {name: {'id': 'sha256:' + str(index) * 64}
                                  for index, name in enumerate(sorted(activate.bundle.SERVICES))}}
        self.record = {'status': 'ready'}
        self.state = {name: 'true|false|healthy' for name in self.manifest['images']}
        self.owner = patch.object(activate, 'ROOT_OWNER', os.getuid())
        self.owner.start()
        self.runtime = patch.object(activate, 'runtime_record', return_value=(self.record, Path('/release'), self.manifest))
        self.runtime.start()
        self.compose = patch.object(activate, 'run_compose', side_effect=lambda *args, **kwargs: CompletedProcess([], 0, args[-1] + '-container'))
        self.compose.start()
        self.command = patch.object(activate, 'command', side_effect=self.inspect)
        self.inspect_mock = self.command.start()
        self.https = patch.object(activate, 'public_readiness')
        self.https.start()
        self.disk = patch.object(activate.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(100, 20, 80))
        self.disk.start()
        self.write_backup(time.time())

    def tearDown(self):
        self.disk.stop()
        self.https.stop()
        self.command.stop()
        self.compose.stop()
        self.runtime.stop()
        self.owner.stop()
        self.temporary.cleanup()

    def inspect(self, args, **kwargs):
        name = args[-1].removesuffix('-container')
        return CompletedProcess([], 0, self.manifest['images'][name]['id'] + '|' + self.state[name])

    def write_backup(self, created):
        stamp = dt.datetime.fromtimestamp(created, dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        archive = self.paths.backups / ('zavliq-' + stamp + '.tar.age')
        activate.atomic_write(archive, 'fixture encrypted bytes ' * 20)
        activate.atomic_write(archive.with_name(archive.name + '.sha256'), activate.bundle.sha256(archive) + '  ' + str(archive) + '\n')
        activate.atomic_write(self.paths.backups / 'last-success', stamp + '\n')
        return archive

    def test_every_service_checked_and_report_readable_under_private_umask(self):
        self.state['gateway'] = 'true|false|'
        previous = os.umask(0o077)
        try:
            report = activate.run_health(self.paths)
        finally:
            os.umask(previous)
        self.assertTrue(report['ok'])
        self.assertEqual(stat.S_IMODE(self.paths.health.stat().st_mode), 0o644)
        self.assertEqual(json.loads(self.paths.health.read_text()), report)
        self.assertEqual({call.args[0][-1] for call in self.inspect_mock.call_args_list},
                         {name + '-container' for name in activate.bundle.SERVICES})
        self.assertEqual(set(report), {'ok', 'checked_at', 'checks'})

    def test_stopped_unhealthy_and_missing_echo_health_each_breach(self):
        for status in ['false|false|healthy', 'true|false|unhealthy', 'true|false|', 'true|true|healthy']:
            with self.subTest(status=status):
                self.state['echo'] = status
                report = activate.run_health(self.paths)
                self.assertFalse(report['ok'])
                self.assertEqual(report['checks'][0]['error'], 'RUNNING_IMAGE_OR_HEALTH_MISMATCH')

    def test_wrong_running_image_and_missing_container_breach(self):
        with patch.object(activate, 'command', return_value=CompletedProcess([], 0, 'sha256:wrong|true|false|healthy')):
            self.assertFalse(activate.run_health(self.paths)['ok'])
        with patch.object(activate, 'run_compose', return_value=CompletedProcess([], 0, '')):
            report = activate.run_health(self.paths)
        self.assertEqual(report['checks'][0]['error'], 'ONE_RUNNING_CONTAINER_PER_SERVICE_REQUIRED')

    def test_nonready_or_incomplete_manifest_cannot_publish_healthy(self):
        for status in ['activating', 'failed']:
            self.record['status'] = status
            self.assertFalse(activate.run_health(self.paths)['ok'])
        self.record['status'] = 'ready'
        del self.manifest['images']['echo']
        self.assertFalse(activate.run_health(self.paths)['ok'])

    def test_stale_reuploaded_future_missing_or_partial_backup_breach(self):
        for created in [time.time() - 86401, time.time() + 120]:
            archive = self.write_backup(created)
            os.utime(archive, None)  # Touching/reuploading an old snapshot cannot refresh it.
            self.assertFalse(activate.run_health(self.paths)['checks'][-1]['ok'])
        archive = self.write_backup(time.time())
        archive.write_bytes(b'partial')
        self.assertFalse(activate.run_health(self.paths)['checks'][-1]['ok'])
        archive.unlink()
        self.assertFalse(activate.run_health(self.paths)['checks'][-1]['ok'])

    def test_public_response_errors_never_expose_content_and_disk_breaches(self):
        with (patch.object(activate, 'public_readiness', side_effect=ValueError('private upstream response')),
              patch.object(activate.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(100, 80, 20))):
            report = activate.run_health(self.paths)
        self.assertFalse(report['ok'])
        self.assertNotIn('private upstream response', json.dumps(report))
        self.assertEqual([check['ok'] for check in report['checks']], [True, False, False, True])

    def test_real_total_deadline_publishes_failure_before_service_timeout(self):
        started = time.monotonic()
        with patch.object(activate, 'public_readiness', side_effect=lambda *_: time.sleep(5)):
            report = activate.run_health(self.paths, timeout=0.05)
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(report['ok'])
        self.assertEqual(report['checks'][-1]['error'], 'HEALTH_DEADLINE')
        self.assertEqual(json.loads(self.paths.health.read_text()), report)


if __name__ == '__main__':
    unittest.main()
