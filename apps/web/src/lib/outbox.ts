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
const key = (userId: string, deviceId: string, roomId: string) =>
  `outbox-v2:${JSON.stringify([userId, deviceId, roomId])}`;
const active = new Map<string, { draft: string; promise: Promise<void> }>();
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
): Promise<void> {
  const storageKey = key(userId, deviceId, roomId);
  const snapshot = structuredClone(draft);
  const fingerprint = signature(snapshot);
  const existing = active.get(storageKey);
  if (existing) {
    if (existing.draft === fingerprint) return existing.promise;
    return Promise.reject(Error("A send is already in progress in this conversation. Wait for its result before sending another message."));
  }
  const promise = (async () => {
    let pending = await pendingSend(userId, deviceId, roomId);
    if (pending && signature(pending) !== fingerprint)
      throw Error("The previous send has an uncertain result. Restore its text and retry it before sending a new message.");
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
  active.set(storageKey, { draft: fingerprint, promise });
  void promise.finally(() => { if (active.get(storageKey)?.promise === promise) active.delete(storageKey); }).catch(() => {});
  return promise;
}
