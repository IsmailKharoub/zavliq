import { useEffect, useState, type FormEvent } from "react";
import type { MatrixClient } from "matrix-js-sdk";
import { verifiedEnrollmentProof } from "./lib/enrollment";
import type { AccountBackupInput } from "./lib/account-backup";

function encrypt(
  input: AccountBackupInput,
  passphrase: string,
): Promise<Uint8Array> {
  return new Promise((resolve, reject) => {
    const worker = new Worker(
      new URL("./lib/account-backup-worker.ts", import.meta.url),
      { type: "module" },
    );
    const finish = () => {
      clearTimeout(timeout);
      worker.terminate();
    };
    const timeout = setTimeout(() => {
      finish();
      reject(
        Error(
          "Encryption took too long on this device. Try again with other apps closed.",
        ),
      );
    }, 60_000);
    worker.onmessage = (
      e: MessageEvent<{ ok: boolean; result?: Uint8Array; error?: string }>,
    ) => {
      finish();
      e.data.ok && e.data.result
        ? resolve(e.data.result)
        : reject(Error(e.data.error || "Backup failed."));
    };
    worker.onerror = () => {
      finish();
      reject(Error("Local encryption could not start. Reload and try again."));
    };
    worker.postMessage({ input, passphrase });
  });
}

export function AccountBackup({ client }: { client: MatrixClient }) {
  const handle = client.getUserId()!.split(":")[0].slice(1);
  const [available, setAvailable] = useState<boolean>(),
    [passphrase, setPassphrase] = useState(""),
    [confirmation, setConfirmation] = useState(""),
    [busy, setBusy] = useState(false),
    [status, setStatus] = useState(""),
    [error, setError] = useState("");
  useEffect(() => {
    verifiedEnrollmentProof(client.getUserId()!, location.origin)
      .then((secret) => setAvailable(!!secret))
      .catch(() => setError("Could not read this device’s recovery state."));
  }, [handle]);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    setStatus("");
    if (passphrase !== confirmation) {
      setError("The two passphrases do not match.");
      return;
    }
    if (passphrase.trim().length < 16) {
      setError("Use at least 16 characters, preferably several random words.");
      return;
    }
    setBusy(true);
    try {
      const secret = await verifiedEnrollmentProof(client.getUserId()!, location.origin);
      if (!secret)
        throw Error(
          "Export account recovery from the device that originally registered this identity.",
        );
      const room_keys_json = await client.getCrypto()!.exportRoomKeysAsJson();
      setStatus("Encrypting account recovery and room keys locally…");
      const bytes = await encrypt(
        {
          handle,
          control_url: location.origin,
          registration_secret: secret,
          room_keys_json,
        },
        passphrase,
      );
      const url = URL.createObjectURL(
        new Blob([new Uint8Array(bytes).buffer], {
          type: "application/octet-stream",
        }),
      );
      const a = document.createElement("a");
      a.href = url;
      a.download = `${handle}-zavliq-recovery.age`;
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30_000);
      setStatus(
        "Encrypted account backup prepared. It restores your identity and available encrypted history into a fresh native client. Keep its passphrase separately.",
      );
    } catch (e) {
      setStatus("");
      setError(e instanceof Error ? e.message : "Account backup failed.");
    } finally {
      setPassphrase("");
      setConfirmation("");
      setBusy(false);
    }
  };
  return (
    <section className="crypto-devices" aria-label="Account recovery">
      <h3>Back up your agent identity</h3>
      <p>
        Save an encrypted recovery file before losing this device. Restore it
        with the native client’s recovery import, then pair a new browser. This
        file grants account access; keep it private.
      </p>
      {available === false ? (
        <p>
          Full account recovery must be exported from the original registering
          device. This paired device can back up its room keys below.
        </p>
      ) : available ? (
        <form onSubmit={submit} aria-busy={busy}>
          <label>
            Account backup passphrase
            <input
              type="password"
              autoComplete="new-password"
              required
              minLength={16}
              maxLength={1024}
              disabled={busy}
              value={passphrase}
              onChange={(e) => setPassphrase(e.target.value)}
            />
          </label>
          <small>
            Use at least 16 characters. Leading and trailing spaces are ignored.
          </small>
          <label>
            Confirm account backup passphrase
            <input
              type="password"
              autoComplete="new-password"
              required
              disabled={busy}
              value={confirmation}
              onChange={(e) => setConfirmation(e.target.value)}
            />
          </label>
          <button className="button secondary" disabled={busy}>
            {busy ? "Encrypting locally…" : "Download account recovery"}
          </button>
        </form>
      ) : (
        <p>Checking this device…</p>
      )}
      {status && <p role="status">{status}</p>}
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
