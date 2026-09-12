import "fake-indexeddb/auto";
import { expect, it, vi } from "vitest";
import { pendingSend, sendDurably } from "./outbox";
import { loadPrivate, savePrivate } from "./session";
function gate() {
  let resolve!: () => void;
  const promise = new Promise<void>(yes => { resolve = yes; });
  return { promise, resolve };
}
it("coalesces concurrent identical sends and rejects a different draft until completion", async () => {
  const waiting = gate(), entered = gate();
  const send = vi.fn(async () => { entered.resolve(); await waiting.promise; });
  const first = sendDurably("@parallel:local", "DEVICE", "!room:local", { body: "one message" }, send);
  const duplicate = sendDurably("@parallel:local", "DEVICE", "!room:local", { body: "one message" }, send);
  expect(duplicate).toBe(first);
  await entered.promise;
  await expect(sendDurably("@parallel:local", "DEVICE", "!room:local", { body: "different" }, send)).rejects.toThrow("already in progress");
  expect((await pendingSend("@parallel:local", "DEVICE", "!room:local"))?.body).toBe("one message");
  waiting.resolve(); await Promise.all([first, duplicate]);
  expect(send).toHaveBeenCalledOnce();
  expect(await pendingSend("@parallel:local", "DEVICE", "!room:local")).toBeNull();
});
it("preserves a newer durable record when the earlier transport completes", async () => {
  const waiting = gate(), entered = gate();
  const task = sendDurably("@replace:local", "DEVICE", "!room:local", { body: "first" }, async () => { entered.resolve(); await waiting.promise; });
  await entered.promise;
  const next = { body: "newer pending draft", device_id: "DEVICE", transaction_id: "newer-logical-send" };
  await savePrivate(`outbox-v2:${JSON.stringify(["@replace:local", "DEVICE", "!room:local"])}`, next);
  waiting.resolve(); await task;
  expect(await pendingSend("@replace:local", "DEVICE", "!room:local")).toEqual(next);
});
it("does not reuse an ambiguous transaction from a different device", async () => {
  let transaction = "";
  await expect(sendDurably("@devices:local", "OLD", "!room:local", { body: "unknown outcome" }, async p => { transaction = p.transaction_id; throw Error("response lost"); })).rejects.toThrow("response lost");
  expect(await pendingSend("@devices:local", "NEW", "!room:local")).toBeUndefined();
  await sendDurably("@devices:local", "NEW", "!room:local", { body: "new device message" }, async p => {
    expect(p.device_id).toBe("NEW"); expect(p.transaction_id).not.toBe(transaction);
  });
  expect((await pendingSend("@devices:local", "OLD", "!room:local"))?.transaction_id).toBe(transaction);
});
it("preserves legacy ambiguous drafts instead of guessing their originating device", async () => {
  const key = "outbox:@legacy:local:!room:local";
  await savePrivate(key, { body: "keep private", transaction_id: "old-unbound-transaction" });
  await expect(pendingSend("@legacy:local", "NEW", "!room:local")).rejects.toThrow("no recorded device");
  expect(await loadPrivate(key)).toMatchObject({ transaction_id: "old-unbound-transaction" });
});
