"""Opt-in two-identity round trip against a local/operator-owned stack."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

from zavliq import Zavliq
from zavliq_echo.service import Echo
from zavliq_echo.state import State


class LoseOneSendResponse:
    def __init__(self, client):
        self.client = client
        self.lost = False

    async def call(self, method, params=None):
        result = await self.client.call(method, params)
        if method == 'send' and not self.lost:
            self.lost = True
            raise TimeoutError('Deliberate local response-loss fixture')
        return result


async def poll(test, timeout=30):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        result = await test()
        if result:
            return result
        await asyncio.sleep(0.2)
    raise AssertionError('Expected condition was not observed within the test deadline')


async def run(args):
    os.umask(0o077)
    root = Path(args.fixture_dir) if args.fixture_dir else Path(tempfile.mkdtemp(prefix='zavliq-echo-live-'))
    suffix = uuid.uuid4().hex[:12]
    identity_path, journal_path = root/'echo', root/'journal'
    def client(path):
        return Zavliq(binary=args.binary, data_dir=str(path), control_url=args.control_url)
    peer = client(root/'peer')
    responder = client(identity_path)
    state = None
    try:
        owner = ((await responder.identity()) if (identity_path/'identity.json').exists() else (await responder.init('echo-test-' + suffix, 'Zavliq echo test fixture')))['user_id']
        if not (root/'peer'/'identity.json').exists():
            await peer.init('echo-peer-' + suffix, 'Zavliq echo peer fixture')
        state = State(journal_path, owner)
        echo = Echo(responder, state, owner)
        await echo.verify_identity()
        await echo.once()
        room = (await peer.create_conversation([owner], encryption='standard'))['room_id']
        raw = '{"number":0.95000000000000000000000001,"integer":123456789012345678901234567890}'
        message = await peer.call('send', {'room_id':room, 'text':'Zavliq literal echo round trip',
                                          'data_json':raw, 'idempotency_key':'echo-first-' + suffix})
        original = message['event_id']
        async def get_reply():
            await echo.once()
            rows = (await peer.call('inbox', {'full':True, 'limit':100}))['items']
            return next((r for r in rows if r['sender'] == owner and r['content'].get('m.relates_to',{}).get('m.in_reply_to',{}).get('event_id') == original), None)
        first = await poll(get_reply)
        assert first['content']['body'] == 'Zavliq literal echo round trip'
        assert first['content']['com.zavliq.data_json'] == raw
        assert first['content']['m.relates_to']['m.in_reply_to']['event_id'] == original
        print(json.dumps({'check':'standard-dm-literal-text-and-exact-json', 'ok':True}), flush=True)
        second = await peer.call('send', {'room_id':room,'text':'restart round trip', 'idempotency_key':'echo-second-' + suffix})
        lossy = LoseOneSendResponse(responder)
        echo = Echo(lossy, state, owner)
        async def lose_response():
            try:
                await echo.once()
            except TimeoutError:
                return True
            return False
        await poll(lose_response)
        state.close()
        state = None
        await responder.close()
        responder = client(identity_path)
        state = State(journal_path, owner)
        echo = Echo(responder, state, owner)
        await echo.verify_identity()
        async def restarted_reply():
            await echo.once()
            rows = (await peer.call('inbox', {'full':True,'limit':100}))['items']
            matches = [r for r in rows if r['sender']==owner and r['content'].get('m.relates_to',{}).get('m.in_reply_to',{}).get('event_id')==second['event_id']]
            assert len(matches) <= 1
            return matches
        await poll(restarted_reply)
        await echo.once()
        print(json.dumps({'check':'restart-after-lost-send-response-deduplicates', 'ok':True}), flush=True)
        await peer.call('send', {'room_id':room,'text':'ignored reply', 'reply_to':first['event_id'], 'idempotency_key':'echo-no-loop-' + suffix})
        group = (await peer.create_conversation([owner], kind='group'))['room_id']
        encrypted = (await peer.create_conversation([owner], encryption='e2ee'))['room_id']
        for _ in range(3):
            await echo.once()
        rows=(await peer.call('inbox', {'full':True,'limit':100}))['items']
        assert len([r for r in rows if r['sender']==owner and r['room_id']==room]) == 2
        pending=(await responder.call('requests'))['items']
        assert {group, encrypted}.issubset({r['room_id'] for r in pending})
        print(json.dumps({'check':'replies-groups-and-encrypted-invites-ignored', 'ok':True}), flush=True)
    finally:
        await responder.close()
        await peer.close()
        if state:
            state.close()
    print(json.dumps({'ok':True,'checks':3}), flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--control-url', default='http://localhost:8080')
    parser.add_argument('--binary', default='crates/zavliq-runtime/target/debug/zavliq')
    parser.add_argument('--fixture-dir', help='Reuse only a private directory created by this fixture test')
    asyncio.run(run(parser.parse_args()))
