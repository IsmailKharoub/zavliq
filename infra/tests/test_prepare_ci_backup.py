"""Offline preparation checks: synthetic archives, metadata and network responses only."""
import contextlib
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import runpy
import stat
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'infra/scripts/prepare-ci-backup.py'
spec = importlib.util.spec_from_file_location('prepare_ci_backup', SCRIPT)
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)
REAL_RUN_PATH = runpy.run_path
BINARY = b'synthetic age executable; never executed\n'
READY = {'ok': True, 'encryption_ready': True, 'off_host_decryptability_verified': False}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def private_write(path, data, mode=0o600):
    with path.open('xb') as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(data)
    return path


def environment():
    account = '123456789012'
    recipient = 'age1' + 'c' * 58 + '\n'
    name = dt.datetime.now(dt.timezone.utc).strftime('zavliq-%Y%m%dT%H%M%SZ.tar.age')
    return {'INSTANCE': 'zavliq-production', 'EXPECTED_ACCOUNT': account,
            'INSTANCE_ARN': 'arn:aws:lightsail:us-east-1:' + account + ':Instance/' + 'a' * 8 + '-aaaa-aaaa-aaaa-' + 'a' * 12,
            'DEPLOY_ROLE': 'arn:aws:iam::' + account + ':role/zavliq-production-github',
            'BUCKET': 'zavliq-production-backups-' + account,
            'EXPECTED_BACKUP_MANIFEST': json.dumps({'name': name, 'bytes': 123, 'sha256': 'f' * 64}),
            'JOURNAL_RECIPIENT': recipient, 'JOURNAL_RECIPIENT_SHA256': sha(recipient.encode())}


def archive_bytes(members=None):
    # tuple: archive name, bytes, tar type. No member is ever extracted as a path.
    members = members if members is not None else [
        ('age/age', BINARY, tarfile.REGTYPE),
        ('age/age-keygen', b'not installed', tarfile.REGTYPE),
        ('../must-not-extract', b'not installed either', tarfile.REGTYPE)]
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as bundle:
        for name, data, kind in members:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.mode = 0o777
            member.size = len(data) if kind == tarfile.REGTYPE else 0
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                member.linkname = '../../outside'
            bundle.addfile(member, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
    return output.getvalue()


def archive_pin(data):
    return {'archive_bytes': len(data), 'archive_sha256': sha(data),
            'download_url': 'https://github.com/FiloSottile/age/releases/download/v1.3.2/age-v1.3.2-linux-amd64.tar.gz',
            'executables': {'age/age': {'bytes': len(BINARY), 'sha256': sha(BINARY)}}}


class FakeCurl:
    def __init__(self, data, error=None):
        self.data, self.error = data, error

    def __call__(self, argv, **kwargs):
        kwargs['stdout'].write(self.data)
        if self.error:
            raise self.error
        return subprocess.CompletedProcess(argv, 0)


class PreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o700)
        self.env = environment()
        self.data = archive_bytes()
        self.pin = archive_pin(self.data)
        self.pins = private_write(self.root / 'pins.json', json.dumps({
            'version': 'v1.3.2', 'platforms': {'linux-amd64': self.pin}}).encode())
        # main deliberately tightens umask; don't change the rest of the test process.
        previous = os.umask(0o077)
        os.umask(previous)
        self.addCleanup(os.umask, previous)

    def main(self, *, env=None, directory=None, native=None, output=None):
        directory = directory or self.root / 'prepared'
        native = native or Mock(side_effect=lambda args: print(json.dumps(READY)))
        def load(path, **kwargs):
            module = REAL_RUN_PATH(path, **kwargs)
            if Path(path).name == 'seal-ci-journal.py':
                module['main'] = native
            return module
        output = io.StringIO() if output is None else output
        with patch.object(prepare.platform, 'system', return_value='Linux'), \
                patch.object(prepare.platform, 'machine', return_value='x86_64'), \
                patch.object(prepare, 'PINS', self.pins), \
                patch.object(prepare.runpy, 'run_path', side_effect=load), \
                patch.object(prepare.subprocess, 'run', side_effect=FakeCurl(self.data)) as remote, \
                contextlib.redirect_stdout(output):
            prepare.main(SimpleNamespace(directory=directory), self.env if env is None else env)
        return json.loads(output.getvalue()), native, remote

    def test_inputs_preserve_exact_manifest_and_recipient_bytes(self):
        self.env['EXPECTED_BACKUP_MANIFEST'] = ' \n' + self.env['EXPECTED_BACKUP_MANIFEST'] + '\n\t'
        manifest, recipient, digest = prepare.inputs(self.env)
        self.assertEqual(manifest, self.env['EXPECTED_BACKUP_MANIFEST'].encode())
        self.assertEqual(recipient, self.env['JOURNAL_RECIPIENT'].encode())
        self.assertEqual(digest, sha(recipient))

    def test_invalid_inputs_fail_before_any_download_or_directory(self):
        manifest = json.loads(self.env['EXPECTED_BACKUP_MANIFEST'])
        bad_manifests = ['', 'not JSON private-value', ' ' * 4097, '[]',
                         json.dumps(manifest | {'extra': True}),
                         json.dumps(manifest | {'bytes': True}),
                         json.dumps(manifest | {'sha256': 'F' * 64}),
                         json.dumps(manifest | {'name': 'zavliq-20000101T000000Z.tar.age'}),
                         json.dumps(manifest | {'name': 'zavliq-29990101T000000Z.tar.age'})]
        changes = [{'EXPECTED_BACKUP_MANIFEST': value} for value in bad_manifests] + [
            {'INSTANCE': 'zavliq-staging'}, {'EXPECTED_ACCOUNT': '999999999999'},
            {'INSTANCE_ARN': self.env['INSTANCE_ARN'].replace('us-east-1', 'eu-west-1')},
            {'DEPLOY_ROLE': self.env['DEPLOY_ROLE'] + '-wrong'}, {'BUCKET': self.env['BUCKET'] + '-wrong'},
            {'JOURNAL_RECIPIENT_SHA256': '0' * 64}, {'JOURNAL_RECIPIENT': 'AGE-SECRET-KEY-NOT-REAL'},
            {'JOURNAL_RECIPIENT': 'é'}, {'JOURNAL_RECIPIENT': 'x' * 129}]
        with patch.object(prepare.platform, 'system', return_value='Linux'), \
                patch.object(prepare.platform, 'machine', return_value='x86_64'), \
                patch.object(prepare, 'download_archive') as remote:
            for index, change in enumerate(changes):
                with self.subTest(index=index), self.assertRaises(Exception):
                    prepare.main(SimpleNamespace(directory=self.root / 'absent'), self.env | change)
                self.assertFalse((self.root / 'absent').exists())
        remote.assert_not_called()

    def test_non_linux_or_wrong_architecture_refuses_before_inputs(self):
        for system, machine in [('Darwin', 'arm64'), ('Linux', 'aarch64'), ('Windows', 'x86_64')]:
            with self.subTest(system=system, machine=machine), \
                    patch.object(prepare.platform, 'system', return_value=system), \
                    patch.object(prepare.platform, 'machine', return_value=machine), \
                    patch.object(prepare, 'inputs') as inputs, \
                    self.assertRaisesRegex(prepare.PreparationError, '^BACKUP_CHECK_LINUX_AMD64_REQUIRED$'):
                prepare.main(SimpleNamespace(directory=self.root / 'absent'), {})
            inputs.assert_not_called()

    def test_exclusive_workspace_never_reuses_existing_or_symlink_directory(self):
        existing = self.root / 'existing'; existing.mkdir(mode=0o700)
        sentinel = private_write(existing / 'sentinel', b'preserve')
        link = self.root / 'link'; link.symlink_to(existing, target_is_directory=True)
        for path in [existing, link]:
            with self.subTest(path=path.name), patch.object(prepare, 'download_archive') as remote, \
                    self.assertRaises(FileExistsError):
                self.main(directory=path)
            remote.assert_not_called()
            self.assertEqual(sentinel.read_bytes(), b'preserve')
            self.assertEqual(list(existing.iterdir()), [sentinel])

    def test_download_checks_exact_size_and_hash_with_finite_clean_curl(self):
        for label, data in [('short', self.data[:-1]), ('oversized', self.data + b'x'),
                            ('corrupt', b'x' + self.data[1:])]:
            with self.subTest(label=label), \
                    patch.object(prepare.subprocess, 'run', side_effect=FakeCurl(data)), \
                    self.assertRaisesRegex(Exception, '^BACKUP_CHECK_DEPENDENCY_ARCHIVE_MISMATCH$'):
                prepare.download_archive(self.root / label, self.pin)
        with patch.object(prepare.subprocess, 'run', side_effect=FakeCurl(self.data)) as remote:
            prepare.download_archive(self.root / 'download', self.pin)
        self.assertEqual((self.root / 'download').read_bytes(), self.data)
        self.assertEqual(stat.S_IMODE((self.root / 'download').stat().st_mode), 0o600)
        argv, options = remote.call_args.args[0], remote.call_args.kwargs
        self.assertEqual(argv[:2], ['curl', '--disable'])  # First option suppresses curlrc loading.
        self.assertEqual(argv[-1], self.pin['download_url'])
        for flag, value in [('--proto', '=https'), ('--proto-redir', '=https'), ('--connect-timeout', '10'),
                            ('--max-time', '75'), ('--max-redirs', '5'),
                            ('--max-filesize', str(len(self.data))), ('--output', '-')]:
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertIn('--fail', argv)
        self.assertEqual(options['timeout'], 80)
        self.assertEqual(options['stdin'], subprocess.DEVNULL)
        self.assertEqual(options['stderr'], subprocess.PIPE)
        self.assertTrue(options['check'])
        self.assertEqual(options['env'], {'PATH': os.defpath, 'LANG': 'C'})
        self.assertTrue(options['stdout'].closed)

    def test_download_rejects_bad_transport_timeout_and_existing_destination(self):
        for label, error, code in [
                ('transport', subprocess.CalledProcessError(22, ['curl'], stderr=b'private remote failure'), 'FAILED'),
                ('deadline', subprocess.TimeoutExpired(['curl'], 80, stderr=b'private timeout detail'), 'TIMEOUT')]:
            with self.subTest(label=label), \
                    patch.object(prepare.subprocess, 'run', side_effect=FakeCurl(b'partial', error)), \
                    self.assertRaisesRegex(prepare.PreparationError, '^BACKUP_CHECK_DEPENDENCY_DOWNLOAD_' + code + '$'):
                prepare.download_archive(self.root / label, self.pin)
        destination = private_write(self.root / 'preserved', b'previous')
        with patch.object(prepare.subprocess, 'run') as remote, self.assertRaises(FileExistsError):
            prepare.download_archive(destination, self.pin)
        remote.assert_not_called()
        self.assertEqual(destination.read_bytes(), b'previous')

    def test_archive_private_no_follow_and_local_hash_rechecked(self):
        archive = private_write(self.root / 'archive', self.data)
        link = self.root / 'linked'; link.symlink_to(archive)
        public = private_write(self.root / 'public', self.data, 0o644)
        corrupt = private_write(self.root / 'corrupt', b'x' + self.data[1:])
        short = private_write(self.root / 'short', self.data[:-1])
        oversized = private_write(self.root / 'large', self.data + b'x')
        for path in [link, public, corrupt, short, oversized]:
            with self.subTest(path=path.name), patch.object(prepare.tarfile, 'open') as untar, \
                    self.assertRaisesRegex(Exception, '^BACKUP_CHECK_DEPENDENCY_ARCHIVE_MISMATCH$'):
                prepare.install_age(path, self.root / 'age', self.pin)
            untar.assert_not_called()
            self.assertFalse((self.root / 'age').exists())

    def test_member_size_hash_link_duplicate_missing_and_count_bound(self):
        good = ('age/age', BINARY, tarfile.REGTYPE)
        cases = {'wrong-size': [('age/age', BINARY[:-1], tarfile.REGTYPE)],
                 'wrong-hash': [('age/age', b'x' + BINARY[1:], tarfile.REGTYPE)],
                 'symlink': [('age/age', b'', tarfile.SYMTYPE)],
                 'hardlink': [('age/age', b'', tarfile.LNKTYPE)],
                 'directory': [('age/age', b'', tarfile.DIRTYPE)],
                 'duplicate': [good, good], 'missing': [('age/age-keygen', BINARY, tarfile.REGTYPE)],
                 'too-many': [good] + [(str(n), b'', tarfile.REGTYPE) for n in range(128)]}
        for label, members in cases.items():
            data = archive_bytes(members)
            archive = private_write(self.root / label, data)
            with self.subTest(label=label), self.assertRaisesRegex(prepare.PreparationError, '^BACKUP_CHECK_DEPENDENCY_MEMBER_MISMATCH$'):
                prepare.install_age(archive, self.root / 'age', archive_pin(data))
            self.assertFalse((self.root / 'age').exists())

    def test_only_exact_executable_installed_with_private_mode_and_no_overwrite(self):
        archive = private_write(self.root / 'archive', self.data)
        destination = self.root / 'age'
        prepare.install_age(archive, destination, self.pin)
        self.assertEqual(destination.read_bytes(), BINARY)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
        self.assertEqual({p.name for p in self.root.iterdir()}, {'archive', 'age', 'pins.json'})
        with self.assertRaises(FileExistsError):
            prepare.install_age(archive, destination, self.pin)
        self.assertEqual(destination.read_bytes(), BINARY)

    def test_main_exact_private_inputs_native_arguments_and_public_allowlist(self):
        result, native, remote = self.main()
        self.assertEqual(result, {'ok': True, 'private_inputs_ready': True, 'encryption_ready': True,
                                  'off_host_decryptability_verified': False})
        directory = self.root / 'prepared'
        self.assertEqual((directory / 'expected-manifest.json').read_bytes(), self.env['EXPECTED_BACKUP_MANIFEST'].encode())
        self.assertEqual((directory / 'recipient.txt').read_bytes(), self.env['JOURNAL_RECIPIENT'].encode())
        for path in directory.rglob('*'):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() or path.name == 'age' else 0o600)
        args = native.call_args.args[0]
        self.assertEqual(vars(args), {'action': 'check', 'directory': directory / 'ciphertext',
            'age_binary': directory / 'dependency/age', 'age_sha256': sha(BINARY), 'age_version': 'v1.3.2',
            'recipient_file': directory / 'recipient.txt', 'recipient_sha256': self.env['JOURNAL_RECIPIENT_SHA256']})
        self.assertEqual(list((directory / 'ciphertext').iterdir()), [])
        self.assertEqual(remote.call_count, 1)

    def test_native_failure_or_unconfirmed_result_never_prints_prepared_success(self):
        for index, behavior in enumerate([RuntimeError('private native failure'),
                lambda args: print(json.dumps(READY | {'off_host_decryptability_verified': True})),
                lambda args: print(json.dumps(READY | {'private-data': 'never public'})),
                lambda args: print('not JSON private stdout')]):
            output = io.StringIO()
            native = Mock(side_effect=behavior)
            with self.subTest(index=index), self.assertRaises(Exception):
                self.main(directory=self.root / ('failed-' + str(index)), native=native, output=output)
            self.assertEqual(output.getvalue(), '')
            native.assert_called_once()
            self.assertEqual(list((self.root / ('failed-' + str(index)) / 'ciphertext').iterdir()), [])

    def test_cli_failures_are_fixed_public_codes_without_private_inputs(self):
        for index, changes in enumerate([{'BUCKET': 'private-mismatched-bucket'},
                                        {'EXPECTED_BACKUP_MANIFEST': 'private-invalid-json'}]):
            output, errors = io.StringIO(), io.StringIO()
            with self.subTest(index=index), patch.dict(os.environ, self.env | changes, clear=True), \
                    patch.object(prepare.platform, 'system', return_value='Linux'), \
                    patch.object(prepare.platform, 'machine', return_value='x86_64'), \
                    patch.object(sys, 'argv', [str(SCRIPT), '--directory', str(self.root / 'absent')]), \
                    patch.object(prepare.subprocess, 'run') as remote, \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), \
                    self.assertRaises(SystemExit) as exited:
                REAL_RUN_PATH(str(SCRIPT), run_name='__main__')
            self.assertEqual(exited.exception.code, 1)
            result = json.loads(output.getvalue())
            self.assertEqual(set(result), {'ok', 'code', 'action'})
            self.assertFalse(result['ok'])
            self.assertEqual(result['code'], 'BACKUP_CHECK_DESTINATION_MISMATCH' if index == 0 else 'BACKUP_CHECK_PREPARATION_FAILED')
            self.assertEqual(errors.getvalue(), '')
            for value in [*changes.values(), self.env['INSTANCE_ARN'], self.env['JOURNAL_RECIPIENT'].strip(), str(self.root)]:
                self.assertNotIn(value, output.getvalue())
            remote.assert_not_called()
            self.assertFalse((self.root / 'absent').exists())


class InactiveWorkflowTests(unittest.TestCase):
    """Inspect the checked-in inactive template without adding a YAML dependency."""
    def setUp(self):
        self.path = ROOT / 'infra/github/backups.yml'
        self.source = self.path.read_text()
        self.steps = re.split(r'(?m)^      - ', self.source)[1:]

    def step(self, identifier):
        matching = [step for step in self.steps if re.search(r'(?m)^        id: ' + identifier + r'$', step)]
        self.assertEqual(len(matching), 1, identifier)
        return matching[0]

    def test_manual_only_protected_main_and_no_continue_on_error(self):
        self.assertFalse((ROOT / '.github/workflows/backups.yml').exists())
        self.assertRegex(self.source, r'(?m)^on:\n  workflow_dispatch:\npermissions:')
        self.assertNotRegex(self.source, r'(?m)^  (schedule|push|pull_request|workflow_call|repository_dispatch):')
        self.assertIn("if: github.repository == 'IsmailKharoub/zavliq' && github.ref == 'refs/heads/main'", self.source)
        self.assertIn('    environment: production\n', self.source)
        self.assertIn('  cancel-in-progress: false\n', self.source)
        self.assertNotIn('continue-on-error:', self.source)
        self.assertNotIn('${{ vars.', self.source)
        self.assertIn('persist-credentials: false', self.steps[0])
        for key in ('LIGHTSAIL_INSTANCE', 'LIGHTSAIL_INSTANCE_ARN', 'AWS_DEPLOY_ROLE_ARN', 'AWS_ACCOUNT_ID'):
            self.assertIn('${{ secrets.' + key + ' }}', self.source)

    def test_preflight_before_oidc_access_fetch_cleanup_seal_upload(self):
        prepare_step = self.step('prepare')
        oidc = next(step for step in self.steps if 'aws-actions/configure-aws-credentials@' in step)
        upload = next(step for step in self.steps if 'actions/upload-artifact@' in step)
        ordered = [prepare_step, oidc, self.step('open_access'), self.step('check_backup'),
                   self.step('close_access'), self.step('seal_journal'), upload]
        indices = [self.steps.index(step) for step in ordered]
        self.assertEqual(indices, sorted(indices))
        self.assertIn('python3 infra/scripts/prepare-ci-backup.py', prepare_step)
        self.assertIn('--directory "$RUNNER_TEMP/zavliq-backup-check"', prepare_step)
        for step in ordered[:4]:
            self.assertNotIn('always()', step)
            self.assertNotRegex(step, r'(?m)^        if:')
        for key in ('BACKUP_EXPECTED_MANIFEST', 'BACKUP_JOURNAL_RECIPIENT', 'BACKUP_JOURNAL_RECIPIENT_SHA256'):
            self.assertIn('${{ secrets.' + key + ' }}', prepare_step)
        opened = self.step('open_access')
        self.assertIn('--instance "$INSTANCE" --instance-arn "$INSTANCE_ARN" --account-id "$EXPECTED_ACCOUNT"', opened)

    def test_existing_only_exact_pin_and_public_output_mandatory(self):
        check = self.step('check_backup')
        self.assertEqual(self.source.count('infra/scripts/fetch-backup.py'), 1)
        self.assertIn('--expected-manifest "$RUNNER_TEMP/zavliq-backup-check/expected-manifest.json"', check)
        self.assertRegex(check, r'--existing-only\s+--public-output(?:\s|$)')
        self.assertIn('--ssh-config "$RUNNER_TEMP/zavliq-access/ssh/config"', check)
        self.assertIn('--bucket "$BUCKET"', check)

    def test_cleanup_and_seal_attempted_outcomes_exclude_early_skipped_or_missing(self):
        for identifier in ['close_access', 'seal_journal']:
            step = self.step(identifier)
            match = re.search(r"(?m)^        if: always\(\) && contains\(fromJSON\('([^']+)'\), steps.open_access.outcome\)$", step)
            self.assertIsNotNone(match, identifier)
            outcomes = json.loads(match.group(1))
            self.assertEqual(outcomes, ['success', 'failure', 'cancelled'])
            for outcome in ['', 'skipped', 'success', 'failure', 'cancelled']:
                self.assertEqual(outcome in outcomes, outcome in ['success', 'failure', 'cancelled'])
        cleanup = self.step('close_access')
        self.assertIn('ci-host-access.py close --directory "$RUNNER_TEMP/zavliq-access" || cleanup_status=$?', cleanup)
        self.assertIn('exit "$cleanup_status"', cleanup)
        self.assertNotIn('--instance', cleanup)  # Cleanup uses saved exact identity, not mutable environment.
        self.assertNotIn('state.json', cleanup)
        self.assertNotIn('ciphertext', cleanup)
        seal_step = self.step('seal_journal')
        self.assertIn('--journal "$RUNNER_TEMP/zavliq-access/state.json"', seal_step)
        self.assertNotRegex(seal_step, r'\b(test -[ef]|\[ -[ef]|\|\| true)\b')
        self.assertNotIn('steps.close_access.outcome', seal_step)

    def test_upload_is_only_exact_ciphertext_and_requires_successful_seal(self):
        uploads = [step for step in self.steps if 'actions/upload-artifact@' in step]
        self.assertEqual(len(uploads), 1)
        upload = uploads[0]
        self.assertIn("if: always() && steps.seal_journal.outcome == 'success'", upload)
        self.assertRegex(upload, r'(?m)^          path: \$\{\{ runner.temp \}\}/zavliq-backup-check/ciphertext/journal.age$')
        self.assertIn('name: production-ssh-rule-${{ github.run_id }}-${{ github.run_attempt }}', upload)
        self.assertIn('if-no-files-found: error', upload)
        self.assertIn('retention-days: 7', upload)
        self.assertNotIn('state.json', upload)
        self.assertNotIn('include-hidden-files:', upload)
        self.assertNotIn('*', upload)
        pins = json.loads(prepare.PINS.read_text())
        seal_step = self.step('seal_journal')
        self.assertIn('--age-version ' + pins['version'] + '\n', seal_step)
        self.assertIn('--age-sha256 ' + pins['platforms']['linux-amd64']['executables']['age/age']['sha256'] + '\n', seal_step)
        self.assertIn('--output "$RUNNER_TEMP/zavliq-backup-check/ciphertext/journal.age"', seal_step)

    def test_session_policy_has_no_s3_writes_and_reserves_cleanup_time(self):
        oidc = next(step for step in self.steps if 'aws-actions/configure-aws-credentials@' in step)
        policy = json.loads(' '.join(oidc.split('inline-session-policy: >-\n', 1)[1].splitlines()))
        actions = []
        for statement in policy['Statement']:
            self.assertEqual(statement['Effect'], 'Allow')
            actions.extend(statement['Action'] if isinstance(statement['Action'], list) else [statement['Action']])
        self.assertEqual({action for action in actions if action.startswith('s3:')}, {'s3:GetObject', 's3:ListBucket'})
        self.assertEqual({action for action in actions if action.startswith('lightsail:')}, {
            'lightsail:GetInstance', 'lightsail:GetOperation', 'lightsail:GetInstanceAccessDetails',
            'lightsail:OpenInstancePublicPorts', 'lightsail:CloseInstancePublicPorts'})
        access_statement = next(s for s in policy['Statement'] if 'lightsail:OpenInstancePublicPorts' in s['Action'])
        self.assertEqual(access_statement['Resource'], '${{ secrets.LIGHTSAIL_INSTANCE_ARN }}')
        self.assertEqual(access_statement['Condition'], {'StringEquals': {'aws:RequestedRegion': 'us-east-1'}})
        limits = [int(re.search(r'(?m)^        timeout-minutes: (\d+)$', step).group(1)) for step in self.steps]
        job_limit = int(re.search(r'(?m)^    timeout-minutes: (\d+)$', self.source).group(1))
        self.assertLess(sum(limits), job_limit)
        credential_seconds = int(re.search(r'role-duration-seconds: (\d+)', oidc).group(1))
        self.assertGreaterEqual(credential_seconds, sum(limits[self.steps.index(oidc):]) * 60)


if __name__ == '__main__':
    unittest.main()
