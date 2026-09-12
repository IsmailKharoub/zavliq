"""Real snapshot archives and file lifecycles; all age/Docker/systemd calls mocked."""
import io
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
from subprocess import CompletedProcess
import tarfile
import time
import unittest
from unittest.mock import patch

import test_activate as base
import restore


class RestoreRig(base.HostRig):
    def __init__(self, paths, plain):
        super().__init__(paths)
        self.plain = plain
        self.streams = []
        self.decrypt_fails = False
        self.public_key = 'age1' + 'b' * 58

    def run(self, args, **kwargs):
        if args[0] in ('age', 'age-keygen') or args[:3] in (
                ['docker', 'context', 'show'], ['docker', 'network', 'ls'], ['docker', 'volume', 'create']):
            self.calls.append((args, kwargs))
            stdout = ''
            if args[0] == 'age-keygen': stdout = self.public_key
            elif args[0] == 'age':
                if self.decrypt_fails: raise RuntimeError('private diagnostic must not be logged')
                shutil.copyfile(self.plain, Path(args[args.index('--output') + 1]))
            elif args[:3] == ['docker', 'context', 'show']: stdout = 'default'
            elif args[:3] == ['docker', 'network', 'ls']: stdout = 'bridge\nhost\nnone' if '--format' in args else ''
            return CompletedProcess(args, 0, stdout, '')
        return super().run(args, **kwargs)

    def stream(self, args, source, values, timeout=600):
        body = source.read_bytes()
        self.streams.append((args, body))
        if args[:2] == ['docker', 'run']:
            assert '-i' in args and '--pull=never' in args
            assert args[args.index('--network') + 1] == 'none'
            with tarfile.open(fileobj=io.BytesIO(body)) as archive:
                assert archive.getmembers()
        else:
            assert body.startswith(b'PGDMP')
            assert 'pg_restore' in args
        return CompletedProcess(args, 0, b'', b'')


class RestoreTests(unittest.TestCase):
    write_env = base.ActivationTests.write_env

    def setUp(self):
        base.ActivationTests.setUp(self)
        self.old_rig = self.rig
        self.rid = 'recovery-test-0001'
        self.transport = self.root / 'transport.age'
        self.transport.write_bytes(b'synthetic transport ciphertext')
        self.transport.chmod(0o600)
        self.key = self.root / 'one-use.key'
        self.key.write_text('synthetic one-use identity')
        self.key.chmod(0o600)
        self.plain = self.root / 'snapshot.tar'
        source_env = {**self.env, 'ZAVLIQ_STATE_DIR': '/etc/zavliq', 'ZAVLIQ_RELEASE': self.manifest['revision']}
        self.files = {name: b'synthetic private state' for name in restore.REQUIRED}
        self.files['postgres.dump'] = b'PGDMP\x00synthetic custom archive'
        self.files['created_at'] = b'2026-09-12T06:00:00Z\n'
        self.files['secrets/compose.env'] = ''.join(f'{k}={v}\n' for k, v in source_env.items()).encode()
        for name in restore.SECRET_NAMES: self.files['secrets/' + name] = b'x' * 48 + b'\n'
        self.rearchive()
        self.rig = RestoreRig(self.paths, self.plain)
        self.command.stop()
        self.command = patch.object(restore.act, 'command', side_effect=self.rig.run)
        self.command.start()
        self.backup.stop()
        self.backup = patch.object(restore.act, 'backup_command', side_effect=lambda script, env: self.rig.run(['bash', str(script)], env=env))
        self.backup.start()
        self.stream_patch = patch.object(restore, 'stream_command', side_effect=self.rig.stream)
        self.stream_patch.start()
        self.machine = patch.object(restore, 'machine_id', return_value='c' * 32)
        self.machine.start()
        self.input = self.root / 'input-receipt.json'
        self.value = {'schema': restore.INPUT_SCHEMA, 'restore_id': self.rid, 'target_machine_id': 'c' * 32,
                      'bundle_id': self.release.name, 'manifest_sha256': self.manifest_hash,
                      'revision': self.manifest['revision'], 'images': self.manifest['images'],
                      'server_name': 'zavliq.com', 'origin': restore.act.bundle.ORIGIN,
                      'source_binding_reviewed': True, 'primary_identity_off_host': True,
                      'transport_key_single_use': True, 'fresh_host_isolated': True,
                      'primary_recipient': 'age1' + 'a' * 58, 'transport_recipient': self.rig.public_key,
                      'original': {'name': 'zavliq-20260912T060001Z.tar.age', 'sha256': 'd' * 64,
                                   'bytes': 12345, 'created_at': '2026-09-12T06:00:00Z'},
                      'transport': {'bytes': self.transport.stat().st_size, 'sha256': restore.act.bundle.sha256(self.transport)},
                      'plaintext': {'bytes': self.plain.stat().st_size, 'sha256': restore.act.bundle.sha256(self.plain)}}
        self.write_input()
        self.paths.env.unlink()
        for name in restore.SECRET_NAMES: (self.paths.state / name).unlink()

    def tearDown(self):
        self.machine.stop()
        self.stream_patch.stop()
        base.ActivationTests.tearDown(self)

    def rearchive(self, extra=None):
        with tarfile.open(self.plain, 'w') as archive:
            for name, body in self.files.items():
                member = tarfile.TarInfo('./' + name)
                member.size, member.mode = len(body), 0o644
                archive.addfile(member, io.BytesIO(body))
            if extra: archive.addfile(extra, io.BytesIO(b'x') if extra.size else None)
        self.plain.chmod(0o600)

    def write_input(self):
        restore.act.write_json(self.input, self.value)
        self.input_hash = restore.act.bundle.sha256(self.input)

    def refresh_snapshot(self):
        self.rearchive()
        self.value['plaintext'] = {'bytes': self.plain.stat().st_size, 'sha256': restore.act.bundle.sha256(self.plain)}
        self.write_input()

    def prepare(self):
        return restore.prepare(self.paths, self.rid, self.release.name, self.manifest_hash,
                               self.input, self.input_hash, self.transport, self.key)

    def verification(self):
        path = restore.record_path(self.paths, self.rid)
        record_hash = restore.act.bundle.sha256(path)
        now = int(time.time())
        value = {'schema': restore.VERIFY_SCHEMA, 'restore_id': self.rid, 'restore_record_sha256': record_hash,
                 'target_machine_id': 'c' * 32, 'manifest_sha256': self.manifest_hash, 'origin': restore.act.bundle.ORIGIN,
                 'verified_at': now, 'expires_at': now + 1800, 'operator_attestations': dict.fromkeys(restore.ATTESTATIONS, True)}
        verification = self.root / 'verification.json'
        restore.act.write_json(verification, value)
        return path, record_hash, verification, value

    def complete(self, record_hash, path):
        return restore.complete(self.paths, self.rid, record_hash, path, restore.act.bundle.sha256(path))

    def test_real_snapshot_prepare_preserves_private_modes_and_only_starts_core(self):
        previous_umask = os.umask(0o077)
        try: report = self.prepare()
        finally: os.umask(previous_umask)
        self.assertEqual(report['phase'], 'restored_private')
        self.assertFalse(report['canonical_https_checked'])
        self.assertFalse(self.key.exists())
        self.assertEqual(json.loads(self.paths.active.read_text())['status'], 'activating')
        self.assertIn('compose -- "$@"', self.paths.wrapper.read_text())
        self.assertEqual(self.paths.current.resolve(), self.release)
        for name in restore.SECRET_NAMES:
            self.assertEqual((self.paths.state / name).read_bytes(), self.files['secrets/' + name])
            self.assertEqual((self.paths.state / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.rig.backup_count, 0)
        commands = [args for args, _ in self.rig.calls]
        self.assertFalse(any('echo-bootstrap' in args or 'enable' in args for args in commands))
        self.assertEqual([args[-1] for args in commands if 'up' in args], ['postgres', 'control'])
        self.assertFalse(any('up' in args and ('gateway' in args or 'echo' in args) for args in commands))
        self.assertEqual(len(self.rig.streams), 6)
        for args, body in self.rig.streams[:4]:
            self.assertTrue(any(image['id'] in args for image in self.manifest['images'].values()))
            with tarfile.open(fileobj=io.BytesIO(body)) as archive:
                for member in archive:
                    self.assertEqual(member.mode & 0o077, 0)
        self.assertFalse(any('down' in args or 'pull' in args or 'build' in args for args in commands))

    def test_receipt_target_images_or_namespace_mismatch_never_decrypts(self):
        for key, bad in [('target_machine_id', 'f' * 32), ('origin', 'http://localhost:8080'), ('primary_identity_off_host', False), ('images', {})]:
            original = self.value[key]
            self.value[key] = bad
            self.write_input()
            with self.subTest(key=key), self.assertRaises(ValueError): self.prepare()
            self.value[key] = original
        self.assertEqual(self.rig.calls, [])
        self.assertTrue(self.key.exists())

    def test_existing_daemon_or_state_refuses_before_any_restore_mutation(self):
        self.rig.existing_resources = True
        with self.assertRaisesRegex(ValueError, 'NO_EXISTING_PRODUCTION'): self.prepare()
        self.assertTrue(self.key.exists())
        self.assertFalse(self.paths.env.exists())
        self.assertFalse(any(args[0] == 'age' or args[:2] == ['docker', 'load'] for args, _ in self.rig.calls))

    def test_primary_key_or_unrelated_key_is_refused_and_never_deleted(self):
        self.rig.public_key = self.value['primary_recipient']
        with self.assertRaisesRegex(ValueError, 'MATCHING_NONPRIMARY'): self.prepare()
        self.assertTrue(self.key.exists())
        self.assertFalse(any(args[0] == 'age' for args, _ in self.rig.calls))

    def test_matched_transport_key_consumed_on_decrypt_failure_without_state_mutation(self):
        self.rig.decrypt_fails = True
        with self.assertRaises(RuntimeError): self.prepare()
        self.assertFalse(self.key.exists())
        self.assertFalse(self.paths.env.exists())
        self.assertFalse(self.paths.active.exists())

    def test_plaintext_hash_mismatch_rejected_before_volume_creation(self):
        self.value['plaintext']['sha256'] = 'e' * 64
        self.write_input()
        with self.assertRaisesRegex(ValueError, 'REVIEWED_INPUT_HASH'): self.prepare()
        self.assertFalse(self.key.exists())
        self.assertFalse(any(args[:3] == ['docker', 'volume', 'create'] for args, _ in self.rig.calls))

    def test_missing_original_signing_or_echo_state_is_never_reprovisioned(self):
        del self.files['synapse/signing.key']
        self.refresh_snapshot()
        with self.assertRaisesRegex(ValueError, 'COMPLETE_ORIGINAL'): self.prepare()
        self.assertFalse(self.paths.env.exists())
        self.assertFalse(any('echo-bootstrap' in args or 'up' in args for args, _ in self.rig.calls))

    def test_missing_matrix_sdk_sync_state_refuses_before_mutation(self):
        del self.files['echo/runtime/matrix/matrix-sdk-state.sqlite3']
        self.refresh_snapshot()
        with self.assertRaisesRegex(ValueError, 'COMPLETE_ORIGINAL'): self.prepare()
        self.assertFalse(self.paths.env.exists())
        self.assertFalse(any('up' in args or args[:3] == ['docker', 'volume', 'create'] for args, _ in self.rig.calls))

    def test_oversized_receipt_is_rejected_before_hashing(self):
        with self.input.open('wb') as output:
            output.truncate(2 * 1024 * 1024 + 1)
        with patch.object(restore.act.bundle, 'require_hash') as hashed:
            with self.assertRaisesRegex(ValueError, 'RESTORE_RECEIPT_TOO_LARGE'):
                restore.receipt(self.input, self.input_hash)
        hashed.assert_not_called()

    def test_late_exact_volume_collision_is_preserved_without_create_or_extract(self):
        original = self.rig.run
        def collision(args, **kwargs):
            if args == ['docker', 'volume', 'ls', '--quiet', '--filter', 'name=zavliq-production_synapse']:
                self.rig.calls.append((args, kwargs))
                return CompletedProcess(args, 0, 'zavliq-production_synapse\n', '')
            return original(args, **kwargs)
        with patch.object(restore.act, 'command', side_effect=collision):
            with self.assertRaisesRegex(ValueError, 'RESTORE_VOLUME_COLLISION'): self.prepare()
        self.assertFalse(self.rig.streams)
        self.assertFalse(any(args[:3] == ['docker', 'volume', 'create'] or 'down' in args for args, _ in self.rig.calls))
        self.assertEqual(json.loads(restore.record_path(self.paths, self.rid).read_text())['phase'], 'failed')

    def test_real_helper_guard_refuses_nonempty_volume_before_tar_extraction(self):
        self.prepare()
        args, body = self.rig.streams[0]
        # Execute the actual fixed helper guard + tar command in an isolated
        # local directory; omit only its privileged ownership/mode suffix.
        helper = args[-1].split(' && chown', 1)[0]
        for kind in ('ordinary', 'hidden', 'dangling'):
            directory = self.root / ('existing-' + kind)
            directory.mkdir()
            marker = directory / ('.hidden' if kind == 'hidden' else 'existing')
            if kind == 'dangling': marker.symlink_to('absent-target')
            else: marker.write_bytes(b'preserve existing fixture')
            command = helper.replace('/restore', shlex.quote(str(directory)))
            result = subprocess.run(['sh', '-c', command], input=body, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 73)
            self.assertEqual(list(directory.iterdir()), [marker])
            if kind != 'dangling': self.assertEqual(marker.read_bytes(), b'preserve existing fixture')
            else: self.assertTrue(marker.is_symlink())
        empty = self.root / 'empty-target'
        empty.mkdir()
        result = subprocess.run(['sh', '-c', helper.replace('/restore', shlex.quote(str(empty)))],
                                input=body, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertTrue((empty / 'signing.key').is_file())

    def test_source_stage_namespace_refused_before_mutation(self):
        self.files['secrets/compose.env'] = self.files['secrets/compose.env'].replace(b'ZAVLIQ_SERVER_NAME=zavliq.com', b'ZAVLIQ_SERVER_NAME=localhost')
        self.refresh_snapshot()
        with self.assertRaisesRegex(ValueError, 'ORIGINAL_PUBLIC_NAMESPACE'): self.prepare()
        self.assertFalse(self.paths.env.exists())

    def test_real_archive_rejects_links_traversal_duplicate_and_expansion(self):
        members = []
        link = tarfile.TarInfo('synapse/link'); link.type = tarfile.SYMTYPE; link.linkname = '/outside'; members.append(link)
        traversal = tarfile.TarInfo('../outside'); traversal.size = 1; members.append(traversal)
        duplicate = tarfile.TarInfo('postgres.dump'); duplicate.size = 1; members.append(duplicate)
        for index, member in enumerate(members):
            self.rearchive(member)
            with self.subTest(member=member.name), self.assertRaisesRegex(ValueError, 'UNSAFE_OR_DUPLICATE'):
                restore.extract_snapshot(self.plain, self.root / ('extract-' + str(index)))
        self.rearchive()
        with self.assertRaisesRegex(ValueError, 'SNAPSHOT_EXPANSION_LIMIT'):
            restore.extract_snapshot(self.plain, self.root / 'small', maximum=1)

    def test_private_core_start_failure_stops_writers_and_preserves_failed_state(self):
        self.rig.fail_up = True
        with self.assertRaises(RuntimeError): self.prepare()
        report = json.loads(restore.record_path(self.paths, self.rid).read_text())
        self.assertEqual(report['phase'], 'failed')
        self.assertEqual(json.loads(self.paths.active.read_text())['status'], 'failed')
        self.assertTrue(any('stop' in args and 'postgres' in args for args, _ in self.rig.calls))
        self.assertFalse(any('down' in args for args, _ in self.rig.calls))
        self.assertTrue(self.paths.env.exists())
        self.assertFalse(self.key.exists())
        with self.assertRaises(ValueError): self.prepare()

    def test_persistent_marker_failure_does_not_skip_independent_stop(self):
        original = restore.act.write_json
        def fail_marker(path, value):
            if path == self.paths.active: raise OSError('persistent marker fixture')
            return original(path, value)
        with patch.object(restore.act, 'write_json', side_effect=fail_marker), self.assertRaises(OSError): self.prepare()
        self.assertTrue(any('stop' in args and 'postgres' in args for args, _ in self.rig.calls))
        report = json.loads(restore.record_path(self.paths, self.rid).read_text())
        self.assertTrue(report['marker_write_incomplete'])
        self.assertEqual(report['phase'], 'failed')

    def test_stale_or_incomplete_verification_cannot_adopt_restore(self):
        self.prepare()
        path, record_hash, verification, value = self.verification()
        value['operator_attestations']['source_writers_and_echo_fenced'] = False
        restore.act.write_json(verification, value)
        with self.assertRaisesRegex(ValueError, 'CURRENT_SAME_ORIGIN'): self.complete(record_hash, verification)
        value['operator_attestations']['source_writers_and_echo_fenced'] = True
        value['expires_at'] = int(time.time()) - 1
        restore.act.write_json(verification, value)
        with self.assertRaisesRegex(ValueError, 'CURRENT_SAME_ORIGIN'): self.complete(record_hash, verification)
        self.assertEqual(json.loads(path.read_text())['phase'], 'restored_private')
        self.assertEqual(self.rig.backup_count, 0)

    def test_complete_checks_actual_https_images_backup_and_adopts_without_new_starts(self):
        self.prepare()
        path, record_hash, verification, _ = self.verification()
        self.rig.calls.clear()
        report = self.complete(record_hash, verification)
        self.assertEqual(report['phase'], 'complete')
        self.assertTrue(report['canonical_https_checked'])
        self.assertTrue(report['external_fencing_route_and_clients_operator_attested'])
        self.assertFalse(report['public_traffic_opened_by_tool'])
        self.assertFalse(report['off_host_backup_verified'])
        self.assertEqual(self.rig.backup_count, 1)
        self.assertEqual(json.loads(self.paths.active.read_text())['status'], 'ready')
        self.assertTrue(any(args[:3] == ['systemctl', 'enable', '--now'] for args, _ in self.rig.calls))
        self.assertFalse(any('up' in args or 'echo-bootstrap' in args for args, _ in self.rig.calls))
        with self.assertRaises(ValueError): self.complete(record_hash, verification)

    def test_binary_restore_stream_is_not_decoded_or_retained_in_output(self):
        self.stream_patch.stop()
        source = self.root / 'binary.dump'
        body = b'PGDMP\x00\xff\xfe\x80payload'
        source.write_bytes(body)
        captured = []
        def run(args, **kwargs):
            captured.append(kwargs['stdin'])
            self.assertEqual(kwargs['stdin'].read(), body)
            self.assertEqual(kwargs['stdout'], restore.subprocess.DEVNULL)
            self.assertEqual(kwargs['stderr'], restore.subprocess.PIPE)
            return CompletedProcess(args, 0)
        with patch.object(restore.subprocess, 'run', side_effect=run):
            restore.stream_command(['controlled-test-only'], source, {})
        self.assertTrue(captured[0].closed)

    def test_complete_actual_https_failure_cannot_mark_ready_or_run_backup(self):
        self.prepare()
        _, record_hash, verification, _ = self.verification()
        with patch.object(restore.act, 'public_readiness', side_effect=ValueError('PUBLIC_HTTPS_HEALTH_REQUIRED')):
            with self.assertRaisesRegex(ValueError, 'PUBLIC_HTTPS_HEALTH_REQUIRED'):
                self.complete(record_hash, verification)
        self.assertEqual(self.rig.backup_count, 0)
        self.assertEqual(json.loads(self.paths.active.read_text())['status'], 'failed')
        self.assertFalse(any('enable' in args for args, _ in self.rig.calls))

    def test_complete_intent_write_failure_still_attempts_writer_cleanup(self):
        self.prepare()
        output, record_hash, verification, _ = self.verification()
        original = restore.act.write_json
        def fail_record(path, value):
            if path == output: raise OSError('persistent restore journal failure')
            return original(path, value)
        with patch.object(restore.act, 'write_json', side_effect=fail_record), self.assertRaises(OSError):
            self.complete(record_hash, verification)
        self.assertEqual(json.loads(self.paths.active.read_text())['status'], 'failed')
        self.assertTrue(any('stop' in args and 'postgres' in args for args, _ in self.rig.calls))
        self.assertEqual(self.rig.backup_count, 0)

    def test_final_journal_failure_after_enable_disables_timers_and_stops_jobs_before_writers(self):
        self.prepare()
        output, record_hash, verification, _ = self.verification()
        original = restore.act.write_json
        def fail_final_record(path, value):
            if path == output and value.get('phase') == 'complete':
                raise OSError('final evidence fixture failure after schedules enabled')
            return original(path, value)
        self.rig.calls.clear()
        with patch.object(restore.act, 'write_json', side_effect=fail_final_record), self.assertRaises(OSError):
            self.complete(record_hash, verification)
        commands = [args for args, _ in self.rig.calls]
        enabled = next(index for index, args in enumerate(commands) if args[:3] == ['systemctl', 'enable', '--now'])
        writers = next(index for index, args in enumerate(commands) if 'stop' in args and 'postgres' in args)
        for timer in restore.act.TIMERS:
            disabled = next(index for index, args in enumerate(commands) if args[:2] == ['systemctl', 'disable'] and timer in args)
            timer_stopped = next(index for index, args in enumerate(commands) if args[:2] == ['systemctl', 'stop'] and timer in args)
            service_stopped = next(index for index, args in enumerate(commands) if args[:2] == ['systemctl', 'stop'] and timer.replace('.timer', '.service') in args)
            self.assertLess(enabled, disabled)
            self.assertLess(disabled, writers)
            self.assertLess(enabled, timer_stopped)
            self.assertLess(timer_stopped, service_stopped)
            self.assertLess(service_stopped, writers)
        self.assertEqual(json.loads(self.paths.active.read_text())['status'], 'failed')
        self.assertEqual(json.loads(output.read_text())['phase'], 'failed')
        self.assertEqual(self.rig.backup_count, 1)

    def test_paused_private_core_with_cached_healthy_status_is_rejected(self):
        original = self.rig.run
        def paused(args, **kwargs):
            if args[:2] == ['docker', 'inspect'] and args[-1] == 'control-container':
                self.rig.calls.append((args, kwargs))
                return CompletedProcess(args, 0, self.manifest['images']['control']['id'] + '|true|true|healthy', '')
            return original(args, **kwargs)
        with patch.object(restore.act, 'command', side_effect=paused):
            with self.assertRaisesRegex(ValueError, 'RESTORED_CORE_IMAGE_OR_HEALTH_MISMATCH'): self.prepare()
        self.assertEqual(json.loads(self.paths.active.read_text())['status'], 'failed')
        self.assertTrue(any('stop' in args and 'postgres' in args for args, _ in self.rig.calls))

    def test_complete_backup_failure_keeps_failed_state_and_stops_writers(self):
        self.prepare()
        _, record_hash, verification, _ = self.verification()
        self.rig.fail_backup = True
        with self.assertRaises(RuntimeError): self.complete(record_hash, verification)
        self.assertEqual(json.loads(self.paths.active.read_text())['status'], 'failed')
        self.assertTrue(any('stop' in args and 'postgres' in args for args, _ in self.rig.calls))
        self.assertFalse(any('enable' in args for args, _ in self.rig.calls))


if __name__ == '__main__': unittest.main()
