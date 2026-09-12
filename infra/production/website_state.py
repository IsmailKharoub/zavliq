"""Validate the optional gateway-only release layered over an immutable base bundle."""
import copy
import hashlib
import json
from pathlib import Path
import re
import stat

SCHEMA = 'zavliq.website.v1'
ACTIVE_SCHEMA = 'zavliq.website-active.v1'
ROOT_OWNER = 0
SERVICES = {'gateway', 'synapse', 'control', 'postgres', 'echo'}


def require(value, code):
    if not value:
        raise ValueError(code)


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def protected(path, private=False):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == ROOT_OWNER and
            not stat.S_IMODE(info.st_mode) & (0o077 if private else 0o022), 'PROTECTED_WEBSITE_FILE_REQUIRED')
    return path


def overlay(image_id):
    require(re.fullmatch(r'sha256:[0-9a-f]{64}', image_id) is not None, 'EXACT_WEBSITE_IMAGE_ID_REQUIRED')
    return 'services:\n  gateway:\n    image: ' + json.dumps(image_id) + '\n    pull_policy: never\n    build: !reset null\n'


def verify_release(directory, expected_hash, base, base_hash):
    require(re.fullmatch(r'[0-9a-f]{40}-web-[0-9a-f]{16}', directory.name) is not None,
            'EXACT_WEBSITE_RELEASE_ID_REQUIRED')
    info = directory.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == ROOT_OWNER and not info.st_mode & 0o022,
            'PROTECTED_WEBSITE_DIRECTORY_REQUIRED')
    manifest_path = protected(directory / 'website-manifest.json')
    require(digest(manifest_path) == expected_hash, 'WEBSITE_MANIFEST_HASH_MISMATCH')
    value = json.loads(manifest_path.read_text())
    require(value.get('schema') == SCHEMA and value.get('architecture') == 'linux/amd64' and
            value.get('base_manifest_sha256') == base_hash and value.get('base_images') == base['images'],
            'UNCHANGED_PRODUCTION_BASE_REQUIRED')
    require(set(value['base_images']) == SERVICES and value.get('origin') == 'https://zavliq.com' and
            value.get('echo_user_id') == '@echo:zavliq.com', 'CANONICAL_WEBSITE_CONFIGURATION_REQUIRED')
    revision = value.get('website_revision', '')
    inputs = value.get('build_inputs', {})
    config_hash = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    require(re.fullmatch(r'[0-9a-f]{40}', revision) is not None and
            directory.name == revision + '-web-' + config_hash[:16], 'WEBSITE_BUILD_IDENTITY_MISMATCH')
    image = value.get('gateway', {})
    require(image.get('ref') == 'zavliq-web:' + directory.name and image.get('id') != base['images']['gateway']['id'],
            'DISTINCT_WEBSITE_IMAGE_REQUIRED')
    expected_overlay = overlay(image.get('id', ''))
    require((protected(directory / 'compose.website.yaml')).read_text() == expected_overlay and
            digest(directory / 'compose.website.yaml') == value.get('compose_overlay_sha256'), 'GATEWAY_ONLY_OVERLAY_REQUIRED')
    require(value.get('dist_sha256', {}).get('index.html') and
            re.fullmatch(r'[0-9a-f]{64}', value['dist_sha256']['index.html']) is not None,
            'BUILT_WEBSITE_INDEX_REQUIRED')
    for key in ['source_archive_sha256', 'web_bases_sha256', 'dockerfile_sha256', 'caddyfile_sha256']:
        require(re.fullmatch(r'[0-9a-f]{64}', inputs.get(key, '')) is not None, 'COMPLETE_WEBSITE_BUILD_PROVENANCE_REQUIRED')
    require(inputs.get('base_manifest_sha256') == base_hash and inputs.get('website_revision') == revision,
            'WEBSITE_BUILD_BASE_BINDING_REQUIRED')
    require(inputs.get('previous_gateway') == base['images']['gateway'] and value.get('preserved_assets_sha256') and
            all(name.startswith('assets/') and value['dist_sha256'].get(name) == expected
                for name, expected in value['preserved_assets_sha256'].items()), 'ORIGINAL_BROWSER_ASSETS_REQUIRED')
    return value


def active(paths, release, base=None):
    marker = paths.state / 'website-active.json'
    if not marker.exists() and not marker.is_symlink():
        return None
    protected(marker, private=True)
    record = json.loads(marker.read_text())
    require(record.get('schema') == ACTIVE_SCHEMA and record.get('status') in {'activating', 'ready', 'failed'},
            'KNOWN_WEBSITE_STATE_REQUIRED')
    base_hash = digest(release / 'production-manifest.json')
    require(record.get('base_manifest_sha256') == base_hash, 'WEBSITE_BASE_POINTER_MISMATCH')
    website_id = record.get('website_id', '')
    require(re.fullmatch(r'[0-9a-f]{40}-web-[0-9a-f]{16}', website_id) is not None, 'EXACT_WEBSITE_RELEASE_ID_REQUIRED')
    directory = paths.releases.parent / 'websites' / website_id
    manifest = verify_release(directory, record['manifest_sha256'],
                              base or json.loads((release / 'production-manifest.json').read_text()), base_hash)
    return record, directory, manifest


def effective_manifest(paths, release, base):
    selected = active(paths, release, base)
    if selected is None:
        return base
    record, _, website = selected
    result = copy.deepcopy(base)
    result['images']['gateway'] = website['gateway']
    result['website_status'] = record['status']
    result['website_revision'] = website['website_revision']
    return result


def active_overlay(paths, release):
    selected = active(paths, release)
    return selected[1] / 'compose.website.yaml' if selected else None
