#!/usr/bin/env python3
"""Build immutable staging images using the exact commit's verified draft runtime."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

POSTGRES_REF = 'postgres:17.11-alpine'
# Existing private-stage amd64 image; a changed upstream tag requires review.
POSTGRES_ID = 'sha256:18cfe3ef5e6815560c98237d6216d1e5119702fb0f3894c8785dd58b8bbe5d73'
ECHO_ID = '@echo:localhost'


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def checked(command, **kwargs):
    return subprocess.run(command, check=True, timeout=kwargs.pop('timeout', 30), **kwargs)


def extract_source(archive, destination):
    with tarfile.open(archive, 'r:gz') as source:
        members = source.getmembers()
        for item in members:
            path = Path(item.name)
            if path.is_absolute() or '..' in path.parts or not (item.isfile() or item.isdir()):
                raise ValueError('SOURCE_ARCHIVE_MEMBER_REFUSED')
            if any(part in {'.git', '.local', '.private', '.staging', '.production', '.terraform'} for part in path.parts):
                raise ValueError('PRIVATE_SOURCE_PATH_REFUSED')
        if sum(item.size for item in members) > 256 * 1024 * 1024:
            raise ValueError('SOURCE_ARCHIVE_TOO_LARGE')
        source.extractall(destination, filter='data')


def source_package(revision, output):
    root = Path(__file__).resolve().parents[2]
    resolved = checked(['git', 'rev-parse', revision + '^{commit}'], cwd=root, capture_output=True, text=True).stdout.strip()
    if not re.fullmatch(r'[0-9a-f]{40}', revision) or resolved != revision:
        raise ValueError('EXACT_COMMIT_REQUIRED')
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    archive = output / 'source.tar.gz'
    with archive.open('wb') as stream:
        checked(['git', 'archive', '--format=tar.gz', revision], cwd=root, stdout=stream, timeout=60)
    manifest = {'revision': revision, 'source_archive_sha256': digest(archive),
                'architecture': 'linux/amd64', 'web_echo_user_id': ECHO_ID,
                'postgres': {'ref': POSTGRES_REF, 'id': POSTGRES_ID}}
    (output / 'source-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return {'operation': 'source_package', **manifest}


def attach_runtime(source, run_id):
    """Only the existing draft workflow for this source commit may supply native code."""
    root = Path(__file__).resolve().parents[2]
    manifest = json.loads((source / 'source-manifest.json').read_text())
    repository = checked(['gh', 'repo', 'view', '--json', 'nameWithOwner', '--jq', '.nameWithOwner'], cwd=root, capture_output=True, text=True).stdout.strip()
    run = json.loads(checked(['gh', 'api', f'repos/{repository}/actions/runs/{run_id}'], cwd=root, capture_output=True, text=True).stdout)
    if run['head_sha'] != manifest['revision'] or run['status'] != 'completed' or run['conclusion'] != 'success' or run['path'] != '.github/workflows/release.yml':
        raise ValueError('SUCCESSFUL_EXACT_COMMIT_DRAFT_WORKFLOW_REQUIRED')
    if (source / 'native-runtime.tar.gz').exists():
        raise ValueError('RUNTIME_ATTACHMENT_ALREADY_EXISTS')
    with tempfile.TemporaryDirectory(prefix='zavliq-draft-runtime-') as temporary:
        checked(['gh', 'run', 'download', str(run_id), '--repo', repository, '--name', 'runtime-x86_64-unknown-linux-gnu', '--dir', temporary], timeout=300)
        archives = list(Path(temporary).glob('*.tar.gz'))
        if len(archives) != 1 or not re.fullmatch(r'zavliq-v[0-9A-Za-z.-]+-x86_64-unknown-linux-gnu\.tar\.gz', archives[0].name):
            raise ValueError('ONE_EXPECTED_LINUX_ARTIFACT_REQUIRED')
        with tarfile.open(archives[0], 'r:gz') as archive:
            members = archive.getmembers()
            if {item.name for item in members} != {'zavliq', 'LICENSE', 'README.md'} or any(not item.isfile() or item.size > 256 * 1024 * 1024 for item in members):
                raise ValueError('RUNTIME_ARCHIVE_MEMBERS_REFUSED')
            binary = archive.extractfile('zavliq').read()
        if not binary.startswith(b'\x7fELF'):
            raise ValueError('ELF_RUNTIME_REQUIRED')
        shutil.copy2(archives[0], source / 'native-runtime.tar.gz')
    manifest['native_runtime'] = {'repository': repository, 'workflow': '.github/workflows/release.yml', 'run_id': run_id,
                                  'head_sha': run['head_sha'], 'target': 'x86_64-unknown-linux-gnu',
                                  'archive_sha256': digest(source / 'native-runtime.tar.gz'), 'binary_sha256': hashlib.sha256(binary).hexdigest()}
    (source / 'source-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return {'operation': 'attach_draft_runtime', **manifest['native_runtime']}


def build_bundle(source, output):
    manifest = json.loads((source / 'source-manifest.json').read_text())
    revision = manifest['revision']
    if not re.fullmatch(r'[0-9a-f]{40}', revision) or manifest['web_echo_user_id'] != ECHO_ID:
        raise ValueError('INVALID_STAGING_SOURCE_MANIFEST')
    if digest(source / 'source.tar.gz') != manifest['source_archive_sha256']:
        raise ValueError('SOURCE_ARCHIVE_HASH_MISMATCH')
    runtime = manifest.get('native_runtime', {})
    if runtime.get('head_sha') != revision or runtime.get('target') != 'x86_64-unknown-linux-gnu' or digest(source / 'native-runtime.tar.gz') != runtime.get('archive_sha256'):
        raise ValueError('VERIFIED_EXACT_COMMIT_RUNTIME_REQUIRED')
    architecture = checked(['docker', 'info', '--format', '{{.OSType}}/{{.Architecture}}'], capture_output=True, text=True).stdout.strip()
    if architecture not in {'linux/amd64', 'linux/x86_64'}:
        raise ValueError('NATIVE_LINUX_AMD64_BUILDER_REQUIRED')
    postgres_id = checked(['docker', 'image', 'inspect', '--format', '{{.Id}}', POSTGRES_REF], capture_output=True, text=True).stdout.strip()
    if postgres_id != POSTGRES_ID or manifest['postgres'] != {'ref': POSTGRES_REF, 'id': POSTGRES_ID}:
        raise ValueError('CACHED_POSTGRES_PIN_REQUIRED')
    refs = {name: f'zavliq-{name}:{revision}' for name in ['synapse', 'control', 'web', 'echo']}
    for ref in refs.values():
        existing = subprocess.run(['docker', 'image', 'inspect', ref], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        if existing.returncode == 0:
            raise ValueError('IMMUTABLE_IMAGE_TAG_ALREADY_EXISTS')
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='zavliq-build-') as temporary:
        directory = Path(temporary)
        extract_source(source / 'source.tar.gz', directory)
        runtime_dir = directory / '.zavliq-runtime'
        runtime_dir.mkdir()
        with tarfile.open(source / 'native-runtime.tar.gz', 'r:gz') as archive:
            binary = archive.extractfile('zavliq').read()
        if hashlib.sha256(binary).hexdigest() != runtime['binary_sha256']:
            raise ValueError('NATIVE_BINARY_HASH_MISMATCH')
        (runtime_dir / 'zavliq').write_bytes(binary)
        (runtime_dir / 'zavliq').chmod(0o755)
        for name, ref in refs.items():
            dockerfile = 'echo-artifact' if name == 'echo' else name
            args = ['docker', 'buildx', 'build', '--platform', 'linux/amd64', '--load', '-f', f'infra/docker/{dockerfile}.Dockerfile', '-t', ref]
            if name == 'web':
                args += ['--build-arg', 'VITE_ECHO_USER_ID=' + ECHO_ID]
            if name == 'echo':
                args += ['--build-arg', 'ZAVLIQ_NATIVE_SHA256=' + runtime['binary_sha256'], '--build-arg', 'ZAVLIQ_SOURCE_REVISION=' + revision]
            # Serial packaging builds only; no Rust compiler runs on the measured host.
            checked(args + ['.'], cwd=directory, timeout=3600)
        refs['postgres'] = POSTGRES_REF
        images = {}
        for name, ref in refs.items():
            image_id = checked(['docker', 'image', 'inspect', '--format', '{{.Id}}', ref], capture_output=True, text=True).stdout.strip()
            images['gateway' if name == 'web' else name] = {'ref': ref, 'id': image_id}
        if images['postgres'] != manifest['postgres']:
            raise ValueError('POSTGRES_PIN_CHANGED_REVIEW_REQUIRED')
        shutil.copytree(directory / 'infra', output / 'infra')
    # The archive is a Docker image archive, not a registry credential export.
    archive = output / 'images.tar'
    checked(['docker', 'save', '-o', str(archive), *refs.values()], timeout=600)
    checked(['gzip', '-n', str(archive)], timeout=600)
    shutil.copy2(source / 'source.tar.gz', output / 'source.tar.gz')
    manifest.update(images=images, images_archive_sha256=digest(output / 'images.tar.gz'))
    manifest['infra_hashes'] = {str(path.relative_to(output)): digest(path) for path in sorted((output / 'infra').rglob('*')) if path.is_file()}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return {'operation': 'build_staging_bundle', 'revision': revision, 'manifest_sha256': digest(output / 'manifest.json'), 'images': images, 'web_echo_user_id': ECHO_ID}


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    source = commands.add_parser('source')
    source.add_argument('--revision', required=True)
    source.add_argument('--output', type=Path, required=True)
    attach = commands.add_parser('attach-runtime')
    attach.add_argument('--source', type=Path, required=True)
    attach.add_argument('--run-id', type=int, required=True)
    build = commands.add_parser('build')
    build.add_argument('--source', type=Path, required=True)
    build.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == 'source':
        result = source_package(args.revision, args.output.resolve())
    elif args.command == 'attach-runtime':
        result = attach_runtime(args.source.resolve(), args.run_id)
    else:
        result = build_bundle(args.source.resolve(), args.output.resolve())
    print(json.dumps(result))


if __name__ == '__main__':
    main()
