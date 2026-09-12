import { beforeAll, describe, expect, it } from 'vitest';
import { initAsync, OlmMachine, UserId, DeviceId, RoomId, EncryptionSettings } from '@matrix-org/matrix-sdk-crypto-wasm';
import { decryptRoomKeyFile, encryptRoomKeyFile, MAX_ROOM_KEY_FILE_BYTES } from './room-key-file';

let fixture: string;
const passphrase = 'several random words for a test';

describe('standard Matrix encrypted room-key files', () => {
  beforeAll(async () => {
    await initAsync();
    const machine = await OlmMachine.initialize(new UserId('@fixture:localhost'), new DeviceId('FIXTURE'));
    try {
      await machine.shareRoomKey(new RoomId('!test:localhost'), [], new EncryptionSettings());
      fixture = await machine.exportRoomKeys(() => true);
      expect(JSON.parse(fixture)).toHaveLength(1);
    } finally { machine.close(); }
  });
  it('round trips through the established crypto implementation without exposing plaintext', async () => {
    const encrypted = await encryptRoomKeyFile(fixture, passphrase);
    expect(encrypted).toContain('-----BEGIN MEGOLM SESSION DATA-----');
    expect(encrypted).not.toContain(JSON.parse(fixture)[0].session_key);
    expect(encrypted).not.toContain('!test:localhost');
    expect(encrypted).not.toContain(passphrase);
    const restored = await decryptRoomKeyFile(encrypted, passphrase);
    expect(JSON.parse(restored)).toEqual(JSON.parse(fixture));
    const fresh = await OlmMachine.initialize(new UserId('@fixture:localhost'), new DeviceId('RESTORED'));
    try {
      const result = await fresh.importExportedRoomKeys(restored, () => {});
      expect(result.importedCount).toBe(1);
      result.free();
      expect(JSON.parse(await fresh.exportRoomKeys(() => true))[0].session_id).toEqual(JSON.parse(fixture)[0].session_id);
    } finally { fresh.close(); }
  });
  it('wrong passphrases and ciphertext tampering fail before any import', async () => {
    const encrypted = await encryptRoomKeyFile(fixture, passphrase);
    await expect(decryptRoomKeyFile(encrypted, 'incorrect passphrase')).rejects.toThrow('incorrect or the room-key file is damaged');
    const lines = encrypted.split('\n');
    const index = lines.findIndex(line => line.length > 10 && !line.startsWith('-----'));
    const line = lines[index]!;
    lines[index] = line.slice(0, -2) + (line.at(-2) === 'A' ? 'B' : 'A') + line.slice(-1);
    await expect(decryptRoomKeyFile(lines.join('\n'), passphrase)).rejects.toThrow();
  });
  it('rejects accidental plaintext/account bundles, weak new passphrases and oversized files', async () => {
    await expect(decryptRoomKeyFile(fixture, passphrase)).rejects.toThrow('encrypted Matrix room-key file');
    await expect(encryptRoomKeyFile(fixture, 'short')).rejects.toThrow('at least 12');
    await expect(decryptRoomKeyFile('x'.repeat(MAX_ROOM_KEY_FILE_BYTES + 1), passphrase)).rejects.toThrow('20 MiB');
  });
});
