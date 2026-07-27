"""Command line for the agent room.

Two decisions here are not stylistic, and both are paid for by incidents:

IDENTITY IS NEVER GUESSED. A sibling tool derived the sender from the working
directory, which mis-signed messages whenever a session was `cd`'d elsewhere,
and one mis-signed message propagated into an unauthorised merge. So the seat
comes from --as or $ROOM_SEAT and from nowhere else. If neither is set this
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
    seat = args.seat or os.environ.get("ROOM_SEAT")
    if not seat:
        sys.exit(
            "error: no seat identity.\n"
            "  Set ROOM_SEAT=<name> or pass --as <name>.\n"
            "  This is not inferred from the working directory on purpose: a\n"
            "  guessed identity signs messages as somebody else."
        )
    return seat.lstrip("@")


def _body(args) -> str:
    if args.stdin:
        return sys.stdin.read()
    if args.file:
        with open(args.file) as fh:
            return fh.read()
    if args.body:
        return " ".join(args.body)
    sys.exit("error: no body. Use --stdin, --file PATH, or give text.")


def _show(msgs, width: int = 0) -> None:
    if not msgs:
        print("(nothing new)")
        return
    for m in msgs:
        print(m.format(width=width))
        print()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="room", description="Shared log for coordinating agents.")
    p.add_argument("--db", default=store.DEFAULT_DB, help="store path")
    p.add_argument("--as", dest="seat", help="acting seat (else $ROOM_SEAT)")
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

    args = p.parse_args(argv)
    conn = store.connect(args.db)

    if args.cmd == "post":
        mid = store.post(conn, _seat(args), _body(args), args.to, args.tag)
        who = (" -> @" + ", @".join(args.to)) if args.to else ""
        print(f"posted [{mid}]{who}")
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
