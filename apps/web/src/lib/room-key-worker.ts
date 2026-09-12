import { decryptRoomKeyFile, encryptRoomKeyFile } from './room-key-file';

self.onmessage = async (event: MessageEvent<{ action: 'export' | 'import'; data: string; passphrase: string }>) => {
  const { action, data, passphrase } = event.data;
  try {
    const result = action === 'export'
      ? await encryptRoomKeyFile(data, passphrase)
      : await decryptRoomKeyFile(data, passphrase);
    self.postMessage({ ok: true, result });
  } catch (error) {
    self.postMessage({ ok: false, error: error instanceof Error ? error.message : 'Room-key processing failed.' });
  }
};
