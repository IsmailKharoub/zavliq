#!/usr/bin/env python3
"""Activate an already built private-stage bundle; never initializes state or builds."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.request

from begin_soak import snapshot
from release_bundle import ECHO_ID, POSTGRES_ID, POSTGRES_REF, digest

ROOT = Path('/opt/zavliq')
ENV_FILE = Path('/etc/zavliq/compose.env')
PROJECT = 'zavliq-load'
PUBLIC_ORIGIN = 'http://localhost:28180'


def load_environment(path):
    return dict(line.split('=', 1) for line in path.read_text().splitlines() if '=' in line and not line.startswith('#'))


def verify_namespace(values):
    expected = {'COMPOSE_PROJECT_NAME': PROJECT, 'ZAVLIQ_SERVER_NAME': 'localhost',
                'ZAVLIQ_PUBLIC_URL': PUBLIC_ORIGIN, 'ZAVLIQ_SITE_ADDRESS': ':80',
                'ZAVLIQ_STATE_DIR': '/etc/zavliq', 'ZAVLIQ_STAGING_WEB_PORT': '19180'}
    if any(values.get(key) != value for key, value in expected.items()):
        raise ValueError('EXISTING_PRIVATE_NAMESPACE_REQUIRED')


def verify_manifest(release, revision, expected_digest):
    if not re.fullmatch(r'[0-9a-f]{40}', revision) or digest(release / 'manifest.json') != expected_digest:
        raise ValueError('REVIEWED_MANIFEST_REQUIRED')
    value = json.loads((release / 'manifest.json').read_text())
    if value['revision'] != revision or value['architecture'] != 'linux/amd64' or value['web_echo_user_id'] != ECHO_ID:
        raise ValueError('UNEXPECTED_STAGING_BUNDLE')
    for name, field in [('source.tar.gz', 'source_archive_sha256'), ('images.tar.gz', 'images_archive_sha256')]:
        if digest(release / name) != value[field]:
            raise ValueError('BUNDLE_ARCHIVE_HASH_MISMATCH')
    for name, expected in value['infra_hashes'].items():
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or path.parts[0] != 'infra' or digest(release / path) != expected:
            raise ValueError('BUNDLE_INFRA_HASH_MISMATCH')
    images = value['images']
    if set(images) != {'synapse', 'control', 'gateway', 'postgres', 'echo'} or images['postgres'] != {'ref': POSTGRES_REF, 'id': POSTGRES_ID}:
        raise ValueError('ALL_EXPECTED_IMAGE_PINS_REQUIRED')
    for service, image in images.items():
        expected_ref = POSTGRES_REF if service == 'postgres' else f'zavliq-{"web" if service == "gateway" else service}:{revision}'
        if image['ref'] != expected_ref or not re.fullmatch(r'sha256:[0-9a-f]{64}', image['id']):
            raise ValueError('UNEXPECTED_IMAGE_PIN')
    if value['native_runtime']['head_sha'] != revision:
        raise ValueError('RUNTIME_SOURCE_MISMATCH')
    return value


def command(args, *, timeout=30, quiet=False):
    return subprocess.run(args, check=True, timeout=timeout, stdout=subprocess.DEVNULL if quiet else None)


def compose(release, *args, env_file=ENV_FILE):
    files = [release / 'infra/compose.yaml', release / 'infra/compose.staging.yaml',
             release / 'infra/staging/compose.echo.yaml', Path('/etc/zavliq/staging-images.json'), Path('/etc/zavliq/admission.yaml')]
    return ['docker', 'compose', '--env-file', str(env_file), *[part for file in files for part in ('-f', str(file))], *args]


def require_image_pins(images):
    for image in images.values():
        actual = subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Id}}', image['ref']], text=True, timeout=5).strip()
        if actual != image['id']:
            raise ValueError('LOCAL_IMAGE_PIN_MISMATCH')


def verify_loopback_bindings(configuration):
    for name, service in configuration['services'].items():
        ports = service.get('ports', [])
        if name == 'gateway':
            if len(ports) != 1 or ports[0].get('host_ip') != '127.0.0.1' or str(ports[0].get('published')) != '19180' or ports[0].get('target') != 80:
                raise ValueError('PRIVATE_GATEWAY_PORT_REQUIRED')
        elif ports:
            raise ValueError('UNEXPECTED_PUBLIC_SERVICE_PORT')


def readiness():
    values = {}
    for path in ['/health', '/_matrix/client/versions', '/.well-known/matrix/client', '/.well-known/zavliq']:
        with urllib.request.urlopen('http://localhost:19180' + path, timeout=5) as response:
            values[path] = json.load(response)
    if values['/.well-known/matrix/client']['m.homeserver']['base_url'] != PUBLIC_ORIGIN:
        raise ValueError('MATRIX_DISCOVERY_CHANGED')
    discovery = values['/.well-known/zavliq']
    if discovery['server_name'] != 'localhost' or discovery['homeserver'] != PUBLIC_ORIGIN or discovery['control_url'] != PUBLIC_ORIGIN:
        raise ValueError('ZAVLIQ_DISCOVERY_CHANGED')
    with urllib.request.urlopen('http://localhost:19180/', timeout=5) as response:
        if response.status != 200 or not response.headers.get('Content-Security-Policy') or response.headers.get('Strict-Transport-Security'):
            raise ValueError('COMPILED_WEB_HEADERS_INVALID')
    return True


def deploy(revision, expected_digest):
    release = ROOT / 'releases' / revision
    manifest = verify_manifest(release, revision, expected_digest)
    environment = load_environment(ENV_FILE)
    verify_namespace(environment)
    if Path('/var/lib/zavliq/staging-soak/marker.json').exists():
        raise ValueError('PRESERVE_AND_CLOSE_EXISTING_SOAK_BEFORE_DEPLOY')
    if not Path('/etc/zavliq/admission.yaml').is_file():
        raise ValueError('EXISTING_ADMISSION_OVERRIDE_REQUIRED')
    if shutil.disk_usage(ROOT).free < (release / 'images.tar.gz').stat().st_size * 3 + 8 * 1024**3:
        raise ValueError('DEPLOYMENT_DISK_HEADROOM_REQUIRED')
    previous = (ROOT / 'current').resolve(strict=True)
    if not re.fullmatch(r'/opt/zavliq/releases/[0-9a-f]{40}', str(previous)):
        raise ValueError('KNOWN_PREVIOUS_RELEASE_REQUIRED')
    evidence = Path('/var/lib/zavliq/deployments') / revision
    evidence.mkdir(parents=True, exist_ok=False, mode=0o700)
    shutil.copy2(ENV_FILE, evidence / 'previous-compose.env')
    services = ['synapse', 'control', 'gateway', 'postgres']
    if subprocess.run(['docker', 'inspect', PROJECT + '-echo-1'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5).returncode == 0:
        services.append('echo')
    before = snapshot(PROJECT, services)
    (evidence / 'previous.json').write_text(json.dumps({'release': str(previous), 'images': before}, indent=2) + '\n')
    if Path('/etc/zavliq/staging-images.json').exists():
        shutil.copy2('/etc/zavliq/staging-images.json', evidence / 'previous-image-pins.json')
    timers = ['zavliq-backup.timer', 'zavliq-retention.timer']
    active_timers = [name for name in timers if subprocess.run(['systemctl', 'is-active', '--quiet', name]).returncode == 0]
    report = {'revision': revision, 'started_at': int(time.time()), 'ok': False}
    try:
        for timer in active_timers:
            command(['systemctl', 'stop', timer])
        command(['systemctl', 'start', 'zavliq-backup.service'], timeout=615)
        # SHA verification happened before load. No pulls, retagging or build commands follow.
        command(['docker', 'load', '-i', str(release / 'images.tar.gz')], timeout=600)
        require_image_pins(manifest['images'])
        mapping = {'synapse-config': 'synapse', 'bootstrap': 'synapse', 'retention': 'synapse', 'echo-bootstrap': 'control'}
        pins = {'services': {name: {'image': image['ref'], 'pull_policy': 'never'} for name, image in manifest['images'].items()}}
        pins['services'].update({name: {'image': manifest['images'][base]['ref'], 'pull_policy': 'never'} for name, base in mapping.items()})
        Path('/etc/zavliq/staging-images.json').write_text(json.dumps(pins))
        # Change only the source revision; every identity/configuration value survives.
        environment['ZAVLIQ_RELEASE'] = revision
        temporary = ENV_FILE.with_suffix('.next')
        temporary.write_text(''.join(key + '=' + value + '\n' for key, value in environment.items()))
        temporary.chmod(0o600)
        config = json.loads(subprocess.check_output(compose(release, '--profile', 'echo', 'config', '--format', 'json', env_file=temporary), text=True, timeout=15))
        verify_loopback_bindings(config)
        temporary.replace(ENV_FILE)
        next_link = ROOT / 'current.next'
        next_link.symlink_to(release)
        next_link.replace(ROOT / 'current')
        # Keep the previous gateway serving until the real Echo identity is ready.
        command(compose(release, 'up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '180', 'synapse', 'control'), timeout=200)
        command(compose(release, '--profile', 'echo', 'stop', 'echo'), timeout=45)
        command(compose(release, '--profile', 'echo-setup', 'run', '--rm', '--no-deps', '-T', 'echo-bootstrap'), timeout=120)
        command(compose(release, '--profile', 'echo', 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate', '--wait', '--wait-timeout', '180', 'echo'), timeout=200)
        report['echo_healthy_before_web_switch'] = True
        # Recreate the namespace-sharing worker after the gateway container changes.
        command(compose(release, '--profile', 'echo', 'stop', 'echo'), timeout=45)
        command(compose(release, 'up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '180', 'gateway'), timeout=200)
        command(compose(release, '--profile', 'echo', 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--force-recreate', '--wait', '--wait-timeout', '180', 'echo'), timeout=200)
        actual = snapshot(PROJECT, manifest['images'])
        if any(actual[name]['image_id'] != image['id'] or not actual[name]['running'] or actual[name]['health'] not in {'', 'healthy'} for name, image in manifest['images'].items()):
            raise ValueError('RUNNING_IMAGE_PINS_OR_HEALTH_MISMATCH')
        readiness()
        report.update(ok=True, completed_at=int(time.time()), images=manifest['images'], source_archive_sha256=manifest['source_archive_sha256'],
                      web_echo_user_id=ECHO_ID, echo_native_binary_sha256=manifest['native_runtime']['binary_sha256'], core_http_readiness=True,
                      final_candidate=True, operator_messaging_and_recovery_checks_pending=True, soak_started=False)
    finally:
        (evidence / 'deployment.json').write_text(json.dumps(report, indent=2) + '\n')
        for timer in active_timers:
            command(['systemctl', 'start', timer])
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--revision', required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--measurement-idle', action='store_true', help='Operator attests that no timed workload is running.')
    args = parser.parse_args()
    if os.geteuid() != 0 or not args.measurement_idle:
        parser.error('Run as root only after the measured workload has finished.')
    os.umask(0o077)
    with Path('/run/lock/zavliq-deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(json.dumps(deploy(args.revision, args.manifest_sha256)))


if __name__ == '__main__':
    main()
