import {
  api,
  comparePrivate,
  loadPrivate,
  randomSecret,
  savePrivate,
  saveSession,
  type Session,
} from "./session";

type VerifiedProof = { user_id: string; control_url: string; secret: string };
export async function verifiedEnrollmentProof(userId: string, controlOrigin: string): Promise<string | undefined> {
  const record = await loadPrivate<VerifiedProof>(`verified-enrollment:${controlOrigin}:${userId}`);
  return record?.user_id === userId && record.control_url === controlOrigin ? record.secret : undefined;
}
export async function enroll(
  handle: string,
  displayName: string,
): Promise<Session> {
  const origin = location.origin;
  const key = `zavliq-pending-${handle}`;
  let secret = await loadPrivate<string>(key);
  if (secret === undefined || secret === null) {
    const proposed = randomSecret();
    const reserved = await comparePrivate(key, value => value === undefined || value === null, { [key]: proposed });
    secret = reserved ? proposed : await loadPrivate<string>(key);
  }
  if (typeof secret !== "string" || !/^[A-Za-z0-9_-]{43,128}$/.test(secret))
    throw Error("The saved enrollment proof cannot be read. Preserve this browser data and use an existing device or recovery file.");
  const session = await api<Session>("/v1/agents", {
    handle,
    display_name: displayName || handle,
    device_display_name: "Zavliq browser",
    registration_secret: secret,
  });
  if (typeof session.user_id !== "string" || !session.user_id.startsWith(`@${handle}:`)
      || session.user_id.length <= handle.length + 2
      || [session.device_id, session.access_token, session.homeserver].some(value => typeof value !== "string" || !value))
    throw Error("The service returned an unexpected identity. Nothing was activated.");
  await savePrivate(`verified-enrollment:${origin}:${session.user_id}`, { user_id: session.user_id, control_url: origin, secret });
  await saveSession(session);
  return session;
}
