"""Offline guard and phase tests. No real binary, account, tunnel or AWS calls."""
import argparse
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import verify

REVISION = 'a' * 40
RUN_ID = 'recovery-' + REVISION[:12] + '-1234abcd'
BINARY_HASH = 'b' * 64
NOW = datetime.now(timezone.utc).timestamp()


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def target(phase='prepare'):
    project = 'zavliq-load' if phase == 'prepare' else 'zavliq-recovery-' + REVISION[:12]
    port = 19180 if phase == 'prepare' else 19181
    return {'final_candidate': True, 'environment': 'aws-staging', 'phase': phase,
        'origin': verify.ORIGIN, 'server_name': 'localhost', 'revision': REVISION,
        'source_archive_sha256': 'c' * 64, 'native_binary_sha256': BINARY_HASH,
        'project': project, 'remote_port': port, 'route_project': project,
        'route_remote_port': port, 'route_checked_at': iso(NOW),
        'images': {name: {'ref': 'synthetic/' + name + ':' + REVISION, 'id': 'sha256:' + str(index) * 64}
                   for index, name in enumerate(['synapse', 'control', 'gateway', 'postgres'])},
        **({'encrypted_backup_sha256': 'd' * 64, 'restore_completed_at': iso(NOW - 10)} if phase == 'verify' else {})}


class GuardTests(unittest.TestCase):
    def test_exact_private_origin_project_and_fresh_attestation_required(self):
        verify.validate_target(target(), 'prepare', REVISION, BINARY_HASH, now=NOW)
        changes = [{'origin': value} for value in ['https://zavliq.com', 'http://localhost:8008',
            'http://127.0.0.1:28180', 'http://localhost:28180/', 'http://localhost:28180@other.test']]
        changes += [{'project': 'zavliq'}, {'remote_port': 19181}, {'route_project': 'another'},
            {'route_remote_port': 19181}, {'server_name': 'zavliq.com'}, {'environment': 'production'},
            {'final_candidate': False}, {'phase': 'verify'}, {'route_checked_at': iso(NOW - 1801)},
            {'route_checked_at': iso(NOW + 61)}, {'route_checked_at': '2026-09-12T12:00:00'}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                verify.validate_target(target() | change, 'prepare', REVISION, BINARY_HASH, now=NOW)

    def test_clone_requires_exact_build_binary_backup_and_post_prepare_restore(self):
        prepared = {'schema': verify.SCHEMA, 'provenance': verify.provenance(target()), 'prepared_at': iso(NOW - 20)}
        verify.validate_target(target('verify'), 'verify', REVISION, BINARY_HASH, now=NOW, prepared=prepared)
        changes = [{'revision': 'e' * 40}, {'native_binary_sha256': 'e' * 64},
            {'source_archive_sha256': 'e' * 64}, {'encrypted_backup_sha256': ''},
            {'restore_completed_at': iso(NOW - 21)}, {'restore_completed_at': iso(NOW + 61)},
            {'images': {name: image | {'id': 'sha256:' + 'f' * 64} for name, image in target()['images'].items()}}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                verify.validate_target(target('verify') | change, 'verify', REVISION, BINARY_HASH,
                    now=NOW, prepared=prepared)
        with self.assertRaisesRegex(ValueError, 'PREPARED_FIXTURE'):
            verify.validate_target(target('verify'), 'verify', REVISION, BINARY_HASH, now=NOW)

    def test_execution_flag_precedes_any_path_or_runtime_access(self):
        with patch.object(verify, 'Fixture', side_effect=AssertionError('Runtime must not start')):
            with self.assertRaisesRegex(ValueError, 'EXECUTION_NOT_ENABLED'):
                asyncio.run(verify.execute(argparse.Namespace(execute=False)))

    def test_original_device_and_origin_are_checked(self):
        identity = {'user_id': '@arden-1234abcd:localhost', 'device_id': 'A', 'homeserver': verify.ORIGIN}
        self.assertEqual(verify.identity_metadata(identity, 'arden-1234abcd'), identity)
        for change in [{'homeserver': 'http://localhost:19181'}, {'device_id': 'NEW'}, {'user_id': '@other:localhost'}]:
            with self.assertRaises(ValueError): verify.identity_metadata(identity | change, expected=identity)

    def test_locks_and_private_files_are_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            for name in ('a', 'b'): (base / name).mkdir(mode=0o700)
            with verify.exclusive_lock(base / 'b/runtime.lock'):
                with self.assertRaisesRegex(ValueError, 'STILL_RUNNING'): verify.stores_closed(base)
            verify.stores_closed(base)
            verify.atomic_json(base / 'state.json', {'safe': True})
            self.assertEqual((base / 'state.json').stat().st_mode & 0o777, 0o600)
            (base / 'alias').symlink_to(base / 'a', target_is_directory=True)
            with self.assertRaisesRegex(ValueError, 'SYMLINK'): verify.private_directory(base / 'alias')

    def test_failed_attempts_are_sanitized_durable_and_do_not_replace_any_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            existing = base / (RUN_ID + '-verify.json')
            existing.write_text('existing successful evidence')
            args = argparse.Namespace(execute=True, phase='verify', run_id=RUN_ID, revision=REVISION)
            async def fail(args, attempt):
                attempt['native_binary_sha256'] = BINARY_HASH
                attempt['provenance'] = verify.provenance(target())
                raise RuntimeError('https://private.example/path?token=secret BODY file.key identity_file_hashes')
            with patch.object(verify, 'EVIDENCE', base), patch.object(verify, 'execute_phase', side_effect=fail), \
                    patch.object(verify.secrets, 'token_hex', side_effect=['1' * 16, '1' * 16, '2' * 16]):
                first = asyncio.run(verify.execute(args))
                second = asyncio.run(verify.execute(args))
            failures = sorted(base.glob('*-failed-*.json'))
            self.assertEqual(len(failures), 2)
            self.assertEqual(json.loads(failures[0].read_text()), first)
            self.assertEqual(json.loads(failures[1].read_text()), second)
            self.assertEqual(first['error_code'], 'RuntimeError')
            self.assertEqual(first['provenance'], verify.provenance(target()))
            self.assertEqual(first['native_binary_sha256'], BINARY_HASH)
            self.assertEqual(existing.read_text(), 'existing successful evidence')
            for forbidden in ['private.example', 'BODY', 'file.key', 'identity_file_hashes', 'token']:
                self.assertNotIn(forbidden, json.dumps([first, second]))
            self.assertEqual(list(base.glob('tmp*')), [])

    def test_failed_validation_records_safe_metadata_without_changing_private_journal(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            private = base / 'private'
            run = private / RUN_ID
            run.mkdir(parents=True, mode=0o700)
            private.chmod(0o700)
            state = {'schema': verify.SCHEMA, 'run_id': RUN_ID, 'phase': 'prepared', 'text': 'private body',
                     'identity_file_hashes': {'a': 'private-integrity-hash'}}
            verify.atomic_json(run / 'state.json', state)
            before = (run / 'state.json').read_bytes()
            executable = base / 'synthetic-binary'
            executable.write_bytes(b'never executed')
            executable.chmod(0o700)
            target_file = base / 'target.json'
            target_file.write_text(json.dumps(target() | {'origin': 'https://private.example/secret'}))
            args = argparse.Namespace(execute=True, phase='prepare', run_id=RUN_ID, revision=REVISION,
                                      binary=executable, target=target_file)
            with patch.object(verify, 'PRIVATE', private), patch.object(verify, 'EVIDENCE', base / 'evidence'), \
                    patch.object(verify.Fixture, 'prepare', new_callable=AsyncMock) as prepare:
                failure = asyncio.run(verify.execute(args))
                prepare.assert_not_called()
            self.assertEqual(failure['error_code'], 'FIXED_STAGING_ORIGIN_REQUIRED')
            self.assertEqual((run / 'state.json').read_bytes(), before)
            self.assertEqual(len(list((base / 'evidence').glob('*-failed-*.json'))), 1)
            for forbidden in ['private body', 'private-integrity-hash', 'private.example']:
                self.assertNotIn(forbidden, json.dumps(failure))


class FakeServer:
    """Only tests driver ordering/contracts; this is not encryption evidence."""
    def __init__(self):
        self.messages, self.transactions, self.clients = {}, {}, []
        self.room = '!synthetic:localhost'
        self.identities, self.downloads, self.trust_calls = {}, [], 0
        self.fail_new_send = False
        self.historical_send_recipient_closed = []

    def client(self, **options):
        client = FakeClient(self, Path(options['data_dir']))
        self.clients.append(client)
        return client

    def put(self, client, transaction, content, data_json=None):
        if transaction in self.transactions:
            return {'event_id': self.transactions[transaction]}
        event = '$synthetic' + str(len(self.messages))
        self.messages[event] = {'event_id': event, 'room_id': self.room,
            'sender': self.identities[client.name]['user_id'], 'content': content,
            **({'data_json': data_json} if data_json is not None else {})}
        self.transactions[transaction] = event
        if '-old-' in transaction:
            self.historical_send_recipient_closed.append(all(item.closed for item in self.clients if item.name == 'b'))
        return {'event_id': event}


class FakeClient:
    def __init__(self, server, directory):
        self.server, self.directory, self.name = server, directory, directory.name
        self.closed = False

    async def init(self, handle):
        self.server.identities[self.name] = {'user_id': '@' + handle + ':localhost', 'device_id': self.name.upper(), 'homeserver': verify.ORIGIN}
        # Deliberately secret-shaped synthetic content must stay out of evidence.
        (self.directory / 'identity.json').write_text('synthetic-access-token-and-registration-secret')
        with closing(sqlite3.connect(self.directory / 'inbox.sqlite3')) as database, database:
            database.execute('CREATE TABLE events(event_id TEXT PRIMARY KEY)')
        return self.server.identities[self.name]

    async def identity(self):
        return self.server.identities[self.name]

    async def create_conversation(self, members, encryption):
        assert encryption == 'e2ee'
        return {'room_id': self.server.room}

    async def send(self, room, *, text=None, data_json=None, idempotency_key):
        if self.server.fail_new_send and '-new-' in idempotency_key:
            self.server.fail_new_send = False
            raise ValueError('SYNTHETIC_INTERRUPTION')
        return self.server.put(self, idempotency_key, {'body': text} if text is not None else {}, data_json)

    async def call(self, method, params=None):
        params = params or {}
        if method in {'accept', 'sync'}: return {}
        if method == 'conversations':
            return {'items': [{'room_id': self.server.room, 'kind': 'dm', 'encrypted': True, 'encryption': 'e2ee',
                'membership': 'joined', 'joined_member_count': 2, 'invited_member_count': 0}]}
        if method == 'crypto_devices':
            return {'devices': [{'device_id': self.name.upper(), 'ed25519': 'synthetic-public-fingerprint'}]}
        if method == 'verify_device':
            self.server.trust_calls += 1
            return {'verified': True}
        if method == 'inbox':
            assert params['sync'] is False and params['full'] is True
            items = [item for item in self.server.messages.values() if item['sender'] != self.server.identities[self.name]['user_id']]
            with closing(sqlite3.connect(self.directory / 'inbox.sqlite3')) as database, database:
                database.executemany('INSERT OR IGNORE INTO events VALUES(?)', [(item['event_id'],) for item in items])
            return {'items': items, 'has_more': False, 'next_cursor': len(items)}
        if method == 'upload':
            self.server.payload = Path(params['path']).read_bytes()
            return self.server.put(self, params['idempotency_key'], {'file': {'v': 'v2', 'key': 'synthetic-secret-file-key'}})
        if method == 'download':
            self.server.downloads.append(params['path'])
            with Path(params['path']).open('xb') as stream: stream.write(self.server.payload)
            return {}
        raise AssertionError('Unexpected driver method ' + method)

    async def close(self):
        self.closed = True


class PhaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.server = FakeServer()
        self.fixture = verify.Fixture(self.base, '/unused/synthetic-binary', self.server.client)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def prepare(self):
        evidence = await self.fixture.prepare(target(), RUN_ID)
        return evidence, json.loads(self.fixture.state_path.read_text())

    async def test_offline_recipient_then_original_stores_complete_two_phases(self):
        evidence, state = await self.prepare()
        self.assertEqual(state['handles'], {'a': 'arden-1234abcd', 'b': 'mira-1234abcd'})
        self.assertEqual(self.server.historical_send_recipient_closed, [True, True, True])
        verify.absent_events(self.base, state['historical_events'].values())
        original_identity_hashes = verify.identity_digest(self.base)
        result = await self.fixture.verify(target('verify'), state)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(verify.identity_digest(self.base), original_identity_hashes)
        self.assertEqual(self.server.trust_calls, 2, 'Verify must reuse existing trust, never re-pair or re-verify')
        self.assertEqual(len(result['new_e2ee_roundtrip_events']), 2)
        self.assertTrue(all(client.closed for client in self.server.clients))
        public = json.dumps([evidence, result])
        for forbidden in ['synthetic-secret', 'registration_secret', 'identity_file_hashes', state['text'], state['data_json']]:
            self.assertNotIn(forbidden, public)

    async def test_cached_historical_event_or_changed_identity_fails_before_runtime_start(self):
        _, state = await self.prepare()
        client_count = len(self.server.clients)
        with closing(sqlite3.connect(self.base / 'b/inbox.sqlite3')) as database, database:
            database.execute('INSERT INTO events VALUES(?)', (state['historical_events']['text'],))
        with self.assertRaisesRegex(ValueError, 'ALREADY_CACHED'): await self.fixture.verify(target('verify'), state)
        self.assertEqual(len(self.server.clients), client_count)
        (self.base / 'a/identity.json').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'IDENTITY_FILES_CHANGED'): await self.fixture.verify(target('verify'), state)
        self.assertEqual(len(self.server.clients), client_count)

    async def test_interrupted_verify_retains_initial_zero_cache_proof_and_downloads_again(self):
        _, state = await self.prepare()
        self.server.fail_new_send = True
        with self.assertRaisesRegex(ValueError, 'SYNTHETIC_INTERRUPTION'):
            await self.fixture.verify(target('verify'), state)
        interrupted = json.loads(self.fixture.state_path.read_text())
        self.assertEqual(interrupted['phase'], 'verifying')
        self.assertTrue(interrupted['historical_cache_empty_before_verify'])
        self.assertTrue(all(client.closed for client in self.server.clients))
        changed_target = target('verify') | {'encrypted_backup_sha256': 'f' * 64}
        with self.assertRaisesRegex(ValueError, 'RESUME_TARGET_MISMATCH'):
            await self.fixture.verify(changed_target, interrupted)
        result = await self.fixture.verify(target('verify'), interrupted)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(len(set(self.server.downloads)), 2)
        with self.assertRaisesRegex(ValueError, 'PREPARED_OR_RESUMABLE'):
            await self.fixture.verify(target('verify'), interrupted)

    async def test_repeated_prepare_preserves_first_identity_and_does_not_create_more_accounts(self):
        await self.prepare()
        count = len(self.server.clients)
        with self.assertRaisesRegex(ValueError, 'PREPARE_ALREADY_STARTED'):
            await self.fixture.prepare(target(), RUN_ID)
        self.assertEqual(len(self.server.clients), count)


if __name__ == '__main__':
    unittest.main()
