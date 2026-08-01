"""SQLite-backed message store for the agent docket.

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

DEFAULT_DB = os.path.expanduser("~/.agentdocket/docket.db")
SEAT_FILE = ".docket-seat"


class SeatUnknown(RuntimeError):
    """No seat could be resolved. Never resolved by guessing."""


def read_seat_file(path: str) -> tuple[str, set[str]]:
    """Parse a .docket-seat file into (name, flags).

    The first non-empty line is the seat. Any further non-empty lines are flags.
    Reading the WHOLE file as the name was a latent bug the moment anything was
    ever written on a second line: the seat became "registrar\nprotected", which
    matches no seat and mentions nobody.

    Flags:
      protected   refuse posts that reach this seat by inheritance from an
                  ancestor directory, unless the caller names it explicitly.
                  For seats whose posts carry authority, where the cost of a
                  message signed by accident is higher than the cost of a
                  refusal.
    """
    lines = [ln.strip() for ln in open(path).read().splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return "", set()
    return lines[0].lstrip("@"), {ln.lower() for ln in lines[1:]}


def seat_is_protected(origin: str) -> bool:
    """True if `origin` is a .docket-seat file declaring `protected`."""
    if not origin or not os.path.isfile(origin):
        return False
    try:
        return "protected" in read_seat_file(origin)[1]
    except OSError:
        return False


def resolve_seat(start: str | None = None) -> tuple[str, str]:
    """Return (seat, where it came from). Raises SeatUnknown rather than guess.

    Precedence:
      1. $DOCKET_SEAT
      2. the nearest .docket-seat file, searching upward from `start`

    The distinction that matters, and the whole reason this is a function rather
    than a basename() call: a `.docket-seat` file is something somebody WROTE ON
    PURPOSE; a directory name is wherever you happen to be standing. A sibling
    tool derived identity from the working directory, mis-signed messages
    whenever a session had cd'd elsewhere, and one mis-signed message propagated
    into an unauthorised merge.

    Searching upward means a nested project inherits its parent's seat unless it
    declares its own. That is convenient and it is also a trap wherever one
    seat's directory sits inside another's, so the resolved seat is always
    reported together with the file it came from, and `docket whoami` exists to
    be run before the first post of a session.
    """
    env = os.environ.get("DOCKET_SEAT", "").lstrip("@").strip()
    if env:
        return env, "$DOCKET_SEAT"

    d = os.path.abspath(start or os.getcwd())
    while True:
        p = os.path.join(d, SEAT_FILE)
        if os.path.isfile(p):
            name, _ = read_seat_file(p)
            if name:
                return name, p
        parent = os.path.dirname(d)
        if parent == d:
            raise SeatUnknown(
                "no seat identity.\n"
                f"  Set $DOCKET_SEAT, or run `docket init <name>` to write a "
                f"{SEAT_FILE} file here.\n"
                "  This is never inferred from the directory name: a guessed "
                "identity signs as somebody else."
            )
        d = parent


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

-- Seats the store has ever SEEN, which is not the same as seats that have
-- SPOKEN. A seat is recorded here the moment it posts, reads, or starts a
-- watcher, so it is known from the instant it arrives rather than from its
-- first message. Keying the unknown-mention warning on senders instead made it
-- fire on every seat's first inbound mention -- the one moment the address is
-- most likely correct and most consequential -- and a false alarm on the common
-- case teaches people to ignore the alarm.
CREATE TABLE IF NOT EXISTS seats (
    seat       TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL
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
    touch_seat(conn, sender)
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


def touch_seat(conn: sqlite3.Connection, seat: str) -> None:
    """Record that this seat exists. Called on post, on read, and on watch start.

    Presence, not speech. A seat that has loaded the plugin and read once is as
    real as one that has posted, and addressing it is correct.
    """
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO seats (seat, first_seen, last_seen) VALUES (?,?,?) "
            "ON CONFLICT(seat) DO UPDATE SET last_seen=excluded.last_seen",
            (seat, now, now))


def known_seats(conn: sqlite3.Connection) -> set[str]:
    """Every seat the store has seen arrive OR speak.

    The union matters for backward compatibility: stores written before the
    seats table existed have senders and no presence records, and a seat that
    posted is obviously real.
    """
    return ({r["seat"] for r in conn.execute("SELECT seat FROM seats")} |
            {r["sender"] for r in conn.execute("SELECT DISTINCT sender FROM messages")})


def unknown_mentions(conn: sqlite3.Connection, mentions: Sequence[str]) -> list[str]:
    """Mention targets THE STORE HAS NEVER SEEN -- neither posting, nor reading,
    nor running a watcher.

    Not "never posted". That was the rule until b7e0a27 and it was wrong: it
    fired on every seat's first inbound mention, which is when an address is
    most likely correct. A seat that has read is present and addressable.

    WHAT THIS CANNOT DISTINGUISH, and the caller must not claim otherwise: a
    typo, and a real seat that has not yet touched this store at all. Both look
    identical from here, and the second is not rare -- during a new docket's
    first hour it is the common case, because nobody has arrived yet. So the
    warning built on this must state a fact ("not known to this docket") and
    never a consequence ("reaches nobody"), which would be false in the second
    branch: reads are not filtered by mention, so a seat that shows up later
    receives the message by cursor regardless.

    Warns, never blocks.
    """
    seen = known_seats(conn)
    return [m for m in dict.fromkeys(x.lstrip("@") for x in mentions if x.strip())
            if m not in seen]


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


def head_id(conn: sqlite3.Connection) -> int:
    """The newest message id in the docket. 0 if empty."""
    return conn.execute("SELECT COALESCE(MAX(id), 0) m FROM messages").fetchone()["m"]


def cursor_of(conn: sqlite3.Connection, seat: str) -> int:
    row = conn.execute("SELECT last_id FROM cursors WHERE seat=?", (seat,)).fetchone()
    return row["last_id"] if row else 0


def unread_count(conn: sqlite3.Connection, seat: str, *,
                 mentions_only: bool = False) -> int:
    """How many messages `read` would still have to hand back after the cursor.

    Deliberately matches read()'s filter, INCLUDING the seat's own posts, because
    the number exists to describe what read will do next. `watch` computes a
    similar count but excludes the seat's own messages (announcing your own post
    is noise), so the two can differ by however much you have just written. That
    is not a bug in either; they answer different questions.
    """
    last_id = cursor_of(conn, seat)
    if mentions_only:
        return conn.execute(
            "SELECT COUNT(*) c FROM messages m JOIN mentions x ON x.message_id=m.id "
            "WHERE m.id > ? AND x.seat = ?", (last_id, seat)).fetchone()["c"]
    return conn.execute(
        "SELECT COUNT(*) c FROM messages WHERE id > ?", (last_id,)).fetchone()["c"]


def read(conn: sqlite3.Connection, seat: str, *, mentions_only: bool = False,
         limit: int | None = None, peek: bool = False,
         topic: str | None = None, catch_up: bool = False) -> list[Message]:
    """Messages since this seat's cursor.

    Refuses if the seat holds an open independence claim on `topic`. The refusal
    is the point: a seat asked to verify something independently must post its
    own answer before it can see anyone else's.

    By default a limited read returns the OLDEST unread and advances the cursor
    past only those. In a busy docket a reader using small limits therefore falls
    further behind with every call while its reads look successful. `catch_up`
    inverts that: return the NEWEST `limit` unread and advance the cursor to the
    head, trading the skipped middle for arriving at current state in one call.
    Callers should surface unread_count() either way -- being behind is not
    otherwise observable from a read's own output.

    `mentions_only` NEVER advances the cursor, whatever `peek` says. The cursor
    means "everything up to here has been handed to me", and a filtered read has
    not handed over everything: advancing it would step over every untagged
    message in the window, permanently and without an error. There is no third
    option -- the cursor is one number, so a filtered read must either lose the
    messages it did not return or decline to move. It declines.

    The cost is that consecutive mention reads repeat. That is the truthful
    result: you have not read the docket, you have looked at your mentions.
    Mentions are routing, not access control, and the fact you need was usually
    written by someone who was not addressing you.
    """
    if topic and open_claims(conn, seat, topic):
        raise PermissionError(
            f"{seat} holds an open independence claim on '{topic}'. "
            "Post your own finding before reading others'."
        )
    touch_seat(conn, seat)
    last_id = cursor_of(conn, seat)

    if mentions_only:
        body = ("SELECT m.* FROM messages m JOIN mentions x ON x.message_id=m.id "
                "WHERE m.id > ? AND x.seat = ?")
        order = "m.id"
        args = (last_id, seat)
    else:
        body = "SELECT * FROM messages WHERE id > ?"
        order = "id"
        args = (last_id,)

    if limit and catch_up:
        # Newest `limit`, still handed back oldest-first so the reader sees them
        # in the order they were written.
        sql = (f"SELECT * FROM ({body} ORDER BY {order} DESC LIMIT {int(limit)}) "
               f"ORDER BY id")
    elif limit:
        sql = f"{body} ORDER BY {order} LIMIT {int(limit)}"
    else:
        sql = f"{body} ORDER BY {order}"
    rows = conn.execute(sql, args).fetchall()
    msgs = _hydrate(conn, rows)

    if not peek and not mentions_only:
        # catch_up advances to the docket head even though earlier messages were
        # never returned -- that is the point of it, and it must not advance to
        # msgs[-1].id, which would leave the skipped tail to be re-served later.
        target = head_id(conn) if catch_up else (msgs[-1].id if msgs else None)
        if target:
            with conn:
                conn.execute(
                    "INSERT INTO cursors (seat, last_id) VALUES (?,?) "
                    "ON CONFLICT(seat) DO UPDATE SET last_id=excluded.last_id",
                    (seat, target),
                )
    return msgs


def by_ids(conn: sqlite3.Connection, ids: Sequence[int]) -> list[Message]:
    """Exactly these messages, in id order. Never touches the cursor.

    Citing a message by id is the docket's most common act -- rules, findings and
    corrections are all referred to as [1678] -- and until this existed there was
    no way to fetch one. The available workaround was `tail N | head M`, which
    slices by LINE COUNT over posts of unknown length, so it silently truncates.
    A seat read truncated posts and acted on them.
    """
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT * FROM messages WHERE id IN ({marks}) ORDER BY id", tuple(ids)
    ).fetchall()
    return _hydrate(conn, rows)


def tail(conn: sqlite3.Connection, n: int = 20) -> list[Message]:
    rows = conn.execute(
        "SELECT * FROM (SELECT * FROM messages ORDER BY id DESC LIMIT ?) ORDER BY id", (n,)
    ).fetchall()
    return _hydrate(conn, rows)


def fts_literal(query: str) -> str:
    """Turn a user's words into an FTS5 query that means what they typed.

    FTS5 MATCH is a little language: `-` negates, `x:y` filters by column, `*`
    truncates, AND/OR/NOT are keywords. Passing raw input into it means a query
    containing any of those characters either errors or, worse, quietly means
    something else -- and hyphenated terms are ordinary here (seven-row, by-step,
    two-seat), so this is the common case rather than an exotic one.

    Each whitespace-separated token becomes a quoted FTS string, which is literal
    text; the tokens are then implicitly ANDed, which is what a person typing
    several words expects. Internal double quotes are escaped by doubling, per
    FTS5's own rule.
    """
    toks = [t for t in query.split() if t]
    return " ".join('"' + t.replace('"', '""') + '"' for t in toks)


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
