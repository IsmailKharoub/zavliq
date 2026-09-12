"""Production transition tests use real local files/archives and mocked host tools."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import shlex
from subprocess import CompletedProcess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import activate
import test_prepare_bundle as fixtures

INFRA = Path(__file__).resolve().parents[2]


def extra_source():
    names = ['scripts/backup.sh', 'scripts/healthcheck.py'] + [
        'systemd/zavliq-' + name + '.' + kind for name in ['backup', 'health', 'retention'] for kind in ['service', 'timer']]
    return {'infra/' + name: (INFRA / name).read_bytes() for name in names}


def make_public(root, paths, revision):
    root.mkdir()
    with patch.object(fixtures, 'REVISION', revision):
        fixture = fixtures.Fixture(root, extra_source=extra_source())
        rig = fixtures.DockerRig(fixture)
        with patch.object(fixtures.bundle, 'checked', side_effect=rig.run):
            fixture.prepare()
    manifest = json.loads((fixture.output / 'production-manifest.json').read_text())
    bundle_id = revision + '-public-' + manifest['public_web']['configuration_sha256'][:16]
    destination = paths.releases / bundle_id
    fixture.output.rename(destination)
    return destination, activate.bundle.sha256(destination / 'production-manifest.json'), manifest


def config(manifest, paths):
    images = {**manifest['images'], **{name: manifest['images'][base] for name, base in activate.bundle.HELPERS.items()}}
    services = {name: {'image': image['id'], 'pull_policy': 'never', 'networks': {'backend': {}}} for name, image in images.items()}
    services['echo']['networks'] = {'edge': {}}
    services['gateway'].update(networks={'backend': {}, 'edge': {}}, ports=[
        {'published': '80', 'target': 80, 'protocol': 'tcp'}, {'published': '443', 'target': 443, 'protocol': 'tcp'},
        {'published': '443', 'target': 443, 'protocol': 'udp'}])
    return {'name': activate.PROJECT, 'services': services,
            'networks': {name: {'name': activate.PROJECT + '_' + name, 'internal': name == 'backend'} for name in ['backend', 'edge']},
            'volumes': {'postgres': {'name': activate.PROJECT + '_postgres'}},
            'secrets': {name: {'file': str(paths.state / name)} for name in ['postgres_password', 'registration_secret', 'policy_secret', 'control_secret']}}


class HostRig:
    def __init__(self, paths):
        self.paths, self.calls, self.backup_count = paths, [], 0
        self.fail_backup, self.fail_up, self.existing_resources = False, False, False
        self.config_mutation = None
        self.latest = None

    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        stdout = ''
        if args[:2] == ['docker', 'info']:
            stdout = 'linux/x86_64'
        elif args[:2] == ['docker', 'compose']:
            release = Path(args[args.index('-f') + 1]).parents[1]
            self.latest = json.loads((release / 'production-manifest.json').read_text())
            self.assert_pinned(args, release)
            if 'config' in args:
                value = config(self.latest, self.paths)
                if self.config_mutation: self.config_mutation(value)
                stdout = json.dumps(value)
            elif 'ps' in args:
                stdout = args[-1] + '-container'
            elif 'up' in args and self.fail_up:
                raise RuntimeError('fixture startup failure')
        elif args[:3] == ['docker', 'image', 'ls']:
            pass
        elif args[:3] == ['docker', 'image', 'inspect']:
            stdout = json.dumps({'Id': args[-1], 'Os': 'linux', 'Architecture': 'amd64'})
        elif args[:2] == ['docker', 'inspect']:
            service = args[-1].removesuffix('-container')
            stdout = self.latest['images'][service]['id'] + '|true|healthy'
        elif args[:2] == ['docker', 'load']:
            pass
        elif args[0] == 'docker':
            stdout = 'existing' if self.existing_resources else ''
        elif args[0] == 'systemctl':
            if args[1] == 'show': stdout = 'active'
        elif args[0] == 'bash':
            self.backup_count += 1
            actual = Path(args[1]).read_text()
            original = (INFRA / 'scripts/backup.sh').read_text()
            assert actual == activate.backup_program(original, self.paths.wrapper)
            assert 'compose="$root/infra/scripts/compose.sh"' not in actual
            assert kwargs['env']['ZAVLIQ_ENV_FILE'] == str(self.paths.env)
            assert kwargs['env']['ZAVLIQ_ENVIRONMENT'] == 'production'
            if self.fail_backup: raise RuntimeError('fixture backup failure')
            self.paths.backups.mkdir(parents=True, exist_ok=True)
            stamp = '20260912T1200' + str(self.backup_count).zfill(2) + 'Z'
            archive = self.paths.backups / ('zavliq-' + stamp + '.tar.age')
            archive.write_bytes(b'synthetic encrypted archive ' + str(self.backup_count).encode())
            archive.with_name(archive.name + '.sha256').write_text(activate.bundle.sha256(archive) + '  ' + archive.name + '\n')
            (self.paths.backups / 'last-success').write_text(stamp)
        else:
            raise AssertionError('Unexpected command: ' + repr(args))
        return CompletedProcess(args, 0, stdout, '')

    def assert_pinned(self, args, release):
        files = [args[index + 1] for index, value in enumerate(args) if value == '-f']
        assert files == [str(release / name) for name in ['infra/compose.yaml', 'infra/compose.production.yaml', 'compose.images.yaml']]
        assert args[args.index('--project-name') + 1] == 'zavliq-production'
        assert not any('staging' in name or '.local' in name for name in files)


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.paths = activate.Paths(self.root)
        self.paths.releases.mkdir(parents=True)
        self.paths.state.mkdir(parents=True, mode=0o700)
        self.paths.systemd.mkdir(parents=True)
        self.env = {'COMPOSE_PROJECT_NAME': activate.PROJECT, 'ZAVLIQ_STATE_DIR': str(self.paths.state), 'ZAVLIQ_SERVER_NAME': 'zavliq.com',
                    'ZAVLIQ_PUBLIC_URL': activate.bundle.ORIGIN, 'ZAVLIQ_SITE_ADDRESS': 'zavliq.com', 'ZAVLIQ_RELEASE': 'development'}
        self.write_env()
        for name in ['postgres_password', 'registration_secret', 'policy_secret', 'control_secret']:
            activate.atomic_write(self.paths.state / name, 'synthetic-' + 'x' * 48 + '\n')
        activate.atomic_write(self.paths.operations, 'ZAVLIQ_PUBLIC_URL=https://zavliq.com\nZAVLIQ_BACKUP_RECIPIENT=age1' + 'a' * 58 + '\n')
        self.release, self.manifest_hash, self.manifest = make_public(self.root / 'input', self.paths, 'a' * 40)
        self.rig = HostRig(self.paths)
        self.owner = patch.object(activate, 'ROOT_OWNER', os.getuid())
        self.owner.start()
        self.command = patch.object(activate, 'command', side_effect=self.rig.run)
        self.command.start()
        self.backup = patch.object(activate, 'backup_command', side_effect=lambda script, environment: self.rig.run(['bash', str(script)], env=environment))
        self.backup.start()
        self.readiness = patch.object(activate, 'public_readiness')
        self.readiness.start()
        self.disk = patch.object(activate.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(100 * 1024**3, 0, 100 * 1024**3))
        self.disk.start()

    def tearDown(self):
        self.disk.stop()
        self.readiness.stop()
        self.backup.stop()
        self.command.stop()
        self.owner.stop()
        self.temporary.cleanup()

    def write_env(self):
        activate.atomic_write(self.paths.env, ''.join(key + '=' + value + '\n' for key, value in self.env.items()))

    def deploy(self, current='none', release=None, digest=None):
        return activate.activate(self.paths, (release or self.release).name, digest or self.manifest_hash, current)

    def next_bundle(self):
        return make_public(self.root / 'next-input', self.paths, 'b' * 40)

    def reports(self):
        return [json.loads(path.read_text()) for path in self.paths.evidence.glob('*/activation.json')]

    def test_real_prepared_bundle_verifies_and_fresh_activation_uses_only_exact_public_images(self):
        report = self.deploy()
        self.assertTrue(report['ok'])
        self.assertFalse(report['launch_verified'])
        self.assertEqual(report['images'], self.manifest['images'])
        self.assertEqual(self.rig.backup_count, 1)
        self.assertEqual(json.loads(self.paths.active.read_text())['status'], 'ready')
        self.assertEqual(self.paths.current.resolve(), self.release)
        commands = [args for args, _ in self.rig.calls]
        self.assertFalse(any('pull' in args or 'build' in args or 'down' in args for args in commands))
        self.assertTrue(any('echo-bootstrap' in args for args in commands))
        for name in ['backup', 'retention']:
            dropin = (self.paths.systemd / ('zavliq-' + name + '.service.d/production-pins.conf')).read_text()
            self.assertIn('/opt/zavliq/production-tools/activate.py ' + name, dropin)
        self.assertIn('compose -- "$@"', self.paths.wrapper.read_text())

    def test_current_pointer_collision_refuses_before_env_or_any_mutation(self):
        before = self.paths.env.read_bytes()
        self.paths.current.with_name('current.next').symlink_to(self.release)
        with self.assertRaisesRegex(ValueError, 'UNRESOLVED_CURRENT_POINTER'):
            self.deploy()
        self.assertEqual(self.paths.env.read_bytes(), before)
        self.assertEqual(self.rig.calls, [])
        self.assertFalse(self.paths.active.exists())

    def test_backup_adapter_matches_actual_archived_script_and_refuses_drift(self):
        original = (INFRA / 'scripts/backup.sh').read_text()
        expected = 'compose="$root/infra/scripts/compose.sh"'
        adapted = activate.backup_program(original, self.paths.wrapper)
        self.assertEqual(adapted.replace('compose=' + str(self.paths.wrapper), expected, 1), original)
        self.assertIn('env_file=${ZAVLIQ_ENV_FILE:-"$root/infra/', adapted)
        for changed in [original + '\n' + expected, original.replace(expected, 'compose=other'), original + '\necho "$root"']:
            with self.assertRaisesRegex(ValueError, 'EXACT_BACKUP_COMPOSE_ASSIGNMENT_REQUIRED'):
                activate.backup_program(changed, self.paths.wrapper)

    def test_pre_transition_backup_failure_restores_schedules_and_keeps_old_ready_config(self):
        self.deploy()
        next_release, next_hash, _ = self.next_bundle()
        previous_env, previous_active = self.paths.env.read_bytes(), self.paths.active.read_bytes()
        self.rig.calls.clear()
        self.rig.fail_backup = True
        with self.assertRaises(RuntimeError):
            self.deploy(self.manifest_hash, next_release, next_hash)
        self.assertEqual(self.paths.env.read_bytes(), previous_env)
        self.assertEqual(self.paths.active.read_bytes(), previous_active)
        self.assertEqual(self.paths.current.resolve(), self.release)
        commands = [args for args, _ in self.rig.calls]
        for timer in activate.TIMERS:
            self.assertIn(['systemctl', 'stop', timer], commands)
            self.assertIn(['systemctl', 'start', timer], commands)
        self.assertFalse(any('up' in args for args in commands))

    def test_partial_env_write_failure_marks_recovery_and_stops_writers_without_starting_old_images(self):
        self.deploy()
        next_release, next_hash, _ = self.next_bundle()
        original = activate.atomic_write

        def fail_env(path, value, mode=0o600):
            if path == self.paths.env: raise OSError('synthetic persistent write failure')
            return original(path, value, mode)

        self.rig.calls.clear()
        with patch.object(activate, 'atomic_write', side_effect=fail_env), self.assertRaises(OSError):
            self.deploy(self.manifest_hash, next_release, next_hash)
        self.assertEqual(json.loads(self.paths.active.read_text())['status'], 'failed')
        commands = [args for args, _ in self.rig.calls]
        self.assertTrue(any('stop' in args and 'postgres' in args for args in commands))
        self.assertFalse(any('up' in args for args in commands))
        self.assertTrue(any(report.get('operator_recovery_required') for report in self.reports()))

    def test_persistent_marker_write_failure_cannot_skip_timer_or_writer_stop(self):
        original = activate.write_json

        def fail_marker(path, value):
            if path == self.paths.active: raise OSError('synthetic persistent marker failure')
            return original(path, value)

        with patch.object(activate, 'write_json', side_effect=fail_marker), self.assertRaises(OSError):
            self.deploy()
        commands = [args for args, _ in self.rig.calls]
        self.assertTrue(any('stop' in args and 'postgres' in args for args in commands))
        for timer in activate.TIMERS: self.assertIn(['systemctl', 'stop', timer], commands)
        self.assertTrue(self.reports()[0]['marker_write_incomplete'])
        self.assertFalse(any('up' in args for args in commands))

    def test_startup_after_possible_migration_failure_never_downgrades_or_deletes_volumes(self):
        self.deploy()
        next_release, next_hash, _ = self.next_bundle()
        self.rig.calls.clear()
        self.rig.fail_up = True
        with self.assertRaises(RuntimeError): self.deploy(self.manifest_hash, next_release, next_hash)
        self.assertEqual(self.paths.current.resolve(), next_release)
        self.assertEqual(json.loads(self.paths.active.read_text())['status'], 'failed')
        for args, _ in self.rig.calls:
            if 'up' in args:
                self.assertIn(str(next_release / 'compose.images.yaml'), args)
                self.assertNotIn(str(self.release / 'compose.images.yaml'), args)
            self.assertNotIn('down', args)
            self.assertNotIn('rm', args)
        with self.assertRaisesRegex(ValueError, 'PREVIOUS_ATTEMPT_REQUIRES_SEPARATE_RECOVERY_REVIEW'):
            self.deploy(next_hash, self.release, self.manifest_hash)

    def test_successful_upgrade_preserves_backup_and_refuses_historical_bundle_as_normal_upgrade(self):
        self.deploy()
        next_release, next_hash, _ = self.next_bundle()
        report = self.deploy(self.manifest_hash, next_release, next_hash)
        self.assertTrue(report['ok'])
        self.assertIn('pre_update_backup', report)
        self.assertIn('post_activation_backup', report)
        self.assertFalse(report['pre_update_backup']['off_host_verified'])
        self.assertEqual((self.paths.state / 'previous-release').read_text().strip(), str(self.release))
        with self.assertRaisesRegex(ValueError, 'PREVIOUS_ATTEMPT_REQUIRES_SEPARATE_RECOVERY_REVIEW'):
            self.deploy(next_hash, self.release, self.manifest_hash)

    def test_writable_bundle_is_refused_before_host_mutations(self):
        (self.release / 'infra/scripts/backup.sh').chmod(0o666)
        with self.assertRaisesRegex(ValueError, 'ROOT_OWNED_IMMUTABLE_BUNDLE_FILES_REQUIRED'):
            self.deploy()
        self.assertEqual(self.rig.calls, [])

    def test_wrong_namespace_or_existing_production_resources_refuses_creation(self):
        self.env['ZAVLIQ_SERVER_NAME'] = 'localhost'
        self.write_env()
        with self.assertRaisesRegex(ValueError, 'CANONICAL_FRESH_PRODUCTION_NAMESPACE_REQUIRED'): self.deploy()
        self.assertEqual(self.rig.calls, [])
        self.env['ZAVLIQ_SERVER_NAME'] = 'zavliq.com'
        self.write_env()
        self.rig.existing_resources = True
        with self.assertRaisesRegex(ValueError, 'NO_EXISTING_PRODUCTION_CONTAINERS_OR_VOLUMES_REQUIRED'): self.deploy()
        self.assertFalse(any(args[:2] == ['docker', 'load'] for args, _ in self.rig.calls))

    def test_private_stage_ports_builds_tags_and_external_volumes_are_rejected(self):
        changes = [lambda value: value['services']['gateway'].update(image='zavliq-web:development'),
                   lambda value: value['services']['control'].update(build={'context': '.'}),
                   lambda value: value['services']['synapse'].update(ports=[{'published': '8008', 'target': 8008}]),
                   lambda value: value['volumes']['postgres'].update(name='zavliq-load_postgres'),
                   lambda value: value['networks']['backend'].update(internal=False)]
        for change in changes:
            with self.subTest(change=change):
                value = config(self.manifest, self.paths)
                change(value)
                with self.assertRaises(ValueError): activate.verify_composed(value, self.manifest, self.paths)

    def test_binary_backup_and_retention_compose_calls_keep_overlay_and_bytes(self):
        activate.run_compose(self.paths, self.release, 'exec', '-T', 'postgres', 'pg_dump', '-Fc', 'synapse', passthrough=True)
        args, options = self.rig.calls[-1]
        self.assertTrue(options['passthrough'])
        self.assertIn(str(self.release / 'compose.images.yaml'), args)
        activate.run_compose(self.paths, self.release, '--profile', 'maintenance', 'run', '--rm', '--no-deps', '-T', 'retention', passthrough=True)
        self.assertIn(str(self.release / 'compose.images.yaml'), self.rig.calls[-1][0])
        self.command.stop()
        try:
            with patch.object(activate.subprocess, 'run', return_value=CompletedProcess([], 0)) as run:
                activate.command(['docker', 'compose', 'exec'], passthrough=True)
            self.assertFalse(run.call_args.kwargs['text'])
            self.assertFalse(run.call_args.kwargs['capture_output'])
        finally:
            self.command.start()

    def test_backup_timeout_allows_real_bash_exit_trap_before_returning_failure(self):
        self.backup.stop()
        try:
            cleanup = self.root / 'cleanup-ran'
            script = self.root / 'timeout-fixture.sh'
            script.write_text("trap 'printf cleanup > " + shlex.quote(str(cleanup)) + "' EXIT\nsleep 10\n")
            with self.assertRaisesRegex(ValueError, '^BACKUP_TIMEOUT$'):
                activate.backup_command(script, {}, timeout=0.2, grace=2)
            self.assertEqual(cleanup.read_text(), 'cleanup')
        finally:
            self.backup.start()


if __name__ == '__main__':
    unittest.main()
