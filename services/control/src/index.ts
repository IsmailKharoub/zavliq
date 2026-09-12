import { buildApp } from './app.js';
import { loadConfig } from './config.js';
const config=loadConfig();
const app=await buildApp(config,{logger:true});
for(const signal of ['SIGTERM','SIGINT']) process.once(signal,()=>{void app.close().then(()=>process.exit(0));});
await app.listen({host:config.host,port:config.port});
