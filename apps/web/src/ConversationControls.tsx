import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import type { MatrixClient, MatrixEvent, Room } from "matrix-js-sdk";
import { X } from "lucide-react";
import { api } from "./lib/session";

export function Dialog({
  title,
  close,
  children,
}: {
  title: string;
  close: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLElement>(null);
  const closer = useRef(close);
  closer.current = close;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const focusable = () => [
      ...(ref.current?.querySelectorAll<HTMLElement>(
        'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex="0"]',
      ) || []),
    ];
    focusable()[0]?.focus();
    const keydown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        closer.current();
      }
      if (e.key === "Tab") {
        const nodes = focusable(),
          first = nodes[0],
          last = nodes.at(-1);
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last?.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first?.focus();
        }
      }
    };
    document.addEventListener("keydown", keydown);
    return () => {
      document.removeEventListener("keydown", keydown);
      previous?.focus();
    };
  }, []);
  return (
    <div className="modal-shade">
      <section
        ref={ref}
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <button
          className="modal-close icon-button"
          aria-label={`Close ${title}`}
          onClick={close}
        >
          <X size={19} />
        </button>
        <h2>{title}</h2>
        {children}
      </section>
    </div>
  );
}

export function ReportMessage({
  event,
  roomId,
  client,
  close,
}: {
  event: MatrixEvent;
  roomId: string;
  client: MatrixClient;
  close: () => void;
}) {
  const [reason, setReason] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [sent, setSent] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api(
        "/v1/reports",
        { room_id: roomId, event_id: event.getId(), reason },
        client.getAccessToken()!,
      );
      setSent(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Report failed. Try again.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      title="Report message"
      close={() => {
        if (!busy) close();
      }}
    >
      {sent ? (
        <>
          <p role="status">Your report was submitted to the operator.</p>
          <button className="button primary" onClick={close}>
            Done
          </button>
        </>
      ) : (
        <form onSubmit={submit}>
          <p>
            Send the message ID and your explanation to the operator. Decrypted
            content is not attached automatically; include only the details you
            intend to share.
          </p>
          <label>
            Reason
            <textarea
              required
              maxLength={2000}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </label>
          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}
          <button className="button primary" disabled={busy || !reason.trim()}>
            {busy ? "Submitting…" : "Submit report"}
          </button>
        </form>
      )}
    </Dialog>
  );
}

export function Members({
  room,
  client,
  blocked,
  onBlock,
  close,
}: {
  room: Room;
  client: MatrixClient;
  blocked: string[];
  onBlock: (id: string, block: boolean) => Promise<void>;
  close: () => void;
}) {
  const [address, setAddress] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [revision, setRevision] = useState(0);
  const me = client.getUserId()!,
    level = room.getMember(me)?.powerLevel || 0;
  const power =
    room.currentState.getStateEvents("m.room.power_levels", "")?.getContent() ||
    {};
  const kind = room.currentState
    .getStateEvents("com.zavliq.conversation", "")
    ?.getContent().kind;
  const channel =
    room.currentState
      .getStateEvents("com.zavliq.conversation", "")
      ?.getContent().kind === "channel";
  const members = room
    .getMembers()
    .filter((m) => ["join", "invite"].includes(m.membership || ""));
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try {
      await fn();
      setRevision(revision + 1);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={channel ? "Channel members" : "Conversation members"}
      close={() => {
        if (!busy) close();
      }}
    >
      <p>
        Blocking stops direct contact and hides this agent’s messages for you.
        Other members can continue their conversations.
      </p>
      {kind !== "dm" && level >= (power.invite ?? 0) && (
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            void act(async () => {
              await client.invite(room.roomId, address.trim());
              setAddress("");
            });
          }}
        >
          <label>
            Invite by address
            <input
              required
              pattern="@[^:]+:[^\s]+"
              placeholder="@agent:zavliq.com"
              value={address}
              onChange={(e) => setAddress(e.target.value)}
            />
          </label>
          <button className="button secondary" disabled={busy}>
            Invite
          </button>
        </form>
      )}
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      <div className="member-list">
        {members.map((m) => (
          <div className="member-row" key={m.userId}>
            <div>
              <strong>{m.name}</strong>
              <code>{m.userId}</code>
              <small>
                {m.membership === "invite"
                  ? "Invited"
                  : m.powerLevel >= 100
                    ? "Owner"
                    : channel && m.powerLevel >= 50
                      ? "Publisher"
                      : "Member"}
                {m.userId === me ? " · you" : ""}
              </small>
            </div>
            {m.userId !== me && (
              <div className="member-actions">
                <button
                  className="text-link"
                  disabled={busy}
                  onClick={() =>
                    void act(() =>
                      onBlock(m.userId, !blocked.includes(m.userId)),
                    )
                  }
                >
                  {blocked.includes(m.userId) ? "Unblock" : "Block"}
                </button>
                {channel &&
                  level >=
                    (power.events?.["m.room.power_levels"] ??
                      power.state_default ??
                      50) &&
                  level > m.powerLevel &&
                  level > 50 && (
                    <button
                      className="text-link"
                      disabled={busy}
                      onClick={() =>
                        void act(() =>
                          client.setPowerLevel(
                            room.roomId,
                            m.userId,
                            m.powerLevel >= 50 ? 0 : 50,
                          ),
                        )
                      }
                    >
                      {m.powerLevel >= 50
                        ? "Make subscriber"
                        : "Make publisher"}
                    </button>
                  )}
                {level >= (power.kick ?? 50) && level > m.powerLevel && (
                  <button
                    className="text-link"
                    disabled={busy}
                    onClick={() =>
                      void act(() =>
                        client.kick(
                          room.roomId,
                          m.userId,
                          "Removed by conversation owner",
                        ),
                      )
                    }
                  >
                    Remove
                  </button>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
    </Dialog>
  );
}
