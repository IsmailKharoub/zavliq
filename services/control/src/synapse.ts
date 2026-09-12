import { ApiError } from './errors.js';

export class UpstreamError extends Error {
  constructor(public status: number, public code: string, public retryAfterMs?: number) { super('Homeserver request failed'); }
}

export class Synapse {
  constructor(private base: string, readonly adminToken: string, private fetcher: typeof fetch = fetch) {}
  async request<T = Record<string, unknown>>(path: string, options: {method?: string; token?: string; body?: unknown; binary?: Buffer; contentType?: string} = {}): Promise<T> {
    let response: Response;
    try {
      response = await this.fetcher(`${this.base}${path}`, {
        method: options.method ?? 'GET',
        headers: {
          ...(options.token ? { Authorization: `Bearer ${options.token}` } : {}),
          ...((options.body !== undefined || options.binary !== undefined) ? {'Content-Type': options.contentType ?? 'application/json'} : {}),
        },
        body: options.binary ? new Uint8Array(options.binary) : (options.body === undefined ? undefined : JSON.stringify(options.body)),
        signal: AbortSignal.timeout(20_000), redirect: 'error',
      });
    } catch { throw new ApiError(503,'HOMESERVER_UNAVAILABLE','The homeserver is temporarily unavailable.','Retry with the same saved request identity.',5_000); }
    const body = await response.json().catch(() => ({})) as Record<string, unknown>;
    if (!response.ok) throw new UpstreamError(response.status, typeof body.errcode === 'string' ? body.errcode : 'M_UNKNOWN', typeof body.retry_after_ms === 'number' ? body.retry_after_ms : undefined);
    return body as T;
  }
  admin<T = Record<string, unknown>>(path: string, method = 'GET', body?: unknown) { return this.request<T>(path,{method,body,token:this.adminToken}); }
  async userExists(userId: string): Promise<boolean> {
    try { await this.admin(`/_synapse/admin/v2/users/${encodeURIComponent(userId)}`); return true; }
    catch(e) { if (e instanceof UpstreamError && e.status === 404) return false; throw e; }
  }
  async whoami(token: string): Promise<{user_id:string;device_id?:string}> {
    return this.request('/_matrix/client/v3/account/whoami',{token});
  }
  async login(userId: string, password: string, deviceId: string, displayName: string) {
    return this.request<{user_id:string;device_id:string;access_token:string}>('/_matrix/client/v3/login', {
      method:'POST',body:{type:'m.login.password',identifier:{type:'m.id.user',user:userId},password,device_id:deviceId,initial_device_display_name:displayName},
    });
  }
}
