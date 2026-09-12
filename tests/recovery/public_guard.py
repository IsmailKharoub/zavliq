"""Fixed production-only verification guards; never deploys or routes a service."""
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import ssl
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('public_recovery_base', Path(__file__).with_name('verify.py'))
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
import zavliq
import zavliq.client
from zavliq import Zavliq, ZavliqError

ORIGIN = 'https://zavliq.com'
SERVER = 'zavliq.com'
OWNER = '@echo:zavliq.com'
REVISION = '2f76469b507c8745d4be5ef311f32774021143aa'
SOURCE_HASH = '2a96f82d9413c3adc8193e4335f6703f7f000923b709f11a24a04e03546a0c5e'
MANIFEST_HASH = '55f0bccfdf7f7a267377fd44ce115603c2e7f8ea383502236f390cee387fc39c'
BINARY_HASH = 'e999ba1d7721b9e026b9a914e57ce766c252501d93e636cca2cba8524b73f5ac'
SDK_HASHES = {'zavliq': '5124055596f961ad12f362f002d3296bf54fe6c1d597f8cd089331a6adadc689',
              'zavliq.client': '4cce7bf19d0e9b93a3af68435040c33056a61695db7b616fa12312e5a45f7751'}
IMAGE_IDS = {
    'synapse': 'sha256:ad076f133f0795ec665d3419c0d2d8b4645853f80f64e27cee1dc00061dfe2d7',
    'control': 'sha256:5f53d7fbef3b59b417716f3798243b48199f9a629c24ec10ad0c1ead711cf0e5',
    'gateway': 'sha256:1a368a99a18db87f8d1794c4bfc15f777774978c639ea43d4c644671f94f945f',
    'postgres': 'sha256:18cfe3ef5e6815560c98237d6216d1e5119702fb0f3894c8785dd58b8bbe5d73',
    'echo': 'sha256:1cb8800c5984b0e4d900a916df57450fb9ab158087c9e9a45764b8c33de51b2b',
}
PRIVATE = ROOT / 'tests/recovery/.local'
EVIDENCE = ROOT / 'tests/recovery/evidence'
require, sha256, utc_now = base.require, base.sha256, base.utc_now
atomic_json, private_directory, exclusive_lock = base.atomic_json, base.private_directory, base.exclusive_lock
find_event, event_id, require_message = base.find_event, base.event_id, base.require_message
HEX64 = re.compile(r'[0-9a-f]{64}')


def artifacts(binary):
    require(sys.flags.isolated == 1, 'ISOLATED_INSTALLED_WHEEL_REQUIRED')
    require(sys.platform == 'darwin' and platform.machine() == 'arm64', 'REVIEWED_MACOS_ARM64_HOST_REQUIRED')
    binary = Path(binary).resolve(strict=True)
    require(binary.is_file() and os.access(binary, os.X_OK) and sha256(binary) == BINARY_HASH,
            'FROZEN_NATIVE_BINARY_REQUIRED')
    for module in (zavliq, zavliq.client):
        location = Path(module.__file__).resolve(strict=True)
        require(location.is_relative_to(Path(sys.prefix).resolve()) and 'site-packages' in location.parts
                and sha256(location) == SDK_HASHES[module.__name__], 'REVIEWED_INSTALLED_WHEEL_REQUIRED')
    return binary


def read_target(path):
    path = Path(path)
    require(not path.is_symlink() and path.is_file() and path.stat().st_size <= 65536,
            'BOUNDED_TARGET_FILE_REQUIRED')
    require(path.stat().st_mode & 0o077 == 0, 'PRIVATE_TARGET_FILE_REQUIRED')
    return json.loads(path.read_text())


def provenance(target):
    return {'revision': REVISION, 'source_archive_sha256': SOURCE_HASH,
            'production_manifest_sha256': MANIFEST_HASH, 'native_binary_sha256': BINARY_HASH,
            'image_ids': IMAGE_IDS, 'origin': ORIGIN, 'server_name': SERVER}


def target_evidence(target):
    result = {'validated_target_json_sha256': hashlib.sha256(json.dumps(target, sort_keys=True).encode()).hexdigest(),
              'target_machine_id_sha256': base.digest_text(target['target_machine_id']),
              'route_checked_at': target['route_checked_at'], 'machine_route_operator_attested': True}
    if target['phase'] == 'verify':
        result.update(source_machine_id_sha256=base.digest_text(target['source_machine_id']),
                      source_writers_fenced_operator_attested=True,
                      restricted_verification_access_operator_attested=True,
                      encrypted_backup_sha256=target['encrypted_backup_sha256'],
                      restore_completed_at=target['restore_completed_at'])
    return result


def validate_target(target, phase, *, prepared=None, now=None):
    require(isinstance(target, dict) and phase in {'prepare', 'verify'}, 'TARGET_PHASE_REQUIRED')
    require(target.get('environment') == 'aws-production' and target.get('final_candidate') is True
            and target.get('public_verification') is True, 'EXPLICIT_PUBLIC_VERIFICATION_REQUIRED')
    require(target.get('phase') == phase and target.get('origin') == ORIGIN
            and target.get('server_name') == SERVER and target.get('project') == 'zavliq-production',
            'FIXED_PUBLIC_TARGET_REQUIRED')
    require(target.get('revision') == REVISION and target.get('source_archive_sha256') == SOURCE_HASH
            and target.get('production_manifest_sha256') == MANIFEST_HASH
            and target.get('native_binary_sha256') == BINARY_HASH, 'FROZEN_PUBLIC_BUILD_REQUIRED')
    images = target.get('images')
    require(isinstance(images, dict) and set(images) == set(IMAGE_IDS)
            and all(isinstance(images[name], dict) and images[name].get('id') == digest
                    for name, digest in IMAGE_IDS.items()), 'EXACT_PUBLIC_IMAGES_REQUIRED')
    machine = target.get('target_machine_id')
    require(isinstance(machine, str) and re.fullmatch(r'[0-9a-f]{32}', machine) is not None
            and target.get('route_machine_id') == machine, 'EXACT_MACHINE_ROUTE_ATTESTATION_REQUIRED')
    now = time.time() if now is None else now
    checked = base.timestamp(target.get('route_checked_at'))
    require(-60 <= now - checked <= 1800, 'FRESH_ROUTE_ATTESTATION_REQUIRED')
    if phase == 'verify':
        require(target.get('verification_access_restricted') is True, 'VERIFICATION_ACCESS_ATTESTATION_REQUIRED')
        require(prepared is not None and provenance(target) == prepared.get('provenance'),
                'PREPARED_PUBLIC_FIXTURE_REQUIRED')
        require(machine != prepared.get('source_machine_id')
                and target.get('source_machine_id') == prepared.get('source_machine_id')
                and target.get('source_writers_fenced') is True, 'DISTINCT_REPLACEMENT_AND_FENCED_SOURCE_REQUIRED')
        require(isinstance(target.get('encrypted_backup_sha256'), str)
                and HEX64.fullmatch(target['encrypted_backup_sha256']) is not None, 'BACKUP_HASH_REQUIRED')
        restored = base.timestamp(target.get('restore_completed_at'))
        require(base.timestamp(prepared['prepared_at']) <= restored <= checked + 60,
                'RESTORE_TIME_MISMATCH')
    return target


def preflight_discovery():
    """Normal certificate/hostname verification; fixed host, bounded bodies, no redirects/proxy."""
    context = ssl.create_default_context()
    documents = {}
    for path in ('/health', '/.well-known/zavliq', '/.well-known/matrix/client'):
        connection = http.client.HTTPSConnection(SERVER, 443, timeout=10, context=context)
        try:
            connection.request('GET', path, headers={'Accept': 'application/json'})
            response = connection.getresponse()
            require(response.status == 200, 'PUBLIC_HTTPS_STATUS')
            require(bool(response.getheader('Strict-Transport-Security')), 'PUBLIC_HSTS_REQUIRED')
            body = response.read(65537)
            require(len(body) <= 65536, 'DISCOVERY_RESPONSE_TOO_LARGE')
            documents[path] = json.loads(body)
        finally:
            connection.close()
    require(documents['/health'].get('status') == 'ok', 'PUBLIC_CONTROL_NOT_READY')
    discovery, matrix = documents['/.well-known/zavliq'], documents['/.well-known/matrix/client']
    require(discovery.get('protocol') == 'zavliq' and discovery.get('control_url') == ORIGIN
            and discovery.get('homeserver') == ORIGIN and discovery.get('server_name') == SERVER,
            'DISCOVERY_ORIGIN_MISMATCH')
    require(matrix.get('m.homeserver', {}).get('base_url') == ORIGIN, 'DISCOVERY_ORIGIN_MISMATCH')
    return {'certificate_verified': True, 'hostname_verified': True, 'redirects_followed': False,
            'canonical_discovery_verified': True, 'registration_open': discovery.get('registration', {}).get('open') is True}


def identity_metadata(value, handle=None, expected=None):
    result = {key: value.get(key) for key in ('user_id', 'device_id', 'homeserver')}
    require(result['homeserver'] == ORIGIN, 'ENROLLED_ORIGIN_MISMATCH')
    require(isinstance(result['user_id'], str) and result['user_id'].endswith(':' + SERVER)
            and isinstance(result['device_id'], str) and bool(result['device_id']), 'PUBLIC_IDENTITY_REQUIRED')
    if handle is not None:
        require(result['user_id'] == '@' + handle + ':' + SERVER, 'ENROLLED_IDENTITY_MISMATCH')
    if expected is not None:
        require(result == expected, 'ORIGINAL_DEVICE_MISMATCH')
    return result


def attachment(item, encrypted):
    content = item.get('content', {})
    require(content.get('msgtype') == 'm.file', 'FILE_MESSAGE_REQUIRED')
    if encrypted:
        media = content.get('file')
        require('url' not in content and isinstance(media, dict) and set(media) == {'url', 'encrypted'}
                and media['encrypted'] is True, 'REDACTED_ENCRYPTED_ATTACHMENT_REQUIRED')
        url = media['url']
    else:
        require('file' not in content, 'STANDARD_ATTACHMENT_REQUIRED')
        url = content.get('url')
    require(isinstance(url, str) and re.fullmatch(r'mxc://zavliq\.com/[A-Za-z0-9_-]+', url) is not None,
            'PUBLIC_MEDIA_NAMESPACE_REQUIRED')


def driver_hashes(path):
    return {'driver_sha256': sha256(path), 'public_guard_sha256': sha256(__file__),
            'shared_fixture_helpers_sha256': sha256(base.__file__), 'installed_sdk_sha256': SDK_HASHES}


def failure(attempt, error):
    result = attempt | {'status': 'failed', 'ok': False, 'error_code': base.failure_code(error),
                        'failed_at': utc_now(), 'stores_preserved': True}
    for _ in range(10):
        unique = base.secrets.token_hex(8)
        path = EVIDENCE / f"{result['run_id'] or 'invalid-public-run'}-{result['phase']}-failed-{unique}.json"
        try:
            atomic_json(path, result | {'attempt_id': unique}, private=False, replace=False)
            return result
        except FileExistsError:
            continue
    raise ValueError('FAILED_EVIDENCE_FILENAME_COLLISION')
