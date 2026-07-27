"""Does the store survive simultaneous writers from separate PROCESSES?

This test exists because the objection that prompted the whole design was
"brittle to simultaneous edits", and the answer given was "SQLite in WAL mode
handles it". That answer is a claim. This file is the check.

Threads would not test it. Python threads share one interpreter and SQLite's
own locking is barely exercised; the real question is what happens when four
independent OS processes, holding separate connections, append at the same
instant. So this spawns processes.

Three properties are asserted, and only the first is obvious:

  1. NOTHING IS LOST     -- every write lands.
  2. NOTHING IS DUPLICATED OR SKIPPED -- ids form a contiguous 1..N run, so the
     total order really is total. If AUTOINCREMENT gapped under contention, the
     "which came first" question the docket exists to answer would be unreliable.
  3. NOTHING IS INTERLEAVED -- each body arrives whole. This is what actually
     breaks with concurrent appends to a plain text file, which is the design
     that was rejected.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agentdocket import store

WRITERS = 8
PER_WRITER = 60


def _hammer(args):
    """One process, its own connection, writing as fast as it can."""
    path, seat = args
    conn = store.connect(path)
    ok = 0
    for i in range(PER_WRITER):
        # A body long enough that a partial write would be visible, and
        # self-describing so interleaving is detectable rather than inferred.
        body = f"{seat}:{i}:" + ("x" * 400) + f":end-{seat}-{i}"
        store.post(conn, sender=seat, body=body, mentions=[f"peer{i % 3}"])
        ok += 1
    conn.close()
    return ok


def test_concurrent_processes():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "docket.db")
        store.connect(path).close()  # create schema once, up front

        seats = [f"seat{n}" for n in range(WRITERS)]
        with mp.Pool(WRITERS) as pool:
            written = sum(pool.map(_hammer, [(path, s) for s in seats]))

        conn = store.connect(path)
        rows = conn.execute("SELECT id, sender, body FROM messages ORDER BY id").fetchall()

        expected = WRITERS * PER_WRITER
        assert written == expected, f"writers reported {written}, expected {expected}"

        # 1. nothing lost
        assert len(rows) == expected, f"stored {len(rows)}, expected {expected}"

        # 2. contiguous total order, no gaps or repeats
        ids = [r["id"] for r in rows]
        assert ids == list(range(1, expected + 1)), "ids are not a contiguous 1..N run"

        # 3. every body whole, and attributed to the process that wrote it
        for r in rows:
            head, idx, _pad = r["body"].split(":", 2)
            assert head == r["sender"], f"body/sender mismatch: {head} vs {r['sender']}"
            assert r["body"].endswith(f":end-{head}-{idx}"), "body truncated or interleaved"

        # each writer's messages are all present
        per = {}
        for r in rows:
            per[r["sender"]] = per.get(r["sender"], 0) + 1
        assert per == {s: PER_WRITER for s in seats}, f"uneven writes: {per}"

        # mentions survived the contention too
        nm = conn.execute("SELECT COUNT(*) c FROM mentions").fetchone()["c"]
        assert nm == expected, f"{nm} mention rows, expected {expected}"
        conn.close()
        print(f"OK: {expected} messages from {WRITERS} concurrent processes, "
              f"contiguous ids 1..{expected}, no interleaving, mentions intact")


def test_search_finds_unaddressed_message():
    """The df926b2 case: a true note that mentioned nobody must still be findable."""
    with tempfile.TemporaryDirectory() as d:
        conn = store.connect(os.path.join(d, "docket.db"))
        store.post(conn, "desktop", "booked df926b2 as mangled predecessor, IGNORE it")
        store.post(conn, "malign", "unrelated chatter", mentions=["lacan"])
        hits = store.search(conn, "df926b2")
        assert len(hits) == 1 and "IGNORE" in hits[0].body
        assert hits[0].mentions == (), "the point is that it addressed no one"
        conn.close()
        print("OK: unaddressed message retrievable by search")


def test_claim_blocks_reading_until_posted():
    """Independence: a seat verifying something cannot read others' answers first."""
    with tempfile.TemporaryDirectory() as d:
        conn = store.connect(os.path.join(d, "docket.db"))
        store.post(conn, "malign", "my verdict on the gate: PASS", mentions=["lacan"])
        store.claim(conn, "lacan", "gate-audit")
        try:
            store.read(conn, "lacan", topic="gate-audit")
            raise AssertionError("read should have been refused while the claim is open")
        except PermissionError:
            pass
        store.release(conn, "lacan", "gate-audit")
        msgs = store.read(conn, "lacan", topic="gate-audit")
        assert len(msgs) == 1
        conn.close()
        print("OK: open claim blocks the read, release restores it")


def test_cursor_advances_and_peek_does_not():
    with tempfile.TemporaryDirectory() as d:
        conn = store.connect(os.path.join(d, "docket.db"))
        for i in range(5):
            store.post(conn, "malign", f"message {i}")
        assert len(store.read(conn, "lacan", peek=True)) == 5
        assert len(store.read(conn, "lacan", peek=True)) == 5, "peek moved the cursor"
        assert len(store.read(conn, "lacan")) == 5
        assert len(store.read(conn, "lacan")) == 0, "cursor did not advance"
        store.post(conn, "desktop", "one more")
        assert len(store.read(conn, "lacan")) == 1
        conn.close()
        print("OK: cursor advances on read, peek leaves it alone")


def test_seat_resolution_never_guesses():
    """Identity comes from a declaration or from nothing. Never from the path.

    The nesting case is not hypothetical: the layout this was built for has one
    seat's directory sitting inside another's, so a child that forgets to
    declare itself silently signs as its parent. Upward search is the
    convenience; reporting the SOURCE alongside the seat is what makes it safe.
    """
    env_backup = os.environ.pop("DOCKET_SEAT", None)
    try:
        with tempfile.TemporaryDirectory() as d:
            parent = os.path.join(d, "parent")
            child = os.path.join(parent, "agents", "child")
            os.makedirs(child)

            # 1. nothing declared anywhere -> refuse, do not invent
            try:
                store.resolve_seat(child)
                raise AssertionError("resolved a seat with nothing declared")
            except store.SeatUnknown:
                pass

            # 2. parent declares, child inherits -- the convenience AND the trap
            with open(os.path.join(parent, store.SEAT_FILE), "w") as fh:
                fh.write("desktop\n")
            seat, src = store.resolve_seat(child)
            assert seat == "desktop", seat
            assert src.endswith(store.SEAT_FILE)

            # 3. nearest declaration wins
            with open(os.path.join(child, store.SEAT_FILE), "w") as fh:
                fh.write("@lacan\n")            # a leading @ is tolerated
            seat, src = store.resolve_seat(child)
            assert seat == "lacan", seat
            assert child in src

            # 4. env beats every file
            os.environ["DOCKET_SEAT"] = "malign"
            assert store.resolve_seat(child) == ("malign", "$DOCKET_SEAT")
            del os.environ["DOCKET_SEAT"]

            # 5. the directory name is never consulted
            assert "child" not in store.resolve_seat(child)[0]
        print("OK: seat resolves by declaration only; nearest wins; env overrides")
    finally:
        if env_backup is not None:
            os.environ["DOCKET_SEAT"] = env_backup


def test_mention_warning_does_not_fire_on_an_arrived_seat():
    """A seat that has arrived but not spoken is a real address, not a typo.

    The first version keyed this on senders, so the warning fired on every
    seat's FIRST INBOUND MENTION -- the moment the address is most likely
    correct and most consequential -- and told the sender it reached nobody when
    in fact it reached them by cursor, since reads are not mention-filtered.
    A false alarm on the common case teaches people to ignore the alarm, which
    is worse than the typo it was built to catch.
    """
    with tempfile.TemporaryDirectory() as d:
        conn = store.connect(os.path.join(d, "docket.db"))

        # Nobody has arrived: a mention really is unknown.
        assert store.unknown_mentions(conn, ["registrar"]) == ["registrar"]

        # registrar loads the plugin and reads. It has posted nothing.
        store.read(conn, "registrar")
        assert store.unknown_mentions(conn, ["registrar"]) == [], \
            "a seat that has read is addressable and must not warn"

        # A genuine typo still warns.
        assert store.unknown_mentions(conn, ["registar"]) == ["registar"]

        # Both at once: only the typo is reported.
        assert store.unknown_mentions(conn, ["registrar", "registar"]) == ["registar"]
        conn.close()
        print("OK: arrived-but-silent seats do not warn; typos still do")


if __name__ == "__main__":
    test_concurrent_processes()
    test_search_finds_unaddressed_message()
    test_claim_blocks_reading_until_posted()
    test_cursor_advances_and_peek_does_not()
    test_seat_resolution_never_guesses()
    test_mention_warning_does_not_fire_on_an_arrived_seat()
    print("\nall passed")
