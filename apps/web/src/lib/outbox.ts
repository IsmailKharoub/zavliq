import type { RoomMessageEventContent } from "matrix-js-sdk/lib/@types/events";
import { comparePrivate, loadPrivate, savePrivate } from "./session";
export type PendingSend = {
  body: string;
  reply_id?: string;
  content?: RoomMessageEventContent;
  transaction_id: string;
  device_id: string;
};
type Draft = Pick<PendingSend, "body" | "reply_id" | "content">;
export type ArchivedSend = PendingSend & { archived_at: number; outcome: "unknown" };
const key = (userId: string, deviceId: string, roomId: string) =>
  `outbox-v2:${JSON.stringify([userId, deviceId, roomId])}`;
const archiveKey = (userId: string, deviceId: string, roomId: string) =>
  `outbox-archives-v1:${JSON.stringify([userId, deviceId, roomId])}`;
const active = new Map<string, { kind: "send" | "archive"; draft?: string; expectedTransactionId?: string; promise: Promise<unknown> }>();
const signature = (draft: Draft) => JSON.stringify([draft.body, draft.reply_id, draft.content]);

export async function pendingSend(userId: string, deviceId: string, roomId: string): Promise<PendingSend | undefined> {
  const pending = await loadPrivate<PendingSend>(key(userId, deviceId, roomId));
  if (pending) {
    if (pending.device_id !== deviceId) throw Error("This pending send belongs to another device and cannot be retried safely here.");
    return pending;
  }
  // Pre-binding drafts cannot safely be assigned to a device after the fact.
  // Preserve them instead of replaying a transaction under a different device.
  if (await loadPrivate(`outbox:${userId}:${roomId}`))
    throw Error("An older pending send has no recorded device. Keep this browser data and resolve that send from its original device before sending here.");
  return pending;
}

export function sendDurably(
  userId: string,
  deviceId: string,
  roomId: string,
  draft: Draft,
  send: (pending: PendingSend) => Promise<unknown>,
  expectedTransactionId?: string,
): Promise<void> {
  const storageKey = key(userId, deviceId, roomId);
  const snapshot = structuredClone(draft);
  const fingerprint = signature(snapshot);
  const existing = active.get(storageKey);
  if (existing) {
    if (existing.kind === "send" && existing.draft === fingerprint && existing.expectedTransactionId === expectedTransactionId)
      return existing.promise as Promise<void>;
    return Promise.reject(Error("A send is already in progress in this conversation. Wait for its result before sending another message."));
  }
  const promise = (async () => {
    let pending = await pendingSend(userId, deviceId, roomId);
    if (expectedTransactionId && pending?.transaction_id !== expectedTransactionId)
      throw Error("This saved send has changed or its retry was stopped. Review the current saved send before retrying.");
    if (pending && signature(pending) !== fingerprint)
      throw Error("The previous send has an uncertain result. Retry the saved send or explicitly stop retrying before sending a new message.");
    if (!pending) {
      pending = { ...snapshot, transaction_id: crypto.randomUUID(), device_id: deviceId };
      await savePrivate(storageKey, pending);
    }
    await send(pending);
    // Do not erase a newer record if state was replaced by another owner/action.
    await comparePrivate(storageKey, (value) => !!value && typeof value === "object"
      && (value as PendingSend).transaction_id === pending.transaction_id
      && (value as PendingSend).device_id === deviceId, { [storageKey]: null });
  })();
  active.set(storageKey, { kind: "send", draft: fingerprint, expectedTransactionId, promise });
  void promise.finally(() => { if (active.get(storageKey)?.promise === promise) active.delete(storageKey); }).catch(() => {});
  return promise;
}

/** Private records only; archived attempts are never returned to the send queue. */
export async function archivesForRoom(userId: string, deviceId: string, roomId: string): Promise<ArchivedSend[]> {
  const records = await loadPrivate<ArchivedSend[]>(archiveKey(userId, deviceId, roomId)) || [];
  if (!Array.isArray(records) || records.some(record => record.device_id !== deviceId))
    throw Error("The stopped-send records do not match this device. Keep this browser data for recovery.");
  return [...records].reverse();
}

export function archivePendingSend(
  userId: string,
  deviceId: string,
  roomId: string,
  expectedTransactionId: string,
  check?: (pending: PendingSend) => void,
): Promise<ArchivedSend> {
  const storageKey = key(userId, deviceId, roomId);
  if (active.has(storageKey))
    return Promise.reject(Error("A send or another outbox action is already in progress. Wait for it to finish before stopping retries."));
  const promise = (async () => {
    const [pending, newestFirst] = await Promise.all([
      pendingSend(userId, deviceId, roomId), archivesForRoom(userId, deviceId, roomId),
    ]);
    if (!pending) {
      const already = newestFirst.find(record => record.transaction_id === expectedTransactionId);
      if (already) return already;
      throw Error("This pending send has changed. Review the current saved send before stopping it.");
    }
    if (pending.transaction_id !== expectedTransactionId)
      throw Error("This pending send has changed. Review the current saved send before stopping it.");
    check?.(pending);
    const archived: ArchivedSend = { ...structuredClone(pending), archived_at: Date.now(), outcome: "unknown" };
    // Keep the complete original descriptor, including attachment recovery data,
    // in the same private transaction that clears this exact pending generation.
    const matched = await comparePrivate(storageKey,
      value => JSON.stringify(value) === JSON.stringify(pending), {
        [archiveKey(userId, deviceId, roomId)]: [...[...newestFirst].reverse(), archived],
        [storageKey]: null,
      });
    if (!matched) throw Error("This pending send changed while you were reviewing it. Its current record has been preserved.");
    return archived;
  })();
  active.set(storageKey, { kind: "archive", promise });
  void promise.finally(() => { if (active.get(storageKey)?.promise === promise) active.delete(storageKey); }).catch(() => {});
  return promise;
}
