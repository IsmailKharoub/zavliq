import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from subprocess import CompletedProcess

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
