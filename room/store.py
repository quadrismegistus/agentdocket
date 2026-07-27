"""SQLite-backed message store for the agent room.

Design constraints, in the order they mattered:

1. THE LOG IS THE RECORD, NOT A CACHE. Rows are append-only. There is no
   eviction, no TTL, no clear(). A coordination log whose entries can vanish is
   worse than no log, because a missing entry is indistinguishable from one that
   was never written.
2. MULTI-PROCESS WRITERS. Four seats across two machines write concurrently and
   none of them coordinate. WAL mode plus a short busy_timeout is what makes
   that safe; see tests/test_concurrency.py, which demonstrates it rather than
   asserting it.
3. TOTAL ORDER. `id` is a monotonic integer, so "which message came first" is
   answerable even when two seats post in the same second. Wall-clock timestamps
   are recorded but never used for ordering.
4. RETRIEVAL BY SOMEONE WHO WASN'T ADDRESSED. The expensive failure this exists
   to prevent is a note that was written, was true, and could not be found later
   because it mentioned nobody. Hence FTS over every message body regardless of
   addressing.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

DEFAULT_DB = os.path.expanduser("~/.agent-room/room.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT    NOT NULL,
    sender   TEXT    NOT NULL,
    tag      TEXT,
    body     TEXT    NOT NULL
);

-- Normalised so "everything addressed to me" is an index scan, not a LIKE.
CREATE TABLE IF NOT EXISTS mentions (
    message_id INTEGER NOT NULL REFERENCES messages(id),
    seat       TEXT    NOT NULL,
    PRIMARY KEY (message_id, seat)
);
CREATE INDEX IF NOT EXISTS idx_mentions_seat ON mentions(seat, message_id);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender, id);

-- Per-seat read position. Advancing is explicit: reading with peek=True does
-- not move it, so a seat can look without losing its place.
CREATE TABLE IF NOT EXISTS cursors (
    seat    TEXT PRIMARY KEY,
    last_id INTEGER NOT NULL DEFAULT 0
);

-- Independence claims. A seat holding an open claim on a topic is refused
-- reads of that topic until it posts. This is the one guarantee a chat app
-- cannot offer: it protects independent verification from anchoring by
-- construction rather than by asking.
CREATE TABLE IF NOT EXISTS claims (
    seat     TEXT NOT NULL,
    topic    TEXT NOT NULL,
    opened   TEXT NOT NULL,
    released TEXT,
    PRIMARY KEY (seat, topic, opened)
);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
    USING fts5(body, content='messages', content_rowid='id');

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, body) VALUES (new.id, new.body);
END;
"""


@dataclass(frozen=True)
class Message:
    id: int
    ts: str
    sender: str
    tag: str | None
    body: str
    mentions: tuple[str, ...]

    def format(self, width: int = 0) -> str:
        who = f"@{','.join(self.mentions)}" if self.mentions else ""
        head = f"[{self.id}] {self.ts} {self.sender}"
        if self.tag:
            head += f" [{self.tag}]"
        if who:
            head += f" -> {who}"
        body = self.body
        if width and len(body) > width:
            body = body[:width].rstrip() + " ..."
        return f"{head}\n{body}"


def connect(path: str = DEFAULT_DB) -> sqlite3.Connection:
    """Open the store, creating it if absent.

    WAL lets readers proceed during a write and lets writers from separate
    processes queue rather than fail. busy_timeout covers the queueing; without
    it a concurrent writer raises 'database is locked' immediately.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def post(conn: sqlite3.Connection, sender: str, body: str,
         mentions: Sequence[str] = (), tag: str | None = None) -> int:
    """Append one message. Returns its id, which is its position in the total order."""
    if not sender:
        raise ValueError("sender is required: an unsigned message is not a record")
    if not body.strip():
        raise ValueError("empty body")
    seats = tuple(dict.fromkeys(m.lstrip("@") for m in mentions if m.strip()))
    with conn:
        cur = conn.execute(
            "INSERT INTO messages (ts, sender, tag, body) VALUES (?,?,?,?)",
            (_now(), sender, tag, body),
        )
        mid = cur.lastrowid
        conn.executemany(
            "INSERT OR IGNORE INTO mentions (message_id, seat) VALUES (?,?)",
            [(mid, s) for s in seats],
        )
    return mid


def _hydrate(conn: sqlite3.Connection, rows: Iterable[sqlite3.Row]) -> list[Message]:
    rows = list(rows)
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    qs = ",".join("?" * len(ids))
    men: dict[int, list[str]] = {}
    for m in conn.execute(
        f"SELECT message_id, seat FROM mentions WHERE message_id IN ({qs}) ORDER BY seat", ids
    ):
        men.setdefault(m["message_id"], []).append(m["seat"])
    return [
        Message(r["id"], r["ts"], r["sender"], r["tag"], r["body"],
                tuple(men.get(r["id"], ())))
        for r in rows
    ]


def read(conn: sqlite3.Connection, seat: str, *, mentions_only: bool = False,
         limit: int | None = None, peek: bool = False,
         topic: str | None = None) -> list[Message]:
    """Messages since this seat's cursor.

    Refuses if the seat holds an open independence claim on `topic`. The refusal
    is the point: a seat asked to verify something independently must post its
    own answer before it can see anyone else's.
    """
    if topic and open_claims(conn, seat, topic):
        raise PermissionError(
            f"{seat} holds an open independence claim on '{topic}'. "
            "Post your own finding before reading others'."
        )
    last = conn.execute(
        "SELECT last_id FROM cursors WHERE seat=?", (seat,)
    ).fetchone()
    last_id = last["last_id"] if last else 0

    sql = ("SELECT m.* FROM messages m JOIN mentions x ON x.message_id=m.id "
           "WHERE m.id > ? AND x.seat = ? ORDER BY m.id") if mentions_only else \
          "SELECT * FROM messages WHERE id > ? ORDER BY id"
    args = (last_id, seat) if mentions_only else (last_id,)
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, args).fetchall()
    msgs = _hydrate(conn, rows)

    if msgs and not peek:
        with conn:
            conn.execute(
                "INSERT INTO cursors (seat, last_id) VALUES (?,?) "
                "ON CONFLICT(seat) DO UPDATE SET last_id=excluded.last_id",
                (seat, msgs[-1].id),
            )
    return msgs


def tail(conn: sqlite3.Connection, n: int = 20) -> list[Message]:
    rows = conn.execute(
        "SELECT * FROM (SELECT * FROM messages ORDER BY id DESC LIMIT ?) ORDER BY id", (n,)
    ).fetchall()
    return _hydrate(conn, rows)


def search(conn: sqlite3.Connection, query: str, limit: int = 50) -> list[Message]:
    """Full text over every body, regardless of who was addressed.

    This is the function that exists because a true, correctly-recorded note was
    once unfindable by the seats that needed it: it mentioned no one.
    """
    rows = conn.execute(
        "SELECT m.* FROM messages_fts f JOIN messages m ON m.id=f.rowid "
        "WHERE messages_fts MATCH ? ORDER BY m.id DESC LIMIT ?", (query, limit)
    ).fetchall()
    return _hydrate(conn, list(reversed(rows)))


def claim(conn: sqlite3.Connection, seat: str, topic: str) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO claims (seat, topic, opened, released) "
                     "VALUES (?,?,?,NULL)", (seat, topic, _now()))


def release(conn: sqlite3.Connection, seat: str, topic: str) -> None:
    with conn:
        conn.execute("UPDATE claims SET released=? WHERE seat=? AND topic=? "
                     "AND released IS NULL", (_now(), seat, topic))


def open_claims(conn: sqlite3.Connection, seat: str, topic: str | None = None) -> list[str]:
    sql = "SELECT topic FROM claims WHERE seat=? AND released IS NULL"
    args: list[str] = [seat]
    if topic:
        sql += " AND topic=?"
        args.append(topic)
    return [r["topic"] for r in conn.execute(sql, args)]


def stats(conn: sqlite3.Connection) -> dict:
    n = conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
    by = {r["sender"]: r["c"] for r in conn.execute(
        "SELECT sender, COUNT(*) c FROM messages GROUP BY sender ORDER BY c DESC")}
    cur = {r["seat"]: r["last_id"] for r in conn.execute("SELECT * FROM cursors")}
    return {"messages": n, "by_sender": by, "cursors": cur,
            "first": conn.execute("SELECT MIN(ts) t FROM messages").fetchone()["t"],
            "last": conn.execute("SELECT MAX(ts) t FROM messages").fetchone()["t"]}
