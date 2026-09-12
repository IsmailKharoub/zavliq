"""Single-process durable policy metadata; never stores message bodies or access tokens."""
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class PolicyStore:
    def __init__(self, path, clock=time.time):
        self.clock = clock
        if path != ':memory:':
            Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        if path != ':memory:':
            os.chmod(path, 0o600)
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          PRAGMA busy_timeout=5000;
          CREATE TABLE IF NOT EXISTS blocks (owner TEXT NOT NULL, target TEXT NOT NULL, PRIMARY KEY(owner,target));
          CREATE TABLE IF NOT EXISTS profiles (owner TEXT PRIMARY KEY, directory_visible INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS contacts (sender TEXT NOT NULL, recipient TEXT NOT NULL, room TEXT NOT NULL, status TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY(sender,recipient));
          CREATE TABLE IF NOT EXISTS trusted_contacts (first_user TEXT NOT NULL, second_user TEXT NOT NULL, PRIMARY KEY(first_user,second_user));
          CREATE TABLE IF NOT EXISTS counters (category TEXT NOT NULL, owner TEXT NOT NULL, bucket INTEGER NOT NULL, used INTEGER NOT NULL, PRIMARY KEY(category,owner,bucket));
          CREATE TABLE IF NOT EXISTS accounted_events (event_id TEXT PRIMARY KEY, at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS member_slots (room TEXT NOT NULL, member TEXT NOT NULL, reserved_at REAL NOT NULL DEFAULT 0, PRIMARY KEY(room,member));
          CREATE TABLE IF NOT EXISTS room_kinds (room TEXT PRIMARY KEY, kind TEXT NOT NULL, encryption TEXT NOT NULL);
        ''')
        self.db.execute("INSERT OR IGNORE INTO trusted_contacts(first_user,second_user) SELECT min(sender,recipient),max(sender,recipient) FROM contacts WHERE status='accepted'")
        if 'reserved_at' not in {row['name'] for row in self.db.execute('PRAGMA table_info(member_slots)')}:
            self.db.execute('ALTER TABLE member_slots ADD COLUMN reserved_at REAL NOT NULL DEFAULT 0')

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def blocked(self, a, b):
        return bool(self.db.execute('SELECT 1 FROM blocks WHERE (owner=? AND target=?) OR (owner=? AND target=?)', (a,b,b,a)).fetchone())

    def block(self, owner, target):
        self.db.execute('INSERT OR IGNORE INTO blocks(owner,target) VALUES (?,?)', (owner,target))

    def unblock(self, owner, target):
        self.db.execute('DELETE FROM blocks WHERE owner=? AND target=?', (owner,target))

    def blocks(self, owner):
        return [r['target'] for r in self.db.execute('SELECT target FROM blocks WHERE owner=? ORDER BY target LIMIT 1000', (owner,))]

    def consume(self, category, owner, event_id, windows):
        """Atomically consume all windows once. Retries cannot double-count an event."""
        now = self.clock()
        with self.transaction():
            self.db.execute('DELETE FROM accounted_events WHERE at < ?', (now-172800,))
            if self.db.execute('SELECT 1 FROM accounted_events WHERE event_id=?', (event_id,)).fetchone():
                return True
            counts = []
            for seconds, limit in windows:
                bucket = int(now // seconds)
                key = f'{category}:{seconds}'
                self.db.execute('DELETE FROM counters WHERE category=? AND bucket < ?', (key,bucket-1))
                row = self.db.execute('SELECT used FROM counters WHERE category=? AND owner=? AND bucket=?', (key,owner,bucket)).fetchone()
                used = row['used'] if row else 0
                if used >= limit:
                    return False
                counts.append((key,bucket))
            for key,bucket in counts:
                self.db.execute('INSERT INTO counters(category,owner,bucket,used) VALUES (?,?,?,1) ON CONFLICT(category,owner,bucket) DO UPDATE SET used=used+1', (key,owner,bucket))
            self.db.execute('INSERT INTO accounted_events(event_id,at) VALUES (?,?)', (event_id,now))
            return True

    def contact_allowed(self, sender, recipient, room, limit=5):
        """A contact request is an invite. Keep one pending request per directed pair."""
        if self.blocked(sender,recipient):
            return False
        now = self.clock()
        with self.transaction():
            # A native Matrix invite persists until accepted/rejected. Do not expire only
            # its metadata and accidentally allow a second simultaneous request.
            first,second=sorted((sender,recipient))
            known = self.db.execute('SELECT 1 FROM trusted_contacts WHERE first_user=? AND second_user=?', (first,second)).fetchone()
            if known:
                return True
            previous = self.db.execute('SELECT * FROM contacts WHERE sender=? AND recipient=?', (sender,recipient)).fetchone()
            if previous and previous['status']=='pending':
                return previous['room']==room
            if previous and previous['status']=='rejected' and previous['at']>now-86400:
                return False
            if not known:
                bucket=int(now//86400)
                self.db.execute("DELETE FROM counters WHERE category='contacts:86400' AND bucket < ?",(bucket-1,))
                row=self.db.execute("SELECT used FROM counters WHERE category='contacts:86400' AND owner=? AND bucket=?",(sender,bucket)).fetchone()
                if row and row['used']>=limit:
                    return False
                self.db.execute("INSERT INTO counters(category,owner,bucket,used) VALUES ('contacts:86400',?,?,1) ON CONFLICT(category,owner,bucket) DO UPDATE SET used=used+1",(sender,bucket))
            self.db.execute("INSERT INTO contacts(sender,recipient,room,status,at) VALUES (?,?,?,'pending',?) ON CONFLICT(sender,recipient) DO UPDATE SET room=excluded.room,status='pending',at=excluded.at", (sender,recipient,room,now))
            return True

    def contact_membership(self, user, room, membership):
        if membership=='join':
            with self.transaction():
                rows=self.db.execute("SELECT sender,recipient FROM contacts WHERE recipient=? AND room=? AND status='pending'",(user,room)).fetchall()
                for row in rows:
                    first,second=sorted((row['sender'],row['recipient']))
                    self.db.execute('INSERT OR IGNORE INTO trusted_contacts(first_user,second_user) VALUES (?,?)',(first,second))
                self.db.execute("UPDATE contacts SET status='accepted',at=? WHERE recipient=? AND room=? AND status='pending'",(self.clock(),user,room))
        elif membership in ('leave','ban'):
            self.db.execute("UPDATE contacts SET status='rejected',at=? WHERE recipient=? AND room=? AND status='pending'",(self.clock(),user,room))
            self.db.execute('DELETE FROM member_slots WHERE room=? AND member=?',(room,user))

    def reserve_member(self, room, user, present, limit):
        with self.transaction():
            now=self.clock()
            for row in self.db.execute('SELECT member FROM member_slots WHERE room=? AND reserved_at<?',(room,now-120)).fetchall():
                if row['member'] not in present:
                    self.db.execute('DELETE FROM member_slots WHERE room=? AND member=?',(room,row['member']))
            for member in present:
                self.db.execute('INSERT OR IGNORE INTO member_slots(room,member,reserved_at) VALUES (?,?,?)',(room,member,now))
            if self.db.execute('SELECT 1 FROM member_slots WHERE room=? AND member=?',(room,user)).fetchone():
                return True
            row=self.db.execute('SELECT count(*) AS n FROM member_slots WHERE room=?',(room,)).fetchone()
            if row['n']>=limit:
                return False
            self.db.execute('INSERT INTO member_slots(room,member,reserved_at) VALUES (?,?,?)',(room,user,now))
            return True

    def usage(self, owner):
        now=self.clock()
        result={}
        for category,seconds,limit in [('messages',86400,1000),('messages',60,30),('contacts',86400,5),('receipts',86400,3000)]:
            bucket=int(now//seconds)
            row=self.db.execute('SELECT used FROM counters WHERE category=? AND owner=? AND bucket=?',(f'{category}:{seconds}',owner,bucket)).fetchone()
            result[f'{category}_per_{"day" if seconds==86400 else "minute"}']={'used':row['used'] if row else 0,'limit':limit,'resets_at_ms':(bucket+1)*seconds*1000}
        return result
