#!/usr/bin/env python3
"""Explicit production activation for reviewed public bundles; no build or cloud access."""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import urllib.request

import prepare_bundle as bundle

PROJECT = 'zavliq-production'
TOOLS = Path('/opt/zavliq/production-tools')
ROOT_OWNER = 0
ENV_KEYS = {'COMPOSE_PROJECT_NAME', 'ZAVLIQ_STATE_DIR', 'ZAVLIQ_SERVER_NAME', 'ZAVLIQ_PUBLIC_URL',
            'ZAVLIQ_SITE_ADDRESS', 'ZAVLIQ_RELEASE', 'ZAVLIQ_ECHO_HOMESERVER_URL', 'ZAVLIQ_ECHO_CONTROL_URL', 'VITE_ECHO_USER_ID'}
TIMERS = ['zavliq-backup.timer', 'zavliq-retention.timer', 'zavliq-health.timer']


class Paths:
    def __init__(self, root=Path('/')):
        self.releases = root / 'opt/zavliq/releases'
        self.current = root / 'opt/zavliq/current'
        self.state = root / 'etc/zavliq'
        self.env = self.state / 'compose.env'
        self.operations = self.state / 'operations.env'
        self.active = self.state / 'production-active.json'
        self.wrapper = self.state / 'production-compose.sh'
        self.evidence = root / 'var/lib/zavliq/deployments'
        self.systemd = root / 'etc/systemd/system'
        self.backups = root / 'var/backups/zavliq'
        self.health = root / 'var/lib/zavliq/health.json'


def private_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != ROOT_OWNER or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError('ROOT_OWNED_PRIVATE_FILE_REQUIRED')


def load_environment(path):
    private_file(path)
    result = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*=[^\s\x00]+', line):
            raise ValueError('SIMPLE_PRIVATE_ENVIRONMENT_REQUIRED')
        key, value = line.split('=', 1)
        if key in result:
            raise ValueError('DUPLICATE_ENVIRONMENT_KEY')
        result[key] = value
    return result


def verify_namespace(values, paths):
    expected = {'COMPOSE_PROJECT_NAME': PROJECT, 'ZAVLIQ_STATE_DIR': str(paths.state), 'ZAVLIQ_SERVER_NAME': 'zavliq.com',
                'ZAVLIQ_PUBLIC_URL': bundle.ORIGIN, 'ZAVLIQ_SITE_ADDRESS': 'zavliq.com'}
    if set(values) - ENV_KEYS or any(values.get(key) != value for key, value in expected.items()):
        raise ValueError('CANONICAL_FRESH_PRODUCTION_NAMESPACE_REQUIRED')
    for key in ['ZAVLIQ_ECHO_HOMESERVER_URL', 'ZAVLIQ_ECHO_CONTROL_URL']:
        if key in values and values[key] != bundle.ORIGIN:
            raise ValueError('PUBLIC_ECHO_ORIGIN_REQUIRED')
    if 'VITE_ECHO_USER_ID' in values and values['VITE_ECHO_USER_ID'] != bundle.ECHO:
        raise ValueError('PUBLIC_ECHO_ID_REQUIRED')
    if not re.fullmatch(r'development|[0-9a-f]{40}', values.get('ZAVLIQ_RELEASE', '')):
        raise ValueError('EXACT_RELEASE_ENVIRONMENT_REQUIRED')


def verify_secrets(paths):
    info = paths.state.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != ROOT_OWNER or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError('PRIVATE_PRODUCTION_STATE_DIRECTORY_REQUIRED')
    for name in ['postgres_password', 'registration_secret', 'policy_secret', 'control_secret']:
        path = paths.state / name
        private_file(path)
        if not 32 <= path.stat().st_size <= 256:
            raise ValueError('EXISTING_INITIALIZED_SECRET_FILES_REQUIRED')


def operations_environment(paths):
    values = load_environment(paths.operations)
    permitted = {'ZAVLIQ_PUBLIC_URL', 'ZAVLIQ_BACKUP_RECIPIENT', 'ZAVLIQ_ENVIRONMENT', 'ZAVLIQ_ENV_FILE', 'ZAVLIQ_BACKUP_DIR'}
    if set(values) - permitted or values.get('ZAVLIQ_PUBLIC_URL') != bundle.ORIGIN or not re.fullmatch(r'age1[0-9a-z]{58}', values.get('ZAVLIQ_BACKUP_RECIPIENT', '')):
        raise ValueError('PRODUCTION_PUBLIC_BACKUP_RECIPIENT_REQUIRED')
    fixed = {'ZAVLIQ_ENVIRONMENT': 'production', 'ZAVLIQ_ENV_FILE': str(paths.env), 'ZAVLIQ_BACKUP_DIR': str(paths.backups)}
    if any(key in values and values[key] != value for key, value in fixed.items()):
        raise ValueError('PRODUCTION_OPERATIONS_PATHS_REQUIRED')
    return {**values, **fixed}


def atomic_write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '-' + secrets.token_hex(6))
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path, value):
    atomic_write(path, json.dumps(value, indent=2) + '\n')


def child_environment(env=None):
    clean = {key: value for key, value in os.environ.items() if key in {'PATH', 'LANG', 'LC_ALL', 'TZ', 'HOME'}}
    clean.update(env or {})
    return clean


def command(args, *, timeout=30, env=None, passthrough=False):
    return subprocess.run(args, check=True, capture_output=not passthrough, text=not passthrough, timeout=timeout, env=child_environment(env))


def backup_command(script, environment, *, timeout=540, grace=15):
    # Leave room inside the existing ten-minute systemd limit for the Bash EXIT
    # trap to resume writers. subprocess.run(timeout=...) would kill Bash first.
    process = subprocess.Popen(['bash', str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True, env=child_environment(environment))
    try:
        process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try: os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        forced = False
        try: process.communicate(timeout=grace)
        except subprocess.TimeoutExpired:
            forced = True
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            process.communicate()
        raise ValueError('BACKUP_TIMEOUT_CLEANUP_UNCONFIRMED' if forced else 'BACKUP_TIMEOUT') from None
    if process.returncode:
        raise ValueError('ENCRYPTED_BACKUP_FAILED')


def release_path(paths, bundle_id):
    if not re.fullmatch(r'[0-9a-f]{40}-public-[0-9a-f]{16}', bundle_id):
        raise ValueError('EXACT_PUBLIC_BUNDLE_ID_REQUIRED')
    path = paths.releases / bundle_id
    if path.is_symlink() or not path.is_dir():
        raise ValueError('UPLOADED_PUBLIC_BUNDLE_REQUIRED')
    return path


def verify_bundle(path, expected_hash):
    # Pre-uploaded executable infrastructure must be adopted by root before
    # activation so another login cannot change it after hash verification.
    for entry in [path, *path.rglob('*')]:
        info = entry.lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) or info.st_uid != ROOT_OWNER or stat.S_IMODE(info.st_mode) & 0o022:
            raise ValueError('ROOT_OWNED_IMMUTABLE_BUNDLE_FILES_REQUIRED')
    bundle.require_hash(path / 'production-manifest.json', expected_hash)
    value = bundle.json_file(path / 'production-manifest.json')
    if value.get('schema') != bundle.SCHEMA or value.get('activation_supported') is not False or value.get('architecture') != 'linux/amd64':
        raise ValueError('EXPLICIT_PRODUCTION_BUNDLE_SCHEMA_REQUIRED')
    inputs = value['public_web']['build_inputs']
    config_hash = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    revision = value.get('revision', '')
    if not re.fullmatch(r'[0-9a-f]{40}', revision) or value['public_web']['configuration_sha256'] != config_hash or path.name != revision + '-public-' + config_hash[:16]:
        raise ValueError('PUBLIC_BUNDLE_PATH_CONFIGURATION_MISMATCH')
    if inputs.get('origin') != bundle.ORIGIN or inputs.get('web_echo_user_id') != bundle.ECHO:
        raise ValueError('CANONICAL_PUBLIC_BUILD_REQUIRED')
    bundle.require_hash(path / 'reviewed-stage-manifest.json', inputs['stage_manifest_sha256'])
    stage = bundle.json_file(path / 'reviewed-stage-manifest.json')
    if (stage.get('revision') != revision or stage.get('architecture') != 'linux/amd64' or stage.get('web_echo_user_id') != '@echo:localhost'
            or stage.get('native_runtime') != value.get('native_runtime') or stage.get('infra_hashes') != value.get('infra_hashes')
            or stage.get('source_archive_sha256') != value.get('source_archive_sha256')):
        raise ValueError('ORIGINAL_STAGE_PROVENANCE_REQUIRED')
    images = value.get('images', {})
    if set(images) != bundle.SERVICES or set(stage.get('images', {})) != bundle.SERVICES:
        raise ValueError('COMPLETE_PRODUCTION_IMAGE_PINS_REQUIRED')
    for name, image in images.items():
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', image.get('id', '')):
            raise ValueError('EXACT_IMAGE_ID_REQUIRED')
        if name != 'gateway' and image != stage['images'][name]:
            raise ValueError('UNCHANGED_STAGE_BACKEND_IMAGES_REQUIRED')
    if images['gateway']['ref'] != 'zavliq-web:' + path.name:
        raise ValueError('DISTINCT_PUBLIC_WEB_TAG_REQUIRED')
    for name, expected in [('source.tar.gz', value['source_archive_sha256']), ('images.tar.gz', value['images_archive_sha256']),
                           ('reviewed-web-bases.json', inputs['web_bases_sha256']), ('public-web.Dockerfile', inputs['pinned_dockerfile_sha256']),
                           ('preparer.py', inputs['preparer_sha256']), ('compose.images.yaml', value['compose_overlay_sha256'])]:
        bundle.require_hash(path / name, expected)
    if (path / 'compose.images.yaml').read_text() != bundle.compose_overlay(images):
        raise ValueError('EXACT_IMAGE_ONLY_OVERLAY_REQUIRED')
    bundle.verify_runtime(path / 'native-runtime.tar.gz', value['native_runtime'], revision)
    bundle.verify_docker_archive(path / 'images.tar.gz', images, value['native_runtime'])
    with tempfile.TemporaryDirectory(prefix='zavliq-production-verify-') as temporary:
        source = Path(temporary)
        bundle.extract_source(path / 'source.tar.gz', source)
        if bundle.tree_hashes(source) != value['infra_hashes'] or bundle.tree_hashes(path) != value['infra_hashes']:
            raise ValueError('ARCHIVED_INFRA_INTEGRITY_REQUIRED')
    return value


def compose_args(paths, release, *args):
    return ['docker', 'compose', '--project-name', PROJECT, '--env-file', str(paths.env),
            '-f', str(release / 'infra/compose.yaml'), '-f', str(release / 'infra/compose.production.yaml'),
            '-f', str(release / 'compose.images.yaml'), *args]


def verify_composed(config, manifest, paths):
    expected = {**manifest['images'], **{name: manifest['images'][base] for name, base in bundle.HELPERS.items()}}
    services = config.get('services', {})
    if config.get('name') != PROJECT or set(services) != set(expected):
        raise ValueError('EXACT_PRODUCTION_SERVICE_SET_REQUIRED')
    for name, service in services.items():
        if service.get('image') != expected[name]['id'] or service.get('build') or service.get('pull_policy') != 'never':
            raise ValueError('RESOLVED_IMAGE_PINS_OR_BUILD_POLICY_INVALID')
        ports = service.get('ports', [])
        if name == 'gateway':
            actual = {(str(port.get('published')), port.get('target'), port.get('protocol', 'tcp')) for port in ports}
            if actual != {('80', 80, 'tcp'), ('443', 443, 'tcp'), ('443', 443, 'udp')} or len(ports) != 3:
                raise ValueError('ONLY_EXPECTED_PUBLIC_GATEWAY_PORTS_ALLOWED')
        elif ports:
            raise ValueError('PRIVATE_BACKEND_PORTS_REQUIRED')
        for mount in service.get('volumes', []):
            if mount.get('type') == 'volume' and mount.get('source') not in config.get('volumes', {}):
                raise ValueError('PROJECT_OWNED_VOLUMES_REQUIRED')
            if mount.get('type') == 'bind' and not (name == 'gateway' and mount.get('source') == str(paths.evidence.parent) and mount.get('target') == '/status' and mount.get('read_only') is True):
                raise ValueError('ONLY_PUBLIC_HEALTH_BIND_MOUNT_ALLOWED')
    for name, volume in config.get('volumes', {}).items():
        if volume.get('name') != PROJECT + '_' + name or volume.get('external'):
            raise ValueError('FRESH_PRODUCTION_VOLUME_NAMES_REQUIRED')
    for name, secret in config.get('secrets', {}).items():
        if secret.get('file') != str(paths.state / name) or name not in {'postgres_password', 'registration_secret', 'policy_secret', 'control_secret'}:
            raise ValueError('EXISTING_PRODUCTION_SECRET_PATHS_REQUIRED')
    if set(config.get('networks', {})) != {'backend', 'edge'}:
        raise ValueError('PRODUCTION_NETWORKS_REQUIRED')
    for name, network in config['networks'].items():
        if network.get('name') != PROJECT + '_' + name or network.get('external') or bool(network.get('internal')) != (name == 'backend'):
            raise ValueError('PRIVATE_BACKEND_NETWORK_REQUIRED')
    for name in set(expected) - {'gateway', 'echo'}:
        if set(services[name].get('networks', {})) != {'backend'}:
            raise ValueError('BACKEND_EGRESS_MUST_REMAIN_DISABLED')
    if set(services['gateway'].get('networks', {})) != {'backend', 'edge'} or set(services['echo'].get('networks', {})) != {'edge'}:
        raise ValueError('EXPECTED_GATEWAY_AND_ECHO_NETWORKS_REQUIRED')


def runtime_record(paths):
    private_file(paths.active)
    record = json.loads(paths.active.read_text())
    if record.get('status') not in {'activating', 'ready', 'failed'}:
        raise ValueError('KNOWN_PRODUCTION_STATE_REQUIRED')
    release = release_path(paths, record['bundle_id'])
    bundle.require_hash(release / 'production-manifest.json', record['manifest_sha256'])
    manifest = bundle.json_file(release / 'production-manifest.json')
    bundle.require_hash(release / 'compose.images.yaml', manifest['compose_overlay_sha256'])
    if (release / 'compose.images.yaml').read_text() != bundle.compose_overlay(manifest['images']):
        raise ValueError('EXACT_IMAGE_ONLY_OVERLAY_REQUIRED')
    for name in ['infra/compose.yaml', 'infra/compose.production.yaml']:
        bundle.require_hash(release / name, manifest['infra_hashes'][name])
    values = load_environment(paths.env)
    verify_namespace(values, paths)
    if values['ZAVLIQ_RELEASE'] != manifest['revision'] or not paths.current.is_symlink() or paths.current.resolve() != release.resolve():
        raise ValueError('ACTIVE_PRODUCTION_POINTER_MISMATCH')
    return record, release, manifest


def run_compose(paths, release, *args, timeout=30, passthrough=False):
    values = load_environment(paths.env)
    verify_namespace(values, paths)
    return command(compose_args(paths, release, *args), timeout=timeout, env=values, passthrough=passthrough)


def backup_program(original, wrapper):
    expected = 'compose="$root/infra/scripts/compose.sh"'
    fallback = 'env_file=${ZAVLIQ_ENV_FILE:-"$root/infra/.${ZAVLIQ_ENVIRONMENT:-local}/compose.env"}'
    if original.count(expected) != 1 or original.count(fallback) != 1 or original.count('$root') != 2:
        raise ValueError('EXACT_BACKUP_COMPOSE_ASSIGNMENT_REQUIRED')
    return original.replace(expected, 'compose=' + shlex.quote(str(wrapper)), 1)


def run_backup(paths):
    _, release, manifest = runtime_record(paths)
    source = release / 'infra/scripts/backup.sh'
    bundle.require_hash(source, manifest['infra_hashes']['infra/scripts/backup.sh'])
    program = backup_program(source.read_text(), paths.wrapper)
    environment = operations_environment(paths)
    started = time.time()
    with tempfile.TemporaryDirectory(prefix='zavliq-production-backup-') as temporary:
        script = Path(temporary) / 'backup.sh'
        script.write_text(program)
        script.chmod(0o700)
        backup_command(script, environment)
    stamp = (paths.backups / 'last-success').read_text().strip()
    if not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z', stamp):
        raise ValueError('NEW_BACKUP_SUCCESS_RECORD_REQUIRED')
    archive = paths.backups / ('zavliq-' + stamp + '.tar.age')
    if not archive.is_file() or archive.is_symlink() or archive.stat().st_mtime < started - 1:
        raise ValueError('NEW_ENCRYPTED_BACKUP_REQUIRED')
    checksum = archive.with_name(archive.name + '.sha256').read_text().split()[0]
    bundle.require_hash(archive, checksum)
    return {'archive': archive.name, 'sha256': checksum, 'bytes': archive.stat().st_size, 'off_host_verified': False}


def require_fresh_project(paths):
    if paths.current.exists() or paths.current.is_symlink() or paths.active.exists() or paths.active.is_symlink() or (paths.state / 'previous-release').exists() or (paths.state / 'previous-release').is_symlink():
        raise ValueError('FRESH_PRODUCTION_HOST_STATE_REQUIRED')
    for args in [['docker', 'ps', '--all', '--quiet', '--filter', 'label=com.docker.compose.project=' + PROJECT],
                 ['docker', 'ps', '--all', '--quiet', '--filter', 'name=' + PROJECT],
                 ['docker', 'volume', 'ls', '--quiet', '--filter', 'name=' + PROJECT],
                 ['docker', 'network', 'ls', '--quiet', '--filter', 'label=com.docker.compose.project=' + PROJECT]]:
        if command(args).stdout.strip():
            raise ValueError('NO_EXISTING_PRODUCTION_CONTAINERS_OR_VOLUMES_REQUIRED')
    if any(paths.systemd.glob('zavliq-*')):
        raise ValueError('NO_UNOWNED_ZAVLIQ_SYSTEMD_UNITS_ALLOWED')


def require_images(manifest):
    for image in manifest['images'].values():
        value = json.loads(command(['docker', 'image', 'inspect', '--format', '{{json .}}', image['id']]).stdout)
        if value.get('Id') != image['id'] or value.get('Os') != 'linux' or value.get('Architecture') != 'amd64':
            raise ValueError('LOADED_EXACT_AMD64_IMAGES_REQUIRED')


def remaining_timeout(deadline, maximum=5):
    if deadline is None:
        return maximum
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('HEALTH_DEADLINE')
    return min(maximum, remaining)


def require_running(paths, release, manifest, deadline=None):
    for name, image in manifest['images'].items():
        ids = run_compose(paths, release, '--profile', 'echo', 'ps', '--quiet', name, timeout=remaining_timeout(deadline, 30)).stdout.split()
        if len(ids) != 1:
            raise ValueError('ONE_RUNNING_CONTAINER_PER_SERVICE_REQUIRED')
        value = command(['docker', 'inspect', '--format', '{{.Image}}|{{.State.Running}}|{{.State.Paused}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}', ids[0]], timeout=remaining_timeout(deadline, 30)).stdout.strip().split('|')
        allowed_health = {'', 'healthy'} if name == 'gateway' else {'healthy'}
        if len(value) != 4 or value[0] != image['id'] or value[1:3] != ['true', 'false'] or value[3] not in allowed_health:
            raise ValueError('RUNNING_IMAGE_OR_HEALTH_MISMATCH')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise ValueError('PUBLIC_HTTPS_REDIRECT_REFUSED')


def public_readiness(deadline=None):
    opener = urllib.request.build_opener(NoRedirect())
    reports = {}
    for path in ['/health', '/_matrix/client/versions', '/.well-known/matrix/client', '/.well-known/zavliq', '/']:
        with opener.open(bundle.ORIGIN + path, timeout=remaining_timeout(deadline)) as response:
            if response.status != 200 or not response.headers.get('Strict-Transport-Security'):
                raise ValueError('PUBLIC_HTTPS_HEALTH_REQUIRED')
            if path == '/':
                if not response.headers.get('Content-Security-Policy'):
                    raise ValueError('PUBLIC_WEB_HEADERS_REQUIRED')
            else:
                data = response.read(65537)
                if len(data) > 65536:
                    raise ValueError('BOUNDED_DISCOVERY_JSON_REQUIRED')
                reports[path] = json.loads(data)
    if (reports['/health'].get('status') != 'ok' or not reports['/_matrix/client/versions'].get('versions')
            or reports['/.well-known/matrix/client'].get('m.homeserver', {}).get('base_url') != bundle.ORIGIN):
        raise ValueError('CANONICAL_PUBLIC_DISCOVERY_REQUIRED')
    discovery = reports['/.well-known/zavliq']
    if any(discovery.get(key) != value for key, value in {'server_name': 'zavliq.com', 'homeserver': bundle.ORIGIN, 'control_url': bundle.ORIGIN}.items()):
        raise ValueError('CANONICAL_PUBLIC_DISCOVERY_REQUIRED')


class HealthDeadline(TimeoutError):
    pass


def run_health(paths, timeout=35):
    """Publish content-free readiness within the health service's 45-second limit."""
    deadline = time.monotonic() + timeout
    checks = []

    def running():
        record, release, manifest = runtime_record(paths)
        if record['status'] != 'ready' or set(manifest['images']) != bundle.SERVICES:
            raise ValueError('READY_PRODUCTION_WITH_ALL_SERVICES_REQUIRED')
        require_running(paths, release, manifest, deadline=deadline)

    def disk():
        usage = shutil.disk_usage(paths.releases)
        if usage.total <= 0 or usage.used / usage.total >= 0.8:
            raise ValueError('DISK_HEADROOM_REQUIRED')

    def backup():
        marker = paths.backups / 'last-success'
        private_file(marker)
        if marker.stat().st_size > 32:
            raise ValueError('BOUNDED_BACKUP_SUCCESS_RECORD_REQUIRED')
        stamp = marker.read_text().strip()
        created = dt.datetime.strptime(stamp, '%Y%m%dT%H%M%SZ').replace(tzinfo=dt.timezone.utc).timestamp()
        if not re.fullmatch(r'[0-9]{8}T[0-9]{6}Z', stamp) or not -60 <= time.time() - created <= 86400:
            raise ValueError('FRESH_KNOWN_BACKUP_REQUIRED')
        archive = paths.backups / ('zavliq-' + stamp + '.tar.age')
        checksum = archive.with_name(archive.name + '.sha256')
        for item in [archive, checksum]:
            private_file(item)
        if archive.stat().st_size <= 100 or checksum.stat().st_size > 4096:
            raise ValueError('COMPLETE_ENCRYPTED_BACKUP_REQUIRED')
        if not re.fullmatch(r'[0-9a-f]{64}  ' + re.escape(str(archive)) + r'\n?', checksum.read_text()):
            raise ValueError('EXACT_BACKUP_CHECKSUM_RECORD_REQUIRED')

    def expired(*_):
        raise HealthDeadline('HEALTH_DEADLINE')

    previous_handler = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        for name, check in [('running_images', running), ('public_https', lambda: public_readiness(deadline)),
                            ('disk', disk), ('backup_age', backup)]:
            try:
                remaining_timeout(deadline)
                check()
                checks.append({'check': name, 'ok': True})
            except HealthDeadline:
                checks.append({'check': name, 'ok': False, 'error': 'HEALTH_DEADLINE'})
                break
            except Exception as error:
                code = str(error) if isinstance(error, ValueError) and re.fullmatch(r'[A-Z0-9_]+', str(error)) else type(error).__name__
                checks.append({'check': name, 'ok': False, 'error': code})
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
    report = {'ok': len(checks) == 4 and all(check['ok'] for check in checks), 'checked_at': int(time.time()), 'checks': checks}
    atomic_write(paths.health, json.dumps(report, separators=(',', ':')) + '\n', mode=0o644)
    return report


def install_operations(paths, release):
    wrapper = '#!/usr/bin/env bash\nset -euo pipefail\nexec /usr/bin/python3 ' + shlex.quote(str(TOOLS / 'activate.py')) + ' compose -- "$@"\n'
    atomic_write(paths.wrapper, wrapper, mode=0o700)
    for name in ['health', 'backup', 'retention']:
        for kind in ['service', 'timer']:
            filename = 'zavliq-' + name + '.' + kind
            atomic_write(paths.systemd / filename, (release / 'infra/systemd' / filename).read_text(), mode=0o644)
    for name in ['backup', 'retention', 'health']:
        dropin = '[Service]\nExecStart=\nExecStart=/usr/bin/python3 ' + str(TOOLS / 'activate.py') + ' ' + name + '\n'
        atomic_write(paths.systemd / ('zavliq-' + name + '.service.d') / 'production-pins.conf', dropin, mode=0o644)
    command(['systemctl', 'daemon-reload'])


def stop_operations(report):
    # A timer may already have triggered a oneshot service. Fence both, and keep
    # failed deployments from re-enabling maintenance automatically after reboot.
    for timer in TIMERS:
        try: command(['systemctl', 'disable', timer])
        except Exception: report['timer_disable_incomplete'] = True
        try: command(['systemctl', 'stop', timer])
        except Exception: report['timer_stop_incomplete'] = True
    for service in [timer.replace('.timer', '.service') for timer in TIMERS]:
        try: command(['systemctl', 'stop', service], timeout=90)
        except Exception: report['maintenance_stop_incomplete'] = True


def activate(paths, bundle_id, expected_hash, expected_current):
    release = release_path(paths, bundle_id)
    manifest = verify_bundle(release, expected_hash)
    values = load_environment(paths.env)
    verify_namespace(values, paths)
    verify_secrets(paths)
    operations_environment(paths)
    for previous_evidence in paths.evidence.glob('*/activation.json'):
        private_file(previous_evidence)
        if json.loads(previous_evidence.read_text()).get('bundle_id') == bundle_id:
            raise ValueError('PREVIOUS_ATTEMPT_REQUIRES_SEPARATE_RECOVERY_REVIEW')
    pointer = paths.current.with_name('current.next')
    if pointer.exists() or pointer.is_symlink():
        raise ValueError('UNRESOLVED_CURRENT_POINTER_REQUIRES_REVIEW')
    if command(['docker', 'info', '--format', '{{.OSType}}/{{.Architecture}}']).stdout.strip() not in {'linux/amd64', 'linux/x86_64'}:
        raise ValueError('NATIVE_AMD64_PRODUCTION_HOST_REQUIRED')
    if shutil.disk_usage(paths.releases).free < (release / 'images.tar.gz').stat().st_size * 3 + 8 * 1024**3:
        raise ValueError('DEPLOYMENT_DISK_HEADROOM_REQUIRED')
    previous = None
    if expected_current == 'none':
        require_fresh_project(paths)
        if values['ZAVLIQ_RELEASE'] != 'development':
            raise ValueError('INITIALIZED_FRESH_CONFIG_REQUIRED')
    else:
        record, old_release, _ = runtime_record(paths)
        if record['status'] != 'ready' or record['manifest_sha256'] != expected_current or record['bundle_id'] == bundle_id:
            raise ValueError('EXACT_SUCCESSFUL_CURRENT_DEPLOYMENT_REQUIRED')
        previous = {'bundle_id': record['bundle_id'], 'manifest_sha256': record['manifest_sha256']}
    # Resolve every profile with the final overlay before loading images, changing
    # current, or starting a container. Unsupported !reset fails this check.
    config = json.loads(run_compose(paths, release, '--profile', '*', 'config', '--format', 'json').stdout)
    verify_composed(config, manifest, paths)
    for image in manifest['images'].values():
        cached = set(command(['docker', 'image', 'ls', '--quiet', '--no-trunc', '--filter', 'reference=' + image['ref']]).stdout.split())
        if cached - {image['id']}:
            raise ValueError('EXISTING_IMAGE_TAG_COLLISION')
    command(['docker', 'load', '-i', str(release / 'images.tar.gz')], timeout=600)
    require_images(manifest)
    evidence = paths.evidence / (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + secrets.token_hex(6))
    evidence.mkdir(parents=True, exist_ok=False, mode=0o700)
    report = {'operation': 'activate_production', 'bundle_id': bundle_id, 'manifest_sha256': expected_hash,
              'previous': previous, 'started_at': int(time.time()), 'ok': False, 'launch_verified': False}
    switched, paused_timers = False, []
    write_json(evidence / 'activation.json', report)
    try:
        if previous:
            for timer in TIMERS:
                if command(['systemctl', 'show', '--property=ActiveState', '--value', timer]).stdout.strip() == 'active':
                    command(['systemctl', 'stop', timer])
                    paused_timers.append(timer)
            report['pre_update_backup'] = run_backup(paths)
            shutil.copy2(paths.env, paths.state / 'previous-compose.env')
            atomic_write(paths.state / 'previous-release', str(old_release) + '\n')
            write_json(evidence / 'previous-active.json', record)
        active = {'bundle_id': bundle_id, 'manifest_sha256': expected_hash, 'previous': previous, 'status': 'activating'}
        switched = True
        write_json(paths.active, active)
        values['ZAVLIQ_RELEASE'] = manifest['revision']
        atomic_write(paths.env, ''.join(key + '=' + value + '\n' for key, value in values.items()))
        pointer.symlink_to(release)
        pointer.replace(paths.current)
        install_operations(paths, release)
        up = ['up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '180']
        run_compose(paths, release, '--profile', 'echo', 'stop', 'echo', 'gateway', timeout=75)
        run_compose(paths, release, *up, 'synapse', 'control', timeout=210)
        run_compose(paths, release, '--profile', 'echo-setup', 'run', '--rm', '--no-deps', '-T', 'echo-bootstrap', timeout=120)
        run_compose(paths, release, *up, 'gateway', timeout=210)
        run_compose(paths, release, '--profile', 'echo', *up, '--no-deps', '--force-recreate', 'echo', timeout=210)
        require_running(paths, release, manifest)
        public_readiness()
        report['post_activation_backup'] = run_backup(paths)
        active['status'] = 'ready'
        write_json(paths.active, active)
        command(['systemctl', 'enable', '--now', *TIMERS])
        report.update(ok=True, completed_at=int(time.time()), images=manifest['images'], public_https_discovery=True,
                      echo_container_healthy=True, independent_messaging_and_restore_checks_pending=True)
    except Exception as error:
        report['error'] = str(error) if isinstance(error, ValueError) and re.fullmatch(r'[A-Z0-9_]+', str(error)) else type(error).__name__
        if switched:
            # A startup may already have migrated durable data. Preserve the new
            # images/configuration and stop writers; never start older DB images.
            active['status'] = 'failed'
            try: write_json(paths.active, active)
            except Exception: report['marker_write_incomplete'] = True
            report['operator_recovery_required'] = True
            stop_operations(report)
            try:
                run_compose(paths, release, '--profile', 'echo', 'stop', '--timeout', '30', 'echo', 'gateway', 'control', 'synapse', 'postgres', timeout=165)
            except Exception:
                report['stop_incomplete'] = True
        else:
            for timer in paused_timers:
                try: command(['systemctl', 'start', timer])
                except Exception: report['timer_resume_incomplete'] = True
        raise
    finally:
        write_json(evidence / 'activation.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    deploy = commands.add_parser('activate')
    deploy.add_argument('--bundle-id', required=True)
    deploy.add_argument('--manifest-sha256', required=True)
    deploy.add_argument('--expected-current-manifest-sha256', required=True)
    commands.add_parser('backup')
    commands.add_parser('retention')
    commands.add_parser('health')
    compose = commands.add_parser('compose')
    compose.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if os.geteuid() != 0 or Path(__file__).resolve() != TOOLS / 'activate.py':
        parser.error('Use the reviewed root-owned /opt/zavliq/production-tools installation on the production host.')
    for name in ['activate.py', 'prepare_bundle.py']:
        info = (TOOLS / name).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
            parser.error('Production tools must be root-owned and not writable by other users.')
    os.umask(0o077)
    paths = Paths()
    try:
        if args.action == 'activate':
            with Path('/run/lock/zavliq-deploy.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = activate(paths, args.bundle_id, args.manifest_sha256, args.expected_current_manifest_sha256)
        elif args.action == 'backup':
            result = run_backup(paths)
        elif args.action == 'health':
            result = run_health(paths)
        else:
            _, release, _ = runtime_record(paths)
            arguments = ['--profile', 'maintenance', 'run', '--rm', '--no-deps', '-T', 'retention'] if args.action == 'retention' else args.arguments
            if arguments[:1] == ['--']:
                arguments = arguments[1:]
            if not arguments or any(arg in {'build', 'pull', 'down', '--build', '--pull', '-f', '--file', '--env-file', '--project-name', '-p'} or arg.startswith(('--file=', '--env-file=', '--project-name=', '--project-directory', '--pull=')) for arg in arguments):
                raise ValueError('PINNED_COMPOSE_COMMAND_REQUIRED')
            # pg_dump and tar output are binary and already redirected by the
            # archived backup script. Preserve bytes without decoding/buffering.
            run_compose(paths, release, *arguments, timeout=240, passthrough=True)
            return
        print(json.dumps(result))
        if result.get('ok') is False:
            raise SystemExit(1)
    except Exception as error:
        code = str(error) if isinstance(error, ValueError) and re.fullmatch(r'[A-Z0-9_]+', str(error)) else type(error).__name__
        print(json.dumps({'operation': args.action, 'ok': False, 'error': code}))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
