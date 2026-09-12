"""Gateway-only deployment checks with real private records and mocked host tools."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
from subprocess import CompletedProcess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import activate as app
import website as web
import website_state as state
import test_activate as fixtures
import test_prepare_bundle as archives


class WebsiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.paths = app.Paths(self.root)
        self.paths.releases.mkdir(parents=True)
        self.paths.state.mkdir(parents=True, mode=0o700)
        for module in [app, state]:
            owner = patch.object(module, 'ROOT_OWNER', os.getuid()); owner.start(); self.addCleanup(owner.stop)
        self.release, self.base_hash, self.base = fixtures.make_public(self.root / 'input', self.paths, 'a' * 40)
        self.paths.current.symlink_to(self.release)
        app.write_json(self.paths.active, {'status': 'ready', 'bundle_id': self.release.name, 'manifest_sha256': self.base_hash})
        env = {'COMPOSE_PROJECT_NAME': app.PROJECT, 'ZAVLIQ_STATE_DIR': str(self.paths.state), 'ZAVLIQ_SERVER_NAME': 'zavliq.com',
               'ZAVLIQ_PUBLIC_URL': web.bundle.ORIGIN, 'ZAVLIQ_SITE_ADDRESS': 'zavliq.com', 'ZAVLIQ_RELEASE': self.base['revision']}
        app.atomic_write(self.paths.env, ''.join(k + '=' + v + '\n' for k, v in env.items()))
        self.base_bytes = {p: p.read_bytes() for p in [self.paths.active, self.paths.env, self.release / 'production-manifest.json', self.release / 'compose.images.yaml']}
        self.folder, self.manifest, self.manifest_hash = self.package()
        self.backend = {name: {'container_id': name + '-original', 'image': value['id'], 'started_at': 'unchanged', 'mounts': []}
                        for name, value in self.base['images'].items() if name != 'gateway'}
        self.commands = []
        for target, name, kwargs in [(web, 'backend_snapshot', {'return_value': self.backend}),
                                      (app, 'require_running', {}), (app, 'public_readiness', {'return_value': None}),
                                      (web, 'web_readiness', {'return_value': {'as_of': 'current'}}),
                                      (web.bundle, 'tagged_ids', {'return_value': set()}),
                                      (app, 'command', {'side_effect': self.command})]:
            guard = patch.object(target, name, **kwargs); mock = guard.start(); self.addCleanup(guard.stop)
            setattr(self, name + '_mock', mock)

    def package(self):
        files = {'source.tar.gz': b'public source', 'website.Dockerfile': b'FROM reviewed', 'Caddyfile': b'public configuration',
                 'web-bases.json': b'reviewed bases', 'preparer.py': b'reviewed preparer',
                 'base-manifest.json': (self.release / 'production-manifest.json').read_bytes()}
        digest = lambda data: hashlib.sha256(data).hexdigest()
        inputs = {'website_revision': 'b' * 40, 'base_manifest_sha256': self.base_hash,
                  'source_archive_sha256': digest(files['source.tar.gz']), 'dockerfile_sha256': digest(files['website.Dockerfile']),
                  'caddyfile_sha256': digest(files['Caddyfile']), 'web_bases_sha256': digest(files['web-bases.json']),
                  'preparer_sha256': digest(files['preparer.py']), 'previous_gateway': self.base['images']['gateway']}
        name = 'b' * 40 + '-web-' + digest(json.dumps(inputs, sort_keys=True).encode())[:16]
        folder = self.paths.releases.parent / 'websites' / name
        folder.mkdir(parents=True, mode=0o700)
        for filename, data in files.items():
            (folder / filename).write_bytes(data)
        config = archives.encoded({'architecture': 'amd64', 'os': 'linux', 'rootfs': {'type': 'layers', 'diff_ids': []}})
        image = {'ref': 'zavliq-web:' + name, 'id': 'sha256:' + digest(config)}
        config_name = image['id'][7:] + '.json'
        archives.archive(folder / 'website-image.tar.gz', {config_name: config, 'manifest.json': archives.encoded([
            {'Config': config_name, 'RepoTags': [image['ref']], 'Layers': []}])})
        (folder / 'compose.website.yaml').write_text(state.overlay(image['id']))
        manifest = {'schema': state.SCHEMA, 'architecture': 'linux/amd64', 'website_revision': 'b' * 40,
                    'base_manifest_sha256': self.base_hash, 'base_images': self.base['images'], 'origin': web.bundle.ORIGIN,
                    'echo_user_id': web.bundle.ECHO, 'build_inputs': inputs, 'gateway': image,
                    'dist_sha256': {'index.html': 'e' * 64, 'stats.html': 'f' * 64, 'assets/old.js': 'c' * 64},
                    'preserved_assets_sha256': {'assets/old.js': 'c' * 64},
                    'images_archive_sha256': state.digest(folder / 'website-image.tar.gz'),
                    'compose_overlay_sha256': state.digest(folder / 'compose.website.yaml')}
        app.write_json(folder / 'website-manifest.json', manifest)
        return folder, manifest, state.digest(folder / 'website-manifest.json')

    def command(self, args, **kwargs):
        self.commands.append(args)
        output = ''
        if args[:2] == ['docker', 'compose']:
            if 'config' in args:
                candidate = copy.deepcopy(self.base); candidate['images']['gateway'] = self.manifest['gateway']
                output = json.dumps(fixtures.config(candidate, self.paths))
            elif 'ps' in args:
                output = 'gateway-container'
        elif args[:3] == ['docker', 'image', 'inspect']:
            output = json.dumps({'Id': args[-1], 'Os': 'linux', 'Architecture': 'amd64'})
        elif args[:2] == ['docker', 'inspect']:
            output = self.base['images']['gateway']['id']
        elif args[:2] != ['docker', 'load']:
            raise AssertionError(args)
        return CompletedProcess(args, 0, output, '')

    def deploy(self, expected='none', **kwargs):
        return web.transition(self.paths, self.folder.name, self.manifest_hash, self.base_hash, expected, **kwargs)

    def assert_base_untouched(self):
        self.assertEqual(self.paths.current.resolve(), self.release)
        for path, data in self.base_bytes.items(): self.assertEqual(path.read_bytes(), data)
        ups = [c for c in self.commands if 'up' in c]
        self.assertTrue(ups)
        for command in ups:
            self.assertEqual(command[-1], 'gateway')
            self.assertIn('--no-deps', command); self.assertIn('--no-build', command)
        self.assertFalse(any(any(value in c for value in ['stop', 'down', 'pause', 'unpause', 'build', 'pull', 'systemctl', 'echo-bootstrap']) for c in self.commands))

    def test_verified_single_gateway_archive_and_successful_transition_preserve_base(self):
        web.verify_package(self.folder, self.manifest_hash, self.base, self.base_hash)
        result = self.deploy()
        self.assertTrue(result['ok']); self.assertTrue(result['backend_containers_unchanged'])
        self.assert_base_untouched()
        _, _, effective = app.runtime_record(self.paths)
        self.assertEqual(effective['images']['gateway'], self.manifest['gateway'])
        for name in state.SERVICES - {'gateway'}: self.assertEqual(effective['images'][name], self.base['images'][name])
        self.assertEqual(app.compose_args(self.paths, self.release, 'ps')[-3:], ['-f', str(self.folder / 'compose.website.yaml'), 'ps'])

    def test_failed_new_html_or_stats_restores_only_old_gateway(self):
        self.web_readiness_mock.side_effect = ValueError('FRESH_PUBLIC_STATS_REQUIRED')
        with self.assertRaisesRegex(ValueError, 'FRESH_PUBLIC_STATS'):
            self.deploy()
        self.assertFalse((self.paths.state / 'website-active.json').exists())
        self.assert_base_untouched()
        reports = list(self.paths.evidence.glob('website-*.json'))
        self.assertTrue(json.loads(reports[0].read_text())['gateway_rollback_verified'])

    def test_explicit_base_rollback_preserves_application_state(self):
        self.deploy(); self.commands.clear()
        result = web.transition(self.paths, 'base', 'none', self.base_hash, self.manifest_hash, rollback=True)
        self.assertTrue(result['ok']); self.assertFalse((self.paths.state / 'website-active.json').exists())
        self.assert_base_untouched()

    def test_interrupted_activation_can_only_recover_journaled_predecessor(self):
        self.deploy()
        marker = self.paths.state / 'website-active.json'
        current = json.loads(marker.read_text()); current['status'] = 'activating'; app.write_json(marker, current)
        with self.assertRaisesRegex(ValueError, 'JOURNALED_ROLLBACK'):
            self.deploy(self.manifest_hash)
        with self.assertRaisesRegex(ValueError, 'JOURNALED_ROLLBACK'):
            self.deploy(self.manifest_hash, rollback=True)
        self.require_running_mock.side_effect = None
        self.assertTrue(web.transition(self.paths, 'base', 'none', self.base_hash, self.manifest_hash, rollback=True)['ok'])
        self.assertFalse(marker.exists()); self.assert_base_untouched()

    def test_crash_mid_base_rollback_retains_retryable_marker(self):
        self.deploy()
        marker = self.paths.state / 'website-active.json'
        with patch.object(web, 'gateway_up', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            web.transition(self.paths, 'base', 'none', self.base_hash, self.manifest_hash, rollback=True)
        pending = json.loads(marker.read_text())
        self.assertEqual(pending['status'], 'activating'); self.assertIsNone(pending['previous'])
        self.assertTrue(web.transition(self.paths, 'base', 'none', self.base_hash, self.manifest_hash, rollback=True)['ok'])
        self.assertFalse(marker.exists())

    def test_failed_interrupted_recovery_never_restarts_failed_candidate(self):
        self.deploy(); marker = self.paths.state / 'website-active.json'
        current = json.loads(marker.read_text()); current['status'] = 'failed'; app.write_json(marker, current)
        with patch.object(web, 'gateway_up', side_effect=ValueError('GATEWAY_FAILED')) as up, self.assertRaises(ValueError):
            web.transition(self.paths, 'base', 'none', self.base_hash, self.manifest_hash, rollback=True)
        self.assertEqual(up.call_count, 1)
        self.assertEqual(json.loads(marker.read_text())['status'], 'failed')

    def test_crash_during_automatic_rollback_keeps_initial_base_journal(self):
        self.web_readiness_mock.side_effect = ValueError('FRESH_PUBLIC_STATS_REQUIRED')
        original = web.gateway_up
        calls = []
        def crash_rollback(*args, **kwargs):
            calls.append(args)
            if len(calls) == 2: raise KeyboardInterrupt()
            return original(*args, **kwargs)
        with patch.object(web, 'gateway_up', side_effect=crash_rollback), self.assertRaises(KeyboardInterrupt):
            self.deploy()
        marker = self.paths.state / 'website-active.json'
        pending = json.loads(marker.read_text())
        self.assertEqual(pending['status'], 'failed'); self.assertIsNone(pending['previous'])
        self.assertTrue(web.transition(self.paths, 'base', 'none', self.base_hash, self.manifest_hash, rollback=True)['ok'])
        self.assertFalse(marker.exists())

    def test_backend_change_and_non_gateway_overlay_fail_before_mutation(self):
        changed = copy.deepcopy(self.base); changed['images']['control']['id'] = 'sha256:' + '9' * 64
        with self.assertRaisesRegex(ValueError, 'UNCHANGED_PRODUCTION_BASE'):
            state.verify_release(self.folder, self.manifest_hash, changed, self.base_hash)
        (self.folder / 'compose.website.yaml').write_text(state.overlay(self.manifest['gateway']['id']) + '  control: {}\n')
        with self.assertRaisesRegex(ValueError, 'GATEWAY_ONLY_OVERLAY'):
            self.deploy()
        self.assertEqual(self.commands, [])

    def test_pending_website_cannot_be_accepted_as_healthy_or_full_deployment(self):
        self.deploy(); marker = self.paths.state / 'website-active.json'
        current = json.loads(marker.read_text()); current['status'] = 'activating'; app.write_json(marker, current)
        self.assertEqual(app.runtime_record(self.paths)[2]['website_status'], 'activating')
        with self.assertRaisesRegex(ValueError, 'WEBSITE_RECONCILIATION'):
            app.activate(self.paths, self.release.name, self.base_hash, self.base_hash)

    def test_old_asset_hash_collision_is_rejected(self):
        self.manifest['dist_sha256']['assets/old.js'] = '0' * 64
        app.write_json(self.folder / 'website-manifest.json', self.manifest)
        with self.assertRaisesRegex(ValueError, 'ORIGINAL_BROWSER_ASSETS'):
            state.verify_release(self.folder, state.digest(self.folder / 'website-manifest.json'), self.base, self.base_hash)

    def test_metadata_archive_symlink_refused(self):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w') as archive:
            info = tarfile.TarInfo('assets/link'); info.type = tarfile.SYMTYPE; info.linkname = '/private'
            archive.addfile(info)
        with self.assertRaisesRegex(ValueError, 'REGULAR_UNIQUE'):
            web.dist_hashes(data.getvalue())

    def test_buildx_uses_its_validated_context_with_explicit_amd64(self):
        command = web.build_command(Path('/source'), Path('/source/website.Dockerfile'), 'zavliq-web:reviewed', 'b' * 40, self.base_hash)
        self.assertEqual(command[:7], ['docker', '--context', 'default', 'buildx', 'build', '--builder', 'default'])
        self.assertEqual(command[command.index('--platform') + 1], 'linux/amd64')
        self.assertIn('--pull=false', command)


if __name__ == '__main__': unittest.main()
