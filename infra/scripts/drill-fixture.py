#!/usr/bin/env python3
"""Explicit local backup/restore drill fixtures; logs checks, never credentials."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import shutil
import urllib.parse
import urllib.request
from zavliq import Zavliq

p=argparse.ArgumentParser()
p.add_argument('phase',choices=['seed','verify'])
p.add_argument('--directory',required=True)
p.add_argument('--origin',required=True)
p.add_argument('--binary',required=True)
a=p.parse_args()
directory=Path(a.directory).resolve()
os.umask(0o077)
directory.mkdir(parents=True,exist_ok=True,mode=0o700)


async def find(client,event_id):
    for _ in range(8):
        inbox=await client.call('inbox',{'full':True,'limit':100})
        matches=[m for m in inbox['items'] if m['event_id']==event_id and 'algorithm' not in m.get('content',{})]
        if matches:return matches[0]
        await asyncio.sleep(.3)
    raise AssertionError('Restored event did not arrive/decrypt')


async def main():
    prefix='' if a.phase=='seed' else 'restored-'
    if a.phase=='verify':
        for who in ['alice','bob']:
            target=directory/(prefix+who)
            if target.exists():raise SystemExit('Use a fresh verification client directory')
            shutil.copytree(directory/who,target)
            identity=target/'identity.json'
            data=json.loads(identity.read_text())
            data['control_url']=a.origin
            data['session']['homeserver']=a.origin
            identity.write_text(json.dumps(data));identity.chmod(0o600)
            # Force history to be fetched from the restored server, not local inbox cache.
            for path in target.glob('inbox.sqlite3*'):path.unlink()
    clients=[Zavliq(binary=a.binary,data_dir=str(directory/(prefix+who)),control_url=a.origin,timeout=180) for who in ['alice','bob']]
    alice,bob=clients
    try:
        if a.phase=='seed':
            suffix=secrets.token_hex(5)
            ai=await alice.init('backup-a-'+suffix);bi=await bob.init('backup-b-'+suffix)
            standard=await alice.create_conversation([bi['user_id']])
            await bob.call('accept',{'room_id':standard['room_id']})
            plain=await alice.send(standard['room_id'],text='restore fixture standard',idempotency_key='restore-standard')
            assert (await find(bob,plain['event_id']))['content']['body']=='restore fixture standard'
            encrypted=await alice.create_conversation([bi['user_id']],encryption='e2ee')
            await bob.call('accept',{'room_id':encrypted['room_id']});await alice.call('sync')
            da=next(d for d in (await alice.call('crypto_devices'))['devices'] if d['device_id']==ai['device_id'])
            db=next(d for d in (await bob.call('crypto_devices'))['devices'] if d['device_id']==bi['device_id'])
            await alice.call('crypto_devices',{'user_id':bi['user_id']});await bob.call('crypto_devices',{'user_id':ai['user_id']})
            await alice.call('verify_device',{'user_id':bi['user_id'],**{k:db[k] for k in ['device_id','ed25519']}})
            await bob.call('verify_device',{'user_id':ai['user_id'],**{k:da[k] for k in ['device_id','ed25519']}})
            marker='backup-secret-'+secrets.token_hex(16)
            secret=await alice.send(encrypted['room_id'],text=marker,idempotency_key='restore-encrypted')
            assert (await find(bob,secret['event_id']))['content']['body']==marker
            source=directory/'attachment.bin';source.write_bytes(secrets.token_bytes(512))
            upload=await alice.call('upload',{'room_id':encrypted['room_id'],'path':str(source),'idempotency_key':'restore-file'})
            await find(bob,upload['event_id'])
            pending=await alice.create_conversation([bi['user_id']])
            identity=json.loads((directory/'alice'/'identity.json').read_text())['session']
            req=urllib.request.Request(a.origin+'/_matrix/client/v3/rooms/'+urllib.parse.quote(encrypted['room_id'],safe='')+'/event/'+urllib.parse.quote(secret['event_id'],safe=''),headers={'Authorization':'Bearer '+identity['access_token']})
            with urllib.request.urlopen(req,timeout=10) as response:wire=json.load(response)
            assert wire['type']=='m.room.encrypted' and marker not in json.dumps(wire)
            evidence={'users':[ai['user_id'],bi['user_id']],'devices':[ai['device_id'],bi['device_id']],'plain':plain['event_id'],'encrypted':secret['event_id'],'marker':marker,'upload':upload['event_id'],'pending':pending['room_id']}
            (directory/'fixture.json').write_text(json.dumps(evidence))
            print(json.dumps({'phase':'seed','ok':True,'checks':['two-independent-identities','standard-history','encrypted-history','encrypted-attachment','pending-request','ciphertext-on-server']}))
        else:
            data=json.loads((directory/'fixture.json').read_text())
            for index,client in enumerate(clients):
                identity=await client.identity()
                assert identity['user_id']==data['users'][index] and identity['device_id']==data['devices'][index]
            assert (await find(bob,data['plain']))['content']['body']=='restore fixture standard'
            assert (await find(bob,data['encrypted']))['content']['body']==data['marker']
            await find(bob,data['upload'])
            await bob.call('download',{'event_id':data['upload'],'path':str(directory/'restored-attachment.bin')})
            assert (directory/'restored-attachment.bin').read_bytes()==(directory/'attachment.bin').read_bytes()
            requests=await bob.call('requests')
            assert data['pending'] in [r['room_id'] for r in requests['items']]
            reply=await bob.send((await find(bob,data['plain']))['room_id'],text='fresh restored delivery',idempotency_key='restore-post-recovery')
            assert (await find(alice,reply['event_id']))['content']['body']=='fresh restored delivery'
            print(json.dumps({'phase':'verify','ok':True,'checks':['identity-and-device-preserved','history-refetched-from-restored-server','standard-history','e2ee-history-with-original-client-keys','encrypted-attachment-integrity','pending-request-preserved','new-message-after-restore']}))
    finally:
        for client in clients:await client.close()

asyncio.run(main())
