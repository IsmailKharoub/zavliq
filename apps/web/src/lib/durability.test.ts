import "fake-indexeddb/auto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { enroll } from "./enrollment";
import { clearSession, loadPrivate, loadSession, savePrivate } from "./session";
import { finishPairing, loadReadySession } from "./pairing";
import { pendingSend, sendDurably } from "./outbox";
beforeEach(() => vi.stubGlobal("location", { origin: "http://localhost" }));
afterEach(() => vi.unstubAllGlobals());
describe("browser failure recovery", () => {
  it("does not activate a paired device until acknowledgement survives a lost response", async () => {
    await clearSession();
    const pairing = {
      pairing_id: "pair-local",
      pairing_secret: "private-fixture",
      confirmation_code: "123456",
      expires_at: Date.now() + 300000,
      user_id: "@paired:localhost",
    };
    const credentials = {
      user_id: "@paired:localhost",
      device_id: "NEW",
      access_token: "dummy-pairing",
      homeserver: "http://localhost",
    };
    await savePrivate("pending-pairing", pairing);
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockImplementationOnce((url) => {
          calls.push(url);
          throw Error("ack response lost");
        })
        .mockImplementationOnce((url) => {
          calls.push(url);
          return Promise.resolve(
            new Response(JSON.stringify({ completed: true }), { status: 200 }),
          );
        }),
    );
    await expect(finishPairing(pairing, credentials)).rejects.toThrow(
      "ack response lost",
    );
    expect(await loadSession()).toBeUndefined();
    expect(await loadPrivate("pending-pairing")).toMatchObject({ credentials });
    expect(await loadReadySession()).toEqual(credentials);
    expect(calls).toEqual([
      "/v1/pairings/pair-local/ack",
      "/v1/pairings/pair-local/ack",
    ]);
    expect(await loadPrivate("pending-pairing")).toBeNull();
  });
  it("keeps the enrollment proof after a response is lost and saves the eventual session", async () => {
    const proofs: string[] = [];
    const result = {
      user_id: "@browser:localhost",
      device_id: "D1",
      access_token: "dummy-only",
      homeserver: "http://localhost",
    };
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockImplementationOnce((_url, options) => {
          proofs.push(JSON.parse(options.body).registration_secret);
          throw Error("connection closed");
        })
        .mockImplementationOnce((_url, options) => {
          proofs.push(JSON.parse(options.body).registration_secret);
          return Promise.resolve(
            new Response(JSON.stringify(result), { status: 201 }),
          );
        }),
    );
    await expect(enroll("browser", "Browser")).rejects.toThrow(
      "connection closed",
    );
    expect(await enroll("browser", "Browser")).toEqual(result);
    expect(proofs[0]).toHaveLength(64);
    expect(proofs[1]).toBe(proofs[0]);
    expect(await loadSession()).toEqual(result);
  });
  it("reuses the durable transaction when acceptance was followed by a network failure", async () => {
    const observed: string[] = [];
    let first = true;
    const send = async (p: { transaction_id: string }) => {
      observed.push(p.transaction_id);
      if (first) {
        first = false;
        throw Error("response lost after server commit");
      }
    };
    await expect(
      sendDurably("@a:local", "DEVICE", "!r:local", { body: "Hello" }, send),
    ).rejects.toThrow("response lost");
    expect((await pendingSend("@a:local", "DEVICE", "!r:local"))?.body).toBe("Hello");
    await expect(
      sendDurably("@a:local", "DEVICE", "!r:local", { body: "Different" }, send),
    ).rejects.toThrow("uncertain result");
    await sendDurably("@a:local", "DEVICE", "!r:local", { body: "Hello" }, send);
    expect(observed).toHaveLength(2);
    expect(observed[0]).toBe(observed[1]);
    expect(await pendingSend("@a:local", "DEVICE", "!r:local")).toBeNull();
  });
  it("isolates pending messages by identity and conversation", async () => {
    await expect(
      sendDurably(
        "@b:local",
        "DEVICE",
        "!private:local",
        { body: "Keep local" },
        async () => {
          throw Error("offline");
        },
      ),
    ).rejects.toThrow();
    expect(
      await pendingSend("@different:local", "DEVICE", "!private:local"),
    ).toBeUndefined();
    expect(await pendingSend("@b:local", "DEVICE", "!other:local")).toBeUndefined();
  });
});
