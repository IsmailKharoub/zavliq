#!/usr/bin/env python3
"""Restore reviewed production state on a fresh replacement host; never provision identities."""
import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
from types import SimpleNamespace

import activate as act

INPUT_SCHEMA = 'zavliq.production-restore-input.v1'
VERIFY_SCHEMA = 'zavliq.production-restore-verification.v1'
MAX_BYTES = 80 * 1024**3
SECRET_NAMES = ('postgres_password', 'registration_secret', 'policy_secret', 'control_secret')
REQUIRED = ('postgres.dump', 'created_at', 'synapse/homeserver.yaml', 'synapse/signing.key',
            'synapse/policy.sqlite', 'control/control.sqlite', 'bootstrap/admin_token',
            'secrets/compose.env', *(f'secrets/{name}' for name in SECRET_NAMES),
            'echo/runtime/identity.json', 'echo/runtime/inbox.sqlite3',
            'echo/runtime/matrix/matrix-sdk-crypto.sqlite3', 'echo/runtime/matrix/matrix-sdk-state.sqlite3',
            'echo/journal/echo.sqlite3')
ATTESTATIONS = ('source_writers_and_echo_fenced', 'canonical_route_to_replacement',
                'public_https_restricted_to_verifiers', 'dns_tls_decisions_reviewed',
                'original_devices_used_without_origin_or_key_changes',
                'old_e2ee_text_json_file_verified', 'new_e2ee_roundtrip_verified',
                'standard_message_and_file_verified', 'memberships_policy_quotas_verified',
                'restored_echo_identity_and_reply_verified', 'no_identity_or_secret_regeneration',
                'replacement_monitor_backup_access_reviewed')


def receipt(path, digest):
    act.private_file(path)
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('RESTORE_RECEIPT_TOO_LARGE')
    act.bundle.require_hash(path, digest)
    return act.bundle.json_file(path)


def machine_id():
    value = Path('/etc/machine-id').read_text().strip()
    if not re.fullmatch(r'[0-9a-f]{32}', value):
        raise ValueError('EXPLICIT_REPLACEMENT_MACHINE_ID_REQUIRED')
    return value


def record_path(paths, restore_id):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{7,63}', restore_id):
        raise ValueError('UNIQUE_RESTORE_ID_REQUIRED')
    return paths.evidence / ('restore-' + restore_id) / 'restore.json'


def digest(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value)


def validate_input(value, restore_id, release, manifest_hash, manifest):
    expected_keys = {'schema', 'restore_id', 'target_machine_id', 'bundle_id', 'manifest_sha256', 'revision', 'images',
                     'server_name', 'origin', 'source_binding_reviewed', 'primary_identity_off_host',
                     'transport_key_single_use', 'fresh_host_isolated', 'primary_recipient', 'transport_recipient',
                     'original', 'transport', 'plaintext'}
    if (set(value) != expected_keys or value.get('schema') != INPUT_SCHEMA or value.get('restore_id') != restore_id
            or value.get('target_machine_id') != machine_id()
            or value.get('bundle_id') != release.name or value.get('manifest_sha256') != manifest_hash
            or value.get('revision') != manifest['revision'] or value.get('images') != manifest['images']
            or value.get('server_name') != 'zavliq.com' or value.get('origin') != act.bundle.ORIGIN):
        raise ValueError('REVIEWED_BACKUP_BUNDLE_TARGET_BINDING_REQUIRED')
    for key in ('source_binding_reviewed', 'primary_identity_off_host', 'transport_key_single_use', 'fresh_host_isolated'):
        if value.get(key) is not True:
            raise ValueError('EXPLICIT_OPERATOR_TRANSPORT_ISOLATION_ATTESTATION_REQUIRED')
    for key in ('original', 'transport', 'plaintext'):
        item = value.get(key, {})
        keys = {'sha256', 'bytes', 'name', 'created_at'} if key == 'original' else {'sha256', 'bytes'}
        if not isinstance(item, dict) or set(item) != keys:
            raise ValueError('EXACT_CONTENT_FREE_RECEIPT_FIELDS_REQUIRED')
        if not digest(item.get('sha256')) or type(item.get('bytes')) is not int or not 0 < item['bytes'] <= MAX_BYTES:
            raise ValueError('BOUNDED_ORIGINAL_TRANSPORT_PLAINTEXT_HASHES_REQUIRED')
    if not re.fullmatch(r'zavliq-[0-9]{8}T[0-9]{6}Z\.tar\.age', value['original'].get('name', '')):
        raise ValueError('EXACT_ORIGINAL_BACKUP_NAME_REQUIRED')
    for key in ('primary_recipient', 'transport_recipient'):
        if not re.fullmatch(r'age1[0-9a-z]{58}', value.get(key, '')):
            raise ValueError('EXPLICIT_AGE_PUBLIC_RECIPIENTS_REQUIRED')
    if value['primary_recipient'] == value['transport_recipient']:
        raise ValueError('PRIMARY_RECOVERY_IDENTITY_MUST_STAY_OFF_HOST')


def verify_file(path, item):
    act.private_file(path)
    if path.stat().st_size != item['bytes']:
        raise ValueError('RESTORE_INPUT_SIZE_MISMATCH')
    act.bundle.require_hash(path, item['sha256'])


@contextmanager
def decrypt_transport(transport, key, value):
    verify_file(transport, value['transport'])
    act.private_file(key)
    if key == transport or key.stat().st_size > 4096:
        raise ValueError('SINGLE_USE_TRANSPORT_IDENTITY_REQUIRED')
    # Inspect only the derived public recipient. An unrelated/primary identity
    # is refused and never deleted; the matched one-use transport key is consumed.
    public = act.command(['age-keygen', '-y', str(key)]).stdout.strip()
    if public != value['transport_recipient'] or public == value['primary_recipient']:
        raise ValueError('MATCHING_NONPRIMARY_TRANSPORT_IDENTITY_REQUIRED')
    try:
        with tempfile.TemporaryDirectory(prefix='zavliq-private-restore-') as temporary:
            plain = Path(temporary) / 'snapshot.tar'
            act.command(['age', '--decrypt', '--identity', str(key), '--output', str(plain), str(transport)], timeout=600)
            plain.chmod(0o600)
            verify_file(plain, value['plaintext'])
            key.unlink()  # The transport identity is unnecessary after decryption.
            yield plain, Path(temporary)
    finally:
        key.unlink(missing_ok=True)  # No promise of forensic erasure on the underlying storage.


def extract_snapshot(archive, destination, maximum=MAX_BYTES):
    destination.mkdir(mode=0o700)
    seen, total = set(), 0
    with tarfile.open(archive, 'r:') as source:
        for member in source:
            path = PurePosixPath(member.name)
            name = str(path)
            if (path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir())
                    or name in seen or len(seen) >= 200000
                    or (name != '.' and path.parts[0] not in {'postgres.dump', 'created_at', 'synapse', 'control', 'bootstrap', 'secrets', 'echo'})):
                raise ValueError('UNSAFE_OR_DUPLICATE_SNAPSHOT_MEMBER')
            seen.add(name)
            total += member.size
            if total > maximum:
                raise ValueError('SNAPSHOT_EXPANSION_LIMIT')
            source.extract(member, destination, filter='data')
    # All snapshot material is private, including implicit parent directories.
    for path in destination.rglob('*'):
        path.chmod(0o700 if path.is_dir() else 0o600)
    destination.chmod(0o700)
    for name in REQUIRED:
        path = destination / name
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError('COMPLETE_ORIGINAL_IDENTITY_AND_ECHO_STATE_REQUIRED')
    with (destination / 'postgres.dump').open('rb') as stream:
        if stream.read(5) != b'PGDMP':
            raise ValueError('POSTGRES_CUSTOM_DUMP_REQUIRED')
    return destination


def validate_snapshot(snapshot, value, paths):
    source = act.load_environment(snapshot / 'secrets/compose.env')
    # Only canonical public backups can enter this path, never localhost stage.
    expected = {'COMPOSE_PROJECT_NAME': act.PROJECT, 'ZAVLIQ_STATE_DIR': '/etc/zavliq',
                'ZAVLIQ_SERVER_NAME': 'zavliq.com', 'ZAVLIQ_PUBLIC_URL': act.bundle.ORIGIN,
                'ZAVLIQ_SITE_ADDRESS': 'zavliq.com', 'ZAVLIQ_RELEASE': value['revision']}
    if any(source.get(key) != wanted for key, wanted in expected.items()) or set(source) - act.ENV_KEYS:
        raise ValueError('ORIGINAL_PUBLIC_NAMESPACE_AND_RELEASE_REQUIRED')
    values = {**source, 'ZAVLIQ_STATE_DIR': str(paths.state)}
    act.verify_namespace(values, paths)
    timestamp = (snapshot / 'created_at').read_text().strip()
    if timestamp != value['original'].get('created_at'):
        raise ValueError('SNAPSHOT_TIME_BINDING_REQUIRED')
    try:
        created = dt.datetime.strptime(timestamp, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc).timestamp()
    except ValueError:
        raise ValueError('SNAPSHOT_TIME_REQUIRED') from None
    if created > time.time() + 300:
        raise ValueError('SNAPSHOT_TIME_IN_FUTURE')
    return values, created


def fresh_host(paths, output):
    act.require_fresh_project(paths)
    if paths.current.with_name('current.next').exists() or paths.current.with_name('current.next').is_symlink():
        raise ValueError('UNRESOLVED_CURRENT_POINTER_REQUIRES_REVIEW')
    if output.parent.exists() or any(paths.evidence.glob('restore-*')):
        raise ValueError('PREVIOUS_RESTORE_STATE_REQUIRES_REVIEW')
    if paths.state.is_symlink() or not paths.state.is_dir() or set(p.name for p in paths.state.iterdir()) != {'operations.env'}:
        raise ValueError('FRESH_STATE_WITH_ONLY_REVIEWED_OPERATIONS_ENV_REQUIRED')
    info = paths.state.stat()
    if info.st_uid != act.ROOT_OWNER or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError('PRIVATE_PRODUCTION_STATE_DIRECTORY_REQUIRED')
    # A dedicated replacement daemon avoids shared-project/volume adoption.
    for args in (['docker', 'ps', '--all', '--quiet'], ['docker', 'volume', 'ls', '--quiet']):
        if act.command(args).stdout.strip():
            raise ValueError('EMPTY_REPLACEMENT_DAEMON_REQUIRED')
    networks = set(act.command(['docker', 'network', 'ls', '--format', '{{.Name}}']).stdout.split())
    if networks - {'bridge', 'host', 'none'}:
        raise ValueError('EMPTY_REPLACEMENT_DAEMON_REQUIRED')
    if act.command(['docker', 'info', '--format', '{{.OSType}}/{{.Architecture}}']).stdout.strip() not in {'linux/amd64', 'linux/x86_64'}:
        raise ValueError('NATIVE_AMD64_PRODUCTION_HOST_REQUIRED')
    if act.command(['docker', 'context', 'show']).stdout.strip() != 'default':
        raise ValueError('DEFAULT_REPLACEMENT_DOCKER_CONTEXT_REQUIRED')


def stream_command(args, source, values, timeout=600):
    with source.open('rb') as stream:
        subprocess.run(args, stdin=stream, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       check=True, timeout=timeout, env=act.child_environment(values))


def stop_writers(paths, release, values, report):
    act.stop_operations(report)
    try:
        # Use an independent verified temporary env even if the persistent env or
        # active marker write failed. Cleanup must not depend on those writes.
        with tempfile.TemporaryDirectory(prefix='zavliq-restore-stop-') as temporary:
            env = Path(temporary) / 'compose.env'
            act.atomic_write(env, ''.join(f'{key}={value}\n' for key, value in values.items()))
            view = SimpleNamespace(env=env)
            act.command(act.compose_args(view, release, '--profile', 'echo', 'stop', '--timeout', '30',
                                         'echo', 'gateway', 'control', 'synapse', 'postgres', include_website=False), timeout=165, env=values)
    except Exception: report['stop_incomplete'] = True


def fail(paths, release, values, output, report, error):
    report.update(phase='failed', ok=False, operator_recovery_required=True,
                  error=str(error) if isinstance(error, ValueError) and re.fullmatch(r'[A-Z0-9_]+', str(error)) else type(error).__name__)
    active = {'status': 'failed', 'bundle_id': release.name, 'manifest_sha256': report['manifest_sha256'],
              'previous': None, 'restore_id': report['restore_id']}
    try: act.write_json(paths.active, active)
    except Exception: report['marker_write_incomplete'] = True
    stop_writers(paths, release, values, report)
    try: act.write_json(output, report)
    except Exception: pass  # Cleanup already ran; preserve the original failure.


def copy_volume(paths, release, manifest, snapshot, service, owner, temporary, values):
    # Fixed names and exact reviewed IDs, no bootstrap entrypoints or networking.
    volume = act.PROJECT + '_' + service
    existing = act.command(['docker', 'volume', 'ls', '--quiet', '--filter', 'name=' + volume]).stdout.split()
    if volume in existing:
        raise ValueError('RESTORE_VOLUME_COLLISION')
    act.command(['docker', 'volume', 'create', '--label', 'com.docker.compose.project=' + act.PROJECT,
                 '--label', 'com.docker.compose.volume=' + service, volume])
    archive = temporary / (service + '.tar')
    with tarfile.open(archive, 'w') as target:
        target.add(snapshot / service, arcname='.')
    image = manifest['images']['synapse' if service in {'synapse', 'bootstrap'} else 'control']['id']
    command = ['docker', 'run', '--rm', '-i', '--pull=never', '--network', 'none', '--user', '0:0',
               '--mount', f'type=volume,src={volume},dst=/restore', '--entrypoint', 'sh', image,
               '-c', 'for entry in /restore/* /restore/.[!.]* /restore/..?*; do '
               'if [ -e "$entry" ] || [ -L "$entry" ]; then exit 73; fi; done; '
               f'tar -xpf - -C /restore && chown -R {owner}:{owner} /restore && chmod 700 /restore']
    stream_command(command, archive, values)
    archive.unlink()


def prepare(paths, restore_id, bundle_id, manifest_hash, input_path, input_hash, transport, transport_key):
    output = record_path(paths, restore_id)
    release = act.release_path(paths, bundle_id)
    manifest = act.verify_bundle(release, manifest_hash)
    value = receipt(input_path, input_hash)
    validate_input(value, restore_id, release, manifest_hash, manifest)
    fresh_host(paths, output)
    operations = act.operations_environment(paths)
    if operations['ZAVLIQ_BACKUP_RECIPIENT'] != value['primary_recipient']:
        raise ValueError('REVIEWED_PRIMARY_BACKUP_RECIPIENT_REQUIRED')
    needed = value['plaintext']['bytes'] * 3 + (release / 'images.tar.gz').stat().st_size * 3 + 8 * 1024**3
    if shutil.disk_usage(paths.releases).free < needed:
        raise ValueError('RESTORE_DISK_HEADROOM_REQUIRED')
    with decrypt_transport(transport, transport_key, value) as (plain, temporary):
        snapshot = extract_snapshot(plain, temporary / 'snapshot', value['plaintext']['bytes'])
        values, created = validate_snapshot(snapshot, value, paths)
        env = temporary / 'compose.env'
        act.atomic_write(env, ''.join(f'{key}={item}\n' for key, item in values.items()))
        view = SimpleNamespace(env=env)
        composed = json.loads(act.command(act.compose_args(view, release, '--profile', '*', 'config', '--format', 'json', include_website=False), env=values).stdout)
        act.verify_composed(composed, manifest, paths)
        for image in manifest['images'].values():
            cached = set(act.command(['docker', 'image', 'ls', '--quiet', '--no-trunc', '--filter', 'reference=' + image['ref']]).stdout.split())
            if cached - {image['id']}:
                raise ValueError('EXISTING_IMAGE_TAG_COLLISION')
        output.parent.mkdir(parents=True, mode=0o700)
        report = {'operation': 'restore_production', 'restore_id': restore_id, 'phase': 'restoring', 'ok': False,
                  'bundle_id': bundle_id, 'manifest_sha256': manifest_hash, 'input_receipt_sha256': input_hash,
                  'target_machine_id': value['target_machine_id'], 'original': value['original'],
                  'transport': value['transport'], 'plaintext': value['plaintext'], 'images': manifest['images'],
                  'started_at': int(time.time()), 'snapshot_age_seconds': max(0, int(time.time() - created)),
                  'external_isolation_operator_attested': True, 'canonical_https_checked': False,
                  'original_archive_binding_operator_attested': True, 'transport_and_plaintext_hashes_verified': True}
        act.write_json(output, report)
        try:
            act.command(['docker', 'load', '-i', str(release / 'images.tar.gz')], timeout=600)
            act.require_images(manifest)
            act.write_json(paths.active, {'status': 'activating', 'bundle_id': bundle_id,
                                         'manifest_sha256': manifest_hash, 'previous': None, 'restore_id': restore_id})
            for name in SECRET_NAMES:
                target = paths.state / name
                shutil.copyfile(snapshot / 'secrets' / name, target)
                target.chmod(0o600)
            act.verify_secrets(paths)
            act.atomic_write(paths.env, env.read_text())
            pointer = paths.current.with_name('current.next')
            pointer.symlink_to(release)
            pointer.replace(paths.current)
            # These fixed volumes must be seeded before any dependency starts.
            for service, owner in [('synapse', 991), ('control', 1000), ('bootstrap', 0), ('echo', 1000)]:
                copy_volume(paths, release, manifest, snapshot, service, owner, temporary, values)
            up = ['up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '180']
            act.run_compose(paths, release, *up, 'postgres', timeout=210)
            stream_command(act.compose_args(paths, release, 'exec', '-T', 'postgres', 'pg_restore', '-U', 'zavliq', '--list'), snapshot / 'postgres.dump', values)
            stream_command(act.compose_args(paths, release, 'exec', '-T', 'postgres', 'pg_restore', '-U', 'zavliq', '--clean', '--if-exists', '--exit-on-error', '--dbname=synapse'), snapshot / 'postgres.dump', values)
            # Existing same-source config/bootstrap dependencies verify the
            # restored signing configuration and token; absence failed above.
            act.run_compose(paths, release, *up, 'synapse', 'control', timeout=210)
            act.install_operations(paths, release)
            for timer in act.TIMERS: act.command(['systemctl', 'stop', timer])
            require_core(paths, release, manifest)
            report.update(phase='restored_private', ok=True, prepared_at=int(time.time()),
                          core_images_healthy=True, gateway_started=False, echo_started=False,
                          schedules_enabled=False, launch_verified=False)
            act.write_json(output, report)
        except Exception as error:
            fail(paths, release, values, output, report, error)
            raise
    return report


def require_core(paths, release, manifest):
    for name in ('postgres', 'synapse', 'control'):
        ids = act.run_compose(paths, release, 'ps', '--quiet', name).stdout.split()
        if len(ids) != 1:
            raise ValueError('ONE_RESTORED_CORE_CONTAINER_REQUIRED')
        wanted = manifest['images'][name]['id'] + '|true|false|healthy'
        actual = act.command(['docker', 'inspect', '--format', '{{.Image}}|{{.State.Running}}|{{.State.Paused}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}', ids[0]]).stdout.strip()
        if actual != wanted:
            raise ValueError('RESTORED_CORE_IMAGE_OR_HEALTH_MISMATCH')


def validate_verification(value, report, record_hash):
    now = time.time()
    keys = {'schema', 'restore_id', 'restore_record_sha256', 'target_machine_id', 'manifest_sha256',
            'origin', 'verified_at', 'expires_at', 'operator_attestations'}
    if (set(value) != keys or value.get('schema') != VERIFY_SCHEMA or value.get('restore_id') != report['restore_id']
            or value.get('restore_record_sha256') != record_hash
            or value.get('target_machine_id') != machine_id() or value.get('target_machine_id') != report['target_machine_id']
            or value.get('manifest_sha256') != report['manifest_sha256'] or value.get('origin') != act.bundle.ORIGIN
            or type(value.get('verified_at')) is not int or type(value.get('expires_at')) is not int
            or not report['prepared_at'] <= value['verified_at'] <= now + 60
            or not now < value['expires_at'] <= value['verified_at'] + 3600
            or set(value.get('operator_attestations', {})) != set(ATTESTATIONS)
            or any(value.get('operator_attestations', {}).get(key) is not True for key in ATTESTATIONS)):
        raise ValueError('CURRENT_SAME_ORIGIN_FENCING_AND_CLIENT_VERIFICATION_RECEIPT_REQUIRED')


def complete(paths, restore_id, expected_record_hash, verification_path, verification_hash):
    output = record_path(paths, restore_id)
    report = receipt(output, expected_record_hash)
    if report.get('phase') != 'restored_private' or report.get('restore_id') != restore_id:
        raise ValueError('UNCONSUMED_PRIVATE_RESTORE_REQUIRED')
    verification = receipt(verification_path, verification_hash)
    validate_verification(verification, report, expected_record_hash)
    active, release, manifest = act.runtime_record(paths)
    if active.get('status') != 'activating' or active.get('restore_id') != restore_id or active['manifest_sha256'] != report['manifest_sha256'] or active['bundle_id'] != report['bundle_id']:
        raise ValueError('EXACT_RESTORE_ADOPTION_STATE_REQUIRED')
    act.verify_bundle(release, report['manifest_sha256'])
    values = act.load_environment(paths.env)
    act.verify_secrets(paths)
    act.operations_environment(paths)
    composed = json.loads(act.run_compose(paths, release, '--profile', '*', 'config', '--format', 'json').stdout)
    act.verify_composed(composed, manifest, paths)
    report.update(phase='completing', ok=False, verification_receipt_sha256=verification_hash)
    try:
        act.write_json(output, report)
        act.require_images(manifest)
        act.require_running(paths, release, manifest)
        act.public_readiness()
        report['post_restore_backup'] = act.run_backup(paths)
        active['status'] = 'ready'
        act.write_json(paths.active, active)
        act.command(['systemctl', 'enable', '--now', *act.TIMERS])
        report.update(phase='complete', ok=True, completed_at=int(time.time()),
                      local_running_images_healthy=True, canonical_https_checked=True,
                      external_fencing_route_and_clients_operator_attested=True,
                      public_traffic_opened_by_tool=False, off_host_backup_verified=False,
                      launch_verified=False)
        act.write_json(output, report)
    except Exception as error:
        fail(paths, release, values, output, report, error)
        raise
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    restore = actions.add_parser('prepare')
    restore.add_argument('--restore-id', required=True)
    restore.add_argument('--bundle-id', required=True)
    restore.add_argument('--manifest-sha256', required=True)
    restore.add_argument('--input-receipt', required=True, type=Path)
    restore.add_argument('--input-receipt-sha256', required=True)
    restore.add_argument('--transport', required=True, type=Path)
    restore.add_argument('--transport-key', required=True, type=Path)
    finish = actions.add_parser('complete')
    finish.add_argument('--restore-id', required=True)
    finish.add_argument('--expected-restore-record-sha256', required=True)
    finish.add_argument('--verification-receipt', required=True, type=Path)
    finish.add_argument('--verification-receipt-sha256', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0 or Path(__file__).resolve() != act.TOOLS / 'restore.py':
        parser.error('Use the reviewed root-owned /opt/zavliq/production-tools installation on the replacement host.')
    for name in ('restore.py', 'activate.py', 'prepare_bundle.py', 'website_state.py', 'website.py'):
        info = (act.TOOLS / name).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            parser.error('Production tools must be root-owned and protected.')
    os.umask(0o077)
    try:
        with Path('/run/lock/zavliq-deploy.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            paths = act.Paths()
            if args.action == 'prepare':
                result = prepare(paths, args.restore_id, args.bundle_id, args.manifest_sha256,
                                 args.input_receipt, args.input_receipt_sha256, args.transport, args.transport_key)
            else:
                result = complete(paths, args.restore_id, args.expected_restore_record_sha256,
                                  args.verification_receipt, args.verification_receipt_sha256)
        print(json.dumps(result))
    except Exception as error:
        code = str(error) if isinstance(error, ValueError) and re.fullmatch(r'[A-Z0-9_]+', str(error)) else type(error).__name__
        print(json.dumps({'operation': 'restore_' + args.action, 'ok': False, 'error': code}))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
