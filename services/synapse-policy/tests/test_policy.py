import io
import os
import tempfile
import sqlite3
import unittest
from types import SimpleNamespace

from synapse.module_api import NOT_SPAM
from synapse.module_api.errors import Codes, SynapseError
from synapse.events import make_event_from_dict
from synapse.module_api.callbacks.spamchecker_callbacks import SpamCheckerModuleApiCallbacks
from zavliq_policy import ZavliqPolicy, CONVERSATION, ENCRYPTION, ALGORITHM, RECEIPT
from zavliq_policy.store import PolicyStore
from zavliq_policy.resource import PolicyResource


class Event:
    def __init__(self, typ, content, sender='@alice:test.local', event_id='$event', room_id='!room:test.local', state_key=None, redacts=None):
        self.type=typ;self.content=content;self.sender=sender;self.event_id=event_id;self.room_id=room_id;self.redacts=redacts
        if state_key is not None:
            self.state_key=state_key
    def is_state(self):
        return hasattr(self,'state_key')


def state(kind='dm',encryption='standard',members=('@alice:test.local',)):
    meta={'kind':kind,'encryption':encryption}
    result={
        ('m.room.create',''):Event('m.room.create',{CONVERSATION:meta,'m.federate':False},state_key='',event_id='$create'),
        (CONVERSATION,''):Event(CONVERSATION,meta,state_key='',event_id='$meta'),
    }
    for member in members:
        result[('m.room.member',member)]=Event('m.room.member',{'membership':'join'},state_key=member,event_id='$'+member)
    if encryption=='e2ee':
        result[(ENCRYPTION,'')]=Event(ENCRYPTION,{'algorithm':ALGORITHM},state_key='',event_id='$encryption')
    return result


class Api:
    server_name='test.local'
    def __init__(self):
        self.state=state();self.resources={}
    def register_spam_checker_callbacks(self,**kwargs):self.spam=kwargs
    def register_third_party_rules_callbacks(self,**kwargs):self.rules=kwargs
    def register_web_resource(self,path,resource):self.resources[path]=resource
    async def get_room_state(self,room):return self.state
    async def get_user_by_req(self,request):
        if request.token!='alice-token':
            raise SynapseError(401,'Invalid token',Codes.UNKNOWN_TOKEN)
        return SimpleNamespace(user=SimpleNamespace(to_string=lambda:'@alice:test.local'))


class StoreTests(unittest.TestCase):
    def setUp(self):self.clock=100000.;self.store=PolicyStore(':memory:',lambda:self.clock)
    def tearDown(self):self.store.db.close()
    def test_atomic_multiwindow_count_and_event_deduplication(self):
        for index in range(30):
            self.assertTrue(self.store.consume('messages','alice',f'${index}',[(60,30),(86400,1000)]))
        self.assertFalse(self.store.consume('messages','alice','$overflow',[(60,30),(86400,1000)]))
        self.assertTrue(self.store.consume('messages','alice','$0',[(60,30),(86400,1000)]))
        self.assertEqual(self.store.usage('alice')['messages_per_day']['used'],30)
        self.clock+=60
        self.assertTrue(self.store.consume('messages','alice','$next-minute',[(60,30),(86400,1000)]))
        self.assertEqual(self.store.usage('alice')['messages_per_day']['used'],31)
    def test_contact_limit_pending_pair_acceptance_rejection_and_blocking(self):
        self.assertTrue(self.store.contact_allowed('alice','bob','one'))
        self.assertTrue(self.store.contact_allowed('alice','bob','one'))
        self.assertFalse(self.store.contact_allowed('alice','bob','two'))
        for recipient in ('c','d','e','f'):
            self.assertTrue(self.store.contact_allowed('alice',recipient,recipient))
        self.assertFalse(self.store.contact_allowed('alice','g','g'))
        self.store.contact_membership('bob','one','join')
        self.assertTrue(self.store.contact_allowed('alice','bob','two'))
        self.store.block('bob','alice')
        self.assertFalse(self.store.contact_allowed('alice','bob','three'))
        self.store.unblock('bob','alice')
        self.assertTrue(self.store.contact_allowed('alice','bob','three'))
        self.store.contact_membership('c','c','leave')
        self.assertFalse(self.store.contact_allowed('alice','c','new-request'))
    def test_counter_and_blocks_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path=os.path.join(directory,'policy.sqlite')
            store=PolicyStore(path,lambda:self.clock)
            store.block('alice','bob');store.consume('messages','alice','$a',[(86400,1)])
            store.db.close();store=PolicyStore(path,lambda:self.clock)
            try:
                self.assertTrue(store.blocked('bob','alice'))
                self.assertFalse(store.consume('messages','alice','$b',[(86400,1)]))
                self.assertEqual(os.stat(path).st_mode & 0o777,0o600)
            finally:store.db.close()
    def test_concurrent_member_reservations_cannot_overfill(self):
        self.assertTrue(self.store.reserve_member('room','bob',['alice'],2))
        self.assertFalse(self.store.reserve_member('room','charlie',['alice'],2))
        self.store.contact_membership('bob','room','leave')
        self.assertTrue(self.store.reserve_member('room','charlie',['alice'],2))
    def test_abandoned_invite_reservation_recovers_without_removing_live_members(self):
        self.assertTrue(self.store.reserve_member('room','bob',['alice'],2))
        self.clock+=121
        self.assertTrue(self.store.reserve_member('room','charlie',['alice'],2))
        self.clock+=121
        self.assertFalse(self.store.reserve_member('room','dave',['alice','charlie'],2))
    def test_policy_metadata_migrates_existing_member_reservations(self):
        with tempfile.TemporaryDirectory() as directory:
            path=os.path.join(directory,'old.sqlite')
            db=sqlite3.connect(path)
            db.execute('CREATE TABLE member_slots(room TEXT NOT NULL,member TEXT NOT NULL,PRIMARY KEY(room,member))')
            db.execute('INSERT INTO member_slots(room,member) VALUES (?,?)',('room','alice'))
            db.commit();db.close()
            store=PolicyStore(path,lambda:self.clock)
            try:
                self.assertTrue(store.reserve_member('room','bob',['alice'],2))
                self.assertFalse(store.reserve_member('room','charlie',['alice','bob'],2))
            finally:store.db.close()


class PolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):self.api=Api();self.policy=ZavliqPolicy({'database_path':':memory:'},self.api)
    def tearDown(self):self.policy.store.db.close()
    async def test_create_room_enforces_private_mode_on_direct_clients(self):
        config={'visibility':'public','preset':'public_chat','initial_state':[{'type':CONVERSATION,'content':{'kind':'dm','encryption':'standard'}}]}
        await self.policy.on_create_room(None,config,False)
        self.assertEqual(config['visibility'],'private');self.assertEqual(config['creation_content']['m.federate'],False)
        states={event['type']:event['content'] for event in config['initial_state']}
        self.assertEqual(states['m.room.join_rules']['join_rule'],'invite')
        self.assertEqual(states['m.room.history_visibility']['history_visibility'],'invited')
        self.assertEqual(config['creation_content'][CONVERSATION],states[CONVERSATION])
    async def test_initial_encryption_requires_matching_metadata_and_cannot_be_changed(self):
        with self.assertRaises(SynapseError):
            await self.policy.on_create_room(None,{'initial_state':[{'type':CONVERSATION,'content':{'kind':'dm','encryption':'e2ee'}}]},False)
        config={'initial_state':[{'type':ENCRYPTION,'content':{'algorithm':ALGORITHM}},{'type':CONVERSATION,'content':{'kind':'group','encryption':'e2ee'}}]}
        await self.policy.on_create_room(None,config,False)
        self.assertEqual(config['initial_state'][0]['type'],CONVERSATION)
        encrypted=state(encryption='e2ee')
        for event in [Event('m.room.message',{'body':'must not leak'}),Event(ENCRYPTION,{'algorithm':'different'},state_key=''),Event(CONVERSATION,{'kind':'dm','encryption':'standard'},state_key=''),Event('m.room.redaction',{},redacts='$encryption')]:
            self.assertFalse((await self.policy.check_event_allowed(event,encrypted))[0])
        self.assertFalse((await self.policy.check_event_allowed(Event(ENCRYPTION,{'algorithm':ALGORITHM},state_key=''),state()))[0])
    async def test_invite_requests_remote_users_blocking_and_private_join(self):
        self.assertEqual(await self.policy.user_may_invite('@alice:test.local','@bob:other.host','!one'),Codes.FORBIDDEN)
        self.assertEqual(await self.policy.user_may_invite('@alice:test.local','@bob:test.local','!one'),NOT_SPAM)
        self.assertEqual(await self.policy.user_may_invite('@alice:test.local','@bob:test.local','!two'),Codes.FORBIDDEN)
        self.assertEqual(await self.policy.user_may_join_room('@bob:test.local','!one',False),Codes.FORBIDDEN)
        self.policy.store.block('@bob:test.local','@alice:test.local')
        self.assertEqual(await self.policy.user_may_join_room('@bob:test.local','!one',True),Codes.FORBIDDEN)
    async def test_join_retry_is_idempotent_without_admitting_former_or_uninvited_members(self):
        for kind in ('dm','group'):
            self.api.state=state(kind=kind,members=('@alice:test.local','@bob:test.local'))
            self.assertEqual(await self.policy.user_may_join_room('@bob:test.local','!joined',False),NOT_SPAM)
            self.assertEqual(await self.policy.user_may_join_room('@outsider:test.local','!joined',False),Codes.FORBIDDEN)
            self.api.state[('m.room.member','@bob:test.local')].content['membership']='leave'
            self.assertEqual(await self.policy.user_may_join_room('@bob:test.local','!joined',False),Codes.FORBIDDEN)
        self.api.state=state(members=('@alice:test.local','@bob:test.local'))
        self.policy.store.block('@alice:test.local','@bob:test.local')
        self.assertEqual(await self.policy.user_may_join_room('@bob:test.local','!joined',False),NOT_SPAM)
        self.api.state[('m.room.member','@bob:test.local')].content['membership']='invite'
        self.assertEqual(await self.policy.user_may_join_room('@bob:test.local','!joined',True),Codes.FORBIDDEN)

    async def test_personal_block_cannot_silence_channel_or_control_group_membership(self):
        self.policy.store.block('@bob:test.local','@alice:test.local')
        self.api.state=state(kind='channel',members=('@alice:test.local','@bob:test.local'))
        message=Event('m.room.message',{'msgtype':'m.text','body':'publisher message'})
        self.assertTrue((await self.policy.check_event_allowed(message,self.api.state))[0])
        self.assertEqual(await self.policy.user_may_join_room('@alice:test.local','!channel',False),NOT_SPAM)
        self.api.state=state(kind='group',members=('@bob:test.local',))
        self.assertEqual(await self.policy.user_may_join_room('@alice:test.local','!group',True),NOT_SPAM)
    async def test_channel_posting_cannot_be_opened_via_power_levels_override(self):
        config={'initial_state':[{'type':CONVERSATION,'content':{'kind':'channel','encryption':'standard'}},{'type':'m.room.power_levels','content':{'events_default':0}}]}
        await self.policy.on_create_room(None,config,False)
        self.assertEqual(next(event for event in config['initial_state'] if event['type']=='m.room.power_levels')['content']['events_default'],50)
        for content in [{'events_default':0},{'events_default':50,'events':{'m.room.message':0}},{'events_default':50,'users_default':50}]:
            self.assertFalse((await self.policy.check_event_allowed(Event('m.room.power_levels',content,state_key=''),state(kind='channel')))[0])
        self.assertTrue(await self.policy.check_visibility('!new:test.local',{},'public'))
        self.assertFalse(await self.policy.check_visibility('!private:test.local',state(),'public'))
    async def test_events_size_receipts_and_custom_types_cannot_evade_quota(self):
        self.assertEqual((await self.policy.check_event_for_spam(Event('m.room.message',{'body':'x'*32769})))[0],Codes.TOO_LARGE)
        self.assertEqual(await self.policy.check_event_for_spam(Event('arbitrary.unmetered',{'body':'hello'})),Codes.FORBIDDEN)
        self.assertEqual(await self.policy.check_event_for_spam(Event('arbitrary.state',{'body':'hello'},state_key='')),Codes.FORBIDDEN)
        self.assertEqual(await self.policy.check_event_for_spam(Event(RECEIPT,{'event_id':'$one','status':'delivered'},event_id='$receipt')),NOT_SPAM)
        self.assertEqual(self.policy.store.usage('@alice:test.local')['messages_per_day']['used'],0)
        self.assertEqual(await self.policy.check_event_for_spam(Event(RECEIPT,{'event_id':'$one','status':'delivered','body':'smuggled'},event_id='$bad')),Codes.BAD_JSON)
    async def test_size_refusal_preserves_synapse_code_and_returns_actionable_client_text(self):
        callbacks=SpamCheckerModuleApiCallbacks(SimpleNamespace(hostname='test.local',get_clock=lambda:SimpleNamespace(time=lambda:0.)))
        callbacks._check_event_for_spam_callbacks.append(self.policy.check_event_for_spam)
        # The serialized {"body":""} envelope is 11 bytes: the existing boundary
        # remains inclusive, while both plaintext and ciphertext count toward it.
        boundary=Event('m.room.message',{'body':'x'*(32768-11)},event_id='$boundary')
        self.assertEqual(await callbacks.check_event_for_spam(boundary),NOT_SPAM)
        for typ,content in [('m.room.message',{'body':'x'*(32768-10)}),
                            ('m.room.encrypted',{'algorithm':ALGORITHM,'ciphertext':'x'*32768})]:
            code,fields=await callbacks.check_event_for_spam(Event(typ,content))
            error=SynapseError(403,'This message has been rejected as probable spam',code,fields)
            response=error.error_dict(None)
            self.assertEqual(error.code,403)
            self.assertEqual(response['errcode'],Codes.TOO_LARGE)
            self.assertEqual(response['limit_bytes'],32768)
            self.assertIn('shorten it or send a file',response['error'])
            self.assertNotIn('spam',response['error'])
        self.assertEqual(self.policy.store.usage('@alice:test.local')['messages_per_day']['used'],1)
    async def test_native_synapse_rust_json_content_is_supported(self):
        event=make_event_from_dict({'type':'m.room.message','room_id':'!x:test.local','sender':'@alice:test.local','event_id':'$native','origin':'test.local','origin_server_ts':1,'auth_events':[],'prev_events':[],'depth':1,'hashes':{'sha256':'test'},'signatures':{},'content':{'msgtype':'m.text','body':'native event','com.zavliq.data':{'nested':['supported']}}})
        self.assertEqual(await self.policy.check_event_for_spam(event),NOT_SPAM)
    async def test_profile_private_default_and_block_resource_ownership(self):
        profile={'user_id':'@alice:test.local'}
        self.assertTrue(await self.policy.check_username_for_spam(profile,'@bob:test.local'))
        resource=PolicyResource(self.policy,'blocks')
        request=SimpleNamespace(method=b'POST',content=io.BytesIO(b'{"user_id":"@bob:test.local","owner":"@victim:test.local"}'),token='alice-token')
        with self.assertRaises(SynapseError):await resource._dispatch(request)
        request.content=io.BytesIO(b'{"user_id":"@bob:test.local"}')
        self.assertTrue((await resource._dispatch(request))['blocked'])
        self.assertEqual(self.policy.store.blocks('@victim:test.local'),[])
        request.token='invalid';request.content=io.BytesIO(b'{"user_id":"@victim:test.local"}')
        with self.assertRaises(SynapseError):await resource._dispatch(request)


if __name__=='__main__':unittest.main()
