import { useRef, useState, type FormEvent } from 'react';
import type { MatrixClient } from 'matrix-js-sdk';
import { Download, FolderKey, Upload } from 'lucide-react';
import { processRoomKeyFile } from './lib/room-key-job';

const MAX_FILE_BYTES = 20 * 1024 * 1024;

export function Recovery({ client }: { client: MatrixClient }) {
  const [mode, setMode] = useState<'export' | 'import'>('export');
  const [passphrase, setPassphrase] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState('');
  const [error, setError] = useState('');
  const fileInput = useRef<HTMLInputElement>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError('');
    setStatus('');
    const crypto = client.getCrypto();
    if (!crypto) { setError('Encryption is still starting. Try again once this device is ready.'); return; }
    if (mode === 'export' && passphrase !== confirmation) { setError('The two passphrases do not match.'); return; }
    if (mode === 'import' && !file) { setError('Select your encrypted Matrix room-key file.'); return; }
    if (mode === 'import' && file && file.size > MAX_FILE_BYTES) { setError('Choose a file smaller than 20 MiB, or use the native client.'); return; }
    setBusy(true);
    try {
      if (mode === 'export') {
        const keys = await crypto.exportRoomKeysAsJson();
        const count = (JSON.parse(keys) as unknown[]).length;
        if (!count) { setStatus('This browser has no encrypted room keys to export yet.'); return; }
        setStatus('Encrypting your room keys locally…');
        const encrypted = await processRoomKeyFile('export', keys, passphrase);
        const url = URL.createObjectURL(new Blob([encrypted], { type: 'text/plain;charset=utf-8' }));
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = `zavliq-room-keys-${new Date().toISOString().slice(0, 10)}.txt`;
        document.body.append(anchor);
        anchor.click();
        anchor.remove();
        setTimeout(() => URL.revokeObjectURL(url), 30_000);
        setStatus(`Encrypted file prepared with ${count} room ${count === 1 ? 'key' : 'keys'}. Keep the file and its passphrase in separate safe places.`);
      } else {
        setStatus('Decrypting your file locally…');
        const plaintext = await processRoomKeyFile('import', await file!.text(), passphrase);
        const count = (JSON.parse(plaintext) as unknown[]).length;
        setStatus('Saving room keys to this browser…');
        await crypto.importRoomKeysAsJson(plaintext);
        setStatus(`Imported ${count} room ${count === 1 ? 'key' : 'keys'} into this browser. Reopen an encrypted conversation to read its available history.`);
        setFile(null);
        if (fileInput.current) fileInput.current.value = '';
      }
    } catch (reason) {
      setStatus('');
      setError(reason instanceof Error ? reason.message : 'Room-key recovery could not be completed.');
    } finally {
      setPassphrase('');
      setConfirmation('');
      setBusy(false);
    }
  };

  return (
    <section className="crypto-devices" aria-labelledby="room-key-recovery-heading">
      <h3 id="room-key-recovery-heading"><FolderKey size={18} aria-hidden="true" /> Encrypted room-key backup</h3>
      <p>Save the keys this browser needs to read encrypted conversation history. The file is encrypted locally; your passphrase and unencrypted keys stay on this device.</p>
      <p>This backs up room keys only. Use the account recovery section above to back up this identity, or export a full recovery bundle from its original native client.</p>
      <div className="inline-form" role="group" aria-label="Room-key backup action">
        <button type="button" className={`button ${mode === 'export' ? 'primary' : 'secondary'}`} disabled={busy} aria-pressed={mode === 'export'} onClick={() => { setMode('export'); setError(''); setStatus(''); setPassphrase(''); setConfirmation(''); }}>Export room keys</button>
        <button type="button" className={`button ${mode === 'import' ? 'primary' : 'secondary'}`} disabled={busy} aria-pressed={mode === 'import'} onClick={() => { setMode('import'); setError(''); setStatus(''); setPassphrase(''); setConfirmation(''); }}>Import room keys</button>
      </div>
      <form className="verify-device" onSubmit={submit} aria-busy={busy}>
        {mode === 'import' && <label>Encrypted Matrix room-key file<input ref={fileInput} type="file" accept=".txt,.keys,.matrix,text/plain" disabled={busy} required onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></label>}
        <label>{mode === 'export' ? 'New backup passphrase' : 'File passphrase'}<input type="password" autoComplete={mode === 'export' ? 'new-password' : 'off'} spellCheck={false} minLength={mode === 'export' ? 12 : 1} maxLength={1024} required value={passphrase} disabled={busy} onChange={(event) => setPassphrase(event.target.value)} aria-describedby="room-key-passphrase-hint" /></label>
        <small id="room-key-passphrase-hint">{mode === 'export' ? 'Use at least 12 characters, preferably several random words. Zavliq cannot recover this passphrase.' : 'Use the exact passphrase from the encrypted file. This action does not replace your account or device.'}</small>
        {mode === 'export' && <label>Confirm backup passphrase<input type="password" autoComplete="new-password" required value={confirmation} disabled={busy} onChange={(event) => setConfirmation(event.target.value)} /></label>}
        <button type="submit" className="button secondary" disabled={busy}>{mode === 'export' ? <Download size={16} aria-hidden="true" /> : <Upload size={16} aria-hidden="true" />}{busy ? 'Working locally…' : mode === 'export' ? 'Download encrypted file' : 'Import into this browser'}</button>
      </form>
      {error && <p className="form-error" role="alert">{error}</p>}
      {status && <p role="status" aria-live="polite">{status}</p>}
    </section>
  );
}
