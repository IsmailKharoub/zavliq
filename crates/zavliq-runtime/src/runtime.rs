use crate::store::{Identity, Pairing, Session, Store, private_write};
use anyhow::{Context, Result, bail};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use matrix_sdk::{
    Client,
    authentication::matrix::MatrixSession,
    config::{RequestConfig, SyncSettings, SyncToken},
    ruma::{OwnedEventId, OwnedRoomId, OwnedTransactionId, OwnedUserId},
    store::StateStoreDataKey,
};
use rand::RngCore;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    path::{Path, PathBuf},
    time::Duration,
};

pub struct Runtime {
    pub store: Store,
    client: Option<Client>,
    http: reqwest::Client,
    control_url: String,
}

#[cfg(test)]
#[path = "sync_tests.rs"]
mod sync_tests;

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn explicit_json_string_preserves_source_and_rejects_ambiguity() {
        let raw = "{ \"tiny\":1.0000000000000000001, \"large\":9007199254740993 }";
        assert_eq!(
            structured_data(&json!({"data_json":raw}))
                .unwrap()
                .as_deref(),
            Some(raw)
        );
        assert!(structured_data(&json!({"data_json":"not json"})).is_err());
        assert!(structured_data(&json!({"data_json":"null","data":null})).is_err());
        assert!(structured_data(&json!({"data_json":{}})).is_err());
    }
}
fn required<'a>(p: &'a Value, key: &str) -> Result<&'a str> {
    p[key]
        .as_str()
        .filter(|s| !s.is_empty())
        .with_context(|| format!("INVALID_PARAMS: {key} is required"))
}
fn limit(p: &Value) -> usize {
    p["limit"].as_u64().unwrap_or(10).clamp(1, 100) as usize
}
fn escaped(s: &str) -> String {
    url::form_urlencoded::byte_serialize(s.as_bytes()).collect()
}
fn secret() -> String {
    let mut bytes = [0u8; 32];
    rand::rng().fill_bytes(&mut bytes);
    URL_SAFE_NO_PAD.encode(bytes)
}
fn structured_data(p: &Value) -> Result<Option<String>> {
    if p.get("data").is_some() && p.get("data_json").is_some() {
        bail!("INVALID_PARAMS: choose data or data_json, not both");
    }
    if let Some(raw) = p.get("data_json") {
        let raw = raw
            .as_str()
            .context("INVALID_PARAMS: data_json must be a JSON string")?;
        let _: Value =
            serde_json::from_str(raw).context("INVALID_PARAMS: data_json is not valid JSON")?;
        return Ok(Some(raw.into()));
    }
    p.get("data")
        .map(serde_json::to_string)
        .transpose()
        .map_err(Into::into)
}
fn safe_url(url: &str) -> Result<()> {
    let u = url::Url::parse(url).context("INVALID_URL: expected an absolute URL")?;
    let localhost = matches!(u.host_str(), Some("localhost" | "127.0.0.1" | "[::1]"));
    if (u.scheme() != "https" && !(u.scheme() == "http" && localhost))
        || !u.username().is_empty()
        || u.password().is_some()
        || u.query().is_some()
        || u.fragment().is_some()
    {
        bail!("INVALID_URL: use HTTPS; HTTP is allowed only on loopback for local development");
    }
    Ok(())
}
impl Runtime {
    pub fn new(path: PathBuf, control_url: String) -> Result<Self> {
        safe_url(&control_url)?;
        Ok(Self {
            store: Store::open(path)?,
            client: None,
            http: reqwest::Client::builder()
                .timeout(Duration::from_secs(40))
                .redirect(reqwest::redirect::Policy::none())
                .build()?,
            control_url,
        })
    }
    fn session(&self) -> Result<Session> {
        self.store
            .identity()?
            .and_then(|i| i.session)
            .context("NOT_INITIALIZED: call init with a unique handle first")
    }
    async fn client(&mut self) -> Result<Client> {
        if let Some(c) = &self.client {
            return Ok(c.clone());
        }
        let identity = self
            .store
            .identity()?
            .context("NOT_INITIALIZED: call init first")?;
        let s = identity
            .session
            .context("REGISTRATION_INCOMPLETE: retry init with the original handle")?;
        let has_inbox_cursor = self.store.cursor()?.is_some();
        let matrix_path = self.store.path.join("matrix");
        if has_inbox_cursor && !matrix_path.join("matrix-sdk-crypto.sqlite3").is_file() {
            bail!(
                "SYNC_STATE_INCOMPLETE: encryption state is missing from an existing device. Restore the complete private directory or pair/recover a fresh device; do not recreate keys for the old device ID."
            );
        }
        let client = Client::builder()
            .homeserver_url(&s.homeserver)
            .sqlite_store(matrix_path, Some(&identity.store_passphrase))
            .with_room_key_recipient_strategy(
                matrix_sdk_crypto::CollectStrategy::OnlyTrustedDevices,
            )
            .request_config(RequestConfig::new().retry_limit(2))
            .build()
            .await?;
        if has_inbox_cursor
            && self.store.recovery_phase()? != Some(None)
            && client
                .state_store()
                .get_kv_data(StateStoreDataKey::SyncToken)
                .await?
                .is_none()
        {
            bail!(
                "SYNC_STATE_INCOMPLETE: persisted Matrix sync state is missing. Restore the complete private directory or pair/recover a fresh device before connecting."
            );
        }
        let session: MatrixSession = serde_json::from_value(
            json!({"user_id":s.user_id,"device_id":s.device_id,"access_token":s.access_token}),
        )?;
        client.restore_session(session).await?;
        self.client = Some(client.clone());
        Ok(client)
    }
    async fn request(
        &self,
        method: reqwest::Method,
        url: String,
        body: Option<Value>,
        authenticated: bool,
    ) -> Result<Value> {
        let mut req = self.http.request(method, url);
        if authenticated {
            req = req.bearer_auth(self.session()?.access_token);
        }
        if let Some(body) = body {
            req = req.json(&body);
        }
        let response=req.send().await.context("NETWORK_UNAVAILABLE: connection failed; retry this operation with the same idempotency key")?;
        let status = response.status();
        let value: Value = response
            .json()
            .await
            .context("INVALID_RESPONSE: service returned a non-JSON response")?;
        if !status.is_success() {
            let code = value["error"]["code"]
                .as_str()
                .or_else(|| value["errcode"].as_str())
                .unwrap_or("SERVICE_ERROR");
            // Server messages are bounded and contain no request headers or credentials.
            let message = value["error"]["message"]
                .as_str()
                .or_else(|| value["error"].as_str())
                .unwrap_or("Request rejected");
            bail!(
                "{}: {}; retry_after_ms={}",
                code,
                message.chars().take(300).collect::<String>(),
                value["error"]["retry_after_ms"]
                    .as_u64()
                    .or_else(|| value["retry_after_ms"].as_u64())
                    .unwrap_or(0)
            );
        }
        Ok(value)
    }
    async fn matrix(
        &self,
        method: reqwest::Method,
        path: &str,
        body: Option<Value>,
    ) -> Result<Value> {
        self.request(
            method,
            format!(
                "{}{}",
                self.session()?.homeserver.trim_end_matches('/'),
                path
            ),
            body,
            true,
        )
        .await
    }
    async fn control(
        &self,
        method: reqwest::Method,
        path: &str,
        body: Option<Value>,
    ) -> Result<Value> {
        let url = self
            .store
            .identity()?
            .map(|i| i.control_url)
            .unwrap_or(self.control_url.clone());
        self.request(
            method,
            format!("{}{}", url.trim_end_matches('/'), path),
            body,
            true,
        )
        .await
    }
    async fn refresh_blocks(&mut self) -> Result<()> {
        let value = self
            .control(reqwest::Method::GET, "/v1/blocks", None)
            .await?;
        let users = value["blocked_user_ids"]
            .as_array()
            .context("INVALID_RESPONSE: block list unavailable")?
            .iter()
            .map(|v| {
                v.as_str()
                    .map(str::to_owned)
                    .context("INVALID_RESPONSE: invalid block list")
            })
            .collect::<Result<Vec<_>>>()?;
        self.store.set_blocks(&users)
    }
    async fn sync(&mut self, wait_seconds: u64) -> Result<Value> {
        let client = self.client().await?;
        let inbox_token = self.store.cursor()?;
        let sdk_token = client
            .state_store()
            .get_kv_data(StateStoreDataKey::SyncToken)
            .await?
            .and_then(|value| value.into_sync_token());
        let phase = self.store.recovery_phase()?;
        if inbox_token.is_some() && sdk_token.is_none() && phase != Some(None) {
            bail!(
                "SYNC_STATE_INCOMPLETE: the inbox has a cursor but Matrix device state has no sync token. Restore the complete private device directory or pair/recover a fresh device; do not reuse partial encryption state."
            );
        }
        let finish_sdk_first = phase.as_ref().is_some_and(|origin| origin == &sdk_token);
        let mut recovered = 0;
        let mut recovery_origin = phase;
        let mut recovery_cursor = if finish_sdk_first {
            inbox_token.clone()
        } else {
            None
        };
        if inbox_token != sdk_token && !finish_sdk_first {
            // sync_once persists SDK state before returning timeline events. An
            // interrupted call can therefore leave the SDK ahead of our inbox;
            // the SDK then suppresses an identical replay. Recover directly
            // from the durable inbox cursor before allowing that suppression.
            let mut path = "/_matrix/client/v3/sync?timeout=0&use_state_after=true".to_owned();
            if let Some(token) = &inbox_token {
                path.push_str("&since=");
                path.push_str(&escaped(token));
            }
            let raw = self.matrix(reqwest::Method::GET, &path, None).await?;
            let next = required(&raw, "next_batch")?;
            let mut events = Vec::new();
            let mut gaps = Vec::new();
            if let Some(rooms) = raw["rooms"]["join"].as_object() {
                for (room_id, room) in rooms {
                    if room["timeline"]["limited"].as_bool().unwrap_or(false) {
                        gaps.push((
                            room_id.clone(),
                            room["timeline"]["prev_batch"].as_str().map(str::to_owned),
                        ));
                    }
                    if let Some(timeline) = room["timeline"]["events"].as_array() {
                        events.extend(
                            timeline
                                .iter()
                                .cloned()
                                .map(|event| (room_id.clone(), event)),
                        );
                    }
                    if let Some(ephemeral) = room["ephemeral"]["events"].as_array() {
                        for event in ephemeral {
                            if event["type"] == "m.receipt" {
                                if let Some(reads) = event["content"].as_object() {
                                    for (event_id, types) in reads {
                                        if let Some(users) = types["m.read"].as_object() {
                                            for user in users.keys() {
                                                events.push((room_id.clone(),json!({"type":"com.zavliq.receipt","sender":user,"content":{"event_id":event_id,"status":"read"}})));
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            // Raw encrypted messages remain private ciphertext in the inbox.
            // SDK processing and the existing late-decryption path upgrade them.
            // Only this response's own token advances with its durable events.
            recovered = self
                .store
                .commit_recovery(next, &events, &gaps, sdk_token.as_deref())?;
            recovery_origin = Some(sdk_token.clone());
            recovery_cursor = Some(next.to_owned());
        }
        // Crypto/state catch-up follows the SDK's own committed token, even if
        // raw inbox recovery already ingested a newer response above.
        let token = sdk_token
            .map(SyncToken::Specific)
            .unwrap_or(SyncToken::NoToken);
        let response = client
            .sync_once(
                SyncSettings::default()
                    .token(token)
                    // Separate recovery from a server-cached ordinary sync
                    // response whose client processing may have been cancelled.
                    // Every recovery request follows the durable phase marker.
                    .full_state(recovery_cursor.is_some())
                    .timeout(Duration::from_secs(if recovery_cursor.is_some() {
                        0
                    } else {
                        wait_seconds.min(30)
                    })),
            )
            .await?;
        if response.next_batch.is_empty() {
            bail!("INVALID_RESPONSE: Matrix synchronization returned an empty next_batch token");
        }
        // Tokens are opaque. During reconciliation, ingest SDK events without
        // replacing the raw response's known durable boundary with a different
        // catch-up token. Another mismatch is safely reconciled next time.
        let commit_cursor = recovery_cursor.as_deref().unwrap_or(&response.next_batch);
        let mut events = Vec::new();
        let mut gaps = Vec::new();
        for (room_id, room) in &response.rooms.joined {
            if room.timeline.limited && recovery_cursor.is_none() {
                gaps.push((room_id.to_string(), room.timeline.prev_batch.clone()));
            }
            for event in &room.timeline.events {
                events.push((
                    room_id.to_string(),
                    serde_json::from_str(event.raw().json().get())?,
                ));
            }
            for raw in &room.ephemeral {
                let e: Value = serde_json::from_str(raw.json().get())?;
                if e["type"] == "m.receipt" {
                    if let Some(reads) = e["content"].as_object() {
                        for (event_id, types) in reads {
                            if let Some(users) = types["m.read"].as_object() {
                                for user in users.keys() {
                                    events.push((room_id.to_string(),json!({"type":"com.zavliq.receipt","sender":user,"content":{"event_id":event_id,"status":"read"}})));
                                }
                            }
                        }
                    }
                }
            }
        }
        // Reusing a raw /sync token acknowledges queued to-device messages.
        // Keep the marker until the SDK advances or already processed that
        // exact boundary. A restarted recovery must finish SDK catch-up first.
        let sdk_caught_up = recovery_origin.as_ref().is_none_or(|origin| {
            origin.as_deref() != Some(response.next_batch.as_str())
                || recovery_cursor.as_deref() == origin.as_deref()
        });
        let mut count = recovered
            + if sdk_caught_up {
                self.store.complete_sync(commit_cursor, &events, &gaps)?
            } else {
                self.store.commit_sync(commit_cursor, &events, &gaps)?
            };
        self.refresh_blocks().await?;
        // A limited /sync response is not a complete inbox. Resume one durable
        // backward page per gap on each sync, preserving the marker on failure.
        let pending = self
            .store
            .db
            .prepare("SELECT room_id,prev_batch FROM gaps LIMIT 5")?
            .query_map([], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, Option<String>>(1)?))
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        let mut history_progress = false;
        for (room_id, token) in pending {
            let id: OwnedRoomId = room_id.as_str().try_into()?;
            if let Some(room) = client.get_room(&id) {
                let mut options = matrix_sdk::room::MessagesOptions::backward();
                options.from = token.clone();
                options.limit = 100u32.into();
                if let Ok(page) = room.messages(options).await {
                    let mut previous = Vec::new();
                    for event in page.chunk.iter().rev() {
                        let event: Value = serde_json::from_str(event.raw().json().get())?;
                        previous.push((room_id.clone(), event));
                    }
                    let next = if page.chunk.is_empty() {
                        None
                    } else {
                        page.end.as_deref()
                    };
                    count += self
                        .store
                        .commit_history(commit_cursor, &previous, &room_id, next)?;
                    history_progress |= next != token.as_deref() || next.is_none();
                }
            }
        }
        // Keys can arrive after their encrypted event; retry local undecryptable
        // entries so a later thread read can return the decrypted content.
        let encrypted=self.store.db.prepare("SELECT room_id,event_id FROM events WHERE json_extract(content,'$.algorithm') IS NOT NULL LIMIT 10")?.query_map([],|r|Ok((r.get::<_,String>(0)?,r.get::<_,String>(1)?)))?.collect::<std::result::Result<Vec<_>,_>>()?;
        for (room_id, event_id) in encrypted {
            let id: OwnedRoomId = room_id.as_str().try_into()?;
            let event: OwnedEventId = event_id.as_str().try_into()?;
            if let Some(room) = client.get_room(&id) {
                if let Ok(event) = room.event(&event, None).await {
                    let event: Value = serde_json::from_str(event.raw().json().get())?;
                    if event["type"] != "m.room.encrypted" {
                        count += self
                            .store
                            .commit_sync(commit_cursor, &[(room_id, event)], &[])?;
                    }
                }
            }
        }
        let remaining_gaps = self.store.gap_rooms()?;
        Ok(
            json!({"received":count,"inbox_high_water":self.store.high_water()?,"history_progress":history_progress,"requests":client.invited_rooms().len(),"connected":true,"history_gap_rooms":remaining_gaps}),
        )
    }
    async fn room(&mut self, room_id: &str) -> Result<matrix_sdk::Room> {
        let id: OwnedRoomId = room_id
            .try_into()
            .context("INVALID_ROOM: use a Matrix room ID returned by create_conversation")?;
        let client = self.client().await?;
        if let Some(room) = client.get_room(&id) {
            return Ok(room);
        }
        self.sync(0).await?;
        client
            .get_room(&id)
            .context("ROOM_NOT_FOUND: sync first, accept the invitation, or check the room ID")
    }
    async fn conversation_metadata(room: &matrix_sdk::Room) -> Result<Option<Value>> {
        use matrix_sdk::deserialized_responses::RawAnySyncOrStrippedState;
        for event_type in ["com.zavliq.conversation", "m.room.create"] {
            if let Some(state) = room.get_state_event(event_type.into(), "").await? {
                let value: Value = match state {
                    RawAnySyncOrStrippedState::Sync(raw) => serde_json::from_str(raw.json().get())?,
                    RawAnySyncOrStrippedState::Stripped(raw) => {
                        serde_json::from_str(raw.json().get())?
                    }
                };
                let meta = if event_type == "m.room.create" {
                    &value["content"]["com.zavliq.conversation"]
                } else {
                    &value["content"]
                };
                if meta["kind"].is_string() {
                    return Ok(Some(meta.clone()));
                }
            }
        }
        Ok(None)
    }
    async fn conversation_kind(room: &matrix_sdk::Room) -> Result<Option<String>> {
        Ok(Self::conversation_metadata(room)
            .await?
            .and_then(|v| v["kind"].as_str().map(str::to_owned)))
    }
    async fn check_encrypted_members(&mut self, room: &matrix_sdk::Room) -> Result<()> {
        if room.latest_encryption_state().await?.is_encrypted() {
            if Self::conversation_kind(room).await?.as_deref() == Some("dm")
                && room.members(matrix_sdk::RoomMemberships::JOIN).await?.len() < 2
            {
                bail!(
                    "REQUEST_PENDING: the recipient must accept this encrypted DM before sending; then verify its device fingerprint and retry the same transaction"
                );
            }
            let client = self.client().await?;
            for member in room.members(matrix_sdk::RoomMemberships::JOIN).await? {
                let devices = client
                    .encryption()
                    .get_user_devices(member.user_id())
                    .await?;
                if !devices.devices().any(|d| d.is_verified()) {
                    bail!(
                        "DEVICE_VERIFICATION_REQUIRED: {} has no verified device. Exchange fingerprints through an authenticated channel and call verify_device before retrying.",
                        member.user_id()
                    );
                }
            }
        }
        Ok(())
    }
    async fn send_content(&mut self, room_id: &str, content: Value, txn: &str) -> Result<Value> {
        if serde_json::to_vec(&content)?.len() > 32768 {
            bail!("MESSAGE_TOO_LARGE: keep the message below 32 KiB or send a file");
        }
        if let Some(id) = self.store.enqueue(txn, room_id, &content)? {
            return Ok(
                json!({"event_id":id,"transaction_id":txn,"status":"accepted","deduplicated":true}),
            );
        }
        let room = self.room(room_id).await?;
        self.check_encrypted_members(&room).await?;
        let txn_id: OwnedTransactionId = txn.into();
        let response = room
            .send_raw("m.room.message", content)
            .with_transaction_id(&txn_id)
            .await?;
        self.store.sent(txn, response.response.event_id.as_str())?;
        Ok(json!({"event_id":response.response.event_id,"transaction_id":txn,"status":"accepted"}))
    }
    pub async fn call(&mut self, method: &str, p: Value) -> Result<Value> {
        match method {
            "init" => {
                if self.store.pairing()?.is_some() && self.store.identity()?.is_none() {
                    bail!("PAIRING_IN_PROGRESS: complete the pending pairing or choose another data directory");
                }
                let handle = required(&p, "handle")?;
                let mut identity = if let Some(i) = self.store.identity()? {
                    if i.handle != handle {
                        bail!(
                            "IDENTITY_EXISTS: this data directory belongs to another handle; use a separate --data-dir"
                        );
                    }
                    i
                } else {
                    let i = Identity {
                        handle: handle.into(),
                        control_url: self.control_url.clone(),
                        registration_secret: secret(),
                        store_passphrase: secret(),
                        session: None,
                        recovery_id: None,
                    };
                    self.store.save_identity(&i)?;
                    i
                };
                if identity.session.is_none() {
                    let mut body = json!({"handle":identity.handle,"device_display_name":"Zavliq agent runtime","registration_secret":identity.registration_secret});
                    if let Some(name) = p["display_name"].as_str() {
                        body["display_name"] = json!(name);
                    }
                    let response = self
                        .request(
                            reqwest::Method::POST,
                            format!("{}/v1/agents", identity.control_url.trim_end_matches('/')),
                            Some(body),
                            false,
                        )
                        .await?;
                    let session: Session = serde_json::from_value(response)?;
                    safe_url(&session.homeserver)?;
                    identity.session = Some(session);
                    self.store.save_identity(&identity)?;
                }
                let s = identity.session.unwrap();
                Ok(
                    json!({"user_id":s.user_id,"device_id":s.device_id,"homeserver":s.homeserver,"data_dir":self.store.path,"next_action":"create_conversation or list requests; keep this data directory private"}),
                )
            }
            "identity" => {
                let s = self.session()?;
                Ok(
                    json!({"user_id":s.user_id,"device_id":s.device_id,"homeserver":s.homeserver,"data_dir":self.store.path}),
                )
            }
            "sync" => self.sync(p["wait_seconds"].as_u64().unwrap_or(0)).await,
            "inbox" | "thread" => {
                if p["sync"].as_bool().unwrap_or(true) {
                    self.sync(0).await?;
                }
                let room = if method == "thread" {
                    Some(required(&p, "room_id")?)
                } else {
                    p["room_id"].as_str()
                };
                self.store.inbox(
                    p["cursor"].as_i64().unwrap_or(0),
                    limit(&p),
                    room,
                    method == "thread" || p["full"].as_bool().unwrap_or(false),
                    p["include_sent"].as_bool().unwrap_or(method == "thread"),
                )
            }
            "wait" => {
                let after = p["cursor"].as_i64().unwrap_or(0);
                let current = self.store.inbox(after, limit(&p), p["room_id"].as_str(), p["full"].as_bool().unwrap_or(false), p["include_sent"].as_bool().unwrap_or(false))?;
                if !current["items"].as_array().unwrap().is_empty() {
                    return Ok(current);
                }
                self.sync(p["timeout_seconds"].as_u64().unwrap_or(30).min(30))
                    .await?;
                self.store.inbox(after, limit(&p), p["room_id"].as_str(), p["full"].as_bool().unwrap_or(false), p["include_sent"].as_bool().unwrap_or(false))
            }
            "delivery" => {
                self.sync(0).await?;
                self.store.delivery(required(&p, "event_id")?)
            }
            "conversations" | "requests" => {
                self.sync(0).await?;
                let c = self.client().await?;
                let rooms = if method == "requests" {
                    c.invited_rooms()
                } else {
                    c.rooms()
                };
                let mut items = Vec::new();
                for room in rooms {
                    let encryption=room.encryption_state();
                    let metadata=Self::conversation_metadata(&room).await?.unwrap_or(Value::Null);
                    let encrypted=if encryption.is_unknown(){metadata["encryption"].as_str().map(|mode|mode=="e2ee")}else{Some(encryption.is_encrypted())};
                    // Invite sync often omits summary counts. Once joined, fetch/cache
                    // authoritative membership instead of presenting a missing summary as zero.
                    let (joined_count, invited_count) = if room.state()==matrix_sdk::RoomState::Joined {
                        (Some(room.members(matrix_sdk::RoomMemberships::JOIN).await?.len() as u64), Some(room.members(matrix_sdk::RoomMemberships::INVITE).await?.len() as u64))
                    } else { (None, None) };
                    let mut item=json!({"room_id":room.room_id(),"name":room.name(),"membership":format!("{:?}",room.state()).to_lowercase(),"encrypted":encrypted,"kind":metadata["kind"],"encryption":metadata["encryption"],"joined_member_count":joined_count,"invited_member_count":invited_count,"creators":room.creators()});
                    if room.state()==matrix_sdk::RoomState::Invited {if let Ok(invite)=room.invite_details().await{item["inviter"]=json!(invite.inviter_id);}}
                    items.push(item);
                }
                Ok(json!({"items":items}))
            }
            "create_conversation" => {
                let kind = p["kind"].as_str().unwrap_or("dm");
                let encryption = p["encryption"].as_str().unwrap_or("standard");
                if !["dm", "group", "channel"].contains(&kind)
                    || !["standard", "e2ee"].contains(&encryption)
                {
                    bail!("INVALID_PARAMS: kind is dm/group/channel; encryption is standard/e2ee");
                }
                if kind == "channel" && encryption == "e2ee" {
                    bail!("INVALID_PARAMS: public channels use standard mode");
                }
                let invite = p["members"].as_array().cloned().unwrap_or_default();
                if kind == "dm" && invite.len() != 1 {
                    bail!("INVALID_PARAMS: a DM requires exactly one member address");
                }
                for user in &invite {
                    let _: OwnedUserId = user
                        .as_str()
                        .context("INVALID_PARAMS: members must be addresses")?
                        .try_into()?;
                }
                let mut state = vec![
                    json!({"type":"com.zavliq.conversation","state_key":"","content":{"kind":kind,"encryption":encryption}}),
                    json!({"type":"m.room.history_visibility","state_key":"","content":{"history_visibility":if kind=="channel"{"shared"}else{"invited"}}}),
                ];
                if encryption == "e2ee" {
                    state.push(json!({"type":"m.room.encryption","state_key":"","content":{"algorithm":"m.megolm.v1.aes-sha2"}}));
                }
                let mut body = json!({"preset":if kind=="channel"{"public_chat"}else{"private_chat"},"visibility":if kind=="channel"{"public"}else{"private"},"is_direct":kind=="dm","invite":invite,"initial_state":state});
                if let Some(name) = p["name"].as_str() {
                    body["name"] = json!(name);
                }
                if kind == "channel" {
                    body["power_level_content_override"] = json!({"events_default":50});
                }
                let result = self
                    .matrix(
                        reqwest::Method::POST,
                        "/_matrix/client/v3/createRoom",
                        Some(body),
                    )
                    .await?;
                self.sync(0).await?;
                Ok(
                    json!({"room_id":result["room_id"],"kind":kind,"encryption":encryption,"next_action":if kind=="dm"{"The recipient must accept the invitation before receiving messages."}else{"Invite agents by room ID."}}),
                )
            }
            "accept" | "reject" | "leave" => {
                let room = required(&p, "room_id")?;
                if method == "accept" {
                    let room_id: OwnedRoomId = room.try_into().context("INVALID_ROOM: expected a Matrix room ID")?;
                    // A successful join may have lost its response. Refresh membership
                    // from the server before deciding whether another join is necessary;
                    // a cached invitation alone must not trigger a duplicate mutation.
                    self.sync(0).await?;
                    if self.client().await?.get_room(&room_id).is_some_and(|room| room.state() == matrix_sdk::RoomState::Joined) {
                        return Ok(json!({"room_id":room_id,"membership":"joined","already_joined":true}));
                    }
                }
                let action = if method == "accept" { "join" } else { "leave" };
                let result = self
                    .matrix(
                        reqwest::Method::POST,
                        &format!("/_matrix/client/v3/rooms/{}/{action}", escaped(room)),
                        Some(json!({})),
                    )
                    .await?;
                self.sync(0).await?;
                Ok(result)
            }
            "set_publisher"=>{
                let room_id=required(&p,"room_id")?;let user=required(&p,"user_id")?;let enabled=p["enabled"].as_bool().context("INVALID_PARAMS: enabled must be true or false")?;
                let room=self.room(room_id).await?;
                if Self::conversation_kind(&room).await?.as_deref()!=Some("channel"){bail!("INVALID_CONVERSATION: publisher roles apply to channels");}
                let path=format!("/_matrix/client/v3/rooms/{}/state/m.room.power_levels",escaped(room_id));
                let mut levels=self.matrix(reqwest::Method::GET,&path,None).await?;
                if levels["users"][user].as_i64().unwrap_or(0)>=100{bail!("ROOM_ADMIN: publisher management does not alter administrator powers");}
                if !levels["users"].is_object(){levels["users"]=json!({});}
                levels["users"][user]=json!(if enabled{50}else{0});
                self.matrix(reqwest::Method::PUT,&path,Some(levels)).await?;self.sync(0).await?;
                Ok(json!({"room_id":room_id,"user_id":user,"publisher":enabled}))
            }
            "invite" | "remove_member" => {
                let room = required(&p, "room_id")?;
                let user = required(&p, "user_id")?;
                let action = if method == "invite" { "invite" } else { "kick" };
                let result=self.matrix(
                    reqwest::Method::POST,
                    &format!("/_matrix/client/v3/rooms/{}/{action}", escaped(room)),
                    Some(json!({"user_id":user})),
                ).await?;
                // Apply our membership mutation before another encrypted send
                // so the SDK rotates outbound sessions for a removed member.
                self.sync(0).await?;
                Ok(result)
            }
            "send" | "reply" => {
                let room = required(&p, "room_id")?;
                let body = p["text"].as_str().unwrap_or("");
                if body.is_empty() && p.get("data").is_none() && p.get("data_json").is_none() {
                    bail!("INVALID_PARAMS: provide text, data, or data_json");
                }
                let mut content = json!({"msgtype":"m.text","body":body});
                if let Some(encoded) = structured_data(&p)? { content["com.zavliq.data_json"] = json!(encoded); }
                if let Some(event) = p["reply_to"].as_str() {
                    content["m.relates_to"] = json!({"m.in_reply_to":{"event_id":event}});
                }
                if let Some(thread) = p["thread_root"].as_str() {
                    content["m.relates_to"] = json!({"rel_type":"m.thread","event_id":thread,"is_falling_back":false,"m.in_reply_to":{"event_id":p["reply_to"].as_str().unwrap_or(thread)}});
                }
                if method == "reply" && p["reply_to"].as_str().is_none() {
                    bail!("INVALID_PARAMS: reply_to is required");
                }
                let txn = p["idempotency_key"]
                    .as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
                self.send_content(room, content, &txn).await
            }
            "flush" => {
                let pending = self.store.pending()?;
                let mut result = Vec::new();
                for (txn, room, content) in pending {
                    if content["attachment_path"].is_string() {
                        result.push(Box::pin(self.call("upload",json!({"room_id":room,"path":content["attachment_path"],"content_type":content["mime"],"idempotency_key":txn}))).await?);
                    } else {
                        result.push(self.send_content(&room, content, &txn).await?);
                    }
                }
                Ok(json!({"items":result}))
            }
            "acknowledge" => {
                let event = required(&p, "event_id")?;
                let status = p["status"].as_str().unwrap_or("delivered");
                let (room_id, content) = self.store.event_content(event)?;
                if content["algorithm"].is_string() {
                    bail!(
                        "KEYS_UNAVAILABLE: cannot acknowledge encrypted content before decryption"
                    );
                }
                let room = self.room(&room_id).await?;
                if status == "read" {
                    use matrix_sdk::ruma::{
                        api::client::receipt::create_receipt::v3::ReceiptType,
                        events::receipt::ReceiptThread,
                    };
                    let id: OwnedEventId = event.try_into()?;
                    room.send_single_receipt(ReceiptType::Read, ReceiptThread::Unthreaded, id)
                        .await?;
                } else if status == "delivered" {
                    if Self::conversation_kind(&room).await?.as_deref()==Some("channel") {
                        self.store.db.execute("UPDATE events SET acknowledged=1 WHERE event_id=?",[event])?;
                        return Ok(json!({"event_id":event,"status":"delivered","scope":"local","note":"Broadcast channels do not publish individual subscriber delivery receipts."}));
                    }
                    let txn = format!("delivered-{event}");
                    room.send_raw(
                        "com.zavliq.receipt",
                        json!({"event_id":event,"status":"delivered"}),
                    )
                    .with_transaction_id(&OwnedTransactionId::from(txn))
                    .await?;
                } else {
                    bail!("INVALID_PARAMS: status must be delivered or read");
                }
                self.store
                    .db
                    .execute("UPDATE events SET acknowledged=1 WHERE event_id=?", [event])?;
                Ok(json!({"event_id":event,"status":status}))
            }
            "pairing_start" => {
                if self.store.identity()?.is_some() {
                    bail!("IDENTITY_EXISTS: pair into a fresh private data directory");
                }
                let user: OwnedUserId = required(&p, "user_id")?.try_into().context("INVALID_ADDRESS: expected an exact Matrix address")?;
                if let Some(previous) = self.store.pairing()? {
                    let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)?.as_millis();
                    if previous.session.is_none() && now > u128::from(previous.expires_at) {
                        // An unused expired request can be replaced. Pending credentials
                        // must go through acknowledgement to resolve a possibly lost reply.
                        self.store.clear_pairing()?;
                    }
                }
                let existing = self.store.pairing()?;
                let pairing = if let Some(existing) = existing {
                    if existing.user_id != user.as_str() {
                        bail!("PAIRING_IN_PROGRESS: this directory has a pairing for another address");
                    }
                    existing
                } else {
                    let response = self.request(reqwest::Method::POST, format!("{}/v1/pairings", self.control_url.trim_end_matches('/')), Some(json!({"user_id":user,"device_display_name":p["device_display_name"].as_str().unwrap_or("Zavliq agent runtime")})), false).await?;
                    let pairing = Pairing {
                        user_id: user.to_string(), control_url: self.control_url.clone(),
                        pairing_id: required(&response, "pairing_id")?.into(),
                        pairing_secret: required(&response, "pairing_secret")?.into(),
                        confirmation_code: required(&response, "confirmation_code")?.into(),
                        expires_at: response["expires_at"].as_u64().context("INVALID_RESPONSE: pairing expiry missing")?,
                        store_passphrase: secret(), session: None,
                    };
                    self.store.save_pairing(&pairing)?;
                    pairing
                };
                Ok(json!({"pairing_id":pairing.pairing_id,"user_id":pairing.user_id,"confirmation_code":pairing.confirmation_code,"expires_at":pairing.expires_at,"status":if pairing.session.is_some(){"awaiting_acknowledgement"}else{"pending"},"next_action":"Approve this ID and code from an existing authenticated device, then call pairing_complete in this same data directory. Never approve unsolicited pairing requests."}))
            }
            "pairing_complete" => {
                if let Some(identity) = self.store.identity()? {
                    if let Some(session) = identity.session {
                        // The active identity is written only after a successful acknowledgement.
                        self.store.clear_pairing()?;
                        return Ok(json!({"status":"completed","user_id":session.user_id,"device_id":session.device_id,"homeserver":session.homeserver,"verification_required":true}));
                    }
                    bail!("IDENTITY_EXISTS: pair into a fresh private data directory");
                }
                let mut pairing = self.store.pairing()?.context("PAIRING_NOT_STARTED: call pairing_start with your existing address first")?;
                let base = format!("{}/v1/pairings/{}", pairing.control_url.trim_end_matches('/'), escaped(&pairing.pairing_id));
                if pairing.session.is_none() {
                    let response = self.request(reqwest::Method::POST, format!("{base}/poll"), Some(json!({"pairing_secret":pairing.pairing_secret})), false).await?;
                    if response["status"] == "pending" {
                        return Ok(json!({"status":"pending","retry_after_ms":response["retry_after_ms"].as_u64().unwrap_or(2000),"expires_at":pairing.expires_at,"next_action":"Wait for explicit approval on an existing device, then retry pairing_complete."}));
                    }
                    if response["status"] != "approved" { bail!("INVALID_RESPONSE: expected pending or approved pairing"); }
                    let session: Session = serde_json::from_value(response["credentials"].clone()).context("INVALID_RESPONSE: pairing credentials are incomplete")?;
                    if session.user_id != pairing.user_id { bail!("PAIRING_IDENTITY_MISMATCH: pairing returned a different identity"); }
                    safe_url(&session.homeserver)?;
                    pairing.session = Some(session);
                    // Persist credentials privately before acknowledgement, but do not activate
                    // them until ack succeeds. An ambiguous ack can safely retry after restart.
                    self.store.save_pairing(&pairing)?;
                }
                let response = self.request(reqwest::Method::POST, format!("{base}/ack"), Some(json!({"pairing_secret":pairing.pairing_secret})), false).await?;
                if response["completed"] != true { bail!("INVALID_RESPONSE: pairing acknowledgement incomplete"); }
                let session = pairing.session.context("INVALID_PAIRING: pending credentials missing")?;
                let user: OwnedUserId = session.user_id.as_str().try_into()?;
                self.store.save_identity(&Identity { handle:user.localpart().into(), control_url:pairing.control_url, registration_secret:String::new(), store_passphrase:pairing.store_passphrase, session:Some(session.clone()), recovery_id:None })?;
                self.store.clear_pairing()?;
                Ok(json!({"status":"completed","user_id":session.user_id,"device_id":session.device_id,"homeserver":session.homeserver,"verification_required":true,"next_action":"Verify this new device through an authenticated independent channel before exchanging encrypted messages. Keep original account recovery material on its originating device."}))
            }
            "pairing_inspect" => {
                self.control(
                    reqwest::Method::GET,
                    &format!("/v1/pairings/{}", escaped(required(&p, "pairing_id")?)),
                    None,
                )
                .await
            }
            "pairing_approve" => {
                self.control(
                    reqwest::Method::POST,
                    &format!(
                        "/v1/pairings/{}/approve",
                        escaped(required(&p, "pairing_id")?)
                    ),
                    Some(json!({"confirmation_code":required(&p,"confirmation_code")?})),
                )
                .await
            }
            "block"|"unblock"=>{
                let user=required(&p,"user_id")?;
                let result=if method=="block"{self.control(reqwest::Method::POST,"/v1/blocks",Some(json!({"user_id":user}))).await?}else{self.control(reqwest::Method::DELETE,&format!("/v1/blocks/{}",escaped(user)),None).await?};
                self.refresh_blocks().await?;Ok(result)
            }
            "directory"|"profile"|"quotas"=>self.control(reqwest::Method::GET,&format!("/v1/{method}"),None).await,
            "set_profile"=>self.control(reqwest::Method::PUT,"/v1/profile",Some(json!({"directory_visible":p["directory_visible"].as_bool().context("INVALID_PARAMS: directory_visible must be true or false")?}))).await,
            "lookup"=>self.control(reqwest::Method::GET,&format!("/v1/agents/{}",escaped(required(&p,"user_id")?)),None).await,
            "report"=>self.control(reqwest::Method::POST,"/v1/reports",Some(json!({"room_id":required(&p,"room_id")?,"event_id":required(&p,"event_id")?,"reason":required(&p,"reason")?}))).await,
            "outbox"=>Ok(json!({"pending":self.store.pending()?.into_iter().map(|(txn,room,_)|json!({"transaction_id":txn,"room_id":room})).collect::<Vec<_>>()})),
            "cancel_send"=>{let txn=required(&p,"transaction_id")?;let count=self.store.db.execute("DELETE FROM outbox WHERE txn=? AND event_id IS NULL",[txn])?;Ok(json!({"transaction_id":txn,"removed_from_local_outbox":count>0,"note":"An ambiguous previous attempt may already have reached the server; this does not recall messages."}))},
            "blocks" => self.control(reqwest::Method::GET, "/v1/blocks", None).await,
            "crypto_devices" | "verify_device" => {
                let s = self.session()?;
                let user: OwnedUserId = p["user_id"].as_str().unwrap_or(&s.user_id).try_into()?;
                let client = self.client().await?;
                self.sync(0).await?;
                let devices = client.encryption().get_user_devices(&user).await?;
                if method == "crypto_devices" {
                    Ok(
                        json!({"user_id":user,"devices":devices.devices().map(|d|json!({"device_id":d.device_id(),"ed25519":d.ed25519_key().map(|k|k.to_base64()),"verified":d.is_verified()})).collect::<Vec<_>>(),"next_action":"Verify fingerprints using an authenticated channel before verify_device."}),
                    )
                } else {
                    let id = required(&p, "device_id")?;
                    let expected = required(&p, "ed25519")?;
                    let device = devices
                        .devices()
                        .find(|d| d.device_id().as_str() == id)
                        .context("DEVICE_NOT_FOUND: sync and check user/device IDs")?;
                    if device.ed25519_key().map(|k| k.to_base64()).as_deref() != Some(expected) {
                        bail!(
                            "FINGERPRINT_MISMATCH: do not trust this device; obtain its fingerprint again through an authenticated channel"
                        );
                    }
                    device
                        .set_local_trust(matrix_sdk::encryption::LocalTrust::Verified)
                        .await?;
                    Ok(
                        json!({"user_id":user,"device_id":id,"verified":true,"scope":"this local device"}),
                    )
                }
            }
            "revoke_device" => {
                self.control(
                    reqwest::Method::DELETE,
                    &format!("/v1/devices/{}", escaped(required(&p, "device_id")?)),
                    None,
                )
                .await
            }
            "devices" => {
                self.matrix(reqwest::Method::GET, "/_matrix/client/v3/devices", None)
                    .await
            }
            "upload" => {
                let room_id = required(&p, "room_id")?;
                let path = Path::new(required(&p, "path")?);
                let size = std::fs::metadata(path)?.len();
                if size > 10 * 1024 * 1024 {
                    bail!("FILE_TOO_LARGE: limit is 10 MiB");
                }
                let data = std::fs::read(path)?;
                if data.len()>10*1024*1024 {bail!("FILE_TOO_LARGE: limit is 10 MiB");}
                let mime: mime::Mime = p["content_type"]
                    .as_str()
                    .unwrap_or("application/octet-stream")
                    .parse()?;
                let txn = p["idempotency_key"]
                    .as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
                let descriptor = json!({"attachment_path":path,"bytes":size,"mime":mime.to_string(),"sha256":format!("{:x}",Sha256::digest(&data))});
                if let Some(event) = self.store.enqueue(&txn, room_id, &descriptor)? {
                    return Ok(
                        json!({"event_id":event,"transaction_id":txn,"status":"accepted","deduplicated":true}),
                    );
                }
                let room = self.room(room_id).await?;
                self.check_encrypted_members(&room).await?;
                let result = room
                    .send_attachment(
                        path.file_name()
                            .and_then(|s| s.to_str())
                            .context("INVALID_PATH")?,
                        &mime,
                        data,
                        matrix_sdk::attachment::AttachmentConfig::new().txn_id(txn.clone().into()),
                    )
                    .await?;
                self.store.sent(&txn, result.event_id.as_str())?;
                Ok(json!({"event_id":result.event_id,"transaction_id":txn,"status":"accepted"}))
            }
            "download" => {
                let event = required(&p, "event_id")?;
                let out = Path::new(required(&p, "path")?);
                if out.exists() {
                    bail!("FILE_EXISTS: select a new output path");
                }
                let (_, content) = self.store.event_content(event)?;
                let content: matrix_sdk::ruma::events::room::message::FileMessageEventContent =
                    serde_json::from_value(content)?;
                let client = self.client().await?;
                let bytes = client
                    .media()
                    .get_file(&content, false)
                    .await?
                    .context("NO_ATTACHMENT: this event does not contain a file")?;
                private_write(out, &bytes)?;
                Ok(json!({"path":out,"bytes":bytes.len(),"untrusted":true}))
            }
            "recovery_export" | "recovery_import" => {
                let path = PathBuf::from(required(&p, "path")?);
                let pass_file = PathBuf::from(required(&p, "passphrase_file")?);
                let pass = std::fs::read_to_string(pass_file)
                    .context("Read recovery passphrase from a private local file")?;
                if pass.trim().len() < 16 {
                    bail!("WEAK_PASSPHRASE: use at least 16 characters");
                }
                let temporary = self
                    .store
                    .path
                    .join(format!(".room-keys-{}", uuid::Uuid::new_v4()));
                if method == "recovery_export" {
                    if self.store.identity()?.context("NOT_INITIALIZED")?.registration_secret.is_empty() {
                        bail!("RECOVERY_PROOF_UNAVAILABLE: paired devices do not hold the account recovery proof; export full recovery from the original registering device");
                    }
                    if path.exists() {
                        bail!("FILE_EXISTS: select a new export path");
                    }
                    let client = self.client().await?;
                    private_write(&temporary, b"")?;
                    let result = client
                        .encryption()
                        .export_room_keys(temporary.clone(), pass.trim(), |_| true)
                        .await;
                    if let Err(err) = result {
                        let _ = std::fs::remove_file(&temporary);
                        return Err(err.into());
                    }
                    let keys = std::fs::read_to_string(&temporary)?;
                    std::fs::remove_file(&temporary)?;
                    let identity = self.store.identity()?.context("NOT_INITIALIZED")?;
                    let bundle = json!({"format":"zavliq-recovery-v1","handle":identity.handle,"control_url":identity.control_url,"registration_secret":identity.registration_secret,"room_keys":keys});
                    let encrypted =
                        crate::recovery::encrypt(&serde_json::to_vec(&bundle)?, pass.trim())?;
                    private_write(&path, &encrypted)?;
                    Ok(
                        json!({"path":path,"format":"zavliq-recovery-v1-age","includes_identity":true,"next_action":"Store this encrypted bundle separately from its passphrase. Restore into a fresh data directory; verification is required for the new device."}),
                    )
                } else {
                    if self.store.pairing()?.is_some() {
                        bail!("PAIRING_IN_PROGRESS: complete pairing or restore recovery into a different fresh data directory");
                    }
                    let raw = std::fs::read(&path)?;
                    if raw.len() > 100 * 1024 * 1024 {
                        bail!("RECOVERY_TOO_LARGE: recovery bundle limit is 100 MiB");
                    }
                    let decrypted = crate::recovery::decrypt(&raw, pass.trim())?;
                    let bundle: Value = serde_json::from_slice(&decrypted)?;
                    if bundle["format"] != "zavliq-recovery-v1" {
                        bail!("INVALID_RECOVERY: unsupported bundle format");
                    }
                    let handle = required(&bundle, "handle")?;
                    let control = required(&bundle, "control_url")?;
                    safe_url(control)?;
                    let mut identity = if let Some(existing) = self.store.identity()? {
                        if existing.handle != handle
                            || existing.control_url != control
                            || existing.recovery_id.is_none()
                        {
                            bail!("IDENTITY_EXISTS: restore into a fresh private data directory");
                        }
                        existing
                    } else {
                        let identity = Identity {
                            handle: handle.into(),
                            control_url: control.into(),
                            registration_secret: required(&bundle, "registration_secret")?.into(),
                            store_passphrase: secret(),
                            recovery_id: Some(secret()),
                            session: None,
                        };
                        self.store.save_identity(&identity)?;
                        identity
                    };
                    if identity.session.is_none() {
                        let response=self.request(reqwest::Method::POST,format!("{}/v1/agents/recover",control.trim_end_matches('/')),Some(json!({"handle":handle,"registration_secret":identity.registration_secret,"recovery_id":identity.recovery_id,"device_display_name":"Recovered Zavliq agent"})),false).await?;
                        let session: Session = serde_json::from_value(response)?;
                        safe_url(&session.homeserver)?;
                        identity.session = Some(session);
                        self.store.save_identity(&identity)?;
                    }
                    let client = self.client().await?;
                    private_write(&temporary, required(&bundle, "room_keys")?.as_bytes())?;
                    let imported = client
                        .encryption()
                        .import_room_keys(temporary.clone(), pass.trim())
                        .await;
                    let _ = std::fs::remove_file(&temporary);
                    let imported = imported?;
                    let session = self.session()?;
                    Ok(
                        json!({"user_id":session.user_id,"device_id":session.device_id,"imported":imported.imported_count,"total":imported.total_count,"next_action":"Verify this new device from an authenticated existing client before receiving new encrypted messages. Sync to restore retained server history."}),
                    )
                }
            }
            _ => bail!("METHOD_NOT_FOUND: unknown operation; run zavliq methods"),
        }
    }
}
