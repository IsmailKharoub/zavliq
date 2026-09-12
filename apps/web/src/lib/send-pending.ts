import type { MatrixClient } from "matrix-js-sdk";
import type { RoomMessageEventContent } from "matrix-js-sdk/lib/@types/events";
import type { PendingSend } from "./outbox";

/** Preserve both the durable transaction and any SDK local echo on retry. */
export async function sendPending(client: MatrixClient, roomId: string, pending: PendingSend): Promise<void> {
  if (client.getDeviceId() !== pending.device_id)
    throw Error("This pending message belongs to another device.");
  const room = client.getRoom(roomId);
  const event = room?.getEventForTxnId(pending.transaction_id)
    || room?.getLiveTimeline().getEvents().find((item) => item.getSender() === client.getUserId()
      && (item.getTxnId() || item.getUnsigned().transaction_id) === pending.transaction_id);
  if (event) {
    if (event.status === "sent" || event.status === null) return;
    if (event.status !== "not_sent")
      throw Error("The original message is still being processed. Wait for its result, then retry the saved draft if needed.");
    await client.resendEvent(event, room!);
    return;
  }
  const content = {
    ...(pending.content || { msgtype: "m.text", body: pending.body }),
    ...(pending.reply_id ? { "m.relates_to": { "m.in_reply_to": { event_id: pending.reply_id } } } : {}),
  } as RoomMessageEventContent;
  await client.sendMessage(roomId, content, pending.transaction_id);
}
