export type Session = {
  user_id: string;
  device_id: string;
  access_token: string;
  homeserver: string;
};
const database = () =>
  new Promise<IDBDatabase>((resolve, reject) => {
    const r = indexedDB.open("zavliq-device", 1);
    r.onupgradeneeded = () => r.result.createObjectStore("session");
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
export async function loadSession(): Promise<Session | undefined> {
  const db = await database();
  return new Promise((resolve, reject) => {
    const tx = db.transaction("session");
    const r = tx.objectStore("session").get("current");
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
    tx.oncomplete = () => db.close();
  });
}
export async function saveSession(session: Session): Promise<void> {
  const db = await database();
  return new Promise((resolve, reject) => {
    const tx = db.transaction("session", "readwrite");
    tx.objectStore("session").put(session, "current");
    tx.oncomplete = () => {
      db.close();
      resolve();
    };
    tx.onerror = () => reject(tx.error);
  });
}
export async function clearSession(): Promise<void> {
  const db = await database();
  return new Promise((resolve, reject) => {
    const tx = db.transaction("session", "readwrite");
    tx.objectStore("session").delete("current");
    tx.oncomplete = () => {
      db.close();
      resolve();
    };
    tx.onerror = () => reject(tx.error);
  });
}
export async function loadPrivate<T>(key: string): Promise<T | undefined> {
  const db = await database();
  return new Promise((resolve, reject) => {
    const tx = db.transaction("session");
    const r = tx.objectStore("session").get(key);
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
    tx.oncomplete = () => db.close();
  });
}
export async function savePrivate(key: string, value: unknown): Promise<void> {
  const db = await database();
  return new Promise((resolve, reject) => {
    const tx = db.transaction("session", "readwrite");
    tx.objectStore("session").put(value, key);
    tx.oncomplete = () => {
      db.close();
      resolve();
    };
    tx.onerror = () => reject(tx.error);
  });
}
export function randomSecret(): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(32)), (n) =>
    n.toString(16).padStart(2, "0"),
  ).join("");
}

/** Compare and update private records in one durable IndexedDB transaction. */
export async function comparePrivate(key: string, matches: (value: unknown) => boolean, updates: Record<string, unknown>): Promise<boolean> {
  const db = await database();
  return new Promise((resolve, reject) => {
    const tx = db.transaction("session", "readwrite");
    const table = tx.objectStore("session");
    const request = table.get(key);
    let matched = false;
    let reason: unknown;
    request.onsuccess = () => {
      try {
        matched = matches(request.result);
        if (matched) for (const [name, value] of Object.entries(updates)) table.put(value, name);
      } catch (error) { reason = error; tx.abort(); }
    };
    tx.oncomplete = () => { db.close(); resolve(matched); };
    tx.onerror = tx.onabort = () => { db.close(); reject(reason || tx.error || Error("Private storage transaction failed.")); };
  });
}
export const API_TIMEOUT_MS = 30_000;
export class ControlApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly code?: string,
    public readonly action?: string,
    public readonly retry_after_ms?: number,
  ) {
    super(message);
    this.name = 'ControlApiError';
  }
}
export async function api<T>(
  path: string,
  body?: unknown,
  token?: string,
  method?: "GET" | "POST" | "PUT" | "DELETE",
): Promise<T> {
  const signal = AbortSignal.timeout(API_TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(path, {
      method: method || (body === undefined ? "GET" : "POST"),
      headers: {
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal,
    });
  } catch (error) {
    if (signal.aborted || (error instanceof DOMException && ['TimeoutError', 'AbortError'].includes(error.name))) {
      throw new ControlApiError('The service took too long to respond. Retry this action; your saved request is preserved.', 0, 'REQUEST_TIMEOUT', 'Retry the same action.');
    }
    // Preserve application errors; transport failures receive a useful connection
    // message without exposing request headers.
    if (error instanceof Error && error.name !== 'TypeError') throw error;
    throw new ControlApiError('The service could not be reached. Check your connection and retry.', 0, 'NETWORK_UNAVAILABLE', 'Check your connection and retry.');
  }
  let data: Record<string, unknown>;
  try {
    const value: unknown = await response.json();
    data = value && typeof value === 'object' ? value as Record<string, unknown> : {};
  } catch {
    throw new ControlApiError('The service returned an unreadable response. Retry this action.', response.status, 'INVALID_RESPONSE');
  }
  if (!response.ok) {
    const details = data.error && typeof data.error === 'object' ? data.error as Record<string, unknown> : {};
    const message = [details.message, data.message, data.error].find(value => typeof value === 'string') as string | undefined;
    const code = [details.code, data.errcode].find(value => typeof value === 'string') as string | undefined;
    const action = typeof details.action === 'string' ? details.action : undefined;
    const retry = typeof details.retry_after_ms === 'number' && details.retry_after_ms >= 0 ? details.retry_after_ms : undefined;
    const wait = retry === undefined ? '' : ` Try again in ${Math.max(1, Math.ceil(retry / 1000))} seconds.`;
    throw new ControlApiError(`${message || 'The request could not be completed.'}${action ? ` ${action}` : ''}${wait}`, response.status, code, action, retry);
  }
  return data as T;
}
