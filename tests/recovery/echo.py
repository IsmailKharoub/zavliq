"""Explicit peer-only check of the final private AWS Echo; never starts a responder."""
import argparse
import asyncio
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('recovery_echo_guard', ROOT / 'tests/recovery/verify.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
from zavliq import Zavliq

PRIVATE = ROOT / 'tests/recovery/.local'
EVIDENCE = ROOT / 'tests/recovery/evidence'
RUN_ID = re.compile(r'echo-([0-9a-f]{12})-([0-9a-f]{8})')
OWNER = '@echo:localhost'
REPLY_TIMEOUT = 60
RAW = '{"decimal":0.95000000000000000000000001,"integer":123456789012345678901234567890}'


def preflight_discovery():
    """Fixed direct socket, no environment proxy, redirects, or response URLs followed."""
    documents = {}
    for path in ('/.well-known/zavliq', '/.well-known/matrix/client'):
        connection = http.client.HTTPConnection('localhost', 28180, timeout=10)
        try:
            connection.request('GET', path, headers={'Accept': 'application/json'})
            response = connection.getresponse()
            guard.require(response.status == 200, 'DISCOVERY_HTTP_STATUS')
            body = response.read(65537)
            guard.require(len(body) <= 65536, 'DISCOVERY_RESPONSE_TOO_LARGE')
            documents[path] = json.loads(body)
        finally:
            connection.close()
    discovery = documents['/.well-known/zavliq']
    matrix = documents['/.well-known/matrix/client']
    guard.require(isinstance(discovery, dict) and discovery.get('control_url') == guard.ORIGIN
                  and discovery.get('homeserver') == guard.ORIGIN
                  and discovery.get('server_name') == 'localhost', 'DISCOVERY_ORIGIN_MISMATCH')
    guard.require(isinstance(matrix, dict) and isinstance(matrix.get('m.homeserver'), dict)
                  and matrix['m.homeserver'].get('base_url') == guard.ORIGIN,
                  'DISCOVERY_ORIGIN_MISMATCH')


async def room_messages(peer, room_id):
    """Drain every actual page. Incomplete history cannot establish exact counts."""
    cursor, rows, seen = 0, [], set()
    for page_index in range(100):
        page = await peer.call('inbox', {'room_id': room_id, 'cursor': cursor,
            'full': True, 'limit': 100, 'sync': page_index == 0})
        guard.require(room_id not in page.get('history_gap_rooms', []), 'ECHO_HISTORY_INCOMPLETE')
        guard.require(isinstance(page.get('items'), list) and type(page.get('has_more')) is bool,
                      'INVALID_INBOX_PAGE')
        for row in page['items']:
            guard.require(row.get('room_id') == room_id, 'INBOX_ROOM_MISMATCH')
            event = guard.event_id(row)
            guard.require(event not in seen, 'DUPLICATE_INBOX_EVENT')
            seen.add(event)
            rows.append(row)
        if not page['has_more']:
            return rows
        following = page.get('next_cursor')
        guard.require(type(following) is int and following > cursor, 'INBOX_CURSOR_DID_NOT_ADVANCE')
        cursor = following
    raise ValueError('INBOX_PAGE_LIMIT_EXCEEDED')


def account_replies(rows, expected, observed):
    """Every Echo message must correspond to exactly one expected original."""
    matches = {}
    for row in rows:
        if row.get('sender') != OWNER:
            continue
        content = row.get('content', {})
        relation = content.get('m.relates_to', {}).get('m.in_reply_to', {}).get('event_id')
        guard.require(relation in expected, 'UNEXPECTED_ECHO_MESSAGE')
        guard.require(relation not in matches, 'DUPLICATE_ECHO_REPLY')
        wanted = expected[relation]
        guard.require(content.get('body') == wanted['text'], 'ECHO_TEXT_MISMATCH')
        guard.require(content.get('com.zavliq.data_json') == wanted.get('data_json'), 'ECHO_JSON_MISMATCH')
        event = guard.event_id(row)
        guard.require(relation not in observed or observed[relation] == event, 'ECHO_REPLY_CHANGED')
        matches[relation] = event
    guard.require(observed.keys() <= matches.keys(), 'ECHO_REPLY_DISAPPEARED')
    return matches


async def await_reply(peer, room, original, expected, observed):
    deadline = time.monotonic() + REPLY_TIMEOUT
    while time.monotonic() < deadline:
        matches = account_replies(await room_messages(peer, room), expected, observed)
        if original in matches:
            observed.update(matches)
            return matches[original]
        await asyncio.sleep(.25)
    raise ValueError('ECHO_REPLY_TIMEOUT')


async def verify_peer(peer, handle, run_id, result):
    identity = guard.identity_metadata(await peer.init(handle, 'Finch'), handle)
    result['peer_user_id'] = identity['user_id']
    room = (await peer.create_conversation([OWNER], encryption='standard'))['room_id']
    result['room_id'] = room
    params = {'room_id': room, 'text': 'Hello from Finch', 'data_json': RAW,
              'idempotency_key': run_id + '-literal'}
    sent = guard.event_id(await peer.call('send', params))
    result['original_event_id'] = sent
    duplicate = guard.event_id(await peer.call('send', params))
    guard.require(duplicate == sent, 'SEND_RETRY_CHANGED_EVENT')
    expected = {sent: {'text': params['text'], 'data_json': RAW}}
    observed = {}
    result['echo_event_id'] = await await_reply(peer, room, sent, expected, observed)
    result['checks'].append('standard_text_exact_json_and_stable_retry')

    encrypted = (await peer.create_conversation([OWNER], encryption='e2ee'))['room_id']
    result['encrypted_room_id'] = encrypted
    group = (await peer.create_conversation([OWNER], kind='group', encryption='standard'))['room_id']
    result['group_room_id'] = group
    result['barriers'] = []
    # Only send the second original after receiving the first barrier's reply.
    # It cannot be part of the responder's earlier captured inbox page, proving
    # a subsequent processing cycle after the unsupported invitations exist.
    for number in (1, 2):
        text = f'Finch is still here after the invitations: {number}'
        original = guard.event_id(await peer.call('send', {'room_id': room, 'text': text,
            'idempotency_key': run_id + f'-barrier-{number}'}))
        guard.require(original not in expected, 'BARRIER_EVENT_NOT_UNIQUE')
        expected[original] = {'text': text}
        barrier = {'original_event_id': original}
        result['barriers'].append(barrier)
        barrier['reply_event_id'] = await await_reply(peer, room, original, expected, observed)

    rooms = {row['room_id']: row for row in (await peer.call('conversations'))['items']}
    for room_id in (encrypted, group):
        row = rooms.get(room_id, {})
        guard.require(row.get('joined_member_count') == 1 and row.get('invited_member_count') == 1,
                      'ECHO_ACCEPTED_UNSUPPORTED_INVITE')
    result['checks'].append('encrypted_and_group_invitations_unaccepted_after_two_fresh_replies')
    final = account_replies(await room_messages(peer, room), expected, observed)
    guard.require(final.keys() == expected.keys() and len(final) == 3, 'ECHO_REPLY_COUNT_MISMATCH')
    result['checks'].append('one_exact_reply_per_original_across_complete_room_history')
    result['echo_message_count'] = len(final)


def failed_attempt(result, error):
    """Use the recovery guard's sanitization and immutable per-attempt publication."""
    result.update(ok=False, status='failed', finished_at=guard.utc_now(),
                  error_code=guard.failure_code(error), stores_preserved=True)
    for _ in range(10):
        attempt = result | {'attempt_id': guard.secrets.token_hex(8)}
        path = EVIDENCE / f"{result['run_id'] or 'invalid-run'}-failed-{attempt['attempt_id']}.json"
        try:
            guard.atomic_json(path, attempt, private=False, replace=False)
            return attempt
        except FileExistsError:
            continue
    raise ValueError('FAILED_EVIDENCE_FILENAME_COLLISION')


async def run(args):
    guard.require(args.execute, 'EXECUTE_REQUIRED')
    os.umask(0o077)
    match = RUN_ID.fullmatch(args.run_id or '')
    revision = args.revision if re.fullmatch(r'[0-9a-f]{40}', args.revision or '') else None
    result = {'schema': 'zavliq-final-echo-v1', 'run_id': args.run_id if match else None,
              'requested_revision': revision, 'started_at': guard.utc_now(),
              'driver_sha256': guard.sha256(__file__), 'ok': False, 'checks': []}
    try:
        guard.require(match is not None and revision is not None and match.group(1) == revision[:12],
                      'REVISION_BOUND_RUN_ID_REQUIRED')
        binary = args.binary.resolve(strict=True)
        guard.require(binary.is_file() and os.access(binary, os.X_OK), 'EXECUTABLE_BINARY_REQUIRED')
        binary_hash = guard.sha256(binary)
        result['native_binary_sha256'] = binary_hash
        target = json.loads(args.target.read_text())
        guard.validate_target(target, 'prepare', revision, binary_hash)
        result['provenance'] = guard.provenance(target)
        guard.private_directory(PRIVATE)
        folder = PRIVATE / args.run_id
        guard.require(not folder.exists() and not folder.is_symlink(), 'FIXTURE_ALREADY_EXISTS')
        guard.require(not (EVIDENCE / (args.run_id + '.json')).exists(), 'EVIDENCE_ALREADY_EXISTS')
        folder.mkdir(mode=0o700, exist_ok=False)
        await asyncio.to_thread(preflight_discovery)
        result['fixed_origin_discovery_verified'] = True
        guard.validate_target(target, 'prepare', revision, binary_hash)
        async with Zavliq(binary=str(binary), data_dir=str(folder / 'peer'),
                          control_url=guard.ORIGIN, timeout=90) as peer:
            await verify_peer(peer, 'finch-' + match.group(2), args.run_id, result)
        result.update(ok=True, status='passed', finished_at=guard.utc_now(),
                      clients_closed=True, stores_preserved=True)
        guard.atomic_json(EVIDENCE / (args.run_id + '.json'), result, private=False, replace=False)
        return result
    except Exception as error:
        return failed_attempt(result, error)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--execute', action='store_true')
    try:
        result = asyncio.run(run(parser.parse_args()))
        print(json.dumps(result), flush=True)
        if not result['ok']:
            sys.exit(1)
    except Exception as error:
        print(json.dumps({'ok': False, 'error_code': guard.failure_code(error), 'stores_preserved': True}), flush=True)
        sys.exit(1)
