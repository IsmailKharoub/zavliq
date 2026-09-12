import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import time


class Capacity(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = max(1, retry_after)


class State:
    """Private processing journal, not a substitute for the native durable inbox."""

    def __init__(self, path: Path, owner: str, now=time.time):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
        self._lock = (path / "echo.lock").open("a")
        os.chmod(path / "echo.lock", 0o600)
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            raise RuntimeError("Another echo process owns this directory.") from None
        self.now = now
        self.db = sqlite3.connect(path / "echo.sqlite3")
        os.chmod(path / "echo.sqlite3", 0o600)
        self.db.executescript("""
          PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY, status TEXT NOT NULL, at INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS counters(scope TEXT NOT NULL, window INTEGER NOT NULL,
            duration INTEGER NOT NULL, used INTEGER NOT NULL, PRIMARY KEY(scope,window,duration));
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('owner',?)", (owner,))
        if self.db.execute("SELECT value FROM metadata WHERE key='owner'").fetchone()[0] != owner:
            self.close()
            raise RuntimeError("This echo journal belongs to another identity.")

    def close(self):
        self.db.close()
        self._lock.close()

    @property
    def cursor(self):
        row = self.db.execute("SELECT value FROM metadata WHERE key='cursor'").fetchone()
        return int(row[0]) if row else 0

    def reserve(self, action: str, limits: list[tuple[str, int, int]]):
        """Charge a logical action once, including ambiguous attempts across restarts."""
        now = int(self.now())
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT status FROM actions WHERE id=?", (action,)).fetchone()
            if row:
                self.db.commit()
                return row[0]
            for scope, duration, cap in limits:
                window = now // duration * duration
                row = self.db.execute("SELECT used FROM counters WHERE scope=? AND window=? AND duration=?",
                                      (scope, window, duration)).fetchone()
                if row and row[0] >= cap:
                    raise Capacity(window + duration - now)
            for scope, duration, _ in limits:
                window = now // duration * duration
                self.db.execute("INSERT INTO counters VALUES(?,?,?,1) ON CONFLICT(scope,window,duration) DO UPDATE SET used=used+1",
                                (scope, window, duration))
            self.db.execute("INSERT INTO actions VALUES(?,'prepared',?)", (action, now))
            self.db.commit()
            return "prepared"
        except BaseException:
            self.db.rollback()
            raise

    def finish(self, action: str, cursor: int | None = None):
        with self.db:
            self.db.execute("INSERT INTO actions VALUES(?,'done',?) ON CONFLICT(id) DO UPDATE SET status='done'",
                            (action, int(self.now())))
            if cursor is not None:
                self.db.execute("INSERT INTO metadata VALUES('cursor',?) ON CONFLICT(key) DO UPDATE SET value=CAST(max(CAST(value AS INTEGER),CAST(excluded.value AS INTEGER)) AS TEXT)", (str(cursor),))

    def advance(self, cursor: int):
        with self.db:
            self.db.execute("INSERT INTO metadata VALUES('cursor',?) ON CONFLICT(key) DO UPDATE SET value=CAST(max(CAST(value AS INTEGER),CAST(excluded.value AS INTEGER)) AS TEXT)", (str(cursor),))

    def prune(self):
        with self.db:
            self.db.execute("DELETE FROM counters WHERE window+duration<?", (int(self.now()) - 86400,))
        # Keep event proofs: late decryption/backfill can re-emit an old event ID.


def transaction_id(event_id: str):
    return "echo-" + hashlib.sha256(event_id.encode()).hexdigest()
