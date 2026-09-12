import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from zavliq import Zavliq, ZavliqError

FAKE = '''#!/usr/bin/env python3
import sys,json
for line in sys.stdin:
    r=json.loads(line)
    if r['method']=='exit': break
    if r['method']=='reject': out={'error':{'code':-32000,'data':{'code':'LIMIT_EXCEEDED','message':'Wait before retry','action':'retry after delay'}}}
    else: out={'result':r['params']}
    print(json.dumps({'jsonrpc':'2.0','method':'message_available','params':{'received':1}}),flush=True)
    print(json.dumps({'jsonrpc':'2.0','id':r['id'],**out}),flush=True)
'''
class ClientTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.binary = Path(self.directory.name)/'runtime'
        self.binary.write_text(FAKE)
        self.binary.chmod(0o700)
    def tearDown(self): self.directory.cleanup()
    async def test_json_and_notifications_are_separate(self):
        async with Zavliq(binary=str(self.binary)) as c:
            value=await c.call('roundtrip',{'text':'hello\nworld','data':{'nested':[1,2]}})
            self.assertEqual(value['text'],'hello\nworld')
            notification=await asyncio.wait_for(c.notifications.get(),1)
            self.assertEqual(notification['method'],'message_available')
    async def test_actionable_errors_preserve_code(self):
        async with Zavliq(binary=str(self.binary)) as c:
            with self.assertRaises(ZavliqError) as caught: await c.call('reject')
            self.assertEqual(caught.exception.code,'LIMIT_EXCEEDED')
            self.assertEqual(caught.exception.action,'retry after delay')
    async def test_process_exit_rejects_pending(self):
        async with Zavliq(binary=str(self.binary),timeout=2) as c:
            with self.assertRaises(ZavliqError) as caught: await c.call('exit')
            self.assertEqual(caught.exception.code,'RUNTIME_CLOSED')
            notification=await asyncio.wait_for(c.notifications.get(),1)
            self.assertEqual(notification['method'],'connection_state')
            self.assertEqual(notification['params']['closed'],True)
            self.assertEqual(notification['params']['code'],'RUNTIME_CLOSED')
    async def test_terminal_notification_survives_full_queue(self):
        async with Zavliq(binary=str(self.binary),timeout=2) as c:
            for _ in range(c.notifications.maxsize):
                c.notifications.put_nowait({'method':'message_available','params':{'received':1}})
            with self.assertRaises(ZavliqError): await c.call('exit')
            items=[c.notifications.get_nowait() for _ in range(c.notifications.qsize())]
            self.assertEqual(len(items),100)
            self.assertEqual(items[-1]['params']['closed'],True)
    async def test_concurrent_calls_keep_matching_ids(self):
        async with Zavliq(binary=str(self.binary)) as c:
            results=await asyncio.gather(*(c.call('roundtrip',{'n':n}) for n in range(10)))
            self.assertEqual([r['n'] for r in results],list(range(10)))
    async def test_inbox_sync_is_fresh_by_default_and_explicitly_local(self):
        async with Zavliq(binary=str(self.binary)) as c:
            self.assertEqual((await c.inbox())['sync'],True)
            self.assertEqual(await c.inbox(7,4,sync=False),{'cursor':7,'limit':4,'sync':False})
if __name__=='__main__': unittest.main()
