"""Opt-in group/channel/permission/crypto-membership integration checks."""
import argparse, asyncio, json, secrets, tempfile, time
from pathlib import Path
import urllib.parse, urllib.request, urllib.error
from zavliq import Zavliq, ZavliqError

async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--control-url',default='http://localhost:3001')
    parser.add_argument('--binary',default=str(Path(__file__).resolve().parents[1]/'target/debug/zavliq'))
    args=parser.parse_args();start=time.monotonic();checks=[]
    def passed(name):
        checks.append(name);print(json.dumps({'check':name,'status':'passed','elapsed_s':round(time.monotonic()-start,2)}),flush=True)
    with tempfile.TemporaryDirectory(prefix='zavliq-rooms-') as tmp:
        base=Path(tmp);suffix=secrets.token_hex(4)
        clients=[Zavliq(binary=args.binary,control_url=args.control_url,data_dir=str(base/name),timeout=120) for name in ('cedar','linden','vale')]
        a,b,c=clients
        async def items(client):return (await client.call('inbox',{'full':True,'limit':100}))['items']
        async def find(client,id):
            for _ in range(5):
                messages=await items(client)
                match=next((m for m in messages if m['event_id']==id and 'algorithm' not in m['content']),None)
                if match:return match
                await asyncio.sleep(.2)
            raise AssertionError('Message missing from recipient inbox')
        def wire(owner,room,event):
            session=json.loads((base/owner/'identity.json').read_text())['session']
            url=session['homeserver']+'/_matrix/client/v3/rooms/'+urllib.parse.quote(room,safe='')+'/event/'+urllib.parse.quote(event,safe='')
            request=urllib.request.Request(url,headers={'Authorization':'Bearer '+session['access_token']})
            with urllib.request.urlopen(request) as response:return json.load(response)
        try:
            identities=await asyncio.gather(*(client.init(name+'-'+suffix) for client,name in zip(clients,('cedar','linden','vale'))))
            ai,bi,ci=identities
            group=await a.create_conversation([bi['user_id'],ci['user_id']],kind='group',name='Shared conversation')
            for client in (b,c):await client.call('accept',{'room_id':group['room_id']})
            greeting=await b.send(group['room_id'],text='Hello everyone',idempotency_key='group-greeting')
            await find(a,greeting['event_id']);await find(c,greeting['event_id'])
            passed('private-group-member-messaging')
            channel=await a.create_conversation([],kind='channel',name='Open updates')
            # Neither reader received an invite: the public room ID suffices.
            for client in (b,c):await client.call('accept',{'room_id':channel['room_id']})
            broadcast=await a.send(channel['room_id'],text='First public update',idempotency_key='channel-first')
            await find(b,broadcast['event_id']);await find(c,broadcast['event_id'])
            try:await b.send(channel['room_id'],text='Unauthorized publication',idempotency_key='subscriber-denied')
            except ZavliqError:pass
            else:raise AssertionError('Subscriber was allowed to publish')
            await b.call('cancel_send',{'transaction_id':'subscriber-denied'})
            local_ack=await b.call('acknowledge',{'event_id':broadcast['event_id'],'status':'delivered'})
            assert local_ack['scope']=='local'
            await a.call('set_publisher',{'room_id':channel['room_id'],'user_id':bi['user_id'],'enabled':True})
            published=await b.send(channel['room_id'],text='Authorized second publisher',idempotency_key='promoted-publisher')
            await find(c,published['event_id'])
            await a.call('set_publisher',{'room_id':channel['room_id'],'user_id':bi['user_id'],'enabled':False})
            passed('public-channel-open-join-publisher-roles-local-receipt')
            await b.call('block',{'user_id':ai['user_id']})
            visible=await a.send(channel['room_id'],text='Other readers still receive this',idempotency_key='channel-after-block')
            await find(c,visible['event_id'])
            assert not any(m['event_id']==visible['event_id'] for m in await items(b))
            await c.call('block',{'user_id':bi['user_id']})
            shared=await b.send(group['room_id'],text='Shared room continues',idempotency_key='group-after-block')
            await find(a,shared['event_id'])
            assert not any(m['event_id']==shared['event_id'] for m in await items(c))
            passed('personal-block-does-not-silence-shared-room')
            await b.call('unblock',{'user_id':ai['user_id']});await c.call('unblock',{'user_id':bi['user_id']})
            encrypted=await a.create_conversation([bi['user_id'],ci['user_id']],kind='group',encryption='e2ee',name='Private exchange')
            for client in (b,c):await client.call('accept',{'room_id':encrypted['room_id']})
            await a.call('sync')
            fingerprints=[]
            for client,identity in zip(clients,identities):
                own=await client.call('crypto_devices')
                fingerprints.append(next(d for d in own['devices'] if d['device_id']==identity['device_id']))
            for sender,identity in zip(clients,identities):
                for target,fp in zip(identities,fingerprints):
                    if target['user_id']==identity['user_id']:continue
                    await sender.call('crypto_devices',{'user_id':target['user_id']})
                    await sender.call('verify_device',{'user_id':target['user_id'],'device_id':fp['device_id'],'ed25519':fp['ed25519']})
            first=await a.send(encrypted['room_id'],text='Before membership change',idempotency_key='encrypted-before')
            await find(b,first['event_id']);await find(c,first['event_id'])
            first_wire=wire('cedar',encrypted['room_id'],first['event_id'])
            await a.call('remove_member',{'room_id':encrypted['room_id'],'user_id':ci['user_id']})
            after=await a.send(encrypted['room_id'],text='After member removal',idempotency_key='encrypted-after')
            await find(b,after['event_id'])
            second_wire=wire('cedar',encrypted['room_id'],after['event_id'])
            assert first_wire['content']['session_id']!=second_wire['content']['session_id']
            try:wire('vale',encrypted['room_id'],after['event_id'])
            except urllib.error.HTTPError as e:assert e.code in (403,404)
            else:raise AssertionError('Removed member fetched a later event')
            passed('encrypted-group-removal-rotates-session-and-denies-later-event')
            try:await a.send(group['room_id'],text='x'*32769,idempotency_key='too-large')
            except ZavliqError as e:assert e.code=='MESSAGE_TOO_LARGE'
            else:raise AssertionError('Oversized message accepted')
            oversized=base/'oversized.bin'
            with oversized.open('wb') as file:file.truncate(10*1024*1024+1)
            try:await a.call('upload',{'room_id':group['room_id'],'path':str(oversized)})
            except ZavliqError as e:assert e.code=='FILE_TOO_LARGE'
            else:raise AssertionError('Oversized file accepted')
            quotas=await a.call('quotas');assert quotas
            passed('client-size-limits-and-service-quota-query')
            print(json.dumps({'result':'passed','checks':checks,'elapsed_s':round(time.monotonic()-start,2)}),flush=True)
        finally:await asyncio.gather(*(client.close() for client in clients))

if __name__=='__main__':asyncio.run(main())
