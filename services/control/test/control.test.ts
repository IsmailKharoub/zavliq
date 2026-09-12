import {test} from 'node:test';
import assert from 'node:assert/strict';
import {randomBytes} from 'node:crypto';
import {mkdtempSync,rmSync,readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {buildApp} from '../src/app.js';
import {Store} from '../src/store.js';
import {Synapse} from '../src/synapse.js';
import type {Config} from '../src/config.js';

const key='a'.repeat(64);
const config:Config={serverName:'test.local',synapseUrl:'http://synapse',publicHomeserverUrl:'https://test.local',publicControlUrl:'https://test.local',adminToken:'test-admin-token',dataKey:key,database:':memory:',allowedOrigins:['https://test.local'],port:3001,host:'127.0.0.1',registrationPerHour:3,registrationPerDay:50,registrationOpen:true,pairingPerDay:1000,trustProxy:false};

function upstream() {
  const users=new Map<string,{password:string}>();
  const tokens=new Map<string,{user_id:string;device_id:string}>();
  const loginTokens=new Map<string,string>();
  let loginCount=0;
  const calls:{url:string;method:string;body:Record<string,unknown>;authorization:string|null}[]=[];
  const fetcher=(async(input:string|URL|Request,init?:RequestInit)=>{
    const url=new URL(String(input));
    const headers=new Headers(init?.headers);
    const raw=typeof init?.body==='string'?init.body:'{}';
    const body=JSON.parse(raw) as Record<string,unknown>;
    const method=init?.method??'GET';
    const path=decodeURIComponent(url.pathname);
    const authorization=headers.get('authorization');
    calls.push({url:path,method,body,authorization});
    const response=(status:number,value:unknown)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
    if(path==='/_matrix/client/versions') return response(200,{versions:['v1.15']});
    if(path.startsWith('/_synapse/admin/v2/users/')) {
      assert.equal(authorization,'Bearer test-admin-token');
      const user=path.slice('/_synapse/admin/v2/users/'.length);
      if(method==='PUT') { assert.equal(body.admin,false);users.set(user,{password:body.password as string});return response(201,{name:user}); }
      return users.has(user)?response(200,{name:user}):response(404,{errcode:'M_NOT_FOUND'});
    }
    if(path==='/_matrix/client/v3/login') {
      const user=body.type==='m.login.token'?loginTokens.get(body.token as string):(body.identifier as {user:string}).user;
      if(!user || (body.type!=='m.login.token' && users.get(user)?.password!==body.password)) return response(403,{errcode:'M_FORBIDDEN'});
      if(body.type==='m.login.token') loginTokens.delete(body.token as string);
      const access_token=`test-token-${++loginCount}`;
      const identity={user_id:user,device_id:body.device_id as string};
      tokens.set(access_token,identity);
      return response(200,{...identity,access_token});
    }
    const identity=tokens.get(authorization?.slice(7)??'');
    if(!identity) return response(401,{errcode:'M_UNKNOWN_TOKEN'});
    if(path==='/_matrix/client/v3/account/whoami') return response(200,identity);
    if(path==='/_matrix/media/v3/upload') return response(200,{content_uri:'mxc://test.local/test-file'});
    if(path==='/_synapse/client/zavliq/blocks') return response(200,{user_id:identity.user_id,blocked_user_ids:[]});
    if(path==='/_synapse/client/zavliq/login-token') {
      const login_token=randomBytes(16).toString('hex');loginTokens.set(login_token,identity.user_id);return response(200,{login_token});
    }
    return response(404,{errcode:'M_NOT_FOUND'});
  }) as typeof fetch;
  return {synapse:new Synapse(config.synapseUrl,config.adminToken,fetcher),users,tokens,calls,logins:()=>loginCount};
}

async function setup(options:Partial<Config>={}) {
  const remote=upstream();
  const store=new Store(':memory:',key);
  const app=await buildApp({...config,...options},{store,synapse:remote.synapse});
  return {app,store,remote,close:async()=>{await app.close();store.close();}};
}

test('registration retries return identical credentials, persist across service restart, and never overwrite another account',async()=>{
  const dir=mkdtempSync(join(tmpdir(),'zavliq-control-test-'));
  const database=join(dir,'control.sqlite');
  const remote=upstream();
  let store=new Store(database,key);
  let app=await buildApp(config,{store,synapse:remote.synapse});
  const secret=randomBytes(32).toString('base64url');
  const payload={handle:'alice',registration_secret:secret};
  try {
    const responses=await Promise.all([app.inject({method:'POST',url:'/v1/agents',payload}),app.inject({method:'POST',url:'/v1/agents',payload})]);
    assert.equal(responses[0]!.statusCode,201);
    assert.deepEqual(responses[0]!.json(),responses[1]!.json());
    assert.equal(remote.logins(),1);
    const credentials=responses[0]!.json();
    await app.close();store.close();
    assert.ok(!readFileSync(database).includes(Buffer.from(secret)));
    assert.ok(!readFileSync(database).includes(Buffer.from(credentials.access_token)));
    store=new Store(database,key);app=await buildApp(config,{store,synapse:remote.synapse});
    const again=await app.inject({method:'POST',url:'/v1/agents',payload});
    assert.equal(again.statusCode,200);assert.deepEqual(again.json(),credentials);
    const other=await app.inject({method:'POST',url:'/v1/agents',payload:{...payload,registration_secret:randomBytes(32).toString('base64url')}});
    assert.equal(other.statusCode,409);assert.equal(remote.logins(),1);
    remote.users.set('@outsider:test.local',{password:'not-ours'});
    const preexisting=await app.inject({method:'POST',url:'/v1/agents',payload:{handle:'outsider',registration_secret:secret}});
    assert.equal(preexisting.statusCode,409);assert.equal(remote.users.get('@outsider:test.local')?.password,'not-ours');
  } finally {await app.close();store.close();rmSync(dir,{recursive:true,force:true});}
});

test('revoked enrollment is not silently resurrected; explicit recovery uses a new stable device',async()=>{
  const {app,remote,close}=await setup();
  try {
    const payload={handle:'alice',registration_secret:randomBytes(32).toString('base64url')};
    const original=(await app.inject({method:'POST',url:'/v1/agents',payload})).json();
    remote.tokens.delete(original.access_token);
    assert.equal((await app.inject({method:'POST',url:'/v1/agents',payload})).statusCode,401);
    assert.equal(remote.logins(),1);
    const recovery={...payload,recovery_id:randomBytes(32).toString('base64url')};
    const result=await app.inject({method:'POST',url:'/v1/agents/recover',payload:recovery});
    assert.equal(result.statusCode,200);assert.notEqual(result.json().device_id,original.device_id);
    assert.equal(result.json().encryption_recovery_required,true);
    const retry=await app.inject({method:'POST',url:'/v1/agents/recover',payload:recovery});
    assert.deepEqual(retry.json(),result.json());assert.equal(remote.logins(),2);
  } finally {await close();}
});

test('registration caps persist and forwarded headers cannot evade the configured IP boundary',async()=>{
  const {app,close}=await setup({registrationPerHour:1});
  try {
    const first=await app.inject({method:'POST',url:'/v1/agents',payload:{handle:'alice',registration_secret:randomBytes(32).toString('base64url')}});
    assert.equal(first.statusCode,201);
    const second=await app.inject({method:'POST',url:'/v1/agents',headers:{'x-forwarded-for':'203.0.113.9'},payload:{handle:'bobby',registration_secret:randomBytes(32).toString('base64url')}});
    assert.equal(second.statusCode,429);assert.equal(second.json().error.code,'REGISTRATION_LIMIT');assert.ok(second.headers['retry-after']);
  } finally {await close();}
});

test('auth identity comes from Synapse; malformed and extra ownership fields rejected',async()=>{
  const {app,close}=await setup();
  try {
    assert.equal((await app.inject({url:'/v1/blocks'})).statusCode,401);
    assert.equal((await app.inject({url:'/v1/blocks',headers:{authorization:'Bearer invalid'}})).statusCode,401);
    const session=(await app.inject({method:'POST',url:'/v1/agents',payload:{handle:'alice',registration_secret:randomBytes(32).toString('base64url')}})).json();
    const result=await app.inject({url:'/v1/blocks',headers:{authorization:`Bearer ${session.access_token}`}});
    assert.equal(result.json().user_id,session.user_id);
    const input=await app.inject({method:'POST',url:'/v1/agents',payload:{handle:'mallory',registration_secret:randomBytes(32).toString('base64url'),admin:true}});
    assert.equal(input.statusCode,400);
    assert.equal((await app.inject({method:'POST',url:'/v1/agents',payload:{handle:'admin',registration_secret:randomBytes(32).toString('base64url')}})).statusCode,409);
  } finally {await close();}
});

test('media reservations enforce bytes atomically and legacy upload paths require authentication',async()=>{
  const {app,store,close}=await setup();
  try {
    const payload={handle:'alice',registration_secret:randomBytes(32).toString('base64url')};
    const session=(await app.inject({method:'POST',url:'/v1/agents',payload})).json();
    const headers={authorization:`Bearer ${session.access_token}`,'content-type':'application/octet-stream'};
    assert.equal((await app.inject({method:'POST',url:'/_matrix/media/r0/upload',headers:{'content-type':'application/octet-stream'},payload:Buffer.from('file')})).statusCode,401);
    const upload=await app.inject({method:'POST',url:'/_matrix/media/v3/upload?filename=file.txt',headers,payload:Buffer.from('file')});
    assert.equal(upload.statusCode,200);assert.ok(upload.json().content_uri);
    store.reserveMedia(session.user_id,100*1024*1024-4);
    const capped=await app.inject({method:'POST',url:'/_matrix/media/v1/upload',headers,payload:Buffer.from('x')});
    assert.equal(capped.statusCode,429);assert.equal(capped.json().error.code,'MEDIA_QUOTA');
    assert.equal((await app.inject({method:'POST',url:'/_matrix/media/v1/create',headers,payload:Buffer.from('{}')})).statusCode,405);
    assert.equal((await app.inject({method:'PUT',url:'/_matrix/media/v3/upload/test.local/id',headers,payload:Buffer.from('file')})).statusCode,405);
  } finally {await close();}
});

test('ciphertext is bound to the owning enrollment and key',()=>{
  const store=new Store(':memory:',key);
  try {const sealed=store.seal({token:'private'},'alice');assert.throws(()=>store.unseal(sealed,'bob'));assert.deepEqual(store.unseal(sealed,'alice'),{token:'private'});}
  finally {store.close();}
});

test('pairing requires target identity, code and private poll proof; creates exactly one new device and clears credentials after durable acknowledgement',async()=>{
  const {app,store,remote,close}=await setup();
  try {
    const enroll=async(handle:string)=>(await app.inject({method:'POST',url:'/v1/agents',payload:{handle,registration_secret:randomBytes(32).toString('base64url')}})).json();
    const alice=await enroll('alice'),bob=await enroll('bobby');
    const pair=(await app.inject({method:'POST',url:'/v1/pairings',payload:{user_id:alice.user_id,device_display_name:'Browser test'}})).json();
    const base=`/v1/pairings/${pair.pairing_id}`;
    assert.equal((await app.inject({url:base})).statusCode,401);
    assert.equal((await app.inject({url:base,headers:{authorization:`Bearer ${bob.access_token}`}})).statusCode,403);
    const poll=()=>app.inject({method:'POST',url:`${base}/poll`,payload:{pairing_secret:pair.pairing_secret}});
    assert.equal((await poll()).json().status,'pending');
    assert.equal((await app.inject({method:'POST',url:`${base}/poll`,payload:{pairing_secret:randomBytes(32).toString('base64url')}})).statusCode,401);
    const wrongCode=pair.confirmation_code==='000000'?'111111':'000000';
    assert.equal((await app.inject({method:'POST',url:`${base}/approve`,headers:{authorization:`Bearer ${alice.access_token}`},payload:{confirmation_code:wrongCode}})).statusCode,403);
    assert.equal((await app.inject({method:'POST',url:`${base}/approve`,headers:{authorization:`Bearer ${bob.access_token}`},payload:{confirmation_code:pair.confirmation_code}})).statusCode,403);
    const approve=()=>app.inject({method:'POST',url:`${base}/approve`,headers:{authorization:`Bearer ${alice.access_token}`},payload:{confirmation_code:pair.confirmation_code}});
    assert.equal((await approve()).statusCode,200);assert.equal((await approve()).statusCode,200);
    const ready=(await poll()).json();
    assert.equal(ready.status,'approved');assert.notEqual(ready.credentials.device_id,alice.device_id);assert.notEqual(ready.credentials.access_token,alice.access_token);assert.equal(ready.credentials.user_id,alice.user_id);
    assert.equal(remote.logins(),3);
    assert.deepEqual((await poll()).json(),ready);
    assert.equal((await app.inject({method:'POST',url:`${base}/ack`,payload:{pairing_secret:pair.pairing_secret}})).statusCode,200);
    assert.equal((await poll()).statusCode,409);
    assert.equal((store.db.prepare('SELECT credentials FROM pairings WHERE id=?').get(pair.pairing_id) as {credentials:unknown}).credentials,null);
    store.db.prepare('UPDATE pairings SET expires_at=? WHERE id=?').run(Date.now()-1,pair.pairing_id);
    assert.equal((await app.inject({method:'POST',url:`${base}/ack`,payload:{pairing_secret:pair.pairing_secret}})).statusCode,200);
    assert.equal((await app.inject({method:'POST',url:`${base}/ack`,payload:{pairing_secret:randomBytes(32).toString('base64url')}})).statusCode,401);
  } finally {await close();}
});

test('pairing expires and rejects code guessing without minting a device',async()=>{
  const {app,store,remote,close}=await setup();
  try {
    const alice=(await app.inject({method:'POST',url:'/v1/agents',payload:{handle:'alice',registration_secret:randomBytes(32).toString('base64url')}})).json();
    const pair=(await app.inject({method:'POST',url:'/v1/pairings',payload:{user_id:alice.user_id,device_display_name:'Browser test'}})).json();
    const code=pair.confirmation_code==='000000'?'111111':'000000';
    for(let index=0;index<5;index++) assert.equal((await app.inject({method:'POST',url:`/v1/pairings/${pair.pairing_id}/approve`,headers:{authorization:`Bearer ${alice.access_token}`},payload:{confirmation_code:code}})).statusCode,403);
    assert.equal((await app.inject({method:'POST',url:`/v1/pairings/${pair.pairing_id}/approve`,headers:{authorization:`Bearer ${alice.access_token}`},payload:{confirmation_code:pair.confirmation_code}})).json().error.code,'PAIRING_LOCKED');
    assert.equal(remote.logins(),1);
    store.db.prepare('UPDATE pairings SET expires_at=? WHERE id=?').run(Date.now()-1,pair.pairing_id);
    assert.equal((await app.inject({method:'POST',url:`/v1/pairings/${pair.pairing_id}/poll`,payload:{pairing_secret:pair.pairing_secret}})).statusCode,410);
  } finally {await close();}
});

test('anonymous pairing has a durable global admission bound',async()=>{
  const {app,store,close}=await setup({pairingPerDay:1});
  try {
    const payload={user_id:'@alice:test.local',device_display_name:'Native runtime'};
    assert.equal((await app.inject({method:'POST',url:'/v1/pairings',payload,remoteAddress:'127.0.0.2'})).statusCode,201);
    const response=await app.inject({method:'POST',url:'/v1/pairings',payload,remoteAddress:'127.0.0.3'});
    assert.equal(response.statusCode,429);assert.equal(response.json().error.code,'PAIRING_CAPACITY');
    assert.equal((store.db.prepare('SELECT count(*) AS n FROM pairings').get() as {n:number}).n,1);
  } finally {await close();}
});
