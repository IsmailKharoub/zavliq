import test from 'node:test';
import assert from 'node:assert/strict';
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';

test('MCP initializes and publishes secret-free actionable tool schemas',async()=>{
  const dir=await mkdtemp(join(tmpdir(),'zavliq-mcp-test-'));
  const transport=new StdioClientTransport({command:process.execPath,args:[resolve('src/index.mjs')],env:{...process.env,ZAVLIQ_DATA_DIR:dir,ZAVLIQ_BINARY:resolve('../../crates/zavliq-runtime/target/debug/zavliq')}});
  const client=new Client({name:'protocol-test',version:'1.0.0'});
  try {
    await client.connect(transport);
    const {tools}=await client.listTools();
    assert.ok(tools.some(t=>t.name==='zavliq_send'));
    assert.ok(tools.some(t=>t.name==='zavliq_wait'));
    assert.equal(JSON.stringify(tools).includes('access_token'),false);
    const response=await client.callTool({name:'zavliq_identity',arguments:{}});
    assert.equal(response.isError,true);
    assert.match(response.content[0].text,/NOT_INITIALIZED/);
  } finally {await client.close();await rm(dir,{recursive:true,force:true});}
});
