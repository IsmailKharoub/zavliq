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
    """Verify pinned OCI graphs or classic config IDs in one streaming archive pass.

    OCI descriptors bind compressed blobs, while rootfs.diff_ids bind unpacked
    layers. Docker 29's containerd store can report an index digest as image ID.
    https://github.com/opencontainers/image-spec/blob/main/image-layout.md
    https://docs.docker.com/engine/storage/containerd/
    """
    import base64
    import zlib
    index_types = {'application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json'}
    manifest_types = {'application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json'}
    config_types = {'application/vnd.oci.image.config.v1+json', 'application/vnd.docker.container.image.v1+json'}
    layer_types = {'application/vnd.oci.image.layer.v1.tar', 'application/vnd.oci.image.layer.v1.tar+gzip',
                   'application/vnd.docker.image.rootfs.diff.tar.gzip'}
    expected = {image['ref']: image['id'] for image in images.values()}
    if len(expected) != len(images):
        raise ValueError('EXACT_DOCKER_ARCHIVE_IMAGES_REQUIRED')
    records, json_bytes, seen, total, cached, inflated_total = {}, {}, set(), 0, 0, 0
    # Hash every present blob once, including optional non-selected content.
    # Cache only small JSON documents; never retain/extract layer payloads.
    with tarfile.open(path, 'r|gz') as archive:
        for member in archive:
            name = str(PurePosixPath(member.name))
            if PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts or not (member.isfile() or member.isdir()):
                raise ValueError('DOCKER_ARCHIVE_MEMBER_REFUSED')
            if name in seen:
                raise ValueError('DUPLICATE_DOCKER_ARCHIVE_MEMBER')
            seen.add(name)
            if len(seen) > 10000 or member.size > 8 * 1024 ** 3:
                raise ValueError('DOCKER_ARCHIVE_SIZE_REFUSED')
            if member.isdir():
                if member.size != 0:
                    raise ValueError('DOCKER_ARCHIVE_MEMBER_REFUSED')
                continue
            total += member.size
            if total > 24 * 1024 ** 3:
                raise ValueError('DOCKER_ARCHIVE_SIZE_REFUSED')
            raw_hash, diff_hash, count, expanded = hashlib.sha256(), hashlib.sha256(), 0, 0
            small = bytearray() if member.size <= 2 * 1024 * 1024 else None
            decoder, compressed = None, None
            stream = archive.extractfile(member)
            while chunk := stream.read(1024 * 1024):
                if compressed is None:
                    compressed = chunk.startswith(b'\x1f\x8b')
                    if compressed:
                        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                raw_hash.update(chunk)
                count += len(chunk)
                if small is not None:
                    small.extend(chunk)
                if decoder is None:
                    diff_hash.update(chunk)
                else:
                    pending = chunk
                    while pending:
                        data = decoder.decompress(pending, 1024 * 1024)
                        expanded += len(data)
                        inflated_total += len(data)
                        if expanded > 8 * 1024 ** 3 or inflated_total > 32 * 1024 ** 3 or decoder.unused_data:
                            raise ValueError('DOCKER_LAYER_ENCODING_REFUSED')
                        diff_hash.update(data)
                        pending = decoder.unconsumed_tail
            if count != member.size or (decoder is not None and not decoder.eof):
                raise ValueError('DOCKER_ARCHIVE_BLOB_TRUNCATED')
            digest = 'sha256:' + raw_hash.hexdigest()
            if name.startswith('blobs/') and name != 'blobs/sha256/' + raw_hash.hexdigest():
                raise ValueError('OCI_BLOB_DIGEST_MISMATCH')
            records[name] = {'digest': digest, 'size': count, 'diff_id': 'sha256:' + diff_hash.hexdigest(), 'gzip': bool(compressed)}
            if small is not None and small.lstrip().startswith((b'{', b'[')):
                cached += len(small)
                if cached > 32 * 1024 * 1024:
                    raise ValueError('DOCKER_ARCHIVE_METADATA_TOO_LARGE')
                json_bytes[name] = bytes(small)

    def no_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('DUPLICATE_DOCKER_JSON_KEY')
            result[key] = value
        return result

    documents = {}
    def document(name):
        if name not in documents:
            if name not in json_bytes:
                raise ValueError('DOCKER_ARCHIVE_JSON_REQUIRED')
            documents[name] = json.loads(json_bytes[name], object_pairs_hook=no_duplicates)
        return documents[name]

    def descriptor(value, required=True):
        if (not isinstance(value, dict) or not re.fullmatch(r'sha256:[0-9a-f]{64}', value.get('digest', ''))
                or type(value.get('size')) is not int or value['size'] < 0 or not isinstance(value.get('mediaType'), str)):
            raise ValueError('OCI_DESCRIPTOR_REFUSED')
        name = 'blobs/sha256/' + value['digest'][7:]
        if 'data' in value:
            inline = value['data']
            if not isinstance(inline, str) or len(inline) > 3 * 1024 * 1024:
                raise ValueError('OCI_INLINE_DATA_REFUSED')
            data = base64.b64decode(inline, validate=True)
            if len(data) != value['size'] or 'sha256:' + hashlib.sha256(data).hexdigest() != value['digest']:
                raise ValueError('OCI_INLINE_DATA_MISMATCH')
            # OCI empty artifact configs may be supplied only as inline data.
            if name not in records and value['mediaType'] == 'application/vnd.oci.empty.v1+json' and data == b'{}':
                records[name] = {'digest': value['digest'], 'size': 2, 'diff_id': value['digest'], 'gzip': False}
                json_bytes[name] = data
        if name not in records:
            if required:
                raise ValueError('OCI_REQUIRED_BLOB_MISSING')
            return None
        if records[name]['size'] != value['size'] or records[name]['digest'] != value['digest']:
            raise ValueError('OCI_DESCRIPTOR_BYTES_MISMATCH')
        return name

    def config_and_layers(config_name, layer_names, ref):
        config = document(config_name)
        if config.get('os') != 'linux' or config.get('architecture') != 'amd64':
            raise ValueError('LINUX_AMD64_IMAGE_REQUIRED')
        rootfs = config.get('rootfs', {})
        if rootfs.get('type') != 'layers' or rootfs.get('diff_ids') != [records[n]['diff_id'] for n in layer_names]:
            raise ValueError('DOCKER_LAYER_DIFF_ID_MISMATCH')
        if 'echo' in images and ref == images['echo']['ref']:
            labels = (config.get('config') or {}).get('Labels') or {}
            if labels.get('org.opencontainers.image.revision') != runtime['head_sha'] or labels.get('com.zavliq.native.sha256') != runtime['binary_sha256']:
                raise ValueError('ECHO_RUNTIME_PROVENANCE_MISMATCH')

    legacy = document('manifest.json')
    if not isinstance(legacy, list):
        raise ValueError('DOCKER_ARCHIVE_MANIFEST_REFUSED')
    tagged = {}
    for item in legacy:
        config_name, layers = item.get('Config'), item.get('Layers')
        if not isinstance(config_name, str) or not isinstance(layers, list) or any(not isinstance(n, str) for n in layers):
            raise ValueError('DOCKER_ARCHIVE_MANIFEST_REFUSED')
        if any(n not in records for n in [config_name, *layers]):
            raise ValueError('DOCKER_CONFIG_OR_LAYER_MISSING')
        tags = item.get('RepoTags') or []
        if not isinstance(tags, list):
            raise ValueError('DOCKER_ARCHIVE_TAG_REFUSED')
        for ref in tags:
            if ref not in expected or ref in tagged:
                raise ValueError('DOCKER_ARCHIVE_IMAGE_PIN_MISMATCH')
            tagged[ref] = (config_name, layers)
    if set(tagged) != set(expected):
        raise ValueError('EXACT_DOCKER_ARCHIVE_IMAGES_REQUIRED')

    if 'index.json' not in records and 'oci-layout' not in records:
        if len(legacy) != len(images) or any(not item.get('RepoTags') for item in legacy):
            raise ValueError('EXACT_DOCKER_ARCHIVE_IMAGES_REQUIRED')
        for ref, (config, layers) in tagged.items():
            if records[config]['digest'] != expected[ref]:
                raise ValueError('DOCKER_ARCHIVE_IMAGE_PIN_MISMATCH')
            config_and_layers(config, layers, ref)
        return
    if document('oci-layout') != {'imageLayoutVersion': '1.0.0'}:
        raise ValueError('OCI_LAYOUT_REFUSED')
    root = document('index.json')
    if root.get('schemaVersion') != 2 or root.get('mediaType') not in index_types or not isinstance(root.get('manifests'), list):
        raise ValueError('OCI_INDEX_REFUSED')

    # Validate every available descriptor edge, even on non-selected platforms.
    walked = set()
    def walk(value, required=True, depth=0):
        if depth > 8:
            raise ValueError('OCI_GRAPH_DEPTH_REFUSED')
        name = descriptor(value, required)
        if name is None or name in walked:
            return
        walked.add(name)
        kind = value['mediaType']
        if kind not in index_types | manifest_types:
            return
        node = document(name)
        if node.get('schemaVersion') != 2 or node.get('mediaType', kind) != kind:
            raise ValueError('OCI_DOCUMENT_TYPE_MISMATCH')
        if kind in index_types:
            if not isinstance(node.get('manifests'), list):
                raise ValueError('OCI_INDEX_REFUSED')
            for child in node['manifests']:
                walk(child, False, depth + 1)
        else:
            descriptor(node.get('config'), False)
            if not isinstance(node.get('layers'), list):
                raise ValueError('OCI_MANIFEST_REFUSED')
            for layer in node['layers']:
                descriptor(layer, False)
        if 'subject' in node:
            descriptor(node['subject'], False)

    def select(value, depth=0):
        if depth > 8:
            raise ValueError('OCI_GRAPH_DEPTH_REFUSED')
        platform = value.get('platform')
        if platform is not None and (platform.get('os'), platform.get('architecture')) != ('linux', 'amd64'):
            return []
        if (value.get('annotations') or {}).get('vnd.docker.reference.type') == 'attestation-manifest':
            return []
        name = descriptor(value)
        node, kind = document(name), value['mediaType']
        if kind in index_types:
            return [leaf for child in node['manifests'] for leaf in select(child, depth + 1)]
        if kind not in manifest_types or node['config']['mediaType'] not in config_types:
            raise ValueError('OCI_RUNNABLE_MANIFEST_REQUIRED')
        config_name = descriptor(node['config'])
        config = document(config_name)
        if (config.get('os'), config.get('architecture')) != ('linux', 'amd64'):
            raise ValueError('LINUX_AMD64_IMAGE_REQUIRED')
        layers = []
        for layer in node['layers']:
            if layer['mediaType'] not in layer_types:
                raise ValueError('OCI_LAYER_MEDIA_TYPE_REFUSED')
            layer_name = descriptor(layer)
            if records[layer_name]['gzip'] != layer['mediaType'].endswith('gzip'):
                raise ValueError('OCI_LAYER_ENCODING_MISMATCH')
            layers.append(layer_name)
        return [(value['digest'], config_name, layers)]

    roots, extras, selected = {}, [], set()
    canonical = {'docker.io/library/' + ref: ref for ref in expected}
    for value in root['manifests']:
        walk(value)
        annotations = value.get('annotations') or {}
        ref = canonical.get(annotations.get('io.containerd.image.name'))
        if ref is None:
            # Extra attestation roots may not carry an image tag/name.
            if 'io.containerd.image.name' in annotations or 'org.opencontainers.image.ref.name' in annotations:
                raise ValueError('DOCKER_ARCHIVE_IMAGE_PIN_MISMATCH')
            extras.append(value)
            continue
        if ref in roots or value['digest'] != expected[ref] or annotations.get('org.opencontainers.image.ref.name') != ref.rsplit(':', 1)[1]:
            raise ValueError('DOCKER_ARCHIVE_IMAGE_PIN_MISMATCH')
        leaves = select(value)
        if len(leaves) != 1:
            raise ValueError('EXACT_AMD64_MANIFEST_REQUIRED')
        leaf_id, config, layers = leaves[0]
        if tagged[ref] != (config, layers):
            raise ValueError('OCI_LEGACY_MAPPING_MISMATCH')
        config_and_layers(config, layers, ref)
        roots[ref] = value['digest']
        selected.add(leaf_id)
    if roots != expected:
        raise ValueError('EXACT_DOCKER_ARCHIVE_IMAGES_REQUIRED')
    for value in extras:
        subject = (value.get('annotations') or {}).get('io.containerd.manifest.subject')
        if subject not in selected or value['mediaType'] not in manifest_types:
            raise ValueError('OCI_UNRELATED_UNTAGGED_ROOT_REFUSED')
        node = document(descriptor(value))
        config = document(descriptor(node['config']))
        artifact = (node.get('artifactType') == 'application/vnd.docker.attestation.manifest.v1+json'
                    and node.get('subject', {}).get('digest') == subject
                    and node['config']['mediaType'] == 'application/vnd.oci.empty.v1+json' and config == {})
        compatible = (node['config']['mediaType'] in config_types
                      and (config.get('os'), config.get('architecture')) == ('unknown', 'unknown'))
        if not (artifact or compatible) or not node['layers']:
            raise ValueError('OCI_ATTESTATION_ROOT_REQUIRED')
        for layer in node['layers']:
            if layer['mediaType'] != 'application/vnd.in-toto+json':
                raise ValueError('OCI_ATTESTATION_ROOT_REQUIRED')
            descriptor(layer)
    # Any untagged legacy records must describe an already checked OCI manifest.
    graph_mappings = set()
    for name in walked:
        node = document(name)
        if isinstance(node, dict) and node.get('mediaType') in manifest_types:
            config = descriptor(node['config'], False)
            layer_names = [descriptor(layer, False) for layer in node['layers']]
            if config is not None and None not in layer_names:
                graph_mappings.add((config, tuple(layer_names)))
    for item in legacy:
        if not item.get('RepoTags') and (item['Config'], tuple(item['Layers'])) not in graph_mappings:
            raise ValueError('OCI_LEGACY_MAPPING_MISMATCH')


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
