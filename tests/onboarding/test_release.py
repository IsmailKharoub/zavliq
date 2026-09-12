import argparse
import asyncio
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import harness
import release
import zavliq


class ReleaseVerificationTests(unittest.TestCase):
    def test_nvm_only_node_and_npm_execute_in_clean_install_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            node_tools = root / '.nvm/versions/node/v24.16.0/bin'
            node_tools.mkdir(parents=True)
            (node_tools / 'node').write_text("#!/bin/sh\nprintf 'v24.16.0\\n'\n")
            # npm normally relies on /usr/bin/env node, so both lookups must work.
            (node_tools / 'npm').write_text('#!/usr/bin/env node\n')
            for name in ('node', 'npm'):
                (node_tools / name).chmod(0o700)
            install = root / 'fresh install'
            install.mkdir()
            caller = {'PATH': str(node_tools) + os.pathsep + '/synthetic/untrusted-bin',
                      **{name: 'synthetic-not-a-credential' for name in ('GH_TOKEN', 'GITHUB_TOKEN',
                         'AWS_SECRET_ACCESS_KEY', 'NPM_TOKEN', 'NODE_OPTIONS', 'PYTHONPATH', 'HTTPS_PROXY')}}
            with patch.dict(os.environ, caller, clear=True):
                selected = release.node_tools_directory()
                clean = release.install_environment(install, selected)
                self.assertEqual(selected, node_tools)
                self.assertEqual(clean['PATH'].split(os.pathsep)[0], str(node_tools))
                self.assertNotIn('/synthetic/untrusted-bin', clean['PATH'])
                self.assertTrue(set(caller).difference({'PATH'}).isdisjoint(clean))
                for name in ('node', 'npm'):
                    self.assertEqual(release.command([name, '--version'], time.monotonic(), env=clean), b'v24.16.0\n')
            self.assertEqual(release.node_tools_directory(node_tools), node_tools)
            self.assertEqual(Path(clean['HOME']), install / 'home')
            for key in ('NPM_CONFIG_USERCONFIG', 'NPM_CONFIG_GLOBALCONFIG'):
                self.assertEqual(Path(clean[key]).parent, install)
                self.assertEqual(Path(clean[key]).read_text(), '')
            self.assertEqual(clean['NPM_CONFIG_REGISTRY'], 'https://registry.npmjs.org/')
            self.assertEqual(clean['PIP_CONFIG_FILE'], os.devnull)

    def test_node_prerequisite_rejects_missing_npm_and_ambiguous_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tools = root / 'node tools'
            tools.mkdir()
            (tools / 'node').write_text('#!/bin/sh\nexit 0\n')
            (tools / 'node').chmod(0o700)
            with self.assertRaisesRegex(ValueError, 'NODE_AND_NPM_EXECUTABLES_REQUIRED'):
                release.node_tools_directory(tools)
            (tools / 'npm').write_text('#!/bin/sh\nexit 0\n')
            (tools / 'npm').chmod(0o700)
            alias = root / 'alias'
            alias.symlink_to(tools, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'CANONICAL_NODE_TOOLS_DIRECTORY_REQUIRED'):
                release.node_tools_directory(alias)
            with self.assertRaisesRegex(ValueError, 'CANONICAL_NODE_TOOLS_DIRECTORY_REQUIRED'):
                release.node_tools_directory(Path('relative/tools'))
            with patch.dict(os.environ, {'PATH': str(root / 'missing')}, clear=True):
                with self.assertRaisesRegex(ValueError, 'NODE_TOOLS_DIRECTORY_REQUIRED'):
                    release.node_tools_directory()

    def test_checksums_and_redirects_fail_closed(self):
        self.assertEqual(release.manifest('a' * 64 + '  install.sh\n')['install.sh'], 'a' * 64)
        for content in ['a' * 64 + '  ../install.sh\n', ('a' * 64 + '  install.sh\n') * 2, 'invalid']:
            with self.assertRaises(ValueError): release.manifest(content)
        for url in ['http://github.com/x', 'https://example.com/x', 'https://github.com@evil.com/x', 'https://user:pass@github.com/x']:
            with self.assertRaises(ValueError): release.endpoint(url)
        self.assertEqual(release.endpoint('https://release-assets.githubusercontent.com/fixed?signature=synthetic'), 'https://release-assets.githubusercontent.com/fixed?signature=synthetic')

    def test_install_time_is_inside_the_gate_without_rounding_grace(self):
        passed = harness.trial_timing(100, 399, True, model_started=340)
        self.assertEqual(passed['installation_seconds'], 240)
        self.assertEqual(passed['model_seconds'], 59)
        self.assertTrue(passed['within_five_minutes'])
        self.assertFalse(harness.trial_timing(100, 400.0001, True, model_started=340)['within_five_minutes'])
        self.assertFalse(harness.trial_timing(100, 200, False, model_started=150)['within_five_minutes'])
        with patch('release.time.monotonic', return_value=401):
            with self.assertRaises(TimeoutError): release.remaining(100)

    def test_no_downloads_without_both_readiness_flags(self):
        with patch('release.download', side_effect=AssertionError('No network allowed')):
            with self.assertRaisesRegex(ValueError, 'READINESS'):
                release.attempt(argparse.Namespace(release_ready=False, service_ready=True))

    def test_manifest_failure_records_failed_attempt_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            peer = root / 'peer'
            peer.mkdir()
            (peer / 'identity.json').write_text('{}')
            args = argparse.Namespace(release_ready=True, service_ready=True, peer_directory=peer,
                run_id='release-fixture', index=1, version='v0.1.0', sha256sums_sha256='0' * 64)
            def fake_download(url, path, *args):
                self.assertTrue(url.startswith('https://github.com/IsmailKharoub/zavliq/releases/download/v0.1.0/'))
                path.write_text('a' * 64 + '  install.sh\n')
            with patch.object(release, 'PRIVATE', root / 'private'), patch.object(release, 'EVIDENCE', root / 'evidence'), patch.object(release, 'require_ledger'), patch.object(release, 'download', side_effect=fake_download), patch.object(release, 'command') as command:
                with self.assertRaisesRegex(ValueError, 'MANIFEST_MISMATCH'): release.attempt(args)
                command.assert_not_called()
                result = json.loads((root / 'evidence/release-fixture-01.json').read_text())
                self.assertFalse(result['passed'])
                self.assertFalse(result['within_five_minutes'])
                with self.assertRaisesRegex(ValueError, 'ALREADY_EXISTS'): release.attempt(args)

    def test_worker_uses_installed_entry_skill_binary_and_original_ledger(self):
        config = {'install_root': '/synthetic/install', 'binary': '/synthetic/install/bin/zavliq',
                  'mcp_entry': '/synthetic/install/node/bundle/node_modules/@zavliq/mcp/src/index.mjs',
                  'skill': '/synthetic/install/skill.md', 'peer_directory': '/synthetic/peer',
                  'index': 2, 'run_id': 'release-fixture', 'started': 100}
        schema = MagicMock()
        schema.start = AsyncMock()
        schema.close = AsyncMock()
        schema.tools = AsyncMock(return_value=[])
        with patch.dict('os.environ', {}, clear=False), patch.object(release, 'require_ledger'), patch.object(release, 'installed_provenance', return_value={}), patch.object(harness, 'McpConnection', return_value=schema) as connection, patch.object(harness, 'Budget') as budget, patch.object(harness, 'trial', new_callable=AsyncMock) as trial:
            asyncio.run(release.worker(config))
            self.assertEqual(connection.call_args.args[1], 'https://zavliq.com')
            self.assertEqual(connection.call_args.args[3], Path(config['mcp_entry']))
            budget.assert_called_once_with(release.LEDGER)
            self.assertEqual(trial.call_args.kwargs['skill_path'], Path(config['skill']))
            self.assertEqual(trial.call_args.kwargs['install_started'], 100)
            self.assertEqual(trial.call_args.args[2], 'sdk')
            self.assertEqual(trial.call_args.args[4], config['binary'])

    def test_worker_timeout_preserves_unknown_timing_breakdown_and_manifest_pin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            node_tools = root / '.nvm/versions/node/v24.16.0/bin'
            node_tools.mkdir(parents=True)
            for name in ('node', 'npm'):
                (node_tools / name).write_text('#!/bin/sh\nexit 99\n')
                (node_tools / name).chmod(0o700)
            peer = root / 'peer'
            peer.mkdir()
            (peer / 'identity.json').write_text('{}')
            native = 'zavliq-v0.1.0-x86_64-unknown-linux-gnu.tar.gz'
            bodies = {name: name.encode() for name in ['install.sh', 'skill.md', 'zavliq-node-0.1.0.tar.gz', 'zavliq-0.1.0-py3-none-any.whl', native]}
            manifest = ''.join(f'{release.hashlib.sha256(body).hexdigest()}  {name}\n' for name, body in bodies.items()).encode()
            pin = release.hashlib.sha256(manifest).hexdigest()
            args = argparse.Namespace(release_ready=True, service_ready=True, peer_directory=peer,
                run_id='release-timeout', index=1, version='v0.1.0', sha256sums_sha256=pin,
                node_tools_dir=node_tools)
            def fake_download(url, path, *unused):
                path.write_bytes(manifest if path.name == 'SHA256SUMS' else bodies[path.name])
            def fake_command(command, *unused, **kwargs):
                env = kwargs['env']
                self.assertEqual(env['PATH'].split(os.pathsep)[0], str(node_tools))
                if '_worker' not in command:
                    for key in ('GH_TOKEN', 'AWS_SECRET_ACCESS_KEY', 'NODE_OPTIONS', 'PYTHONPATH', 'HTTPS_PROXY'):
                        self.assertNotIn(key, env)
                    self.assertNotIn('/synthetic/caller-bin', env['PATH'])
                if command[0] == 'node': return b'v24.0.0\n'
                if command[0] == 'sh':
                    self.assertEqual(command[command.index('--sha256sums-sha256') + 1], pin)
                    binary = Path(command[command.index('--install-dir') + 1]) / 'zavliq'
                    binary.parent.mkdir()
                    binary.write_bytes(b'synthetic executable')
                if '_worker' in command:
                    # Bedrock worker authentication is deliberately retained;
                    # the selected Node directory must override caller lookup.
                    self.assertEqual(env['AWS_SECRET_ACCESS_KEY'], 'synthetic-not-a-credential')
                    raise TimeoutError('synthetic worker timeout')
                return b''
            caller = {'PATH': '/synthetic/caller-bin', 'AWS_SECRET_ACCESS_KEY': 'synthetic-not-a-credential',
                      'GH_TOKEN': 'synthetic-not-a-credential', 'NODE_OPTIONS': '--synthetic'}
            with patch.dict(os.environ, caller, clear=True), patch.object(release, 'PRIVATE', root / 'private'), patch.object(release, 'EVIDENCE', root / 'evidence'), patch.object(release, 'require_ledger'), patch.object(release, 'download', side_effect=fake_download), patch.object(release, 'command', side_effect=fake_command), patch.object(release, 'unpack_node'), patch('release.platform.system', return_value='Linux'), patch('release.platform.machine', return_value='x86_64'):
                with self.assertRaises(TimeoutError): release.attempt(args)
            result = json.loads((root / 'evidence/release-timeout-01.json').read_text())
            self.assertFalse(result['within_five_minutes'])
            self.assertEqual(result['failure_stage'], 'worker')
            self.assertIsNone(result['installation_seconds'])
            self.assertIsNone(result['model_seconds'])
            self.assertGreaterEqual(result['elapsed_seconds'], 0)

    def test_original_ledger_is_required_and_exhaustion_does_not_block_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ledger.sqlite3'
            with patch.object(release, 'LEDGER', path):
                with self.assertRaisesRegex(ValueError, 'ORIGINAL_CUMULATIVE'):
                    release.require_ledger()
                budget = harness.Budget(path)
                budget.db.execute('UPDATE calls SET charged=2')
                budget.db.commit()
                with self.assertRaisesRegex(ValueError, 'BUDGET_UNAVAILABLE'):
                    release.require_ledger()
                release.require_ledger(spending=False)
                budget.db.close()

    def test_installed_package_provenance_rejects_checkout_or_changed_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {'module': root / 'venv/lib/site-packages/zavliq/__init__.py', 'binary': root / 'bin/zavliq',
                     'mcp_entry': root / 'node/bundle/node_modules/@zavliq/mcp/src/index.mjs', 'skill': root / 'skill.md'}
            for path in paths.values():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('synthetic fixture')
            for name in ['client', 'mcp']:
                path = root / 'node/bundle/node_modules/@zavliq' / name / 'package.json'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('{"version":"0.1.0"}')
            config = {'install_root': str(root), 'version': 'v0.1.0', 'manifest_sha256': 'a' * 64,
                      **{name: str(paths[name]) for name in ['binary', 'mcp_entry', 'skill']},
                      'binary_sha256': release.digest(paths['binary']), 'asset_sha256': {'skill.md': release.digest(paths['skill'])}}
            with patch.object(zavliq, '__file__', str(paths['module'])), patch('release.inspect.getfile', return_value=str(paths['module'])), patch('release.importlib.metadata.version', return_value='0.1.0'):
                proof = release.installed_provenance(config)
                self.assertEqual(proof['origin'], 'https://zavliq.com')
                self.assertFalse(proof['checkout_fallback'])
                with patch.object(zavliq, '__file__', str(Path(harness.__file__))):
                    with self.assertRaisesRegex(ValueError, 'FALLBACK'): release.installed_provenance(config)
                paths['skill'].write_text('changed')
                with self.assertRaisesRegex(ValueError, 'CHANGED'): release.installed_provenance(config)

    def test_node_extraction_rejects_links_and_parent_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, kind in [('bundle/../../outside', tarfile.REGTYPE), ('bundle/link', tarfile.SYMTYPE)]:
                archive = root / 'fixture.tar'
                with tarfile.open(archive, 'w') as output:
                    item = tarfile.TarInfo(name)
                    item.type = kind
                    item.size = 1 if kind == tarfile.REGTYPE else 0
                    output.addfile(item, io.BytesIO(b'x') if item.size else None)
                with self.assertRaises(ValueError): release.unpack_node(archive, root / 'target', 'bundle')

    def test_ten_attempt_assignments_preserve_both_providers_and_interfaces(self):
        assignments = [release.assignment(index) for index in range(1, 11)]
        self.assertEqual(sum(model == release.MODELS[0] for model, _ in assignments), 5)
        self.assertEqual(sum(transport == 'mcp' for _, transport in assignments), 5)


if __name__ == '__main__':
    unittest.main()
