"""Offline public-driver contract/order tests; no real clients, registrations or TLS calls."""
import argparse
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import ssl
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import public_guard as guard
import public_echo as echo
import public_recovery as recovery

RUN = 'public-recovery-2f76469b507c-1234abcd'


def target(phase='prepare'):
    now = datetime.now(timezone.utc).isoformat()
    return {'environment': 'aws-production', 'final_candidate': True, 'public_verification': True,
        'phase': phase, 'origin': guard.ORIGIN, 'server_name': guard.SERVER, 'project': 'zavliq-production',
        'revision': guard.REVISION, 'source_archive_sha256': guard.SOURCE_HASH,
        'production_manifest_sha256': guard.MANIFEST_HASH, 'native_binary_sha256': guard.BINARY_HASH,
        'images': {name: {'id': value} for name, value in guard.IMAGE_IDS.items()},
        'target_machine_id': ('1' if phase == 'prepare' else '2') * 32,
        'route_machine_id': ('1' if phase == 'prepare' else '2') * 32,
        'route_checked_at': now, 'verification_access_restricted': True,
        **({'source_machine_id': '1' * 32, 'source_writers_fenced': True,
            'encrypted_backup_sha256': 'e' * 64, 'restore_completed_at': now} if phase == 'verify' else {})}


class PublicGuardTests(unittest.TestCase):
    def test_fixed_public_build_origin_and_different_restored_machine(self):
        source = target()
        guard.validate_target(source, 'prepare')
        state = {'provenance': guard.provenance(source), 'source_machine_id': '1' * 32,
                 'prepared_at': datetime.now(timezone.utc).isoformat()}
        restored = target('verify')
        guard.validate_target(restored, 'verify', prepared=state)
        changes = [{'origin': 'http://localhost:28180'}, {'origin': 'https://zavliq.com/'},
            {'origin': 'https://zavliq.com.other.invalid'}, {'server_name': 'localhost'},
            {'revision': 'a' * 40}, {'native_binary_sha256': 'b' * 64},
            {'production_manifest_sha256': 'b' * 64}, {'environment': 'aws-staging'},
            {'project': 'zavliq-load'},
            {'route_checked_at': '2001-01-01T00:00:00Z'}, {'images': dict(list(source['images'].items())[:-1])}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                guard.validate_target(source | change, 'prepare')
        for change in [{'target_machine_id': '1' * 32, 'route_machine_id': '1' * 32},
                       {'source_writers_fenced': False}, {'verification_access_restricted': False}, {'source_machine_id': '3' * 32},
                       {'encrypted_backup_sha256': ''}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                guard.validate_target(restored | change, 'verify', prepared=state)
        guard.validate_target(source | {'verification_access_restricted': False}, 'prepare')

    def connections(self, status=200, advertised=None):
        documents = [{'status': 'ok'}, {'protocol': 'zavliq', 'control_url': advertised or guard.ORIGIN,
            'homeserver': guard.ORIGIN, 'server_name': guard.SERVER, 'registration': {'open': True}},
            {'m.homeserver': {'base_url': guard.ORIGIN}}]
        values = []
        for document in documents:
            connection = Mock()
            response = connection.getresponse.return_value
            response.status = status
            response.getheader.return_value = 'max-age=31536000'
            response.read.return_value = json.dumps(document).encode()
            values.append(connection)
        return values

    def test_https_uses_system_trust_hostname_and_fixed_paths(self):
        connections = self.connections()
        with patch.object(guard.http.client, 'HTTPSConnection', side_effect=connections) as factory:
            result = guard.preflight_discovery()
        self.assertTrue(result['certificate_verified'])
        for call in factory.call_args_list:
            self.assertEqual(call.args, ('zavliq.com', 443))
            self.assertTrue(call.kwargs['context'].check_hostname)
            self.assertEqual(call.kwargs['context'].verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual([c.request.call_args.args[1] for c in connections],
                         ['/health', '/.well-known/zavliq', '/.well-known/matrix/client'])
        for connection in connections: connection.close.assert_called_once()

    def test_redirect_wrong_namespace_and_bad_certificate_cannot_pass(self):
        for options in [{'status': 302}, {'advertised': 'http://localhost:28180'}]:
            with self.subTest(options=options), patch.object(guard.http.client, 'HTTPSConnection',
                                                           side_effect=self.connections(**options)):
                with self.assertRaises(ValueError): guard.preflight_discovery()
        connections = self.connections()
        connections[0].request.side_effect = ssl.SSLCertVerificationError('synthetic certificate failure')
        with patch.object(guard.http.client, 'HTTPSConnection', side_effect=connections):
            with self.assertRaises(ssl.SSLCertVerificationError): guard.preflight_discovery()
        connections[0].close.assert_called_once()

    def test_public_attachment_namespace_redaction_and_plaintext_boundaries(self):
        media = {'url': 'mxc://zavliq.com/file', 'encrypted': True}
        content = {'msgtype': 'm.file', 'file': media}
        guard.attachment({'content': content}, True)
        guard.attachment({'content': {'msgtype': 'm.file', 'url': media['url']}}, False)
        for change in [media | {'encrypted': 1}, media | {'encrypted': False}, media | {'key': 'synthetic'},
                       media | {'url': 'mxc://localhost/file'}, media | {'url': 'https://zavliq.com/file'}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                guard.attachment({'content': content | {'file': change}}, True)
        with self.assertRaises(ValueError): guard.attachment({'content': content | {'url': media['url']}}, True)
        with self.assertRaises(ValueError): guard.attachment({'content': content}, False)

    def test_nonzero_quota_loss_and_expired_window_cannot_pass(self):
        before = {key: {'used': 8 if key == 'messages_per_day' else 2, 'limit': value,
                        'resets_at_ms': int(time.time() * 1000 + 60000)} for key, value in recovery.LIMITS.items()}
        recovery.quota_preserved(before, before)
        after = json.loads(json.dumps(before)); after['messages_per_day']['used'] = 0
        with self.assertRaisesRegex(ValueError, 'COUNTER_MISMATCH'): recovery.quota_preserved(before, after)
        after = json.loads(json.dumps(before)); after['contacts_per_day']['resets_at_ms'] += 86400000
        with self.assertRaisesRegex(ValueError, 'WINDOW_EXPIRED'): recovery.quota_preserved(before, after)

    def test_no_execution_flag_precedes_any_artifact_or_network_access(self):
        for module in (echo, recovery):
            with patch.object(module.guard, 'artifacts', side_effect=AssertionError('no artifact access')):
                with self.assertRaisesRegex(ValueError, 'EXECUTION_NOT_ENABLED'):
                    asyncio.run(module.execute(argparse.Namespace(execute=False)))

    def test_artifact_and_tls_failures_open_no_runtime_or_identity_directory(self):
        for module, run_id in [(echo, 'public-echo-2f76469b507c-1234abcd'), (recovery, RUN)]:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                args = argparse.Namespace(execute=True, phase='prepare', run_id=run_id,
                    revision=guard.REVISION, binary=root/'unused', target=root/'target')
                with (patch.object(module.guard, 'PRIVATE', root/'private'),
                      patch.object(module.guard, 'EVIDENCE', root/'evidence'),
                      patch.object(module.guard, 'artifacts', return_value=root/'unused'),
                      patch.object(module.guard, 'read_target', return_value=target()),
                      patch.object(module.guard, 'preflight_discovery', side_effect=ValueError('PUBLIC_HSTS_REQUIRED')),
                      patch.object(module, 'Zavliq', side_effect=AssertionError('no runtime'))):
                    if module is recovery:
                        with patch.object(module, 'Fixture', side_effect=AssertionError('no fixture')):
                            result = asyncio.run(module.execute(args))
                    else: result = asyncio.run(module.execute(args))
                self.assertEqual(result['error_code'], 'PUBLIC_HSTS_REQUIRED')
                self.assertFalse((root/'private').exists())
                self.assertEqual(len(list((root/'evidence').glob('*-failed-*.json'))), 1)


class FakeServer:
    def __init__(self):
        self.clients, self.identities, self.rooms, self.messages = [], {}, {}, {}
        self.transactions, self.payloads, self.downloads, self.blocked = {}, {}, [], set()
        self.trust_calls, self.init_calls, self.old_offline = 0, 0, []
        self.fail_new_send = False
        self.reset_quota = False
        self.reset_block = False
        self.accept_pending = False
        self.reset_membership = False
        self.allow_forbidden_invite = False
        self.bad_file = False
        self.quota_used = 10
        self.reset_at = (int(time.time() // 86400) + 1) * 86400000

    def factory(self, **options):
        assert options['control_url'] == guard.ORIGIN
        client = FakeClient(self, Path(options['data_dir']))
        self.clients.append(client)
        return client

    def put(self, client, room, key, content, data_json=None):
        if key in self.transactions: return {'event_id': self.transactions[key]}
        event = '$event' + str(len(self.messages))
        self.messages[event] = {'event_id': event, 'room_id': room,
            'sender': self.identities[client.name]['user_id'], 'content': content,
            **({'data_json': data_json} if data_json is not None else {})}
        self.transactions[key] = event
        if '-old-' in key:
            self.old_offline.append(all(c.closed for c in self.clients if c.name == 'b'))
        if '-new-' in key: self.quota_used += 1
        return {'event_id': event}


class FakeClient:
    def __init__(self, server, directory):
        self.server, self.directory, self.name = server, directory, directory.name
        self.closed = False

    async def init(self, handle):
        self.server.init_calls += 1
        identity = {'user_id': '@' + handle + ':zavliq.com', 'device_id': self.name.upper(), 'homeserver': guard.ORIGIN}
        self.server.identities[self.name] = identity
        (self.directory/'identity.json').write_text('synthetic credential-shaped private fixture')
        with closing(sqlite3.connect(self.directory/'inbox.sqlite3')) as database, database:
            database.execute('CREATE TABLE events(event_id TEXT PRIMARY KEY)')
        return identity

    async def identity(self): return self.server.identities[self.name]

    async def create_conversation(self, members, *, kind='dm', encryption='standard'):
        room = '!room' + str(len(self.server.rooms)) + ':zavliq.com'
        self.server.rooms[room] = {'creator': self.name, 'members': {self.name}, 'invited': set(members),
                                  'kind': kind, 'encryption': encryption}
        return {'room_id': room}

    async def send(self, room, *, text=None, data_json=None, idempotency_key):
        if self.server.fail_new_send and '-new-' in idempotency_key:
            self.server.fail_new_send = False
            raise ValueError('SYNTHETIC_INTERRUPTION')
        return self.server.put(self, room, idempotency_key, {'body': text} if text is not None else {}, data_json)

    async def call(self, method, params=None):
        params = params or {}
        if method == 'accept':
            room = self.server.rooms[params['room_id']]
            room['members'].add(self.name)
            room['invited'].discard(self.server.identities[self.name]['user_id'])
            return {}
        if method == 'conversations':
            rows = []
            for room_id, room in self.server.rooms.items():
                if self.name not in room['members']: continue
                pending = guard.OWNER in room['invited']
                rows.append({'room_id': room_id, 'kind': room['kind'], 'encryption': room['encryption'],
                    'encrypted': room['encryption']=='e2ee',
                    'membership': 'left' if self.server.reset_membership and room['kind']=='channel' else 'joined',
                    'joined_member_count': 2 if pending and self.server.accept_pending else len(room['members']),
                    'invited_member_count': 0 if pending and self.server.accept_pending else len(room['invited'])})
            return {'items': rows}
        if method == 'block': self.server.blocked.add(params['user_id']); return {}
        if method == 'blocks': return {'blocked_user_ids': [] if self.server.reset_block else sorted(self.server.blocked)}
        if method == 'crypto_devices': return {'devices': [{'device_id': self.name.upper(), 'ed25519': 'public-fingerprint'}]}
        if method == 'verify_device': self.server.trust_calls += 1; return {'verified': True}
        if method == 'sync': return {}
        if method == 'quotas':
            return {'user_id': self.server.identities[self.name]['user_id'], 'quotas': {
                key: {'used': 0 if self.server.reset_quota else self.server.quota_used if key=='messages_per_day' else 2,
                      'limit': value, 'resets_at_ms': self.server.reset_at} for key,value in recovery.LIMITS.items()}}
        if method == 'inbox':
            rows = [item for item in self.server.messages.values() if item['room_id']==params['room_id']
                    and item['sender'] != self.server.identities[self.name]['user_id']]
            cursor = params['cursor']; page = rows[cursor:cursor+1]
            with closing(sqlite3.connect(self.directory/'inbox.sqlite3')) as database, database:
                database.executemany('INSERT OR IGNORE INTO events VALUES(?)', [(item['event_id'],) for item in page])
            return {'items': page, 'next_cursor': cursor+len(page), 'has_more': cursor+len(page)<len(rows)}
        if method == 'upload':
            content = {'msgtype': 'm.file'}
            if self.server.rooms[params['room_id']]['encryption']=='e2ee':
                content['file'] = {'url': 'mxc://zavliq.com/file', 'encrypted': True}
            else: content['url'] = 'mxc://zavliq.com/file'
            event = self.server.put(self, params['room_id'], params['idempotency_key'], content)
            self.server.payloads[event['event_id']] = Path(params['path']).read_bytes()
            return event
        if method == 'download':
            self.server.downloads.append(params['path'])
            with Path(params['path']).open('xb') as stream:
                stream.write(b'wrong file' if self.server.bad_file else self.server.payloads[params['event_id']])
            return {}
        if method == 'invite':
            assert self.server.rooms[params['room_id']]['creator']==self.name
            if self.server.allow_forbidden_invite: return {}
            raise recovery.guard.ZavliqError('M_FORBIDDEN', 'synthetic refusal')
        raise AssertionError('Unexpected method '+method)

    async def close(self): self.closed=True


class PublicRecoveryPhaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.server = FakeServer()
        self.fixture = recovery.Fixture(self.folder, '/never-executed', self.server.factory)

    async def asyncTearDown(self): self.temp.cleanup()

    async def prepared(self):
        result = await self.fixture.prepare(target(), RUN)
        return result, json.loads(self.fixture.state_path.read_text())

    async def test_three_signup_budget_original_stores_all_unseen_history_and_downloads(self):
        prepared, state = await self.prepared()
        self.assertEqual(self.server.init_calls, 2)
        self.assertEqual(self.server.old_offline, [True]*8)
        baseline=self.server.transactions[RUN+'-baseline']
        self.assertEqual(self.server.messages[baseline]['room_id'],state['rooms']['trust_e2ee'])
        self.assertNotEqual(state['rooms']['trust_e2ee'],state['rooms']['e2ee'])
        guard.base.absent_events(self.folder, [x['event_id'] for x in state['historical_events'].values()])
        identities = guard.base.identity_digest(self.folder)
        result = await self.fixture.verify(target('verify'), state)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(len(result['new_events']), 9)
        self.assertEqual(self.server.trust_calls, 2)
        self.assertEqual(self.server.init_calls, 2)
        self.assertEqual(len(set(self.server.downloads)), 2)
        self.assertEqual(guard.base.identity_digest(self.folder), identities)
        self.assertTrue(all(c.closed for c in self.server.clients))
        public = json.dumps([prepared,result])
        for forbidden in ['credential-shaped', 'identity_file_hashes', state['text'], state['data_json']]:
            self.assertNotIn(forbidden, public)

    async def test_counter_block_pending_membership_and_file_loss_each_fail(self):
        for fault in ('reset_quota','reset_block','accept_pending','reset_membership','bad_file','allow_forbidden_invite'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                server=FakeServer(); fixture=recovery.Fixture(Path(temporary),'/never-executed',server.factory)
                await fixture.prepare(target(),RUN)
                state=json.loads(fixture.state_path.read_text())
                setattr(server,fault,True)
                with self.assertRaises(ValueError): await fixture.verify(target('verify'),state)
                self.assertTrue(all(c.closed for c in server.clients))

    async def test_cached_history_or_changed_identity_cannot_open_runtime(self):
        _,state=await self.prepared()
        count=len(self.server.clients)
        event=next(iter(state['historical_events'].values()))['event_id']
        with closing(sqlite3.connect(self.folder/'b/inbox.sqlite3')) as database,database:
            database.execute('INSERT INTO events VALUES(?)',(event,))
        with self.assertRaisesRegex(ValueError,'ALREADY_CACHED'): await self.fixture.verify(target('verify'),state)
        self.assertEqual(len(self.server.clients),count)
        (self.folder/'a/identity.json').write_text('changed original')
        with self.assertRaisesRegex(ValueError,'IDENTITY_FILES_CHANGED'): await self.fixture.verify(target('verify'),state)
        self.assertEqual(len(self.server.clients),count)

    async def test_interrupted_verify_preserves_initial_empty_proof_and_downloads_fresh(self):
        _,state=await self.prepared()
        restored=target('verify')
        self.server.fail_new_send=True
        with self.assertRaisesRegex(ValueError,'SYNTHETIC_INTERRUPTION'):
            await self.fixture.verify(restored,state)
        saved=json.loads(self.fixture.state_path.read_text())
        self.assertTrue(saved['historical_cache_empty_before_first_sync'])
        first=saved['first_verify_at']
        self.server.quota_used+=1
        result=await self.fixture.verify(restored,saved)
        self.assertEqual(result['status'],'passed')
        self.assertEqual(saved['first_verify_at'],first)
        self.assertEqual(len(set(self.server.downloads)),4)
        self.assertEqual(self.server.trust_calls,2)
        self.assertEqual(self.server.init_calls,2)


class PublicEchoAccountingTests(unittest.TestCase):
    def row(self, event='$reply', relation='$original'):
        return {'event_id':event,'sender':guard.OWNER,'content':{'body':'literal',
            'm.relates_to':{'m.in_reply_to':{'event_id':relation}}}}

    def test_unrelated_duplicate_changed_or_missing_replies_cannot_pass(self):
        expected={'$original':{'text':'literal'}}
        self.assertEqual(echo.replies([self.row()],expected,{}),{'$original':'$reply'})
        for rows,observed in [([self.row(relation='$other')],{}),
                              ([self.row(),self.row(event='$duplicate')],{}),
                              ([self.row()],{'$original':'$old'}),([] ,{'$original':'$reply'})]:
            with self.subTest(rows=rows),self.assertRaises(ValueError):echo.replies(rows,expected,observed)


class EchoServer:
    def __init__(self):
        self.transactions, self.messages, self.rooms, self.clients = {}, [], [], []
        self.init_calls, self.read_replies = 0, set()
        self.stop_after = None
        self.unrelated = False
        self.changed_device = False
        self.lose_response_for = None
        self.send_calls = []

    def factory(self, **options):
        client = EchoPeer(self, Path(options['data_dir']))
        self.clients.append(client)
        return client


class EchoPeer:
    def __init__(self, server, folder): self.server,self.folder,self.closed=server,folder,False
    async def __aenter__(self): return self
    async def __aexit__(self,*_): self.closed=True
    async def init(self,handle,display_name):
        self.server.init_calls+=1
        self.server.identity={'user_id':'@'+handle+':zavliq.com','device_id':'FINCH','homeserver':guard.ORIGIN}
        self.folder.mkdir(mode=0o700)
        (self.folder/'identity.json').write_text('synthetic original Finch fixture')
        return self.server.identity
    async def identity(self):return self.server.identity
    async def create_conversation(self,members,*,kind='dm',encryption='standard'):
        room='!'+str(len(self.server.rooms))+':zavliq.com'
        self.server.rooms.append(room)
        return {'room_id':room}
    async def call(self,method,params=None):
        params=params or {}
        if method=='send':
            key=params['idempotency_key']
            self.server.send_calls.append(key)
            if key in self.server.transactions:return {'event_id':self.server.transactions[key]}
            if key.endswith('-barrier-2'):
                earlier=key.removesuffix('2')+'1'
                assert self.server.transactions[earlier] in self.server.read_replies
            event='$sent'+str(len(self.server.transactions)+1)
            self.server.transactions[key]=event
            if self.server.stop_after is None or len(self.server.transactions)<=self.server.stop_after:
                content={'body':params['text'],'m.relates_to':{'m.in_reply_to':{'event_id':event}}}
                if 'data_json' in params:content['com.zavliq.data_json']=params['data_json']
                self.server.messages.append({'event_id':'$reply'+str(len(self.server.messages)),
                    'room_id':params['room_id'],'sender':guard.OWNER,'content':content})
            if self.server.unrelated:
                self.server.messages.append({'event_id':'$unrelated','room_id':params['room_id'],
                    'sender':guard.OWNER,'content':{'body':'extra unrelated reply'}})
                self.server.unrelated=False
            if key == self.server.lose_response_for:
                # The event and responder output already exist; only the RPC
                # response is lost, before the driver can save its event ID.
                self.server.lose_response_for = None
                raise ValueError('SYNTHETIC_ACCEPTED_RESPONSE_LOST')
            return {'event_id':event}
        if method=='crypto_devices':
            return {'devices':[{'device_id':'CHANGED' if self.server.changed_device else 'ECHO','ed25519':'public-fixture-key'}]}
        if method=='conversations':
            return {'items':[{'room_id':room,'joined_member_count':1,'invited_member_count':1} for room in self.server.rooms[1:]]}
        if method=='inbox':
            rows=self.server.messages[params['cursor']:params['cursor']+1]
            for row in rows:
                self.server.read_replies.add(row['content'].get('m.relates_to',{}).get('m.in_reply_to',{}).get('event_id'))
            return {'items':rows,'has_more':params['cursor']+len(rows)<len(self.server.messages),
                    'next_cursor':params['cursor']+len(rows),'history_gap_rooms':[]}
        raise AssertionError(method)


class PublicEchoPhaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.server=EchoServer()
        self.args=argparse.Namespace(execute=True,phase='prepare',run_id='public-echo-2f76469b507c-5678abcd',
                                    revision=guard.REVISION,binary=self.root/'unused',target=self.root/'target')
        self.target=target()
        self.patches=[patch.object(echo.guard,'PRIVATE',self.root/'private'),
            patch.object(echo.guard,'EVIDENCE',self.root/'evidence'),
            patch.object(echo.guard,'artifacts',return_value=self.root/'unused'),
            patch.object(echo.guard,'read_target',side_effect=lambda _:self.target),
            patch.object(echo.guard,'preflight_discovery',return_value={'registration_open':True}),
            patch.object(echo,'Zavliq',side_effect=self.server.factory),patch.object(echo,'REPLY_TIMEOUT',.002)]
        for value in self.patches:value.start()
    async def asyncTearDown(self):
        for value in reversed(self.patches):value.stop()
        self.temp.cleanup()

    async def test_one_signup_then_same_identity_original_echo_and_five_total_replies(self):
        first=await echo.execute(self.args)
        self.assertTrue(first['ok']);self.assertEqual(first['reply_count'],3)
        self.args.phase='verify';self.target=target('verify')
        second=await echo.execute(self.args)
        self.assertTrue(second['ok']);self.assertEqual(second['reply_count'],5)
        self.assertEqual(self.server.init_calls,1)
        self.assertTrue(second['original_echo_devices_preserved'])
        self.assertTrue(all(c.closed for c in self.server.clients))

    async def test_stopped_echo_fails_and_retry_cannot_create_more_signup(self):
        self.server.stop_after=1
        result=await echo.execute(self.args)
        self.assertEqual(result['error_code'],'ECHO_REPLY_TIMEOUT')
        self.assertEqual(self.server.init_calls,1)
        before=(self.root/'private'/self.args.run_id/'state.json').read_bytes()
        again=await echo.execute(self.args)
        self.assertEqual(again['error_code'],'FRESH_PUBLIC_ECHO_STORE_REQUIRED')
        self.assertEqual(self.server.init_calls,1)
        self.assertEqual((self.root/'private'/self.args.run_id/'state.json').read_bytes(),before)

    async def test_replacement_echo_device_cannot_pass(self):
        self.assertTrue((await echo.execute(self.args))['ok'])
        self.args.phase='verify';self.target=target('verify');self.server.changed_device=True
        result=await echo.execute(self.args)
        self.assertEqual(result['error_code'],'ORIGINAL_ECHO_DEVICES_CHANGED')
        self.assertEqual(self.server.init_calls,1)

    async def accepted_response_lost(self, number):
        self.assertTrue((await echo.execute(self.args))['ok'])
        self.args.phase='verify';self.target=target('verify')
        key=self.args.run_id+f'-verify-barrier-{number}'
        self.server.lose_response_for=key
        failed=await echo.execute(self.args)
        self.assertEqual(failed['error_code'],'SYNTHETIC_ACCEPTED_RESPONSE_LOST')
        state=json.loads((self.root/'private'/self.args.run_id/'state.json').read_text())
        original=self.server.transactions[key]
        self.assertNotIn(original,state['expected'])
        self.assertEqual(state['barrier_intents']['verify'][str(number)],{})
        self.assertTrue(any(row['content']['m.relates_to']['m.in_reply_to']['event_id']==original
                            for row in self.server.messages))
        return key,original

    async def test_first_accepted_barrier_lost_response_reconciles_before_history(self):
        key,original=await self.accepted_response_lost(1)
        self.assertNotIn(self.args.run_id+'-verify-barrier-2',self.server.transactions)
        result=await echo.execute(self.args)
        self.assertTrue(result['ok']);self.assertEqual(result['reply_count'],5)
        self.assertEqual(self.server.transactions[key],original)
        self.assertEqual(len(self.server.transactions),5)
        self.assertEqual(len(self.server.messages),5)
        self.assertEqual(self.server.init_calls,1)
        self.assertTrue(all(client.closed for client in self.server.clients))

    async def test_second_accepted_barrier_lost_response_reconciles_before_first_wait(self):
        key,original=await self.accepted_response_lost(2)
        result=await echo.execute(self.args)
        self.assertTrue(result['ok']);self.assertEqual(result['reply_count'],5)
        self.assertEqual(self.server.transactions[key],original)
        self.assertEqual(len(self.server.transactions),5)
        self.assertEqual(len(self.server.messages),5)
        self.assertEqual(self.server.init_calls,1)

    async def test_lost_response_reconciliation_still_rejects_unrelated_reply(self):
        await self.accepted_response_lost(1)
        self.server.messages.append({'event_id':'$unrelated','room_id':self.server.rooms[0],
            'sender':guard.OWNER,'content':{'body':'unrelated'}})
        result=await echo.execute(self.args)
        self.assertEqual(result['error_code'],'UNEXPECTED_ECHO_MESSAGE')
        self.assertNotIn(self.args.run_id+'-verify-barrier-2',self.server.transactions)

    async def test_lost_response_reconciliation_still_rejects_duplicate_reply(self):
        _,original=await self.accepted_response_lost(1)
        duplicate=next(row for row in self.server.messages
                       if row['content']['m.relates_to']['m.in_reply_to']['event_id']==original)
        self.server.messages.append(duplicate|{'event_id':'$duplicate'})
        result=await echo.execute(self.args)
        self.assertEqual(result['error_code'],'DUPLICATE_ECHO_REPLY')
        self.assertNotIn(self.args.run_id+'-verify-barrier-2',self.server.transactions)

    async def test_accepted_lost_response_without_echo_reply_cannot_advance_barrier(self):
        self.assertTrue((await echo.execute(self.args))['ok'])
        self.args.phase='verify';self.target=target('verify')
        key=self.args.run_id+'-verify-barrier-1'
        self.server.stop_after=3
        self.server.lose_response_for=key
        self.assertEqual((await echo.execute(self.args))['error_code'],'SYNTHETIC_ACCEPTED_RESPONSE_LOST')
        self.assertIn(key,self.server.transactions)
        self.assertEqual(len(self.server.messages),3)
        result=await echo.execute(self.args)
        self.assertEqual(result['error_code'],'ECHO_REPLY_TIMEOUT')
        self.assertNotIn(self.args.run_id+'-verify-barrier-2',self.server.transactions)
        self.assertEqual(len(self.server.transactions),4)
        self.assertEqual(self.server.init_calls,1)


if __name__=='__main__':unittest.main()
