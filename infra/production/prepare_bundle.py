#!/usr/bin/env python3
"""Prepare a separate public-web bundle from reviewed stage bytes; never activate it."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile

SCHEMA = 'zavliq.production-bundle.v1'
ORIGIN = 'https://zavliq.com'
ECHO = '@echo:zavliq.com'
BASE_REFS = {'node': 'node:24.21.0-bookworm-slim', 'caddy': 'caddy:2.11.4-alpine'}
SERVICES = {'synapse', 'control', 'gateway', 'postgres', 'echo'}
HELPERS = {'synapse-config': 'synapse', 'bootstrap': 'synapse', 'retention': 'synapse', 'echo-bootstrap': 'control'}
PRIVATE_PARTS = {'.git', '.local', '.private', '.staging', '.production', '.terraform'}


def sha256(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def regular(path):
    if not path.is_file() or path.is_symlink():
        raise ValueError('REGULAR_INPUT_FILE_REQUIRED')
    return path


def require_hash(path, expected):
    if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected) or sha256(regular(path)) != expected:
        raise ValueError('REVIEWED_INPUT_HASH_MISMATCH')


def json_file(path):
    regular(path)
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('MANIFEST_TOO_LARGE')
    return json.loads(path.read_text())


def safe_member(member):
    path = PurePosixPath(member.name)
    if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
        raise ValueError('ARCHIVE_MEMBER_REFUSED')
    if PRIVATE_PARTS.intersection(path.parts):
        raise ValueError('PRIVATE_SOURCE_PATH_REFUSED')


def extract_source(archive_path, destination):
    with tarfile.open(archive_path, 'r:gz') as archive:
        total, seen = 0, set()
        for member in archive:
            safe_member(member)
            name = str(PurePosixPath(member.name))
            if name in seen:
                raise ValueError('DUPLICATE_ARCHIVE_MEMBER')
            seen.add(name)
            total += member.size
            if total > 256 * 1024 * 1024:
                raise ValueError('SOURCE_ARCHIVE_TOO_LARGE')
            archive.extract(member, destination, filter='data')
    # The data filter deliberately drops directory modes; under our private
    # umask those directories otherwise become 0700 and Docker COPY preserves
    # that restriction for unprivileged image users. Only public source children
    # are normalized. Keep the outer workspace private and file modes intact.
    for path in destination.rglob('*'):
        if path.is_dir():
            path.chmod(0o755)


def tree_hashes(directory):
    result = {}
    for path in sorted((directory / 'infra').rglob('*')):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError('INFRA_MEMBER_REFUSED')
        if path.is_file():
            result[path.relative_to(directory).as_posix()] = sha256(path)
    return result


def verify_runtime(path, expected, revision):
    if (expected.get('repository') != 'IsmailKharoub/zavliq' or expected.get('workflow') != '.github/workflows/release.yml'
            or expected.get('head_sha') != revision or expected.get('target') != 'x86_64-unknown-linux-gnu'
            or type(expected.get('run_id')) is not int or expected['run_id'] <= 0):
        raise ValueError('EXACT_DRAFT_RUNTIME_PROVENANCE_REQUIRED')
    require_hash(path, expected.get('archive_sha256'))
    with tarfile.open(path, 'r:gz') as archive:
        members = archive.getmembers()
        if len(members) != 3 or {member.name for member in members} != {'zavliq', 'LICENSE', 'README.md'}:
            raise ValueError('RUNTIME_ARCHIVE_MEMBERS_REFUSED')
        if any(not member.isfile() or member.size > 256 * 1024 * 1024 for member in members):
            raise ValueError('RUNTIME_ARCHIVE_MEMBERS_REFUSED')
        binary = archive.extractfile('zavliq').read()
    if len(binary) < 20 or binary[:6] != b'\x7fELF\x02\x01' or binary[18:20] != b'\x3e\x00':
        raise ValueError('LINUX_AMD64_ELF_REQUIRED')
    if hashlib.sha256(binary).hexdigest() != expected.get('binary_sha256'):
        raise ValueError('NATIVE_BINARY_HASH_MISMATCH')


def verify_docker_archive(path, images, runtime):
    """Check tags and exact image configuration bytes before docker load can retag anything."""
    expected = {image['ref']: image['id'] for image in images.values()}
    with tarfile.open(path, 'r:gz') as archive:
        manifest_member = archive.getmember('manifest.json')
        if not manifest_member.isfile() or manifest_member.size > 1024 * 1024:
            raise ValueError('DOCKER_ARCHIVE_MANIFEST_REFUSED')
        manifest = json.load(archive.extractfile(manifest_member))
        found = {}
        for item in manifest:
            config_name = PurePosixPath(item['Config'])
            if config_name.is_absolute() or '..' in config_name.parts:
                raise ValueError('DOCKER_CONFIG_PATH_REFUSED')
            member = archive.getmember(str(config_name))
            if not member.isfile() or member.size > 2 * 1024 * 1024:
                raise ValueError('DOCKER_CONFIG_REFUSED')
            data = archive.extractfile(member).read()
            image_id = 'sha256:' + hashlib.sha256(data).hexdigest()
            config = json.loads(data)
            if config.get('os') != 'linux' or config.get('architecture') != 'amd64':
                raise ValueError('LINUX_AMD64_IMAGE_REQUIRED')
            for ref in item.get('RepoTags') or []:
                if ref in found or expected.get(ref) != image_id:
                    raise ValueError('DOCKER_ARCHIVE_IMAGE_PIN_MISMATCH')
                found[ref] = image_id
            if image_id == images['echo']['id']:
                labels = (config.get('config') or {}).get('Labels') or {}
                if labels.get('org.opencontainers.image.revision') != runtime['head_sha'] or labels.get('com.zavliq.native.sha256') != runtime['binary_sha256']:
                    raise ValueError('ECHO_RUNTIME_PROVENANCE_MISMATCH')
        if found != expected or len(manifest) != len(images):
            raise ValueError('EXACT_DOCKER_ARCHIVE_IMAGES_REQUIRED')


def verify_inputs(stage, manifest_hash, runtime_path, bases_path, bases_hash, source):
    require_hash(stage / 'manifest.json', manifest_hash)
    value = json_file(stage / 'manifest.json')
    revision = value.get('revision', '')
    if not re.fullmatch(r'[0-9a-f]{40}', revision) or value.get('architecture') != 'linux/amd64' or value.get('web_echo_user_id') != '@echo:localhost':
        raise ValueError('REVIEWED_FINAL_STAGE_BUNDLE_REQUIRED')
    for name, field in [('source.tar.gz', 'source_archive_sha256'), ('images.tar.gz', 'images_archive_sha256')]:
        require_hash(stage / name, value.get(field))
    images = value.get('images', {})
    if set(images) != SERVICES:
        raise ValueError('ALL_STAGE_IMAGES_REQUIRED')
    for name, image in images.items():
        ref = 'postgres:17.11-alpine' if name == 'postgres' else f'zavliq-{"web" if name == "gateway" else name}:{revision}'
        if image.get('ref') != ref or not re.fullmatch(r'sha256:[0-9a-f]{64}', image.get('id', '')):
            raise ValueError('EXACT_STAGE_IMAGE_PINS_REQUIRED')
    if value.get('postgres') != images['postgres']:
        raise ValueError('STAGE_POSTGRES_PROVENANCE_MISMATCH')
    verify_runtime(runtime_path, value.get('native_runtime', {}), revision)
    verify_docker_archive(stage / 'images.tar.gz', images, value['native_runtime'])
    extract_source(stage / 'source.tar.gz', source)
    expected_infra = value.get('infra_hashes')
    if not expected_infra or tree_hashes(stage) != expected_infra or tree_hashes(source) != expected_infra:
        raise ValueError('EXACT_ARCHIVED_INFRA_REQUIRED')
    require_hash(bases_path, bases_hash)
    bases = json_file(bases_path)
    if bases.get('schema') != 'zavliq.public-web-bases.v1' or bases.get('architecture') != 'linux/amd64' or set(bases.get('images', {})) != set(BASE_REFS):
        raise ValueError('REVIEWED_WEB_BASE_PINS_REQUIRED')
    for name, base in bases['images'].items():
        repository = 'docker.io/library/' + name
        if base.get('ref') != BASE_REFS[name] or not re.fullmatch(r'sha256:[0-9a-f]{64}', base.get('id', '')) or not re.fullmatch(re.escape(repository) + r'@sha256:[0-9a-f]{64}', base.get('digest_ref', '')):
            raise ValueError('EXACT_WEB_BASE_PINS_REQUIRED')
    return value, bases


def checked(command, *, timeout=30, cwd=None):
    return subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout, cwd=cwd)


def inspect_image(ref):
    image = json.loads(checked(['docker', 'image', 'inspect', '--format', '{{json .}}', ref]).stdout)
    if image.get('Os') != 'linux' or image.get('Architecture') != 'amd64':
        raise ValueError('CACHED_LINUX_AMD64_IMAGE_REQUIRED')
    return image


def tagged_ids(ref):
    return set(checked(['docker', 'image', 'ls', '--quiet', '--no-trunc', '--filter', 'reference=' + ref]).stdout.split())


def require_local_builder(bases, images, web_tag):
    if checked(['docker', 'context', 'show']).stdout.strip() != 'default':
        raise ValueError('DEFAULT_DOCKER_CONTEXT_REQUIRED')
    architecture = checked(['docker', 'info', '--format', '{{.OSType}}/{{.Architecture}}']).stdout.strip()
    if architecture not in {'linux/amd64', 'linux/x86_64'}:
        raise ValueError('NATIVE_LINUX_AMD64_BUILDER_REQUIRED')
    # The docker driver shares the daemon's verified local image cache. Do not
    # silently select a container/remote builder or bootstrap a new builder.
    builder = checked(['docker', 'buildx', 'inspect', 'default']).stdout
    if not re.search(r'^Driver:\s+docker\s*$', builder, re.MULTILINE):
        raise ValueError('LOCAL_DOCKER_BUILDER_REQUIRED')
    for base in bases['images'].values():
        image = inspect_image(base['digest_ref'])
        if image.get('Id') != base['id']:
            raise ValueError('CACHED_REVIEWED_BASE_IMAGE_REQUIRED')
    for image in images.values():
        if tagged_ids(image['ref']) - {image['id']}:
            raise ValueError('EXISTING_STAGE_TAG_COLLISION')
    if tagged_ids(web_tag):
        raise ValueError('IMMUTABLE_PUBLIC_WEB_TAG_ALREADY_EXISTS')


def pinned_dockerfile(source, bases):
    path = source / 'infra/docker/web.Dockerfile'
    original = regular(path).read_text()
    expected = ['FROM ' + BASE_REFS['node'] + ' AS build', 'FROM ' + BASE_REFS['caddy']]
    if [line for line in original.splitlines() if line.startswith('FROM ')] != expected or '# syntax=' in original:
        raise ValueError('REVIEWED_WEB_DOCKERFILE_REQUIRED')
    pinned = original.replace(expected[0], 'FROM ' + bases['images']['node']['digest_ref'] + ' AS build', 1)
    pinned = pinned.replace(expected[1], 'FROM ' + bases['images']['caddy']['digest_ref'], 1)
    target = source / 'infra/docker/public-web.Dockerfile'
    target.write_text(pinned)
    shutil.copy2(regular(path.with_name(path.name + '.dockerignore')), target.with_name(target.name + '.dockerignore'))
    return target, {'archived_dockerfile_sha256': sha256(path), 'pinned_dockerfile_sha256': sha256(target)}


def compose_overlay(images):
    pins = {**images, **{name: images[base] for name, base in HELPERS.items()}}
    lines = ['# Preparation only: activation requires a separately reviewed production procedure.', 'services:']
    for name, image in sorted(pins.items()):
        lines += [f'  {name}:', '    image: ' + json.dumps(image['id']), '    pull_policy: never', '    build: !reset null']
    return '\n'.join(lines) + '\n'


def build_command(tag, revision, stage_hash):
    return ['docker', 'buildx', 'build', '--builder', 'default', '--platform', 'linux/amd64', '--pull=false', '--load',
            '-f', 'infra/docker/public-web.Dockerfile', '-t', tag, '--build-arg', 'VITE_ECHO_USER_ID=' + ECHO,
            '--label', 'org.opencontainers.image.revision=' + revision,
            '--label', 'com.zavliq.stage-manifest.sha256=' + stage_hash,
            '--label', 'com.zavliq.web.echo-user-id=' + ECHO, '.']


def prepare(stage, manifest_hash, runtime_path, bases_path, bases_hash, output):
    if output.exists():
        raise ValueError('FRESH_OUTPUT_DIRECTORY_REQUIRED')
    with tempfile.TemporaryDirectory(prefix='zavliq-public-source-') as temporary:
        source = Path(temporary)
        stage_manifest, bases = verify_inputs(stage, manifest_hash, runtime_path, bases_path, bases_hash, source)
        dockerfile, dockerfile_proof = pinned_dockerfile(source, bases)
        build_inputs = {'stage_manifest_sha256': manifest_hash, 'web_bases_sha256': bases_hash,
                        'preparer_sha256': sha256(Path(__file__).resolve()), 'origin': ORIGIN,
                        'web_echo_user_id': ECHO, **dockerfile_proof}
        config_hash = hashlib.sha256(json.dumps(build_inputs, sort_keys=True).encode()).hexdigest()
        revision = stage_manifest['revision']
        tag = f'zavliq-web:{revision}-public-{config_hash[:16]}'
        require_local_builder(bases, stage_manifest['images'], tag)
        # Every external input was verified before this first Docker mutation.
        checked(['docker', 'load', '-i', str(stage / 'images.tar.gz')], timeout=600)
        for image in stage_manifest['images'].values():
            if inspect_image(image['ref'])['Id'] != image['id']:
                raise ValueError('LOADED_STAGE_IMAGE_PIN_MISMATCH')
        # Recheck public tag immediately before building; never replace a cached
        # stage or prior public tag. Use an otherwise idle, exclusive builder.
        if tagged_ids(tag):
            raise ValueError('IMMUTABLE_PUBLIC_WEB_TAG_ALREADY_EXISTS')
        command = build_command(tag, revision, manifest_hash)
        checked(command, timeout=3600, cwd=source)
        web = inspect_image(tag)
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', web.get('Id', '')):
            raise ValueError('PUBLIC_WEB_IMAGE_ID_REQUIRED')
        expected_labels = {'org.opencontainers.image.revision': revision, 'com.zavliq.stage-manifest.sha256': manifest_hash,
                           'com.zavliq.web.echo-user-id': ECHO}
        labels = (web.get('Config') or {}).get('Labels') or {}
        if any(labels.get(key) != value for key, value in expected_labels.items()):
            raise ValueError('PUBLIC_WEB_BUILD_LABEL_MISMATCH')
        images = {**stage_manifest['images'], 'gateway': {'ref': tag, 'id': web['Id']}}
        for name, image in images.items():
            if inspect_image(image['ref'])['Id'] != image['id']:
                raise ValueError('FINAL_IMAGE_PIN_CHANGED')
        output.mkdir(parents=True, exist_ok=False, mode=0o700)
        archive = output / 'images.tar'
        checked(['docker', 'save', '-o', str(archive), *[images[name]['ref'] for name in sorted(images)]], timeout=600)
        with archive.open('rb') as raw, (output / 'images.tar.gz').open('wb') as compressed:
            with gzip.GzipFile(filename='', mode='wb', fileobj=compressed, mtime=0) as encoded:
                shutil.copyfileobj(raw, encoded, length=1024 * 1024)
        archive.unlink()
        verify_docker_archive(output / 'images.tar.gz', images, stage_manifest['native_runtime'])
        for incoming, name in [(stage / 'source.tar.gz', 'source.tar.gz'), (stage / 'manifest.json', 'reviewed-stage-manifest.json'),
                               (runtime_path, 'native-runtime.tar.gz'), (bases_path, 'reviewed-web-bases.json')]:
            shutil.copy2(incoming, output / name)
        # Carry only the exact archived infra. Generated build inputs live beside
        # it, never masquerading as files from the reviewed application commit.
        shutil.copytree(stage / 'infra', output / 'infra')
        shutil.copy2(dockerfile, output / 'public-web.Dockerfile')
        shutil.copy2(Path(__file__).resolve(), output / 'preparer.py')
        (output / 'compose.images.yaml').write_text(compose_overlay(images))
        # Fail before publishing the completion manifest if an operator changed
        # an input during the build or a copy did not preserve the reviewed bytes.
        for name, expected in [('source.tar.gz', stage_manifest['source_archive_sha256']),
                               ('native-runtime.tar.gz', stage_manifest['native_runtime']['archive_sha256']),
                               ('reviewed-stage-manifest.json', manifest_hash), ('reviewed-web-bases.json', bases_hash),
                               ('preparer.py', build_inputs['preparer_sha256'])]:
            require_hash(output / name, expected)
        if tree_hashes(output) != stage_manifest['infra_hashes']:
            raise ValueError('COPIED_INFRA_HASH_MISMATCH')
        manifest = {'schema': SCHEMA, 'activation_supported': False, 'revision': revision, 'architecture': 'linux/amd64',
                    'source_archive_sha256': stage_manifest['source_archive_sha256'],
                    'native_runtime': stage_manifest['native_runtime'], 'images': images,
                    'images_archive_sha256': sha256(output / 'images.tar.gz'), 'infra_hashes': stage_manifest['infra_hashes'],
                    'compose_overlay_sha256': sha256(output / 'compose.images.yaml'),
                    'reviewed_stage': {'manifest_sha256': manifest_hash, 'images_archive_sha256': stage_manifest['images_archive_sha256'],
                                       'images': stage_manifest['images'], 'web_echo_user_id': '@echo:localhost'},
                    'public_web': {'build_inputs': build_inputs, 'configuration_sha256': config_hash, 'bases': bases,
                                   'command': command, 'source_changes': ['Dockerfile FROM references pinned to reviewed cached digests'],
                                   'configuration_change': {'VITE_ECHO_USER_ID': {'from': '@echo:localhost', 'to': ECHO}}}}
        # A completed manifest is written last. Failed preparations cannot be
        # mistaken for a complete bundle by a future reviewed activation tool.
        (output / 'production-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        return {'operation': 'prepare_public_bundle', 'revision': revision, 'manifest_sha256': sha256(output / 'production-manifest.json'),
                'public_web': images['gateway'], 'activation_supported': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage-bundle', type=Path, required=True)
    parser.add_argument('--stage-manifest-sha256', required=True)
    parser.add_argument('--native-runtime', type=Path, required=True)
    parser.add_argument('--web-bases', type=Path, required=True)
    parser.add_argument('--web-bases-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--exclusive-builder', action='store_true', help='Operator attests the native amd64 Docker builder is idle and exclusively assigned.')
    args = parser.parse_args()
    if not args.exclusive_builder:
        parser.error('An explicitly assigned, idle native amd64 builder is required.')
    os.umask(0o077)
    try:
        result = prepare(args.stage_bundle.resolve(), args.stage_manifest_sha256, args.native_runtime.resolve(),
                         args.web_bases.resolve(), args.web_bases_sha256, args.output.resolve())
    except Exception as error:
        # Build output may include source paths. Surface a classified failure,
        # never an arbitrary subprocess body or operator environment value.
        code = str(error) if isinstance(error, ValueError) and re.fullmatch(r'[A-Z0-9_]+', str(error)) else type(error).__name__
        print(json.dumps({'operation': 'prepare_public_bundle', 'ok': False, 'error': code}))
        raise SystemExit(1) from None
    print(json.dumps(result))


if __name__ == '__main__':
    main()
