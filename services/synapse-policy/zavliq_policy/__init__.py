import json
from canonicaljson import encode_canonical_json
from pathlib import Path
from synapse.module_api import NOT_SPAM
from synapse.module_api.errors import Codes, SynapseError
from .store import PolicyStore

CONVERSATION = 'com.zavliq.conversation'
ENCRYPTION = 'm.room.encryption'
RECEIPT = 'com.zavliq.receipt'
ALGORITHM = 'm.megolm.v1.aes-sha2'


class ZavliqPolicy:
    """Local-only rules. Must be loaded on the single Synapse writer process."""
    @staticmethod
    def parse_config(config):
        path=config.get('database_path','/data/policy.sqlite')
        if path != ':memory:' and not Path(path).is_absolute():
            raise ValueError('Zavliq policy database_path must be absolute')
        return {'database_path':path}

    def __init__(self, config, api):
        self.api=api
        self.store=PolicyStore(config['database_path'])
        api.register_spam_checker_callbacks(
            user_may_invite=self.user_may_invite,
            user_may_join_room=self.user_may_join_room,
            user_may_send_3pid_invite=self.deny_threepid,
            check_event_for_spam=self.check_event_for_spam,
            check_username_for_spam=self.check_username_for_spam,
            user_may_publish_room=self.user_may_publish_room,
        )
        api.register_third_party_rules_callbacks(
            on_create_room=self.on_create_room,
            check_event_allowed=self.check_event_allowed,
            on_new_event=self.on_new_event,
            check_visibility_can_be_modified=self.check_visibility,
        )
        from .resource import PolicyResource
        for name in ['blocks','profile','directory','quotas','health','login-token']:
            api.register_web_resource(f'/_synapse/client/zavliq/{name}',PolicyResource(self,name))

    def local(self, user):
        return isinstance(user,str) and user.startswith('@') and user.endswith(':'+self.api.server_name)

    @staticmethod
    def metadata(state):
        event=state.get((CONVERSATION,''))
        if event:
            return event.content
        create=state.get(('m.room.create',''))
        if create and CONVERSATION in create.content:
            return create.content[CONVERSATION]
        return {'kind':'group','encryption':'standard'}

    async def on_create_room(self, requester, request_content, is_requester_admin):
        initial=request_content.setdefault('initial_state',[])
        by_type={}
        for event in initial:
            key=(event.get('type'),event.get('state_key',''))
            if key in by_type:
                raise SynapseError(400,'Duplicate initial room state',Codes.BAD_JSON)
            by_type[key]=event
        meta=by_type.get((CONVERSATION,''),{}).get('content')
        if meta is None:
            meta={'kind':'group','encryption':'standard'}
            initial.append({'type':CONVERSATION,'state_key':'','content':meta})
        if set(meta)!= {'kind','encryption'} or meta['kind'] not in ('dm','group','channel') or meta['encryption'] not in ('standard','e2ee'):
            raise SynapseError(400,'Use valid Zavliq conversation kind and encryption',Codes.BAD_JSON)
        encryption=by_type.get((ENCRYPTION,''),{}).get('content')
        if meta['encryption']=='e2ee':
            if meta['kind']=='channel' or not encryption or encryption.get('algorithm')!=ALGORITHM:
                raise SynapseError(400,'Encrypted private rooms need Matrix Megolm initial state',Codes.BAD_JSON)
        elif encryption:
            raise SynapseError(400,'Standard conversations cannot include encryption state',Codes.BAD_JSON)
        if request_content.get('invite_3pid'):
            raise SynapseError(403,'Invite exact agent addresses only',Codes.FORBIDDEN)
        invites=request_content.get('invite',[])
        if any(not self.local(user) for user in invites):
            raise SynapseError(403,'Federation is disabled',Codes.FORBIDDEN)
        cap={'dm':2,'group':100,'channel':1000}[meta['kind']]
        if len(set(invites))+1>cap:
            raise SynapseError(403,'Conversation member limit exceeded',Codes.FORBIDDEN)
        create=request_content.setdefault('creation_content',{})
        create['m.federate']=False
        # Creation content is immutable and available before Synapse emits preset state.
        create[CONVERSATION]=meta.copy()
        initial.sort(key=lambda event: 0 if event.get('type')==CONVERSATION else 1)
        # Invited history preserves the initial message when a recipient accepts later.
        public=meta['kind']=='channel'
        request_content['visibility']='public' if public else 'private'
        request_content['preset']='public_chat' if public else 'private_chat'
        for kind,content in [('m.room.join_rules',{'join_rule':'public' if public else 'invite'}),('m.room.history_visibility',{'history_visibility':'shared' if public else 'invited'}),('m.room.guest_access',{'guest_access':'forbidden'})]:
            initial[:]=[event for event in initial if event.get('type')!=kind]
            initial.append({'type':kind,'state_key':'','content':content})
        if public:
            levels=request_content.setdefault('power_level_content_override',{})
            levels['events_default']=50
            levels['state_default']=50
            levels['users_default']=0
            # Explicit initial power levels take precedence over the override in Synapse.
            custom=by_type.get(('m.room.power_levels',''))
            if custom:
                custom['content'].update(events_default=50,state_default=50,users_default=0)

    async def deny_threepid(self,*args):
        return Codes.FORBIDDEN

    async def user_may_invite(self, inviter, invitee, room_id):
        if not self.local(inviter) or not self.local(invitee):
            return Codes.FORBIDDEN
        state=await self.api.get_room_state(room_id)
        kind=self.metadata(state)['kind']
        present=[key[1] for key,event in state.items() if key[0]=='m.room.member' and event.content.get('membership') in ('join','invite')]
        if not self.store.reserve_member(room_id,invitee,present,{'dm':2,'group':100,'channel':1000}[kind]):
            return Codes.FORBIDDEN
        allowed=self.store.contact_allowed(inviter,invitee,room_id)
        if not allowed and invitee not in present:
            self.store.db.execute('DELETE FROM member_slots WHERE room=? AND member=?',(room_id,invitee))
        return NOT_SPAM if allowed else Codes.FORBIDDEN

    async def user_may_join_room(self,user,room,is_invited):
        if not self.local(user):
            return Codes.FORBIDDEN
        state=await self.api.get_room_state(room)
        existing=state.get(('m.room.member',user))
        # Retrying an accepted join does not create a new membership or contact.
        # Synapse reports is_invited=False once the first join has succeeded.
        if existing and existing.content.get('membership')=='join':
            return NOT_SPAM
        meta=self.metadata(state)
        if meta['kind']!='channel' and not is_invited:
            return Codes.FORBIDDEN
        members=[key[1] for key,event in state.items() if key[0]=='m.room.member' and event.content.get('membership') in ('join','invite')]
        if meta['kind']=='dm' and any(self.store.blocked(user,member) for member in members):
            return Codes.FORBIDDEN
        return NOT_SPAM if self.store.reserve_member(room,user,members,{'dm':2,'group':100,'channel':1000}[meta['kind']]) else Codes.FORBIDDEN

    async def check_username_for_spam(self,user_profile,requester_id):
        owner=user_profile['user_id']
        row=self.store.db.execute('SELECT directory_visible FROM profiles WHERE owner=?',(owner,)).fetchone()
        return not row or not row['directory_visible'] or self.store.blocked(owner,requester_id)

    async def check_visibility(self,room_id,state,new_visibility):
        # Synapse checks creation visibility before persisting any initial state.
        # on_create_room already restricts the requested visibility by validated kind.
        if not state:
            return True
        return new_visibility=='private' or self.metadata(state)['kind']=='channel'

    async def user_may_publish_room(self,user_id,room_id):
        state=await self.api.get_room_state(room_id)
        return NOT_SPAM if self.metadata(state)['kind']=='channel' else Codes.FORBIDDEN

    async def check_event_allowed(self,event,state):
        """Validate immutable room boundaries on every path, including raw state sends."""
        typ=event.type
        meta=self.metadata(state)
        old=state.get((typ,getattr(event,'state_key','')))
        if typ==CONVERSATION:
            if set(event.content)!={'kind','encryption'} or event.content!=meta:
                return False,None
            if old and event.content!=old.content:
                return False,None
            if event.content.get('kind') not in ('dm','group','channel') or event.content.get('encryption') not in ('standard','e2ee'):
                return False,None
        if typ==ENCRYPTION:
            # Initial state event order is fixed in on_create_room below by caller metadata.
            if meta['encryption']!='e2ee' or event.content.get('algorithm')!=ALGORITHM or (old and old.content!=event.content):
                return False,None
        if typ=='m.room.join_rules' and event.content.get('join_rule')!=('public' if meta['kind']=='channel' else 'invite'):
            return False,None
        if typ=='m.room.history_visibility' and event.content.get('history_visibility') not in (('shared',) if meta['kind']=='channel' else ('invited',)):
            return False,None
        if typ=='m.room.guest_access' and event.content.get('guest_access')!='forbidden':
            return False,None
        if typ=='m.room.power_levels' and meta['kind']=='channel':
            if event.content.get('events_default',0)<50 or event.content.get('users_default',0)>=50:
                return False,None
            if any(level<50 for event_type,level in event.content.get('events',{}).items() if event_type in ('m.room.message','m.room.encrypted',RECEIPT)):
                return False,None
        if typ=='m.room.redaction' and (getattr(event,'redacts',None) or event.content.get('redacts')) in {e.event_id for k,e in state.items() if k[0] in (CONVERSATION,ENCRYPTION)}:
            return False,None
        if typ in ('m.room.message','m.room.encrypted',RECEIPT):
            if meta['encryption']=='e2ee' and typ!='m.room.encrypted':
                return False,None
            if meta['encryption']=='standard' and typ=='m.room.encrypted':
                return False,None
            members=[key[1] for key,e in state.items() if key[0]=='m.room.member' and e.content.get('membership')=='join']
            # Personal blocking must not give a group member power to silence everyone.
            # Shared-group participation is governed by room roles and explicit membership.
            if meta['kind']=='dm' and any(self.store.blocked(event.sender,member) for member in members):
                return False,None
        return True,None

    async def check_event_for_spam(self,event):
        if not self.local(event.sender):
            return Codes.FORBIDDEN
        if len(encode_canonical_json(event.content))>32768:
            return Codes.TOO_LARGE
        if event.is_state():
            if event.type not in ('m.room.create','m.room.member','m.room.power_levels','m.room.join_rules','m.room.history_visibility','m.room.guest_access','m.room.name','m.room.topic','m.room.avatar','m.room.canonical_alias',CONVERSATION,ENCRYPTION,'m.room.server_acl','m.room.pinned_events'):
                return Codes.FORBIDDEN
            # Membership and room configuration events cannot be an unmetered message path.
            return NOT_SPAM if self.store.consume('state',event.sender,event.event_id,[(60,60),(86400,1000)]) else Codes.LIMIT_EXCEEDED
        if event.type=='m.room.redaction':
            return NOT_SPAM
        if event.type not in ('m.room.message','m.room.encrypted',RECEIPT):
            return Codes.FORBIDDEN
        if event.type==RECEIPT:
            if set(event.content)!= {'event_id','status'} or not isinstance(event.content.get('event_id'),str) or event.content.get('status')!='delivered':
                return Codes.BAD_JSON
            allowed=self.store.consume('receipts',event.sender,event.event_id,[(60,90),(86400,3000)])
        else:
            allowed=self.store.consume('messages',event.sender,event.event_id,[(60,30),(86400,1000)])
        return NOT_SPAM if allowed else Codes.LIMIT_EXCEEDED

    async def on_new_event(self,event,state):
        if event.type==CONVERSATION:
            self.store.db.execute('INSERT OR IGNORE INTO room_kinds(room,kind,encryption) VALUES (?,?,?)',(event.room_id,event.content['kind'],event.content['encryption']))
        if event.type=='m.room.member':
            self.store.contact_membership(event.state_key,event.room_id,event.content.get('membership'))
