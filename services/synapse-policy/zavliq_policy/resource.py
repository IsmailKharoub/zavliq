import json
import secrets
from synapse.module_api import DirectServeJsonResource
from synapse.module_api.errors import SynapseError, Codes


class PolicyResource(DirectServeJsonResource):
    isLeaf=True
    def __init__(self,policy,name):
        super().__init__()
        self.policy=policy
        self.name=name

    async def _handle(self,request):
        request.setHeader(b'Cache-Control',b'no-store')
        return 200,await self._dispatch(request)

    _async_render_GET=_handle
    _async_render_POST=_handle
    _async_render_PUT=_handle
    _async_render_DELETE=_handle

    async def _dispatch(self,request):
        method=request.method.decode()
        if self.name=='health' and method=='GET':
            self.policy.store.db.execute('SELECT 1')
            return {'status':'ok','policy_version':'0.1.0'}
        requester=await self.policy.api.get_user_by_req(request)
        owner=requester.user.to_string()
        if not self.policy.local(owner):
            raise SynapseError(403,'Local accounts only',Codes.FORBIDDEN)
        store=self.policy.store
        body={}
        if method in ('POST','PUT','DELETE'):
            raw=request.content.read(4097)
            if len(raw)>4096:
                raise SynapseError(413,'Request too large',Codes.TOO_LARGE)
            try:
                body=json.loads(raw or b'{}')
            except (ValueError,UnicodeDecodeError):
                raise SynapseError(400,'Invalid JSON',Codes.BAD_JSON)
            if not isinstance(body,dict):
                raise SynapseError(400,'Expected object',Codes.BAD_JSON)
        if self.name=='blocks':
            if method=='GET':
                return {'blocked_user_ids':store.blocks(owner)}
            if method in ('POST','DELETE'):
                target=body.get('user_id')
                if set(body)!={'user_id'} or not self.policy.local(target) or len(target)>255 or target==owner:
                    raise SynapseError(400,'Expected another exact local user_id',Codes.BAD_JSON)
                if method=='POST':
                    if len(store.blocks(owner))>=1000 and target not in store.blocks(owner):
                        raise SynapseError(429,'Block list limit reached',Codes.LIMIT_EXCEEDED)
                    store.block(owner,target)
                else:
                    store.unblock(owner,target)
                return {'user_id':target,'blocked':method=='POST'}
        if self.name=='profile':
            if method=='PUT':
                if set(body)!={'directory_visible'} or not isinstance(body['directory_visible'],bool):
                    raise SynapseError(400,'Expected directory_visible boolean',Codes.BAD_JSON)
                store.db.execute('INSERT INTO profiles(owner,directory_visible) VALUES (?,?) ON CONFLICT(owner) DO UPDATE SET directory_visible=excluded.directory_visible',(owner,int(body['directory_visible'])))
            if method in ('GET','PUT'):
                row=store.db.execute('SELECT directory_visible FROM profiles WHERE owner=?',(owner,)).fetchone()
                return {'user_id':owner,'directory_visible':bool(row and row['directory_visible'])}
        if self.name=='directory' and method=='GET':
            users=[row['owner'] for row in store.db.execute('SELECT owner FROM profiles WHERE directory_visible=1 ORDER BY owner LIMIT 101') if not store.blocked(owner,row['owner'])]
            return {'agents':[{'user_id':user} for user in users[:100]],'limited':len(users)>100}
        if self.name=='quotas' and method=='GET':
            return {'user_id':owner,'quotas':store.usage(owner)}
        if self.name=='login-token' and method=='POST':
            if body:
                raise SynapseError(400,'Login token takes no identity override',Codes.BAD_JSON)
            if not store.consume('devices',owner,'$device-'+secrets.token_hex(16),[(86400,10)]):
                raise SynapseError(429,'Device creation limit reached',Codes.LIMIT_EXCEEDED)
            login_token=await self.policy.api.create_login_token(owner,duration_in_ms=120000)
            return {'login_token':login_token,'expires_in_ms':120000}
        raise SynapseError(405,'Unsupported method',Codes.UNRECOGNIZED)
