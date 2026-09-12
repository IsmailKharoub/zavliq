import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { Session } from "./session";
const sdk = vi.hoisted(() => ({ createClient: vi.fn() }));
vi.mock("matrix-js-sdk", () => sdk);
function deferred<T = void>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const session = (token: string): Session => ({ user_id: "@fixture:local", device_id: "LOCAL", access_token: token, homeserver: "http://localhost" });
const held = new Set<string>();
const order: string[] = [];
let matrix: typeof import("./matrix");
function client(name: string, init = Promise.resolve(), start = Promise.resolve()) {
  const crypto = { globalBlacklistUnverifiedDevices: false };
  return {
    initRustCrypto: vi.fn(async () => { order.push(`init:${name}`); await init; order.push(`initialized:${name}`); }),
    startClient: vi.fn(async () => { order.push(`start:${name}`); await start; }),
    stopClient: vi.fn(() => order.push(`stop:${name}`)),
    getCrypto: () => crypto,
  };
}
beforeEach(async () => {
  vi.resetModules(); sdk.createClient.mockReset(); held.clear(); order.length = 0;
  vi.stubGlobal("location", { origin: "http://localhost" });
  vi.stubGlobal("navigator", { locks: { request: vi.fn(async (name, _options, callback) => {
    if (held.has(name)) return callback(null);
    held.add(name); order.push("locked");
    try { return await callback({ name }); }
    finally { held.delete(name); order.push("released"); }
  }) } });
  matrix = await import("./matrix");
});
afterEach(async () => { await matrix.disconnect(); vi.unstubAllGlobals(); });

it("holds the crypto lock through cancelled initialization and closes before reconnecting", async () => {
  const initializing = deferred();
  const first = client("first", initializing.promise), second = client("second");
  sdk.createClient.mockReturnValueOnce(first).mockReturnValueOnce(second);
  const pending = matrix.connect(session("first"));
  const rejected = expect(pending).rejects.toThrow("closed");
  await vi.waitFor(() => expect(first.initRustCrypto).toHaveBeenCalledOnce());
  const closed = matrix.disconnect();
  const reopened = matrix.connect(session("second"));
  await Promise.resolve();
  expect(held.size).toBe(1);
  expect(first.stopClient).not.toHaveBeenCalled();
  expect(sdk.createClient).toHaveBeenCalledOnce();
  initializing.resolve();
  await rejected; await closed;
  expect(await reopened).toBe(second);
  expect(first.startClient).not.toHaveBeenCalled();
  expect(first.stopClient).toHaveBeenCalledOnce();
  expect(order.indexOf("stop:first")).toBeLessThan(order.indexOf("released"));
  expect(order.indexOf("released")).toBeLessThan(order.indexOf("init:second"));
  expect(second.getCrypto().globalBlacklistUnverifiedDevices).toBe(true);
  expect(sdk.createClient.mock.calls[1][0].localTimeoutMs).toBe(matrix.MATRIX_HTTP_TIMEOUT_MS);
});

it("an older startup failure cannot clear the newer connection or its lock", async () => {
  const initializing = deferred();
  const first = client("first", initializing.promise), second = client("second");
  sdk.createClient.mockReturnValueOnce(first).mockReturnValueOnce(second);
  const initial = matrix.connect(session("first"));
  const rejected = expect(initial).rejects.toThrow("initialization unavailable");
  await vi.waitFor(() => expect(first.initRustCrypto).toHaveBeenCalledOnce());
  const newer = matrix.connect(session("second"));
  initializing.reject(Error("initialization unavailable"));
  await rejected;
  expect(await newer).toBe(second);
  expect(matrix.connect(session("second"))).toBe(newer);
  expect(held.size).toBe(1);
  expect(sdk.createClient).toHaveBeenCalledTimes(2);
});

it("disconnect also waits for startClient before closing and releasing", async () => {
  const starting = deferred();
  const first = client("first", Promise.resolve(), starting.promise);
  sdk.createClient.mockReturnValue(first);
  const pending = matrix.connect(session("first"));
  const rejected = expect(pending).rejects.toThrow("closed");
  await vi.waitFor(() => expect(first.startClient).toHaveBeenCalledOnce());
  const closing = matrix.disconnect();
  expect(held.size).toBe(1);
  expect(first.stopClient).not.toHaveBeenCalled();
  starting.resolve();
  await rejected; await closing;
  expect(first.stopClient).toHaveBeenCalledOnce();
  expect(held.size).toBe(0);
});

it("an unavailable tab lock never opens crypto storage", async () => {
  held.add("zavliq:@fixture:local:LOCAL");
  await expect(matrix.connect(session("same"))).rejects.toThrow("another browser tab");
  expect(sdk.createClient).not.toHaveBeenCalled();
  expect(held.size).toBe(1);
});
