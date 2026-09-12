import { describe, expect, it, vi } from "vitest";
import { MatrixEvent, EventStatus, type MatrixClient, type Room } from "matrix-js-sdk";
import { sendPending } from "./send-pending";
import type { PendingSend } from "./outbox";

const pending: PendingSend = { body: "Hello", device_id: "DEVICE", transaction_id: "saved-transaction" };
function fixture(status?: EventStatus | null, remote = false) {
  const event = new MatrixEvent({ event_id: "$message", sender: "@sender:local", type: "m.room.message",
    content: { msgtype: "m.text", body: "Hello" }, unsigned: { transaction_id: pending.transaction_id } });
  event.setTxnId(pending.transaction_id);
  event.status = status ?? null;
  const room = { getEventForTxnId: () => remote ? undefined : event, getLiveTimeline: () => ({ getEvents: () => [event] }) } as unknown as Room;
  const client = { getDeviceId: () => "DEVICE", getUserId: () => "@sender:local", getRoom: () => room,
    sendMessage: vi.fn().mockResolvedValue({ event_id: "$message" }), resendEvent: vi.fn().mockResolvedValue({ event_id: "$message" }) } as unknown as MatrixClient;
  return { event, room, client };
}
describe("durable sends and SDK local echoes", () => {
  it("resends the existing failed local event using its original transaction", async () => {
    const { client, event, room } = fixture(EventStatus.NOT_SENT);
    await sendPending(client, "!room:local", pending);
    expect(client.resendEvent).toHaveBeenCalledWith(event, room);
    expect(client.sendMessage).not.toHaveBeenCalled();
    expect(event.getTxnId()).toBe(pending.transaction_id);
  });
  it.each([EventStatus.SENT, null])("settles an accepted event with status %s without a second send", async (status) => {
    const { client } = fixture(status, status === null);
    await sendPending(client, "!room:local", pending);
    expect(client.resendEvent).not.toHaveBeenCalled();
    expect(client.sendMessage).not.toHaveBeenCalled();
  });
  it("preserves the draft while the SDK is still sending", async () => {
    const { client } = fixture(EventStatus.SENDING);
    await expect(sendPending(client, "!room:local", pending)).rejects.toThrow("still being processed");
    expect(client.resendEvent).not.toHaveBeenCalled();
  });
  it("uses the saved transaction after a fresh connection and preserves exact JSON and reply", async () => {
    const { client } = fixture();
    client.getRoom = () => null;
    const json = '{"count":9007199254740993}';
    await sendPending(client, "!room:local", { ...pending, reply_id: "$parent", content: {
      msgtype: "m.text", body: "Structured JSON", "com.zavliq.data_json": json,
    } as PendingSend["content"] });
    expect(client.sendMessage).toHaveBeenCalledWith("!room:local", expect.objectContaining({
      "com.zavliq.data_json": json, "m.relates_to": { "m.in_reply_to": { event_id: "$parent" } },
    }), pending.transaction_id);
  });
  it("never retries under a different device", async () => {
    const { client } = fixture(EventStatus.NOT_SENT);
    await expect(sendPending(client, "!room:local", { ...pending, device_id: "OTHER" })).rejects.toThrow("another device");
    expect(client.resendEvent).not.toHaveBeenCalled();
  });
});
