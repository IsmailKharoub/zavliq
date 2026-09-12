"""Isolated verification-driver tests; no native process, account, or network calls."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import echo

REVISION = 'a' * 40
RUN_ID = 'echo-' + REVISION[:12] + '-1234abcd'


def target(binary_hash):
    return {'final_candidate': True, 'environment': 'aws-staging', 'phase': 'prepare',
        'origin': echo.guard.ORIGIN, 'server_name': 'localhost', 'revision': REVISION,
        'source_archive_sha256': 'c' * 64, 'native_binary_sha256': binary_hash,
        'project': 'zavliq-load', 'remote_port': 19180, 'route_project': 'zavliq-load',
        'route_remote_port': 19180, 'route_checked_at': datetime.now(timezone.utc).isoformat(),
        'images': {name: {'ref': 'synthetic/' + name, 'id': 'sha256:' + str(index) * 64}
                   for index, name in enumerate(['synapse', 'control', 'gateway', 'postgres'])}}


class DiscoveryTests(unittest.TestCase):
    def documents(self):
        return [{'control_url': echo.guard.ORIGIN, 'homeserver': echo.guard.ORIGIN, 'server_name': 'localhost'},
                {'m.homeserver': {'base_url': echo.guard.ORIGIN}}]

    def connections(self, documents, statuses=(200, 200)):
        result = []
        for document, status in zip(documents, statuses):
            connection = Mock()
            connection.getresponse.return_value.status = status
            connection.getresponse.return_value.read.return_value = json.dumps(document).encode()
            result.append(connection)
        return result

    def test_only_fixed_socket_and_paths_are_used(self):
        connections = self.connections(self.documents())
        with patch.object(echo.http.client, 'HTTPConnection', side_effect=connections) as factory:
            echo.preflight_discovery()
        self.assertEqual(factory.call_count, 2)
        for call in factory.call_args_list:
            self.assertEqual(call.args, ('localhost', 28180))
            self.assertEqual(call.kwargs, {'timeout': 10})
        self.assertEqual([c.request.call_args.args for c in connections],
                         [('GET', '/.well-known/zavliq'), ('GET', '/.well-known/matrix/client')])
        for connection in connections:
            connection.close.assert_called_once()

    def test_redirect_is_not_followed(self):
        connections = self.connections(self.documents(), (302, 200))
        with patch.object(echo.http.client, 'HTTPConnection', side_effect=connections) as factory:
            with self.assertRaisesRegex(ValueError, 'DISCOVERY_HTTP_STATUS'):
                echo.preflight_discovery()
        self.assertEqual(factory.call_count, 1)
        connections[0].close.assert_called_once()

    def test_misadvertised_origin_is_rejected(self):
        for field in ('control_url', 'homeserver', 'matrix'):
            documents = self.documents()
            if field == 'matrix':
                documents[1]['m.homeserver']['base_url'] = 'http://localhost:8080'
            else:
                documents[0][field] = 'http://localhost:8080'
            with self.subTest(field=field), patch.object(echo.http.client, 'HTTPConnection',
                                                        side_effect=self.connections(documents)):
                with self.assertRaisesRegex(ValueError, 'DISCOVERY_ORIGIN_MISMATCH'):
                    echo.preflight_discovery()


class Peer:
    """Models Echo input/output timing only, never Matrix or cryptography."""
    def __init__(self, stop_after=None, unrelated=False, duplicate=False, bad_origin=False, accept_unsupported=False):
        self.stop_after, self.unrelated, self.duplicate = stop_after, unrelated, duplicate
        self.bad_origin, self.accept_unsupported = bad_origin, accept_unsupported
        self.messages, self.transactions, self.calls = [], {}, []
        self.created, self.init_calls, self.reply_reads = [], [], set()
        self.closed = False
        self.options = None

    def factory(self, **options):
        self.options = options
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def init(self, handle, display_name):
        self.init_calls.append((handle, display_name))
        directory = Path(self.options['data_dir'])
        directory.mkdir(mode=0o700)
        (directory / 'fixture-marker').write_text('synthetic fixture store, not credentials')
        return {'user_id': '@' + handle + ':localhost', 'device_id': 'SYNTHETIC',
                'homeserver': 'http://localhost:8080' if self.bad_origin else echo.guard.ORIGIN}

    async def create_conversation(self, members, *, encryption='standard', kind='dm'):
        room = '!group:localhost' if kind == 'group' else '!encrypted:localhost' if encryption == 'e2ee' else '!dm:localhost'
        self.created.append(room)
        return {'room_id': room}

    def append_reply(self, original, params):
        content = {'body': params['text'], 'msgtype': 'm.text',
                   'm.relates_to': {'m.in_reply_to': {'event_id': original}}}
        if 'data_json' in params:
            content['com.zavliq.data_json'] = params['data_json']
        self.messages.append({'event_id': '$reply' + str(len(self.messages) + 1),
            'cursor': len(self.messages) + 1, 'room_id': '!dm:localhost', 'sender': echo.OWNER, 'content': content})

    async def call(self, method, params=None):
        self.calls.append((method, dict(params or {})))
        if method == 'send':
            key = params['idempotency_key']
            if key in self.transactions:
                return {'event_id': self.transactions[key]}
            number = len(self.transactions) + 1
            if number == 3:
                # The second barrier must only be sent after reading the first.
                assert '$original2' in self.reply_reads
            original = '$original' + str(number)
            self.transactions[key] = original
            if self.stop_after is None or number <= self.stop_after:
                self.append_reply(original, params)
            if number == 1 and self.unrelated:
                self.messages.append({'event_id': '$unrelated', 'cursor': 2, 'room_id': '!dm:localhost',
                                      'sender': echo.OWNER, 'content': {'body': 'unexpected extra message'}})
            if number == 3 and self.duplicate:
                # Place the duplicate after several pages, not beside the first reply.
                self.append_reply('$original1', {'text': 'Hello from Finch', 'data_json': echo.RAW})
            return {'event_id': original}
        if method == 'inbox':
            assert params['room_id'] == '!dm:localhost'
            assert params['limit'] == 100
            cursor = params['cursor']
            remaining = [row for row in self.messages if row['cursor'] > cursor]
            rows = remaining[:1]  # Small pages force the driver through real pagination.
            for row in rows:
                relation = row['content'].get('m.relates_to', {}).get('m.in_reply_to', {}).get('event_id')
                self.reply_reads.add(relation)
            return {'items': rows, 'has_more': len(remaining) > 1,
                    'next_cursor': rows[-1]['cursor'] if rows else cursor, 'history_gap_rooms': []}
        if method == 'conversations':
            return {'items': [{'room_id': room, 'joined_member_count': 2 if self.accept_unsupported else 1,
                              'invited_member_count': 0 if self.accept_unsupported else 1}
                             for room in self.created]}
        raise AssertionError('Unexpected driver operation')


class DriverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.private, self.evidence = self.base / 'private', self.base / 'evidence'
        self.binary = self.base / 'never-executed'
        self.binary.write_bytes(b'synthetic executable; never run')
        self.binary.chmod(0o500)
        self.target_file = self.base / 'target.json'
        self.target_file.write_text(json.dumps(target(echo.guard.sha256(self.binary))))
        self.args = argparse.Namespace(execute=True, target=self.target_file, binary=self.binary,
                                       revision=REVISION, run_id=RUN_ID)
        for name, value in [('PRIVATE', self.private), ('EVIDENCE', self.evidence), ('REPLY_TIMEOUT', .002)]:
            patcher = patch.object(echo, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def execute(self, peer, preflight=None):
        with patch.object(echo, 'Zavliq', side_effect=peer.factory) as factory, \
                patch.object(echo, 'preflight_discovery', side_effect=preflight) as discovery, \
                patch.object(echo.asyncio, 'sleep', new_callable=AsyncMock):
            result = await echo.run(self.args)
        return result, factory, discovery

    async def test_success_requires_two_sequential_barriers_and_all_pages(self):
        peer = Peer()
        result, factory, discovery = await self.execute(peer)
        self.assertTrue(result['ok'])
        self.assertEqual(result['echo_message_count'], 3)
        self.assertEqual(len(result['barriers']), 2)
        self.assertEqual(peer.init_calls, [('finch-1234abcd', 'Finch')])
        self.assertEqual(len(peer.transactions), 3)  # The first send is retried, never duplicated.
        self.assertTrue(peer.closed)
        self.assertTrue(any(method == 'inbox' and params['cursor'] >= 2 and params['sync'] is False
                            for method, params in peer.calls))
        self.assertEqual(json.loads((self.evidence / (RUN_ID + '.json')).read_text()), result)
        discovery.assert_called_once()
        factory.assert_called_once()

    async def test_stopped_echo_cannot_pass_by_reusing_an_old_reply(self):
        for replies in (1, 2):
            with self.subTest(successful_replies=replies):
                self.args.run_id = 'echo-' + REVISION[:12] + '-1234abc' + str(replies)
                peer = Peer(stop_after=replies)
                result, _, _ = await self.execute(peer)
                self.assertFalse(result['ok'])
                self.assertEqual(result['error_code'], 'ECHO_REPLY_TIMEOUT')
                self.assertTrue(peer.closed)
                self.assertTrue((self.private / self.args.run_id / 'peer/fixture-marker').is_file())
                self.assertFalse((self.evidence / (self.args.run_id + '.json')).exists())

    async def test_unrelated_echo_message_is_not_hidden_by_relation_filtering(self):
        result, _, _ = await self.execute(Peer(unrelated=True))
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'UNEXPECTED_ECHO_MESSAGE')

    async def test_duplicate_on_later_page_cannot_pass(self):
        result, _, _ = await self.execute(Peer(duplicate=True))
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'DUPLICATE_ECHO_REPLY')

    async def test_enrollment_origin_is_checked_before_rooms_or_messages(self):
        peer = Peer(bad_origin=True)
        result, _, _ = await self.execute(peer)
        self.assertEqual(result['error_code'], 'ENROLLED_ORIGIN_MISMATCH')
        self.assertEqual(peer.created, [])
        self.assertEqual(peer.transactions, {})
        self.assertTrue(peer.closed)

    async def test_unsupported_invitation_membership_cannot_pass(self):
        result, _, _ = await self.execute(Peer(accept_unsupported=True))
        self.assertEqual(result['error_code'], 'ECHO_ACCEPTED_UNSUPPORTED_INVITE')
        self.assertFalse(result['ok'])

    async def test_discovery_failure_records_sanitized_evidence_without_starting_client(self):
        result, factory, _ = await self.execute(Peer(), ValueError('DISCOVERY_ORIGIN_MISMATCH'))
        factory.assert_not_called()
        self.assertFalse(result['ok'])
        self.assertEqual(result['error_code'], 'DISCOVERY_ORIGIN_MISMATCH')
        failures = list(self.evidence.glob('*-failed-*.json'))
        self.assertEqual(len(failures), 1)
        self.assertEqual(json.loads(failures[0].read_text()), result)
        self.assertFalse((self.private / RUN_ID / 'peer').exists())

    async def test_guard_failure_does_not_reach_discovery_and_discards_private_error_text(self):
        self.target_file.write_text(json.dumps(target(echo.guard.sha256(self.binary)) | {'origin': 'https://private.invalid/token'}))
        result, factory, discovery = await self.execute(Peer())
        self.assertEqual(result['error_code'], 'FIXED_STAGING_ORIGIN_REQUIRED')
        factory.assert_not_called()
        discovery.assert_not_called()
        self.assertNotIn('private.invalid', json.dumps(result))
        self.args.run_id = 'echo-' + REVISION[:12] + '-1234abce'
        self.target_file.write_text(json.dumps(target(echo.guard.sha256(self.binary))))
        result, factory, _ = await self.execute(Peer(), RuntimeError('private URL token=never-print-this'))
        self.assertEqual(result['error_code'], 'RuntimeError')
        self.assertNotIn('never-print-this', json.dumps(result))
        factory.assert_not_called()

    async def test_retry_never_replaces_existing_fixture_or_success_evidence(self):
        first, _, _ = await self.execute(Peer())
        before = (self.evidence / (RUN_ID + '.json')).read_bytes()
        marker = self.private / RUN_ID / 'peer/fixture-marker'
        marker_before = marker.read_bytes()
        for _ in range(2):
            failed, factory, discovery = await self.execute(Peer())
            self.assertEqual(failed['error_code'], 'FIXTURE_ALREADY_EXISTS')
            factory.assert_not_called()
            discovery.assert_not_called()
        self.assertTrue(first['ok'])
        self.assertEqual((self.evidence / (RUN_ID + '.json')).read_bytes(), before)
        self.assertEqual(marker.read_bytes(), marker_before)
        self.assertEqual(len(list(self.evidence.glob('*-failed-*.json'))), 2)

    async def test_execute_flag_precedes_any_path_or_network_access(self):
        with patch.object(echo, 'preflight_discovery') as discovery, patch.object(echo, 'Zavliq') as factory:
            with self.assertRaisesRegex(ValueError, 'EXECUTE_REQUIRED'):
                await echo.run(argparse.Namespace(execute=False))
        discovery.assert_not_called()
        factory.assert_not_called()
        self.assertFalse(self.evidence.exists())

    async def test_nonadvancing_page_and_history_gap_fail_closed(self):
        for page, code in [({'items': [], 'has_more': True, 'next_cursor': 0}, 'INBOX_CURSOR_DID_NOT_ADVANCE'),
                           ({'items': [], 'has_more': False, 'history_gap_rooms': ['!dm:localhost']}, 'ECHO_HISTORY_INCOMPLETE')]:
            client = Mock()
            client.call = AsyncMock(return_value=page)
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, code):
                await echo.room_messages(client, '!dm:localhost')


if __name__ == '__main__':
    unittest.main()
