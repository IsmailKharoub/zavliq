import { useEffect, useRef, useState } from "react";
import type { MatrixClient } from "matrix-js-sdk";
import { archivePendingSend, archivesForRoom, pendingSend, sendDurably, type ArchivedSend, type PendingSend } from "./lib/outbox";
import { sendPending } from "./lib/send-pending";
import { assertStoppable, cancelStoppedEcho } from "./lib/stop-pending";

type Props = {
  client: MatrixClient;
  roomId: string;
  busy: boolean;
  revision: number;
  run: (fn: () => Promise<unknown>) => Promise<void>;
  onStopped: () => void;
  onSent?: () => void;
};
type Snapshot = { scope: string; pending?: PendingSend; archives: ArchivedSend[] };

export function PendingSendNotice({ client, roomId, busy, revision, run, onStopped, onSent }: Props) {
  const userId = client.getUserId()!, deviceId = client.getDeviceId()!;
  const scope = JSON.stringify([userId, deviceId, roomId]);
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const runRef = useRef(run);
  runRef.current = run;
  const loadErrorScope = useRef<string | undefined>(undefined);
  const [snapshot, setSnapshot] = useState<Snapshot>();
  const [refresh, setRefresh] = useState(0);
  const [confirmation, setConfirmation] = useState<string>();

  useEffect(() => {
    let live = true;
    if (!busy) void Promise.all([pendingSend(userId, deviceId, roomId), archivesForRoom(userId, deviceId, roomId)])
      .then(([pending, archives]) => { if (live) { loadErrorScope.current = undefined; setSnapshot({ scope, pending, archives }); } })
      .catch(error => {
        if (live && loadErrorScope.current !== scope) {
          loadErrorScope.current = scope;
          void runRef.current(async () => { throw error; });
        }
      });
    return () => { live = false; };
  }, [client, roomId, scope, busy, revision, refresh, userId, deviceId]);

  const current = snapshot?.scope === scope ? snapshot : undefined;
  const pending = current?.pending;
  const execute = (action: () => Promise<unknown>) => {
    void run(async () => {
      try { await action(); }
      finally { if (scopeRef.current === scope) setRefresh(value => value + 1); }
    });
  };
  const retry = () => {
    if (!pending) return;
    execute(async () => {
      await sendDurably(userId, deviceId, roomId,
        { body: pending.body, reply_id: pending.reply_id, content: pending.content },
        original => sendPending(client, roomId, original), pending.transaction_id);
      if (scopeRef.current === scope) { setConfirmation(undefined); (onSent || onStopped)(); }
    });
  };
  const stop = () => {
    if (!pending || confirmation !== pending.transaction_id) return;
    execute(async () => {
      const archived = await archivePendingSend(userId, deviceId, roomId, pending.transaction_id,
        original => assertStoppable(client, roomId, original));
      // Once committed, free the composer even if SDK local-echo cleanup fails.
      if (scopeRef.current === scope) { setConfirmation(undefined); onStopped(); }
      cancelStoppedEcho(client, roomId, archived);
    });
  };

  if (!pending && !current?.archives.length) return null;
  return <section className="pending-send-notice" aria-label="Saved send attempts" aria-busy={busy}>
    {pending && <div className="notice">
      <p>A saved send has an uncertain result. Retry the original attempt or stop retrying to compose a new message.</p>
      <p className="saved-send-body">{pending.body.slice(0, 500)}{pending.body.length > 500 ? "…" : ""}</p>
      {confirmation === pending.transaction_id ? <div>
        <p>This message may already have reached the conversation. Stop retrying and keep a private record? Sending it again as a new message could create a duplicate.</p>
        <button type="button" className="button secondary" disabled={busy} onClick={stop}>Stop retrying and keep record</button>
        <button type="button" className="text-link" disabled={busy} onClick={() => setConfirmation(undefined)}>Keep pending</button>
      </div> : <div>
        <button type="button" className="button secondary" disabled={busy} onClick={retry}>Retry saved send</button>
        <button type="button" className="text-link" disabled={busy} onClick={() => setConfirmation(pending.transaction_id)}>Stop retrying</button>
      </div>}
    </div>}
    {!!current?.archives.length && <StoppedSendHistory records={current.archives} />}
  </section>;
}

export function StoppedSendHistory({ records }: { records: ArchivedSend[] }) {
  return <details>
      <summary>Stopped retries ({records.length})</summary>
      <p>Private records for this device and conversation. Acceptance remains unknown; stopping a retry does not recall a message.</p>
      <ul>{records.map(record => <li key={record.transaction_id}>
        <small>{new Date(record.archived_at).toLocaleString()} · Acceptance unknown</small>
        <p className="saved-send-body">{record.body.slice(0, 500)}{record.body.length > 500 ? "…" : ""}</p>
      </li>)}</ul>
    </details>;
}
