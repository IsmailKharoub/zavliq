use anyhow::{Context, Result, bail};
use fs2::FileExt;
use rusqlite::{Connection, OptionalExtension, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    fs::{self, File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
};

#[derive(Serialize, Deserialize)]
pub struct Identity {
    pub handle: String,
    pub control_url: String,
    pub registration_secret: String,
    pub store_passphrase: String,
    pub session: Option<Session>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub recovery_id: Option<String>,
}
#[derive(Serialize, Deserialize, Clone)]
pub struct Session {
    pub user_id: String,
    pub device_id: String,
    pub access_token: String,
    pub homeserver: String,
}

#[derive(Serialize, Deserialize)]
pub struct Pairing {
    pub user_id: String,
    pub control_url: String,
    pub pairing_id: String,
    pub pairing_secret: String,
    pub confirmation_code: String,
    pub expires_at: u64,
    pub store_passphrase: String,
    pub session: Option<Session>,
}

pub struct Store {
    pub path: PathBuf,
    pub db: Connection,
    _lock: File,
}

pub fn private_dir(path: &Path) -> Result<()> {
    fs::create_dir_all(path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

pub fn private_write(path: &Path, bytes: &[u8]) -> Result<()> {
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let temp = parent.join(format!(".zavliq-{}.tmp", uuid::Uuid::new_v4()));
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = options.open(&temp)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    fs::rename(&temp, path)?;
    File::open(parent)?.sync_all()?;
    Ok(())
}

fn redact_media_keys(content: &mut Value) {
    for pointer in ["/file", "/info/thumbnail_file"] {
        if let Some(file) = content.pointer_mut(pointer) {
            if file.get("key").is_some() {
                let url = file.get("url").cloned().unwrap_or(Value::Null);
                *file = json!({"url":url,"encrypted":true});
            }
        }
    }
    if let Some(edited) = content.get_mut("m.new_content") {
        redact_media_keys(edited);
    }
}

impl Store {
    pub fn open(path: PathBuf) -> Result<Self> {
        private_dir(&path)?;
        let lock = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(path.join("runtime.lock"))?;
        lock.try_lock_exclusive().context("IDENTITY_BUSY: another runtime owns this identity; use its SDK connection or stop it first")?;
        let db = Connection::open(path.join("inbox.sqlite3"))?;
        db.execute_batch("PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON;
          CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL, room_id TEXT NOT NULL, sender TEXT NOT NULL, content TEXT NOT NULL, ts INTEGER NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0);
          CREATE INDEX IF NOT EXISTS events_room_seq ON events(room_id,seq);
          CREATE TABLE IF NOT EXISTS outbox(txn TEXT PRIMARY KEY, room_id TEXT NOT NULL, content TEXT NOT NULL, event_id TEXT);
          CREATE TABLE IF NOT EXISTS blocked_senders(user_id TEXT PRIMARY KEY);
          CREATE TABLE IF NOT EXISTS receipts(event_id TEXT NOT NULL, sender TEXT NOT NULL, status TEXT NOT NULL, PRIMARY KEY(event_id,sender,status));
          CREATE TABLE IF NOT EXISTS gaps(room_id TEXT PRIMARY KEY, prev_batch TEXT, observed_at INTEGER NOT NULL);")?;
        Ok(Self {
            path,
            db,
            _lock: lock,
        })
    }
    pub fn identity(&self) -> Result<Option<Identity>> {
        let path = self.path.join("identity.json");
        if !path.exists() {
            return Ok(None);
        }
        Ok(Some(serde_json::from_slice(&fs::read(path)?)?))
    }
    pub fn save_identity(&self, identity: &Identity) -> Result<()> {
        private_write(
            &self.path.join("identity.json"),
            &serde_json::to_vec(identity)?,
        )
    }
    pub fn pairing(&self) -> Result<Option<Pairing>> {
        let path = self.path.join("pairing.json");
        if !path.exists() {
            return Ok(None);
        }
        Ok(Some(serde_json::from_slice(&fs::read(path)?)?))
    }
    pub fn save_pairing(&self, pairing: &Pairing) -> Result<()> {
        private_write(
            &self.path.join("pairing.json"),
            &serde_json::to_vec(pairing)?,
        )
    }
    pub fn clear_pairing(&self) -> Result<()> {
        let path = self.path.join("pairing.json");
        if path.exists() {
            fs::remove_file(path)?;
            File::open(&self.path)?.sync_all()?;
        }
        Ok(())
    }
    pub fn cursor(&self) -> Result<Option<String>> {
        Ok(self
            .db
            .query_row("SELECT value FROM metadata WHERE key='sync'", [], |row| {
                row.get(0)
            })
            .optional()?)
    }
    pub fn commit_sync(
        &mut self,
        cursor: &str,
        events: &[(String, Value)],
        gaps: &[(String, Option<String>)],
    ) -> Result<usize> {
        self.commit_batch(cursor, events, gaps, None)
    }
    pub fn commit_history(
        &mut self,
        cursor: &str,
        events: &[(String, Value)],
        room: &str,
        next: Option<&str>,
    ) -> Result<usize> {
        self.commit_batch(cursor, events, &[], Some((room, next)))
    }
    fn commit_batch(
        &mut self,
        cursor: &str,
        events: &[(String, Value)],
        gaps: &[(String, Option<String>)],
        history: Option<(&str, Option<&str>)>,
    ) -> Result<usize> {
        let tx = self.db.transaction()?;
        let mut inserted = 0;
        for (room_id, event) in events {
            if event["type"] == "com.zavliq.receipt" {
                if let (Some(target), Some(sender), Some(status)) = (
                    event["content"]["event_id"].as_str(),
                    event["sender"].as_str(),
                    event["content"]["status"].as_str(),
                ) {
                    if matches!(status, "delivered" | "read") {
                        tx.execute(
                            "INSERT OR IGNORE INTO receipts(event_id,sender,status) VALUES(?,?,?)",
                            params![target, sender, status],
                        )?;
                    }
                }
                continue;
            }
            if let (Some(id), Some(sender), Some(content)) = (
                event["event_id"].as_str(),
                event["sender"].as_str(),
                event.get("content"),
            ) {
                if !matches!(
                    event["type"].as_str(),
                    Some("m.room.message" | "m.room.encrypted")
                ) {
                    continue;
                }
                inserted += tx.execute("INSERT INTO events(event_id,room_id,sender,content,ts) VALUES(?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET content=excluded.content,seq=excluded.seq WHERE json_extract(events.content,'$.algorithm') IS NOT NULL AND json_extract(excluded.content,'$.algorithm') IS NULL", params![id,room_id,sender,serde_json::to_string(content)?,event["origin_server_ts"].as_i64().unwrap_or(0)])?;
            }
        }
        for (room, prev) in gaps {
            tx.execute("INSERT INTO gaps(room_id,prev_batch,observed_at) VALUES(?,?,unixepoch()) ON CONFLICT(room_id) DO UPDATE SET prev_batch=excluded.prev_batch,observed_at=excluded.observed_at", params![room,prev])?;
        }
        tx.execute("INSERT INTO metadata(key,value) VALUES('sync',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", [cursor])?;
        if let Some((room, next)) = history {
            if let Some(next) = next {
                tx.execute(
                    "UPDATE gaps SET prev_batch=? WHERE room_id=?",
                    params![next, room],
                )?;
            } else {
                tx.execute("DELETE FROM gaps WHERE room_id=?", [room])?;
            }
        }
        tx.commit()?;
        Ok(inserted)
    }
    pub fn inbox(
        &self,
        after: i64,
        limit: usize,
        room: Option<&str>,
        full: bool,
        include_sent: bool,
    ) -> Result<Value> {
        let own_user = if include_sent {
            None
        } else {
            self.identity()?.and_then(|i| i.session).map(|s| s.user_id)
        };
        let mut stmt = self.db.prepare("SELECT seq,event_id,room_id,sender,content,ts,acknowledged FROM events WHERE seq>? AND (? IS NULL OR room_id=?) AND (? IS NULL OR sender<>?) AND sender NOT IN (SELECT user_id FROM blocked_senders) ORDER BY seq LIMIT ?")?;
        let records = stmt.query_map(
            params![
                after,
                room,
                room,
                own_user,
                own_user,
                (limit.clamp(1, 100) + 1) as i64
            ],
            |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                    r.get::<_, String>(4)?,
                    r.get::<_, i64>(5)?,
                    r.get::<_, bool>(6)?,
                ))
            },
        )?;
        let mut items = Vec::new();
        for row in records {
            let (seq, event_id, room_id, sender, raw, ts, ack) = row?;
            let mut content: Value = serde_json::from_str(&raw)?;
            if let Some(encoded) = content["com.zavliq.data_json"].as_str() {
                if let Ok(data) = serde_json::from_str::<Value>(encoded) {
                    content["com.zavliq.data"] = data;
                }
            }
            redact_media_keys(&mut content);
            if content["algorithm"].is_string() {
                content = json!({"algorithm":content["algorithm"],"unavailable":"keys_missing"});
            }
            let mut item = json!({"cursor":seq,"event_id":event_id,"room_id":room_id,"sender":sender,"timestamp":ts,"acknowledged":ack,"untrusted":true});
            if full {
                if let Some(data) = content.get("com.zavliq.data") {
                    item["data"] = data.clone();
                }
                if let Some(encoded) = content.get("com.zavliq.data_json") {
                    item["data_json"] = encoded.clone();
                }
                item["content"] = content;
            } else {
                item["preview"] = json!(
                    content["body"]
                        .as_str()
                        .unwrap_or(if content["algorithm"].is_string() {
                            "[encrypted: keys unavailable]"
                        } else {
                            "[structured event]"
                        })
                        .chars()
                        .take(160)
                        .collect::<String>()
                );
                if content["msgtype"].as_str() == Some("m.file") {
                    item["attachment"] = json!(true);
                }
            }
            items.push(item);
        }
        let has_more = items.len() > limit.clamp(1, 100);
        items.truncate(limit.clamp(1, 100));
        let mut next = items
            .last()
            .map(|v| v["cursor"].as_i64().unwrap())
            .unwrap_or(after);
        if !has_more {
            let high_water: i64 = self.db.query_row(
                "SELECT COALESCE(MAX(seq),0) FROM events WHERE (? IS NULL OR room_id=?)",
                params![room, room],
                |r| r.get(0),
            )?;
            next = next.max(high_water);
        }
        let gaps: Vec<String> = self
            .db
            .prepare("SELECT room_id FROM gaps")?
            .query_map([], |row| row.get(0))?
            .collect::<std::result::Result<_, _>>()?;
        Ok(json!({"items":items,"next_cursor":next,"has_more":has_more,"history_gap_rooms":gaps}))
    }
    pub fn set_blocks(&mut self, users: &[String]) -> Result<()> {
        let tx = self.db.transaction()?;
        tx.execute("DELETE FROM blocked_senders", [])?;
        for user in users {
            tx.execute(
                "INSERT OR IGNORE INTO blocked_senders(user_id) VALUES(?)",
                [user],
            )?;
        }
        tx.commit()?;
        Ok(())
    }
    pub fn delivery(&self, event_id: &str) -> Result<Value> {
        let receipts = self
            .db
            .prepare("SELECT sender,status FROM receipts WHERE event_id=?")?
            .query_map([event_id], |r| {
                Ok(json!({"user_id":r.get::<_,String>(0)?,"status":r.get::<_,String>(1)?}))
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        let accepted = self
            .db
            .query_row("SELECT 1 FROM outbox WHERE event_id=?", [event_id], |r| {
                r.get::<_, i64>(0)
            })
            .optional()?
            .is_some();
        Ok(
            json!({"event_id":event_id,"accepted":accepted,"receipts":receipts,"meaning":"Receipts confirm client delivery or explicit reading, never task completion."}),
        )
    }
    pub fn event_content(&self, event_id: &str) -> Result<(String, Value)> {
        let (room, raw): (String, String) = self
            .db
            .query_row(
                "SELECT room_id,content FROM events WHERE event_id=? AND sender NOT IN (SELECT user_id FROM blocked_senders)",
                [event_id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .context("EVENT_NOT_LOCAL: synchronize first or check event ID")?;
        Ok((room, serde_json::from_str(&raw)?))
    }
    pub fn enqueue(&self, txn: &str, room: &str, content: &Value) -> Result<Option<String>> {
        let serialized = serde_json::to_string(content)?;
        if let Some((old_room, old_content, event)) = self
            .db
            .query_row(
                "SELECT room_id,content,event_id FROM outbox WHERE txn=?",
                [txn],
                |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, Option<String>>(2)?,
                    ))
                },
            )
            .optional()?
        {
            if old_room != room || old_content != serialized {
                bail!("IDEMPOTENCY_CONFLICT: reuse a key only with the same room and message");
            }
            return Ok(event);
        }
        self.db.execute(
            "INSERT INTO outbox(txn,room_id,content) VALUES(?,?,?)",
            params![txn, room, serialized],
        )?;
        Ok(None)
    }
    pub fn sent(&self, txn: &str, event: &str) -> Result<()> {
        self.db.execute(
            "UPDATE outbox SET event_id=? WHERE txn=?",
            params![event, txn],
        )?;
        Ok(())
    }
    pub fn pending(&self) -> Result<Vec<(String, String, Value)>> {
        let rows = self
            .db
            .prepare(
                "SELECT txn,room_id,content FROM outbox WHERE event_id IS NULL ORDER BY rowid",
            )?
            .query_map([], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                ))
            })?
            .collect::<std::result::Result<Vec<_>, _>>()?;
        rows.into_iter()
            .map(|(t, r, c)| Ok((t, r, serde_json::from_str(&c)?)))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn store_locks_and_releases_identity() {
        let dir = tempfile::tempdir().unwrap();
        let first = Store::open(dir.path().to_owned()).unwrap();
        assert!(Store::open(dir.path().to_owned()).is_err());
        drop(first);
        assert!(Store::open(dir.path().to_owned()).is_ok());
    }
    #[test]
    fn sync_deduplicates_and_commits_cursor_atomically() {
        let dir = tempfile::tempdir().unwrap();
        let mut s = Store::open(dir.path().to_owned()).unwrap();
        let event = json!({"event_id":"$one","type":"m.room.message","sender":"@peer:test","content":{"body":"hello","msgtype":"m.text"},"origin_server_ts":10});
        s.commit_sync("a", &[("!r:test".into(), event.clone())], &[])
            .unwrap();
        s.commit_sync("b", &[("!r:test".into(), event)], &[])
            .unwrap();
        assert_eq!(
            s.inbox(0, 10, None, false, false).unwrap()["items"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        assert_eq!(s.cursor().unwrap().as_deref(), Some("b"));
        drop(s);
        let s = Store::open(dir.path().to_owned()).unwrap();
        assert_eq!(
            s.inbox(0, 10, None, true, false).unwrap()["items"][0]["content"]["body"],
            "hello"
        );
    }
    #[test]
    fn late_decryption_reappears_after_cursor_without_duplicate_event() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = Store::open(dir.path().to_owned()).unwrap();
        let encrypted = json!({"event_id":"$encrypted","sender":"@peer:local","type":"m.room.encrypted","content":{"algorithm":"m.megolm.v1.aes-sha2","ciphertext":"opaque"}});
        store
            .commit_sync("first", &[("!room:local".into(), encrypted)], &[])
            .unwrap();
        let cursor = store.inbox(0, 10, None, false, false).unwrap()["next_cursor"]
            .as_i64()
            .unwrap();
        let decrypted = json!({"event_id":"$encrypted","sender":"@peer:local","type":"m.room.message","content":{"body":"now readable","msgtype":"m.text"}});
        store
            .commit_sync("later", &[("!room:local".into(), decrypted)], &[])
            .unwrap();
        assert_eq!(
            store.inbox(cursor, 10, None, true, false).unwrap()["items"][0]["content"]["body"],
            "now readable"
        );
        assert_eq!(
            store.inbox(0, 10, None, true, false).unwrap()["items"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
    }
    #[test]
    fn receipt_events_do_not_enter_the_message_inbox() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = Store::open(dir.path().to_owned()).unwrap();
        let event = json!({"type":"com.zavliq.receipt","sender":"@peer:local","content":{"event_id":"$message","status":"delivered"}});
        store
            .commit_sync("receipt", &[("!room:local".into(), event)], &[])
            .unwrap();
        assert_eq!(
            store.delivery("$message").unwrap()["receipts"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
        assert!(
            store.inbox(0, 10, None, false, false).unwrap()["items"]
                .as_array()
                .unwrap()
                .is_empty()
        );
    }
    #[test]
    fn blocked_senders_are_hidden_and_cursor_advances() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = Store::open(dir.path().to_owned()).unwrap();
        let event = json!({"event_id":"$blocked","sender":"@blocked:local","type":"m.room.message","content":{"body":"hidden","msgtype":"m.text"}});
        store
            .commit_sync("s", &[("!room:local".into(), event)], &[])
            .unwrap();
        store.set_blocks(&["@blocked:local".into()]).unwrap();
        let inbox = store.inbox(0, 10, None, true, false).unwrap();
        assert!(inbox["items"].as_array().unwrap().is_empty());
        assert!(inbox["next_cursor"].as_i64().unwrap() > 0);
        assert!(store.event_content("$blocked").is_err());
        store.set_blocks(&[]).unwrap();
        assert_eq!(
            store.inbox(0, 10, None, true, false).unwrap()["items"]
                .as_array()
                .unwrap()
                .len(),
            1
        );
    }
    #[test]
    fn structured_payload_preserves_non_matrix_json_numbers() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = Store::open(dir.path().to_owned()).unwrap();
        let data: Value = serde_json::from_str(
            r#"{"confidence":0.95,"count":9007199254740993,"huge":123456789012345678901234567890}"#,
        )
        .unwrap();
        let event = json!({"event_id":"$json","sender":"@peer:local","type":"m.room.message","content":{"body":"","msgtype":"m.text","com.zavliq.data_json":serde_json::to_string(&data).unwrap()}});
        store
            .commit_sync("s", &[("!room:local".into(), event)], &[])
            .unwrap();
        assert_eq!(
            store.inbox(0, 10, None, true, false).unwrap()["items"][0]["data"],
            data
        );
    }
    #[test]
    fn encrypted_media_keys_stay_local_when_full_content_is_requested() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = Store::open(dir.path().to_owned()).unwrap();
        let event = json!({"event_id":"$file","sender":"@peer:local","type":"m.room.message","content":{"body":"document","msgtype":"m.file","file":{"url":"mxc://local/id","key":{"k":"private-file-key"},"iv":"private-iv"},"info":{"thumbnail_file":{"url":"mxc://local/thumb","key":{"k":"private-thumbnail-key"}}}}});
        store
            .commit_sync("s", &[("!room:local".into(), event)], &[])
            .unwrap();
        let result = store.inbox(0, 10, None, true, false).unwrap();
        assert!(!result.to_string().contains("private-"));
        assert_eq!(result["items"][0]["content"]["file"]["encrypted"], true);
        assert_eq!(
            store.event_content("$file").unwrap().1["file"]["key"]["k"],
            "private-file-key"
        );
    }
    #[test]
    fn idempotency_rejects_changed_payload_and_restores_result() {
        let dir = tempfile::tempdir().unwrap();
        let s = Store::open(dir.path().to_owned()).unwrap();
        let c = json!({"body":"a"});
        assert_eq!(s.enqueue("stable", "room", &c).unwrap(), None);
        assert!(s.enqueue("stable", "room", &json!({"body":"b"})).is_err());
        s.sent("stable", "event").unwrap();
        assert_eq!(
            s.enqueue("stable", "room", &c).unwrap().as_deref(),
            Some("event")
        );
    }
    #[test]
    fn inbound_pagination_skips_sent_messages_without_skipping_peer_messages() {
        let dir = tempfile::tempdir().unwrap();
        let mut store = Store::open(dir.path().to_owned()).unwrap();
        store
            .save_identity(&Identity {
                handle: "owner".into(),
                control_url: "https://example.invalid".into(),
                registration_secret: "synthetic-proof".into(),
                store_passphrase: "synthetic-local-key".into(),
                recovery_id: None,
                session: Some(Session {
                    user_id: "@owner:local".into(),
                    device_id: "dummy".into(),
                    access_token: "synthetic-token".into(),
                    homeserver: "https://example.invalid".into(),
                }),
            })
            .unwrap();
        let event = |id: &str, sender: &str| json!({"event_id":id,"sender":sender,"type":"m.room.message","content":{"body":id,"msgtype":"m.text"}});
        let messages = [
            ("$own1", "@owner:local"),
            ("$peer1", "@peer:local"),
            ("$own2", "@owner:local"),
            ("$peer2", "@peer:local"),
            ("$own3", "@owner:local"),
        ]
        .iter()
        .map(|(id, sender)| ("!room:local".into(), event(id, sender)))
        .collect::<Vec<_>>();
        store
            .commit_sync(
                "s",
                &messages,
                &[("!room:local".into(), Some("older".into()))],
            )
            .unwrap();
        let first = store.inbox(0, 1, None, true, false).unwrap();
        assert_eq!(first["items"][0]["event_id"], "$peer1");
        assert_eq!(first["has_more"], true);
        let second = store
            .inbox(first["next_cursor"].as_i64().unwrap(), 1, None, true, false)
            .unwrap();
        assert_eq!(second["items"][0]["event_id"], "$peer2");
        assert_eq!(second["has_more"], false);
        assert!(
            second["next_cursor"].as_i64().unwrap()
                > second["items"][0]["cursor"].as_i64().unwrap()
        );
        assert_eq!(second["history_gap_rooms"][0], "!room:local");
        assert_eq!(
            store.inbox(0, 10, None, true, true).unwrap()["items"]
                .as_array()
                .unwrap()
                .len(),
            5
        );
        assert!(
            store
                .inbox(
                    second["next_cursor"].as_i64().unwrap(),
                    10,
                    None,
                    false,
                    false
                )
                .unwrap()["items"]
                .as_array()
                .unwrap()
                .is_empty()
        );
    }
}
