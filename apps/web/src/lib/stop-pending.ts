import type { MatrixClient } from "matrix-js-sdk";
import type { PendingSend } from "./outbox";

function originalEvent(client: MatrixClient, roomId: string, pending: PendingSend) {
  if (client.getDeviceId() !== pending.device_id)
    throw Error("This pending message belongs to another device.");
  const room = client.getRoom(roomId);
  const known = room?.getEventForTxnId(pending.transaction_id);
  if (known && known.getSender() === client.getUserId()) return known;
  return room?.getLiveTimeline().getEvents().find(event => event.getSender() === client.getUserId()
    && (event.getTxnId() || event.getUnsigned().transaction_id) === pending.transaction_id);
}

/** Call inside the outbox mutation guard before archiving its durable record. */
export function assertStoppable(client: MatrixClient, roomId: string, pending: PendingSend): void {
  const event = originalEvent(client, roomId, pending);
  if (event && event.status !== null && !["not_sent", "sent", "cancelled"].includes(event.status))
    throw Error("The original message is still being processed. Wait for its result before stopping retries.");
}

/** Only remove a terminal local echo after its attempt has been durably archived. */
export function cancelStoppedEcho(client: MatrixClient, roomId: string, pending: PendingSend): void {
  const event = originalEvent(client, roomId, pending);
  if (event?.status === "not_sent") client.cancelPendingEvent(event);
}
