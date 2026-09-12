/** Operator-only initial device provisioning. Run under bootstrap.sh's native-store flock. */
import { randomBytes } from 'node:crypto';
import { chmodSync, closeSync, existsSync, fsyncSync, lstatSync, mkdirSync, openSync, readFileSync, readdirSync, renameSync, unlinkSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

export class BootstrapError extends Error {
  constructor(code) { super(code); this.code = code; }
}
const fail = (code) => { throw new BootstrapError(code); };
const secret = () => randomBytes(32).toString('base64url');

export function privateWrite(path, value) {
  const temporary = join(dirname(path), `.echo-${randomBytes(12).toString('hex')}.tmp`);
  let fd;
  try {
    fd = openSync(temporary, 'wx', 0o600);
    writeFileSync(fd, JSON.stringify(value));
    fsyncSync(fd);
    closeSync(fd); fd = undefined;
    renameSync(temporary, path);
    const parent = openSync(dirname(path), 'r');
    try { fsyncSync(parent); } finally { closeSync(parent); }
  } finally {
    if (fd !== undefined) closeSync(fd);
    if (existsSync(temporary)) unlinkSync(temporary);
  }
}

function regular(path) {
  try { return lstatSync(path).isFile(); }
  catch (error) { if (error.code === 'ENOENT') return false; throw error; }
}
function directory(path) {
  mkdirSync(path, { recursive: true, mode: 0o700 });
  if (!lstatSync(path).isDirectory()) fail('PRIVATE_DIRECTORY_REQUIRED');
  chmodSync(path, 0o700);
}
function publicUrl(value) {
  const url = new URL(value);
  if ((url.protocol !== 'https:' && !(url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)))
      || url.username || url.password || url.search || url.hash) fail('HTTPS_OR_LOOPBACK_REQUIRED');
  return url.toString().replace(/\/$/, '');
}
function sameSession(a, b) {
  return a && b && ['user_id', 'device_id', 'access_token', 'homeserver'].every(key => a[key] === b[key]);
}

export async function bootstrap({ store, synapse, config, runtimeDir = '/echo/runtime', write = privateWrite }) {
  const homeserver = publicUrl(config.publicHomeserverUrl);
  const controlUrl = publicUrl(config.publicControlUrl);
  if (!/^[a-zA-Z0-9.-]+(?::[0-9]+)?$/.test(config.serverName)) fail('INVALID_SERVER_NAME');
  const userId = `@echo:${config.serverName}`;
  directory(runtimeDir);
  const identityPath = join(runtimeDir, 'identity.json');
  let identity;
  if (existsSync(identityPath)) {
    if (!regular(identityPath)) fail('PRIVATE_IDENTITY_REQUIRED');
    identity = JSON.parse(readFileSync(identityPath, 'utf8'));
    if (identity.handle !== 'echo' || identity.control_url !== controlUrl
        || !/^[A-Za-z0-9_-]{43}$/.test(identity.registration_secret)
        || !/^[A-Za-z0-9_-]{43}$/.test(identity.store_passphrase)
        || (identity.session !== null && typeof identity.session !== 'object')) fail('IDENTITY_MISMATCH');
    chmodSync(identityPath, 0o600);
  } else {
    // A missing identity never authorizes attaching another device to an old store.
    if (readdirSync(runtimeDir).some(name => name !== 'runtime.lock')) fail('RESTORE_COMPLETE_RUNTIME');
    identity = { handle: 'echo', control_url: controlUrl, registration_secret: secret(), store_passphrase: secret(), session: null };
    // Persist the only recovery proof before creating an account or contacting Synapse.
    write(identityPath, identity);
  }
  if (identity.session === null && readdirSync(runtimeDir).some(name => !['runtime.lock', 'identity.json'].includes(name))) {
    fail('RESTORE_COMPLETE_RUNTIME');
  }
  const row = store.reserve('echo', identity.registration_secret, 'operator:echo', config.registrationPerHour ?? 3, config.registrationPerDay ?? 50);
  let session = store.credentials(row);
  if (identity.session && (!session || !sameSession(identity.session, session))) fail('IDENTITY_MISMATCH');
  if (session) {
    if (session.user_id !== userId || session.device_id !== row.device || session.homeserver !== homeserver) fail('IDENTITY_MISMATCH');
    const current = await synapse.whoami(session.access_token);
    if (current.user_id !== userId || current.device_id !== row.device) fail('EXPLICIT_DEVICE_RECOVERY_REQUIRED');
  } else {
    const exists = await synapse.userExists(userId);
    if (row.phase === 'collision' || (row.phase === 'reserved' && exists)) {
      store.phase('echo', 'collision');
      fail('RESERVED_ACCOUNT_COLLISION');
    }
    if (!exists) {
      store.phase('echo', 'creating');
      await synapse.admin(`/_synapse/admin/v2/users/${encodeURIComponent(userId)}`, 'PUT', {
        password: store.password(identity.registration_secret), admin: false, deactivated: false,
        displayname: 'Zavliq Echo · automated demo',
      });
    }
    const login = await synapse.login(userId, store.password(identity.registration_secret), row.device, 'Zavliq Echo service');
    if (login.user_id !== userId || login.device_id !== row.device || typeof login.access_token !== 'string' || !login.access_token) fail('UNEXPECTED_UPSTREAM_IDENTITY');
    session = { user_id: login.user_id, device_id: login.device_id, access_token: login.access_token, homeserver };
    store.complete('echo', session);
  }
  // Identity-only copies must not initialize a second crypto store for an active device.
  // The full-volume restore keeps the encrypted Matrix store and its original keys.
  const cryptoPath = join(runtimeDir, 'matrix', 'matrix-sdk-crypto.sqlite3');
  const hasCrypto = regular(cryptoPath) && lstatSync(cryptoPath).size > 0;
  if (!hasCrypto) {
    const keys = await synapse.request('/_matrix/client/v3/keys/query', {
      method: 'POST', token: session.access_token, body: { device_keys: { [userId]: [row.device] } },
    });
    if (!keys.device_keys || typeof keys.device_keys !== 'object' || Object.keys(keys.failures ?? {}).length) fail('DEVICE_KEYS_UNVERIFIED');
    if (keys.device_keys[userId]?.[row.device]) fail('RESTORE_COMPLETE_RUNTIME');
  }
  if (!identity.session) write(identityPath, { ...identity, session });
  return { event: 'echo_bootstrap_ready', user_id: userId, device_id: row.device };
}

async function main() {
  let store;
  try {
    if (process.env.ZAVLIQ_ECHO_BOOTSTRAP_ENABLED !== 'true' || process.env.ZAVLIQ_ECHO_LOCKED !== 'true') fail('OPERATOR_WRAPPER_REQUIRED');
    const [{ loadConfig }, { Store }, { Synapse }] = await Promise.all([
      import('../control/dist/config.js'), import('../control/dist/store.js'), import('../control/dist/synapse.js'),
    ]);
    const config = loadConfig();
    // A typo must not quietly create an unrelated control database.
    if (!regular(config.database)) fail('EXISTING_CONTROL_DATABASE_REQUIRED');
    store = new Store(config.database, config.dataKey);
    const result = await bootstrap({ store, synapse: new Synapse(config.synapseUrl, config.adminToken), config });
    console.log(JSON.stringify(result));
  } catch (error) {
    console.error(JSON.stringify({ event: 'echo_bootstrap_stopped', code: error instanceof BootstrapError ? error.code : 'PROVISIONING_FAILED',
      action: 'Keep the private volume; check operator configuration and restore the full runtime or explicitly recover a fresh device. Never delete identity files to retry.' }));
    process.exitCode = 1;
  } finally { store?.close(); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) await main();
