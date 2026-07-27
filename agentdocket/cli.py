"""Command line for the agent docket.

Two decisions here are not stylistic, and both are paid for by incidents:

IDENTITY IS NEVER GUESSED. A sibling tool derived the sender from the working
directory, which mis-signed messages whenever a session was `cd`'d elsewhere,
and one mis-signed message propagated into an unauthorised merge. So the seat
comes from --as or $DOCKET_SEAT and from nowhere else. If neither is set this
exits with an error rather than inventing an answer. A wrong signature is worse
than a refused command.

LONG BODIES COME FROM STDIN OR A FILE, NOT FROM ARGV. Backticks in a shell
string are command substitution, and text passed through argv gets silently
eaten before the program ever sees it. That destroyed two real citations in one
day, the second time in a message describing the first. `--stdin` and `--file`
are the safe paths and the help text says so.
"""
from __future__ import annotations

import argparse
import os
import sys

from . import store


def _seat(args) -> str:
    if args.seat:
        return args.seat.lstrip("@")
    try:
        return store.resolve_seat()[0]
    except store.SeatUnknown as e:
        sys.exit(f"error: {e}")


def _body(args) -> str:
    if args.stdin:
        return sys.stdin.read()
    if args.file:
        with open(args.file) as fh:
            return fh.read()
    if args.body:
        return " ".join(args.body)
    sys.exit("error: no body. Use --stdin, --file PATH, or give text.")


def _watch(conn, seat: str, interval: float, width: int) -> None:
    """Print one line per new message, forever. Used as a plugin monitor.

    A docket is a pull store: a message sits unread until somebody calls read,
    and nothing prompts them to. Ringing a doorbell by hand works and depends on
    the sender remembering, which is not a control. This closes the loop from
    the other side: the reader's session watches, so delivery does not depend on
    the writer doing anything.

    Two deliberate choices:

    - IT DOES NOT ADVANCE THE CURSOR. Announcing a message is not reading it.
      If this consumed the cursor, the agent's own `read` would come back empty
      and the message would be announced and then lost, which is worse than no
      announcement at all.
    - IT SKIPS YOUR OWN MESSAGES. Being notified of your own post is noise, and
      noise is what makes people stop reading notifications.
    """
    import time as _t
    store.touch_seat(conn, seat)   # arriving is enough to be addressable
    seen = conn.execute("SELECT COALESCE(MAX(id), 0) m FROM messages").fetchone()["m"]
    row = conn.execute("SELECT last_id FROM cursors WHERE seat=?", (seat,)).fetchone()
    unread = conn.execute(
        "SELECT COUNT(*) c FROM messages WHERE id > ? AND sender != ?",
        (row["last_id"] if row else 0, seat)).fetchone()["c"]
    if unread:
        print(f"[docket] {unread} unread message(s) waiting. Call docket_read.", flush=True)

    while True:
        try:
            rows = conn.execute(
                "SELECT * FROM messages WHERE id > ? AND sender != ? ORDER BY id",
                (seen, seat)).fetchall()
            for m in _hydrate_safe(conn, rows):
                body = " ".join(m.body.split())
                if len(body) > width:
                    body = body[:width].rstrip() + "..."
                to = (" -> @" + ",".join(m.mentions)) if m.mentions else ""
                tag = f" [{m.tag}]" if m.tag else ""
                print(f"[docket] new [{m.id}] from {m.sender}{tag}{to}: {body}", flush=True)
                seen = max(seen, m.id)
            if rows:
                print("[docket] call docket_read to take these into context.", flush=True)
        except Exception as e:  # a monitor that dies stops watching, silently
            print(f"[docket] watch error: {e}", flush=True)
        _t.sleep(interval)


def _hydrate_safe(conn, rows):
    return store._hydrate(conn, rows)


def _show(msgs, width: int = 0) -> None:
    if not msgs:
        print("(nothing new)")
        return
    for m in msgs:
        print(m.format(width=width))
        print()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="docket", description="Shared log for coordinating agents.")
    p.add_argument("--db", default=store.DEFAULT_DB, help="store path")
    p.add_argument("--as", dest="seat", help="acting seat (else $DOCKET_SEAT)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("post", help="append a message")
    sp.add_argument("body", nargs="*", help="short text; prefer --stdin for anything long")
    sp.add_argument("--to", action="append", default=[], metavar="SEAT",
                    help="mention a seat (repeatable)")
    sp.add_argument("--tag", help="DECISION / RESULT / STATUS / QUESTION")
    sp.add_argument("--stdin", action="store_true",
                    help="read body from stdin (SAFE: argv eats backticks)")
    sp.add_argument("--file", help="read body from a file (SAFE)")

    sr = sub.add_parser("read", help="messages since your cursor")
    sr.add_argument("--mentions", action="store_true", help="only what mentions you")
    sr.add_argument("--peek", action="store_true", help="do not advance the cursor")
    sr.add_argument("--limit", type=int)
    sr.add_argument("--topic", help="refuse if you hold an open claim on it")
    sr.add_argument("--width", type=int, default=0, help="truncate bodies for scanning")

    st = sub.add_parser("tail", help="last N messages regardless of cursor")
    st.add_argument("n", nargs="?", type=int, default=20)
    st.add_argument("--width", type=int, default=0)

    ss = sub.add_parser("search", help="full text over every message")
    ss.add_argument("query", nargs="+")
    ss.add_argument("--width", type=int, default=0)

    sc = sub.add_parser("claim", help="open an independence claim on a topic")
    sc.add_argument("topic")
    srl = sub.add_parser("release", help="close it, restoring reads")
    srl.add_argument("topic")
    sub.add_parser("claims", help="your open claims")
    sub.add_parser("stats", help="counts, senders, cursors")
    sw = sub.add_parser("watch", help="announce new messages as they arrive (for plugin monitors)")
    sw.add_argument("--interval", type=float, default=5.0, help="seconds between checks")
    sw.add_argument("--width", type=int, default=90, help="body characters per line")

    sub.add_parser("whoami", help="the seat you would sign as, and where it came from")
    si = sub.add_parser("init", help=f"write a {store.SEAT_FILE} file naming this seat")
    si.add_argument("name")

    args = p.parse_args(argv)

    # These two must work before any store exists.
    if args.cmd == "init":
        with open(store.SEAT_FILE, "w") as fh:
            fh.write(args.name.lstrip("@") + "\n")
        print(f"wrote {store.SEAT_FILE}: {args.name}\n"
              f"Sessions started at or below this directory now sign as "
              f"'{args.name}' unless $DOCKET_SEAT overrides.")
        return 0
    if args.cmd == "whoami":
        if args.seat:
            print(f"{args.seat}  (from --as)")
            return 0
        try:
            seat, src = store.resolve_seat()
            print(f"{seat}  (from {src})")
        except store.SeatUnknown as e:
            sys.exit(f"error: {e}")
        return 0

    conn = store.connect(args.db)

    if args.cmd == "post":
        unknown = store.unknown_mentions(conn, args.to)
        mid = store.post(conn, _seat(args), _body(args), args.to, args.tag)
        who = (" -> @" + ", @".join(args.to)) if args.to else ""
        print(f"posted [{mid}]{who}")
        if unknown:
            seen = sorted(store.known_seats(conn))
            print(f"  NOTE: {', '.join(repr(u) for u in unknown)} not yet known to "
                  f"this docket.\n"
                  f"  Either a typo, or a seat that has not arrived yet -- it will "
                  f"receive this by cursor if it shows up.\n"
                  f"  seats seen so far: {', '.join(seen) or '(none)'}",
                  file=sys.stderr)
    elif args.cmd == "read":
        try:
            _show(store.read(conn, _seat(args), mentions_only=args.mentions,
                             limit=args.limit, peek=args.peek, topic=args.topic),
                  args.width)
        except PermissionError as e:
            sys.exit(f"refused: {e}")
    elif args.cmd == "tail":
        _show(store.tail(conn, args.n), args.width)
    elif args.cmd == "search":
        _show(store.search(conn, " ".join(args.query)), args.width)
    elif args.cmd == "claim":
        store.claim(conn, _seat(args), args.topic)
        print(f"claimed '{args.topic}'. Reads on it are refused until you release.")
    elif args.cmd == "release":
        store.release(conn, _seat(args), args.topic)
        print(f"released '{args.topic}'.")
    elif args.cmd == "claims":
        got = store.open_claims(conn, _seat(args))
        print("\n".join(got) if got else "(none open)")
    elif args.cmd == "watch":
        _watch(conn, _seat(args), args.interval, args.width)
    elif args.cmd == "stats":
        s = store.stats(conn)
        print(f"{s['messages']} messages  {s['first']} .. {s['last']}")
        for k, v in s["by_sender"].items():
            print(f"  {k:<24} {v}")
        if s["cursors"]:
            print("cursors: " + ", ".join(f"{k}@{v}" for k, v in s["cursors"].items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
