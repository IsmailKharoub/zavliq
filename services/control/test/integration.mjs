/** Real local/staging stack check. No credentials or message content are written to stdout. */
import assert from 'node:assert/strict';
import {randomBytes} from 'node:crypto';
import {mkdtemp,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

const control=process.env.ZAVLIQ_CONTROL_URL??'http://127.0.0.1:3001';
const matrix=process.env.ZAVLIQ_MATRIX_URL??'http://127.0.0.1:8008';
const prefix='it'+randomBytes(4).toString('hex');
let checks=0;
const done=name=>{checks++;process.stdout.write(`ok ${checks} - ${name}\n`);};
async function call(base,path,{method='GET',token,body,raw}={}) {
  const response=await fetch(base+path,{method,headers:{...(token?{authorization:`Bearer ${token}`} : {}),...((body!==undefined||raw)?{'content-type':raw?'application/octet-stream':'application/json'}:{})},body:raw??(body!==undefined?JSON.stringify(body):undefined),signal:AbortSignal.timeout(30000)});
  const value=await response.json();return {status:response.status,value};
}
const mat=(session,path,options={})=>call(matrix,'/_matrix/client/v3'+path,{...options,token:session.access_token});
const ctl=(session,path,options={})=>call(control,path,{...options,token:session.access_token});
const expect=(result,status)=>{assert.equal(result.status,status,`Unexpected HTTP response: ${JSON.stringify(result.value)}`);return result.value;};
const enrollmentA={handle:prefix+'a',registration_secret:randomBytes(32).toString('base64url')};
const enrollmentB={handle:prefix+'b',registration_secret:randomBytes(32).toString('base64url')};
const alice=expect(await call(control,'/v1/agents',{method:'POST',body:enrollmentA}),201);
const bob=expect(await call(control,'/v1/agents',{method:'POST',body:enrollmentB}),201);
// Runtime-like private credential persistence before any message interaction.
const privateDirectory=await mkdtemp(join(tmpdir(),'zavliq-server-integration-'));
await writeFile(join(privateDirectory,'sessions.json'),JSON.stringify({alice,bob,enrollmentA,enrollmentB}),{mode:0o600});
assert.deepEqual(expect(await call(control,'/v1/agents',{method:'POST',body:enrollmentA}),200),alice);
assert.deepEqual(expect(await ctl(alice,'/v1/me'),200),{user_id:alice.user_id,device_id:alice.device_id,is_guest:false});
done('registration idempotency and server-bound identity');
expect(await call(control,'/v1/me'),401);
expect(await call(control,'/v1/agents',{method:'POST',body:{...enrollmentA,admin:true}}),400);
done('unauthorized and extra ownership fields rejected');

const create=async(session,kind='dm',invites=[])=>expect(await mat(session,'/createRoom',{method:'POST',body:{visibility:kind==='channel'?'public':'private',preset:kind==='channel'?'public_chat':'private_chat',is_direct:kind==='dm',invite:invites,initial_state:[{type:'com.zavliq.conversation',state_key:'',content:{kind,encryption:'standard'}}]}}),200).room_id;
const room=await create(alice,'dm',[bob.user_id]);
const roomPath=`/rooms/${encodeURIComponent(room)}`;
const initial=expect(await mat(alice,`${roomPath}/send/m.room.message/initial`,{method:'PUT',body:{msgtype:'m.text',body:'Integration pre-accept message'}}),200);
const inviteSync=expect(await mat(bob,'/sync?timeout=0'),200);
assert.ok(inviteSync.rooms?.invite?.[room]);
expect(await mat(bob,`/join/${encodeURIComponent(room)}`,{method:'POST',body:{}}),200);
const history=expect(await mat(bob,`${roomPath}/messages?dir=b&limit=20`),200);
assert.ok(history.chunk.some(event=>event.event_id===initial.event_id));
done('contact invitation and pre-accept message available after join');
const first=expect(await mat(alice,`${roomPath}/send/m.room.message/stable-transaction`,{method:'PUT',body:{msgtype:'m.text',body:'Integration durable send'}}),200);
const repeat=expect(await mat(alice,`${roomPath}/send/m.room.message/stable-transaction`,{method:'PUT',body:{msgtype:'m.text',body:'Integration durable send'}}),200);
assert.equal(first.event_id,repeat.event_id);
done('Matrix stable transaction ID deduplicates send retries');
expect(await mat(alice,`${roomPath}/state/m.room.encryption`,{method:'PUT',body:{algorithm:'m.megolm.v1.aes-sha2'}}),403);
expect(await mat(alice,`${roomPath}/state/com.zavliq.conversation`,{method:'PUT',body:{kind:'channel',encryption:'standard'}}),403);
expect(await mat(alice,`${roomPath}/state/m.room.join_rules`,{method:'PUT',body:{join_rule:'public'}}),403);
done('direct Matrix state writes cannot change conversation privacy/encryption');
expect(await ctl(bob,'/v1/blocks',{method:'POST',body:{user_id:alice.user_id}}),200);
expect(await mat(alice,`${roomPath}/send/m.room.message/blocked`,{method:'PUT',body:{msgtype:'m.text',body:'Integration blocked send'}}),403);
expect(await ctl(bob,`/v1/blocks/${encodeURIComponent(alice.user_id)}`,{method:'DELETE'}),200);
expect(await mat(alice,`${roomPath}/send/m.room.message/unblocked`,{method:'PUT',body:{msgtype:'m.text',body:'Integration unblocked send'}}),200);
done('blocking is enforced by Synapse even through native message endpoints');
const privateRoom=await create(alice,'group');
expect(await mat(bob,`/join/${encodeURIComponent(privateRoom)}`,{method:'POST',body:{}}),403);
done('uninvited agent cannot join a private conversation');

const pair=expect(await call(control,'/v1/pairings',{method:'POST',body:{user_id:alice.user_id,device_display_name:'Integration browser'}}),201);
const pairPath=`/v1/pairings/${pair.pairing_id}`;
expect(await ctl(bob,`${pairPath}/approve`,{method:'POST',body:{confirmation_code:pair.confirmation_code}}),403);
expect(await ctl(alice,`${pairPath}/approve`,{method:'POST',body:{confirmation_code:pair.confirmation_code}}),200);
const paired=expect(await call(control,`${pairPath}/poll`,{method:'POST',body:{pairing_secret:pair.pairing_secret}}),200).credentials;
assert.equal(paired.user_id,alice.user_id);assert.notEqual(paired.device_id,alice.device_id);assert.notEqual(paired.access_token,alice.access_token);
await writeFile(join(privateDirectory,'paired.json'),JSON.stringify(paired),{mode:0o600});
expect(await call(control,`${pairPath}/ack`,{method:'POST',body:{pairing_secret:pair.pairing_secret}}),200);
expect(await call(control,`${pairPath}/poll`,{method:'POST',body:{pairing_secret:pair.pairing_secret}}),409);
expect(await ctl(alice,`/v1/devices/${encodeURIComponent(paired.device_id)}`,{method:'DELETE'}),200);
expect(await ctl(paired,'/v1/me'),401);
done('browser pairing binds identity and creates a separately revocable device');
expect(await call(control,'/_matrix/media/v3/upload?filename=integration.bin',{method:'POST',token:alice.access_token,raw:Buffer.from('integration media')}),200);
expect(await call(control,'/_matrix/media/r0/upload',{method:'POST',raw:Buffer.from('unauthorized')}),401);
done('file transfer uses authenticated quota gateway');

const channel=await create(alice,'channel');
const channelPath=`/rooms/${encodeURIComponent(channel)}`;
expect(await mat(bob,`/join/${encodeURIComponent(channel)}`,{method:'POST',body:{}}),200);
expect(await mat(bob,`${channelPath}/send/m.room.message/subscriber-message`,{method:'PUT',body:{msgtype:'m.text',body:'Subscribers cannot publish'}}),403);
expect(await mat(alice,`${channelPath}/send/m.room.message/publisher-message`,{method:'PUT',body:{msgtype:'m.text',body:'Publisher can publish'}}),200);
expect(await mat(alice,`/directory/list/room/${encodeURIComponent(privateRoom)}`,{method:'PUT',body:{visibility:'public'}}),403);
done('public broadcast channel is joinable and only publishers can send');
process.stdout.write(`Passed ${checks} live integration checks. Private fixtures retained for restart/recovery validation.\n`);
