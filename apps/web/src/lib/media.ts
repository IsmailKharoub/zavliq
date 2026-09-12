import type { RoomMessageEventContent } from "matrix-js-sdk/lib/@types/events";
import type { MatrixClient, MatrixEvent } from "matrix-js-sdk";
import {
  decryptAttachment,
  encryptAttachment,
} from "matrix-encrypt-attachment";
import { pendingSend, sendDurably } from "./outbox";
import { sendPending } from "./send-pending";

const MAX_FILE = 10 * 1024 * 1024;
export async function sendFile(
  client: MatrixClient,
  roomId: string,
  file: File,
  encrypted: boolean,
): Promise<void> {
  if (!file.size || file.size > MAX_FILE)
    throw Error("Choose a file between 1 byte and 10 MiB.");
  if (await pendingSend(client.getUserId()!, client.getDeviceId()!, roomId))
    throw Error(
      "Retry the previous pending message before attaching a new file.",
    );
  const sdk = await import("matrix-js-sdk");
  let content: RoomMessageEventContent;
  if (encrypted) {
    const result = await encryptAttachment(await file.arrayBuffer());
    const hash = result.info.hashes?.sha256;
    if (!hash || result.info.v !== "v2")
      throw Error(
        "Attachment encryption did not produce a supported integrity record.",
      );
    const upload = await client.uploadContent(
      new Blob([result.data], { type: "application/octet-stream" }),
      { name: "encrypted", type: "application/octet-stream" },
    );
    content = {
      msgtype: sdk.MsgType.File,
      body: file.name,
      filename: file.name,
      info: {
        size: file.size,
        mimetype: file.type || "application/octet-stream",
      },
      file: {
        ...result.info,
        v: "v2",
        hashes: { sha256: hash },
        url: upload.content_uri,
      },
    };
  } else {
    const upload = await client.uploadContent(file, {
      name: file.name,
      type: file.type || "application/octet-stream",
    });
    content = {
      msgtype: sdk.MsgType.File,
      body: file.name,
      filename: file.name,
      info: {
        size: file.size,
        mimetype: file.type || "application/octet-stream",
      },
      url: upload.content_uri,
    };
  }
  await sendDurably(
    client.getUserId()!,
    client.getDeviceId()!,
    roomId,
    { body: file.name, content },
    (p) => sendPending(client, roomId, p),
  );
}
export async function downloadFile(
  client: MatrixClient,
  event: MatrixEvent,
): Promise<void> {
  const content = event.getContent();
  const mxc = content.file?.url || content.url;
  if (typeof mxc !== "string" || !mxc.startsWith("mxc://"))
    throw Error("This message has no valid attachment.");
  const url = client.mxcUrlToHttp(
    mxc,
    undefined,
    undefined,
    undefined,
    false,
    false,
    true,
  );
  if (!url || new URL(url).origin !== location.origin)
    throw Error("Attachment address does not belong to this service.");
  const target = new URL(url);
  target.searchParams.set("allow_remote", "false");
  const response = await fetch(target, {
    headers: { Authorization: `Bearer ${client.getAccessToken()}` },
    redirect: "error",
    signal: AbortSignal.timeout(60_000),
  });
  if (!response.ok)
    throw Error(
      response.status === 404
        ? "This attachment has expired or is no longer available."
        : "The attachment could not be downloaded.",
    );
  if (Number(response.headers.get("content-length") || 0) > MAX_FILE)
    throw Error("Attachment exceeds the 10 MiB limit.");
  const reader = response.body?.getReader();
  if (!reader) throw Error("Attachment response is empty.");
  let total = 0;
  const chunks: Uint8Array[] = [];
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.length;
    if (total > MAX_FILE) {
      await reader.cancel();
      throw Error("Attachment exceeds the 10 MiB limit.");
    }
    chunks.push(value);
  }
  const combined = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    combined.set(chunk, offset);
    offset += chunk.length;
  }
  const data = content.file
    ? await decryptAttachment(combined.buffer, content.file)
    : combined.buffer;
  const blob = new Blob([data], { type: "application/octet-stream" });
  const blobUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = blobUrl;
  anchor.download = String(content.filename || content.body || "attachment")
    .replace(/[\x00-\x1f\x7f/\\]/g, "_")
    .slice(0, 255);
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(blobUrl), 30000);
}
