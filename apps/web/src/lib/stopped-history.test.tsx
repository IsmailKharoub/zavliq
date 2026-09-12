import { expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { StoppedSendHistory } from "../PendingSendNotice";
import type { ArchivedSend } from "./outbox";

it("renders only the stopped draft body and status, never its private attachment descriptor", () => {
  const record = { body: "report <script>literal</script>.txt", transaction_id: "original", device_id: "DEVICE",
    archived_at: 1000, outcome: "unknown", content: { msgtype: "m.file", body: "report.txt",
      file: { key: { k: "private-attachment-key-fixture" }, url: "mxc://local/private-file" } } } as ArchivedSend;
  const html = renderToStaticMarkup(<StoppedSendHistory records={[record]} />);
  expect(html).toContain("Acceptance unknown");
  expect(html).toContain("&lt;script&gt;literal&lt;/script&gt;");
  expect(html).not.toContain("private-attachment-key-fixture");
  expect(html).not.toContain("mxc://");
  expect(html).not.toContain("<script>");
  expect(html).not.toContain("<details open");
});
