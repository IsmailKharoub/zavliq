"""One public Finch identity checks Echo, then reuses that original device after restore."""
import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time

spec = importlib.util.spec_from_file_location('public_echo_guard', Path(__file__).with_name('public_guard.py'))
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
spec = importlib.util.spec_from_file_location('public_echo_paging', Path(__file__).with_name('echo.py'))
paging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(paging)
Zavliq = guard.Zavliq
RUN_ID = re.compile(r'public-echo-(2f76469b507c)-([0-9a-f]{8})')
RAW = '{"decimal":0.95000000000000000000000001,"integer":123456789012345678901234567890}'
REPLY_TIMEOUT = 60


def replies(rows, expected, observed):
    matches = {}
    for row in rows:
        if row.get('sender') != guard.OWNER:
            continue
        content = row.get('content', {})
        relation = content.get('m.relates_to', {}).get('m.in_reply_to', {}).get('event_id')
        guard.require(relation in expected, 'UNEXPECTED_ECHO_MESSAGE')
        guard.require(relation not in matches, 'DUPLICATE_ECHO_REPLY')
        guard.require(content.get('body') == expected[relation]['text'] and
                      content.get('com.zavliq.data_json') == expected[relation].get('data_json'),
                      'ECHO_LITERAL_CONTENT_MISMATCH')
        event = guard.event_id(row)
        guard.require(relation not in observed or observed[relation] == event, 'ECHO_REPLY_CHANGED')
        matches[relation] = event
    guard.require(observed.keys() <= matches.keys(), 'ECHO_REPLY_DISAPPEARED')
    return matches


async def await_reply(peer, state, original):
    deadline = time.monotonic() + REPLY_TIMEOUT
    while time.monotonic() < deadline:
        found = replies(await paging.room_messages(peer, state['room_id']), state['expected'], state['observed'])
        if original in found:
            state['observed'].update(found)
            return found[original]
        await asyncio.sleep(.25)
    raise ValueError('ECHO_REPLY_TIMEOUT')


async def public_devices(peer):
    values = (await peer.call('crypto_devices', {'user_id': guard.OWNER}))['devices']
    result = sorted(({'device_id': item['device_id'], 'ed25519': item['ed25519']} for item in values),
                    key=lambda item: item['device_id'])
    guard.require(bool(result) and all(item['device_id'] and item['ed25519'] for item in result),
                  'ECHO_PUBLIC_DEVICE_KEYS_REQUIRED')
    return result


async def send_barrier(peer, state, phase, number, save):
    # Journal intent before the RPC: acceptance can outlive a lost response.
    intents = state.setdefault('barrier_intents', {}).setdefault(phase, {})
    intent = intents.setdefault(str(number), {})
    save()
    text = f'Finch public {phase} processing barrier {number}'
    original = guard.event_id(await peer.call('send', {'room_id': state['room_id'], 'text': text,
        'idempotency_key': state['run_id'] + f'-{phase}-barrier-{number}'}))
    guard.require(intent.get('event_id', original) == original, 'SEND_RETRY_CHANGED_EVENT')
    intent['event_id'] = original
    state['expected'][original] = {'text': text}
    save()
    return original


async def reconcile_barriers(peer, state, phase, save):
    intents = state.get('barrier_intents', {}).get(phase, {})
    guard.require(set(intents) in (set(), {'1'}, {'1', '2'}), 'INVALID_BARRIER_INTENTS')
    # Only previously attempted sends may be replayed ahead of history reads.
    # A second intent exists only after the first reply was observed and saved.
    for number in (1, 2):
        if str(number) in intents:
            await send_barrier(peer, state, phase, number, save)


async def barriers(peer, state, phase, save):
    for number in (1, 2):
        original = await send_barrier(peer, state, phase, number, save)
        await await_reply(peer, state, original)
        save()


async def unsupported_still_pending(peer, state):
    rooms = {row['room_id']: row for row in (await peer.call('conversations'))['items']}
    for key in ('encrypted_room_id', 'group_room_id'):
        row = rooms.get(state[key], {})
        guard.require(row.get('joined_member_count') == 1 and row.get('invited_member_count') == 1,
                      'ECHO_ACCEPTED_UNSUPPORTED_INVITE')


async def execute(args):
    guard.require(args.execute is True, 'EXECUTION_NOT_ENABLED_NO_NETWORK_PERFORMED')
    os.umask(0o077)
    match = RUN_ID.fullmatch(args.run_id or '')
    attempt = {'schema': 'zavliq-public-echo-v1', 'run_id': args.run_id if match else None,
               'phase': args.phase if args.phase in ('prepare', 'verify') else 'invalid',
               'started_at': guard.utc_now(), **guard.driver_hashes(__file__),
               'paging_helper_sha256': guard.sha256(paging.__file__)}
    try:
        guard.require(match is not None and args.revision == guard.REVISION, 'REVISION_BOUND_PUBLIC_RUN_REQUIRED')
        binary = guard.artifacts(args.binary)
        target = guard.read_target(args.target)
        folder = guard.PRIVATE / args.run_id
        guard.require(not folder.is_symlink(), 'SYMLINK_REFUSED')
        state_path = folder / 'state.json'
        guard.require(not state_path.is_symlink(), 'SYMLINK_REFUSED')
        state = json.loads(state_path.read_text()) if state_path.is_file() else None
        guard.validate_target(target, args.phase, prepared=state)
        if args.phase == 'prepare':
            guard.require(not folder.exists() and not folder.is_symlink(), 'FRESH_PUBLIC_ECHO_STORE_REQUIRED')
        else:
            guard.require(state is not None and state.get('phase') in {'prepared', 'verifying'}
                          and state.get('run_id') == args.run_id, 'PREPARED_PUBLIC_ECHO_REQUIRED')
            guard.private_directory(folder)
            guard.private_directory(folder / 'peer')
        attempt['provenance'] = guard.provenance(target)
        attempt['target_attestation'] = guard.target_evidence(target)
        discovery = await asyncio.to_thread(guard.preflight_discovery)
        guard.validate_target(target, args.phase, prepared=state)
        if args.phase == 'prepare':
            guard.require(discovery['registration_open'], 'PUBLIC_REGISTRATION_CLOSED')
            guard.private_directory(guard.PRIVATE)
            folder.mkdir(mode=0o700)
            state = {'run_id': args.run_id, 'phase': 'preparing', 'provenance': guard.provenance(target),
                     'source_machine_id': target['target_machine_id'], 'expected': {}, 'observed': {}}
        def save(): guard.atomic_json(state_path, state)
        with guard.exclusive_lock(folder / 'fixture.lock'):
            if args.phase == 'verify':
                with guard.exclusive_lock(folder / 'peer/runtime.lock'):
                    pass
                guard.require(not (folder / 'peer/identity.json').is_symlink(), 'SYMLINK_REFUSED')
                guard.require(guard.sha256(folder / 'peer/identity.json') == state['identity_sha256'],
                              'ORIGINAL_IDENTITY_FILES_CHANGED')
                restore = {key: target[key] for key in ('target_machine_id', 'encrypted_backup_sha256', 'restore_completed_at')}
                guard.require(state.get('restore', restore) == restore, 'VERIFY_RESUME_TARGET_MISMATCH')
                state.update(phase='verifying', restore=restore)
            save()
            async with Zavliq(binary=str(binary), data_dir=str(folder / 'peer'),
                              control_url=guard.ORIGIN, timeout=90) as peer:
                if args.phase == 'prepare':
                    handle = 'finch-' + match.group(2)
                    state['identity'] = guard.identity_metadata(await peer.init(handle, 'Finch'), handle)
                    save()
                    state['room_id'] = (await peer.create_conversation([guard.OWNER], encryption='standard'))['room_id']
                    save()
                    params = {'room_id': state['room_id'], 'text': 'Hello from Finch', 'data_json': RAW,
                              'idempotency_key': args.run_id + '-literal'}
                    original = guard.event_id(await peer.call('send', params))
                    state['literal_event_id'] = original
                    state['expected'][original] = {'text': params['text'], 'data_json': RAW}
                    save()
                    guard.require(guard.event_id(await peer.call('send', params)) == original, 'SEND_RETRY_CHANGED_EVENT')
                    await await_reply(peer, state, original)
                    state['encrypted_room_id'] = (await peer.create_conversation([guard.OWNER], encryption='e2ee'))['room_id']
                    save()
                    state['group_room_id'] = (await peer.create_conversation([guard.OWNER], kind='group', encryption='standard'))['room_id']
                    save()
                    state['echo_devices'] = await public_devices(peer)
                else:
                    guard.identity_metadata(await peer.identity(), expected=state['identity'])
                    guard.require(await public_devices(peer) == state['echo_devices'], 'ORIGINAL_ECHO_DEVICES_CHANGED')
                    await reconcile_barriers(peer, state, args.phase, save)
                    found = replies(await paging.room_messages(peer, state['room_id']), state['expected'], state['observed'])
                    prepared = state.get('prepared_event_ids', [])
                    guard.require(len(prepared) == 3 and len(set(prepared)) == 3 and set(prepared) <= found.keys(),
                                  'RESTORED_ECHO_HISTORY_INCOMPLETE')
                await barriers(peer, state, args.phase, save)
                await unsupported_still_pending(peer, state)
                found = replies(await paging.room_messages(peer, state['room_id']), state['expected'], state['observed'])
                guard.require(found.keys() == state['expected'].keys() and len(found) == (3 if args.phase == 'prepare' else 5),
                              'ECHO_REPLY_COUNT_MISMATCH')
            state['identity_sha256'] = guard.sha256(folder / 'peer/identity.json')
            state.update(phase='prepared' if args.phase == 'prepare' else 'verified')
            if args.phase == 'prepare':
                state['prepared_at'] = guard.utc_now()
                state['prepared_event_ids'] = list(state['expected'])
            save()
        result = attempt | {'ok': True, 'status': 'prepared' if args.phase == 'prepare' else 'passed',
            'finished_at': guard.utc_now(), 'origin': guard.ORIGIN, 'https': discovery,
            'peer_identity': state['identity'], 'echo_public_devices': state['echo_devices'],
            'room_id': state['room_id'], 'reply_count': len(state['observed']),
            'literal_json_sha256': guard.base.digest_text(RAW), 'literal_text_and_json_exact': True,
            'stable_send_retry': True, 'one_reply_per_original': True,
            'unsupported_invitations_pending_after_two_sequential_replies': True,
            'original_echo_devices_preserved': args.phase == 'verify',
            'registration_calls': 1 if args.phase == 'prepare' else 0,
            'clients_closed': True, 'stores_preserved': True, 'launch_verified': False}
        guard.atomic_json(guard.EVIDENCE / (args.run_id + '-' + args.phase + '.json'), result, private=False, replace=False)
        return result
    except Exception as error:
        return guard.failure(attempt, error)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['prepare', 'verify'])
    parser.add_argument('--target', type=Path, required=True)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    try:
        result = asyncio.run(execute(args))
        print(json.dumps(result))
        if not result['ok']: raise SystemExit(1)
    except Exception as error:
        print(json.dumps({'ok': False, 'error_code': guard.base.failure_code(error), 'stores_preserved': True}))
        raise SystemExit(1) from None
