"""Offline smoke-driver boundaries: fake downloads/processes only, no package installs."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import install_smoke as smoke


def readiness():
    return {key: True for key in ('python_local_inbox', 'python_identity_rejected',
        'node_sdk_local_inbox', 'node_identity_rejected', 'mcp_initialized', 'mcp_identity_rejected',
        'runtime_locks_released', 'identity_files_absent')} | {
        'tool_names': smoke.TOOLS, 'tool_count': 17, 'schemas_sha256': smoke.SCHEMAS_HASH}


class InstallSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / 'fresh install'
        self.node_tools = Path(self.temporary.name).resolve() / '.nvm/versions/node/v24.16.0/bin'
        self.node_tools.mkdir(parents=True)
        for name in ('node', 'npm'):
            (self.node_tools / name).write_text('#!/bin/sh\nexit 99\n')
            (self.node_tools / name).chmod(0o700)
        self.native = b'synthetic native payload'
        self.methods = b'synthetic exact methods JSON\n'
        self.bodies = {name: name.encode() for name in ('install.sh', 'skill.md',
            'zavliq-node-0.1.0.tar.gz', 'zavliq-0.1.0-py3-none-any.whl',
            'zavliq-v0.1.0-aarch64-apple-darwin.tar.gz', 'zavliq-v0.1.0-x86_64-unknown-linux-gnu.tar.gz')}
        self.manifest = ''.join(hashlib.sha256(body).hexdigest() + '  ' + name + '\n'
                                for name, body in self.bodies.items()).encode()
        self.pin = hashlib.sha256(self.manifest).hexdigest()
        self.args = argparse.Namespace(execute=True, release_ready=True, version=smoke.VERSION,
            sha256sums_sha256=self.pin, root=self.root, node_tools_dir=self.node_tools)
        self.downloads, self.commands = [], []
        self.bad_manifest = self.bad_asset = self.worker_timeout = False
        self.worker_report = readiness()

    def download(self, url, path, *_):
        self.assertTrue(url.startswith('https://github.com/IsmailKharoub/zavliq/releases/download/v0.1.0/'))
        self.downloads.append(path.name)
        body = self.manifest if path.name == 'SHA256SUMS' else self.bodies[path.name]
        if (self.bad_manifest and path.name == 'SHA256SUMS') or (self.bad_asset and path.name == 'skill.md'):
            body += b'changed'
        path.write_bytes(body)

    def command(self, values, _started, *, env, cwd=None):
        self.commands.append(values)
        for name in ('GH_TOKEN', 'GITHUB_TOKEN', 'AWS_SECRET_ACCESS_KEY', 'NODE_OPTIONS', 'PYTHONPATH', 'HTTPS_PROXY'):
            self.assertNotIn(name, env)
        self.assertEqual(env['HOME'], str(self.root / 'home'))
        self.assertEqual(env['PATH'].split(os.pathsep)[0], str(self.node_tools))
        if values[:2] == ['node', '--version']: return b'v24.0.0\n'
        if values[:2] == ['npm', '--version']: return b'11.0.0\n'
        if values[0] == 'sh':
            self.assertEqual(values[values.index('--sha256sums-sha256') + 1], self.pin)
            binary = Path(values[values.index('--install-dir') + 1]) / 'zavliq'
            binary.parent.mkdir()
            binary.write_bytes(self.native)
        if values[-1] == '--version': return b'zavliq 0.1.0\n'
        if values[-1] == 'methods': return self.methods
        if 'pip' in values:
            self.assertTrue({'--no-index', '--no-deps', '-I', '-B'} <= set(values))
        if values[:2] == ['npm', 'ci']:
            self.assertIn('--ignore-scripts', values)
            self.assertEqual(cwd, self.root / 'node/zavliq-node-0.1.0')
        if '_worker' in values:
            self.assertTrue({'-I', '-B'} <= set(values))
            if self.worker_timeout: raise TimeoutError('synthetic failure with secret-shaped private context')
            return json.dumps({'readiness': self.worker_report, 'installed_provenance': {}}).encode()
        return b''

    def execute(self, system='Darwin', machine='arm64'):
        binary_hash = hashlib.sha256(self.native).hexdigest()
        with (patch.object(smoke, 'MANIFEST', self.pin),
              patch.object(smoke, 'BINARIES', {key: binary_hash for key in smoke.BINARIES}),
              patch.object(smoke, 'METHODS_HASH', hashlib.sha256(self.methods).hexdigest()),
              patch.object(smoke.platform, 'system', return_value=system),
              patch.object(smoke.platform, 'machine', return_value=machine),
              patch.object(smoke.release, 'download', side_effect=self.download),
              patch.object(smoke.release, 'command', side_effect=self.command),
              patch.object(smoke.release, 'unpack_node')):
            return smoke.run(self.args)

    def test_explicit_execution_and_pin_fail_before_any_download_or_directory(self):
        for change in ({'execute': False}, {'release_ready': False}, {'version': 'v0.2.0'},
                       {'sha256sums_sha256': '0' * 64}):
            with self.subTest(change=change):
                original = vars(self.args).copy()
                vars(self.args).update(change)
                with self.assertRaises(ValueError): self.execute()
                vars(self.args).update(original)
                self.assertFalse(self.root.exists())
                self.assertEqual(self.downloads, [])

    def test_both_platforms_install_pinned_release_then_only_local_readiness(self):
        for system, machine, target in [('Darwin', 'arm64', 'aarch64-apple-darwin'),
                                        ('Linux', 'x86_64', 'x86_64-unknown-linux-gnu')]:
            with self.subTest(target=target):
                self.root = Path(self.temporary.name).resolve() / target
                self.args.root = self.root
                result = self.execute(system, machine)
                self.assertTrue(result['ok'])
                self.assertEqual(result['platform_target'], target)
                self.assertFalse(result['full_onboarding_gate_passed'])
                for key in ('registration_calls', 'message_calls', 'model_calls'):
                    self.assertEqual(result[key], 0)
                self.assertEqual(json.loads((self.root / 'result.json').read_text()), result)
                before = len(self.downloads)
                with self.assertRaisesRegex(ValueError, 'FRESH_ABSOLUTE'): self.execute(system, machine)
                self.assertEqual(len(self.downloads), before)

    def test_manifest_mismatch_cannot_run_installer(self):
        self.bad_manifest = True
        result = self.execute()
        self.assertEqual(result['error_code'], 'REVIEWED_MANIFEST_MISMATCH')
        self.assertEqual(self.commands, [])
        self.assertEqual(self.downloads, ['SHA256SUMS'])

    def test_asset_mismatch_cannot_run_installer(self):
        self.bad_asset = True
        result = self.execute()
        self.assertEqual(result['error_code'], 'RELEASE_ASSET_CHECKSUM_MISMATCH')
        self.assertEqual(self.commands, [])

    def test_timeout_preserves_failure_without_exception_details(self):
        self.worker_timeout = True
        result = self.execute()
        self.assertFalse(result['ok'])
        self.assertEqual(result['failure_stage'], 'local_readiness')
        self.assertEqual(result['error_code'], 'TimeoutError')
        self.assertNotIn('secret-shaped', json.dumps(result))
        self.assertTrue((self.root / 'worker.json').is_file())

    def test_missing_local_checks_or_wrong_schema_cannot_pass(self):
        for change in ({'runtime_locks_released': False}, {'python_identity_rejected': False},
                       {'identity_files_absent': False}, {'tool_names': smoke.TOOLS[:-1]},
                       {'schemas_sha256': '0' * 64}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                smoke.validate_report(readiness() | change)
        self.worker_report['mcp_identity_rejected'] = False
        self.assertFalse(self.execute()['ok'])

    def test_installed_wheel_mismatch_precedes_native_client_open(self):
        with (patch.object(smoke.release, 'installed_provenance', return_value={key: 'wrong' for key in smoke.PYTHON_HASHES}),
              patch.dict(sys.modules, {'zavliq': None})):
            with self.assertRaisesRegex(ValueError, 'INSTALLED_WHEEL_BYTES_MISMATCH'):
                asyncio.run(smoke.worker({'version': smoke.VERSION, 'manifest_sha256': smoke.MANIFEST,
                                          'local_only': smoke.LOCAL_ONLY}))

    def test_clean_environment_has_private_configs_and_caches_without_inherited_auth(self):
        self.root.mkdir()
        with patch.dict(os.environ, {'GH_TOKEN': 'synthetic', 'NODE_OPTIONS': 'synthetic', 'HTTPS_PROXY': 'synthetic'}):
            env = smoke.environment(self.root, self.node_tools)
        for key in ('GH_TOKEN', 'NODE_OPTIONS', 'HTTPS_PROXY'): self.assertNotIn(key, env)
        self.assertNotEqual(env['NPM_CONFIG_USERCONFIG'], env['NPM_CONFIG_GLOBALCONFIG'])
        for key in ('NPM_CONFIG_USERCONFIG', 'NPM_CONFIG_GLOBALCONFIG'):
            self.assertEqual(Path(env[key]).read_text(), '')
        self.assertEqual(env['NPM_CONFIG_CACHE'], str(self.root / 'npm-cache'))

    def test_nvm_prerequisites_are_resolved_once_without_inheriting_caller_path(self):
        caller_path = str(self.node_tools) + os.pathsep + '/synthetic/untrusted-bin'
        with patch.dict(os.environ, {'PATH': caller_path, 'NVM_TOKEN': 'synthetic'}):
            selected = smoke.node_tools_directory()
            self.root.mkdir()
            clean = smoke.environment(self.root, selected)
        self.assertEqual(selected, self.node_tools)
        self.assertEqual(clean['PATH'].split(os.pathsep)[0], str(self.node_tools))
        self.assertNotIn('/synthetic/untrusted-bin', clean['PATH'])
        self.assertNotIn('NVM_TOKEN', clean)
        self.assertEqual(smoke.node_tools_directory(self.node_tools), self.node_tools)

    def test_prerequisite_directory_rejects_missing_npm_and_symlink_alias(self):
        alias = self.node_tools.parent / 'alias'
        alias.symlink_to(self.node_tools, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'CANONICAL_NODE_TOOLS_DIRECTORY'):
            smoke.node_tools_directory(alias)
        (self.node_tools / 'npm').unlink()
        with self.assertRaisesRegex(ValueError, 'NODE_AND_NPM_EXECUTABLES_REQUIRED'):
            smoke.node_tools_directory(self.node_tools)


if __name__ == '__main__':
    unittest.main()
