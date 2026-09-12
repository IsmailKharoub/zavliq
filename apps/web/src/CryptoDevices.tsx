import { useEffect, useState, type FormEvent } from "react";
import type { MatrixClient, Device } from "matrix-js-sdk";
import { Check, KeyRound, Search } from "lucide-react";

export function CryptoDevices({ client }: { client: MatrixClient }) {
  const [own, setOwn] = useState("");
  const [address, setAddress] = useState(client.getUserId() || "");
  const [devices, setDevices] = useState<Device[]>([]);
  const [verified, setVerified] = useState<Record<string, boolean>>({});
  const [fingerprints, setFingerprints] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    client
      .getCrypto()
      ?.getOwnDeviceKeys()
      .then((k) => setOwn(k.ed25519))
      .catch((e) => setError(e.message));
  }, [client]);
  const find = async (e?: FormEvent) => {
    e?.preventDefault();
    setBusy(true);
    setError("");
    try {
      const map = await client.getCrypto()!.getUserDeviceInfo([address], true);
      const items = [...(map.get(address)?.values() || [])];
      const trust = await Promise.all(
        items.map(
          async (d) =>
            [
              d.deviceId,
              !!(
                await client
                  .getCrypto()!
                  .getDeviceVerificationStatus(d.userId, d.deviceId)
              )?.localVerified,
            ] as const,
        ),
      );
      setVerified(Object.fromEntries(trust));
      setDevices(items);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load device keys.");
    } finally {
      setBusy(false);
    }
  };
  const verify = async (device: Device) => {
    setError("");
    const actual = device.getFingerprint();
    if (
      !actual ||
      fingerprints[device.deviceId]?.replace(/\s/g, "") !== actual
    ) {
      setError(
        "The independently obtained fingerprint does not match this device. Do not verify it.",
      );
      return;
    }
    setBusy(true);
    try {
      await client
        .getCrypto()!
        .setDeviceVerified(device.userId, device.deviceId, true);
      await find();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Device verification failed.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="crypto-devices">
      <h3>Encryption fingerprints</h3>
      <p>
        Encrypted messages are shared only with verified devices. Compare
        fingerprints through an independent, trusted channel before verifying a
        peer.
      </p>
      <label>
        This browser’s public fingerprint
        <code>{own || "Publishing device keys…"}</code>
      </label>
      <form className="inline-form" onSubmit={find}>
        <label>
          Agent address
          <input
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            required
            placeholder="@agent:zavliq.com"
          />
        </label>
        <button className="button secondary" disabled={busy}>
          <Search size={16} />
          Find devices
        </button>
      </form>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      {devices.map((d) => (
        <div className="crypto-device" key={d.deviceId}>
          <div className="device-row">
            <KeyRound size={19} />
            <div>
              <strong>{d.displayName || d.deviceId}</strong>
              <span>
                {d.userId} · {d.deviceId}
              </span>
            </div>
            {(verified[d.deviceId] || d.verified === 1) && (
              <span className="verified">
                <Check size={14} />
                Verified
              </span>
            )}
          </div>
          <code>{d.getFingerprint() || "Fingerprint unavailable"}</code>
          {!verified[d.deviceId] &&
            d.verified !== 1 &&
            d.deviceId !== client.getDeviceId() && (
              <div className="verify-device">
                <label>
                  Fingerprint from your trusted channel
                  <input
                    value={fingerprints[d.deviceId] || ""}
                    onChange={(e) =>
                      setFingerprints({
                        ...fingerprints,
                        [d.deviceId]: e.target.value,
                      })
                    }
                    spellCheck={false}
                    autoComplete="off"
                  />
                </label>
                <button
                  className="button secondary"
                  disabled={busy || !fingerprints[d.deviceId]}
                  onClick={() => verify(d)}
                >
                  Verify matching device
                </button>
              </div>
            )}
        </div>
      ))}
    </section>
  );
}
