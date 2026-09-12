import { initAsync, OlmMachine } from '@matrix-org/matrix-sdk-crypto-wasm';

export const MAX_ROOM_KEY_FILE_BYTES = 20 * 1024 * 1024;
const BEGIN = '-----BEGIN MEGOLM SESSION DATA-----';
const END = '-----END MEGOLM SESSION DATA-----';
const encoder = new TextEncoder();

function checkSize(value: string) {
  if (encoder.encode(value).byteLength > MAX_ROOM_KEY_FILE_BYTES) {
    throw new Error('This room-key file exceeds the 20 MiB browser limit. Use the native client for larger backups.');
  }
}

/** Standard Matrix encrypted key-file format, implemented by Matrix's established Rust crypto library. */
export async function encryptRoomKeyFile(roomKeysJson: string, passphrase: string): Promise<string> {
  if (passphrase.trim().length < 12) throw new Error('Use a passphrase of at least 12 characters, preferably several random words.');
  checkSize(roomKeysJson);
  const parsed: unknown = JSON.parse(roomKeysJson);
  if (!Array.isArray(parsed)) throw new Error('The client did not provide a valid room-key export.');
  await initAsync();
  const encrypted = OlmMachine.encryptExportedRoomKeys(roomKeysJson, passphrase, 100_000);
  checkSize(encrypted);
  return encrypted;
}

export async function decryptRoomKeyFile(encrypted: string, passphrase: string): Promise<string> {
  if (!passphrase) throw new Error('Enter the passphrase used when this file was exported.');
  checkSize(encrypted);
  if (!encrypted.trim().startsWith(BEGIN) || !encrypted.trim().endsWith(END)) {
    throw new Error('Choose an encrypted Matrix room-key file. Native account recovery bundles use the native client.');
  }
  await initAsync();
  let plaintext: string;
  try {
    plaintext = OlmMachine.decryptExportedRoomKeys(encrypted, passphrase);
  } catch {
    throw new Error('The passphrase is incorrect or the room-key file is damaged. Nothing was imported.');
  }
  checkSize(plaintext);
  try {
    if (!Array.isArray(JSON.parse(plaintext))) throw new Error('Invalid key list');
  } catch {
    throw new Error('The decrypted file does not contain a Matrix room-key list. Nothing was imported.');
  }
  return plaintext;
}
