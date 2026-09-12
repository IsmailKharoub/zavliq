import { api, comparePrivate, loadPrivate, loadSession, type Session } from "./session";
export type PendingPairing = {
  pairing_id: string;
  pairing_secret: string;
  confirmation_code: string;
  expires_at: number;
  user_id: string;
  credentials?: Session;
};
const sameSession = (a: Session | undefined, b: Session) => !!a
  && a.user_id === b.user_id && a.device_id === b.device_id
  && a.access_token === b.access_token && a.homeserver === b.homeserver;
function samePairing(value: unknown, pairing: PendingPairing): value is PendingPairing {
  if (!value || typeof value !== "object") return false;
  const saved = value as PendingPairing;
  return saved.pairing_id === pairing.pairing_id && saved.pairing_secret === pairing.pairing_secret
    && saved.user_id === pairing.user_id;
}
async function alreadyFinished(credentials: Session): Promise<boolean> {
  const pending = await loadPrivate<PendingPairing>("pending-pairing");
  return !pending && sameSession(await loadSession(), credentials);
}
function changed(): Error {
  return Error("This pairing was cancelled or replaced. Continue the current request; check your device list for any unused approved browser device.");
}
export async function finishPairing(pairing: PendingPairing, credentials: Session): Promise<Session> {
  if (credentials.user_id !== pairing.user_id) throw Error("Pairing returned a different identity. Keep your existing device and start a new request.");
  const staged = await comparePrivate("pending-pairing", value => samePairing(value, pairing)
    && (!value.credentials || sameSession(value.credentials, credentials)),
    { "pending-pairing": { ...pairing, credentials } });
  if (!staged) {
    if (await alreadyFinished(credentials)) return credentials;
    throw changed();
  }
  // Acknowledgement may succeed remotely even if its response is lost. Keep the
  // staged credentials private and inactive until the idempotent ACK succeeds.
  const result = await api<{ completed: boolean }>(`/v1/pairings/${pairing.pairing_id}/ack`, {
    pairing_secret: pairing.pairing_secret,
  });
  if (result.completed !== true) throw Error("The device acknowledgement was not confirmed. Retry this pairing.");
  const committed = await comparePrivate("pending-pairing", value => samePairing(value, pairing)
    && sameSession(value.credentials, credentials), { current: credentials, "pending-pairing": null });
  if (!committed && !await alreadyFinished(credentials)) throw changed();
  return credentials;
}
export async function loadReadySession(): Promise<Session | undefined> {
  const pairing = await loadPrivate<PendingPairing>("pending-pairing");
  if (pairing?.credentials) return finishPairing(pairing, pairing.credentials);
  return loadSession();
}
