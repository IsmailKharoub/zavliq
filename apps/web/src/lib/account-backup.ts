import { Encrypter } from "age-encryption";
import { encryptRoomKeyFile } from "./room-key-file";

export type AccountBackupInput = {
  handle: string;
  control_url: string;
  registration_secret: string;
  room_keys_json: string;
};

/** Interoperable with the native runtime's existing age + Matrix recovery format. */
export async function encryptAccountBackup(
  input: AccountBackupInput,
  passphrase: string,
): Promise<Uint8Array> {
  const password = passphrase.trim();
  if (password.length < 16)
    throw new Error(
      "Use at least 16 characters, preferably several random words.",
    );
  if (
    !/^[a-z][a-z0-9_-]{2,31}$/.test(input.handle) ||
    !/^[A-Za-z0-9_-]{43,128}$/.test(input.registration_secret)
  )
    throw new Error(
      "This browser does not have the original account recovery proof.",
    );
  const room_keys = await encryptRoomKeyFile(input.room_keys_json, password);
  const bundle = {
    format: "zavliq-recovery-v1",
    handle: input.handle,
    control_url: input.control_url,
    registration_secret: input.registration_secret,
    room_keys,
  };
  const encrypter = new Encrypter();
  encrypter.setPassphrase(password);
  return encrypter.encrypt(JSON.stringify(bundle));
}
