"""Three-signup-compatible public recovery fixture; operator owns backup, hosts and DNS."""
import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import time

spec = importlib.util.spec_from_file_location('public_state_guard', Path(__file__).with_name('public_guard.py'))
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
Zavliq = guard.Zavliq
RUN_ID = re.compile(r'public-recovery-(2f76469b507c)-([0-9a-f]{8})')
SCHEMA = 'zavliq-public-original-device-recovery-v1'
RAW = '{ "exact": 1.0000000000000000001, "count": 9007199254740993 }'
LIMITS = {'messages_per_day': 1000, 'messages_per_minute': 30, 'contacts_per_day': 5, 'receipts_per_day': 3000}


def quota_shape(result, owner):
    guard.require(result.get('user_id') == owner and isinstance(result.get('quotas'), dict), 'QUOTA_IDENTITY_MISMATCH')
    quotas = result['quotas']
    guard.require(set(quotas) == set(LIMITS), 'QUOTA_FIELDS_MISMATCH')
    for key, limit in LIMITS.items():
        item = quotas[key]
        guard.require(isinstance(item, dict) and item.get('limit') == limit
                      and type(item.get('used')) is int and item['used'] >= 0
                      and type(item.get('resets_at_ms')) is int, 'NORMAL_QUOTA_CONTRACT_REQUIRED')
    return quotas


def quota_preserved(before, after, *, now=None):
    now = time.time() if now is None else now
    for key in ('messages_per_day', 'contacts_per_day'):
        guard.require(before[key]['used'] > 0, 'NONZERO_QUOTA_BASELINE_REQUIRED')
        guard.require(before[key]['resets_at_ms'] == after[key]['resets_at_ms']
                      and now * 1000 < before[key]['resets_at_ms'], 'QUOTA_PROOF_WINDOW_EXPIRED')
        guard.require(before[key]['used'] == after[key]['used'], 'RESTORED_QUOTA_COUNTER_MISMATCH')


async def rooms_for(client):
    rows = (await client.call('conversations'))['items']
    guard.require(isinstance(rows, list), 'CONVERSATION_LIST_REQUIRED')
    return {row['room_id']: row for row in rows}


async def memberships(a, b, state):
    aa, bb = await rooms_for(a), await rooms_for(b)
    for key, kind, encryption in [('standard', 'dm', 'standard'), ('e2ee', 'dm', 'e2ee'),
                                  ('trust_e2ee', 'dm', 'e2ee'),
                                  ('group_a', 'group', 'standard'), ('group_b', 'group', 'standard'),
                                  ('channel', 'channel', 'standard')]:
        room_id = state['rooms'][key]
        for rooms in (aa, bb):
            row = rooms.get(room_id, {})
            guard.require(row.get('membership') == 'joined' and row.get('kind') == kind
                          and row.get('encryption') == encryption and row.get('encrypted') is (encryption == 'e2ee')
                          and row.get('joined_member_count') == 2 and row.get('invited_member_count') == 0,
                          'RESTORED_MEMBERSHIP_OR_MODE_MISMATCH')
    pending = aa.get(state['rooms']['pending_echo'], {})
    guard.require(pending.get('membership') == 'joined' and pending.get('kind') == 'dm'
                  and pending.get('encryption') == 'e2ee' and pending.get('encrypted') is True
                  and pending.get('joined_member_count') == 1 and pending.get('invited_member_count') == 1,
                  'PENDING_CONTACT_NOT_PRESERVED')


async def blocked_echo(b):
    value = await b.call('blocks')
    guard.require(value.get('blocked_user_ids') == [guard.OWNER], 'RESTORED_BLOCK_NOT_PRESERVED')


async def denied_invite(client, room):
    try:
        await client.call('invite', {'room_id': room, 'user_id': guard.OWNER})
    except guard.ZavliqError as error:
        guard.require(error.code in {'M_FORBIDDEN', 'FORBIDDEN'}, 'EXPECTED_POLICY_REFUSAL_REQUIRED')
        return
    raise ValueError('CONTACT_POLICY_WAS_NOT_ENFORCED')


class Fixture:
    def __init__(self, folder, binary, factory=Zavliq):
        self.folder, self.binary, self.factory = Path(folder), str(binary), factory
        self.state_path = self.folder / 'state.json'

    def save(self, state): guard.atomic_json(self.state_path, state)

    def client(self, name):
        return self.factory(binary=self.binary, data_dir=str(self.folder / name), control_url=guard.ORIGIN, timeout=120)

    async def prepare(self, target, run_id):
        guard.require(not self.state_path.exists() and all(not (self.folder / name).exists() for name in ('a', 'b')),
                      'PREPARE_ALREADY_STARTED_PRESERVE_STORES')
        for name in ('a', 'b'): guard.private_directory(self.folder / name)
        suffix = RUN_ID.fullmatch(run_id).group(2)
        state = {'schema': SCHEMA, 'run_id': run_id, 'phase': 'preparing',
                 'provenance': guard.provenance(target), 'source_machine_id': target['target_machine_id'],
                 'handles': {'a': 'arden-' + suffix, 'b': 'mira-' + suffix},
                 'identities': {}, 'rooms': {}, 'historical_events': {}, 'new_events': {},
                 'created_at': guard.utc_now(), 'text': 'Retained public fixture ' + run_id, 'data_json': RAW}
        self.save(state)
        source = self.folder / 'source.bin'
        with source.open('xb') as stream:
            os.chmod(source, 0o600)
            stream.write(b'Zavliq public original-device restore\x00' + secrets.token_bytes(512))
            stream.flush(); os.fsync(stream.fileno())
        a, b = self.client('a'), self.client('b')
        try:
            for name, client in [('a', a), ('b', b)]:
                state['identities'][name] = guard.identity_metadata(await client.init(state['handles'][name]), state['handles'][name])
                self.save(state)
            for key, creator, peer, user, kind, encryption in [
                ('standard', a, b, 'b', 'dm', 'standard'), ('trust_e2ee', a, b, 'b', 'dm', 'e2ee'),
                ('group_a', a, b, 'b', 'group', 'standard'), ('group_b', b, a, 'a', 'group', 'standard')]:
                room = await creator.create_conversation([state['identities'][user]['user_id']], kind=kind, encryption=encryption)
                state['rooms'][key] = room['room_id']; self.save(state)
                await peer.call('accept', {'room_id': room['room_id']})
            state['rooms']['channel'] = (await a.create_conversation([], kind='channel', encryption='standard'))['room_id']
            self.save(state)
            await b.call('accept', {'room_id': state['rooms']['channel']})
            state['rooms']['pending_echo'] = (await a.create_conversation([guard.OWNER], encryption='e2ee'))['room_id']
            self.save(state)
            await b.call('block', {'user_id': guard.OWNER})
            await guard.base.trust_devices(a, b, state['identities'])
            baseline = await b.send(state['rooms']['trust_e2ee'], text='Public pre-backup trust check', idempotency_key=run_id + '-baseline')
            guard.require_message(await guard.find_event(a, state['rooms']['trust_e2ee'], guard.event_id(baseline)),
                                  state['identities']['b']['user_id'], text='Public pre-backup trust check')
            # Delivery receipts are encrypted room events too. Keep the baseline
            # in a separate room so it cannot create/share the retained room's
            # outbound Megolm session before B goes offline.
            state['rooms']['e2ee'] = (await a.create_conversation([state['identities']['b']['user_id']], encryption='e2ee'))['room_id']
            self.save(state)
            await b.call('accept', {'room_id': state['rooms']['e2ee']})
            await memberships(a, b, state)
            await blocked_echo(b)
            await b.close()
            state['recipient_closed_before_historical_send_at'] = guard.utc_now(); self.save(state)
            for mode in ('standard', 'e2ee'):
                room = state['rooms'][mode]
                for kind in ('text', 'json', 'file'):
                    key = mode + '_' + kind
                    if kind == 'file':
                        response = await a.call('upload', {'room_id': room, 'path': str(source), 'idempotency_key': run_id + '-old-' + key})
                    else:
                        response = await a.send(room, **({'text': state['text']} if kind == 'text' else {'data_json': RAW}),
                                                idempotency_key=run_id + '-old-' + key)
                    state['historical_events'][key] = {'event_id': guard.event_id(response), 'room_id': room,
                                                       'kind': kind, 'encryption': mode}
                    self.save(state)
            for key in ('group_a', 'channel'):
                response = await a.send(state['rooms'][key], text=state['text'], idempotency_key=run_id + '-old-' + key)
                state['historical_events'][key + '_text'] = {'event_id': guard.event_id(response),
                    'room_id': state['rooms'][key], 'kind': 'text', 'encryption': 'standard'}
                self.save(state)
            await a.call('sync', {'wait_seconds': 0})
            state['quota_before'] = quota_shape(await a.call('quotas'), state['identities']['a']['user_id'])
            guard.require(state['quota_before']['messages_per_day']['used'] >= 8
                          and state['quota_before']['contacts_per_day']['used'] >= 2, 'MEANINGFUL_QUOTA_BASELINE_REQUIRED')
            self.save(state)
        finally:
            await a.close(); await b.close()
        guard.base.stores_closed(self.folder)
        guard.base.absent_events(self.folder, [event['event_id'] for event in state['historical_events'].values()])
        state['identity_file_hashes'] = guard.base.identity_digest(self.folder)
        state['content_hashes'] = {'text_sha256': guard.base.digest_text(state['text']),
                                   'json_sha256': guard.base.digest_text(RAW), 'file_sha256': guard.sha256(source)}
        state.update(phase='prepared', prepared_at=guard.utc_now())
        self.save(state)
        return self.evidence(state, 'prepare')

    async def verify(self, target, state):
        guard.require(state.get('phase') in {'prepared', 'verifying'}, 'PREPARED_PUBLIC_FIXTURE_REQUIRED')
        guard.base.stores_closed(self.folder)
        guard.require(guard.base.identity_digest(self.folder) == state['identity_file_hashes'], 'ORIGINAL_IDENTITY_FILES_CHANGED')
        restore = {key: target[key] for key in ('target_machine_id', 'encrypted_backup_sha256', 'restore_completed_at')}
        if state['phase'] == 'prepared':
            guard.base.absent_events(self.folder, [event['event_id'] for event in state['historical_events'].values()])
            state.update(phase='verifying', restore=restore, first_verify_at=guard.utc_now(),
                         historical_cache_empty_before_first_sync=True)
            self.save(state)
        else:
            guard.require(state.get('restore') == restore and state.get('historical_cache_empty_before_first_sync') is True,
                          'VERIFY_RESUME_TARGET_MISMATCH')
        a, b = self.client('a'), self.client('b')
        try:
            for name, client in [('a', a), ('b', b)]:
                guard.identity_metadata(await client.identity(), expected=state['identities'][name])
            # B's restore sync may consume its own counters through receipts. A
            # already ingested its sends before the snapshot; prove A's nonzero
            # daily message/contact usage before any new verification mutation.
            if not state.get('quota_preserved_before_mutations'):
                after = quota_shape(await a.call('quotas'), state['identities']['a']['user_id'])
                quota_preserved(state['quota_before'], after)
                state['quota_after'] = after
                state['quota_preserved_before_mutations'] = True; self.save(state)
            await memberships(a, b, state)
            await blocked_echo(b)
            state['checks'] = ['original_memberships_modes_and_pending_contact', 'original_echo_block', 'daily_message_and_contact_counters']
            self.save(state)
            for key, event in state['historical_events'].items():
                item = await guard.find_event(b, event['room_id'], event['event_id'])
                guard.require_message(item, state['identities']['a']['user_id'],
                                      **({'text': state['text']} if event['kind'] == 'text' else
                                         {'data_json': state['data_json']} if event['kind'] == 'json' else {}))
                if event['kind'] == 'file':
                    guard.attachment(item, event['encryption'] == 'e2ee')
                    output = self.folder / ('restored-' + key + '-' + secrets.token_hex(6) + '.bin')
                    await b.call('download', {'event_id': event['event_id'], 'path': str(output)})
                    guard.require(guard.sha256(output) == state['content_hashes']['file_sha256'], 'RESTORED_FILE_HASH_MISMATCH')
            state['checks'].append('unseen_standard_and_e2ee_text_exact_json_files_and_group_channel_messages')
            self.save(state)
            # Both probes target owned, joined groups with spare membership.
            # A has an unaccepted Echo contact; B has an explicit Echo block.
            await denied_invite(a, state['rooms']['group_a'])
            await denied_invite(b, state['rooms']['group_b'])
            state['checks'].append('pending_contact_and_block_invite_enforcement')
            self.save(state)
            for mode in ('standard', 'e2ee', 'group_a', 'group_b'):
                for name, sender, receiver, peer in [('a', a, b, 'b'), ('b', b, a, 'a')]:
                    key = mode + '_' + name + '_to_' + peer
                    text = 'Public post-restore ' + key
                    response = await sender.send(state['rooms'][mode], text=text, data_json=RAW,
                                                 idempotency_key=state['run_id'] + '-new-' + key)
                    state['new_events'][key] = guard.event_id(response); self.save(state)
                    guard.require_message(await guard.find_event(receiver, state['rooms'][mode], guard.event_id(response)),
                                          state['identities'][name]['user_id'], text=text, data_json=RAW)
            published = await a.send(state['rooms']['channel'], text=state['text'],
                                     idempotency_key=state['run_id'] + '-new-channel')
            state['new_events']['channel_a_to_b'] = guard.event_id(published); self.save(state)
            guard.require_message(await guard.find_event(b, state['rooms']['channel'], guard.event_id(published)),
                                  state['identities']['a']['user_id'], text=state['text'])
            await memberships(a, b, state)
            state['checks'].append('bidirectional_new_standard_and_e2ee_text_exact_json_without_reverification')
            state['checks'].append('new_group_roundtrips_and_channel_publication_confirm_live_memberships')
        finally:
            await a.close(); await b.close()
        guard.base.stores_closed(self.folder)
        state.update(phase='verified', verified_at=guard.utc_now())
        state['verification_seconds'] = round(guard.base.timestamp(state['verified_at']) - guard.base.timestamp(state['first_verify_at']), 3)
        self.save(state)
        return self.evidence(state, 'verify')

    @staticmethod
    def evidence(state, phase):
        result = {key: state[key] for key in ('schema', 'run_id', 'provenance', 'identities', 'rooms',
                  'prepared_at', 'historical_events', 'content_hashes')}
        result.update(phase=phase, status='prepared' if phase == 'prepare' else 'passed', ok=True,
                      registration_calls=2 if phase == 'prepare' else 0, clients_closed=True,
                      historical_recipient_cache_empty_at_prepare=True, launch_verified=False,
                      encrypted_baseline_in_separate_room=True,
                      limitations=['Backup, off-host retrieval, host replacement, fencing, DNS/TLS routing and full RTO/RPO are operator-owned.',
                                   'Only daily message/contact quota counters are compared in the same UTC bucket; limit exhaustion, minute-counter expiry and media quota accounting are not tested.',
                                   'Channel publication and joined memberships are checked; publisher permission changes, bans and directory settings are not tested.',
                                   'Browser/client-key-loss recovery and restored Echo device/reply checks are separate.'])
        if phase == 'verify':
            result.update(verified_at=state['verified_at'], verification_seconds=state['verification_seconds'],
                encrypted_backup_sha256=state['restore']['encrypted_backup_sha256'],
                restore_completed_at=state['restore']['restore_completed_at'],
                historical_cache_empty_before_first_sync=state['historical_cache_empty_before_first_sync'],
                original_user_device_identity_files_preserved=True, fresh_download_paths_hash_matched=True,
                encrypted_attachment_keys_redacted=True, checks=state['checks'], new_events=state['new_events'],
                daily_quota_counters_before={key: state['quota_before'][key] for key in ('messages_per_day', 'contacts_per_day')},
                daily_quota_counters_after={key: state['quota_after'][key] for key in ('messages_per_day', 'contacts_per_day')})
        return result


async def execute(args):
    guard.require(args.execute is True, 'EXECUTION_NOT_ENABLED_NO_NETWORK_PERFORMED')
    os.umask(0o077)
    match = RUN_ID.fullmatch(args.run_id or '')
    attempt = {'schema': SCHEMA, 'run_id': args.run_id if match else None,
               'phase': args.phase if args.phase in ('prepare', 'verify') else 'invalid',
               'started_at': guard.utc_now(), **guard.driver_hashes(__file__)}
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
        if state is not None:
            guard.require(state.get('schema') == SCHEMA and state.get('run_id') == args.run_id, 'FIXTURE_STATE_MISMATCH')
        if args.phase == 'prepare': guard.require(not folder.exists(), 'FRESH_PUBLIC_FIXTURE_REQUIRED')
        attempt['provenance'] = guard.provenance(target)
        attempt['target_attestation'] = guard.target_evidence(target)
        discovery = await asyncio.to_thread(guard.preflight_discovery)
        guard.validate_target(target, args.phase, prepared=state)
        if args.phase == 'prepare': guard.require(discovery['registration_open'], 'PUBLIC_REGISTRATION_CLOSED')
        guard.private_directory(guard.PRIVATE)
        guard.private_directory(folder)
        fixture = Fixture(folder, binary)
        with guard.exclusive_lock(folder / 'fixture.lock'):
            result = await fixture.prepare(target, args.run_id) if args.phase == 'prepare' else await fixture.verify(target, state)
        result.update(https=discovery, target_attestation=guard.target_evidence(target), **guard.driver_hashes(__file__))
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
