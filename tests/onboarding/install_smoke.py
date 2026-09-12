#!/usr/bin/env python3
"""Anonymous pinned-release installation and local readiness only; no accounts or models."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sys
import time

spec = importlib.util.spec_from_file_location('release_install_helpers', Path(__file__).with_name('release.py'))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

VERSION = 'v0.1.0'
REVISION = '2f76469b507c8745d4be5ef311f32774021143aa'
MANIFEST = 'f81baae19d3bd9b5c32484afe2f225d509f769e2b77ffa73f995bf67d4887cf8'
TARGETS = {('Darwin', 'arm64'): 'aarch64-apple-darwin', ('Linux', 'x86_64'): 'x86_64-unknown-linux-gnu'}
BINARIES = {'aarch64-apple-darwin': 'e999ba1d7721b9e026b9a914e57ce766c252501d93e636cca2cba8524b73f5ac',
            'x86_64-unknown-linux-gnu': '1b9ab48464831519d4a90a003fca18594d2588b79f05b66ea7d9c46e5c49b329'}
PYTHON_HASHES = {'python_module_sha256': '5124055596f961ad12f362f002d3296bf54fe6c1d597f8cd089331a6adadc689',
                 'python_client_sha256': '4cce7bf19d0e9b93a3af68435040c33056a61695db7b616fa12312e5a45f7751'}
METHODS_HASH = '19dd3d449170b30a403e0594e4859f28545eb9a90d48ccde45f420891569077d'
SCHEMAS_HASH = 'f8bed13c083d47d0fca659913280b512298357acc005d8cb20f9e16077822263'
TOOLS = sorted('block conversations create crypto directory flush identity inbox init membership pairing publisher receipt send thread transfer wait'.split())
TOOLS = ['zavliq_' + name for name in TOOLS]
LOCAL_ONLY = 'http://127.0.0.1:9'

# Adapted from the previously verified private-artifact readiness probe. All
# package imports resolve from this file's freshly installed portable bundle.
NODE_PROBE = r"""
import assert from 'node:assert/strict';
import {readFile, realpath, access} from 'node:fs/promises';
import {join, relative, isAbsolute} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import {Zavliq} from '@zavliq/client';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
const config = JSON.parse(await readFile(process.argv[2], 'utf8'));
const root = config.install_root, bundle = config.bundle;
const modulePath = await realpath(fileURLToPath(import.meta.resolve('@zavliq/client')));
const relation = relative(await realpath(join(bundle, 'node_modules')), modulePath);
assert(relation && !relation.startsWith('..') && !isAbsolute(relation));
const sdkDir = join(root, 'identity-node'), mcpDir = join(root, 'identity-mcp');
const sdk = new Zavliq({binary:config.binary, dataDir:sdkDir, controlUrl:config.local_only, timeoutMs:5000});
try {
  const inbox = await sdk.inbox(0, 10, {sync:false});
  assert.deepEqual(inbox.items, []); assert.equal(inbox.next_cursor, 0);
  await assert.rejects(sdk.identity(), error => error.code === 'NOT_INITIALIZED');
} finally { sdk.close(); }
const transport = new StdioClientTransport({command:process.execPath, args:[config.mcp_entry],
  env:{...process.env, ZAVLIQ_BINARY:config.binary, ZAVLIQ_DATA_DIR:mcpDir, ZAVLIQ_CONTROL_URL:config.local_only}});
const mcp = new Client({name:'zavliq-anonymous-install-readiness', version:'0.1.0'});
let proof;
try {
  await mcp.connect(transport);
  const listing = await mcp.listTools();
  const schema = JSON.stringify(listing.tools);
  assert.deepEqual(listing.tools.map(tool => tool.name).sort(), config.tools);
  assert(!/access_token|registration_secret|store_passphrase|pairing_secret/.test(schema));
  const schemaHash = createHash('sha256').update(schema).digest('hex');
  assert.equal(schemaHash, config.schemas_sha256);
  const result = await mcp.callTool({name:'zavliq_identity', arguments:{}});
  assert.equal(result.isError, true);
  assert.equal(JSON.parse(result.content[0].text).error.code, 'NOT_INITIALIZED');
  proof = {node_sdk_local_inbox:true, node_identity_rejected:true, mcp_initialized:true,
    mcp_identity_rejected:true, tool_names:config.tools, tool_count:listing.tools.length,
    schemas_sha256:schemaHash, node_module_sha256:createHash('sha256').update(await readFile(modulePath)).digest('hex')};
} finally { await mcp.close(); }
for (const directory of [sdkDir, mcpDir]) {
  await assert.rejects(access(join(directory, 'identity.json')), error => error.code === 'ENOENT');
}
console.log(JSON.stringify(proof));
"""


def require(condition, code):
    if not condition:
        raise ValueError(code)


def write_json(path, value):
    with path.open('x') as output:
        json.dump(value, output, indent=2)
        output.write('\n')


def node_tools_directory(explicit=None):
    if explicit is None:
        node = shutil.which('node')
        require(node is not None, 'NODE_TOOLS_DIRECTORY_REQUIRED')
        directory = Path(node).parent.resolve(strict=True)
    else:
        directory = Path(explicit)
    require(directory.is_absolute() and directory.resolve(strict=True) == directory and directory.is_dir(),
            'CANONICAL_NODE_TOOLS_DIRECTORY_REQUIRED')
    require(all((directory / name).is_file() and os.access(directory / name, os.X_OK)
                for name in ('node', 'npm')), 'NODE_AND_NPM_EXECUTABLES_REQUIRED')
    return directory


def environment(root, node_tools):
    """Fresh user/global configs and caches; no caller tokens or proxy settings."""
    home = root / 'home'
    home.mkdir(mode=0o700)
    npmrc = root / 'empty-user.npmrc'
    global_npmrc = root / 'empty-global.npmrc'
    npmrc.write_text('')
    global_npmrc.write_text('')
    return {'HOME': str(home), 'PATH': str(node_tools) + os.pathsep + os.defpath + os.pathsep + '/usr/local/bin:/opt/homebrew/bin',
        'TMPDIR': str(root), 'LANG': 'C.UTF-8', 'NPM_CONFIG_USERCONFIG': str(npmrc),
        'NPM_CONFIG_GLOBALCONFIG': str(global_npmrc), 'NPM_CONFIG_CACHE': str(root / 'npm-cache'),
        'NPM_CONFIG_REGISTRY': 'https://registry.npmjs.org/', 'PIP_CONFIG_FILE': os.devnull,
        'PIP_DISABLE_PIP_VERSION_CHECK': '1', 'PYTHONDONTWRITEBYTECODE': '1'}


def validate_report(report):
    require(all(report.get(key) is True for key in ('python_local_inbox', 'python_identity_rejected',
        'node_sdk_local_inbox', 'node_identity_rejected', 'mcp_initialized', 'mcp_identity_rejected',
        'runtime_locks_released', 'identity_files_absent')), 'INCOMPLETE_LOCAL_READINESS')
    require(report.get('tool_names') == TOOLS and report.get('tool_count') == 17 and
            report.get('schemas_sha256') == SCHEMAS_HASH, 'MCP_SCHEMA_MISMATCH')


async def worker(config):
    require(sys.flags.isolated == 1, 'ISOLATED_PYTHON_REQUIRED')
    require(config['version'] == VERSION and config['manifest_sha256'] == MANIFEST and
            config['local_only'] == LOCAL_ONLY, 'PINNED_WORKER_CONFIG_REQUIRED')
    provenance = release.installed_provenance(config)
    require(all(provenance[key] == expected for key, expected in PYTHON_HASHES.items()), 'INSTALLED_WHEEL_BYTES_MISMATCH')
    from zavliq import Zavliq, ZavliqError
    root = Path(config['install_root'])
    async with Zavliq(binary=config['binary'], data_dir=str(root / 'identity-python'),
                      control_url=LOCAL_ONLY, timeout=5) as client:
        inbox = await client.inbox(0, 10, sync=False)
        require(inbox['items'] == [] and inbox['next_cursor'] == 0, 'FRESH_LOCAL_INBOX_REQUIRED')
        try:
            await client.identity()
        except ZavliqError as error:
            require(error.code == 'NOT_INITIALIZED', 'EXPECTED_NOT_INITIALIZED')
        else:
            raise ValueError('UNEXPECTED_REGISTERED_IDENTITY')
    probe = Path(config['bundle']) / 'anonymous-install-smoke.mjs'
    with probe.open('x') as output:
        output.write(NODE_PROBE)
    output = release.command(['node', str(probe), config['config_path']], config['started'],
                             env=os.environ.copy(), cwd=config['bundle'])
    report = json.loads(output)
    for name in ('identity-python', 'identity-node', 'identity-mcp'):
        directory = root / name
        require(not (directory / 'identity.json').exists() and not (directory / 'identity.json').is_symlink(),
                'UNEXPECTED_REGISTERED_IDENTITY')
        with (directory / 'runtime.lock').open('rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock, fcntl.LOCK_UN)
    report.update(python_local_inbox=True, python_identity_rejected=True,
                  runtime_locks_released=True, identity_files_absent=True)
    validate_report(report)
    return {'readiness': report, 'installed_provenance': provenance}


def run(args):
    require(args.execute and args.release_ready, 'EXPLICIT_PUBLISHED_RELEASE_EXECUTION_REQUIRED')
    require(sys.flags.isolated == 1, 'ISOLATED_PYTHON_REQUIRED')
    require(args.version == VERSION and args.sha256sums_sha256 == MANIFEST, 'EXACT_RELEASE_PIN_REQUIRED')
    target = TARGETS.get((platform.system(), platform.machine()))
    require(target is not None, 'SUPPORTED_NATIVE_PLATFORM_REQUIRED')
    root = args.root
    require(root.is_absolute() and root.parent.resolve(strict=True) == root.parent and
            not root.exists() and not root.is_symlink(), 'FRESH_ABSOLUTE_INSTALL_ROOT_REQUIRED')
    node_tools = node_tools_directory(args.node_tools_dir)
    root.mkdir(mode=0o700)
    started = time.monotonic()
    result = {'schema': 'zavliq-anonymous-install-smoke-v1', 'scope': 'anonymous_release_install_and_local_readiness',
        'ok': False, 'source_sha': REVISION, 'version': VERSION, 'manifest_sha256': MANIFEST,
        'started_at': datetime.now(timezone.utc).isoformat(), 'runtime_control_origin': LOCAL_ONLY,
        'platform_target': target, 'driver_sha256': release.digest(Path(__file__)),
        'download_helper_sha256': release.digest(Path(release.__file__)),
        'registration_calls': 0, 'message_calls': 0, 'model_calls': 0,
        'full_onboarding_gate_passed': False, 'production_recovery_gate_passed': False}
    stage = 'download'
    try:
        base = f'https://github.com/{release.REPOSITORY}/releases/download/{VERSION}'
        release.download(base + '/SHA256SUMS', root / 'SHA256SUMS', started, 1024 * 1024)
        require(release.digest(root / 'SHA256SUMS') == MANIFEST, 'REVIEWED_MANIFEST_MISMATCH')
        checksums = release.manifest((root / 'SHA256SUMS').read_text())
        native = f'zavliq-{VERSION}-{target}.tar.gz'
        assets = ['install.sh', 'skill.md', 'zavliq-node-0.1.0.tar.gz', 'zavliq-0.1.0-py3-none-any.whl']
        require(set([*assets, native]) <= checksums.keys(), 'REQUIRED_RELEASE_ASSET_MISSING')
        for name in assets:
            release.download(base + '/' + name, root / name, started)
            require(release.digest(root / name) == checksums[name], 'RELEASE_ASSET_CHECKSUM_MISMATCH')
        stage = 'installation'
        clean = environment(root, node_tools)
        def command(values, **options):
            return release.command(values, started, env=clean, **options)
        node_version = command(['node', '--version']).decode().strip()
        require(bool(re.fullmatch(r'v\d+\.\d+\.\d+', node_version)) and int(node_version.split('.')[0][1:]) >= 22,
                'NODE_22_REQUIRED')
        npm_version = command(['npm', '--version']).decode().strip()
        command(['sh', str(root / 'install.sh'), '--version', VERSION, '--sha256sums-sha256', MANIFEST,
                 '--install-dir', str(root / 'bin')])
        binary = root / 'bin/zavliq'
        require(release.digest(binary) == BINARIES[target], 'INSTALLED_NATIVE_BYTES_MISMATCH')
        require(command([str(binary), '--version']).decode().strip() == 'zavliq 0.1.0', 'NATIVE_VERSION_MISMATCH')
        methods = command([str(binary), 'methods'])
        require(release.hashlib.sha256(methods).hexdigest() == METHODS_HASH, 'NATIVE_METHODS_MISMATCH')
        command([sys.executable, '-I', '-B', '-m', 'venv', str(root / 'venv')])
        python = root / 'venv/bin/python'
        command([str(python), '-I', '-B', '-m', 'pip', 'install', '--no-index', '--no-deps',
                 '--disable-pip-version-check', str(root / assets[-1])])
        node = root / 'node'
        node.mkdir()
        release.unpack_node(root / assets[-2], node, 'zavliq-node-0.1.0')
        bundle = node / 'zavliq-node-0.1.0'
        command(['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund'], cwd=bundle)
        config = {'install_root': str(root), 'version': VERSION, 'manifest_sha256': MANIFEST,
            'asset_sha256': {name: checksums[name] for name in [*assets, native]},
            'binary': str(binary), 'binary_sha256': BINARIES[target], 'bundle': str(bundle),
            'mcp_entry': str(bundle / 'node_modules/@zavliq/mcp/src/index.mjs'), 'skill': str(root / 'skill.md'),
            'started': started, 'node_version': node_version, 'local_only': LOCAL_ONLY,
            'prerequisite_node_tools_dir': str(node_tools),
            'tools': TOOLS, 'schemas_sha256': SCHEMAS_HASH, 'config_path': str(root / 'worker.json')}
        write_json(root / 'worker.json', config)
        stage = 'local_readiness'
        output = command([str(python), '-I', '-B', str(Path(__file__).resolve()), '_worker',
                          '--config', config['config_path']])
        proof = json.loads(output)
        validate_report(proof['readiness'])
        result.update(proof, ok=True, native_version='zavliq 0.1.0', native_methods_sha256=METHODS_HASH,
                      npm_version=npm_version, anonymous_public_installation_verified=True)
    except Exception as error:
        code = str(error) if isinstance(error, ValueError) and re.fullmatch(r'[A-Z_]{1,100}', str(error)) else type(error).__name__
        result.update(error_code=code, failure_stage=stage)
    result.update(elapsed_seconds=round(time.monotonic() - started, 3), install_root_preserved=True,
                  finished_at=datetime.now(timezone.utc).isoformat())
    write_json(root / 'result.json', result)
    return result


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    attempt = commands.add_parser('run')
    attempt.add_argument('--root', type=Path, required=True)
    attempt.add_argument('--version', required=True)
    attempt.add_argument('--sha256sums-sha256', required=True)
    attempt.add_argument('--node-tools-dir', type=Path,
                         help='Canonical directory containing executable node and npm; default resolves caller node once.')
    attempt.add_argument('--execute', action='store_true')
    attempt.add_argument('--release-ready', action='store_true')
    internal = commands.add_parser('_worker')
    internal.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    if args.command == '_worker':
        result = asyncio.run(worker(json.loads(args.config.read_text())))
    else:
        result = run(args)
    print(json.dumps(result))
    if result.get('ok') is False:
        raise SystemExit(1)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'ok': False, 'error_class': type(error).__name__, 'scope': 'anonymous_install_smoke'}))
        raise SystemExit(1) from None
