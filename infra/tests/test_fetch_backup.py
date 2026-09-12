import base64
import contextlib
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
import runpy
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


class ObjectService:
    """Synthetic byte store with S3-style conditional creation, never real I/O."""
    def __init__(self):
        self.payload = b'synthetic-encrypted-archive-fixture'
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self.name = dt.datetime.now(dt.timezone.utc).strftime('zavliq-%Y%m%dT%H%M%SZ.tar.age')
        self.key = 'daily/' + self.name
        self.manifest = {'name': self.name, 'bytes': len(self.payload), 'sha256': self.digest}
        self.sidecar = (self.digest + '  ' + self.name + '\n').encode()
        self.objects = {}
        self.calls = []
        self.before_put = None
        self.after_put = None
        self.download_timeout = False

    def object(self, body, metadata=None, encryption='AES256'):
        return {'body': body, 'metadata': metadata or {}, 'encryption': encryption}

    def existing(self):
        self.objects[self.key] = self.object(self.payload, {'sha256': self.digest})
        # The currently deployed uploader created sidecars without hash metadata.
        self.objects[self.key + '.sha256'] = self.object(self.sidecar)
        return self

    @property
    def writes(self):
        return [argv for argv in self.calls if argv[:3] == ['aws', 's3api', 'put-object']]

    @property
    def downloads(self):
        return [argv for argv in self.calls if argv[0] == 'ssh' and argv[-1].startswith('sudo cat ')]

    def run(self, argv, **kwargs):
        self.calls.append(argv)
        if not 0 < kwargs['timeout'] <= fetch.TRANSFER_SECONDS:
            raise AssertionError('Unbounded command')
        if argv[0] == 'ssh':
            for option in ['BatchMode=yes', 'ConnectTimeout=20', 'ServerAliveInterval=15', 'ServerAliveCountMax=4']:
                if option not in argv:
                    raise AssertionError('Missing SSH bound')
            if argv[-1] == 'sudo python3 -':
                return SimpleNamespace(returncode=0, stdout=json.dumps(self.manifest), stderr='')
            if argv[-1] != 'sudo cat /var/backups/zavliq/' + self.name:
                raise AssertionError('Unexpected SSH command')
            kwargs['stdout'].write(self.payload)
            if self.download_timeout:
                raise subprocess.TimeoutExpired('ssh', kwargs['timeout'], stderr=b'sensitive remote output')
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        if argv[:2] != ['aws', 's3api']:
            raise AssertionError('Unexpected executable/API')
        if kwargs['env']['AWS_MAX_ATTEMPTS'] != '2':
            raise AssertionError('Retry budget changed')
        if '--cli-connect-timeout' not in argv or '--cli-read-timeout' not in argv:
            raise AssertionError('Missing AWS bounds')
        value = lambda key: argv[argv.index(key) + 1]
        operation = argv[2]
        if operation == 'list-objects-v2':
            if '--no-paginate' not in argv or value('--max-keys') != '1':
                raise AssertionError('Unbounded list')
            keys = sorted(key for key in self.objects if key.startswith(value('--prefix')))[:1]
            response = {'Contents': [{'Key': key} for key in keys]}
        elif operation == 'head-object':
            item = self.objects[value('--key')]
            response = {'Metadata': item['metadata'], 'ContentLength': len(item['body']),
                        'ServerSideEncryption': item['encryption']}
        elif operation == 'get-object':
            item = self.objects[value('--key')]
            last = int(value('--range').removeprefix('bytes=0-'))
            Path(argv[argv.index('--range') + 2]).write_bytes(item['body'][:last + 1])
            response = {'ContentLength': min(len(item['body']), last + 1)}
        elif operation == 'put-object':
            key = value('--key')
            if self.before_put:
                self.before_put(key)
            if value('--if-none-match') != '*':
                raise AssertionError('Unconditional overwrite attempt')
            if key in self.objects:
                raise subprocess.CalledProcessError(1, 'aws', stderr=b'412 PreconditionFailed private resource')
            body = Path(value('--body')).read_bytes()
            digest = hashlib.sha256(body).hexdigest()
            checksum = base64.b64encode(bytes.fromhex(digest)).decode()
            if value('--checksum-sha256') != checksum or int(value('--content-length')) != len(body):
                raise AssertionError('Body checksum or length mismatch')
            if value('--server-side-encryption') != 'AES256':
                raise AssertionError('Encryption changed')
            self.objects[key] = self.object(body, {'sha256': value('--metadata').removeprefix('sha256=')})
            if self.after_put:
                self.after_put(key)
            response = {'ChecksumSHA256': checksum, 'ServerSideEncryption': 'AES256'}
        else:
            raise AssertionError('Unexpected API ' + operation)
        return SimpleNamespace(returncode=0, stdout=json.dumps(response), stderr='')


class FetchBackup(unittest.TestCase):
    def args(self, directory, **options):
        return SimpleNamespace(directory=Path(directory), ssh_config=Path(directory) / 'config',
                               bucket='synthetic-backups', **options)

    def invoke(self, service, directory, **options):
        output = io.StringIO()
        with patch.object(fetch.subprocess, 'run', side_effect=service.run), contextlib.redirect_stdout(output):
            fetch.main(self.args(directory, **options))
        return json.loads(output.getvalue())

    def pin(self, directory, manifest):
        path = Path(directory) / 'expected.json'
        path.write_text(json.dumps(manifest))
        path.chmod(0o600)
        return path

    def assert_failure(self, service, error, pattern=None):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with patch.object(fetch.subprocess, 'run', side_effect=service.run), contextlib.redirect_stdout(output):
                if pattern:
                    with self.assertRaisesRegex(error, pattern):
                        fetch.main(self.args(directory))
                else:
                    with self.assertRaises(error):
                        fetch.main(self.args(directory))
            self.assertEqual(output.getvalue(), '')

    def test_manifest_rejects_stale_path_and_digest(self):
        now = dt.datetime(2026, 9, 12, tzinfo=dt.timezone.utc)
        base = {'name': 'zavliq-20260912T000000Z.tar.age', 'bytes': 123, 'sha256': 'a' * 64}
        self.assertEqual(fetch.validate_manifest(base, now), base)
        for change in ({'name': '../private'}, {'name': 'zavliq-20260910T000000Z.tar.age'},
                       {'name': 'zavliq-20260913T000000Z.tar.age'}, {'sha256': 'bad'}, {'bytes': -1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                fetch.validate_manifest({**base, **change}, now)

    def test_existing_archive_metadata_and_exact_legacy_sidecar_skip_transfer_without_readback_claim(self):
        service = ObjectService().existing()
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(service, directory)
        self.assertEqual(result['status'], 'already_present')
        self.assertEqual(result['archive_bytes_transferred'], 0)
        self.assertEqual(result['snapshot_sha256'], service.digest)
        self.assertTrue(result['remote_metadata_matches'])
        self.assertTrue(result['checksum_sidecar_readback_verified'])
        self.assertFalse(result['local_ciphertext_sha256_verified'])
        self.assertFalse(result['s3_sha256_validated_on_create'])
        self.assertFalse(result['off_host_ciphertext_readback_verified'])
        self.assertNotIn('checksum_verified', result)
        self.assertFalse(service.downloads)
        self.assertFalse(service.writes)

    def test_new_archive_and_sidecar_use_conditional_checksummed_puts(self):
        service = ObjectService()
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(service, directory)
            self.assertEqual((Path(directory) / service.name).read_bytes(), service.payload)
        self.assertEqual(result['archive_bytes_transferred'], len(service.payload))
        self.assertEqual(result['status'], 'uploaded')
        self.assertTrue(result['local_ciphertext_sha256_verified'])
        self.assertTrue(result['s3_sha256_validated_on_create'])
        self.assertTrue(result['remote_metadata_matches'])
        self.assertTrue(result['checksum_sidecar_readback_verified'])
        self.assertFalse(result['off_host_ciphertext_readback_verified'])
        self.assertEqual(len(service.downloads), 1)
        self.assertEqual(len(service.writes), 2)
        self.assertEqual(service.objects[service.key]['body'], service.payload)
        self.assertEqual(service.objects[service.key + '.sha256']['body'], service.sidecar)

    def test_existing_only_requires_pin_before_any_remote_call(self):
        service = ObjectService().existing()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(fetch.BackupError, 'BACKUP_EXPECTED_MANIFEST_REQUIRED'):
                self.invoke(service, directory, existing_only=True)
        self.assertFalse(service.calls)

    def test_invalid_private_pin_fails_before_any_remote_call(self):
        for kind in ['missing', 'empty', 'oversized', 'invalid-json', 'invalid-manifest',
                     'stale', 'public-mode', 'symlink', 'directory', 'fifo']:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                service = ObjectService().existing()
                path = self.pin(directory, service.manifest)
                if kind == 'missing':
                    path.unlink()
                elif kind == 'empty':
                    path.write_bytes(b'')
                elif kind == 'oversized':
                    path.write_bytes(b' ' * 4097)
                elif kind == 'invalid-json':
                    path.write_bytes(b'private malformed pin')
                elif kind == 'invalid-manifest':
                    path.write_text(json.dumps({**service.manifest, 'extra': 'private'}))
                elif kind == 'stale':
                    path.write_text(json.dumps({**service.manifest, 'name': 'zavliq-20000101T000000Z.tar.age'}))
                elif kind == 'public-mode':
                    path.chmod(0o640)
                elif kind == 'symlink':
                    target = path.with_name('target.json')
                    path.rename(target)
                    path.symlink_to(target)
                elif kind == 'directory':
                    path.unlink()
                    path.mkdir()
                elif kind == 'fifo':
                    path.unlink()
                    os.mkfifo(path, 0o600)
                with self.assertRaisesRegex(fetch.BackupError, '^BACKUP_EXPECTED_MANIFEST_INVALID$'):
                    self.invoke(service, directory, expected_manifest=path, existing_only=True)
                self.assertFalse(service.calls)

    def test_each_pin_field_mismatch_fails_before_aws_or_archive_download(self):
        next_name = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)).strftime('zavliq-%Y%m%dT%H%M%SZ.tar.age')
        for change in [{'name': next_name}, {'bytes': 999}, {'sha256': 'b' * 64}]:
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                service = ObjectService().existing()
                path = self.pin(directory, service.manifest)
                service.manifest.update(change)
                with self.assertRaisesRegex(fetch.BackupError, '^BACKUP_EXPECTED_MANIFEST_MISMATCH$'):
                    self.invoke(service, directory, expected_manifest=path, existing_only=True)
                self.assertEqual(len(service.calls), 1)
                self.assertEqual(service.calls[0][-1], 'sudo python3 -')

    def test_pin_is_not_reloaded_to_accept_a_newer_latest_snapshot(self):
        service = ObjectService().existing()
        with tempfile.TemporaryDirectory() as directory:
            path = self.pin(directory, service.manifest)
            def changed_latest(argv, **kwargs):
                service.manifest['name'] = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)).strftime('zavliq-%Y%m%dT%H%M%SZ.tar.age')
                path.write_text(json.dumps(service.manifest))
                return service.run(argv, **kwargs)
            with patch.object(fetch.subprocess, 'run', side_effect=changed_latest):
                with self.assertRaisesRegex(fetch.BackupError, '^BACKUP_EXPECTED_MANIFEST_MISMATCH$'):
                    fetch.main(self.args(directory, expected_manifest=path, existing_only=True))
        self.assertEqual(len(service.calls), 1)

    def test_existing_only_missing_objects_never_create_or_download(self):
        for missing, code in [('archive', 'BACKUP_EXISTING_ARCHIVE_REQUIRED'),
                              ('sidecar', 'BACKUP_EXISTING_SIDECAR_REQUIRED')]:
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as directory:
                service = ObjectService().existing()
                del service.objects[service.key + ('.sha256' if missing == 'sidecar' else '')]
                path = self.pin(directory, service.manifest)
                before = sorted(Path(directory).iterdir())
                with self.assertRaisesRegex(fetch.BackupError, '^' + code + '$'):
                    self.invoke(service, directory, expected_manifest=path, existing_only=True)
                self.assertEqual(sorted(Path(directory).iterdir()), before)
                self.assertFalse(service.writes)
                self.assertFalse(service.downloads)

    def test_existing_only_conflicting_or_disappearing_sidecar_never_falls_back_to_creation(self):
        for disappears in [False, True]:
            with self.subTest(disappears=disappears), tempfile.TemporaryDirectory() as directory:
                service = ObjectService().existing()
                path = self.pin(directory, service.manifest)
                if not disappears:
                    service.objects[service.key + '.sha256']['body'] = b'x' * len(service.sidecar)
                def command(argv, **kwargs):
                    if disappears and argv[:3] == ['aws', 's3api', 'get-object']:
                        service.calls.append(argv)
                        del service.objects[service.key + '.sha256']
                        raise subprocess.CalledProcessError(1, 'aws', stderr=b'private vanished sidecar')
                    return service.run(argv, **kwargs)
                with patch.object(fetch.subprocess, 'run', side_effect=command):
                    with self.assertRaises(subprocess.CalledProcessError if disappears else fetch.BackupError):
                        fetch.main(self.args(directory, expected_manifest=path, existing_only=True))
                self.assertFalse(service.writes)
                self.assertFalse(service.downloads)
                self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_public_output_omits_snapshot_details_for_existing_and_new_objects(self):
        allowed = {'status', 'local_ciphertext_sha256_verified', 's3_sha256_validated_on_create',
                   'remote_metadata_matches', 'checksum_sidecar_readback_verified',
                   'off_host_ciphertext_readback_verified'}
        for existing in [False, True]:
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory:
                service = ObjectService()
                if existing:
                    service.existing()
                path = self.pin(directory, service.manifest)
                result = self.invoke(service, directory, expected_manifest=path,
                                     existing_only=existing, public_output=True)
                self.assertEqual(set(result), allowed)
                self.assertEqual(result['status'], 'already_present' if existing else 'uploaded')
                self.assertEqual(result['local_ciphertext_sha256_verified'], not existing)
                self.assertEqual(result['s3_sha256_validated_on_create'], not existing)
                self.assertTrue(result['remote_metadata_matches'])
                self.assertTrue(result['checksum_sidecar_readback_verified'])
                self.assertFalse(result['off_host_ciphertext_readback_verified'])
                for value in [service.name, service.digest, str(len(service.payload)), 'synthetic-backups']:
                    self.assertNotIn(value, json.dumps(result))
                self.assertEqual(len(service.downloads), 0 if existing else 1)
                self.assertEqual(len(service.writes), 0 if existing else 2)

    def test_cli_accepts_pinned_existing_only_public_check(self):
        service = ObjectService().existing()
        with tempfile.TemporaryDirectory() as directory:
            path = self.pin(directory, service.manifest)
            arguments = [str(spec.origin), '--ssh-config', 'unused', '--bucket', 'synthetic-backups',
                         '--directory', directory, '--expected-manifest', str(path),
                         '--existing-only', '--public-output']
            output, errors = io.StringIO(), io.StringIO()
            with patch.object(subprocess, 'run', side_effect=service.run), patch.object(sys, 'argv', arguments), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                runpy.run_path(str(spec.origin), run_name='__main__')
        self.assertEqual(errors.getvalue(), '')
        self.assertEqual(json.loads(output.getvalue())['status'], 'already_present')
        self.assertNotIn(service.name, output.getvalue())
        self.assertNotIn(service.digest, output.getvalue())
        self.assertFalse(service.writes)
        self.assertFalse(service.downloads)

    def test_existing_archive_conflicts_never_download_or_overwrite(self):
        for change in [{'metadata': {'sha256': 'b' * 64}}, {'metadata': {}},
                       {'body': b'other-sized object'}, {'encryption': 'aws:kms'}]:
            with self.subTest(change=change):
                service = ObjectService().existing()
                service.objects[service.key].update(change)
                before = json.dumps(service.objects, default=lambda data: data.hex(), sort_keys=True)
                self.assert_failure(service, RuntimeError, 'BACKUP_REMOTE_KEY_CONFLICT')
                self.assertFalse(service.downloads)
                self.assertFalse(service.writes)
                self.assertEqual(json.dumps(service.objects, default=lambda data: data.hex(), sort_keys=True), before)

    def test_existing_sidecar_conflict_is_read_and_preserved_before_archive_download(self):
        service = ObjectService()
        wrong = ('b' * 64 + '  ' + service.name + '\n').encode()
        service.objects[service.key + '.sha256'] = service.object(wrong)
        self.assert_failure(service, RuntimeError, 'BACKUP_REMOTE_KEY_CONFLICT')
        self.assertFalse(service.downloads)
        self.assertFalse(service.writes)
        self.assertEqual(service.objects[service.key + '.sha256']['body'], wrong)

    def test_conditional_archive_create_preserves_concurrent_winner(self):
        service = ObjectService()
        winner = b'concurrent archive that must survive'
        def race(key):
            service.objects[key] = service.object(winner)
        service.before_put = race
        self.assert_failure(service, subprocess.CalledProcessError)
        self.assertEqual(service.objects[service.key]['body'], winner)
        self.assertEqual(len(service.writes), 1)
        self.assertNotIn(service.key + '.sha256', service.objects)

    def test_conditional_sidecar_create_preserves_concurrent_winner(self):
        service = ObjectService()
        winner = b'concurrent sidecar that must survive'
        def race(key):
            if key.endswith('.sha256'):
                service.objects[key] = service.object(winner)
        service.before_put = race
        self.assert_failure(service, subprocess.CalledProcessError)
        self.assertEqual(service.objects[service.key]['body'], service.payload)
        self.assertEqual(service.objects[service.key + '.sha256']['body'], winner)
        self.assertEqual(len(service.writes), 2)

    def test_download_hash_mismatch_never_uploads(self):
        service = ObjectService()
        service.manifest['sha256'] = 'b' * 64
        self.assert_failure(service, RuntimeError, 'Encrypted backup size/checksum mismatch')
        self.assertEqual(len(service.downloads), 1)
        self.assertFalse(service.writes)

    def test_access_denial_does_not_fall_back_to_download(self):
        with patch.object(fetch.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stderr='private AccessDenied')):
            with self.assertRaisesRegex(RuntimeError, 'Cannot inspect'):
                fetch.matching_object('synthetic', 'daily/archive', 'a' * 64, 1)

    def test_download_timeout_preserves_local_bytes_and_never_uploads(self):
        service = ObjectService()
        service.download_timeout = True
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(fetch.subprocess, 'run', side_effect=service.run):
                with self.assertRaises(subprocess.TimeoutExpired):
                    fetch.main(self.args(directory))
            self.assertEqual((Path(directory) / service.name).read_bytes(), service.payload)
        self.assertFalse(service.writes)

    def test_ambiguous_archive_put_does_not_claim_success_or_retry_overwrite(self):
        service = ObjectService()
        def lost_response(key):
            raise subprocess.TimeoutExpired('aws', 1, stderr=b'private response')
        service.after_put = lost_response
        self.assert_failure(service, subprocess.TimeoutExpired)
        self.assertEqual(len(service.writes), 1)
        self.assertEqual(service.objects[service.key]['body'], service.payload)
        self.assertNotIn(service.key + '.sha256', service.objects)
        service.after_put = None
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(service, directory)
        self.assertEqual(result['status'], 'already_present')
        self.assertFalse(result['local_ciphertext_sha256_verified'])
        self.assertFalse(result['off_host_ciphertext_readback_verified'])
        self.assertEqual(len(service.writes), 2)  # Only the missing sidecar is created on retry.

    def test_ambiguous_sidecar_put_can_resume_without_overwriting_either_object(self):
        service = ObjectService()
        def lost_response(key):
            if key.endswith('.sha256'):
                raise subprocess.TimeoutExpired('aws', 1)
        service.after_put = lost_response
        self.assert_failure(service, subprocess.TimeoutExpired)
        self.assertEqual(len(service.writes), 2)
        service.after_put = None
        with tempfile.TemporaryDirectory() as directory:
            result = self.invoke(service, directory)
        self.assertEqual(len(service.writes), 2)
        self.assertEqual(result['status'], 'already_present')
        self.assertFalse(result['off_host_ciphertext_readback_verified'])

    def test_unconfirmed_put_response_does_not_publish_success(self):
        service = ObjectService()
        def command(argv, **kwargs):
            response = service.run(argv, **kwargs)
            if argv[:3] == ['aws', 's3api', 'put-object']:
                response.stdout = json.dumps({'ChecksumSHA256': 'wrong', 'ServerSideEncryption': 'AES256'})
            return response
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with patch.object(fetch.subprocess, 'run', side_effect=command), contextlib.redirect_stdout(output):
                with self.assertRaisesRegex(RuntimeError, 'BACKUP_CONDITIONAL_UPLOAD_UNCONFIRMED'):
                    fetch.main(self.args(directory))
            self.assertEqual(output.getvalue(), '')
        self.assertEqual(len(service.writes), 1)
        self.assertNotIn(service.key + '.sha256', service.objects)

    def test_new_single_put_cap_is_decimal_and_precedes_download(self):
        self.assertEqual(fetch.MAX_NEW_UPLOAD_BYTES, 5_000_000_000)
        service = ObjectService()
        service.manifest['bytes'] = fetch.MAX_NEW_UPLOAD_BYTES + 1
        self.assertEqual(fetch.validate_manifest(service.manifest), service.manifest)
        self.assert_failure(service, RuntimeError, 'BACKUP_NEW_UPLOAD_EXCEEDS_SINGLE_PUT_LIMIT')
        self.assertFalse(service.downloads)
        self.assertFalse(service.writes)

    def test_put_cap_boundary_without_allocating_large_file(self):
        digest = 'a' * 64
        response = SimpleNamespace(stdout=json.dumps({'ChecksumSHA256': base64.b64encode(bytes.fromhex(digest)).decode(), 'ServerSideEncryption': 'AES256'}))
        for size, accepted in [(5_000_000_000, True), (5_000_000_001, False)]:
            with self.subTest(size=size), patch.object(Path, 'stat', return_value=SimpleNamespace(st_size=size)), patch.object(fetch, 'command', return_value=response) as command:
                if accepted:
                    fetch.create_object('synthetic', 'daily/archive', Path('synthetic'), digest, fetch.Deadline(), 60)
                    self.assertEqual(command.call_args.args[0][command.call_args.args[0].index('--content-length') + 1], str(size))
                else:
                    with self.assertRaisesRegex(RuntimeError, 'SINGLE_PUT_LIMIT'):
                        fetch.create_object('synthetic', 'daily/archive', Path('synthetic'), digest, fetch.Deadline(), 60)
                    command.assert_not_called()

    def test_cli_errors_redact_remote_output_and_keep_specific_cap_code(self):
        for oversized in [False, True]:
            with self.subTest(oversized=oversized), tempfile.TemporaryDirectory() as directory:
                service = ObjectService()
                service.manifest['bytes'] = fetch.MAX_NEW_UPLOAD_BYTES + 1
                def command(argv, **kwargs):
                    if oversized:
                        return service.run(argv, **kwargs)
                    raise subprocess.CalledProcessError(1, 'private command', output='private output', stderr='private credential marker')
                arguments = [str(spec.origin), '--ssh-config', 'unused', '--bucket', 'synthetic',
                             '--directory', directory, '--public-output']
                output, errors = io.StringIO(), io.StringIO()
                with patch.object(subprocess, 'run', side_effect=command), patch.object(sys, 'argv', arguments), \
                        contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    with self.assertRaises(SystemExit) as result:
                        runpy.run_path(str(spec.origin), run_name='__main__')
                self.assertEqual(result.exception.code, 1)
                self.assertNotIn('private', output.getvalue() + errors.getvalue())
                self.assertEqual(errors.getvalue(), '')
                self.assertEqual(set(json.loads(output.getvalue())), {'ok', 'code', 'error_class', 'action'})
                self.assertEqual(json.loads(output.getvalue())['code'],
                                 'BACKUP_NEW_UPLOAD_EXCEEDS_SINGLE_PUT_LIMIT' if oversized else 'BACKUP_TRANSFER_FAILED')

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
            with patch.object(fetch.subprocess, 'run', side_effect=command):
                with self.assertRaisesRegex(TimeoutError, 'BACKUP_TRANSFER_TIMEOUT'):
                    fetch.main(self.args(directory), fetch.Deadline(10))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], 10)

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
