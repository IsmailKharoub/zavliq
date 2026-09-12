import { DatabaseSync } from 'node:sqlite';
import { createCipheriv, createDecipheriv, createHmac, randomBytes, timingSafeEqual } from 'node:crypto';
import { mkdirSync, chmodSync } from 'node:fs';
import { dirname } from 'node:path';
import { ApiError } from './errors.js';

export interface Enrollment {
  handle: string; proof: string; device: string; phase: string; credentials: string | null;
}
export interface Credentials { user_id: string; device_id: string; access_token: string; homeserver: string }

/** Control metadata only. Message history and membership belong to Synapse/Postgres. */
export class Store {
  readonly db: DatabaseSync;
  private readonly key: Buffer;
  constructor(path: string, dataKey: string, private now = () => Date.now()) {
    this.key = Buffer.from(dataKey, 'hex');
    if (path !== ':memory:') mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    this.db = new DatabaseSync(path);
    if (path !== ':memory:') chmodSync(path, 0o600);
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;
      CREATE TABLE IF NOT EXISTS enrollments (
        handle TEXT PRIMARY KEY, proof TEXT NOT NULL, device TEXT NOT NULL,
        phase TEXT NOT NULL DEFAULT 'reserved', credentials TEXT, created_at INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS admissions (id INTEGER PRIMARY KEY, ip_hash TEXT NOT NULL, at INTEGER NOT NULL);
      CREATE INDEX IF NOT EXISTS admissions_at ON admissions(at);
      CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY, ip_hash TEXT NOT NULL, at INTEGER NOT NULL);
      CREATE INDEX IF NOT EXISTS attempts_at ON attempts(at);
      CREATE TABLE IF NOT EXISTS media (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, bytes INTEGER NOT NULL, at INTEGER NOT NULL, content_uri TEXT);
      CREATE INDEX IF NOT EXISTS media_owner ON media(user_id,at);
      CREATE TABLE IF NOT EXISTS recoveries (id TEXT PRIMARY KEY, handle TEXT NOT NULL, credentials TEXT, device TEXT NOT NULL, at INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS pairings (
        id TEXT PRIMARY KEY, proof TEXT NOT NULL, confirmation TEXT NOT NULL, user_id TEXT NOT NULL,
        device_display_name TEXT NOT NULL, device_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        credentials TEXT, expires_at INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, ip_hash TEXT NOT NULL, created_at INTEGER NOT NULL
      );
    `);
  }
  close() { this.db.close(); }
  fingerprint(value: string, purpose: string) { return createHmac('sha256', this.key).update(purpose).update('\0').update(value).digest('hex'); }
  password(secret: string) { return this.fingerprint(secret, 'matrix-password'); }
  seal(value: unknown, context: string): string {
    const nonce = randomBytes(12);
    const cipher = createCipheriv('aes-256-gcm', this.key, nonce);
    cipher.setAAD(Buffer.from(context));
    const ciphertext = Buffer.concat([cipher.update(JSON.stringify(value), 'utf8'), cipher.final()]);
    return Buffer.concat([nonce, cipher.getAuthTag(), ciphertext]).toString('base64url');
  }
  unseal<T>(value: string, context: string): T {
    const raw = Buffer.from(value, 'base64url');
    const decipher = createDecipheriv('aes-256-gcm', this.key, raw.subarray(0, 12));
    decipher.setAAD(Buffer.from(context));
    decipher.setAuthTag(raw.subarray(12, 28));
    return JSON.parse(Buffer.concat([decipher.update(raw.subarray(28)), decipher.final()]).toString('utf8')) as T;
  }
  transaction<T>(fn: () => T): T {
    this.db.exec('BEGIN IMMEDIATE');
    try { const value = fn(); this.db.exec('COMMIT'); return value; }
    catch (e) { this.db.exec('ROLLBACK'); throw e; }
  }
  attempt(ip: string) {
    const at = this.now(), hash = this.fingerprint(ip, 'admission-ip');
    this.transaction(() => {
      this.db.prepare('DELETE FROM attempts WHERE at < ?').run(at - 3_600_000);
      const count = this.db.prepare('SELECT count(*) AS n FROM attempts WHERE ip_hash=? AND at>?').get(hash, at - 60_000) as {n:number};
      if (count.n >= 20) throw new ApiError(429, 'RATE_LIMITED', 'Too many registration attempts.', 'Wait before retrying.', 60_000);
      this.db.prepare('INSERT INTO attempts(ip_hash,at) VALUES (?,?)').run(hash, at);
    });
  }
  find(handle: string): Enrollment | undefined { return this.db.prepare('SELECT * FROM enrollments WHERE handle=?').get(handle) as unknown as Enrollment | undefined; }
  reserve(handle: string, secret: string, ip: string, perHour: number, perDay: number): Enrollment {
    return this.transaction(() => {
      const existing = this.find(handle);
      const proof = this.fingerprint(secret, 'registration-proof');
      if (existing) {
        if (!timingSafeEqual(Buffer.from(existing.proof), Buffer.from(proof))) throw new ApiError(409, 'HANDLE_TAKEN', 'This handle is unavailable.', 'Choose another handle.');
        return existing;
      }
      const at = this.now(), ipHash = this.fingerprint(ip, 'admission-ip');
      this.db.prepare('DELETE FROM admissions WHERE at < ?').run(at - 86_400_000);
      const daily = this.db.prepare('SELECT count(*) AS n FROM admissions').get() as {n:number};
      const hourly = this.db.prepare('SELECT count(*) AS n FROM admissions WHERE ip_hash=? AND at>?').get(ipHash, at - 3_600_000) as {n:number};
      if (daily.n >= perDay || hourly.n >= perHour) throw new ApiError(429, 'REGISTRATION_LIMIT', 'Registration capacity has been reached.', 'Keep your saved registration secret and retry later.', daily.n >= perDay ? 86_400_000 : 3_600_000);
      this.db.prepare('INSERT INTO admissions(ip_hash,at) VALUES (?,?)').run(ipHash, at);
      const device = `ZAVL${randomBytes(12).toString('hex').toUpperCase()}`;
      this.db.prepare('INSERT INTO enrollments(handle,proof,device,created_at) VALUES (?,?,?,?)').run(handle,proof,device,at);
      return this.find(handle)!;
    });
  }
  phase(handle: string, phase: string) { this.db.prepare('UPDATE enrollments SET phase=? WHERE handle=?').run(phase, handle); }
  complete(handle: string, credentials: Credentials) {
    this.db.prepare("UPDATE enrollments SET phase='complete', credentials=? WHERE handle=?").run(this.seal(credentials, `enrollment:${handle}`), handle);
  }
  credentials(row: Enrollment): Credentials | undefined { return row.credentials ? this.unseal<Credentials>(row.credentials, `enrollment:${row.handle}`) : undefined; }
  assertProof(handle: string, secret: string): Enrollment {
    const row = this.find(handle);
    const proof = this.fingerprint(secret, 'registration-proof');
    if (!row || !timingSafeEqual(Buffer.from(row.proof),Buffer.from(proof))) throw new ApiError(401, 'INVALID_RECOVERY', 'The recovery credentials are invalid.', 'Use the original private registration secret.');
    return row;
  }
  reserveMedia(userId: string, bytes: number): string {
    return this.transaction(() => {
      const at = this.now();
      // Keeping reservations after uncertain upstream results prevents retry quota bypass.
      const usage = this.db.prepare('SELECT coalesce(sum(bytes),0) AS n FROM media WHERE user_id=?').get(userId) as {n:number};
      if (usage.n + bytes > 100 * 1024 * 1024) throw new ApiError(429,'MEDIA_QUOTA','Retained file allowance is full.','Wait for retention expiry before uploading more files.');
      const id = randomBytes(16).toString('hex');
      this.db.prepare('INSERT INTO media(id,user_id,bytes,at) VALUES (?,?,?,?)').run(id,userId,bytes,at);
      return id;
    });
  }
  finishMedia(id: string, uri: string) { this.db.prepare('UPDATE media SET content_uri=? WHERE id=?').run(uri,id); }
  releaseMedia(id: string) { this.db.prepare('DELETE FROM media WHERE id=?').run(id); }
}
