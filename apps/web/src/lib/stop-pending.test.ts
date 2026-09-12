import { expect, it, vi } from "vitest";
import { EventStatus, MatrixEvent, type MatrixClient, type Room } from "matrix-js-sdk";
import { assertStoppable, cancelStoppedEcho } from "./stop-pending";

const pending = { body: "fixture", device_id: "DEVICE", transaction_id: "original" };
function fixture(status: EventStatus | null) {
  const event = new MatrixEvent({ sender: "@sender:local", event_id: "~local", type: "m.room.message", content: { body: "fixture" } });
  event.setTxnId(pending.transaction_id); event.status = status;
  const room = { getEventForTxnId: () => event, getLiveTimeline: () => ({ getEvents: () => [event] }) } as unknown as Room;
  const client = { getUserId: () => "@sender:local", getDeviceId: () => "DEVICE", getRoom: () => room,
    cancelPendingEvent: vi.fn() } as unknown as MatrixClient;
  return { event, client };
}

it.each([EventStatus.SENDING, EventStatus.QUEUED, EventStatus.ENCRYPTING])("refuses to stop a processing SDK event (%s)", status => {
  const { client } = fixture(status);
  expect(() => assertStoppable(client, "!room:local", pending)).toThrow("still being processed");
  cancelStoppedEcho(client, "!room:local", pending);
  expect(client.cancelPendingEvent).not.toHaveBeenCalled();
});
it("cancels only the original terminal failed echo, preserving accepted events and other devices", () => {
  const { client, event } = fixture(EventStatus.NOT_SENT);
  expect(() => assertStoppable(client, "!room:local", pending)).not.toThrow();
  cancelStoppedEcho(client, "!room:local", pending);
  expect(client.cancelPendingEvent).toHaveBeenCalledExactlyOnceWith(event);
  vi.mocked(client.cancelPendingEvent).mockClear();
  event.status = EventStatus.SENT;
  cancelStoppedEcho(client, "!room:local", pending);
  event.status = null;
  cancelStoppedEcho(client, "!room:local", pending);
  expect(client.cancelPendingEvent).not.toHaveBeenCalled();
  expect(() => assertStoppable(client, "!room:local", { ...pending, device_id: "OTHER" })).toThrow("another device");
});
