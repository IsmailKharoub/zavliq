#!/usr/bin/env python3
"""Explicit operator-run public release installation followed by unchanged model tools.

No action occurs on import. Run one numbered attempt at a time so production
admission can be respected between attempts. The original cost ledger is shared.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import hashlib
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.request
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / 'tests/onboarding/.local'
EVIDENCE = ROOT / 'tests/onboarding/evidence'
LEDGER = PRIVATE / 'budget.sqlite3'
ORIGIN = 'https://zavliq.com'
REPOSITORY = 'IsmailKharoub/zavliq'
HOSTS = {'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com', 'github-releases.githubusercontent.com'}
MODELS = ['amazon.nova-micro-v1:0', 'us.meta.llama3-3-70b-instruct-v1:0']
DEADLINE_SECONDS = 300


def assignment(index):
    return MODELS[(index - 1) % 2], 'mcp' if (index - 1) % 4 in (0, 3) else 'sdk'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def remaining(started):
    value = DEADLINE_SECONDS - (time.monotonic() - started)
    if value <= 0:
        raise TimeoutError('INSTALL_TO_REPLY_TIMEOUT')
    return value


def manifest(text):
    entries = {}
    for line in text.splitlines():
        match = re.fullmatch(r'([a-fA-F0-9]{64})  ([A-Za-z0-9._-]+)', line)
        if not match or match[2] in entries:
            raise ValueError('INVALID_RELEASE_CHECKSUMS')
        entries[match[2]] = match[1].lower()
    return entries


def endpoint(url):
    value = urlsplit(url)
    if value.scheme != 'https' or value.hostname not in HOSTS or value.username or value.password or value.port or value.fragment:
        raise ValueError('UNEXPECTED_RELEASE_ENDPOINT')
    return url


class ReleaseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return super().redirect_request(request, fp, code, message, headers, endpoint(newurl))


def download(url, destination, started, maximum=32 * 1024 * 1024):
    # A fresh opener has no cookies, auth handlers, netrc or inherited proxy auth.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), ReleaseRedirect())
    with opener.open(endpoint(url), timeout=min(20, remaining(started))) as response, destination.open('xb') as output:
        count = 0
        while True:
            remaining(started)
            chunk = response.read(65536)
            if not chunk:
                break
            count += len(chunk)
            if count > maximum:
                raise ValueError('RELEASE_ASSET_TOO_LARGE')
            output.write(chunk)


def command(args, started, *, env, cwd=None):
    process = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        output, _ = process.communicate(timeout=remaining(started))
    except BaseException:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise
    if process.returncode:
        raise RuntimeError('INSTALL_OR_WORKER_COMMAND_FAILED')
    return output


def node_tools_directory(explicit=None):
    """Resolve the operator's Node prerequisite without inheriting their PATH."""
    if explicit is None:
        node = shutil.which('node')
        if node is None:
            raise ValueError('NODE_TOOLS_DIRECTORY_REQUIRED')
        directory = Path(node).parent.resolve(strict=True)
    else:
        directory = Path(explicit)
    if not (directory.is_absolute() and directory.resolve(strict=True) == directory and directory.is_dir()):
        raise ValueError('CANONICAL_NODE_TOOLS_DIRECTORY_REQUIRED')
    if not all((directory / name).is_file() and os.access(directory / name, os.X_OK)
               for name in ('node', 'npm')):
        raise ValueError('NODE_AND_NPM_EXECUTABLES_REQUIRED')
    return directory


def install_environment(root, node_tools):
    """Match the reviewed anonymous smoke's fresh configs and credential isolation."""
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


def unpack_node(archive, destination, name):
    with tarfile.open(archive) as source:
        members = source.getmembers()
        if sum(item.size for item in members) > 64 * 1024 * 1024:
            raise ValueError('NODE_ARCHIVE_TOO_LARGE')
        seen = set()
        for item in members:
            path = Path(item.name)
            if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != name or item.name in seen or not (item.isdir() or item.isfile()):
                raise ValueError('INVALID_NODE_ARCHIVE')
            seen.add(item.name)
        for item in members:
            target = destination / item.name
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(item) as input_file, target.open('xb') as output:
                    output.write(input_file.read())


def inside(path, root):
    path = Path(path).resolve(strict=True)
    if not path.is_relative_to(root.resolve(strict=True)):
        raise ValueError('CHECKOUT_OR_EXTERNAL_PACKAGE_FALLBACK')
    return path


def require_ledger(*, spending=True):
    if not LEDGER.is_file():
        raise ValueError('ORIGINAL_CUMULATIVE_LEDGER_REQUIRED')
    with closing(sqlite3.connect(LEDGER.as_uri() + '?mode=ro', uri=True)) as db:
        total, mismatches = db.execute("SELECT SUM(charged),SUM(state='accounting_mismatch') FROM calls").fetchone()
        if total is None or (spending and (total >= 2 or mismatches)):
            raise ValueError('BUDGET_UNAVAILABLE')


def installed_provenance(config):
    import zavliq
    root = Path(config['install_root'])
    package = inside(zavliq.__file__, root / 'venv')
    client_module = inside(inspect.getfile(zavliq.Zavliq), root / 'venv')
    inside(config['binary'], root / 'bin')
    entry = inside(config['mcp_entry'], root / 'node')
    skill = inside(config['skill'], root)
    if importlib.metadata.version('zavliq') != config['version'][1:]:
        raise ValueError('INSTALLED_WHEEL_VERSION_MISMATCH')
    for path, expected in [(Path(config['binary']), config['binary_sha256']), (skill, config['asset_sha256']['skill.md'])]:
        if digest(path) != expected:
            raise ValueError('INSTALLED_ASSET_CHANGED')
    node_modules = entry.parents[3]
    for name in ['client', 'mcp']:
        package_file = inside(node_modules / '@zavliq' / name / 'package.json', root / 'node')
        if json.loads(package_file.read_text())['version'] != config['version'][1:]:
            raise ValueError('INSTALLED_NODE_VERSION_MISMATCH')
    return {'version': config['version'], 'repository': REPOSITORY, 'origin': ORIGIN,
            'sha256sums_sha256': config['manifest_sha256'], 'asset_sha256': config['asset_sha256'],
            'binary_sha256': config['binary_sha256'], 'python_module_sha256': digest(package),
            'python_client_sha256': digest(client_module), 'node_version': config.get('node_version'),
            'platform': {'system': platform.system(), 'machine': platform.machine(), 'python_version': platform.python_version()},
            'mcp_entry_sha256': digest(entry), 'anonymous_downloads': True,
            'operator_install': True, 'fresh_install_root': True, 'checkout_fallback': False}


async def worker(config):
    os.environ['ZAVLIQ_ONBOARDING_INSTALLED'] = '1'
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import harness
    require_ledger()
    provenance = installed_provenance(config)
    schema = harness.McpConnection(Path(config['install_root']) / 'schema', ORIGIN, config['binary'], Path(config['mcp_entry']))
    await schema.start()
    try:
        tools = harness.bedrock_tools(await schema.tools())
    finally:
        await schema.close()
    provenance['tool_schema_sha256'] = hashlib.sha256(json.dumps(tools, sort_keys=True).encode()).hexdigest()
    model, transport = assignment(config['index'])
    await harness.trial(config['index'], model, transport, ORIGIN, config['binary'], tools,
        harness.Budget(LEDGER), config['run_id'], skill_path=Path(config['skill']),
        mcp_entry=Path(config['mcp_entry']), peer_directory=Path(config['peer_directory']),
        install_started=config['started'], provenance=provenance)


def attempt(args):
    if not args.release_ready or not args.service_ready:
        raise ValueError('EXPLICIT_PUBLISHED_RELEASE_AND_SERVICE_READINESS_REQUIRED')
    require_ledger()
    peer = args.peer_directory.resolve(strict=True)
    if not (peer / 'identity.json').is_file():
        raise ValueError('PREPROVISIONED_FIXTURE_PEER_REQUIRED')
    identifier = f'{args.run_id}-{args.index:02d}'
    evidence = EVIDENCE / f'{identifier}.json'
    root = PRIVATE / 'release-installs' / identifier
    if evidence.exists() or root.exists():
        raise ValueError('ATTEMPT_ALREADY_EXISTS_KEEP_FAILURE_EVIDENCE')
    root.mkdir(parents=True, mode=0o700)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    failure_stage = 'installation'
    try:
        base = f'https://github.com/{REPOSITORY}/releases/download/{args.version}'
        download(base + '/SHA256SUMS', root / 'SHA256SUMS', started, 1024 * 1024)
        if digest(root / 'SHA256SUMS') != args.sha256sums_sha256:
            raise ValueError('REVIEWED_MANIFEST_MISMATCH')
        checksums = manifest((root / 'SHA256SUMS').read_text())
        target = {('Darwin', 'arm64'): 'aarch64-apple-darwin', ('Linux', 'x86_64'): 'x86_64-unknown-linux-gnu'}.get((platform.system(), platform.machine()))
        native_asset = f'zavliq-{args.version}-{target}.tar.gz'
        if target is None or native_asset not in checksums:
            raise ValueError('SUPPORTED_NATIVE_RELEASE_REQUIRED')
        assets = ['install.sh', 'skill.md', f'zavliq-node-{args.version[1:]}.tar.gz', f'zavliq-{args.version[1:]}-py3-none-any.whl']
        for name in assets:
            if name not in checksums:
                raise ValueError('REQUIRED_RELEASE_ASSET_MISSING')
            download(base + '/' + name, root / name, started)
            if digest(root / name) != checksums[name]:
                raise ValueError('RELEASE_ASSET_CHECKSUM_MISMATCH')
        # Download/install children receive no GitHub, npm, AWS or proxy credentials.
        node_tools = node_tools_directory(args.node_tools_dir)
        clean = install_environment(root, node_tools)
        node_version = command(['node', '--version'], started, env=clean).decode().strip()
        if not re.fullmatch(r'v\d+\.\d+\.\d+', node_version) or int(node_version.split('.')[0][1:]) < 22:
            raise ValueError('NODE_22_REQUIRED')
        command(['sh', str(root / 'install.sh'), '--version', args.version, '--sha256sums-sha256', args.sha256sums_sha256, '--install-dir', str(root / 'bin')], started, env=clean)
        binary = root / 'bin/zavliq'
        command([str(binary), 'methods'], started, env=clean)
        command([sys.executable, '-m', 'venv', str(root / 'venv')], started, env=clean)
        python = root / 'venv/bin/python'
        command([str(python), '-m', 'pip', 'install', '--no-deps', str(root / assets[-1])], started, env=clean)
        node = root / 'node'
        node.mkdir()
        name = f'zavliq-node-{args.version[1:]}'
        unpack_node(root / (name + '.tar.gz'), node, name)
        bundle = node / name
        command(['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund'], started, env=clean, cwd=bundle)
        config = {'install_root': str(root), 'version': args.version, 'manifest_sha256': args.sha256sums_sha256,
                  'asset_sha256': {name: checksums[name] for name in [*assets, native_asset]}, 'binary': str(binary), 'binary_sha256': digest(binary),
                  'mcp_entry': str(bundle / 'node_modules/@zavliq/mcp/src/index.mjs'), 'skill': str(root / 'skill.md'),
                  'peer_directory': str(peer), 'run_id': args.run_id, 'index': args.index, 'started': started, 'node_version': node_version}
        config_path = root / 'worker.json'
        config_path.write_text(json.dumps(config))
        failure_stage = 'worker'
        # Keep the existing Bedrock worker authentication context, but make its
        # MCP lookup use the same validated Node prerequisite as installation.
        worker_env = {**os.environ, 'PATH': str(node_tools) + os.pathsep + os.environ.get('PATH', os.defpath)}
        output = command([str(python), '-I', str(Path(__file__).resolve()), '_worker', '--config', str(config_path)], started, env=worker_env)
        if not evidence.is_file():
            raise ValueError('MISSING_TRIAL_EVIDENCE')
        print(output.decode(), end='')
    except Exception as error:
        if not evidence.exists():
            model, transport = assignment(args.index)
            elapsed = round(time.monotonic() - started, 3)
            result = {'trial': identifier, 'model': model, 'provider': 'Amazon' if model == MODELS[0] else 'Meta',
                      'transport': transport, 'passed': False, 'within_five_minutes': False,
                      'elapsed_seconds': elapsed, 'installation_seconds': elapsed if failure_stage == 'installation' else None,
                      'model_seconds': 0 if failure_stage == 'installation' else None, 'failure_stage': failure_stage,
                      'outcome': type(error).__name__, 'scope': 'public_release_install_attempt', 'version': args.version}
            evidence.write_text(json.dumps(result, indent=2) + '\n')
        raise


def summary(run_id):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import harness
    require_ledger(spending=False)
    results = [json.loads((EVIDENCE / f'{run_id}-{index:02d}.json').read_text()) for index in range(1, 11)]
    report = harness.summarize(results, run_id, harness.Budget(LEDGER))
    provenance = [item.get('release_install', {}) for item in results if item.get('within_five_minutes')]
    report['scope'] = 'documented_operator_anonymous_install_and_model_onboarding'
    report['gate_passed'] = report['gate_passed'] and all(p.get('anonymous_downloads') and p.get('checkout_fallback') is False and p.get('origin') == ORIGIN for p in provenance) and len({(p.get('version'), p.get('sha256sums_sha256')) for p in provenance}) == 1
    harness.write_json(EVIDENCE / (run_id + '-summary.json'), report)
    print(json.dumps(report))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    run = sub.add_parser('trial')
    run.add_argument('--version', required=True)
    run.add_argument('--sha256sums-sha256', required=True)
    run.add_argument('--run-id', required=True)
    run.add_argument('--index', type=int, choices=range(1, 11), required=True)
    run.add_argument('--peer-directory', type=Path, required=True)
    run.add_argument('--node-tools-dir', type=Path,
                     help='Canonical absolute directory containing executable node and npm; defaults to the caller-installed Node directory.')
    run.add_argument('--release-ready', action='store_true')
    run.add_argument('--service-ready', action='store_true')
    aggregate = sub.add_parser('summary')
    aggregate.add_argument('--run-id', required=True)
    internal = sub.add_parser('_worker')
    internal.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    if args.command == '_worker':
        asyncio.run(worker(json.loads(args.config.read_text())))
        return
    if not re.fullmatch(r'release-[a-z0-9][a-z0-9-]{0,14}', args.run_id):
        parser.error('Use release- followed by 1–15 lowercase letters, digits or hyphens; fixture handles must fit the service limit.')
    if args.command == 'summary':
        summary(args.run_id)
    else:
        if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', args.version) or not re.fullmatch(r'[a-f0-9]{64}', args.sha256sums_sha256):
            parser.error('Use an explicit release version and reviewed SHA256SUMS digest.')
        attempt(args)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'operation': 'release_onboarding', 'ok': False, 'error_class': type(error).__name__}), file=sys.stderr)
        raise SystemExit(1)
