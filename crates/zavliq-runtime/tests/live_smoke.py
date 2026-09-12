"""Opt-in local/live integration gate. Creates two clearly named test identities.
Run with PYTHONPATH=packages/client-python/src python3 crates/zavliq-runtime/tests/live_smoke.py.
Only pass --control-url for a service you operate. Never prints credentials/content.
"""
import argparse
import asyncio
import json
from pathlib import Path
import secrets
import tempfile
import time
import urllib.parse
import urllib.request
from zavliq import Zavliq, ZavliqError

async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--control-url', default='http://localhost:3001')
    parser.add_argument('--binary', default=str(Path(__file__).resolve().parents[1]/'target/debug/zavliq'))
    args=parser.parse_args()
    started=time.monotonic()
    checks=[]
    with tempfile.TemporaryDirectory(prefix='zavliq-smoke-') as temporary:
        base=Path(temporary)
        options={'binary':args.binary,'control_url':args.control_url,'timeout':180}
        a=Zavliq(data_dir=str(base/'alice'),**options)
        b=Zavliq(data_dir=str(base/'bob'),**options)
        recovered=None
        async def check(name):
            checks.append(name)
            print(json.dumps({'check':name,'status':'passed','elapsed_s':round(time.monotonic()-started,2)}),flush=True)
        async def find(client,event):
            for _ in range(8):
                inbox=await client.call('inbox',{'full':True,'limit':100})
                matches=[m for m in inbox['items'] if m['event_id']==event and 'algorithm' not in m.get('content',{})]
                if matches:return matches[0]
                await asyncio.sleep(.3)
            raise AssertionError('Expected decrypted event missing after sync retries')
        try:
            suffix=secrets.token_hex(5)
            ai=await a.init('arden-'+suffix)
            bi=await b.init('mira-'+suffix)
            assert ai['user_id']!=bi['user_id']
            assert not {'access_token','registration_secret'} & ai.keys()
            assert (await a.init('arden-'+suffix))['device_id']==ai['device_id']
            await check('registration-idempotent-no-secrets')
            room=await a.create_conversation([bi['user_id']])
            requests=await b.call('requests')
            request=next(r for r in requests['items'] if r['room_id']==room['room_id'])
            assert request['kind']=='dm' and request['encrypted'] is False and request['inviter']==ai['user_id']
            print(json.dumps({'check':'invited-metadata','joined_member_count':request['joined_member_count'],'invited_member_count':request['invited_member_count']}),flush=True)
            # Standard messages sent during the request remain visible after accept.
            sent=await a.send(room['room_id'],text='standard hello',idempotency_key='std-1')
            await b.call('accept',{'room_id':room['room_id']})
            # Model losing the successful accept response and restarting before
            # retrying. The real private-room policy rejects a redundant raw join.
            await b.close()
            b=Zavliq(data_dir=str(base/'bob'),**options)
            accepted_again=await b.call('accept',{'room_id':room['room_id']})
            assert accepted_again['room_id']==room['room_id'] and accepted_again['membership']=='joined' and accepted_again['already_joined'] is True
            message=await find(b,sent['event_id'])
            assert message['content']['body']=='standard hello'
            again=await a.send(room['room_id'],text='standard hello',idempotency_key='std-1')
            assert sent['event_id']==again['event_id']
            await b.call('acknowledge',{'event_id':sent['event_id'],'status':'delivered'})
            assert (await a.call('delivery',{'event_id':sent['event_id']}))['receipts']
            structured=await a.send(room['room_id'],data={'confidence':0.95,'count':9007199254740993,'huge':123456789012345678901234567890},idempotency_key='standard-json')
            structured_message=await find(b,structured['event_id'])
            assert structured_message['data']=={'confidence':0.95,'count':9007199254740993,'huge':123456789012345678901234567890}
            own_inbox=await a.call('inbox',{'limit':1})
            assert own_inbox['items']==[] and not own_inbox['has_more']
            assert (await a.call('thread',{'room_id':room['room_id'],'limit':1}))['items'][0]['sender']==ai['user_id']
            raw_json='{ \"exact\": 1.0000000000000000001, \"large\":9007199254740993 }'
            raw_sent=await a.send(room['room_id'],data_json=raw_json,idempotency_key='raw-json')
            assert (await find(b,raw_sent['event_id']))['data_json']==raw_json
            reply=await b.send(room['room_id'],text='peer reply',idempotency_key='reply')
            inbound=await a.call('wait',{'cursor':own_inbox['next_cursor'],'limit':1,'room_id':room['room_id'],'timeout_seconds':2})
            assert inbound['items'][0]['event_id']==reply['event_id'] and inbound['items'][0]['sender']==bi['user_id']
            await check('request-accept-standard-message-dedup-delivery-json-inbound-only')
            encrypted=await a.create_conversation([bi['user_id']],encryption='e2ee')
            try:await a.send(encrypted['room_id'],text='Wait for acceptance',idempotency_key='pending-encrypted')
            except ZavliqError as error:assert error.code=='REQUEST_PENDING'
            else:raise AssertionError('Encrypted message accepted before its recipient joined')
            await a.call('cancel_send',{'transaction_id':'pending-encrypted'})
            await b.call('accept',{'room_id':encrypted['room_id']})
            await a.call('sync')
            own_a=await a.call('crypto_devices')
            own_b=await b.call('crypto_devices')
            device_a=next(d for d in own_a['devices'] if d['device_id']==ai['device_id'])
            device_b=next(d for d in own_b['devices'] if d['device_id']==bi['device_id'])
            await a.call('crypto_devices',{'user_id':bi['user_id']})
            await b.call('crypto_devices',{'user_id':ai['user_id']})
            # The test harness owns both devices and authenticates fingerprints.
            await a.call('verify_device',{'user_id':bi['user_id'],**{k:device_b[k] for k in ('device_id','ed25519')}})
            await b.call('verify_device',{'user_id':ai['user_id'],**{k:device_a[k] for k in ('device_id','ed25519')}})
            marker='encrypted-payload-'+secrets.token_hex(12)
            es=await a.send(encrypted['room_id'],text=marker,data={'verified':True,'confidence':0.95,'count':9007199254740993},idempotency_key='e2ee-1')
            event=await find(b,es['event_id'])
            assert event['content']['body']==marker
            assert event['content']['com.zavliq.data']=={'verified':True,'confidence':0.95,'count':9007199254740993}
            # Inspect only this test-created account, with no secret-bearing output.
            session=json.loads((base/'alice'/'identity.json').read_text())['session']
            url=session['homeserver']+'/_matrix/client/v3/rooms/'+urllib.parse.quote(encrypted['room_id'],safe='')+'/event/'+urllib.parse.quote(es['event_id'],safe='')
            request=urllib.request.Request(url,headers={'Authorization':'Bearer '+session['access_token']})
            with urllib.request.urlopen(request) as response: wire=json.load(response)
            assert wire['type']=='m.room.encrypted' and marker not in json.dumps(wire)
            await check('e2ee-text-json-no-server-plaintext')
            source=base/'payload.bin';source.write_bytes(b'private attachment\x00'+secrets.token_bytes(128))
            upload=await a.call('upload',{'room_id':encrypted['room_id'],'path':str(source),'idempotency_key':'file-1'})
            file_event=await find(b,upload['event_id'])
            assert 'file' in file_event['content'] and 'url' not in file_event['content']
            mxc=urllib.parse.urlparse(file_event['content']['file']['url'])
            media_url=session['homeserver']+'/_matrix/client/v1/media/download/'+mxc.netloc+mxc.path
            media_request=urllib.request.Request(media_url,headers={'Authorization':'Bearer '+session['access_token']})
            with urllib.request.urlopen(media_request) as response: encrypted_bytes=response.read()
            assert encrypted_bytes!=source.read_bytes() and b'private attachment' not in encrypted_bytes
            await b.call('download',{'event_id':upload['event_id'],'path':str(base/'download.bin')})
            assert (base/'download.bin').read_bytes()==source.read_bytes()
            await check('e2ee-attachment-roundtrip')
            await b.close()
            offline=await a.send(encrypted['room_id'],text='offline encrypted message',idempotency_key='offline-1')
            b=Zavliq(data_dir=str(base/'bob'),**options)
            assert (await b.identity())['device_id']==bi['device_id']
            assert (await find(b,offline['event_id']))['content']['body']=='offline encrypted message'
            assert (await find(b,es['event_id']))['content']['body']==marker
            await check('restart-offline-and-crypto-persistence')
            passphrase=base/'passphrase';passphrase.write_text(secrets.token_urlsafe(32));passphrase.chmod(0o600)
            backup=base/'recovery.age'
            await b.call('recovery_export',{'path':str(backup),'passphrase_file':str(passphrase)})
            recovered=Zavliq(data_dir=str(base/'restored'),**options)
            restored=await recovered.call('recovery_import',{'path':str(backup),'passphrase_file':str(passphrase)})
            assert restored['user_id']==bi['user_id'] and restored['device_id']!=bi['device_id']
            assert (await find(recovered,es['event_id']))['content']['body']==marker
            await check('recovery-new-device-restores-identity-and-history')
            print(json.dumps({'result':'passed','checks':checks,'elapsed_s':round(time.monotonic()-started,2)}),flush=True)
        finally:
            await a.close();await b.close()
            if recovered:await recovered.close()

if __name__=='__main__':asyncio.run(main())
