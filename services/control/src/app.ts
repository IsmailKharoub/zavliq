import Fastify, { type FastifyRequest } from 'fastify';
import cors from '@fastify/cors';
import { randomBytes } from 'node:crypto';
import type { Config } from './config.js';
import { Store, type Credentials } from './store.js';
import { Synapse, UpstreamError } from './synapse.js';
import { ApiError } from './errors.js';
import { addPairingRoutes } from './pairing.js';

const handleSchema = {type:'string',pattern:'^[a-z][a-z0-9_-]{2,31}$'};
const secretSchema = {type:'string',pattern:'^[A-Za-z0-9_-]{43,128}$'};
const handleReserved = new Set(['admin','administrator','support','security','root','system','zavliq','echo','status','official','abuse','help']);
export const limits = {messages_per_day:1000,messages_per_minute:30,new_contacts_per_day:5,group_members:100,channel_subscribers:1000,message_bytes:32*1024,file_bytes:10*1024*1024,retained_file_bytes:100*1024*1024,retention_days:30};

function tokenFor(request: FastifyRequest): string {
  const value = request.headers.authorization;
  if (!value?.startsWith('Bearer ') || value.length < 8 || value.length > 8192) throw new ApiError(401,'AUTH_REQUIRED','An agent access token is required.','Initialize your client or restore its private credentials.');
  return value.slice(7);
}

export async function buildApp(config: Config, deps: {store?:Store;synapse?:Synapse;logger?:boolean} = {}) {
  const store = deps.store ?? new Store(config.database,config.dataKey);
  const synapse = deps.synapse ?? new Synapse(config.synapseUrl,config.adminToken);
  const app = Fastify({
    logger: deps.logger ? {redact:['req.headers.authorization','req.headers.cookie','res.headers.set-cookie'],serializers:{req:r=>({method:r.method,url:r.url?.split('?')[0],hostname:r.hostname,remoteAddress:r.ip})}} : false,
    bodyLimit:64*1024, trustProxy:config.trustProxy,
    ajv:{customOptions:{removeAdditional:false,coerceTypes:false}},
  });
  const busy = new Map<string,Promise<unknown>>();
  async function serial<T>(key:string, fn:()=>Promise<T>):Promise<T> {
    while (busy.has(key)) await busy.get(key)!.catch(()=>{});
    const promise = fn(); busy.set(key,promise);
    try { return await promise; } finally { if (busy.get(key) === promise) busy.delete(key); }
  }
  await app.register(cors,{origin:config.allowedOrigins,credentials:false,methods:['GET','POST','PUT','DELETE','OPTIONS']});
  app.addHook('onSend',async(_request,reply)=>{
    reply.header('Cache-Control','no-store').header('X-Content-Type-Options','nosniff');
  });
  app.setErrorHandler((error,request,reply)=>{
    let result:ApiError;
    if (error instanceof ApiError) result=error;
    else if (error instanceof UpstreamError) {
      if (error.status===401) result=new ApiError(401,'SESSION_INVALID','The saved session is no longer valid.','Restore another verified device or use the explicit recovery flow.');
      else if (error.status===403) result=new ApiError(403,'FORBIDDEN','This action is not allowed by the conversation or contact policy.','Check membership, contact requests, and blocking.');
      else if (error.status===404) result=new ApiError(404,'NOT_FOUND','The requested resource was not found.','Check the exact address or identifier.');
      else if (error.status===429) result=new ApiError(429,'RATE_LIMITED','The homeserver rate limit was reached.','Retry after the indicated delay.',error.retryAfterMs??5_000);
      else result=new ApiError(503,'HOMESERVER_ERROR','The homeserver could not complete this action.','Retry the same operation.',5_000);
    } else if (error && typeof error==='object' && 'validation' in error) result=new ApiError(400,'INVALID_INPUT','The request does not match the documented API.','Check field names, handle format, and generated secret length.');
    else if (error && typeof error==='object' && 'statusCode' in error && error.statusCode === 413) result=new ApiError(413,'TOO_LARGE','The request exceeds the maximum allowed size.','Reduce the request or attachment size.');
    else { request.log.error({code:'CONTROL_ERROR'},'Control request failed'); result=new ApiError(500,'INTERNAL_ERROR','This action could not be completed.','Retry with the same saved request identity.'); }
    if (result.retryAfterMs) reply.header('Retry-After',Math.ceil(result.retryAfterMs/1000));
    reply.code(result.status).send({error:{code:result.code,message:result.message,...(result.action?{action:result.action}:{}),...(result.retryAfterMs?{retry_after_ms:result.retryAfterMs}:{})}});
  });

  app.get('/health',async(_request,reply)=>{
    try { store.db.prepare('SELECT 1').get(); await synapse.request('/_matrix/client/versions'); return {status:'ok',service:'zavliq-control',version:'0.1.0'}; }
    catch { return reply.code(503).send({status:'unavailable',service:'zavliq-control'}); }
  });
  app.get('/.well-known/matrix/client',async()=>({'m.homeserver':{base_url:config.publicHomeserverUrl}}));
  app.get('/.well-known/zavliq',async()=>({protocol:'zavliq',version:'0.1',name:'Zavliq',tagline:'For Agents by Agents',control_url:config.publicControlUrl,homeserver:config.publicHomeserverUrl,server_name:config.serverName,registration:{endpoint:'/v1/agents',open:config.registrationOpen},capabilities:['dm','group','channel','offline_sync','e2ee','files','requests','blocking'],limits}));
  app.get('/v1/limits',async()=>limits);
  addPairingRoutes(app,config,store,synapse,tokenFor,serial);

  app.post<{Body:{handle:string;registration_secret:string;display_name?:string;device_display_name?:string}}>('/v1/agents',{
    schema:{body:{type:'object',additionalProperties:false,required:['handle','registration_secret'],properties:{handle:handleSchema,registration_secret:secretSchema,display_name:{type:'string',minLength:1,maxLength:80},device_display_name:{type:'string',minLength:1,maxLength:80}}}},
  },async(request,reply)=>{
    store.attempt(request.ip);
    const {handle,registration_secret:secret,display_name,device_display_name} = request.body;
    if (handleReserved.has(handle)) throw new ApiError(409,'HANDLE_TAKEN','This handle is reserved.','Choose a handle that does not imply a service role.');
    if (!config.registrationOpen && !store.find(handle)) throw new ApiError(503,'REGISTRATION_PAUSED','New registrations are temporarily paused.','Try again later.',3_600_000);
    return serial(`enroll:${handle}`,async()=>{
      const row=store.reserve(handle,secret,request.ip,config.registrationPerHour,config.registrationPerDay);
      const cached=store.credentials(row);
      if (cached) {
        // Never resurrect revoked credentials by silently logging in again.
        const current=await synapse.whoami(cached.access_token);
        if (current.user_id!==cached.user_id || current.device_id!==cached.device_id) throw new ApiError(401,'SESSION_INVALID','The original enrollment session was revoked.','Use explicit device recovery.');
        return reply.code(200).send(cached);
      }
      const userId=`@${handle}:${config.serverName}`;
      const exists=await synapse.userExists(userId);
      if (row.phase==='reserved' && exists) { store.phase(handle,'collision'); throw new ApiError(409,'HANDLE_TAKEN','This handle is unavailable.','Choose another handle.'); }
      if (row.phase==='collision') throw new ApiError(409,'HANDLE_TAKEN','This handle is unavailable.','Choose another handle.');
      if (!exists) {
        store.phase(handle,'creating');
        await synapse.admin(`/_synapse/admin/v2/users/${encodeURIComponent(userId)}`,'PUT',{password:store.password(secret),admin:false,deactivated:false,displayname:display_name??handle});
      }
      const session=await synapse.login(userId,store.password(secret),row.device,device_display_name??'Zavliq agent runtime');
      if(session.user_id!==userId || session.device_id!==row.device) throw new ApiError(502,'INVALID_UPSTREAM','The homeserver returned an unexpected identity.');
      const result={...session,homeserver:config.publicHomeserverUrl};
      store.complete(handle,result);
      return reply.code(201).send(result);
    });
  });

  app.post<{Body:{handle:string;registration_secret:string;recovery_id:string;device_display_name?:string}}>('/v1/agents/recover',{
    schema:{body:{type:'object',additionalProperties:false,required:['handle','registration_secret','recovery_id'],properties:{handle:handleSchema,registration_secret:secretSchema,recovery_id:secretSchema,device_display_name:{type:'string',minLength:1,maxLength:80}}}},
  },async(request)=>{
    store.attempt(request.ip);
    const {handle,registration_secret:secret,recovery_id,device_display_name}=request.body;
    store.assertProof(handle,secret);
    const key=store.fingerprint(`${handle}:${recovery_id}`,'recovery');
    return serial(`recover:${key}`,async()=>{
      let row=store.db.prepare('SELECT * FROM recoveries WHERE id=?').get(key) as {credentials:string|null;device:string}|undefined;
      if(row?.credentials) {
        const cached=store.unseal<Credentials>(row.credentials,`recovery:${key}`);
        await synapse.whoami(cached.access_token);
        return {...cached,encryption_recovery_required:true};
      }
      if(!row) {
        const count=store.db.prepare('SELECT count(*) AS n FROM recoveries WHERE handle=? AND at>?').get(handle,Date.now()-86_400_000) as {n:number};
        if(count.n>=5) throw new ApiError(429,'RECOVERY_LIMIT','Too many new devices were requested.','Use an existing device or retry tomorrow.',86_400_000);
        row={credentials:null,device:`ZAVL${randomBytes(12).toString('hex').toUpperCase()}`};
        store.db.prepare('INSERT INTO recoveries(id,handle,device,at) VALUES (?,?,?,?)').run(key,handle,row.device,Date.now());
      }
      const session=await synapse.login(`@${handle}:${config.serverName}`,store.password(secret),row.device,device_display_name??'Recovered Zavliq device');
      const result={...session,homeserver:config.publicHomeserverUrl};
      store.db.prepare('UPDATE recoveries SET credentials=? WHERE id=?').run(store.seal(result,`recovery:${key}`),key);
      return {...result,encryption_recovery_required:true};
    });
  });

  app.get('/v1/me',async(request)=>synapse.whoami(tokenFor(request)));
  app.get('/v1/devices',async(request)=>synapse.request('/_matrix/client/v3/devices',{token:tokenFor(request)}));
  app.delete<{Params:{deviceId:string}}>('/v1/devices/:deviceId',async(request)=>{
    const token=tokenFor(request), identity=await synapse.whoami(token);
    if(request.params.deviceId.length>255) throw new ApiError(400,'INVALID_INPUT','Invalid device identifier.');
    await synapse.admin(`/_synapse/admin/v2/users/${encodeURIComponent(identity.user_id)}/devices/${encodeURIComponent(request.params.deviceId)}`,'DELETE');
    return {revoked:true,device_id:request.params.deviceId};
  });
  app.get<{Params:{userId:string}}>('/v1/agents/:userId',async(request)=>{
    await synapse.whoami(tokenFor(request));
    const userId=request.params.userId;
    if(!userId.startsWith('@') || !userId.endsWith(`:${config.serverName}`) || userId.length>255) throw new ApiError(400,'INVALID_ADDRESS','Use an exact local Matrix address.','Example: @your-agent:'+config.serverName);
    const profile=await synapse.request<{displayname?:string;avatar_url?:string}>(`/_matrix/client/v3/profile/${encodeURIComponent(userId)}`,{token:tokenFor(request)});
    return {user_id:userId,display_name:profile.displayname??null,avatar_url:profile.avatar_url??null};
  });
  for (const name of ['blocks','profile','quotas'] as const) {
    app.get(`/v1/${name}`,async(request)=>synapse.request(`/_synapse/client/zavliq/${name}`,{token:tokenFor(request)}));
  }
  app.post<{Body:{user_id:string}}>('/v1/blocks',{schema:{body:{type:'object',additionalProperties:false,required:['user_id'],properties:{user_id:{type:'string',minLength:4,maxLength:255}}}}},async(request)=>synapse.request('/_synapse/client/zavliq/blocks',{method:'POST',token:tokenFor(request),body:request.body}));
  app.delete<{Params:{userId:string}}>('/v1/blocks/:userId',async(request)=>synapse.request('/_synapse/client/zavliq/blocks',{method:'DELETE',token:tokenFor(request),body:{user_id:request.params.userId}}));
  app.put<{Body:{directory_visible:boolean}}>('/v1/profile',{schema:{body:{type:'object',additionalProperties:false,required:['directory_visible'],properties:{directory_visible:{type:'boolean'}}}}},async(request)=>synapse.request('/_synapse/client/zavliq/profile',{method:'PUT',token:tokenFor(request),body:request.body}));
  app.get('/v1/directory',async(request)=>synapse.request('/_synapse/client/zavliq/directory',{token:tokenFor(request)}));
  app.post<{Body:{room_id:string;event_id?:string;reason:string}}>('/v1/reports',{schema:{body:{type:'object',additionalProperties:false,required:['room_id','reason'],properties:{room_id:{type:'string',maxLength:255},event_id:{type:'string',maxLength:255},reason:{type:'string',minLength:1,maxLength:2000}}}}},async(request)=>{
    const {room_id,event_id,reason}=request.body;
    if(!event_id) throw new ApiError(400,'EVENT_REQUIRED','A report needs the event identifier.','Select the message to report.');
    await synapse.request(`/_matrix/client/v3/rooms/${encodeURIComponent(room_id)}/report/${encodeURIComponent(event_id)}`,{method:'POST',token:tokenFor(request),body:{reason,score:-100}});
    return {submitted:true};
  });

  // Only public ingress may reach Synapse. Proxy every legacy/current upload path here.
  await app.register(async(media)=>{
    media.removeAllContentTypeParsers();
    media.addContentTypeParser('*',{parseAs:'buffer',bodyLimit:limits.file_bytes},(_req,body,done)=>done(null,body));
    for(const prefix of ['/_matrix/media/v3','/_matrix/media/r0','/_matrix/media/v1','/_matrix/client/v1/media']) {
      media.post<{Body:Buffer;Querystring:{filename?:string}}>(`${prefix}/upload`,{bodyLimit:limits.file_bytes},async(request)=>{
        const token=tokenFor(request),identity=await synapse.whoami(token);
        const bytes=request.body;
        if(!Buffer.isBuffer(bytes) || bytes.length===0) throw new ApiError(400,'EMPTY_FILE','An attachment must contain bytes.');
        const reservation=store.reserveMedia(identity.user_id,bytes.length);
        const filename=request.query.filename;
        if(filename && (filename.length>255 || /[\x00-\x1f\x7f]/.test(filename))) { store.releaseMedia(reservation);throw new ApiError(400,'INVALID_FILENAME','Use a filename under 256 characters without control characters.'); }
        try {
          const result=await synapse.request<{content_uri:string}>(`/_matrix/media/v3/upload${filename?`?filename=${encodeURIComponent(filename)}`:''}`,{method:'POST',token,binary:bytes,contentType:request.headers['content-type']??'application/octet-stream'});
          store.finishMedia(reservation,result.content_uri);
          return result;
        } catch(e) { if(e instanceof UpstreamError && e.status>=400 && e.status<500) store.releaseMedia(reservation);throw e; }
      });
      media.all(`${prefix}/upload/*`,async()=>{throw new ApiError(405,'UPLOAD_METHOD_UNSUPPORTED','Use synchronous POST media upload.','Upload with the Zavliq runtime or POST /_matrix/media/v3/upload.');});
      media.all(`${prefix}/create`,async()=>{throw new ApiError(405,'UPLOAD_METHOD_UNSUPPORTED','Asynchronous media reservations are unavailable.','Use synchronous POST media upload.');});
    }
  });
  const mediaCleanup=setInterval(()=>{
    void (async()=>{
      const rows=store.db.prepare('SELECT id,content_uri FROM media WHERE at<? AND content_uri IS NOT NULL LIMIT 100').all(Date.now()-30*86_400_000) as {id:string;content_uri:string}[];
      for(const row of rows) {
        const uri=new URL(row.content_uri);
        if(uri.protocol!=='mxc:' || uri.host!==config.serverName) continue;
        try {
          await synapse.admin(`/_synapse/admin/v1/media/${encodeURIComponent(uri.host)}/${encodeURIComponent(uri.pathname.slice(1))}`,'DELETE');
          store.releaseMedia(row.id);
        } catch(e) { if(e instanceof UpstreamError && e.status===404) store.releaseMedia(row.id); }
      }
    })().catch(()=>{});
  },300_000);mediaCleanup.unref();
  app.addHook('onClose',async()=>clearInterval(mediaCleanup));
  if(!deps.store) app.addHook('onClose',async()=>store.close());
  return app;
}
