import {
  encryptAccountBackup,
  type AccountBackupInput,
} from "./account-backup";
self.onmessage = async (
  event: MessageEvent<{ input: AccountBackupInput; passphrase: string }>,
) => {
  try {
    const result = await encryptAccountBackup(
      event.data.input,
      event.data.passphrase,
    );
    self.postMessage({ ok: true, result });
  } catch {
    self.postMessage({
      ok: false,
      error: "Local account backup failed. Check the passphrase and try again.",
    });
  }
};
