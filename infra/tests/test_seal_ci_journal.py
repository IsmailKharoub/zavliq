"""Synthetic files/processes only; native tests require explicit reviewed binaries."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import runpy
import signal
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).parents[1] / 'scripts/seal-ci-journal.py'
spec = importlib.util.spec_from_file_location('seal_ci_journal', SCRIPT)
seal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seal)
PLAINTEXT = b' \n{ "unknown_future_field": [3, 2, 1], "close_required" : true }\n\t'
CIPHERTEXT = b'age-encryption.org/v1\n' + b'synthetic-ciphertext-' * 20


def write_private(path, data, mode=0o600):
    with path.open('xb') as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(data)
    return path


class FakeAge:
    def __init__(self, *, ciphertext=CIPHERTEXT, version=b'v1.3.2\n', after_encrypt=None, after_version=None):
        self.ciphertext = ciphertext
        self.version = version
        self.after_encrypt = after_encrypt
        self.after_version = after_version
        self.calls = []

    def __call__(self, argv, *, end, data=None, output=subprocess.PIPE):
        self.calls.append((argv, data, output))
        if argv[-1] == '--version':
            if self.after_version:
                self.after_version()
            return self.version
        if argv[1:3] != ['--encrypt', '--recipient']:
            raise AssertionError('Unexpected age operation')
        output.write(self.ciphertext)
        output.flush()
        if self.after_encrypt:
            self.after_encrypt()
        return None


class SealJournal(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.source = write_private(self.root / 'state.json', PLAINTEXT)
        self.binary = write_private(self.root / 'age', b'synthetic executable; never executed', 0o700)
        self.recipient = write_private(self.root / 'recipient', b'age1' + b'c' * 58 + b'\n')
        self.directory = self.root / 'ciphertexts'
        self.directory.mkdir(mode=0o700)
        self.output = self.directory / 'journal.age'

    def args(self, **changes):
        value = dict(action='seal', journal=self.source, output=self.output, directory=self.directory,
                     age_binary=self.binary, age_sha256=hashlib.sha256(self.binary.read_bytes()).hexdigest(),
                     age_version='v1.3.2', recipient_file=self.recipient,
                     recipient_sha256=hashlib.sha256(self.recipient.read_bytes()).hexdigest())
        value.update(changes)
        return SimpleNamespace(**value)

    def invoke(self, fake=None, args=None):
        fake = fake or FakeAge()
        output = io.StringIO()
        with patch.object(seal, 'run_age', side_effect=fake), contextlib.redirect_stdout(output):
            seal.main(args or self.args())
        return json.loads(output.getvalue()), fake

    def expect_failure(self, code, *, fake=None, args=None, error=None):
        output = io.StringIO()
        fake = fake or FakeAge()
        with patch.object(seal, 'run_age', side_effect=fake), contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(error or seal.SealError, code):
                seal.main(args or self.args())
        self.assertEqual(output.getvalue(), '')
        return fake

    def test_exact_stdin_no_parse_and_public_success_allowlist(self):
        # No JSON decoding should occur, even for future/unknown source fields.
        with patch.object(seal.json, 'loads', side_effect=AssertionError('Source parsed')):
            output = io.StringIO(); fake = FakeAge()
            with patch.object(seal, 'run_age', side_effect=fake), contextlib.redirect_stdout(output):
                seal.main(self.args())
        result = json.loads(output.getvalue())
        self.assertEqual(fake.calls[1][1], PLAINTEXT)
        self.assertEqual(self.source.read_bytes(), PLAINTEXT)
        self.assertEqual(self.output.read_bytes(), CIPHERTEXT)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertEqual(set(result), {'ok', 'ciphertext_sha256', 'ciphertext_bytes', 'off_host_preservation_verified'})
        self.assertEqual(result, {'ok': True, 'ciphertext_sha256': hashlib.sha256(CIPHERTEXT).hexdigest(),
                                  'ciphertext_bytes': len(CIPHERTEXT), 'off_host_preservation_verified': False})
        for private in (str(self.source), hashlib.sha256(PLAINTEXT).hexdigest(), self.recipient.read_text().strip(), 'unknown_future_field'):
            self.assertNotIn(private, output.getvalue())
        self.assertEqual(list(self.directory.iterdir()), [self.output])

    def test_missing_unsafe_and_oversized_source_fail_before_age(self):
        args = self.args()
        for kind in ('missing', 'empty', 'oversized', 'public', 'symlink', 'directory', 'fifo'):
            with self.subTest(kind=kind):
                path = self.root / ('source-' + kind)
                if kind not in ('missing', 'directory', 'fifo', 'symlink'):
                    write_private(path, b'' if kind == 'empty' else b'x' * (4097 if kind == 'oversized' else 10))
                if kind == 'public': path.chmod(0o640)
                elif kind == 'symlink': path.symlink_to(self.source)
                elif kind == 'directory': path.mkdir(mode=0o700)
                elif kind == 'fifo': os.mkfifo(path, 0o600)
                args.journal = path
                fake = self.expect_failure('^CI_JOURNAL_PRIVATE_SOURCE_REQUIRED$', args=args)
                self.assertFalse(fake.calls)
        self.assertEqual(self.source.read_bytes(), PLAINTEXT)
        self.assertFalse(list(self.directory.iterdir()))

    def test_wrong_source_owner_fails_before_age(self):
        args = self.args(); real = os.fstat
        def other_owner(fd):
            value = real(fd)
            return SimpleNamespace(st_mode=value.st_mode, st_uid=os.geteuid()+1, st_size=value.st_size)
        with patch.object(seal.os, 'fstat', side_effect=other_owner):
            fake = self.expect_failure('^CI_JOURNAL_PRIVATE_SOURCE_REQUIRED$', args=args)
        self.assertFalse(fake.calls)

    def test_binary_and_recipient_pin_mismatch_precede_age(self):
        for field, code in [('age_sha256', 'CI_JOURNAL_PINNED_AGE_REQUIRED'), ('recipient_sha256', 'CI_JOURNAL_RECIPIENT_PIN_MISMATCH')]:
            with self.subTest(field=field):
                fake = self.expect_failure('^' + code + '$', args=self.args(**{field: '0'*64}))
                self.assertFalse(fake.calls)

    def test_binary_executable_mode_and_recipient_type_are_required(self):
        args = self.args(); self.binary.chmod(0o600)
        self.assertFalse(self.expect_failure('^CI_JOURNAL_PINNED_AGE_REQUIRED$', args=args).calls)
        self.binary.chmod(0o700)
        for value in (b'age-plugin-example\n', b'ssh-ed25519 not-a-recipient\n', b'AGE-SECRET-KEY-NOT-REAL\n', b'\xff'):
            with self.subTest(value=value):
                self.recipient.write_bytes(value)
                self.assertFalse(self.expect_failure('^CI_JOURNAL_RECIPIENT_REQUIRED$').calls)

    def test_stale_version_stops_before_encryption(self):
        fake = self.expect_failure('^CI_JOURNAL_AGE_VERSION_MISMATCH$', fake=FakeAge(version=b'v0.9.0\n'))
        self.assertEqual(len(fake.calls), 1)
        self.assertFalse(list(self.directory.iterdir()))

    def test_zero_partial_wrong_header_and_oversized_ciphertext_refused(self):
        for ciphertext in (b'', b'age-encryption.org/v1\n', b'x'*256,
                           b'age-encryption.org/v1\n' + b'x'*8192):
            with self.subTest(length=len(ciphertext)):
                self.expect_failure('^CI_JOURNAL_CIPHERTEXT_UNCONFIRMED$', fake=FakeAge(ciphertext=ciphertext))
                self.assertFalse(list(self.directory.iterdir()))
                self.assertEqual(self.source.read_bytes(), PLAINTEXT)

    def test_changed_source_recipient_or_binary_is_not_published(self):
        for field, code in [('journal', 'SOURCE'), ('recipient_file', 'RECIPIENT'), ('age_binary', 'AGE')]:
            with self.subTest(field=field):
                args = self.args(); path = getattr(args, field); original = path.read_bytes()
                fake = FakeAge(after_encrypt=lambda: path.write_bytes(original+b'changed'))
                self.expect_failure('^CI_JOURNAL_' + code + '_CHANGED$', fake=fake, args=args)
                self.assertFalse(self.output.exists())
                self.assertEqual(path.read_bytes(), original+b'changed')
                path.write_bytes(original)

    def test_same_bytes_at_replaced_source_inode_still_refused(self):
        def replace():
            fresh = write_private(self.root/'new-state', PLAINTEXT)
            fresh.replace(self.source)
        self.expect_failure('^CI_JOURNAL_SOURCE_CHANGED$', fake=FakeAge(after_encrypt=replace))
        self.assertFalse(self.output.exists())
        self.assertEqual(self.source.read_bytes(), PLAINTEXT)

    def test_existing_final_file_or_symlink_precedes_age(self):
        for link in (False, True):
            with self.subTest(link=link):
                if link: self.output.symlink_to(self.source)
                else: write_private(self.output, b'concurrent winner')
                fake = self.expect_failure('^CI_JOURNAL_OUTPUT_EXISTS$')
                self.assertFalse(fake.calls)
                self.assertEqual(self.output.read_bytes(), PLAINTEXT if link else b'concurrent winner')
                self.output.unlink()

    def test_final_creation_race_preserves_winner(self):
        real = os.link
        def raced(src, dst, **kwargs):
            write_private(self.output, b'winner')
            return real(src, dst, **kwargs)
        with patch.object(seal.os, 'link', side_effect=raced):
            self.expect_failure('', error=FileExistsError)
        self.assertEqual(self.output.read_bytes(), b'winner')
        self.assertEqual(self.source.read_bytes(), PLAINTEXT)
        self.assertEqual(list(self.directory.iterdir()), [self.output])

    def test_temp_creation_collision_preserves_unowned_file(self):
        name = '.journal-'+'f'*32+'.partial'; existing = write_private(self.directory/name, b'winner')
        with patch.object(seal.secrets, 'token_hex', return_value='f'*32):
            self.expect_failure('', error=FileExistsError)
        self.assertEqual(existing.read_bytes(), b'winner')
        self.assertFalse(self.output.exists())

    def test_temp_path_substitution_cannot_publish_or_delete_winner(self):
        winner = b'replacement must survive'
        def replace():
            temporary = next(self.directory.glob('.journal-*.partial'))
            temporary.unlink()
            write_private(temporary, winner)
        self.expect_failure('^CI_JOURNAL_TEMPORARY_CHANGED$', fake=FakeAge(after_encrypt=replace))
        self.assertFalse(self.output.exists())
        self.assertEqual(next(self.directory.iterdir()).read_bytes(), winner)
        self.assertEqual(self.source.read_bytes(), PLAINTEXT)

    def test_post_link_final_replacement_is_unconfirmed_and_preserved(self):
        real = os.link
        def replace(src, dst, **kwargs):
            real(src, dst, **kwargs)
            self.output.unlink(); write_private(self.output, b'final race winner')
        with patch.object(seal.os, 'link', side_effect=replace):
            self.expect_failure('^CI_JOURNAL_CIPHERTEXT_CHANGED$')
        self.assertEqual(self.output.read_bytes(), b'final race winner')
        self.assertEqual(self.source.read_bytes(), PLAINTEXT)

    def test_post_link_same_inode_ciphertext_change_is_unconfirmed(self):
        real = os.link
        def mutate(src, dst, **kwargs):
            real(src, dst, **kwargs)
            self.output.write_bytes(CIPHERTEXT[:-1]+b'!')
        with patch.object(seal.os, 'link', side_effect=mutate):
            self.expect_failure('^CI_JOURNAL_CIPHERTEXT_CHANGED$')
        self.assertEqual(self.output.read_bytes(), CIPHERTEXT[:-1]+b'!')
        self.assertEqual(self.source.read_bytes(), PLAINTEXT)

    def test_output_directory_replacement_before_temp_or_publication_refused(self):
        for when in ('version', 'encrypt'):
            with self.subTest(when=when):
                moved = self.root/('moved-'+when)
                def move():
                    self.directory.rename(moved); self.directory.mkdir(mode=0o700)
                fake = FakeAge(**{'after_'+when: move})
                self.expect_failure('^CI_JOURNAL_OUTPUT_DIRECTORY_CHANGED$', fake=fake)
                self.assertFalse(list(self.directory.iterdir()))
                self.assertFalse(list(moved.iterdir()))
                self.assertEqual(self.source.read_bytes(), PLAINTEXT)

    def test_file_fsync_failure_never_publishes(self):
        real = os.fsync
        def broken(fd):
            if stat.S_ISREG(os.fstat(fd).st_mode): raise OSError('synthetic private fsync error')
            return real(fd)
        with patch.object(seal.os, 'fsync', side_effect=broken): self.expect_failure('', error=OSError)
        self.assertFalse(list(self.directory.iterdir()))
        self.assertEqual(self.source.read_bytes(), PLAINTEXT)

    def test_directory_fsync_failure_preserves_final_without_success(self):
        real = os.fsync
        def broken(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode): raise OSError('synthetic private fsync error')
            return real(fd)
        with patch.object(seal.os, 'fsync', side_effect=broken): self.expect_failure('', error=OSError)
        self.assertEqual(self.output.read_bytes(), CIPHERTEXT)
        self.assertEqual(self.source.read_bytes(), PLAINTEXT)

    def test_check_uses_canary_and_creates_no_final_or_required_journal(self):
        args = self.args(action='check'); self.source.unlink()
        result, fake = self.invoke(args=args)
        self.assertEqual(fake.calls[1][1], seal.CANARY)
        self.assertEqual(result, {'ok': True, 'encryption_ready': True, 'off_host_decryptability_verified': False})
        self.assertFalse(list(self.directory.iterdir()))
        self.assertFalse(self.source.exists())

    def test_check_cannot_hide_failed_encryption(self):
        self.expect_failure('^CI_JOURNAL_CIPHERTEXT_UNCONFIRMED$', fake=FakeAge(ciphertext=b''), args=self.args(action='check'))
        self.assertFalse(list(self.directory.iterdir()))

    def test_cli_errors_have_fixed_public_schema_and_redact_subprocess_output(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                args = self.args(); source = self.root/'absent' if missing else self.source
                argv = [str(SCRIPT), 'seal', '--age-binary', str(self.binary), '--age-sha256', args.age_sha256,
                        '--age-version', args.age_version, '--recipient-file', str(self.recipient),
                        '--recipient-sha256', args.recipient_sha256, '--journal', str(source), '--output', str(self.output)]
                output, errors = io.StringIO(), io.StringIO()
                failure = subprocess.CalledProcessError(9, ['synthetic-private-command'], output=b'private stdout marker', stderr=b'private stderr marker')
                with patch.object(sys, 'argv', argv), patch.object(subprocess, 'Popen', side_effect=failure) as process, contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    with self.assertRaises(SystemExit) as stopped: runpy.run_path(str(SCRIPT), run_name='__main__')
                self.assertEqual(stopped.exception.code, 1)
                result = json.loads(output.getvalue())
                self.assertEqual(set(result), {'ok', 'code', 'preservation', 'action'})
                self.assertEqual(result['code'], 'CI_JOURNAL_PRIVATE_SOURCE_REQUIRED' if missing else 'CI_JOURNAL_PRESERVATION_UNCONFIRMED')
                self.assertFalse(result['ok']); self.assertEqual(result['preservation'], 'unconfirmed')
                self.assertEqual(errors.getvalue(), '')
                for marker in ('private stdout marker','private stderr marker',str(self.root),args.recipient_sha256,args.age_sha256):
                    self.assertNotIn(marker, output.getvalue()+errors.getvalue())
                if missing: process.assert_not_called()
        self.assertEqual(self.source.read_bytes(), PLAINTEXT)


class AgeProcess(unittest.TestCase):
    def test_nonzero_exit_or_stderr_fails_without_echo(self):
        for code, stderr in ((1,b''),(0,b'private error')):
            process = Mock(returncode=code); process.communicate.return_value=(b'private stdout',stderr)
            output = io.StringIO()
            with patch.object(seal.subprocess, 'Popen', return_value=process), contextlib.redirect_stdout(output), self.assertRaisesRegex(seal.SealError,'^CI_JOURNAL_ENCRYPTION_FAILED$'):
                seal.run_age(['synthetic-age'],end=time.monotonic()+5,data=b'fixture')
            self.assertEqual(output.getvalue(),'')

    def test_timeout_and_keyboard_interrupt_kill_group_then_reap(self):
        for interruption in (subprocess.TimeoutExpired('age',1),KeyboardInterrupt()):
            process=Mock(pid=123456,returncode=-9); process.communicate.side_effect=[interruption,(b'',b'')]
            with patch.dict(os.environ,{'AWS_SECRET_ACCESS_KEY':'synthetic secret','AGE_PLUGIN_PATH':'untrusted'}), patch.object(seal.subprocess,'Popen',return_value=process) as popen, patch.object(seal.os,'killpg') as kill, self.assertRaisesRegex(seal.SealError,'^CI_JOURNAL_ENCRYPTION_INTERRUPTED$'):
                seal.run_age(['synthetic-age'],end=time.monotonic()+5,data=b'fixture')
            kill.assert_called_once_with(123456,signal.SIGKILL)
            self.assertEqual(process.communicate.call_count,2)
            self.assertEqual(process.communicate.call_args.kwargs,{'timeout':5})
            self.assertTrue(popen.call_args.kwargs['start_new_session'])
            self.assertEqual(popen.call_args.kwargs['env'],{'PATH':os.defpath,'LANG':'C'})
            self.assertEqual(popen.call_args.kwargs['stderr'],subprocess.PIPE)
            self.assertEqual(popen.call_args.kwargs['stdin'],subprocess.PIPE)

    def test_child_reap_timeout_stays_unconfirmed(self):
        process=Mock(pid=123456); process.communicate.side_effect=[subprocess.TimeoutExpired('age',1),subprocess.TimeoutExpired('age',5)]
        with patch.object(seal.subprocess,'Popen',return_value=process),patch.object(seal.os,'killpg') as kill,self.assertRaisesRegex(seal.SealError,'^CI_JOURNAL_CHILD_CLEANUP_UNCONFIRMED$'):
            seal.run_age(['synthetic-age'],end=time.monotonic()+5)
        kill.assert_called_once_with(123456,signal.SIGKILL)

    def test_expired_budget_never_launches_child(self):
        with patch.object(seal.subprocess,'Popen') as popen,self.assertRaisesRegex(seal.SealError,'^CI_JOURNAL_ENCRYPTION_TIMEOUT$'):
            seal.run_age(['synthetic-age'],end=time.monotonic()-1)
        popen.assert_not_called()


class NativeRoundtrip(unittest.TestCase):
    """Explicit opt-in; no discovery of system binaries or existing private keys."""
    @classmethod
    def setUpClass(cls):
        age, keygen = os.environ.get('ZAVLIQ_TEST_AGE'), os.environ.get('ZAVLIQ_TEST_AGE_KEYGEN')
        if not age and not keygen:
            raise unittest.SkipTest('Set both reviewed disposable-test binary paths for native age checks')
        if not age or not keygen:
            raise AssertionError('Both explicit test binary paths are required')
        manifest=json.loads((SCRIPT.parents[1]/'dependencies/age-v1.3.2.json').read_text())
        target={'Darwin':'darwin','Linux':'linux'}.get(platform.system(),'')+'-'+{'arm64':'arm64','aarch64':'arm64','x86_64':'amd64','AMD64':'amd64'}.get(platform.machine(),'')
        approved=manifest['platforms'][target]['executables']
        cls.version=manifest['version']; cls.age=Path(age); cls.keygen=Path(keygen)
        for name,path in [('age',cls.age),('age-keygen',cls.keygen)]:
            if not path.is_absolute() or path.resolve()!=path or not stat.S_ISREG(path.lstat().st_mode):
                raise AssertionError('Explicit regular non-symlink absolute binary required')
            info=approved['age/'+name]
            if path.stat().st_size!=info['bytes'] or hashlib.sha256(path.read_bytes()).hexdigest()!=info['sha256']:
                raise AssertionError('Test binary differs from reviewed public dependency pin')

    def test_disposable_identity_exact_roundtrip_and_public_canary(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve();root.chmod(0o700)
            identity=root/'disposable-key.txt'
            clean={'PATH':os.defpath,'LANG':'C'}
            # Both generated secret output and public output are captured, never logged.
            subprocess.run([str(self.keygen),'-o',str(identity)],check=True,capture_output=True,timeout=10,env=clean)
            identity.chmod(0o600)
            public=subprocess.run([str(self.keygen),'-y',str(identity)],check=True,capture_output=True,timeout=10,env=clean).stdout
            recipient=write_private(root/'recipient.txt',public)
            journal=write_private(root/'state.json',PLAINTEXT)
            directory=root/'output';directory.mkdir(mode=0o700)
            args=SimpleNamespace(action='check',directory=directory,age_binary=self.age,
                                 age_sha256=hashlib.sha256(self.age.read_bytes()).hexdigest(),age_version=self.version,
                                 recipient_file=recipient,recipient_sha256=hashlib.sha256(public).hexdigest(),
                                 journal=journal,output=directory/'journal.age')
            stdout=io.StringIO()
            with contextlib.redirect_stdout(stdout):seal.main(args)
            self.assertEqual(json.loads(stdout.getvalue()),{'ok':True,'encryption_ready':True,'off_host_decryptability_verified':False})
            self.assertFalse(list(directory.iterdir()))
            args.action='seal';stdout=io.StringIO()
            with contextlib.redirect_stdout(stdout):seal.main(args)
            result=json.loads(stdout.getvalue())
            decrypted=subprocess.run([str(self.age),'--decrypt','--identity',str(identity),str(args.output)],check=True,capture_output=True,timeout=10,env=clean)
            self.assertEqual(decrypted.stdout,PLAINTEXT);self.assertEqual(decrypted.stderr,b'')
            self.assertEqual(journal.read_bytes(),PLAINTEXT)
            self.assertTrue(result['ok']);self.assertFalse(result['off_host_preservation_verified'])
            self.assertEqual(result['ciphertext_sha256'],hashlib.sha256(args.output.read_bytes()).hexdigest())
            self.assertEqual(result['ciphertext_bytes'],args.output.stat().st_size)
            self.assertEqual(list(directory.iterdir()),[args.output])


if __name__ == '__main__':
    unittest.main()
