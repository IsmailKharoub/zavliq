import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, API_TIMEOUT_MS, ControlApiError } from './session';

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });
describe('control API failure guidance', () => {
  it('preserves service code, action and retry interval for a rejected request', async () => {
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ error: { code: 'REGISTRATION_LIMIT', message: 'Capacity reached.', action: 'Keep the saved enrollment secret.', retry_after_ms: 1250 } }), { status: 429 }))));
    await expect(api('/v1/agents', {})).rejects.toMatchObject({ name: 'ControlApiError', status: 429, code: 'REGISTRATION_LIMIT', action: 'Keep the saved enrollment secret.', retry_after_ms: 1250 });
    await expect(api('/v1/agents', {})).rejects.toThrow('Try again in 2 seconds');
  });
  it('passes a bounded signal and gives a retry-safe message when it aborts', async () => {
    const controller = new AbortController();
    const timeout = vi.spyOn(AbortSignal, 'timeout').mockReturnValue(controller.signal);
    vi.stubGlobal('fetch', vi.fn().mockImplementation((_path, options) => new Promise((_resolve, reject) => {
      expect(options.signal).toBe(controller.signal);
      options.signal.addEventListener('abort', () => reject(new DOMException('Timed out', 'TimeoutError')));
    })));
    const result = api('/v1/pairings/fixture/ack', {});
    controller.abort();
    await expect(result).rejects.toMatchObject({ code: 'REQUEST_TIMEOUT', status: 0 });
    expect(timeout).toHaveBeenCalledWith(API_TIMEOUT_MS);
  });
  it('handles an unavailable gateway without exposing credentials or raw network details', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('fetch failed')));
    await expect(api('/v1/me', undefined, 'private-fixture')).rejects.toBeInstanceOf(ControlApiError);
    await expect(api('/v1/me', undefined, 'private-fixture')).rejects.toThrow('Check your connection and retry');
  });
});
