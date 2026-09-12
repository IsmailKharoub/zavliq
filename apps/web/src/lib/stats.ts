export interface PublicStatsSnapshot {
  schema: 'zavliq-public-stats-v1';
  as_of: string;
  refresh_seconds: 300;
  counts: {
    registered_total: number;
    registered_service: number;
    registered_test: number;
    registered_other: number;
    retained_conversations: number;
  };
  activity_24h: {
    from: string;
    to: string;
    plaintext_messages: number;
    encrypted_events: number;
    message_activity: number;
    participating_identities: number;
  };
  daily_utc: Array<{
    date: string;
    complete: boolean;
    plaintext_messages: number;
    encrypted_events: number;
    message_activity: number;
    participating_identities: number;
  }>;
}

type StatsErrorCode = 'network' | 'http' | 'timeout' | 'aborted' | 'invalid' | 'oversized';
const MESSAGES: Record<StatsErrorCode, string> = {
  network: 'Public stats are temporarily unavailable.',
  http: 'Public stats are temporarily unavailable.',
  timeout: 'Public stats took too long to load.',
  aborted: 'Stats request cancelled.',
  invalid: 'Public stats returned an invalid snapshot.',
  oversized: 'Public stats returned an oversized snapshot.',
};

export class StatsError extends Error {
  constructor(public readonly code: StatsErrorCode) {
    super(MESSAGES[code]);
    this.name = 'StatsError';
  }
}

const DAY_MS = 86_400_000;
const MAX_BYTES = 64 * 1024;
const ACTIVITY_KEYS = ['plaintext_messages', 'encrypted_events', 'message_activity', 'participating_identities'];

function valid(condition: unknown): asserts condition {
  if (!condition) throw new StatsError('invalid');
}

function object(value: unknown, keys: string[]): Record<string, unknown> {
  valid(value !== null && typeof value === 'object' && !Array.isArray(value));
  const result = value as Record<string, unknown>;
  valid(Object.keys(result).length === keys.length && keys.every(key => Object.hasOwn(result, key)));
  return result;
}

function counter(value: unknown): number {
  valid(typeof value === 'number' && Number.isSafeInteger(value) && value >= 0);
  return value;
}

/** Only UTC ISO timestamps with real calendar dates; Date.parse alone normalizes broken dates. */
function timestamp(value: unknown): number {
  valid(typeof value === 'string');
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?(?:Z|\+00:00)$/.exec(value);
  valid(match);
  const parsed = Date.parse(value);
  const date = new Date(parsed);
  valid(Number.isFinite(parsed) && date.getUTCFullYear() === Number(match[1]) &&
    date.getUTCMonth() + 1 === Number(match[2]) && date.getUTCDate() === Number(match[3]) &&
    date.getUTCHours() === Number(match[4]) && date.getUTCMinutes() === Number(match[5]) &&
    date.getUTCSeconds() === Number(match[6]));
  return parsed;
}

function activity(value: Record<string, unknown>) {
  const plaintext_messages = counter(value.plaintext_messages);
  const encrypted_events = counter(value.encrypted_events);
  const message_activity = counter(value.message_activity);
  const participating_identities = counter(value.participating_identities);
  valid(Number.isSafeInteger(plaintext_messages + encrypted_events) &&
    plaintext_messages + encrypted_events === message_activity && participating_identities <= message_activity);
  return { plaintext_messages, encrypted_events, message_activity, participating_identities };
}

/** Valid stale snapshots stay usable. Freshness is evaluated separately against the display clock. */
export function parseStatsSnapshot(value: unknown, nowMs = Date.now()): PublicStatsSnapshot {
  valid(Number.isFinite(nowMs));
  const root = object(value, ['schema', 'as_of', 'refresh_seconds', 'counts', 'activity_24h', 'daily_utc']);
  valid(root.schema === 'zavliq-public-stats-v1' && root.refresh_seconds === 300);
  const asOf = timestamp(root.as_of);
  valid(asOf <= nowMs + 60_000);
  const counts = object(root.counts, ['registered_total', 'registered_service', 'registered_test', 'registered_other', 'retained_conversations']);
  const parsedCounts = {
    registered_total: counter(counts.registered_total),
    registered_service: counter(counts.registered_service),
    registered_test: counter(counts.registered_test),
    registered_other: counter(counts.registered_other),
    retained_conversations: counter(counts.retained_conversations),
  };
  const total = parsedCounts.registered_service + parsedCounts.registered_test + parsedCounts.registered_other;
  valid(Number.isSafeInteger(total) && total === parsedCounts.registered_total);
  const window = object(root.activity_24h, ['from', 'to', ...ACTIVITY_KEYS]);
  const from = timestamp(window.from);
  const to = timestamp(window.to);
  valid(to === asOf && to - from === DAY_MS);
  valid(Array.isArray(root.daily_utc) && root.daily_utc.length === 7);
  const today = Math.floor(asOf / DAY_MS) * DAY_MS;
  const daily = root.daily_utc.map(value => {
    const row = object(value, ['date', 'complete', ...ACTIVITY_KEYS]);
    valid(typeof row.date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(row.date));
    timestamp(row.date + 'T00:00:00Z');
    valid(typeof row.complete === 'boolean');
    return { date: row.date, complete: row.complete, ...activity(row) };
  }).sort((a, b) => a.date.localeCompare(b.date));
  daily.forEach((row, index) => {
    valid(row.date === new Date(today - (6 - index) * DAY_MS).toISOString().slice(0, 10));
    valid(row.complete === (index < 6));
  });
  return {
    schema: 'zavliq-public-stats-v1', as_of: root.as_of as string, refresh_seconds: 300,
    counts: parsedCounts,
    activity_24h: { from: window.from as string, to: window.to as string, ...activity(window) },
    daily_utc: daily,
  };
}

export function statsFreshness(snapshot: PublicStatsSnapshot, nowMs = Date.now()) {
  valid(Number.isFinite(nowMs));
  const ageSeconds = Math.max(0, (nowMs - timestamp(snapshot.as_of)) / 1000);
  const staleAfterSeconds = snapshot.refresh_seconds * 3;
  return { ageSeconds, stale: ageSeconds >= staleAfterSeconds, staleAfterSeconds };
}

/** One bounded attempt, including a stalled response body. No retries or synthetic zero fallback. */
export async function fetchStatsSnapshot(
  { signal, timeoutMs = 10_000 }: { signal?: AbortSignal; timeoutMs?: number } = {},
): Promise<PublicStatsSnapshot> {
  valid(Number.isFinite(timeoutMs) && timeoutMs > 0 && timeoutMs <= 30_000);
  if (signal?.aborted) throw new StatsError('aborted');
  const controller = new AbortController();
  let cancelBody: (() => void) | undefined;
  let rejectCancellation!: (error: StatsError) => void;
  const cancellation = new Promise<never>((_, reject) => { rejectCancellation = reject; });
  const cancel = (code: 'timeout' | 'aborted') => {
    rejectCancellation(new StatsError(code));
    controller.abort();
    cancelBody?.();
  };
  const onAbort = () => cancel('aborted');
  signal?.addEventListener('abort', onAbort, { once: true });
  const timer = setTimeout(() => cancel('timeout'), timeoutMs);
  const request = async () => {
    const response = await fetch('/_zavliq/stats', {
      signal: controller.signal, credentials: 'omit', cache: 'no-store', redirect: 'error',
      headers: { Accept: 'application/json' },
    });
    if (!response.ok) throw new StatsError('http');
    valid(response.headers.get('content-type')?.split(';')[0].trim().toLowerCase() === 'application/json');
    const length = response.headers.get('content-length');
    if (length !== null) {
      valid(/^\d+$/.test(length));
      if (Number(length) > MAX_BYTES) throw new StatsError('oversized');
    }
    valid(response.body !== null);
    const reader = response.body.getReader();
    cancelBody = () => { void reader.cancel().catch(() => undefined); };
    const chunks: Uint8Array[] = [];
    let bytes = 0;
    try {
      while (true) {
        const part = await reader.read();
        if (part.done) break;
        bytes += part.value.byteLength;
        if (bytes > MAX_BYTES) throw new StatsError('oversized');
        chunks.push(part.value);
      }
      const joined = new Uint8Array(bytes);
      let offset = 0;
      for (const chunk of chunks) { joined.set(chunk, offset); offset += chunk.byteLength; }
      try {
        return parseStatsSnapshot(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(joined)));
      } catch (error) {
        if (error instanceof StatsError) throw error;
        throw new StatsError('invalid');
      }
    } finally {
      cancelBody();
      reader.releaseLock();
      cancelBody = undefined;
    }
  };
  try {
    return await Promise.race([cancellation, request()]);
  } catch (error) {
    if (error instanceof StatsError) throw error;
    throw new StatsError('network');
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', onAbort);
    controller.abort();
    cancelBody?.();
  }
}
