"""Opt-in final-candidate server restore fixture; never imports or copies identities.

Only the fixed, privately forwarded staging origin is accepted. Network/account
operations require --execute; unit tests inject an in-memory SDK instead.
"""
import argparse
import asyncio
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / 'tests/recovery/.local'
EVIDENCE = ROOT / 'tests/recovery/evidence'
ORIGIN = 'http://localhost:28180'
SCHEMA = 'zavliq-e2ee-server-restore-v1'
HEX64 = re.compile(r'[0-9a-f]{64}')
RUN_ID = re.compile(r'recovery-([0-9a-f]{12})-([0-9a-f]{8})')
from zavliq import Zavliq


def require(condition, code):
    if not condition:
        raise ValueError(code)


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def digest_text(value):
    return hashlib.sha256(value.encode()).hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    require(isinstance(value, str), 'TIMESTAMP_REQUIRED')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(parsed.utcoffset() is not None, 'TIMESTAMP_TIMEZONE_REQUIRED')
        return parsed.timestamp()
    except (TypeError, ValueError) as error:
        raise ValueError('INVALID_TIMESTAMP') from error


def provenance(target):
    """Only non-secret build identity is eligible for public evidence."""
    return {key: target[key] for key in (
        'revision', 'source_archive_sha256', 'native_binary_sha256') } | {
        'image_ids': {name: image['id'] for name, image in target['images'].items()}}


def validate_target(target, phase, revision, binary_sha256, *, now=None, prepared=None):
    require(phase in {'prepare', 'verify'}, 'INVALID_PHASE')
    require(re.fullmatch(r'[0-9a-f]{40}', revision or '') is not None, 'EXACT_REVISION_REQUIRED')
    require(target.get('revision') == revision, 'REVISION_MISMATCH')
    require(target.get('final_candidate') is True, 'FINAL_CANDIDATE_REQUIRED')
    require(target.get('environment') == 'aws-staging', 'STAGING_REQUIRED')
    require(target.get('phase') == phase, 'TARGET_PHASE_MISMATCH')
    require(target.get('origin') == ORIGIN, 'FIXED_STAGING_ORIGIN_REQUIRED')
    require(target.get('server_name') == 'localhost', 'SERVER_NAME_MISMATCH')
    project = 'zavliq-load' if phase == 'prepare' else 'zavliq-recovery-' + revision[:12]
    port = 19180 if phase == 'prepare' else 19181
    require(target.get('project') == project, 'DEDICATED_PROJECT_REQUIRED')
    require(type(target.get('remote_port')) is int and target['remote_port'] == port, 'REMOTE_PORT_MISMATCH')
    require(target.get('route_project') == project and target.get('route_remote_port') == port,
            'ROUTE_ATTESTATION_MISMATCH')
    now = time.time() if now is None else now
    checked = timestamp(target.get('route_checked_at'))
    require(-60 <= now - checked <= 1800, 'FRESH_ROUTE_ATTESTATION_REQUIRED')
    require(HEX64.fullmatch(target.get('source_archive_sha256', '')) is not None, 'SOURCE_HASH_REQUIRED')
    require(HEX64.fullmatch(binary_sha256 or '') is not None and
            target.get('native_binary_sha256') == binary_sha256, 'BINARY_HASH_MISMATCH')
    images = target.get('images')
    require(isinstance(images, dict) and {'synapse', 'control', 'gateway', 'postgres'} <= images.keys(),
            'IMAGE_PROVENANCE_REQUIRED')
    for service, image in images.items():
        require(re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', service) is not None and isinstance(image, dict),
                'INVALID_IMAGE_PROVENANCE')
        require(isinstance(image.get('ref'), str) and 0 < len(image['ref']) <= 256 and
                re.fullmatch(r'sha256:[0-9a-f]{64}', image.get('id', '')) is not None,
                'INVALID_IMAGE_PROVENANCE')
    if phase == 'verify':
        require(prepared is not None and prepared.get('schema') == SCHEMA, 'PREPARED_FIXTURE_REQUIRED')
        require(provenance(target) == prepared['provenance'], 'RESTORE_BUILD_MISMATCH')
        require(HEX64.fullmatch(target.get('encrypted_backup_sha256', '')) is not None, 'BACKUP_HASH_REQUIRED')
        restored = timestamp(target.get('restore_completed_at'))
        require(timestamp(prepared['prepared_at']) <= restored <= checked + 60, 'RESTORE_TIME_MISMATCH')
    return target


def atomic_json(path, value, *, private=True, replace=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700 if private else 0o755)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.chmod(temporary, 0o600 if private else 0o644)
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
            if replace:
                os.replace(temporary, path)
            else:
                os.link(temporary, path)  # Atomic publication; an existing record is never overwritten.
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def exclusive_lock(path):
    path = Path(path)
    require(not path.is_symlink(), 'SYMLINK_REFUSED')
    with path.open('a+b') as stream:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('CLIENT_OR_FIXTURE_STILL_RUNNING') from error
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def private_directory(path):
    require(not path.is_symlink(), 'SYMLINK_REFUSED')
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    require(path.stat().st_mode & 0o077 == 0, 'PRIVATE_DIRECTORY_PERMISSIONS_REQUIRED')


def stores_closed(base):
    # Native fs2 and this guard use the same advisory Unix flock primitive.
    for name in ('a', 'b'):
        path = base / name
        require(path.is_dir() and not path.is_symlink(), 'ORIGINAL_CLIENT_STORE_REQUIRED')
        with exclusive_lock(path / 'runtime.lock'):
            pass


def absent_events(base, event_ids):
    """Read counts only, with no identity/token/content read or database mutation."""
    database = base / 'b/inbox.sqlite3'
    require(database.is_file() and not database.is_symlink(), 'ORIGINAL_INBOX_REQUIRED')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
        for event_id in event_ids:
            require(connection.execute('SELECT count(*) FROM events WHERE event_id=?', (event_id,)).fetchone()[0] == 0,
                    'HISTORICAL_EVENT_ALREADY_CACHED')


def identity_digest(base):
    result = {}
    for name in ('a', 'b'):
        path = base / name / 'identity.json'
        require(path.is_file() and not path.is_symlink(), 'ORIGINAL_IDENTITY_REQUIRED')
        result[name] = sha256(path)
    return result


def identity_metadata(value, handle=None, expected=None):
    result = {key: value.get(key) for key in ('user_id', 'device_id', 'homeserver')}
    require(result['homeserver'] == ORIGIN, 'ENROLLED_ORIGIN_MISMATCH')
    require(isinstance(result['user_id'], str) and result['user_id'].endswith(':localhost'), 'ENROLLED_SERVER_MISMATCH')
    require(isinstance(result['device_id'], str) and result['device_id'], 'DEVICE_ID_REQUIRED')
    if handle:
        require(result['user_id'] == '@' + handle + ':localhost', 'ENROLLED_IDENTITY_MISMATCH')
    if expected:
        require(result == expected, 'ORIGINAL_DEVICE_MISMATCH')
    return result


async def exact_room(client, room_id):
    rooms = (await client.call('conversations'))['items']
    room = next((item for item in rooms if item['room_id'] == room_id), None)
    require(room is not None and room.get('kind') == 'dm' and room.get('encrypted') is True and
            room.get('encryption') == 'e2ee' and room.get('membership') == 'joined' and
            room.get('joined_member_count') == 2 and room.get('invited_member_count') == 0,
            'EXACT_JOINED_E2EE_DM_REQUIRED')


async def trust_devices(a, b, identities):
    own = {}
    for name, client in [('a', a), ('b', b)]:
        records = (await client.call('crypto_devices'))['devices']
        own[name] = next(device for device in records if device['device_id'] == identities[name]['device_id'])
    for client, peer in [(a, 'b'), (b, 'a')]:
        user_id = identities[peer]['user_id']
        await client.call('crypto_devices', {'user_id': user_id})
        result = await client.call('verify_device', {'user_id': user_id,
            'device_id': own[peer]['device_id'], 'ed25519': own[peer]['ed25519']})
        require(result.get('verified') is True, 'DEVICE_VERIFICATION_FAILED')


async def find_event(client, room_id, event_id, *, attempts=12):
    # Full pagination avoids accidentally treating a preview or the first page
    # as history. Re-read from zero after sync so late decryption can be found.
    for _ in range(attempts):
        await client.call('sync', {'wait_seconds': 0})
        cursor = 0
        for _ in range(100):
            page = await client.call('inbox', {'room_id': room_id, 'cursor': cursor,
                'limit': 100, 'full': True, 'sync': False})
            for item in page['items']:
                if item['event_id'] == event_id and 'algorithm' not in item.get('content', {}):
                    require(item.get('room_id') == room_id, 'EVENT_ROOM_MISMATCH')
                    return item
            if not page.get('has_more'):
                break
            require(page['next_cursor'] > cursor, 'INBOX_CURSOR_DID_NOT_ADVANCE')
            cursor = page['next_cursor']
        await asyncio.sleep(0.25)
    raise ValueError('DECRYPTED_EVENT_NOT_OBSERVED')


def require_message(item, sender, *, text=None, data_json=None):
    require(item.get('sender') == sender, 'EVENT_SENDER_MISMATCH')
    if text is not None:
        require(item.get('content', {}).get('body') == text, 'TEXT_CONTENT_MISMATCH')
    if data_json is not None:
        require(item.get('data_json') == data_json, 'JSON_CONTENT_MISMATCH')


def require_encrypted_attachment(item):
    # Full inbox results deliberately redact media keys. The private native
    # event retains the descriptor used by the explicit cache-disabled download.
    content = item.get('content', {})
    media = content.get('file')
    require(content.get('msgtype') == 'm.file' and 'url' not in content and
            isinstance(media, dict) and set(media) == {'url', 'encrypted'} and
            media.get('encrypted') is True and isinstance(media.get('url'), str) and
            re.fullmatch(r'mxc://localhost/[A-Za-z0-9_-]+', media['url']) is not None,
            'REDACTED_ENCRYPTED_ATTACHMENT_REQUIRED')


def event_id(result):
    value = result.get('event_id')
    require(isinstance(value, str) and value.startswith('$'), 'ACCEPTED_EVENT_ID_REQUIRED')
    return value


def public_evidence(state, phase):
    result = {key: state[key] for key in ('schema', 'run_id', 'provenance', 'identities', 'room_id',
        'prepared_at', 'historical_events', 'content_hashes')}
    result.update(phase=phase, status='prepared' if phase == 'prepare' else 'passed',
        origin=ORIGIN, server_name='localhost', clients_closed=True, room_encryption='e2ee',
        fingerprints_authenticated_by='fixture owning both original device stores',
        routing_proof='operator attestation; identical restored services cannot self-distinguish by HTTP health',
        historical_recipient_cache_empty_at_prepare=True)
    if phase == 'verify':
        result.update(verified_at=state['verified_at'],
            source_project='zavliq-load', restore_project=state['restore']['project'],
            encrypted_backup_sha256=state['restore']['encrypted_backup_sha256'],
            restore_completed_at=state['restore']['restore_completed_at'],
            historical_recipient_cache_empty_before_first_restore_sync=True,
            original_user_and_device_ids_preserved=True,
            historical_text_and_exact_json_decrypted=True, encrypted_attachment_download_hash_matched=True,
            encrypted_attachment_inbox_keys_redacted=True,
            new_e2ee_roundtrip_events=state['new_events'],
            verification_seconds=state['verification_seconds'])
    return result


class Fixture:
    def __init__(self, base, binary, factory=Zavliq):
        self.base, self.binary, self.factory = Path(base), str(binary), factory
        self.state_path = self.base / 'state.json'

    def save(self, state):
        atomic_json(self.state_path, state)

    def client(self, name):
        return self.factory(binary=self.binary, data_dir=str(self.base / name), control_url=ORIGIN, timeout=120)

    async def prepare(self, target, run_id):
        require(not self.state_path.exists(), 'PREPARE_ALREADY_STARTED_PRESERVE_EXISTING_STORES')
        for name in ('a', 'b'):
            path = self.base / name
            require(not path.exists(), 'FRESH_FIXTURE_DIRECTORIES_REQUIRED')
            private_directory(path)
        suffix = RUN_ID.fullmatch(run_id).group(2)
        state = {'schema': SCHEMA, 'run_id': run_id, 'phase': 'preparing', 'provenance': provenance(target),
            'handles': {'a': 'arden-' + suffix, 'b': 'mira-' + suffix},
            'created_at': utc_now(), 'historical_events': {}, 'new_events': {},
            'text': 'Synthetic retained E2EE fixture ' + run_id,
            'data_json': '{ "exact": 1.0000000000000000001, "count": 9007199254740993 }'}
        self.save(state)
        payload = self.base / 'source.bin'
        with payload.open('xb') as stream:
            os.chmod(payload, 0o600)
            stream.write(b'Zavliq synthetic encrypted restore fixture\x00' + secrets.token_bytes(512))
            stream.flush()
            os.fsync(stream.fileno())
        a, b = self.client('a'), self.client('b')
        try:
            state['identities'] = {}
            for name, client in [('a', a), ('b', b)]:
                state['identities'][name] = identity_metadata(await client.init(state['handles'][name]), state['handles'][name])
                self.save(state)
            require(state['identities']['a']['user_id'] != state['identities']['b']['user_id'], 'TWO_IDENTITIES_REQUIRED')
            room = await a.create_conversation([state['identities']['b']['user_id']], encryption='e2ee')
            state['room_id'] = room['room_id']
            self.save(state)
            await b.call('accept', {'room_id': state['room_id']})
            await exact_room(a, state['room_id'])
            await exact_room(b, state['room_id'])
            await trust_devices(a, b, state['identities'])
            # A has never sent in this room. B goes offline before A creates its
            # outbound Megolm session: restore must deliver B's queued room keys.
            baseline = await b.send(state['room_id'], text='Synthetic pre-backup trust check', idempotency_key=run_id + '-baseline')
            require_message(await find_event(a, state['room_id'], event_id(baseline)), state['identities']['b']['user_id'],
                            text='Synthetic pre-backup trust check')
            await b.close()
            state['recipient_closed_before_historical_send_at'] = utc_now()
            self.save(state)
            state['historical_events']['text'] = event_id(await a.send(state['room_id'], text=state['text'], idempotency_key=run_id + '-old-text'))
            self.save(state)
            state['historical_events']['json'] = event_id(await a.send(state['room_id'], data_json=state['data_json'], idempotency_key=run_id + '-old-json'))
            self.save(state)
            state['historical_events']['file'] = event_id(await a.call('upload', {'room_id': state['room_id'], 'path': str(payload), 'idempotency_key': run_id + '-old-file'}))
            self.save(state)
        finally:
            await a.close()
            await b.close()
        stores_closed(self.base)
        absent_events(self.base, state['historical_events'].values())
        state['identity_file_hashes'] = identity_digest(self.base)  # Private integrity check only.
        state['content_hashes'] = {'text_sha256': digest_text(state['text']), 'json_sha256': digest_text(state['data_json']), 'file_sha256': sha256(payload)}
        state.update(phase='prepared', prepared_at=utc_now())
        self.save(state)
        return public_evidence(state, 'prepare')

    async def verify(self, target, state):
        require(state['phase'] in {'prepared', 'verifying'}, 'PREPARED_OR_RESUMABLE_VERIFY_REQUIRED')
        stores_closed(self.base)
        require(identity_digest(self.base) == state['identity_file_hashes'], 'ORIGINAL_IDENTITY_FILES_CHANGED')
        restore = {key: target[key] for key in ('project', 'encrypted_backup_sha256', 'restore_completed_at')}
        if state['phase'] == 'prepared':
            absent_events(self.base, state['historical_events'].values())
            state.update(phase='verifying', restore=restore, first_verify_at=utc_now(), historical_cache_empty_before_verify=True)
            self.save(state)  # Before starting either background-syncing runtime.
        else:
            require(state.get('restore') == restore and state.get('historical_cache_empty_before_verify') is True,
                    'VERIFY_RESUME_TARGET_MISMATCH')
        started = time.monotonic()
        a, b = self.client('a'), self.client('b')
        try:
            for name, client in [('a', a), ('b', b)]:
                identity_metadata(await client.identity(), expected=state['identities'][name])
                await exact_room(client, state['room_id'])
            sender = state['identities']['a']['user_id']
            require_message(await find_event(b, state['room_id'], state['historical_events']['text']), sender, text=state['text'])
            require_message(await find_event(b, state['room_id'], state['historical_events']['json']), sender, data_json=state['data_json'])
            old_file = await find_event(b, state['room_id'], state['historical_events']['file'])
            require_message(old_file, sender)
            require_encrypted_attachment(old_file)
            # Always a new path. A resumed verification cannot pass by reusing a
            # previously downloaded plaintext file. SDK media caching is disabled.
            output = self.base / ('restored-' + secrets.token_hex(6) + '.bin')
            await b.call('download', {'event_id': state['historical_events']['file'], 'path': str(output)})
            require(sha256(output) == state['content_hashes']['file_sha256'], 'RESTORED_FILE_HASH_MISMATCH')
            state['historical_verified_at'] = utc_now()
            self.save(state)
            # Existing trust/key state must work without re-verifying/re-pairing.
            for name, sender_client, receiver_client, peer in [('a', a, b, 'b'), ('b', b, a, 'a')]:
                text = 'Synthetic post-restore reply from ' + name
                result = await sender_client.send(state['room_id'], text=text, data_json=state['data_json'],
                    idempotency_key=state['run_id'] + '-new-' + name)
                state['new_events'][name + '_to_' + peer] = event_id(result)
                self.save(state)
                require_message(await find_event(receiver_client, state['room_id'], event_id(result)),
                    state['identities'][name]['user_id'], text=text, data_json=state['data_json'])
        finally:
            await a.close()
            await b.close()
        stores_closed(self.base)
        finished = utc_now()
        state.update(phase='verified', verified_at=finished,
            verification_seconds=round(timestamp(finished) - timestamp(state['first_verify_at']), 3),
            last_attempt_seconds=round(time.monotonic() - started, 3))
        self.save(state)
        return public_evidence(state, 'verify')


def failure_code(error):
    # Never emit exception messages containing URLs, payloads, tokens or keys.
    code = getattr(error, 'code', None)
    if isinstance(code, str) and re.fullmatch(r'[A-Z_]{1,100}', code):
        return code
    if isinstance(error, ValueError) and re.fullmatch(r'[A-Z_]{1,100}', str(error)):
        return str(error)
    return type(error).__name__


def record_failed_attempt(attempt, error):
    result = attempt | {'status': 'failed', 'failed_at': utc_now(),
        'error_code': failure_code(error), 'stores_preserved': True}
    for _ in range(10):
        result['attempt_id'] = secrets.token_hex(8)
        filename = f"{result['run_id'] or 'invalid-run'}-{result['phase']}-failed-{result['attempt_id']}.json"
        try:
            atomic_json(EVIDENCE / filename, result, private=False, replace=False)
            return result
        except FileExistsError:
            continue
    raise ValueError('FAILED_EVIDENCE_FILENAME_COLLISION')


async def execute(args):
    require(args.execute is True, 'EXECUTION_NOT_ENABLED_NO_NETWORK_PERFORMED')
    # Whitelist metadata before validation: invalid CLI values must not become
    # filenames, public URLs or arbitrary strings in failed-attempt evidence.
    attempt = {'schema': SCHEMA, 'phase': args.phase if args.phase in {'prepare', 'verify'} else 'unknown',
        'run_id': args.run_id if isinstance(args.run_id, str) and RUN_ID.fullmatch(args.run_id) else None,
        'requested_revision': args.revision if isinstance(args.revision, str) and re.fullmatch(r'[0-9a-f]{40}', args.revision) else None,
        'started_at': utc_now(), 'driver_sha256': sha256(Path(__file__))}
    try:
        return await execute_phase(args, attempt)
    except Exception as error:
        return record_failed_attempt(attempt, error)


async def execute_phase(args, attempt):
    match = RUN_ID.fullmatch(args.run_id or '')
    require(match is not None and match.group(1) == args.revision[:12], 'REVISION_BOUND_RUN_ID_REQUIRED')
    binary = args.binary.resolve(strict=True)
    require(binary.is_file() and os.access(binary, os.X_OK), 'EXECUTABLE_BINARY_REQUIRED')
    attempt['native_binary_sha256'] = sha256(binary)
    target = json.loads(args.target.read_text())
    base = PRIVATE / args.run_id
    private_directory(PRIVATE)
    private_directory(base)
    fixture = Fixture(base, binary)
    with exclusive_lock(base / 'fixture.lock'):
        state = json.loads(fixture.state_path.read_text()) if fixture.state_path.exists() else None
        if state is not None:
            require(state.get('run_id') == args.run_id and state.get('schema') == SCHEMA, 'FIXTURE_STATE_MISMATCH')
        validate_target(target, args.phase, args.revision, attempt['native_binary_sha256'], prepared=state)
        attempt['provenance'] = provenance(target)
        if args.phase == 'prepare':
            result = await fixture.prepare(target, args.run_id)
        else:
            result = await fixture.verify(target, state)
        result['driver_sha256'] = attempt['driver_sha256']
        atomic_json(EVIDENCE / (args.run_id + '-' + args.phase + '.json'), result, private=False)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['prepare', 'verify'])
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--execute', action='store_true', help='Explicitly execute this phase after the final build and routing are authorized.')
    args = parser.parse_args()
    try:
        result = asyncio.run(execute(args))
        print(json.dumps(result, sort_keys=True))
        if result.get('status') == 'failed':
            raise SystemExit(1)
    except Exception as error:
        print(json.dumps({'phase': args.phase, 'status': 'failed', 'error_code': failure_code(error), 'stores_preserved': True}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
