import "fake-indexeddb/auto";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { finishPairing, type PendingPairing } from "./pairing";
import { clearSession, loadPrivate, loadSession, savePrivate, saveSession, type Session } from "./session";
const pairing: PendingPairing = { pairing_id: "first", pairing_secret: "private-fixture", confirmation_code: "123456", user_id: "@pair:local", expires_at: Date.now()+300000 };
const credentials: Session = { user_id: pairing.user_id, device_id: "NEW", access_token: "dummy-local-fixture", homeserver: "http://localhost" };
const previous: Session = { ...credentials, device_id: "PREVIOUS", access_token: "previous-local-fixture" };
beforeEach(async () => { await clearSession(); await savePrivate("pending-pairing", pairing); });
afterEach(() => vi.unstubAllGlobals());
it("does not adopt a cancelled generation or erase its replacement after ACK", async () => {
  await saveSession(previous);
  const replacement = { ...pairing, pairing_id: "replacement", pairing_secret: "new-private-fixture" };
  vi.stubGlobal("fetch", vi.fn(async () => {
    await savePrivate("pending-pairing", replacement);
    return new Response(JSON.stringify({ completed: true }), { status: 200 });
  }));
  await expect(finishPairing(pairing, credentials)).rejects.toThrow("cancelled or replaced");
  expect(await loadSession()).toEqual(previous);
  expect(await loadPrivate("pending-pairing")).toEqual(replacement);
});
it("does not resurrect cancelled credentials before starting ACK", async () => {
  await savePrivate("pending-pairing", null);
  const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
  await expect(finishPairing(pairing, credentials)).rejects.toThrow("cancelled or replaced");
  expect(fetch).not.toHaveBeenCalled();
  expect(await loadSession()).toBeUndefined();
});
it("concurrent finalizers preserve one completed session and can finish idempotently", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ completed: true }), { status: 200 })));
  const results = await Promise.all([finishPairing(pairing, credentials), finishPairing(pairing, credentials)]);
  expect(results).toEqual([credentials, credentials]);
  expect(await loadSession()).toEqual(credentials);
  expect(await loadPrivate("pending-pairing")).toBeNull();
});
it("requires a confirmed ACK and leaves unconfirmed credentials staged", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ completed: false }), { status: 200 })));
  await expect(finishPairing(pairing, credentials)).rejects.toThrow("not confirmed");
  expect(await loadSession()).toBeUndefined();
  expect(await loadPrivate("pending-pairing")).toMatchObject({ credentials });
});
