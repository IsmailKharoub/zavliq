/** Run passphrase derivation off the UI thread, with a hard bound on damaged/hostile files. */
export function processRoomKeyFile(action: 'export' | 'import', data: string, passphrase: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const worker = new Worker(new URL('./room-key-worker.ts', import.meta.url), { type: 'module' });
    const finish = () => { clearTimeout(timeout); worker.terminate(); };
    const timeout = setTimeout(() => {
      finish();
      reject(new Error('This file took too long to process locally. Check the file or use the native client. Nothing was imported.'));
    }, 20_000);
    worker.onmessage = (event: MessageEvent<{ ok: boolean; result?: string; error?: string }>) => {
      finish();
      if (event.data.ok && typeof event.data.result === 'string') resolve(event.data.result);
      else reject(new Error(event.data.error || 'Room-key processing failed.'));
    };
    worker.onerror = () => {
      finish();
      reject(new Error('Local encryption could not start. Reload the browser or use the native client.'));
    };
    worker.postMessage({ action, data, passphrase });
  });
}
