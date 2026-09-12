import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchStatsSnapshot, parseStatsSnapshot, statsFreshness, StatsError } from './stats';

const NOW = Date.parse('2026-09-12T12:00:00Z');
const DAY = 86_400_000;
const zero = { plaintext_messages: 0, encrypted_events: 0, message_activity: 0, participating_identities: 0 };

function snapshot(at = NOW) {
  const midnight = Math.floor(at / DAY) * DAY;
  return {
    schema: 'zavliq-public-stats-v1', as_of: new Date(at).toISOString(), refresh_seconds: 300,
    counts: { registered_total: 0, registered_service: 0, registered_test: 0, registered_other: 0, retained_conversations: 0 },
    activity_24h: { from: new Date(at - DAY).toISOString(), to: new Date(at).toISOString(), ...zero },
    daily_utc: Array.from({ length: 7 }, (_, index) => ({
      date: new Date(midnight - (6 - index) * DAY).toISOString().slice(0, 10), complete: index < 6, ...zero,
    })),
  };
}

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('public stats parsing and freshness', () => {
  it('accepts real zero counts and normalizes the seven days without mutating input', () => {
    const raw = snapshot(); raw.daily_utc.reverse();
    const parsed = parseStatsSnapshot(raw, NOW);
    expect(parsed.counts.registered_total).toBe(0);
    expect(parsed.activity_24h.message_activity).toBe(0);
    expect(parsed.daily_utc.map(row => row.date)).toEqual(snapshot().daily_utc.map(row => row.date));
    expect(raw.daily_utc[0].date).toBe('2026-09-12');
  });

  it('accepts valid stale snapshots and evaluates the 15-minute threshold separately', () => {
    const stale = parseStatsSnapshot(snapshot(NOW - 8 * DAY), NOW);
    expect(statsFreshness(stale, NOW)).toEqual({ ageSeconds: 8 * 86_400, stale: true, staleAfterSeconds: 900 });
    const current = parseStatsSnapshot(snapshot(), NOW);
    expect(statsFreshness(current, NOW + 899_999).stale).toBe(false);
    expect(statsFreshness(current, NOW + 900_000).stale).toBe(true);
  });

  it('allows modest clock skew with zero displayed age and rejects larger future snapshots', () => {
    const future = parseStatsSnapshot(snapshot(NOW + 59_000), NOW);
    expect(statsFreshness(future, NOW).ageSeconds).toBe(0);
    expect(() => parseStatsSnapshot(snapshot(NOW + 60_001), NOW)).toThrow(StatsError);
  });

  it('rejects negative, fractional, unsafe, missing, string and inconsistent counters', () => {
    for (const bad of [-1, 0.5, Number.MAX_SAFE_INTEGER + 1, NaN, '0', undefined]) {
      const raw = snapshot();
      Object.assign(raw.counts, { retained_conversations: bad });
      expect(() => parseStatsSnapshot(raw, NOW)).toThrow(StatsError);
    }
    const split = snapshot(); split.counts.registered_service = 1;
    expect(() => parseStatsSnapshot(split, NOW)).toThrow(StatsError);
    const activity = snapshot(); activity.activity_24h.encrypted_events = 1;
    expect(() => parseStatsSnapshot(activity, NOW)).toThrow(StatsError);
    const participants = snapshot(); participants.daily_utc[0].participating_identities = 1;
    expect(() => parseStatsSnapshot(participants, NOW)).toThrow(StatsError);
    const overflow = snapshot();
    Object.assign(overflow.counts, { registered_total: Number.MAX_SAFE_INTEGER, registered_service: Number.MAX_SAFE_INTEGER, registered_other: 1 });
    expect(() => parseStatsSnapshot(overflow, NOW)).toThrow(StatsError);
  });

  it('rejects mislabeled windows, broken calendars, timezones and incomplete day coverage', () => {
    const variants = [
      (raw: ReturnType<typeof snapshot>) => { raw.activity_24h.from = '2026-09-10T12:00:00Z'; },
      (raw: ReturnType<typeof snapshot>) => { raw.activity_24h.to = '2026-09-12T11:59:59Z'; },
      (raw: ReturnType<typeof snapshot>) => { raw.as_of = '2026-02-30T12:00:00Z'; },
      (raw: ReturnType<typeof snapshot>) => { raw.as_of = '2026-09-12T13:00:00+01:00'; },
      (raw: ReturnType<typeof snapshot>) => { raw.as_of = '2026-09-12T24:00:00Z'; },
      (raw: ReturnType<typeof snapshot>) => { raw.daily_utc[0].date = '2026-02-30'; },
      (raw: ReturnType<typeof snapshot>) => { raw.daily_utc[0].date = raw.daily_utc[1].date; },
      (raw: ReturnType<typeof snapshot>) => { raw.daily_utc[6].complete = true; },
      (raw: ReturnType<typeof snapshot>) => { raw.daily_utc[0].complete = false; },
      (raw: ReturnType<typeof snapshot>) => { raw.daily_utc.pop(); },
      (raw: ReturnType<typeof snapshot>) => { raw.refresh_seconds = 0; },
      (raw: ReturnType<typeof snapshot>) => { Object.assign(raw, { error: 'upstream failed' }); },
    ];
    for (const change of variants) {
      const raw = snapshot(); change(raw);
      expect(() => parseStatsSnapshot(raw, NOW)).toThrow(StatsError);
    }
  });

  it('accepts leap-day boundaries and nonzero internally consistent counts', () => {
    const at = Date.parse('2024-03-01T00:00:00Z'); const raw = snapshot(at);
    Object.assign(raw.counts, { registered_total: 6, registered_service: 1, registered_test: 2, registered_other: 3 });
    Object.assign(raw.activity_24h, { plaintext_messages: 2, encrypted_events: 3, message_activity: 5, participating_identities: 4 });
    expect(parseStatsSnapshot(raw, at).daily_utc[5].date).toBe('2024-02-29');
  });
});

describe('public stats fetch', () => {
  it('makes one anonymous uncached JSON request and returns zero as data', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(snapshot(Date.now())), { headers: { 'content-type': 'application/json; charset=utf-8' } }));
    vi.stubGlobal('fetch', fetchMock);
    expect((await fetchStatsSnapshot()).counts.registered_total).toBe(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith('/_zavliq/stats', expect.objectContaining({ credentials: 'omit', cache: 'no-store', redirect: 'error', headers: { Accept: 'application/json' } }));
  });

  it('never substitutes zero on HTTP, network, non-JSON or malformed JSON failures', async () => {
    for (const response of [new Response('unavailable', { status: 503 }),
      new Response('<html>not JSON</html>', { headers: { 'content-type': 'text/html' } }),
      new Response('{broken', { headers: { 'content-type': 'application/json' } })]) {
      const fetchMock = vi.fn().mockResolvedValue(response); vi.stubGlobal('fetch', fetchMock);
      await expect(fetchStatsSnapshot()).rejects.toBeInstanceOf(StatsError);
      expect(fetchMock).toHaveBeenCalledTimes(1);
    }
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('private upstream details')));
    await expect(fetchStatsSnapshot()).rejects.toMatchObject({ code: 'network', message: 'Public stats are temporarily unavailable.' });
  });

  it('bounds both declared and streamed response sizes', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { headers: { 'content-type': 'application/json', 'content-length': '65537' } })));
    await expect(fetchStatsSnapshot()).rejects.toMatchObject({ code: 'oversized' });
    const cancel = vi.fn();
    const body = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(new Uint8Array(65537)); }, cancel });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(body, { headers: { 'content-type': 'application/json' } })));
    await expect(fetchStatsSnapshot()).rejects.toMatchObject({ code: 'oversized' });
    expect(cancel).toHaveBeenCalled();
  });

  it('times out a stalled fetch and a stalled body, cancels work and never retries', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockImplementation(() => new Promise(() => undefined)); vi.stubGlobal('fetch', fetchMock);
    const waiting = fetchStatsSnapshot({ timeoutMs: 25 });
    const rejection = expect(waiting).rejects.toMatchObject({ code: 'timeout' });
    await vi.advanceTimersByTimeAsync(25); await rejection;
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const cancel = vi.fn();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new ReadableStream({ cancel }), { headers: { 'content-type': 'application/json' } })));
    const body = fetchStatsSnapshot({ timeoutMs: 25 });
    const bodyRejection = expect(body).rejects.toMatchObject({ code: 'timeout' });
    await vi.advanceTimersByTimeAsync(25); await bodyRejection;
    expect(cancel).toHaveBeenCalled();
  });

  it('honors pre-abort and unmount cancellation without retrying or leaving a timer', async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn().mockImplementation(() => new Promise(() => undefined)); vi.stubGlobal('fetch', fetchMock);
    const controller = new AbortController(); controller.abort();
    await expect(fetchStatsSnapshot({ signal: controller.signal })).rejects.toMatchObject({ code: 'aborted' });
    expect(fetchMock).not.toHaveBeenCalled();
    const mounted = new AbortController();
    const waiting = fetchStatsSnapshot({ signal: mounted.signal });
    const rejection = expect(waiting).rejects.toMatchObject({ code: 'aborted' });
    mounted.abort(); await rejection;
    expect(fetchMock.mock.calls[0][1].signal.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });
});
