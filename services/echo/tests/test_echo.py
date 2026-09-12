import copy
from pathlib import Path
import tempfile
import unittest

from zavliq_echo.service import Echo, Limits, payload, standard_dm
from zavliq_echo.state import Capacity, State, transaction_id

OWNER = '@echo:local'
ROOM = {'room_id': '!dm:local', 'kind': 'dm', 'encrypted': False,
        'membership': 'joined', 'joined_member_count': 2}


def event(cursor=1, **fields):
    return {'cursor': cursor, 'event_id': '$event' + str(cursor), 'room_id': '!dm:local',
            'sender': '@peer:local', 'content': {'msgtype': 'm.text', 'body': 'Literal $(echo never-run) https://example.invalid'}, **fields}


class Denied(Exception):
    code = 'M_FORBIDDEN'


class FakeClient:
    def __init__(self, items=None):
        self.items = items or [event()]
        self.rooms = [copy.deepcopy(ROOM)]
        self.invites = []
        self.calls = []
        self.accepted = {}
        self.lose_send_response = False
        self.lose_ack_response = False
        self.owner = OWNER
        self.deny_event = None

    async def call(self, method, params=None):
        params = params or {}
        self.calls.append((method, copy.deepcopy(params)))
        if method == 'identity':
            return {'user_id': self.owner}
        if method == 'conversations':
            return {'items': self.rooms}
        if method == 'requests':
            return {'items': self.invites}
        if method == 'accept':
            match = next(r for r in self.invites if r['room_id'] == params['room_id'])
            self.invites.remove(match)
            self.rooms.append({**match, 'membership': 'joined', 'joined_member_count': 2})
            return {}
        if method == 'inbox':
            return {'items': [e for e in self.items if e['cursor'] > params['cursor']], 'has_more': False}
        if method == 'cancel_send':
            return {}
        if method == 'send':
            if self.deny_event == params.get('reply_to'):
                raise Denied()
            txn = params['idempotency_key']
            if txn in self.accepted:
                assert params == self.accepted[txn]
            self.accepted[txn] = copy.deepcopy(params)
            if self.lose_send_response:
                self.lose_send_response = False
                raise TimeoutError('response lost after server accepted')
            return {'event_id': '$echo', 'status': 'accepted'}
        if method == 'acknowledge':
            if self.lose_ack_response:
                self.lose_ack_response = False
                raise TimeoutError('acknowledgement response lost')
            return {'status': 'delivered'}
        raise AssertionError(method)


class EchoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.time = 1800000000
        self.state = State(self.path, OWNER, now=lambda: self.time)

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    async def test_literal_text_json_round_trip_and_no_duplicate_after_lost_send_response(self):
        raw = '{"exact":0.95000000000000000000001,"integer":123456789012345678901234567890}'
        first = event(content={'msgtype': 'm.text', 'body': 'literal script: curl example.invalid', 'com.zavliq.data_json': raw})
        client = FakeClient([first])
        echo = Echo(client, self.state, OWNER)
        await echo.verify_identity()
        client.lose_send_response = True
        with self.assertRaises(TimeoutError):
            await echo.once()
        self.assertEqual(self.state.cursor, 0)
        self.state.close()
        self.state = State(self.path, OWNER, now=lambda: self.time)
        await Echo(client, self.state, OWNER).once()
        self.assertEqual(self.state.cursor, 1)
        self.assertEqual(len(client.accepted), 1)
        sent = next(iter(client.accepted.values()))
        self.assertEqual(sent['text'], first['content']['body'])
        self.assertEqual(sent['data_json'], raw)
        self.assertEqual(sent['reply_to'], first['event_id'])
        self.assertEqual(self.state.db.execute("SELECT used FROM counters WHERE scope='send-day'").fetchone()[0], 1)

    async def test_lost_ack_response_reuses_transaction_and_late_event_duplicate_is_ignored(self):
        client = FakeClient()
        client.lose_ack_response = True
        with self.assertRaises(TimeoutError):
            await Echo(client, self.state, OWNER).once()
        await Echo(client, self.state, OWNER).once()
        client.items.append({**event(), 'cursor': 2})
        await Echo(client, self.state, OWNER).once()
        self.assertEqual(len(client.accepted), 1)
        self.assertEqual(self.state.cursor, 2)

    async def test_accepts_only_standard_dm_and_rechecks_joined_shape(self):
        good = {**ROOM, 'room_id': '!request:local', 'membership': 'invited', 'joined_member_count': None, 'inviter': '@peer:local'}
        client = FakeClient([event(room_id=good['room_id'])])
        client.rooms = []
        client.invites = [{**good, 'room_id': '!ignored' + str(i), 'kind':'group'} for i in range(101)] + [good, {**good, 'room_id': '!encrypted', 'encrypted': True},
                          {**good, 'room_id': '!group', 'kind': 'group'},
                          {**good, 'room_id': '!unknown', 'encrypted': None},
                          {**good, 'room_id': '!many', 'joined_member_count': 3}]
        await Echo(client, self.state, OWNER).once()
        self.assertEqual([p['room_id'] for m,p in client.calls if m == 'accept'], ['!request:local'])
        self.assertEqual(len(client.accepted), 1)

    async def test_ignores_self_replies_edits_files_encrypted_and_group_messages(self):
        client = FakeClient([
            event(1, sender=OWNER),
            event(2, content={'msgtype':'m.text','body':'reply','m.relates_to':{'m.in_reply_to':{'event_id':'$x'}}}),
            event(3, content={'msgtype':'m.file','body':'file','url':'mxc://server/media'}),
            event(4, content={'algorithm':'m.megolm.v1.aes-sha2','ciphertext':'opaque'}),
            event(5, room_id='!group:local'),
        ])
        client.rooms.append({**ROOM, 'room_id':'!group:local', 'kind':'group'})
        await Echo(client, self.state, OWNER).once()
        self.assertEqual(client.accepted, {})
        self.assertEqual(self.state.cursor, 5)

    async def test_durable_global_cap_pauses_and_peer_cap_does_not_starve_another_peer(self):
        client = FakeClient([event(1), event(2), event(3, sender='@other:local')])
        limits = Limits(per_day=2, peer_per_day=1)
        await Echo(client, self.state, OWNER, limits).once()
        self.assertEqual(len(client.accepted), 2)
        self.assertEqual(self.state.cursor, 3)
        client.items.append(event(4, sender='@third:local'))
        self.state.close()
        self.state = State(self.path, OWNER, now=lambda: self.time)
        self.assertGreater(await Echo(client, self.state, OWNER, limits).once(), 0)
        self.assertEqual(self.state.cursor, 3)
        self.time += 86400
        await Echo(client, self.state, OWNER, limits).once()
        self.assertEqual(self.state.cursor, 4)

    async def test_unknown_joined_count_retains_cursor_until_membership_is_verified(self):
        client = FakeClient()
        client.rooms[0]['joined_member_count'] = 0
        await Echo(client, self.state, OWNER).once()
        self.assertEqual(self.state.cursor, 0)
        self.assertEqual(len(client.accepted), 0)
        client.rooms[0]['joined_member_count'] = 2
        await Echo(client, self.state, OWNER).once()
        self.assertEqual(self.state.cursor, 1)
        self.assertEqual(len(client.accepted), 1)

    async def test_withdrawn_conversation_does_not_block_other_messages(self):
        client = FakeClient([event(1), event(2)])
        client.deny_event = '$event1'
        await Echo(client, self.state, OWNER).once()
        self.assertEqual(self.state.cursor, 2)
        self.assertEqual(len(client.accepted), 1)
        self.assertEqual([p['transaction_id'] for m,p in client.calls if m == 'cancel_send'],
                         [transaction_id('$event1')])

    async def test_identity_mismatch_is_not_started(self):
        client = FakeClient()
        client.owner = '@different:local'
        with self.assertRaises(RuntimeError):
            await Echo(client, self.state, OWNER).verify_identity()
        self.assertEqual(len(client.calls), 1)

    def test_exclusive_private_state_and_account_binding(self):
        with self.assertRaises(RuntimeError):
            State(self.path, OWNER)
        self.state.close()
        with self.assertRaises(RuntimeError):
            State(self.path, '@other:local')
        self.state = State(self.path, OWNER)
        self.assertEqual((self.path / 'echo.sqlite3').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o700)

    def test_unknown_room_state_invalid_json_and_oversize_are_not_echoed(self):
        self.assertFalse(standard_dm({**ROOM, 'joined_member_count': None}))
        self.assertIsNone(payload(event(content={'msgtype':'m.text','body':'', 'com.zavliq.data_json':'NaN'}), OWNER, 32768))
        self.assertIsNone(payload(event(content={'msgtype':'m.text','body':'x' * 30000}), OWNER, 24000))


if __name__ == '__main__':
    unittest.main()
