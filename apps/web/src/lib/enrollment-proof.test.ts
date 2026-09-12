import "fake-indexeddb/auto";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { enroll, verifiedEnrollmentProof } from "./enrollment";
import { clearSession, loadPrivate, loadSession, saveSession, type Session } from "./session";
const origin = "http://localhost";
const credentials = (handle: string): Session => ({ user_id: `@${handle}:local`, device_id: "DEVICE", access_token: "synthetic-enrollment-token", homeserver: origin });
beforeEach(async () => { vi.stubGlobal("location", { origin }); await clearSession(); });
afterEach(() => vi.unstubAllGlobals());

it("a rejected signup remains pending data and cannot become a paired account's backup proof", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: { code: "HANDLE_TAKEN", message: "Handle is already registered." } }), { status: 409 })));
  await expect(enroll("taken-handle", "")).rejects.toThrow("already registered");
  expect(await loadPrivate("zavliq-pending-taken-handle")).toMatch(/^[a-f0-9]{64}$/);
  const paired = { ...credentials("taken-handle"), device_id: "PAIRED" };
  await saveSession(paired);
  expect(await loadSession()).toEqual(paired);
  expect(await verifiedEnrollmentProof(paired.user_id, origin)).toBeUndefined();
});

it("promotes successful proof only for the exact returned identity and control origin", async () => {
  let proof = "";
  const result = credentials("bound-handle");
  vi.stubGlobal("fetch", vi.fn(async (_url, request) => {
    proof = JSON.parse(request.body).registration_secret;
    return new Response(JSON.stringify(result), { status: 201 });
  }));
  expect(await enroll("bound-handle", "Bound fixture")).toEqual(result);
  expect(await verifiedEnrollmentProof(result.user_id, origin)).toBe(proof);
  expect(await verifiedEnrollmentProof("@another:local", origin)).toBeUndefined();
  expect(await verifiedEnrollmentProof(result.user_id, "https://different.example")).toBeUndefined();
});

it("concurrent enrollment attempts reuse one durable secret even when a response is lost", async () => {
  const proofs: string[] = [];
  let release!: () => void;
  const arrived = new Promise<void>(resolve => { release = resolve; });
  const result = credentials("concurrent-handle");
  vi.stubGlobal("fetch", vi.fn(async (_url, request) => {
    const index = proofs.push(JSON.parse(request.body).registration_secret);
    if (proofs.length === 2) release();
    await arrived;
    if (index === 1) throw Error("synthetic response loss after registration");
    return new Response(JSON.stringify(result), { status: 200 });
  }));
  const results = await Promise.allSettled([enroll("concurrent-handle", ""), enroll("concurrent-handle", "")]);
  expect(results.filter(result => result.status === "fulfilled")).toHaveLength(1);
  expect(results.filter(result => result.status === "rejected")).toHaveLength(1);
  expect(proofs).toHaveLength(2);
  expect(proofs[0]).toBe(proofs[1]);
  expect(await loadPrivate("zavliq-pending-concurrent-handle")).toBe(proofs[0]);
  expect(await verifiedEnrollmentProof(result.user_id, origin)).toBe(proofs[0]);
  expect(await loadSession()).toEqual(result);
});

it("rejects an unexpected response identity before promoting proof or activating a session", async () => {
  const wrong = credentials("different-handle");
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(wrong), { status: 201 })));
  await expect(enroll("expected-handle", "")).rejects.toThrow("unexpected identity");
  expect(await loadSession()).toBeUndefined();
  expect(await verifiedEnrollmentProof(wrong.user_id, origin)).toBeUndefined();
  expect(await verifiedEnrollmentProof("@expected-handle:local", origin)).toBeUndefined();
  expect(await loadPrivate("zavliq-pending-expected-handle")).toMatch(/^[a-f0-9]{64}$/);
});
