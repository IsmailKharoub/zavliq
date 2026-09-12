import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from subprocess import CalledProcessError, CompletedProcess

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import release_bundle as bundle
import deploy_bundle as deploy


class ReleaseTests(unittest.TestCase):
    def test_archive_rejects_traversal_symlinks_and_private_paths(self):
        for name, kind in [('../outside', tarfile.REGTYPE), ('ordinary-link', tarfile.SYMTYPE), ('tests/onboarding/.local/private', tarfile.REGTYPE)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                with tarfile.open(root / 'source.tar.gz', 'w:gz') as archive:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.size = 1 if kind == tarfile.REGTYPE else 0
                    member.linkname = '/outside' if kind == tarfile.SYMTYPE else ''
                    archive.addfile(member, io.BytesIO(b'x') if member.size else None)
                with self.assertRaises(ValueError):
                    bundle.extract_source(root / 'source.tar.gz', root / 'out')

    def test_regular_committed_source_extracts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with tarfile.open(root / 'source.tar.gz', 'w:gz') as archive:
                member = tarfile.TarInfo('infra/example.py')
                member.size = 1
                archive.addfile(member, io.BytesIO(b'x'))
            bundle.extract_source(root / 'source.tar.gz', root / 'out')
            self.assertEqual((root / 'out/infra/example.py').read_bytes(), b'x')

    def test_private_umask_preserves_readable_source_and_executable_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / 'out'
            destination.mkdir(mode=0o700)
            with tarfile.open(root / 'source.tar.gz', 'w:gz') as archive:
                directory = tarfile.TarInfo('services/policy')
                directory.type = tarfile.DIRTYPE
                directory.mode = 0o755
                archive.addfile(directory)
                for name, mode in [('services/policy/__init__.py', 0o644),
                                   ('infra/scripts/start.sh', 0o755)]:
                    member = tarfile.TarInfo(name)
                    member.size = 1
                    member.mode = mode
                    archive.addfile(member, io.BytesIO(b'x'))
            previous_umask = os.umask(0o077)
            try:
                bundle.extract_source(root / 'source.tar.gz', destination)
            finally:
                os.umask(previous_umask)
            for path in destination.rglob('*'):
                if path.is_dir():
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE((destination / 'services/policy/__init__.py').stat().st_mode), 0o644)
            self.assertEqual(stat.S_IMODE((destination / 'infra/scripts/start.sh').stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)

    def test_postgres_pin_fails_before_any_build_or_pull(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ['source.tar.gz', 'native-runtime.tar.gz']:
                (root / name).write_bytes(b'fixture')
            manifest = {'revision': 'a' * 40, 'web_echo_user_id': bundle.ECHO_ID,
                        'source_archive_sha256': bundle.digest(root / 'source.tar.gz'),
                        'native_runtime': {'head_sha': 'a' * 40, 'target': 'x86_64-unknown-linux-gnu', 'archive_sha256': bundle.digest(root / 'native-runtime.tar.gz')},
                        'postgres': {'ref': bundle.POSTGRES_REF, 'id': bundle.POSTGRES_ID}}
            (root / 'source-manifest.json').write_text(json.dumps(manifest))
            with patch.object(bundle, 'checked', side_effect=[CompletedProcess([], 0, 'linux/x86_64\n'), CompletedProcess([], 0, 'sha256:' + 'b' * 64)]) as run:
                with self.assertRaisesRegex(ValueError, 'CACHED_POSTGRES_PIN_REQUIRED'):
                    bundle.build_bundle(root, root / 'out')
                self.assertEqual(len(run.call_args_list), 2)
                self.assertFalse((root / 'out').exists())

    def test_service_user_import_failure_prevents_image_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            revision = 'a' * 40
            with tarfile.open(root / 'source.tar.gz', 'w:gz') as archive:
                member = tarfile.TarInfo('infra/example.py')
                member.size = 1
                archive.addfile(member, io.BytesIO(b'x'))
            binary = b'\x7fELFfixture'
            with tarfile.open(root / 'native-runtime.tar.gz', 'w:gz') as archive:
                member = tarfile.TarInfo('zavliq')
                member.size = len(binary)
                archive.addfile(member, io.BytesIO(binary))
            manifest = {'revision': revision, 'web_echo_user_id': bundle.ECHO_ID,
                        'source_archive_sha256': bundle.digest(root / 'source.tar.gz'),
                        'native_runtime': {'head_sha': revision, 'target': 'x86_64-unknown-linux-gnu',
                                           'archive_sha256': bundle.digest(root / 'native-runtime.tar.gz'),
                                           'binary_sha256': hashlib.sha256(binary).hexdigest()},
                        'postgres': {'ref': bundle.POSTGRES_REF, 'id': bundle.POSTGRES_ID}}
            (root / 'source-manifest.json').write_text(json.dumps(manifest))
            commands = []

            def execute(command, **kwargs):
                commands.append(command)
                if command[:2] == ['docker', 'info']:
                    return CompletedProcess(command, 0, 'linux/amd64\n')
                if command[:3] == ['docker', 'image', 'inspect']:
                    return CompletedProcess(command, 0, bundle.POSTGRES_ID + '\n')
                if command[:2] == ['docker', 'run']:
                    raise CalledProcessError(1, command)
                return CompletedProcess(command, 0)

            with patch.object(bundle, 'checked', side_effect=execute), \
                    patch.object(bundle.subprocess, 'run', return_value=CompletedProcess([], 1)):
                with self.assertRaises(CalledProcessError):
                    bundle.build_bundle(root, root / 'out')
            self.assertEqual(len([command for command in commands if command[:3] == ['docker', 'buildx', 'build']]), 4)
            self.assertEqual(commands[-1], ['docker', 'run', '--rm', '--network', 'none', '--user', '991:991',
                                          '--entrypoint', 'python', 'zavliq-synapse:' + revision, '-c',
                                          'from zavliq_policy import ZavliqPolicy; assert callable(ZavliqPolicy)'])
            self.assertFalse(any(command[:2] == ['docker', 'save'] for command in commands))
            self.assertFalse((root / 'out/images.tar').exists())

    def test_stage_namespace_cannot_be_reinitialized_or_moved(self):
        values = {'COMPOSE_PROJECT_NAME': 'zavliq-load', 'ZAVLIQ_SERVER_NAME': 'localhost', 'ZAVLIQ_PUBLIC_URL': 'http://localhost:28180',
                  'ZAVLIQ_SITE_ADDRESS': ':80', 'ZAVLIQ_STATE_DIR': '/etc/zavliq', 'ZAVLIQ_STAGING_WEB_PORT': '19180'}
        deploy.verify_namespace(values)
        for key, value in [('ZAVLIQ_SERVER_NAME', 'zavliq.com'), ('COMPOSE_PROJECT_NAME', 'new-project'), ('ZAVLIQ_STATE_DIR', '/new-state')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                deploy.verify_namespace({**values, key: value})

    def test_public_service_ports_are_refused(self):
        gateway = {'ports': [{'host_ip': '127.0.0.1', 'published': '19180', 'target': 80}]}
        deploy.verify_loopback_bindings({'services': {'gateway': gateway, 'control': {}}})
        with self.assertRaises(ValueError):
            deploy.verify_loopback_bindings({'services': {'gateway': {'ports': [{'host_ip': '0.0.0.0', 'published': '19180', 'target': 80}]}}})
        with self.assertRaises(ValueError):
            deploy.verify_loopback_bindings({'services': {'gateway': gateway, 'synapse': {'ports': [{'published': '8008', 'target': 8008}]}}})

    def test_manifest_corruption_fails_before_loading_images(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'manifest.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'REVIEWED_MANIFEST_REQUIRED'):
                deploy.verify_manifest(root, 'a' * 40, 'b' * 64)


if __name__ == '__main__':
    unittest.main()
