//! Isolated regressions for the SDK/inbox commit boundary; no live service.
use super::*;
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicUsize, Ordering},
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
    sync::Notify,
};

const ROOM: &str = "!cedar:localhost";
const EVENT: &str = "$durable-cancellation-message";

fn fixture_event(encrypted: bool) -> Value {
    let content = if encrypted {
        json!({"algorithm":"m.megolm.v1.aes-sha2","ciphertext":"AwgAEg","sender_key":"synthetic-sender","device_id":"LINDEN","session_id":"synthetic-session"})
    } else {
        json!({"msgtype":"m.text","body":"Keep this message across cancellation."})
    };
    json!({"room_id":ROOM,"event_id":EVENT,"type":if encrypted {"m.room.encrypted"} else {"m.room.message"},"sender":"@linden:localhost","origin_server_ts":4,"content":content})
}

fn sync_response(token: &str, events: Vec<Value>, prev: Option<&str>, encrypted: bool) -> Value {
    let mut state = vec![
        json!({"event_id":"$creation","type":"m.room.create","sender":"@cedar:localhost","state_key":"","origin_server_ts":1,"content":{"creator":"@cedar:localhost","room_version":"10"}}),
        json!({"event_id":"$cedar","type":"m.room.member","sender":"@cedar:localhost","state_key":"@cedar:localhost","origin_server_ts":2,"content":{"membership":"join"}}),
        json!({"event_id":"$linden","type":"m.room.member","sender":"@linden:localhost","state_key":"@linden:localhost","origin_server_ts":3,"content":{"membership":"join"}}),
    ];
    if encrypted {
        state.push(json!({"event_id":"$encryption","type":"m.room.encryption","sender":"@cedar:localhost","state_key":"","origin_server_ts":3,"content":{"algorithm":"m.megolm.v1.aes-sha2"}}));
    }
    json!({"next_batch":token,"rooms":{"join":{ROOM:{"state":{"events":state},"timeline":{"limited":prev.is_some(),"prev_batch":prev,"events":events},"summary":{"m.joined_member_count":2}}}},"device_one_time_keys_count":{"signed_curve25519":50}})
}

async fn http_fixture(
    respond: impl Fn(&str, &Value) -> Value + Send + Sync + 'static,
) -> (String, tokio::task::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let origin = format!("http://{}", listener.local_addr().unwrap());
    let respond = Arc::new(respond);
    let task = tokio::spawn(async move {
        loop {
            let (mut socket, _) = listener.accept().await.unwrap();
            let respond = respond.clone();
            tokio::spawn(async move {
                let mut bytes = Vec::new();
                let header_end = loop {
                    let mut buffer = [0; 8192];
                    let count = socket.read(&mut buffer).await.unwrap();
                    if count == 0 {
                        return;
                    }
                    bytes.extend_from_slice(&buffer[..count]);
                    if let Some(end) = bytes.windows(4).position(|v| v == b"\r\n\r\n") {
                        break end + 4;
                    }
                    assert!(bytes.len() < 1024 * 1024);
                };
                let header = String::from_utf8_lossy(&bytes[..header_end]).into_owned();
                let length = header
                    .lines()
                    .find_map(|line| {
                        let (key, value) = line.split_once(':')?;
                        key.eq_ignore_ascii_case("content-length")
                            .then(|| value.trim().parse::<usize>().unwrap())
                    })
                    .unwrap_or(0);
                while bytes.len() < header_end + length {
                    let mut buffer = [0; 8192];
                    let count = socket.read(&mut buffer).await.unwrap();
                    if count == 0 {
                        return;
                    }
                    bytes.extend_from_slice(&buffer[..count]);
                }
                let path = header.split_whitespace().nth(1).unwrap();
                let payload = serde_json::from_slice(&bytes[header_end..header_end + length])
                    .unwrap_or(Value::Null);
                let result = if path.contains("/versions") {
                    json!({"versions":["v1.11"]})
                } else if path == "/v1/blocks" {
                    json!({"blocked_user_ids":[]})
                } else {
                    respond(path, &payload)
                };
                let body = serde_json::to_vec(&result).unwrap();
                let header = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                );
                if socket.write_all(header.as_bytes()).await.is_ok() {
                    let _ = socket.write_all(&body).await;
                }
            });
        }
    });
    (origin, task)
}

async fn fixture_server(
    encrypted: bool,
    limited: bool,
    older_catchup: bool,
) -> (String, tokio::task::JoinHandle<()>) {
    let calls = AtomicUsize::new(0);
    http_fixture(move |path, _| {
        if path.contains("/sync?") || path.ends_with("/sync") {
            let call = calls.fetch_add(1, Ordering::SeqCst);
            let advanced = call > 0 && limited && !(older_catchup && call == 2);
            let event = if advanced {
                json!({"room_id":ROOM,"event_id":"$later-message","type":"m.room.message","sender":"@linden:localhost","origin_server_ts":5,"content":{"msgtype":"m.text","body":"Later message"}})
            } else { fixture_event(encrypted) };
            sync_response(if advanced { "batch-2" } else { "batch-1" }, vec![event], advanced.then_some("before-message"), encrypted)
        } else if path.contains("/messages") {
            json!({"start":"before-message","chunk":[fixture_event(encrypted)]})
        } else if path.contains("/event/") { fixture_event(encrypted)
        } else if path.contains("/keys/upload") { json!({"one_time_key_counts":{"signed_curve25519":50}})
        } else if path.contains("/keys/query") { json!({"device_keys":{},"failures":{}})
        } else { json!({}) }
    }).await
}

fn fixture_runtime(directory: &std::path::Path, origin: &str) -> Runtime {
    let runtime = Runtime::new(directory.to_owned(), origin.to_owned()).unwrap();
    runtime
        .store
        .save_identity(&Identity {
            handle: "cedar".into(),
            control_url: origin.into(),
            registration_secret: "synthetic-proof".into(),
            store_passphrase: "synthetic-private-store".into(),
            recovery_id: None,
            session: Some(Session {
                user_id: "@cedar:localhost".into(),
                device_id: "CEDAR".into(),
                access_token: "synthetic-token".into(),
                homeserver: origin.into(),
            }),
        })
        .unwrap();
    runtime
}

async fn cancellation_case(restart: bool, encrypted: bool, limited: bool, older_catchup: bool) {
    use matrix_sdk::ruma::events::AnySyncTimelineEvent;
    let (origin, server) = fixture_server(encrypted, limited, older_catchup).await;
    let directory = tempfile::tempdir().unwrap();
    let mut runtime = Runtime::new(directory.path().to_owned(), origin.clone()).unwrap();
    runtime
        .store
        .save_identity(&Identity {
            handle: "cedar".into(),
            control_url: origin.clone(),
            registration_secret: "synthetic-proof".into(),
            store_passphrase: "synthetic-private-store".into(),
            session: Some(Session {
                user_id: "@cedar:localhost".into(),
                device_id: "CEDAR".into(),
                access_token: "synthetic-token".into(),
                homeserver: origin.clone(),
            }),
            recovery_id: None,
        })
        .unwrap();
    let client = runtime.client().await.unwrap();
    let entered = Arc::new(Notify::new());
    let unblock = Arc::new(Notify::new());
    let handler = client.add_event_handler({
        let entered = entered.clone();
        let unblock = unblock.clone();
        move |_event: AnySyncTimelineEvent| {
            let entered = entered.clone();
            let unblock = unblock.clone();
            async move {
                entered.notify_one();
                unblock.notified().await;
            }
        }
    });
    {
        // SDK state is durable before event handlers run, while Zavliq has not
        // yet received the SyncResponse. This is the RPC cancellation window.
        let sync = runtime.sync(0);
        tokio::pin!(sync);
        tokio::select! {
            _ = entered.notified() => {},
            result = &mut sync => panic!("sync completed before cancellation: {result:?}"),
            _ = tokio::time::sleep(Duration::from_secs(30)) => panic!("fixture sync never reached event handler"),
        }
    }
    client.remove_event_handler(handler);
    unblock.notify_waiters();
    assert_eq!(runtime.store.cursor().unwrap(), None);
    assert_eq!(
        runtime.store.inbox(0, 100, None, true, false).unwrap()["items"]
            .as_array()
            .unwrap()
            .len(),
        0
    );
    assert_eq!(
        client
            .state_store()
            .get_kv_data(StateStoreDataKey::SyncToken)
            .await
            .unwrap()
            .and_then(|value| value.into_sync_token())
            .as_deref(),
        Some("batch-1")
    );
    if restart {
        drop(client);
        drop(runtime);
        runtime = Runtime::new(directory.path().to_owned(), origin).unwrap();
    }
    runtime.sync(0).await.unwrap();
    let inbox = runtime.store.inbox(0, 100, None, true, false).unwrap();
    assert_eq!(
        runtime.store.cursor().unwrap().as_deref(),
        Some(if limited { "batch-2" } else { "batch-1" })
    );
    assert_eq!(
        inbox["items"].as_array().unwrap().len(),
        if limited { 2 } else { 1 },
        "SDK-persisted events must survive an interrupted inbox commit"
    );
    let recovered = inbox["items"]
        .as_array()
        .unwrap()
        .iter()
        .find(|event| event["event_id"] == EVENT)
        .unwrap();
    if encrypted {
        assert_eq!(recovered["content"]["unavailable"], "keys_missing");
    }
    assert!(inbox["history_gap_rooms"].as_array().unwrap().is_empty());
    runtime.sync(0).await.unwrap();
    assert_eq!(
        runtime.store.inbox(0, 100, None, true, false).unwrap()["items"]
            .as_array()
            .unwrap()
            .len(),
        if limited { 2 } else { 1 }
    );
    server.abort();
}

#[tokio::test]
async fn cancelled_sdk_sync_replays_into_durable_inbox() {
    cancellation_case(false, false, false, false).await;
}

#[tokio::test]
async fn interrupted_sdk_commit_survives_process_restart() {
    cancellation_case(true, false, false, false).await;
}

#[tokio::test]
async fn interrupted_encrypted_event_remains_available_for_later_keys() {
    cancellation_case(true, true, false, false).await;
}

#[tokio::test]
async fn interrupted_sync_backfills_a_limited_recovery_response() {
    cancellation_case(true, false, true, false).await;
}

#[tokio::test]
async fn older_suppressed_sdk_catchup_does_not_regress_recovered_cursor() {
    cancellation_case(true, false, true, true).await;
}

#[tokio::test]
async fn newer_gap_survives_older_limited_catchup_and_restart_with_overlap() {
    let requests = Arc::new(Mutex::new(Vec::<String>::new()));
    let calls = AtomicUsize::new(0);
    let numbered = |number: usize| {
        let mut event = fixture_event(false);
        event["event_id"] = json!(format!("$message-{number}"));
        event["origin_server_ts"] = json!(number + 10);
        event
    };
    let (origin, server) = http_fixture({
        let requests = requests.clone();
        move |path, _| {
            if path.contains("/sync?") {
                match calls.fetch_add(1, Ordering::SeqCst) {
                    0 => sync_response("batch-1", vec![numbered(150)], None, false),
                    1 => sync_response("batch-3", vec![numbered(221)], Some("newer-221"), false),
                    2 => sync_response("batch-2", vec![numbered(151)], Some("older-151"), false),
                    _ => sync_response("batch-3", vec![], None, false),
                }
            } else if path.contains("/messages") {
                requests.lock().unwrap().push(path.into());
                let (range, end) = if path.contains("from=newer-221") {
                    (121..221, Some("before-121"))
                } else if path.contains("from=before-121") {
                    (21..121, Some("before-21"))
                } else if path.contains("from=before-21") {
                    (1..21, None)
                } else { panic!("gap replaced or lost: {path}"); };
                json!({"start":"page-start","end":end,"chunk":range.rev().map(numbered).collect::<Vec<_>>()})
            } else if path.contains("/keys/upload") {
                json!({"one_time_key_counts":{"signed_curve25519":50}})
            } else if path.contains("/keys/query") { json!({"device_keys":{},"failures":{}})
            } else { json!({}) }
        }
    }).await;
    let directory = tempfile::tempdir().unwrap();
    let mut runtime = fixture_runtime(directory.path(), &origin);
    // Exact state at the interrupted boundary: SDK committed, inbox did not.
    let client = runtime.client().await.unwrap();
    client
        .sync_once(SyncSettings::default().token(SyncToken::NoToken))
        .await
        .unwrap();
    runtime.sync(0).await.unwrap();
    assert_eq!(runtime.store.cursor().unwrap().as_deref(), Some("batch-3"));
    assert_eq!(
        runtime
            .store
            .db
            .query_row(
                "SELECT prev_batch FROM gaps WHERE room_id=?",
                [ROOM],
                |row| row.get::<_, String>(0)
            )
            .unwrap(),
        "before-121"
    );
    assert!(runtime.store.event_content("$message-151").is_ok());
    assert!(runtime.store.event_content("$message-1").is_err());
    // Resume from persisted page progress after the overlapping first page.
    drop(client);
    drop(runtime);
    let mut runtime = Runtime::new(directory.path().to_owned(), origin).unwrap();
    runtime.sync(0).await.unwrap();
    assert!(
        !runtime.store.inbox(0, 1, None, true, false).unwrap()["history_gap_rooms"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    runtime.sync(0).await.unwrap();
    assert_eq!(
        runtime
            .store
            .db
            .query_row("SELECT count(*) FROM events", [], |row| row
                .get::<_, usize>(0))
            .unwrap(),
        221
    );
    assert!(
        runtime.store.inbox(0, 1, None, true, false).unwrap()["history_gap_rooms"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    assert_eq!(requests.lock().unwrap().len(), 3);
    server.abort();
}

#[derive(Default)]
struct QueuedKeyFixture {
    upload: Value,
    ciphertext: Option<Value>,
    queued_key: Option<Value>,
    sync_requests: Vec<String>,
    acknowledged_keys: usize,
}

#[tokio::test]
async fn interrupted_raw_recovery_preserves_real_queued_room_key_across_restart() {
    use matrix_sdk::ruma::{device_id, room_id, serde::Raw, user_id};
    use matrix_sdk_crypto::{
        Account, DeviceData, EncryptionSettings, olm::OutboundGroupSession, types::DeviceKeys,
    };
    let state = Arc::new(Mutex::new(QueuedKeyFixture::default()));
    let (origin, server) = http_fixture({
        let state = state.clone();
        move |path, body| {
            let mut state = state.lock().unwrap();
            if path.contains("/keys/upload") {
                if state.upload.is_null() {
                    state.upload = json!({});
                }
                for name in ["device_keys", "one_time_keys"] {
                    if body[name].is_object() && !body[name].as_object().unwrap().is_empty() {
                        state.upload[name] = body[name].clone();
                    }
                }
                json!({"one_time_key_counts":{"signed_curve25519":50}})
            } else if path.contains("/keys/query") {
                json!({"device_keys":{},"failures":{}})
            } else if path.contains("/sync?") {
                state.sync_requests.push(path.into());
                if state.ciphertext.is_some()
                    && path.contains("since=batch-1")
                    && !path.contains("full_state=true")
                    && !path.contains("use_state_after=true")
                {
                    // A server-cached ordinary response predating the room
                    // key must never satisfy the recovery SDK catch-up.
                    return sync_response("batch-cached", vec![], None, true);
                }
                // Synapse acknowledges and removes queued to-device keys when
                // the supplied since token includes their stream position.
                if path.contains("since=batch-2") && state.queued_key.take().is_some() {
                    state.acknowledged_keys += 1;
                }
                let events = state.ciphertext.iter().cloned().collect::<Vec<_>>();
                let mut response = sync_response(
                    if events.is_empty() {
                        "batch-1"
                    } else {
                        "batch-2"
                    },
                    events,
                    None,
                    true,
                );
                response["to_device"] =
                    json!({"events":state.queued_key.iter().cloned().collect::<Vec<_>>()});
                response
            } else if path.contains("/event/") {
                state.ciphertext.clone().unwrap()
            } else {
                json!({})
            }
        }
    })
    .await;
    let directory = tempfile::tempdir().unwrap();
    let mut runtime = fixture_runtime(directory.path(), &origin);
    runtime.sync(0).await.unwrap();
    let upload = state.lock().unwrap().upload.clone();
    let keys: DeviceKeys = serde_json::from_value(upload["device_keys"].clone()).unwrap();
    let receiver = DeviceData::try_from(&keys).unwrap();
    let sender = Account::with_device_id(user_id!("@linden:localhost"), device_id!("LINDEN"));
    let mut session = sender
        .create_outbound_session(
            &receiver,
            &serde_json::from_value(upload["one_time_keys"].clone()).unwrap(),
            sender.device_keys(),
        )
        .unwrap();
    let group = OutboundGroupSession::new(
        device_id!("LINDEN").to_owned(),
        Arc::new(sender.static_data().identity_keys()),
        room_id!("!cedar:localhost"),
        EncryptionSettings::default(),
    )
    .unwrap();
    let room_key = json!({"algorithm":"m.megolm.v1.aes-sha2","room_id":ROOM,"session_id":group.session_id(),"session_key":group.session_key().await.to_base64()});
    let olm = session
        .encrypt(&receiver, "m.room_key", room_key, None)
        .await
        .unwrap();
    let cleartext = Raw::from_json_string(
        json!({"msgtype":"m.text","body":"The queued room key survived recovery interruption."})
            .to_string(),
    )
    .unwrap();
    let encrypted = group.encrypt("m.room.message", &cleartext).await;
    let ciphertext = json!({"room_id":ROOM,"event_id":"$queued-key-message","sender":"@linden:localhost","origin_server_ts":15,"type":"m.room.encrypted","content":encrypted.content});
    {
        let mut state = state.lock().unwrap();
        state.ciphertext = Some(ciphertext.clone());
        state.queued_key =
            Some(json!({"type":"m.room.encrypted","sender":"@linden:localhost","content":olm}));
    }
    // Inject a process interruption immediately after the actual atomic raw
    // recovery commit, before the SDK can process that response's to-device key.
    let raw = runtime
        .matrix(
            reqwest::Method::GET,
            "/_matrix/client/v3/sync?since=batch-1&timeout=0&use_state_after=true",
            None,
        )
        .await
        .unwrap();
    assert_eq!(raw["to_device"]["events"].as_array().unwrap().len(), 1);
    runtime
        .store
        .commit_recovery(
            "batch-2",
            &[(ROOM.into(), ciphertext)],
            &[],
            Some("batch-1"),
        )
        .unwrap();
    assert_eq!(
        runtime.store.recovery_phase().unwrap(),
        Some(Some("batch-1".into()))
    );
    let first_after_restart = state.lock().unwrap().sync_requests.len();
    drop(runtime);
    let mut runtime = Runtime::new(directory.path().to_owned(), origin).unwrap();
    runtime.sync(0).await.unwrap();
    {
        let state = state.lock().unwrap();
        assert!(
            state.sync_requests[first_after_restart].contains("since=batch-1"),
            "SDK must process the queued key before any request acknowledges batch-2"
        );
        assert!(state.sync_requests[first_after_restart].contains("full_state=true"));
        assert_eq!(state.acknowledged_keys, 0);
    }
    assert_eq!(
        runtime
            .store
            .event_content("$queued-key-message")
            .unwrap()
            .1["body"],
        "The queued room key survived recovery interruption."
    );
    assert_eq!(runtime.store.recovery_phase().unwrap(), None);
    runtime.sync(0).await.unwrap();
    assert_eq!(state.lock().unwrap().acknowledged_keys, 1);
    assert_eq!(
        runtime
            .store
            .db
            .query_row("SELECT count(*) FROM events", [], |row| row
                .get::<_, usize>(0))
            .unwrap(),
        1
    );
    server.abort();
}

#[tokio::test]
async fn partial_sdk_store_is_rejected_before_restoring_an_existing_device() {
    let (origin, server) = fixture_server(false, false, false).await;
    let directory = tempfile::tempdir().unwrap();
    let mut runtime = fixture_runtime(directory.path(), &origin);
    runtime.sync(0).await.unwrap();
    let client = runtime.client().await.unwrap();
    client
        .state_store()
        .remove_kv_data(StateStoreDataKey::SyncToken)
        .await
        .unwrap();
    drop(client);
    drop(runtime);
    let mut runtime = Runtime::new(directory.path().to_owned(), origin).unwrap();
    let error = match runtime.client().await {
        Ok(_) => panic!("partial state must not restore the old device"),
        Err(error) => error,
    };
    assert!(error.to_string().contains("SYNC_STATE_INCOMPLETE"));
    assert!(runtime.client.is_none());
    assert_eq!(runtime.store.cursor().unwrap().as_deref(), Some("batch-1"));
    // A marker captured with a real SDK token does not authorize forgetting
    // that token later: this is partial state damage, not an initial catch-up.
    runtime
        .store
        .commit_recovery("batch-2", &[], &[], Some("batch-1"))
        .unwrap();
    let error = match runtime.client().await {
        Ok(_) => panic!("damaged pending recovery must fail closed"),
        Err(error) => error,
    };
    assert!(error.to_string().contains("SYNC_STATE_INCOMPLETE"));
    server.abort();
}

#[tokio::test]
async fn initial_recovery_phase_without_sdk_token_resumes_initial_sdk_sync() {
    let (origin, server) = fixture_server(false, false, false).await;
    let directory = tempfile::tempdir().unwrap();
    let mut runtime = fixture_runtime(directory.path(), &origin);
    let client = runtime.client().await.unwrap();
    runtime
        .store
        .commit_recovery("batch-1", &[(ROOM.into(), fixture_event(false))], &[], None)
        .unwrap();
    drop(client);
    drop(runtime);
    let mut runtime = Runtime::new(directory.path().to_owned(), origin).unwrap();
    runtime.sync(0).await.unwrap();
    assert_eq!(runtime.store.recovery_phase().unwrap(), None);
    assert_eq!(runtime.store.cursor().unwrap().as_deref(), Some("batch-1"));
    assert_eq!(
        runtime
            .store
            .db
            .query_row("SELECT count(*) FROM events", [], |row| row
                .get::<_, usize>(0))
            .unwrap(),
        1
    );
    server.abort();
}
