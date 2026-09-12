#!/usr/bin/env python3
"""Package and activate a gateway-only website; never restart application writers."""
import argparse
import copy
import datetime as dt
import fcntl
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.request

import activate as app
import prepare_bundle as bundle
import website_state as state


def write_json(path, value):
    app.atomic_write(path, json.dumps(value, indent=2) + '\n')


def remove_marker(path):
    path.unlink(missing_ok=True)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def inspect_amd64(ref):
    root = json.loads(bundle.checked(['docker', 'image', 'inspect', '--format', '{{json .}}', ref]).stdout)
    value = json.loads(bundle.checked(['docker', 'image', 'inspect', '--platform', 'linux/amd64', '--format', '{{json .}}', ref]).stdout)
    state.require(value.get('Os') == 'linux' and value.get('Architecture') == 'amd64', 'EXACT_AMD64_WEBSITE_IMAGE_REQUIRED')
    # Docker's containerd store reports an index ID without --platform and a
    # selected manifest ID with it. Preserve the reviewed index pin separately.
    value['selected_manifest_id'] = value['Id']
    value['Id'] = root['Id']
    return value


def validate_bases(path, expected_hash):
    bundle.require_hash(path, expected_hash)
    value = bundle.json_file(path)
    state.require(value.get('schema') == 'zavliq.public-web-bases.v1' and value.get('architecture') == 'linux/amd64' and
                  set(value.get('images', {})) == set(bundle.BASE_REFS), 'REVIEWED_WEBSITE_BASES_REQUIRED')
    for name, item in value['images'].items():
        state.require(item.get('ref') == bundle.BASE_REFS[name] and
                      re.fullmatch(r'sha256:[0-9a-f]{64}', item.get('id', '')) is not None and
                      re.fullmatch(r'docker.io/library/' + name + r'@sha256:[0-9a-f]{64}', item.get('digest_ref', '')) is not None,
                      'EXACT_WEBSITE_BASE_PINS_REQUIRED')
        state.require(inspect_amd64(item['digest_ref'])['Id'] == item['id'], 'CACHED_WEBSITE_BASE_MISMATCH')
    return value


def dist_hashes(tar_bytes):
    result = {}
    state.require(len(tar_bytes) <= 128 * 1024**2, 'BOUNDED_WEBSITE_DIST_REQUIRED')
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as archive:
        for member in archive:
            name = member.name.removeprefix('./')
            if member.isdir():
                continue
            state.require(member.isfile() and not name.startswith('/') and '..' not in Path(name).parts and
                          name not in result and len(result) < 2000, 'REGULAR_UNIQUE_WEBSITE_FILES_REQUIRED')
            with archive.extractfile(member) as stream:
                result[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    state.require('index.html' in result and any(name.startswith('assets/') for name in result), 'BUILT_WEBSITE_REQUIRED')
    return result


def image_dist(image_id):
    return subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', '/bin/tar', image_id,
                           '-C', '/srv', '-cf', '-', '.'], check=True, capture_output=True, timeout=60).stdout


def prepare(args):
    source, output = args.source.resolve(), args.output.resolve()
    bundle.require_hash(source, args.source_sha256)
    bundle.require_hash(args.base_manifest, args.base_manifest_sha256)
    base = bundle.json_file(args.base_manifest)
    state.require(base.get('schema') == bundle.SCHEMA and set(base.get('images', {})) == bundle.SERVICES and
                  base.get('public_web', {}).get('build_inputs', {}).get('origin') == bundle.ORIGIN,
                  'REVIEWED_PUBLIC_BASE_MANIFEST_REQUIRED')
    state.require(re.fullmatch(r'[0-9a-f]{40}', args.revision) is not None and not output.exists(),
                  'FRESH_WEBSITE_OUTPUT_AND_EXACT_REVISION_REQUIRED')
    with tarfile.open(source, 'r:gz') as archive:
        state.require(archive.pax_headers.get('comment') == args.revision, 'GIT_ARCHIVE_REVISION_REQUIRED')
    context = bundle.checked(['docker', 'context', 'show']).stdout.strip()
    state.require(context in {'default', 'desktop-linux'}, 'LOCAL_DEFAULT_BUILDER_REQUIRED')
    daemon = bundle.checked(['docker', 'info', '--format', '{{.ID}}']).stdout.strip()
    state.require(daemon and bundle.checked(['docker', '--context', 'default', 'info', '--format', '{{.ID}}']).stdout.strip() == daemon,
                  'DEFAULT_BUILDER_MUST_SHARE_LOCAL_DAEMON')
    builder = bundle.checked(['docker', 'buildx', 'inspect', 'default']).stdout
    state.require(re.search(r'^Driver:\s+docker\s*$', builder, re.MULTILINE), 'LOCAL_DOCKER_BUILDER_REQUIRED')
    bases = validate_bases(args.web_bases, args.web_bases_sha256)
    previous = base['images']['gateway']
    state.require(inspect_amd64(previous['ref'])['Id'] == previous['id'], 'CACHED_ORIGINAL_GATEWAY_REQUIRED')
    previous_assets = {name: value for name, value in dist_hashes(image_dist(previous['id'])).items() if name.startswith('assets/')}
    output.mkdir(mode=0o700)
    report = {'operation': 'prepare_website', 'ok': False, 'started_at': int(time.time()), 'website_revision': args.revision}
    try:
        with tempfile.TemporaryDirectory(prefix='zavliq-website-source-') as temporary:
            root = Path(temporary)
            bundle.extract_source(source, root)
            dockerfile, _ = bundle.pinned_dockerfile(root, bases)
            original_from = 'FROM ' + bases['images']['caddy']['digest_ref']
            dockerfile.write_text(dockerfile.read_text().replace(original_from,
                'FROM ' + previous['ref'] + ' AS previous_public\n' + original_from +
                '\nCOPY --from=previous_public /srv/assets /srv/assets', 1))
            inputs = {'website_revision': args.revision, 'source_archive_sha256': args.source_sha256,
                      'base_manifest_sha256': args.base_manifest_sha256, 'web_bases_sha256': args.web_bases_sha256,
                      'dockerfile_sha256': bundle.sha256(dockerfile), 'caddyfile_sha256': bundle.sha256(root / 'infra/Caddyfile'),
                      'preparer_sha256': bundle.sha256(Path(__file__)), 'echo_user_id': bundle.ECHO, 'origin': bundle.ORIGIN,
                      'previous_gateway': previous}
            build_id = args.revision + '-web-' + hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()[:16]
            tag = 'zavliq-web:' + build_id
            state.require(not bundle.tagged_ids(tag), 'WEBSITE_TAG_ALREADY_EXISTS')
            report['website_id'] = build_id
            write_json(output / 'preparation.json', report)
            command = ['docker', 'buildx', 'build', '--builder', 'default', '--platform', 'linux/amd64', '--pull=false', '--load',
                       '-f', str(dockerfile), '-t', tag, '--build-arg', 'VITE_ECHO_USER_ID=' + bundle.ECHO,
                       '--label', 'org.opencontainers.image.revision=' + args.revision,
                       '--label', 'com.zavliq.website.base-manifest=' + args.base_manifest_sha256, str(root)]
            bundle.checked(command, timeout=900)
            image = inspect_amd64(tag)
            labels = image.get('Config', {}).get('Labels', {})
            state.require(labels.get('org.opencontainers.image.revision') == args.revision and
                          labels.get('com.zavliq.website.base-manifest') == args.base_manifest_sha256, 'WEBSITE_IMAGE_LABEL_MISMATCH')
            gateway = {'ref': tag, 'id': image['Id']}
            # These temporary image checks have no network, mounts or application state.
            caddy = bundle.checked(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', '/bin/cat', image['Id'], '/etc/caddy/Caddyfile']).stdout
            state.require(caddy.encode() == (root / 'infra/Caddyfile').read_bytes(), 'BUILT_CADDY_BYTES_MISMATCH')
            bundle.checked(['docker', 'run', '--rm', '--network', 'none', '--env', 'ZAVLIQ_SITE_ADDRESS=localhost',
                            '--env', 'ZAVLIQ_PUBLIC_URL=' + bundle.ORIGIN, '--entrypoint', 'caddy', image['Id'],
                            'validate', '--config', '/etc/caddy/Caddyfile', '--adapter', 'caddyfile'], timeout=30)
            hashes = dist_hashes(image_dist(image['Id']))
            state.require(all(hashes.get(name) == value for name, value in previous_assets.items()), 'EXISTING_BROWSER_ASSET_BYTES_CHANGED')
            state.require(inspect_amd64(previous['ref'])['Id'] == previous['id'], 'ORIGINAL_GATEWAY_PIN_CHANGED_DURING_BUILD')
            bundle.checked(['docker', 'save', '-o', str(output / 'website-image.tar'), tag], timeout=180)
            with (output / 'website-image.tar').open('rb') as raw, (output / 'website-image.tar.gz').open('wb') as dest:
                with gzip.GzipFile(fileobj=dest, mode='wb', mtime=0) as compressed:
                    shutil.copyfileobj(raw, compressed)
            (output / 'website-image.tar').unlink()
            bundle.verify_docker_archive(output / 'website-image.tar.gz', {'gateway': gateway}, {})
            for origin, filename in [(source, 'source.tar.gz'), (args.base_manifest, 'base-manifest.json'),
                                     (args.web_bases, 'web-bases.json'), (dockerfile, 'website.Dockerfile'),
                                     (root / 'infra/Caddyfile', 'Caddyfile'), (Path(__file__), 'preparer.py')]:
                shutil.copyfile(origin, output / filename)
            (output / 'compose.website.yaml').write_text(state.overlay(gateway['id']))
            manifest = {'schema': state.SCHEMA, 'architecture': 'linux/amd64', 'website_revision': args.revision,
                        'base_manifest_sha256': args.base_manifest_sha256, 'base_images': base['images'],
                        'origin': bundle.ORIGIN, 'echo_user_id': bundle.ECHO, 'build_inputs': inputs,
                        'gateway': gateway, 'dist_sha256': hashes, 'preserved_assets_sha256': previous_assets,
                        'images_archive_sha256': bundle.sha256(output / 'website-image.tar.gz'),
                        'compose_overlay_sha256': bundle.sha256(output / 'compose.website.yaml'),
                        'builder': {'context': context, 'default_builder_same_local_daemon': True, 'target': 'linux/amd64'}}
            write_json(output / 'website-manifest.json', manifest)
            report.update(ok=True, completed_at=int(time.time()), manifest_sha256=bundle.sha256(output / 'website-manifest.json'), gateway=gateway)
            return report
    finally:
        write_json(output / 'preparation.json', report)


def stats_readiness(timeout=5):
    opener = urllib.request.build_opener(app.NoRedirect())
    with opener.open(bundle.ORIGIN + '/_zavliq/stats', timeout=timeout) as response:
        body = response.read(16385)
        state.require(response.status == 200 and len(body) <= 16384 and response.headers.get('Cache-Control') == 'no-store',
                      'BOUNDED_UNCACHED_PUBLIC_STATS_REQUIRED')
        value = json.loads(body)
    state.require(value.get('schema') == 'zavliq-public-stats-v1', 'PUBLIC_STATS_SCHEMA_REQUIRED')
    stamp = dt.datetime.fromisoformat(value.get('as_of', '').replace('Z', '+00:00'))
    state.require(stamp.tzinfo is not None and -60 <= time.time() - stamp.timestamp() <= 900, 'FRESH_PUBLIC_STATS_REQUIRED')
    return {'as_of': value['as_of'], 'sha256': hashlib.sha256(body).hexdigest()}


def web_readiness(manifest):
    app.public_readiness()
    opener = urllib.request.build_opener(app.NoRedirect())
    with opener.open(bundle.ORIGIN + '/stats', timeout=5) as response:
        body = response.read(2 * 1024**2 + 1)
        state.require(response.status == 200 and len(body) <= 2 * 1024**2 and
                      hashlib.sha256(body).hexdigest() == manifest['dist_sha256'].get('stats.html', manifest['dist_sha256']['index.html']), 'NEW_WEBSITE_HTML_REQUIRED')
    return stats_readiness()


def backend_snapshot(paths, release):
    result = {}
    for service in sorted(bundle.SERVICES - {'gateway'}):
        ids = app.run_compose(paths, release, '--profile', 'echo', 'ps', '-q', service).stdout.split()
        state.require(len(ids) == 1, 'ONE_ORIGINAL_BACKEND_CONTAINER_REQUIRED')
        fields = '{"Id":{{json .Id}},"Image":{{json .Image}},"State":{"Running":{{json .State.Running}},"Paused":{{json .State.Paused}},"StartedAt":{{json .State.StartedAt}},"Health":{"Status":{{json .State.Health.Status}}}},"Mounts":{{json .Mounts}}}'
        value = json.loads(app.command(['docker', 'inspect', '--format', fields, ids[0]]).stdout)
        status = value['State']
        state.require(status['Running'] is True and status.get('Paused') is False and
                      status.get('Health', {}).get('Status') == 'healthy', 'HEALTHY_UNPAUSED_BACKENDS_REQUIRED')
        result[service] = {'container_id': value['Id'], 'image': value['Image'], 'started_at': status['StartedAt'],
                           'mounts': sorted((m['Type'], m.get('Name', ''), m['Destination'], m['RW']) for m in value['Mounts'])}
    return result


def verify_package(directory, expected_hash, base, base_hash):
    manifest = state.verify_release(directory, expected_hash, base, base_hash)
    inputs = manifest['build_inputs']
    checks = {'source.tar.gz': inputs['source_archive_sha256'], 'base-manifest.json': base_hash,
              'web-bases.json': inputs['web_bases_sha256'], 'website.Dockerfile': inputs['dockerfile_sha256'],
              'Caddyfile': inputs['caddyfile_sha256'], 'website-image.tar.gz': manifest['images_archive_sha256'],
              'preparer.py': inputs['preparer_sha256']}
    for filename, expected in checks.items():
        bundle.require_hash(state.protected(directory / filename), expected)
    bundle.verify_docker_archive(directory / 'website-image.tar.gz', {'gateway': manifest['gateway']}, {})
    return manifest


def gateway_up(paths, release, override=None):
    app.run_compose(paths, release, *(['-f', str(override)] if override else []),
                    'up', '-d', '--no-deps', '--no-build', '--wait', '--wait-timeout', '60', 'gateway', timeout=90)


def transition(paths, website_id, expected_hash, expected_base, expected_website, *, rollback=False):
    record, release, effective = app.runtime_record(paths)
    state.require(record['status'] == 'ready' and record['manifest_sha256'] == expected_base,
                  'EXPECTED_READY_PRODUCTION_BASE_REQUIRED')
    base = bundle.json_file(release / 'production-manifest.json')
    current = state.active(paths, release, base)
    state.require((current[0]['manifest_sha256'] if current else 'none') == expected_website, 'EXPECTED_CURRENT_WEBSITE_REQUIRED')
    interrupted = effective.get('website_status', 'ready') != 'ready'
    if interrupted:
        predecessor = current[0].get('previous')
        state.require(rollback and ((predecessor is None and website_id == 'base' and expected_hash == 'none') or
                      (predecessor is not None and predecessor == {'website_id': website_id, 'manifest_sha256': expected_hash})),
                      'INTERRUPTED_WEBSITE_REQUIRES_JOURNALED_ROLLBACK')
    before = backend_snapshot(paths, release)
    state.require(all(value['image'] == base['images'][name]['id'] for name, value in before.items()), 'EXACT_ORIGINAL_BACKEND_IMAGES_REQUIRED')
    if not interrupted:
        app.require_running(paths, release, effective)
    marker = paths.state / 'website-active.json'
    previous = copy.deepcopy(current[0]) if current else None
    selected = None
    if website_id != 'base':
        directory = paths.releases.parent / 'websites' / website_id
        selected = verify_package(directory, expected_hash, base, expected_base)
        state.require(rollback or not current or current[0]['website_id'] != website_id, 'DISTINCT_WEBSITE_TRANSITION_REQUIRED')
        if current and not rollback:
            state.require(all(selected['dist_sha256'].get(name) == value for name, value in current[2]['dist_sha256'].items()
                              if name.startswith('assets/')), 'CURRENT_BROWSER_ASSETS_MUST_SURVIVE_UPGRADE')
        # Validate the exact merged config before any image or container changes.
        args = app.compose_args(paths, release, '-f', str(directory / 'compose.website.yaml'), '--profile', '*', 'config', '--format', 'json')
        composed = json.loads(app.command(args, env=app.load_environment(paths.env)).stdout)
        candidate = copy.deepcopy(base); candidate['images']['gateway'] = selected['gateway']
        app.verify_composed(composed, candidate, paths)
        state.require(not (bundle.tagged_ids(selected['gateway']['ref']) - {selected['gateway']['id']}), 'WEBSITE_TAG_COLLISION')
        app.command(['docker', 'load', '-i', str(directory / 'website-image.tar.gz')], timeout=180)
        app.require_images(candidate)
    else:
        state.require(rollback and current is not None and expected_hash == 'none', 'BASE_ONLY_FOR_EXPLICIT_ROLLBACK')
        candidate = base
        app.require_images(candidate)
    if interrupted:
        ids = app.run_compose(paths, release, 'ps', '--all', '-q', 'gateway').stdout.split()
        state.require(len(ids) <= 1, 'ONE_OR_ZERO_INTERRUPTED_GATEWAY_REQUIRED')
        if ids:
            actual = app.command(['docker', 'inspect', '--format', '{{.Image}}', ids[0]]).stdout.strip()
            state.require(actual in {effective['images']['gateway']['id'], candidate['images']['gateway']['id'], base['images']['gateway']['id']},
                          'JOURNALED_GATEWAY_IMAGE_REQUIRED')
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + os.urandom(4).hex()
    evidence = paths.evidence / ('website-' + stamp + '.json')
    report = {'operation': 'website_rollback' if rollback else 'website_activate', 'ok': False, 'started_at': int(time.time()),
              'base_manifest_sha256': expected_base, 'website_id': website_id, 'website_manifest_sha256': expected_hash,
              'previous_website': previous, 'backends_before': before, 'recovering_interrupted_transition': interrupted}
    write_json(evidence, report)
    try:
        new_record = {'schema': state.ACTIVE_SCHEMA, 'status': 'activating', 'website_id': website_id,
                      'manifest_sha256': expected_hash, 'base_manifest_sha256': expected_base,
                      'previous': {'website_id': previous['website_id'], 'manifest_sha256': previous['manifest_sha256']} if previous else None}
        if rollback:
            # Keep the source website marker until the target is actually healthy.
            # Its journaled predecessor permits the same rollback after SIGKILL.
            pending = copy.deepcopy(previous)
            pending['status'] = 'activating'
            pending['previous'] = {'website_id': website_id, 'manifest_sha256': expected_hash} if selected else None
            write_json(marker, pending)
        else:
            write_json(marker, new_record)
        gateway_up(paths, release, (directory / 'compose.website.yaml' if selected else release / 'compose.images.yaml') if rollback else None)
        app.require_running(paths, release, candidate)
        report['public_checks'] = web_readiness(selected) if selected else (app.public_readiness() or {'base_website': True})
        state.require(backend_snapshot(paths, release) == before, 'ORIGINAL_BACKEND_CONTAINERS_CHANGED')
        if selected:
            new_record['status'] = 'ready'; write_json(marker, new_record)
        else:
            remove_marker(marker)
        report.update(ok=True, completed_at=int(time.time()), backend_containers_unchanged=True, gateway=candidate['images']['gateway'])
        return report
    except Exception as error:
        report['failure'] = str(error) if isinstance(error, ValueError) and re.fullmatch(r'[A-Z0-9_]+', str(error)) else type(error).__name__
        if interrupted:
            previous['status'] = 'failed'
            write_json(marker, previous)
            report['gateway_rollback_verified'] = False
            report['operator_gateway_recovery_required'] = True
            raise
        # Gateway-only rollback is safe: no application writer/image/volume transition occurs.
        try:
            pending = copy.deepcopy(new_record if selected else previous)
            pending['status'] = 'failed'
            pending['previous'] = {'website_id': previous['website_id'], 'manifest_sha256': previous['manifest_sha256']} if previous else None
            write_json(marker, pending)
            previous_overlay = paths.releases.parent / 'websites' / previous['website_id'] / 'compose.website.yaml' if previous else release / 'compose.images.yaml'
            gateway_up(paths, release, previous_overlay)
            app.require_running(paths, release, effective)
            app.public_readiness()
            state.require(backend_snapshot(paths, release) == before, 'ORIGINAL_BACKEND_CONTAINERS_CHANGED')
            if previous:
                write_json(marker, previous)
            else:
                remove_marker(marker)
            report['gateway_rollback_verified'] = True
        except Exception as rollback_error:
            report['gateway_rollback_verified'] = False
            report['rollback_error'] = type(rollback_error).__name__
        raise
    finally:
        write_json(evidence, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    build = commands.add_parser('prepare')
    for flag in ['source', 'base-manifest', 'web-bases', 'output']:
        build.add_argument('--' + flag, type=Path, required=True)
    for flag in ['source-sha256', 'base-manifest-sha256', 'web-bases-sha256', 'revision']:
        build.add_argument('--' + flag, required=True)
    build.add_argument('--exclusive-builder', action='store_true', required=True)
    for operation in ['activate', 'rollback']:
        deploy = commands.add_parser(operation)
        for flag in ['website-id', 'manifest-sha256', 'base-manifest-sha256', 'expected-current-website']:
            deploy.add_argument('--' + flag, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.operation == 'prepare':
            result = prepare(args)
        else:
            state.require(os.geteuid() == 0 and Path(__file__).resolve() == app.TOOLS / 'website.py', 'INSTALLED_ROOT_WEBSITE_TOOL_REQUIRED')
            for name in ['website.py', 'website_state.py', 'activate.py', 'prepare_bundle.py']:
                state.protected(app.TOOLS / name)
            with Path('/run/lock/zavliq-deploy.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = transition(app.Paths(), args.website_id, args.manifest_sha256, args.base_manifest_sha256,
                                    args.expected_current_website, rollback=args.operation == 'rollback')
        print(json.dumps(result))
    except Exception as error:
        code = str(error) if isinstance(error, ValueError) and re.fullmatch(r'[A-Z0-9_]+', str(error)) else type(error).__name__
        print(json.dumps({'ok': False, 'operation': args.operation, 'code': code, 'evidence_preserved': True}))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
