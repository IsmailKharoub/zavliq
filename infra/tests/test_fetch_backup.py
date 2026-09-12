import contextlib
import datetime as dt
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('fetch_backup', Path(__file__).parents[1] / 'scripts/fetch-backup.py')
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)


class FetchBackup(unittest.TestCase):
    def test_manifest_rejects_stale_path_and_digest(self):
        now = dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc)
        base = {'name': 'zavliq-20260912T000000Z.tar.age', 'bytes': 123, 'sha256': 'a' * 64}
        self.assertEqual(fetch.validate_manifest(base, now), base)
        for change in ({'name': '../private'}, {'name': 'zavliq-20260910T000000Z.tar.age'}, {'name': 'zavliq-20260913T000000Z.tar.age'}, {'sha256': 'bad'}, {'bytes': -1}):
            with self.assertRaises(ValueError):
                fetch.validate_manifest({**base, **change}, now)

    def scenario(self, already_exists):
        payload = b'synthetic-encrypted-archive-fixture'
        digest = hashlib.sha256(payload).hexdigest()
        name = dt.datetime.now(dt.timezone.utc).strftime('zavliq-%Y%m%dT%H%M%SZ.tar.age')
        manifest = {'name': name, 'bytes': len(payload), 'sha256': digest}
        calls = []
        uploaded = False

        def command(argv, **kwargs):
            nonlocal uploaded
            calls.append(argv)
            self.assertGreater(kwargs['timeout'], 0)
            self.assertLessEqual(kwargs['timeout'], fetch.TRANSFER_SECONDS)
            if argv[0] == 'ssh':
                for value in ['BatchMode=yes', 'ConnectTimeout=20', 'ServerAliveInterval=15', 'ServerAliveCountMax=4']:
                    self.assertIn(value, argv)
            else:
                self.assertEqual(kwargs['env']['AWS_MAX_ATTEMPTS'], '2')
                self.assertIn('--cli-connect-timeout', argv)
                self.assertIn('--cli-read-timeout', argv)
            if argv[0] == 'ssh' and argv[-1] == 'sudo python3 -':
                return SimpleNamespace(returncode=0, stdout=json.dumps(manifest), stderr='')
            if argv[:3] == ['aws', 's3api', 'list-objects-v2']:
                return SimpleNamespace(returncode=0, stdout=json.dumps({'Contents': [{'Key': 'daily/' + name}]} if already_exists or uploaded else {}), stderr='')
            if argv[:3] == ['aws', 's3api', 'head-object']:
                if already_exists or uploaded:
                    return SimpleNamespace(returncode=0, stdout=json.dumps({'Metadata': {'sha256': digest}}), stderr='')
                return SimpleNamespace(returncode=1, stdout='', stderr='(404) Not Found')
            if argv[0] == 'ssh':
                kwargs['stdout'].write(payload)
            elif argv[:3] == ['aws', 's3', 'cp'] and argv[3].endswith('.tar.age'):
                uploaded = True
            return SimpleNamespace(returncode=0, stdout='', stderr='')

        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(directory=Path(directory), ssh_config=Path(directory) / 'config', bucket='synthetic-backups')
            output = io.StringIO()
            with patch.object(fetch.subprocess, 'run', side_effect=command), contextlib.redirect_stdout(output):
                fetch.main(args)
            return calls, json.loads(output.getvalue())

    def test_existing_matching_archive_is_not_downloaded_again(self):
        calls, result = self.scenario(True)
        self.assertEqual(result['archive_bytes_transferred'], 0)
        self.assertFalse(any(c[0] == 'ssh' and c[-1].startswith('sudo cat ') for c in calls))

    def test_new_archive_is_verified_uploaded_and_confirmed(self):
        calls, result = self.scenario(False)
        self.assertGreater(result['archive_bytes_transferred'], 0)
        self.assertEqual(result['status'], 'uploaded')
        self.assertEqual(sum(c[:3] == ['aws', 's3api', 'head-object'] for c in calls), 1)
        self.assertEqual(sum(c[:3] == ['aws', 's3api', 'list-objects-v2'] for c in calls), 2)

    def test_access_denial_does_not_fall_back_to_download(self):
        with patch.object(fetch.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stderr='(403) AccessDenied')):
            with self.assertRaisesRegex(RuntimeError, 'Cannot inspect'):
                fetch.matching_object('synthetic', 'daily/archive', 'a' * 64)


    def test_download_timeout_keeps_partial_local_file_and_never_uploads(self):
        payload = b'partial encrypted bytes'
        name = dt.datetime.now(dt.timezone.utc).strftime('zavliq-%Y%m%dT%H%M%SZ.tar.age')
        manifest = {'name': name, 'bytes': 1000, 'sha256': 'a' * 64}
        calls = []
        def command(argv, **kwargs):
            calls.append(argv)
            if argv[0] == 'ssh' and argv[-1] == 'sudo python3 -':
                return SimpleNamespace(returncode=0, stdout=json.dumps(manifest))
            if argv[:3] == ['aws', 's3api', 'list-objects-v2']:
                return SimpleNamespace(returncode=0, stdout='{}')
            self.assertEqual(argv[0], 'ssh')
            self.assertLessEqual(kwargs['timeout'], 360)
            kwargs['stdout'].write(payload)
            raise subprocess.TimeoutExpired('ssh', kwargs['timeout'], stderr=b'sensitive remote output')
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(directory=Path(directory), ssh_config=Path(directory) / 'config', bucket='synthetic')
            output = io.StringIO()
            with patch.object(fetch.subprocess, 'run', side_effect=command), contextlib.redirect_stdout(output):
                with self.assertRaises(subprocess.TimeoutExpired):
                    fetch.main(args)
            self.assertEqual((args.directory / name).read_bytes(), payload)
            self.assertEqual(output.getvalue(), '')
        self.assertFalse(any(c[:3] == ['aws', 's3', 'cp'] for c in calls))

    def test_whole_transfer_deadline_is_shared_across_metadata_commands(self):
        clock = [0]
        name = dt.datetime.now(dt.timezone.utc).strftime('zavliq-%Y%m%dT%H%M%SZ.tar.age')
        manifest = {'name': name, 'bytes': 1, 'sha256': 'a' * 64}
        calls = []
        def command(argv, **kwargs):
            calls.append((argv, kwargs['timeout']))
            if argv[0] == 'ssh':
                clock[0] = 6
                return SimpleNamespace(returncode=0, stdout=json.dumps(manifest))
            self.assertEqual(kwargs['timeout'], 4)
            clock[0] = 10
            return SimpleNamespace(returncode=0, stdout=json.dumps({'Contents': [{'Key': 'daily/' + name}]}))
        with tempfile.TemporaryDirectory() as directory, patch.object(fetch.time, 'monotonic', side_effect=lambda: clock[0]):
            args = SimpleNamespace(directory=Path(directory), ssh_config=Path(directory) / 'config', bucket='synthetic')
            with patch.object(fetch.subprocess, 'run', side_effect=command):
                with self.assertRaisesRegex(TimeoutError, 'BACKUP_TRANSFER_TIMEOUT'):
                    fetch.main(args, fetch.Deadline(10))
        self.assertEqual(len(calls), 2)  # HEAD/download/upload never starts after the budget expires.
        self.assertEqual(calls[0][1], 10)

    def test_upload_timeout_never_reports_verified_success(self):
        name = dt.datetime.now(dt.timezone.utc).strftime('zavliq-%Y%m%dT%H%M%SZ.tar.age')
        payload = b'encrypted archive fixture'
        manifest = {'name': name, 'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
        calls = []
        def command(argv, **kwargs):
            calls.append(argv)
            if argv[-1] == 'sudo python3 -':
                return SimpleNamespace(returncode=0, stdout=json.dumps(manifest))
            if argv[:3] == ['aws', 's3api', 'list-objects-v2']:
                return SimpleNamespace(returncode=0, stdout='{}')
            if argv[0] == 'ssh':
                kwargs['stdout'].write(payload)
                return SimpleNamespace(returncode=0)
            self.assertEqual(argv[:3], ['aws', 's3', 'cp'])
            raise subprocess.TimeoutExpired('aws', kwargs['timeout'])
        with tempfile.TemporaryDirectory() as directory:
            args = SimpleNamespace(directory=Path(directory), ssh_config=Path(directory) / 'config', bucket='synthetic')
            output = io.StringIO()
            with patch.object(fetch.subprocess, 'run', side_effect=command), contextlib.redirect_stdout(output):
                with self.assertRaises(subprocess.TimeoutExpired):
                    fetch.main(args)
            self.assertEqual(output.getvalue(), '')
            self.assertFalse((args.directory / (name + '.sha256')).exists())
        self.assertEqual(sum(c[:3] == ['aws', 's3', 'cp'] for c in calls), 1)

    def test_stalled_child_is_killed_at_the_remaining_deadline(self):
        run = subprocess.run
        def stalled(_argv, **kwargs):
            self.assertLessEqual(kwargs['timeout'], .08)
            return run([sys.executable, '-c', 'import time; time.sleep(5)'], **kwargs)
        started = time.monotonic()
        with patch.object(fetch.subprocess, 'run', side_effect=stalled):
            with self.assertRaises(subprocess.TimeoutExpired):
                fetch.command(['ssh', 'synthetic'], fetch.Deadline(.08), 360, capture_output=True)
        self.assertLess(time.monotonic() - started, 2)


if __name__ == '__main__':
    unittest.main()
