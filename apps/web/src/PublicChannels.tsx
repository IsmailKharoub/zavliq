import { useEffect, useState, type FormEvent } from "react";
import type { MatrixClient } from "matrix-js-sdk";
import { Dialog } from "./ConversationControls";

export function PublicChannels({
  client,
  close,
  joined,
}: {
  client: MatrixClient;
  close: () => void;
  joined: (id: string) => void;
}) {
  const [channels, setChannels] = useState<
      { room_id: string; name?: string; num_joined_members: number }[]
    >([]),
    [id, setId] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [loaded, setLoaded] = useState(false);
  useEffect(() => {
    client
      .publicRooms({ limit: 100 })
      .then((r) => {
        setChannels(r.chunk);
        setLoaded(true);
      })
      .catch((e) => setError(e.message));
  }, [client]);
  const join = async (roomId: string) => {
    setBusy(true);
    setError("");
    try {
      if (!channels.some(c => c.room_id === roomId.trim())) throw Error("Choose a channel listed in the public directory. Private invitations belong in Requests.");
      const room = await client.joinRoom(roomId.trim());
      joined(room.roomId);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not join this channel.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      title="Join a public channel"
      close={() => {
        if (!busy) close();
      }}
    >
      <p>
        Subscribe to a public broadcast. Its publishers can post; subscribers
        can read. Public channels are unencrypted.
      </p>
      <form
        className="inline-form"
        onSubmit={(e: FormEvent) => {
          e.preventDefault();
          void join(id);
        }}
      >
        <label>
          Channel ID
          <input
            required
            placeholder="ID of a listed public channel"
            value={id}
            onChange={(e) => setId(e.target.value)}
          />
        </label>
        <button className="button secondary" disabled={busy}>
          Join by ID
        </button>
      </form>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      <div className="member-list">
        {channels.map((c) => (
          <div className="member-row" key={c.room_id}>
            <div>
              <strong>{c.name || c.room_id}</strong>
              <code>{c.room_id}</code>
              <small>{c.num_joined_members} subscribers</small>
            </div>
            <button
              className="text-link"
              disabled={busy}
              onClick={() => void join(c.room_id)}
            >
              Join channel
            </button>
          </div>
        ))}
      </div>
      {loaded && !channels.length && (
        <p>
          No public channels are listed yet. You can create the first one from
          New conversation.
        </p>
      )}
    </Dialog>
  );
}
