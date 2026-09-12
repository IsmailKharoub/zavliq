import { useEffect, useRef, useState, type FormEvent } from "react";
import { ArrowLeft, Copy, Link2, RefreshCw, Shield } from "lucide-react";
import {
  api,
  comparePrivate,
  loadPrivate,
  savePrivate,
  type Session,
} from "./lib/session";
import { finishPairing, type PendingPairing as Pairing } from "./lib/pairing";
export default function PairingForm({
  onSession,
  back,
}: {
  onSession: (s: Session) => void;
  back: () => void;
}) {
  const [address, setAddress] = useState("");
  const [pending, setPending] = useState<Pairing>();
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState(false);
  const working = useRef(true);
  const generation = useRef(0);
  useEffect(() => {
    let live = true;
    loadPrivate<Pairing>("pending-pairing")
      .then((p) => {
        if (p && live) setPending(p);
      })
      .catch(() => { if (live) setError("Could not read this browser’s saved pairing. Reload to retry."); })
      .finally(() => { if (live) { working.current = false; setBusy(false); } });
    return () => { live = false; };
  }, []);
  useEffect(() => {
    if (!pending) return;
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    let failures = 0;
    const version = generation.current;
    const active = () => live && version === generation.current;
    const finish = async (pairing: Pairing, credentials: Session) => {
      if (!active() || working.current) return;
      working.current = true;
      setBusy(true);
      try {
        const session = await finishPairing(pairing, credentials);
        if (active()) onSession(session);
      } finally {
        working.current = false;
        if (active()) setBusy(false);
      }
    };
    const poll = async () => {
      if (!active()) return;
      try {
        const saved = await loadPrivate<Pairing>("pending-pairing");
        if (!active()) return;
        if (saved?.pairing_id !== pending.pairing_id || saved.pairing_secret !== pending.pairing_secret) {
          setError("This request was replaced in another tab. Reload to continue the current request.");
          return;
        }
        if (saved?.credentials) {
          await finish(saved, saved.credentials);
          return;
        }
        if (Date.now() > pending.expires_at) {
          setError("This pairing expired. Start a new request.");
          return;
        }
        const result = await api<{
          status: string;
          credentials?: Session;
          retry_after_ms?: number;
        }>(`/v1/pairings/${pending.pairing_id}/poll`, {
          pairing_secret: pending.pairing_secret,
        });
        if (!active()) return;
        if (result.status === "approved" && result.credentials) {
          await finish(pending, result.credentials);
          return;
        }
        failures = 0;
        timer = setTimeout(poll, Math.max(2000, result.retry_after_ms || 2000));
      } catch (e) {
        if (!active()) return;
        failures++;
        setError(
          e instanceof Error
            ? e.message
            : "Pairing connection interrupted. Retrying.",
        );
        timer = setTimeout(poll, Math.min(15000, 2000 * 2 ** failures));
      }
    };
    poll();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [pending, onSession]);
  const start = async (e: FormEvent) => {
    e.preventDefault();
    if (working.current) return;
    working.current = true;
    generation.current++;
    setBusy(true);
    setError("");
    try {
      const p = await api<Omit<Pairing, "user_id">>("/v1/pairings", {
        user_id: address,
        device_display_name: "Zavliq browser",
      });
      const value = { ...p, user_id: address };
      await savePrivate("pending-pairing", value);
      setPending(value);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start pairing.");
    } finally {
      working.current = false;
      setBusy(false);
    }
  };
  const command = pending
    ? `zavliq call pairing_approve --params '${JSON.stringify({ pairing_id: pending.pairing_id, confirmation_code: pending.confirmation_code })}'`
    : "";
  return (
    <>
      <button className="text-link" disabled={busy} onClick={() => { if (!working.current) { generation.current++; back(); } }}>
        <ArrowLeft size={16} />
        Create a new identity instead
      </button>
      <span className="step-counter">PAIR AN EXISTING AGENT</span>
      <h2>Your inbox, on this device.</h2>
      <p>
        Approve this browser from an existing agent runtime. Your identity stays
        the same; this browser gets its own device keys.
      </p>
      {pending ? (
        <div className="pairing-pending">
          <div className="pair-code" aria-label="Pairing confirmation code">
            {pending.confirmation_code}
          </div>
          <p>
            Confirm this code from <strong>{pending.user_id}</strong>. The
            request expires in five minutes.
          </p>
          <div className="inline-code">
            <code>{command}</code>
            <button
              className="copy"
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(command);
                  setCopied(true);
                } catch {
                  setError(
                    "Clipboard unavailable. Select the command to copy it.",
                  );
                }
              }}
            >
              <Copy size={15} />
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
          <p className="pairing-wait">
            <RefreshCw size={15} /> {busy ? "Securing this device…" : "Waiting for your agent’s approval"}
          </p>
          <button
            className="text-link"
            disabled={busy}
            onClick={async () => {
              if (working.current) return;
              working.current = true;
              setBusy(true);
              generation.current++;
              try {
                const cleared = await comparePrivate("pending-pairing", value => {
                  const saved = value as Pairing | null;
                  return saved?.pairing_id === pending.pairing_id && saved.pairing_secret === pending.pairing_secret && !saved.credentials;
                }, { "pending-pairing": null });
                if (!cleared) throw Error("This request has already changed or received credentials. Reload to finish the current pairing.");
                setPending(undefined);
                setError("");
              } catch (error) {
                setError(error instanceof Error ? error.message : "Could not replace this pairing.");
              } finally {
                working.current = false;
                setBusy(false);
              }
            }}
          >
            Start a new request
          </button>
        </div>
      ) : (
        <form onSubmit={start}>
          <label>
            Agent address
            <input
              value={address}
              disabled={busy}
              onChange={(e) => setAddress(e.target.value)}
              required
              placeholder="@your-agent:zavliq.com"
              autoComplete="off"
            />
          </label>
          <button className="button primary" disabled={busy}>
            {busy ? "Starting pairing…" : "Request connection"}
            <Link2 size={16} />
          </button>
        </form>
      )}
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      <div className="privacy-note">
        <Shield size={18} />
        <p>
          Pairing grants access to this identity’s conversations. Encrypted
          history requires separate device verification. Approve only a browser
          you control.
        </p>
      </div>
    </>
  );
}
