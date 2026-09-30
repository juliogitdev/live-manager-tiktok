"""Single-process SQLite journal. Commit before exposing events to consumers.

WAL + FULL protects committed writes on a persistent filesystem. No durability is
claimed for an ephemeral host filesystem, nor for events never received from TikTok.
"""
import json
import sqlite3
import time
from pathlib import Path


class EventStore:
    def __init__(self, path, retention_seconds=86400):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.retention_seconds = retention_seconds
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS sessions (
                sid TEXT PRIMARY KEY, username TEXT NOT NULL, snapshot TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                sid TEXT NOT NULL REFERENCES sessions(sid) ON DELETE CASCADE,
                seq INTEGER NOT NULL, id TEXT NOT NULL, origin TEXT,
                timestamp INTEGER NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(sid, seq), UNIQUE(sid, origin));
            CREATE INDEX IF NOT EXISTS event_time ON events(timestamp);
            CREATE TABLE IF NOT EXISTS consumers (
                sid TEXT NOT NULL REFERENCES sessions(sid) ON DELETE CASCADE,
                consumer TEXT NOT NULL, ack INTEGER NOT NULL, delivered INTEGER NOT NULL,
                last_seen INTEGER NOT NULL, gaps INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(sid, consumer));
            CREATE TABLE IF NOT EXISTS participants (
                sid TEXT NOT NULL REFERENCES sessions(sid) ON DELETE CASCADE,
                identity TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(sid, identity));
        ''')

    def save(self, session, event=None, origin=None):
        snap = session.snapshot()
        for key in ("last_safety_event", "last_monitor_event"):
            if snap.get(key):
                snap[key] = {**snap[key], "data": {}}
        with self.db:
            self.db.execute("INSERT INTO sessions VALUES(?,?,?) ON CONFLICT(sid) DO UPDATE SET snapshot=excluded.snapshot",
                            (session.session_id, session.username.casefold(), json.dumps(snap)))
            if event:
                user = (event.get("data") or {}).get("user") or {}
                identity = str(user.get("user_id") or user.get("unique_id") or "")
                if identity and identity in session.participants:
                    self.db.execute("INSERT INTO participants VALUES(?,?,?) ON CONFLICT(sid,identity) DO UPDATE SET payload=excluded.payload",
                                    (session.session_id, identity, json.dumps(session.participants[identity])))
                # Raw diagnostics stay in the bounded admin buffer. The durable journal
                # stores compact control markers so diagnostics cannot evict gifts by count.
                stored = dict(event)
                if event["type"] not in {"gift", "like", "comment", "share", "follow", "subscription"}:
                    stored["data"] = {}
                self.db.execute("INSERT INTO events VALUES(?,?,?,?,?,?)",
                                (session.session_id, event["seq"], event["id"], origin,
                                 event["timestamp"], json.dumps(stored)))

    def seen(self, sid, origin):
        return origin is not None and self.db.execute("SELECT 1 FROM events WHERE sid=? AND origin=?", (sid, origin)).fetchone() is not None

    def snapshots(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT snapshot FROM sessions")]

    def participants(self, sid):
        return {name: json.loads(payload) for name, payload in self.db.execute(
            "SELECT identity,payload FROM participants WHERE sid=?", (sid,))}

    def read(self, sid, after, limit=500):
        cutoff = int(time.time()*1000) - self.retention_seconds*1000
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT payload FROM events WHERE sid=? AND seq>? AND timestamp>=? ORDER BY seq LIMIT ?",
            (sid, after, cutoff, limit))]

    def oldest(self, sid, latest):
        cutoff = int(time.time()*1000) - self.retention_seconds*1000
        row = self.db.execute("SELECT MIN(seq) FROM events WHERE sid=? AND timestamp>=?", (sid, cutoff)).fetchone()
        return row[0] if row[0] is not None else latest + 1

    def consumer(self, sid, name):
        row = self.db.execute("SELECT ack,delivered,last_seen,gaps FROM consumers WHERE sid=? AND consumer=?", (sid, name)).fetchone()
        return dict(zip(("ack", "delivered", "last_seen", "gaps"), row)) if row else None

    def touch(self, sid, name, initial, max_consumers=100):
        current = self.consumer(sid, name)
        if current is None:
            count = self.db.execute("SELECT COUNT(*) FROM consumers WHERE sid=?", (sid,)).fetchone()[0]
            if count >= max_consumers:
                raise ValueError("Consumer limit reached")
        with self.db:
            self.db.execute("INSERT INTO consumers(sid,consumer,ack,delivered,last_seen) VALUES(?,?,?,?,?) "
                            "ON CONFLICT(sid,consumer) DO UPDATE SET last_seen=excluded.last_seen",
                            (sid, name, initial, initial, int(time.time()*1000)))
        return self.consumer(sid, name)

    def delivered(self, sid, name, cursor, gap=False):
        with self.db:
            self.db.execute("UPDATE consumers SET delivered=MAX(delivered,?),gaps=gaps+? WHERE sid=? AND consumer=?",
                            (cursor, int(gap), sid, name))

    def ack(self, sid, name, cursor):
        row = self.consumer(sid, name)
        if row is None or cursor > row["delivered"]:
            raise ValueError("ACK exceeds delivered cursor")
        with self.db:
            self.db.execute("UPDATE consumers SET ack=MAX(ack,?),last_seen=? WHERE sid=? AND consumer=?",
                            (cursor, int(time.time()*1000), sid, name))
        return self.consumer(sid, name)["ack"]

    def metrics(self, sid, latest):
        return [{"consumer_id": name, "ack": ack, "delivered": delivered, "last_seen": seen,
                 "lag_events": max(0, latest-ack), "gap_count": gaps}
                for name, ack, delivered, seen, gaps in self.db.execute(
                    "SELECT consumer,ack,delivered,last_seen,gaps FROM consumers WHERE sid=?", (sid,))]

    def prune(self):
        cutoff = int(time.time()*1000) - self.retention_seconds*1000
        with self.db:
            self.db.execute("DELETE FROM events WHERE timestamp<?", (cutoff,))
            self.db.execute("DELETE FROM consumers WHERE last_seen<?", (cutoff,))

    def delete(self, sid):
        with self.db:
            self.db.execute("DELETE FROM sessions WHERE sid=?", (sid,))

    def close(self):
        self.db.close()
