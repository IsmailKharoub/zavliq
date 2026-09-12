import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, chmod, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Zavliq } from '../dist/index.js';

async function fixture(t) {
  const dir = await mkdtemp(join(tmpdir(), 'zavliq-client-test-'));
  const binary = join(dir, 'runtime');
  await writeFile(binary, `#!/usr/bin/env node
import('node:readline').then(({createInterface})=>{
createInterface({input:process.stdin}).on('line', line=>{
const r=JSON.parse(line); if(r.method==='exit'){process.exit(0);return;}
process.stdout.write(JSON.stringify({jsonrpc:'2.0',method:'message_available',params:{received:1}})+'\\n');
const result = r.method==='reject' ? {error:{data:{code:'LIMIT_EXCEEDED',message:'Wait before retry',action:'retry after delay'}}} : {result:r.params};
const out=JSON.stringify({jsonrpc:'2.0',id:r.id,...result})+'\\n';
process.stdout.write(out.slice(0,10));process.stdout.write(out.slice(10));
});});
`);
  await chmod(binary, 0o700);
  const client = new Zavliq({binary, timeoutMs: 3000});
  t.after(async()=>{client.close();await rm(dir,{recursive:true,force:true});});
  return client;
}
test('preserves JSON across fragmented responses and notifications', async t=>{
  const client=await fixture(t);let notifications=0;client.on('notification',()=>notifications++);
  const input={text:'line one\nline two',data:{nested:[1,2]}};
  assert.deepEqual(await client.call('roundtrip',input),input);assert.equal(notifications,1);
});
test('concurrent requests resolve by ID',async t=>{const c=await fixture(t);assert.deepEqual(await Promise.all(Array.from({length:10},(_,n)=>c.call('roundtrip',{n}))),Array.from({length:10},(_,n)=>({n})));});
test('service error is actionable',async t=>{const c=await fixture(t);await assert.rejects(c.call('reject'),e=>e.code==='LIMIT_EXCEEDED'&&e.action==='retry after delay');});
test('exit rejects pending operations and wakes notification listeners',async t=>{const c=await fixture(t);const terminal=new Promise(resolve=>c.on('notification',value=>{if(value.params?.closed)resolve(value);}));await assert.rejects(c.call('exit'),e=>e.code==='RUNTIME_CLOSED');assert.deepEqual((await terminal).params.closed,true);});

test('unserializable payload fails without breaking next request',async t=>{const c=await fixture(t);const cyclic={};cyclic.self=cyclic;await assert.rejects(c.call('roundtrip',cyclic),e=>e.code==='INVALID_PARAMS');assert.deepEqual(await c.call('roundtrip',{valid:true}),{valid:true});});

test('inbox sync is fresh by default and explicitly local',async t=>{const c=await fixture(t);assert.equal((await c.inbox()).sync,true);assert.deepEqual(await c.inbox(7,4,{sync:false}),{cursor:7,limit:4,sync:false});});
