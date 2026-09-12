import type { MatrixClient } from "matrix-js-sdk";
import type { Session } from "./session";

export const MATRIX_HTTP_TIMEOUT_MS = 40_000;
type Connection = {
  token: string;
  cancelled: boolean;
  client?: MatrixClient;
  ready: Promise<MatrixClient>;
  release?: () => void;
  lockDone?: Promise<unknown>;
  closing?: Promise<void>;
  cleanup?: Promise<void>;
};
let current: Connection | undefined;
let closing: Promise<void> = Promise.resolve();

function checkActive(connection: Connection) {
  if (connection.cancelled) throw Error("This connection was closed. Connect the desired device again.");
}
function stopAndRelease(connection: Connection): Promise<void> {
  // initRustCrypto must have settled before stopClient closes its OlmMachine.
  // A failed close retains the lock until this document is closed.
  return connection.cleanup ??= (async () => {
    connection.client?.stopClient();
    connection.release?.();
    await connection.lockDone?.catch(() => {});
  })();
}
async function initialize(connection: Connection, session: Session, previous: Promise<void>): Promise<MatrixClient> {
  try {
    await previous;
    checkActive(connection);
    if (!navigator.locks)
      throw Error("This browser cannot lock device storage safely. Use a current browser or the native client.");
    await new Promise<void>((resolve, reject) => {
      const held = new Promise<void>((release) => { connection.release = release; });
      connection.lockDone = navigator.locks.request(
        `zavliq:${session.user_id}:${session.device_id}`,
        { ifAvailable: true },
        async (lock) => {
          if (!lock) { reject(Error("This identity is open in another browser tab. Close that tab, then reload this one.")); return; }
          resolve();
          await held;
        },
      );
      void connection.lockDone.catch(reject);
    });
    checkActive(connection);
    const sdk = await import("matrix-js-sdk");
    checkActive(connection);
    const client = sdk.createClient({
      baseUrl: location.origin,
      userId: session.user_id,
      deviceId: session.device_id,
      accessToken: session.access_token,
      localTimeoutMs: MATRIX_HTTP_TIMEOUT_MS,
    });
    connection.client = client;
    await client.initRustCrypto({ useIndexedDB: true, cryptoDatabasePrefix: `zavliq-${session.device_id}` });
    checkActive(connection);
    client.getCrypto()!.globalBlacklistUnverifiedDevices = true;
    await client.startClient({ initialSyncLimit: 30 });
    checkActive(connection);
    return client;
  } catch (error) {
    await stopAndRelease(connection);
    if (current === connection) current = undefined;
    throw error;
  }
}

export function connect(session: Session): Promise<MatrixClient> {
  if (current && !current.cancelled && current.token === session.access_token) return current.ready;
  if (current) void disconnect();
  const previous = closing;
  const connection = { token: session.access_token, cancelled: false } as Connection;
  current = connection;
  connection.ready = initialize(connection, session, previous);
  return connection.ready;
}

export function disconnect(): Promise<void> {
  const connection = current;
  if (!connection) return closing;
  connection.cancelled = true;
  current = undefined;
  if (!connection.closing) {
    connection.closing = (async () => {
      await connection.ready.catch(() => {});
      await stopAndRelease(connection);
    })();
  }
  closing = connection.closing;
  return closing;
}
if (import.meta.hot) import.meta.hot.dispose(() => { void disconnect(); });
