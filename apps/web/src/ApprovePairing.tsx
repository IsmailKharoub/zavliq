import { useState, type FormEvent } from 'react';
import type { MatrixClient } from 'matrix-js-sdk';
import { Link2, Search, ShieldCheck } from 'lucide-react';
import { api } from './lib/session';

type PairingDetails = {
  pairing_id: string;
  user_id: string;
  device_display_name: string;
  expires_at: number;
  status: 'pending' | 'approved' | 'consumed' | 'expired';
  device_id?: string;
};

export function ApprovePairing({ client }: { client: MatrixClient }) {
  const [pairingId, setPairingId] = useState('');
  const [details, setDetails] = useState<PairingDetails>();
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [complete, setComplete] = useState(false);

  const inspect = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true); setError(''); setComplete(false); setDetails(undefined); setCode('');
    try {
      const result = await api<PairingDetails>(`/v1/pairings/${encodeURIComponent(pairingId.trim())}`, undefined, client.getAccessToken()!);
      if (result.user_id !== client.getUserId()) throw new Error('This request belongs to a different identity. Start pairing with this browser’s agent address.');
      setDetails(result);
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not inspect this pairing request.'); }
    finally { setBusy(false); }
  };
  const approve = async (event: FormEvent) => {
    event.preventDefault();
    if (!details || details.status !== 'pending') return;
    if (Date.now() >= details.expires_at) { setError('This request expired. Start a new request on the device you want to connect.'); return; }
    setBusy(true); setError('');
    try {
      const result = await api<PairingDetails>(`/v1/pairings/${encodeURIComponent(details.pairing_id)}/approve`, { confirmation_code: code }, client.getAccessToken()!);
      setDetails(result); setComplete(true); setCode('');
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not approve this pairing.'); }
    finally { setBusy(false); }
  };

  return <section className="crypto-devices" aria-labelledby="approve-pairing-title">
    <h3 id="approve-pairing-title"><Link2 size={18} aria-hidden="true" /> Connect another device</h3>
    <p>Start pairing in the native client, then inspect its request here. Approval adds a separate device to <strong>{client.getUserId()}</strong>.</p>
    <form className="inline-form" onSubmit={inspect}>
      <label>Pairing request ID<input value={pairingId} required pattern="[A-Za-z0-9_-]{43}" autoComplete="off" spellCheck={false} disabled={busy} onChange={event => { setPairingId(event.target.value); setDetails(undefined); setComplete(false); }} /></label>
      <button className="button secondary" disabled={busy || !pairingId.trim()}><Search size={16} aria-hidden="true" />Inspect request</button>
    </form>
    {details && <div className="crypto-device">
      <strong>{details.device_display_name}</strong>
      <p>Agent: <code>{details.user_id}</code><br />Expires {new Date(details.expires_at).toLocaleTimeString()}</p>
      {details.status === 'pending' && <form className="verify-device" onSubmit={approve} aria-busy={busy}>
        <p>Compare the six-digit code shown on the device you started. Approve only a device you control; it will gain access to this identity.</p>
        <label>Code from the new device<input inputMode="numeric" autoComplete="off" pattern="[0-9]{6}" minLength={6} maxLength={6} value={code} required disabled={busy} onChange={event => setCode(event.target.value.replace(/\D/g, '').slice(0, 6))} /></label>
        <button className="button secondary" disabled={busy || code.length !== 6}><ShieldCheck size={16} aria-hidden="true" />{busy ? 'Approving…' : 'Approve this device'}</button>
      </form>}
      {(complete || details.status === 'approved') && <p role="status">Approved. Finish connecting in the native client before the request expires, then verify its encryption fingerprint to share encrypted history.</p>}
      {details.status === 'consumed' && <p role="status">This device has finished pairing. Manage it in the device list above.</p>}
    </div>}
    {error && <p className="form-error" role="alert">{error}</p>}
  </section>;
}
