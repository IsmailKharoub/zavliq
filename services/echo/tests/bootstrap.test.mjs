import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { Store } from '../../control/dist/store.js';
import { bootstrap, privateWrite } from '../bootstrap.mjs';

function fixture(t) {
  const directory = mkdtempSync(join(tmpdir(), 'echo-bootstrap-test-'));
  const runtimeDir = join(directory, 'runtime');
  const store = new Store(join(directory, 'control.sqlite'), 'a'.repeat(64));
  t.after(() => { store.close(); rmSync(directory, { recursive: true, force: true }); });
  const config = { serverName: 'local', publicHomeserverUrl: 'http://localhost:8080', publicControlUrl: 'http://localhost:8080' };
  const fake = {
    exists: false, creates: 0, logins: 0, keys: false, revoked: false, loseCreate: false,
    async userExists() {
      const identity = JSON.parse(readFileSync(join(runtimeDir, 'identity.json')));
      assert.equal(identity.session, null, 'proof is persisted before the first server operation');
      assert.equal(identity.registration_secret.length, 43);
      return this.exists;
    },
    async admin(path, method, body) {
      assert.equal(path, '/_synapse/admin/v2/users/%40echo%3Alocal');
      assert.equal(method, 'PUT');
      assert.equal(body.admin, false);
      this.exists = true; this.creates++;
      this.password = body.password;
      if (this.loseCreate) { this.loseCreate = false; throw new Error('lost creation response'); }
      return {};
    },
    async login(user_id, password, device_id) {
      assert.equal(password, this.password);
      this.logins++;
      this.session = { user_id, device_id, access_token: 'fake-token-' + this.logins };
      return this.session;
    },
    async whoami() {
      if (this.revoked) throw new Error('revoked fixture token');
      return { user_id: this.session.user_id, device_id: this.session.device_id };
    },
    async request(path, options) {
      assert.equal(path, '/_matrix/client/v3/keys/query');
      assert.deepEqual(options.body.device_keys, { '@echo:local': [this.session.device_id] });
      return { device_keys: this.keys ? { '@echo:local': { [this.session.device_id]: { keys: {} } } } : {} };
    },
  };
  const invoke = (extra = {}) => bootstrap({ store, synapse: fake, config, runtimeDir, ...extra });
  const identity = () => JSON.parse(readFileSync(join(runtimeDir, 'identity.json')));
  return { directory, runtimeDir, store, fake, config, invoke, identity };
}

test('new operator enrollment persists proof first and retries the same private device without rewriting', async t => {
  const f = fixture(t);
  const first = await f.invoke();
  const path = join(f.runtimeDir, 'identity.json');
  const initial = readFileSync(path, 'utf8');
  const before = statSync(path).mtimeMs;
  assert.equal(first.user_id, '@echo:local');
  assert.deepEqual(Object.keys(first).sort(), ['device_id', 'event', 'user_id']);
  assert.equal(statSync(path).mode & 0o777, 0o600);
  assert.equal(statSync(f.runtimeDir).mode & 0o777, 0o700);
  assert.deepEqual(await f.invoke(), first);
  assert.equal(readFileSync(path, 'utf8'), initial);
  assert.equal(statSync(path).mtimeMs, before);
  assert.equal(f.fake.creates, 1);
  assert.equal(f.fake.logins, 1);
});

test('lost account creation response retains proof and resumes ownership with the same device', async t => {
  const f = fixture(t);
  f.fake.loseCreate = true;
  await assert.rejects(f.invoke(), /lost creation/);
  const pending = f.identity();
  const device = f.store.find('echo').device;
  assert.equal(pending.session, null);
  await f.invoke();
  assert.equal(f.identity().registration_secret, pending.registration_secret);
  assert.equal(f.identity().session.device_id, device);
  assert.equal(f.fake.creates, 1);
});

test('lost final private write recovers cached credentials without another login', async t => {
  const f = fixture(t);
  await assert.rejects(f.invoke({ write: (path, identity) => {
    if (identity.session) throw new Error('disk unavailable');
    privateWrite(path, identity);
  } }), /disk unavailable/);
  assert.equal(f.identity().session, null);
  await f.invoke();
  assert.equal(f.fake.logins, 1);
  assert.equal(f.identity().session.access_token, 'fake-token-1');
});

test('preexisting reserved account collision never overwrites or logs into that account', async t => {
  const f = fixture(t);
  f.fake.exists = true;
  await assert.rejects(f.invoke(), { code: 'RESERVED_ACCOUNT_COLLISION' });
  await assert.rejects(f.invoke(), { code: 'RESERVED_ACCOUNT_COLLISION' });
  assert.equal(f.fake.creates, 0);
  assert.equal(f.fake.logins, 0);
  assert.equal(f.identity().session, null);
});

test('revoked enrollment never silently rotates or resurrects the original device', async t => {
  const f = fixture(t);
  await f.invoke();
  const initial = readFileSync(join(f.runtimeDir, 'identity.json'), 'utf8');
  f.fake.revoked = true;
  await assert.rejects(f.invoke(), /revoked/);
  assert.equal(f.fake.logins, 1);
  assert.equal(readFileSync(join(f.runtimeDir, 'identity.json'), 'utf8'), initial);
});

test('identity-only copy cannot recreate a crypto store for a device with published keys', async t => {
  const f = fixture(t);
  await f.invoke();
  f.fake.keys = true;
  await assert.rejects(f.invoke(), { code: 'RESTORE_COMPLETE_RUNTIME' });
  assert.equal(f.fake.logins, 1);
  mkdirSync(join(f.runtimeDir, 'matrix'));
  // The operator must restore the actual full encrypted database, never this test fixture.
  writeFileSync(join(f.runtimeDir, 'matrix', 'matrix-sdk-crypto.sqlite3'), 'fake restored database');
  await f.invoke();
  assert.equal(f.fake.logins, 1);
});

test('missing identity with existing native state and different identity are refused without server writes', async t => {
  const f = fixture(t);
  mkdirSync(f.runtimeDir);
  writeFileSync(join(f.runtimeDir, 'inbox.sqlite3'), 'old state');
  await assert.rejects(f.invoke(), { code: 'RESTORE_COMPLETE_RUNTIME' });
  rmSync(join(f.runtimeDir, 'inbox.sqlite3'));
  await f.invoke();
  const identity = f.identity();
  privateWrite(join(f.runtimeDir, 'identity.json'), { ...identity, session: { ...identity.session, device_id: 'OTHER' } });
  await assert.rejects(f.invoke(), { code: 'IDENTITY_MISMATCH' });
  assert.equal(f.fake.logins, 1);
});

test('unexpected upstream identity and unsupported public origins never seed credentials', async t => {
  const f = fixture(t);
  await assert.rejects(f.invoke({ config: { ...f.config, publicControlUrl: 'http://host.docker.internal:8080' } }), { code: 'HTTPS_OR_LOOPBACK_REQUIRED' });
  f.fake.login = async () => ({ user_id: '@wrong:local', device_id: 'wrong', access_token: 'fake' });
  await assert.rejects(f.invoke(), { code: 'UNEXPECTED_UPSTREAM_IDENTITY' });
  assert.equal(f.identity().session, null);
  assert.equal(f.store.credentials(f.store.find('echo')), undefined);
});

test('seeded identity is read by the real native runtime without exposing credentials', { skip: !process.env.ZAVLIQ_TEST_BINARY }, async t => {
  const f = fixture(t);
  const result = await f.invoke();
  const output = execFileSync(process.env.ZAVLIQ_TEST_BINARY, ['--data-dir', f.runtimeDir,
    '--control-url', f.config.publicControlUrl, 'call', 'identity'], { encoding: 'utf8', timeout: 10_000 });
  const publicIdentity = JSON.parse(output);
  assert.equal(publicIdentity.user_id, result.user_id);
  assert.equal(publicIdentity.device_id, result.device_id);
  assert.equal(output.includes(f.identity().registration_secret), false);
  assert.equal(output.includes(f.identity().store_passphrase), false);
  assert.equal(output.includes(f.identity().session.access_token), false);
});
