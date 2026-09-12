import "fake-indexeddb/auto";
import { expect, it, vi } from "vitest";
import { archivePendingSend, archivesForRoom, pendingSend, sendDurably } from "./outbox";
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

it("archives a refused or ambiguous attempt intact and permits a new logical send without replaying it", async () => {
  const user = "@archive:local", device = "DEVICE", room = "!room:local";
  const original = { body: "attachment.txt", content: { msgtype: "m.file", body: "attachment.txt", file: {
    url: "mxc://local/private-file", key: { k: "fixture-attachment-recovery-key" },
  } } } as Parameters<typeof sendDurably>[3];
  await expect(sendDurably(user, device, room, original, async () => { throw Error("refused fixture"); })).rejects.toThrow("refused");
  const pending = (await pendingSend(user, device, room))!;
  await expect(archivePendingSend(user, "OTHER", room, pending.transaction_id)).rejects.toThrow("changed");
  expect(await pendingSend(user, device, room)).toEqual(pending);
  const archived = await archivePendingSend(user, device, room, pending.transaction_id);
  expect(archived).toMatchObject({ ...pending, outcome: "unknown" });
  expect(await pendingSend(user, device, room)).toBeNull();
  expect(await archivesForRoom(user, device, room)).toEqual([archived]);
  const next = vi.fn(async (record) => { expect(record.transaction_id).not.toBe(pending.transaction_id); });
  await sendDurably(user, device, room, { body: "new logical message" }, next);
  expect(next).toHaveBeenCalledOnce();
  // Read again from IndexedDB, as a new connection/reload would.
  expect(await archivesForRoom(user, device, room)).toEqual([archived]);
  expect(await archivesForRoom(user, "OTHER", room)).toEqual([]);
  expect(await archivesForRoom("@other:local", device, room)).toEqual([]);
  expect(await archivesForRoom(user, device, "!other:local")).toEqual([]);
  const retry = vi.fn();
  await expect(sendDurably(user, device, room, original, retry, pending.transaction_id)).rejects.toThrow("retry was stopped");
  expect(retry).not.toHaveBeenCalled();
  expect(await pendingSend(user, device, room)).toBeNull();
});

it("does not archive while an active send is unsettled or reuse a stale confirmation", async () => {
  const user = "@archive-race:local", device = "DEVICE", room = "!room:local";
  const entered = gate(), waiting = gate();
  const sending = sendDurably(user, device, room, { body: "original" }, async () => {
    entered.resolve(); await waiting.promise; throw Error("lost response");
  });
  await entered.promise;
  const pending = (await pendingSend(user, device, room))!;
  await expect(archivePendingSend(user, device, room, pending.transaction_id)).rejects.toThrow("already in progress");
  waiting.resolve(); await expect(sending).rejects.toThrow("lost response");
  const replacement = { ...pending, transaction_id: "replacement", body: "changed" };
  await savePrivate(`outbox-v2:${JSON.stringify([user, device, room])}`, replacement);
  await expect(archivePendingSend(user, device, room, pending.transaction_id)).rejects.toThrow("changed");
  expect(await pendingSend(user, device, room)).toEqual(replacement);
  expect(await archivesForRoom(user, device, room)).toEqual([]);
});

it("serializes archival with sends and keeps pending intact when private archive storage fails", async () => {
  const user = "@archive-storage:local", device = "DEVICE", room = "!room:local";
  await expect(sendDurably(user, device, room, { body: "keep me" }, async () => { throw Error("timeout"); })).rejects.toThrow();
  const pending = (await pendingSend(user, device, room))!;
  const originalPut = IDBObjectStore.prototype.put;
  const put = vi.spyOn(IDBObjectStore.prototype, "put").mockImplementation(function (this: IDBObjectStore, value, storageKey) {
    if (String(storageKey).startsWith("outbox-archives-v1:")) throw new DOMException("Private storage full", "QuotaExceededError");
    return originalPut.call(this, value, storageKey);
  });
  try {
    const archiving = archivePendingSend(user, device, room, pending.transaction_id);
    await expect(sendDurably(user, device, room, { body: "different" }, vi.fn())).rejects.toThrow("already in progress");
    await expect(archiving).rejects.toThrow("Private storage full");
  } finally { put.mockRestore(); }
  expect(await pendingSend(user, device, room)).toEqual(pending);
  expect(await archivesForRoom(user, device, room)).toEqual([]);
  await expect(archivePendingSend(user, device, room, pending.transaction_id, () => { throw Error("SDK is processing"); })).rejects.toThrow("SDK is processing");
  expect(await pendingSend(user, device, room)).toEqual(pending);
});

it("CAS preserves a replaced record and creates no archive if it changes after the stop check", async () => {
  const user = "@archive-cas:local", device = "DEVICE", room = "!room:local";
  const storageKey = `outbox-v2:${JSON.stringify([user, device, room])}`;
  await expect(sendDurably(user, device, room, { body: "original" }, async () => { throw Error("timeout"); })).rejects.toThrow();
  const pending = (await pendingSend(user, device, room))!;
  const replacement = { ...pending, transaction_id: "next-generation" };
  let replacementWrite: Promise<void> | undefined;
  await expect(archivePendingSend(user, device, room, pending.transaction_id, () => {
    replacementWrite = savePrivate(storageKey, replacement);
  })).rejects.toThrow("changed while");
  await replacementWrite;
  expect(await pendingSend(user, device, room)).toEqual(replacement);
  expect(await archivesForRoom(user, device, room)).toEqual([]);
});
