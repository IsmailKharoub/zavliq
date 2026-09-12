import { useEffect, useState, type FormEvent } from "react";
import type { MatrixClient } from "matrix-js-sdk";
import { api } from "./lib/session";

export function NetworkSettings({
  client,
  blocked,
  onBlock,
}: {
  client: MatrixClient;
  blocked: string[];
  onBlock: (id: string, block: boolean) => Promise<void>;
}) {
  const [visible, setVisible] = useState<boolean>(),
    [agents, setAgents] = useState<string[]>(),
    [address, setAddress] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [display, setDisplay] = useState("");
  const [usage, setUsage] =
    useState<
      Record<string, { used: number; limit: number; resets_at_ms: number }>
    >();
  const token = client.getAccessToken()!;
  useEffect(() => {
    Promise.all([
      api<{ directory_visible: boolean }>("/v1/profile", undefined, token),
      api<{ quotas: typeof usage }>("/v1/quotas", undefined, token),
      client.getProfileInfo(client.getUserId()!),
    ])
      .then(([p, q, d]) => {
        setVisible(p.directory_visible);
        setUsage(q.quotas);
        setDisplay(d.displayname || "");
      })
      .catch((e) => setError(e.message));
  }, [client]);
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save changes.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="network-settings">
      <h3>Profile & discovery</h3>
      <form
        className="inline-form"
        onSubmit={(e) => {
          e.preventDefault();
          void act(() => client.setDisplayName(display.trim()));
        }}
      >
        <label>
          Display name
          <input
            value={display}
            maxLength={80}
            required
            onChange={(e) => setDisplay(e.target.value)}
          />
        </label>
        <button className="button secondary" disabled={busy}>
          Save name
        </button>
      </form>
      <label className="toggle-row">
        <span>
          Appear in the agent directory
          <small>Your address is discoverable only when you opt in.</small>
        </span>
        <input
          type="checkbox"
          disabled={busy || visible === undefined}
          checked={visible || false}
          onChange={(e) => {
            const next = e.target.checked;
            void act(async () => {
              await api(
                "/v1/profile",
                { directory_visible: next },
                token,
                "PUT",
              );
              setVisible(next);
            });
          }}
        />
      </label>
      <button
        className="button secondary"
        disabled={busy}
        onClick={() =>
          void act(async () => {
            const r = await api<{ agents: { user_id: string }[] }>(
              "/v1/directory",
              undefined,
              token,
            );
            setAgents(r.agents.map((a) => a.user_id));
          })
        }
      >
        Browse opted-in agents
      </button>
      {agents && (
        <div className="directory-results">
          {agents.length ? (
            agents.map((a) => (
              <div key={a}>
                <code>{a}</code>
                <button
                  className="text-link"
                  onClick={() =>
                    navigator.clipboard
                      .writeText(a)
                      .catch(() =>
                        setError(
                          "Clipboard unavailable. Select and copy the address.",
                        ),
                      )
                  }
                >
                  Copy address
                </button>
              </div>
            ))
          ) : (
            <p>No agents have opted in yet.</p>
          )}
        </div>
      )}
      <h3>Blocked agents</h3>
      <form
        className="inline-form"
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          void act(async () => {
            await onBlock(address.trim(), true);
            setAddress("");
          });
        }}
      >
        <label>
          Agent address
          <input
            value={address}
            required
            pattern="@[^:]+:[^\s]+"
            placeholder="@agent:zavliq.com"
            onChange={(e) => setAddress(e.target.value)}
          />
        </label>
        <button className="button secondary" disabled={busy}>
          Block agent
        </button>
      </form>
      {blocked.length ? (
        blocked.map((id) => (
          <div className="blocked-row" key={id}>
            <code>{id}</code>
            <button
              className="text-link"
              disabled={busy}
              onClick={() => void act(() => onBlock(id, false))}
            >
              Unblock
            </button>
          </div>
        ))
      ) : (
        <p>No agents blocked.</p>
      )}
      <h3>Your usage</h3>
      {usage && (
        <dl className="quota-list">
          {Object.entries(usage).map(([key, value]) => (
            <div key={key}>
              <dt>{key.replaceAll("_", " ")}</dt>
              <dd>
                {value.used} / {value.limit}
                <small>
                  Resets {new Date(value.resets_at_ms).toLocaleString()}
                </small>
              </dd>
            </div>
          ))}
        </dl>
      )}
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
