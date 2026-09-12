import {randomBytes,randomInt,timingSafeEqual} from 'node:crypto';
import type {FastifyInstance,FastifyRequest} from 'fastify';
import type {Config} from './config.js';
import {ApiError} from './errors.js';
import {Store,type Credentials} from './store.js';
import {Synapse} from './synapse.js';

type Pairing={id:string;proof:string;confirmation:string;user_id:string;device_display_name:string;device_id:string;status:string;credentials:string|null;expires_at:number;attempts:number};
type SecretBody={pairing_secret:string};
const secretBody={type:'object',additionalProperties:false,required:['pairing_secret'],properties:{pairing_secret:{type:'string',pattern:'^[A-Za-z0-9_-]{43}$'}}};

export function addPairingRoutes(app:FastifyInstance,config:Config,store:Store,synapse:Synapse,tokenFor:(request:FastifyRequest)=>string,serial:<T>(key:string,fn:()=>Promise<T>)=>Promise<T>) {
  function find(id:string,allowConsumed=false):Pairing {
    if(!/^[A-Za-z0-9_-]{43}$/.test(id)) throw new ApiError(404,'PAIRING_NOT_FOUND','This pairing request was not found.','Start a new pairing request in the browser.');
    const row=store.db.prepare('SELECT * FROM pairings WHERE id=?').get(id) as Pairing|undefined;
    if(!row) throw new ApiError(404,'PAIRING_NOT_FOUND','This pairing request was not found.','Start a new pairing request in the browser.');
    if(row.expires_at<Date.now() && !(allowConsumed && row.status==='consumed')) throw new ApiError(410,'PAIRING_EXPIRED','This pairing request has expired.','Start a new pairing request in the browser.');
    return row;
  }
  function checkSecret(row:Pairing,secret:string) {
    const proof=store.fingerprint(secret,'pairing-poll');
    if(!timingSafeEqual(Buffer.from(row.proof),Buffer.from(proof))) throw new ApiError(401,'PAIRING_SECRET_INVALID','The private pairing proof is invalid.','Use the browser that started this pairing.');
  }
  async function owner(request:FastifyRequest,row:Pairing) {
    const token=tokenFor(request);
    const identity=await synapse.whoami(token);
    if(identity.user_id!==row.user_id) throw new ApiError(403,'PAIRING_WRONG_IDENTITY','This request belongs to another agent identity.','Approve from the requested agent account.');
    return token;
  }
  const details=(row:Pairing)=>({pairing_id:row.id,user_id:row.user_id,device_display_name:row.device_display_name,expires_at:row.expires_at,status:row.status});
  app.post<{Body:{user_id:string;device_display_name:string}}>('/v1/pairings',{schema:{body:{type:'object',additionalProperties:false,required:['user_id','device_display_name'],properties:{user_id:{type:'string',minLength:4,maxLength:255},device_display_name:{type:'string',minLength:1,maxLength:80}}}}},async(request,reply)=>{
    store.attempt(request.ip);
    const {user_id,device_display_name}=request.body;
    if(!user_id.startsWith('@') || !user_id.endsWith(`:${config.serverName}`)) throw new ApiError(400,'INVALID_ADDRESS','Use the exact local address of the agent to pair.');
    const id=randomBytes(32).toString('base64url'),secret=randomBytes(32).toString('base64url');
    const code=randomInt(0,1_000_000).toString().padStart(6,'0');
    const at=Date.now(),expires=at+300_000;
    store.transaction(()=>{
      const ip=store.fingerprint(request.ip,'pairing-ip');
      const count=store.db.prepare('SELECT count(*) AS n FROM pairings WHERE ip_hash=? AND created_at>?').get(ip,at-3_600_000) as {n:number};
      if(count.n>=10) throw new ApiError(429,'PAIRING_LIMIT','Too many browser pairing requests.','Wait before starting another request.',3_600_000);
      const global=store.db.prepare('SELECT count(*) AS n FROM pairings WHERE created_at>?').get(at-86_400_000) as {n:number};
      if(global.n>=config.pairingPerDay) throw new ApiError(429,'PAIRING_CAPACITY','Daily device-pairing capacity has been reached.','Continue with an existing device and retry later.',86_400_000);
      store.db.prepare('INSERT INTO pairings(id,proof,confirmation,user_id,device_display_name,device_id,expires_at,ip_hash,created_at) VALUES (?,?,?,?,?,?,?,?,?)').run(id,store.fingerprint(secret,'pairing-poll'),store.fingerprint(code,`pairing-code:${id}`),user_id,device_display_name,`ZAVW${randomBytes(12).toString('hex').toUpperCase()}`,expires,ip,at);
    });
    return reply.code(201).send({pairing_id:id,pairing_secret:secret,confirmation_code:code,expires_at:expires});
  });
  app.get<{Params:{id:string}}>('/v1/pairings/:id',async(request)=>{
    const row=find(request.params.id);await owner(request,row);return details(row);
  });
  app.post<{Params:{id:string};Body:{confirmation_code:string}}>('/v1/pairings/:id/approve',{schema:{body:{type:'object',additionalProperties:false,required:['confirmation_code'],properties:{confirmation_code:{type:'string',pattern:'^[0-9]{6}$'}}}}},async(request)=>{
    return serial(`pairing:${request.params.id}`,async()=>{
      const row=find(request.params.id),token=await owner(request,row);
      if(row.attempts>=5) throw new ApiError(403,'PAIRING_LOCKED','This pairing request is locked after incorrect codes.','Start a new pairing request.');
      if(!timingSafeEqual(Buffer.from(row.confirmation),Buffer.from(store.fingerprint(request.body.confirmation_code,`pairing-code:${row.id}`)))) {
        store.db.prepare('UPDATE pairings SET attempts=attempts+1 WHERE id=?').run(row.id);
        throw new ApiError(403,'PAIRING_CODE_MISMATCH','The code does not match the browser request.','Check the code shown in your browser before approving.');
      }
      if(row.status==='consumed') throw new ApiError(409,'PAIRING_CONSUMED','This pairing request has already been completed.');
      if(row.status==='approved') return {...details(row),device_id:row.device_id,verification_required:true};
      // A short-lived Matrix login token creates a distinct device; never clone the approver's device.
      const login=await synapse.request<{login_token:string}>('/_synapse/client/zavliq/login-token',{method:'POST',token,body:{}});
      const session=await synapse.request<{user_id:string;device_id:string;access_token:string}>('/_matrix/client/v3/login',{method:'POST',body:{type:'m.login.token',token:login.login_token,device_id:row.device_id,initial_device_display_name:row.device_display_name}});
      if(session.user_id!==row.user_id || session.device_id!==row.device_id) throw new ApiError(502,'INVALID_UPSTREAM','The homeserver returned an unexpected paired identity.');
      const credentials:Credentials={...session,homeserver:config.publicHomeserverUrl};
      store.db.prepare("UPDATE pairings SET status='approved',credentials=? WHERE id=?").run(store.seal(credentials,`pairing:${row.id}`),row.id);
      return {...details({...row,status:'approved'}),device_id:row.device_id,verification_required:true};
    });
  });
  app.post<{Params:{id:string};Body:SecretBody}>('/v1/pairings/:id/poll',{schema:{body:secretBody}},async(request)=>{
    const row=find(request.params.id);checkSecret(row,request.body.pairing_secret);
    if(row.status==='consumed') throw new ApiError(409,'PAIRING_CONSUMED','This pairing was completed and its credentials were cleared.','Use the credentials already saved in this browser.');
    if(row.status==='approved' && row.credentials) return {status:'approved',credentials:store.unseal<Credentials>(row.credentials,`pairing:${row.id}`),verification_required:true};
    return {status:'pending',retry_after_ms:2000,expires_at:row.expires_at};
  });
  app.post<{Params:{id:string};Body:SecretBody}>('/v1/pairings/:id/ack',{schema:{body:secretBody}},async(request)=>serial(`pairing:${request.params.id}`,async()=>{
    const row=find(request.params.id,true);checkSecret(row,request.body.pairing_secret);
    if(row.status==='pending') throw new ApiError(409,'PAIRING_PENDING','This pairing request has not been approved.');
    store.db.prepare("UPDATE pairings SET status='consumed',credentials=NULL WHERE id=?").run(row.id);
    return {completed:true};
  }));
  async function expire() {
    const rows=store.db.prepare("SELECT * FROM pairings WHERE expires_at<? AND status NOT IN ('consumed','expired')").all(Date.now()) as Pairing[];
    for(const row of rows) {
      try {
        if(row.status==='approved') await synapse.admin(`/_synapse/admin/v2/users/${encodeURIComponent(row.user_id)}/devices/${encodeURIComponent(row.device_id)}`,'DELETE');
        store.db.prepare("UPDATE pairings SET status='expired',credentials=NULL WHERE id=?").run(row.id);
      } catch { /* Retry next minute; never claim revocation without a successful response. */ }
    }
    store.db.prepare("DELETE FROM pairings WHERE status='expired' AND created_at<?").run(Date.now()-86_400_000);
    store.db.prepare("DELETE FROM pairings WHERE status='consumed' AND created_at<?").run(Date.now()-7*86_400_000);
  }
  const timer=setInterval(()=>{void expire().catch(()=>{});},60_000);timer.unref();
  app.addHook('onClose',async()=>clearInterval(timer));
}
