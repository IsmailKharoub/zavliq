import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { EventEmitter } from 'node:events';
import { randomUUID } from 'node:crypto';

export interface Options {
  /** Path to the installed native zavliq executable. */
  binary?: string;
  /** Private persistent identity directory. One runtime may open it at a time. */
  dataDir?: string;
  controlUrl?: string;
  /** A timeout means the outcome may be unknown. Reuse send idempotency keys. */
  timeoutMs?: number;
}
export interface Identity { user_id: string; device_id: string; homeserver: string; data_dir: string }
export interface Accepted { event_id: string; transaction_id: string; status: 'accepted'; deduplicated?: boolean }
export interface Message { cursor: number; event_id: string; room_id: string; sender: string; timestamp: number; untrusted: true; preview?: string; content?: Record<string, unknown>; data?: unknown; data_json?: string }
export interface Inbox { items: Message[]; next_cursor: number; has_more: boolean; history_gap_rooms: string[] }
export interface Send { room_id: string; text?: string; data?: unknown; data_json?: string; reply_to?: string; thread_root?: string; idempotency_key?: string }
export class ZavliqError extends Error {
  constructor(public readonly code: string, message: string, public readonly action?: string) { super(message); this.name = 'ZavliqError'; }
}

/** A local process connection. It never runs an agent or acts on incoming messages. */
export class Zavliq extends EventEmitter {
  private readonly child: ChildProcessWithoutNullStreams;
  private readonly pending = new Map<number, {resolve: (value: unknown) => void; reject: (error: Error) => void; timer: NodeJS.Timeout}>();
  private sequence = 0;
  private buffer = '';
  private closed = false;
  private terminalNotified = false;
  private readonly timeout: number;

  constructor(options: Options = {}) {
    super();
    this.timeout = options.timeoutMs ?? 120_000;
    const args = ['rpc'];
    if (options.dataDir) args.push('--data-dir', options.dataDir);
    if (options.controlUrl) args.push('--control-url', options.controlUrl);
    this.child = spawn(options.binary ?? process.env.ZAVLIQ_BINARY ?? 'zavliq', args, { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
    this.child.stdout.setEncoding('utf8');
    this.child.stdout.on('data', (chunk: string) => this.consume(chunk));
    // Runtime stderr is deliberately not surfaced to model-facing callers.
    this.child.stderr.resume();
    this.child.on('error', () => { this.closed = true; this.failAll(new ZavliqError('RUNTIME_UNAVAILABLE', 'Unable to start zavliq. Install the native runtime or set ZAVLIQ_BINARY.')); this.notifyClosed(); });
    this.child.on('exit', () => { this.closed = true; this.failAll(new ZavliqError('RUNTIME_CLOSED', 'Runtime exited. Check whether another runtime owns this identity directory.')); this.notifyClosed(); });
  }
  private notifyClosed(): void {
    if (this.terminalNotified) return;
    this.terminalNotified = true;
    this.emit('notification', {method: 'connection_state', params: {connected: false, closed: true, code: 'RUNTIME_CLOSED', action: 'Create a new client connection; the identity and inbox remain on disk.'}});
  }
  private failAll(error: Error): void {
    for (const request of this.pending.values()) { clearTimeout(request.timer); request.reject(error); }
    this.pending.clear();
  }
  private consume(chunk: string): void {
    this.buffer += chunk;
    if (this.buffer.length > 8 * 1024 * 1024) { this.failAll(new ZavliqError('INVALID_RESPONSE', 'Runtime response exceeded 8 MiB.')); this.close(); return; }
    let newline: number;
    while ((newline = this.buffer.indexOf('\n')) >= 0) {
      const line = this.buffer.slice(0, newline); this.buffer = this.buffer.slice(newline + 1);
      if (!line.trim()) continue;
      let message: any;
      try { message = JSON.parse(line); } catch { this.failAll(new ZavliqError('INVALID_RESPONSE', 'Runtime emitted invalid JSON.')); this.close(); return; }
      if (message.method && message.id === undefined) { this.emit('notification', {method: message.method, params: message.params}); continue; }
      const request = this.pending.get(message.id);
      if (!request) continue;
      this.pending.delete(message.id); clearTimeout(request.timer);
      if (message.error) {
        const detail = message.error.data ?? message.error;
        request.reject(new ZavliqError(String(detail.code ?? 'OPERATION_FAILED'), detail.message ?? 'Zavliq operation failed.', detail.action));
      } else request.resolve(message.result);
    }
  }
  call<T = Record<string, unknown>>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    if (this.closed) return Promise.reject(new ZavliqError('RUNTIME_CLOSED', 'Create a new client connection.'));
    const id = ++this.sequence;
    let line: string;
    try { line = `${JSON.stringify({jsonrpc: '2.0', id, method, params})}\n`; }
    catch { return Promise.reject(new ZavliqError('INVALID_PARAMS', 'Parameters must be JSON serializable. Encode JavaScript BigInt values as strings.')); }
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new ZavliqError('OUTCOME_UNKNOWN', 'Operation timed out; it may still complete. Reuse its idempotency key when retrying a send.', 'Call flush to resume persisted pending sends.'));
      }, this.timeout);
      this.pending.set(id, {resolve: value => resolve(value as T), reject, timer});
      this.child.stdin.write(line, error => {
        if (!error) return;
        clearTimeout(timer); this.pending.delete(id); reject(new ZavliqError('RUNTIME_CLOSED', 'Unable to write to runtime.'));
      });
    });
  }
  init(handle: string, displayName?: string): Promise<Identity> { return this.call('init', {handle, display_name: displayName}); }
  pairStart(userId: string, deviceDisplayName?: string): Promise<Record<string, unknown>> { return this.call('pairing_start', {user_id: userId, device_display_name: deviceDisplayName}); }
  pairComplete(): Promise<Record<string, unknown>> { return this.call('pairing_complete'); }
  identity(): Promise<Identity> { return this.call('identity'); }
  send(message: Send): Promise<Accepted> { return this.call('send', {...message, idempotency_key: message.idempotency_key ?? randomUUID()}); }
  /** Set sync:false after a notification to drain the last synchronized local snapshot. */
  inbox(cursor = 0, limit = 10, options: {sync?: boolean} = {}): Promise<Inbox> { return this.call('inbox', {cursor, limit, sync: options.sync ?? true}); }
  wait(cursor = 0, timeoutSeconds = 30): Promise<Inbox> { return this.call('wait', {cursor, timeout_seconds: timeoutSeconds}); }
  createConversation(members: string[], options: {kind?: 'dm'|'group'|'channel'; encryption?: 'standard'|'e2ee'; name?: string} = {}): Promise<{room_id: string}> {
    return this.call('create_conversation', {members, ...options});
  }
  /** End this process connection. Identity and messages remain on disk. */
  close(): void { if (this.closed) return; this.closed = true; this.child.stdin.end(); this.child.kill('SIGTERM'); this.failAll(new ZavliqError('RUNTIME_CLOSED', 'Client connection closed.')); }
}
