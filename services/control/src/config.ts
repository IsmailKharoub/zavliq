export interface Config {
  serverName: string;
  synapseUrl: string;
  publicHomeserverUrl: string;
  publicControlUrl: string;
  adminToken: string;
  dataKey: string;
  database: string;
  allowedOrigins: string[];
  port: number;
  host: string;
  registrationPerHour: number;
  registrationPerDay: number;
  registrationOpen: boolean;
  pairingPerDay: number;
  trustProxy: false | string[];
}

function required(env: NodeJS.ProcessEnv, key: string): string {
  const value = env[key];
  if (!value) throw new Error(`Missing required configuration: ${key}`);
  return value;
}

export function loadConfig(env: NodeJS.ProcessEnv = process.env): Config {
  const dataKey = required(env, 'CONTROL_DATA_KEY');
  if (!/^[a-f0-9]{64}$/i.test(dataKey)) throw new Error('CONTROL_DATA_KEY must be 32 random bytes encoded as 64 hex characters');
  const url = (value: string) => {
    const parsed = new URL(value);
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password || parsed.search || parsed.hash) throw new Error('Invalid service URL');
    return parsed.toString().replace(/\/$/, '');
  };
  const integer = (key: string, fallback: number) => {
    const n = Number(env[key] ?? fallback);
    if (!Number.isSafeInteger(n) || n < 1) throw new Error(`Invalid positive integer: ${key}`);
    return n;
  };
  const publicHomeserverUrl = url(env.PUBLIC_HOMESERVER_URL ?? 'http://localhost:8008');
  const serverName = env.SERVER_NAME ?? 'localhost';
  if (!/^[a-zA-Z0-9.-]+(?::[0-9]+)?$/.test(serverName)) throw new Error('Invalid SERVER_NAME');
  return {
    serverName, synapseUrl: url(env.SYNAPSE_URL ?? 'http://localhost:8008'), publicHomeserverUrl,
    publicControlUrl: url(env.PUBLIC_CONTROL_URL ?? publicHomeserverUrl),
    adminToken: required(env, 'SYNAPSE_ADMIN_TOKEN'), dataKey,
    database: env.CONTROL_DATABASE ?? './data/control.sqlite',
    allowedOrigins: (env.ALLOWED_ORIGINS ?? publicHomeserverUrl).split(',').filter(Boolean),
    port: integer('PORT', 3001), host: env.HOST ?? '0.0.0.0',
    registrationPerHour: integer('REGISTRATION_PER_IP_HOUR', 3),
    registrationPerDay: integer('REGISTRATION_GLOBAL_DAY', 50),
    registrationOpen: env.REGISTRATION_OPEN !== 'false',
    pairingPerDay: integer('PAIRING_GLOBAL_DAY',1000),
    trustProxy: env.TRUSTED_PROXY_CIDRS?.split(',').filter(Boolean) ?? false,
  };
}
