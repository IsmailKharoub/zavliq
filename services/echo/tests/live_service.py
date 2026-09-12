"""Opt-in peer checks against an already supervised, operator-provisioned Echo."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
import uuid

from zavliq import Zavliq


async def poll(check, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = await check()
        if result:
            return result
        await asyncio.sleep(0.25)
    raise AssertionError('Echo fixture condition was not observed before its deadline')


async def run(args):
    os.umask(0o077)
    target = urlsplit(args.control_url)
    if target.hostname not in ('localhost', '127.0.0.1') or target.port not in (19280, 19380):
        raise ValueError('This fixture requires the isolated Echo check or restore on port 19280 or 19380')
    root = Path(args.fixture_dir)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    suffix = uuid.uuid4().hex[:12]
    async with Zavliq(binary=args.binary, data_dir=str(root / 'peer'), control_url=args.control_url) as peer:
        if not (root / 'peer' / 'identity.json').exists():
            await peer.init('echo-peer-' + suffix, 'Echo operator verification fixture')
        room = (await peer.create_conversation([args.owner], encryption='standard'))['room_id']
        raw = '{"decimal":0.95000000000000000000000001,"integer":123456789012345678901234567890}'
        sent = await peer.call('send', {'room_id': room, 'text': 'Literal supervised echo check',
            'data_json': raw, 'idempotency_key': 'operator-check-' + suffix})

        async def reply():
            rows = (await peer.call('inbox', {'full': True, 'limit': 1000}))['items']
            matches = [row for row in rows if row['sender'] == args.owner
                and row['room_id'] == room and row['content'].get('m.relates_to', {}).get('m.in_reply_to', {}).get('event_id') == sent['event_id']]
            assert len(matches) <= 1
            return matches[0] if matches else None

        received = await poll(reply)
        assert received['content']['body'] == 'Literal supervised echo check'
        assert received['content']['com.zavliq.data_json'] == raw
        print(json.dumps({'check': 'supervised-standard-text-and-exact-json', 'ok': True}), flush=True)
        encrypted = (await peer.create_conversation([args.owner], encryption='e2ee'))['room_id']
        group = (await peer.create_conversation([args.owner], kind='group', encryption='standard'))['room_id']
        # Allow several healthy responder cycles, then inspect authoritative membership.
        await asyncio.sleep(8)
        rooms = {item['room_id']: item for item in (await peer.call('conversations'))['items']}
        for room_id in (encrypted, group):
            assert rooms[room_id]['joined_member_count'] == 1
            assert rooms[room_id]['invited_member_count'] == 1
        print(json.dumps({'check': 'encrypted-and-group-invites-remain-unaccepted', 'ok': True}), flush=True)
        again = await reply()
        assert again['event_id'] == received['event_id']
        (root / 'last-public-result.json').write_text(json.dumps({'owner': args.owner, 'standard_room_id': room,
            'original_event_id': sent['event_id'], 'echo_event_id': received['event_id'], 'encrypted_room_id': encrypted,
            'group_room_id': group, 'checks': 3, 'ok': True}) + '\n')
        print(json.dumps({'check': 'one-reply-after-additional-processing-cycles', 'ok': True}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--control-url', default='http://localhost:19280')
    parser.add_argument('--owner', default='@echo:localhost')
    parser.add_argument('--binary', default='crates/zavliq-runtime/target/debug/zavliq')
    parser.add_argument('--fixture-dir', default='services/echo/.local/fixture')
    asyncio.run(run(parser.parse_args()))
