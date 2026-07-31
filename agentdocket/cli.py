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


def _watch(conn, seat: str, interval: float, width: int,
           mentions_only: bool = False) -> None:
    """Print one line per new message, forever. Used as a plugin monitor.

    A docket is a pull store: a message sits unread until somebody calls read,
    and nothing prompts them to. Ringing a doorbell by hand works and depends on
    the sender remembering, which is not a control. This closes the loop from
    the other side: the reader's session watches, so delivery does not depend on
    the writer doing anything.

    Three deliberate choices:

    - IT DOES NOT ADVANCE THE CURSOR. Announcing a message is not reading it.
      If this consumed the cursor, the agent's own `read` would come back empty
      and the message would be announced and then lost, which is worse than no
      announcement at all.
    - IT SKIPS YOUR OWN MESSAGES. Being notified of your own post is noise, and
      noise is what makes people stop reading notifications.
    - `mentions_only` FILTERS THE ANNOUNCEMENT, NEVER THE READ. Because this
      never writes the cursor, quietening the watch cannot lose a message: the
      untagged traffic is still sitting there for the next `read`. Filtering the
      READ instead -- `read(mentions_only=True)` -- looks equivalent and is not:
      that advances the cursor to the last MENTION, stepping silently over every
      untagged message before it, permanently. Notify narrow, read wide.
    """
    import time as _t
    store.touch_seat(conn, seat)   # arriving is enough to be addressable
    seen = conn.execute("SELECT COALESCE(MAX(id), 0) m FROM messages").fetchone()["m"]
    row = conn.execute("SELECT last_id FROM cursors WHERE seat=?", (seat,)).fetchone()
    last_id = row["last_id"] if row else 0
    # The count stays WIDE even when announcements are narrow: it describes what
    # a read would hand back, and a read is not mention-filtered.
    unread = conn.execute(
        "SELECT COUNT(*) c FROM messages WHERE id > ? AND sender != ?",
        (last_id, seat)).fetchone()["c"]
    if unread:
        print(f"[docket] {unread} unread message(s) waiting. Call docket_read.", flush=True)

    if mentions_only:
        sql = ("SELECT m.* FROM messages m JOIN mentions x ON x.message_id=m.id "
               "WHERE m.id > ? AND m.sender != ? AND x.seat = ? ORDER BY m.id")
        args_for = lambda s: (s, seat, seat)
    else:
        sql = "SELECT * FROM messages WHERE id > ? AND sender != ? ORDER BY id"
        args_for = lambda s: (s, seat)

    while True:
        try:
            rows = conn.execute(sql, args_for(seen)).fetchall()
            for m in _hydrate_safe(conn, rows):
                body = " ".join(m.body.split())
                if len(body) > width:
                    body = body[:width].rstrip() + "..."
                to = (" -> @" + ",".join(m.mentions)) if m.mentions else ""
                tag = f" [{m.tag}]" if m.tag else ""
                print(f"[docket] new [{m.id}] from {m.sender}{tag}{to}: {body}", flush=True)
            if rows:
                print("[docket] call docket_read to take these into context.", flush=True)
            # Advance past everything CONSIDERED, not just everything announced.
            # Under mentions_only the announced set is sparse, and marking only
            # announced ids would leave the scan window growing without bound --
            # re-reading the same untagged traffic on every tick forever.
            seen = max(seen, conn.execute(
                "SELECT COALESCE(MAX(id), 0) m FROM messages").fetchone()["m"])
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


def position_line(conn, seat: str, *, mentions_only: bool = False) -> str:
    """Where this seat now stands, for the footer of every read.

    Without this a limited read is indistinguishable from a complete one: the
    docket CAN tell you that you are behind -- `stats` shows cursors and head in
    one cheap call -- but only if you already suspect it, which is precisely the
    reader who does not need telling. The number was already being computed by
    `watch`; it just never reached this path.

    The count is always WIDE, even after a mention-filtered read. It reports what
    a plain read would still hand back, which is the number that matters; a count
    that shrank to match the filter would flatter the reader in exactly the
    situation the footer exists to warn about.
    """
    remaining = store.unread_count(conn, seat)
    head = store.head_id(conn)
    at = store.cursor_of(conn, seat)
    note = ("\n[docket] --mentions does not advance your cursor: this was a look "
            "at your mentions, not a read of the docket.") if mentions_only else ""
    if not remaining:
        return f"[docket] {seat}: up to date at [{head}].{note}"
    return (f"[docket] {seat}: {remaining} unread remaining; you are at [{at}], "
            f"head is [{head}]. Use --catch-up to jump to the head.{note}")


def _show_position(conn, seat: str, *, mentions_only: bool = False) -> None:
    print(position_line(conn, seat, mentions_only=mentions_only))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="docket", description="Shared log for coordinating agents.")
    p.add_argument("--db", default=store.DEFAULT_DB, help="store path")
    p.add_argument("--as", dest="seat", help="acting seat (else $DOCKET_SEAT)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("post", help="append a message")
    sp.add_argument("body", nargs="*", help="short text; prefer --stdin for anything long")
    sp.add_argument("--to", action="append", default=[], metavar="SEAT",
                    help="mention a seat (repeatable; commas also split, so "
                         "--to a,b is the same as --to a --to b)")
    sp.add_argument("--tag", help="DECISION / RESULT / STATUS / QUESTION")
    sp.add_argument("--stdin", action="store_true",
                    help="read body from stdin (SAFE: argv eats backticks)")
    sp.add_argument("--file", help="read body from a file (SAFE)")

    sr = sub.add_parser("read", help="messages since your cursor")
    sr.add_argument("--mentions", action="store_true", help="only what mentions you")
    sr.add_argument("--peek", action="store_true", help="do not advance the cursor")
    sr.add_argument("--limit", type=int)
    sr.add_argument("--catch-up", action="store_true",
                    help="with --limit: return the NEWEST unread and jump the "
                         "cursor to the head, skipping the middle")
    sr.add_argument("--topic", help="refuse if you hold an open claim on it")
    sr.add_argument("--width", type=int, default=0, help="truncate bodies for scanning")

    st = sub.add_parser("tail", help="last N messages regardless of cursor")
    # -n is accepted as well as the positional because every other CLI in the
    # world spells it that way, and `docket tail -n 12` used to die with an
    # unrecognized-arguments error that reads like the command is wrong.
    st.add_argument("n", nargs="?", type=int, default=20)
    st.add_argument("-n", "--n", dest="n_flag", type=int, default=None,
                    help="alias for the positional count")
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
    sw.add_argument("--mentions", action="store_true",
                    help="announce only messages that mention you. Safe: watch never "
                         "writes the cursor, so untagged traffic still arrives on your "
                         "next read. Do NOT use read --mentions for this -- that DOES "
                         "advance the cursor and drops the untagged messages for good.")

    sv = sub.add_parser("serve", help="web viewer (read-only)")
    sv.add_argument("--host", default="127.0.0.1",
                    help="bind address (0.0.0.0 for Tailscale/LAN access)")
    sv.add_argument("--port", type=int, default=8484)

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
        # Commas split. `--to malign,lacan` used to mention one seat literally
        # named "malign,lacan", which exists nowhere, so no watcher fired and the
        # only complaint was an easily-missed NOTE. For a routing field a comma is
        # operator error every time, and silent non-delivery is the worst outcome
        # available: the sender believes it was routed.
        args.to = [t for raw in args.to for t in raw.replace(",", " ").split() if t]
        unknown = store.unknown_mentions(conn, args.to)
        seat = _seat(args)
        # Warn when the seat was INHERITED from an ancestor directory rather than
        # declared where you are standing. One seat's tree containing another's is
        # the normal layout, so a call from the parent signs as the parent -- and
        # if the parent is the seat whose posts are rulings, that is a fabricated
        # ruling with nothing said about it.
        try:
            _, origin = store.resolve_seat()
        except store.SeatUnknown:
            origin = None
        if origin and origin != "$DOCKET_SEAT" and not args.seat:
            home = os.path.dirname(origin)
            if os.path.abspath(home) != os.path.abspath(os.getcwd()):
                print(f"  WARNING: seat '{seat}' was inherited from {origin},\n"
                      f"  not declared in {os.getcwd()}. You are posting as the seat that "
                      f"owns the enclosing tree.\n"
                      f"  If that is not what you meant, use --as or $DOCKET_SEAT.",
                      file=sys.stderr)
        mid = store.post(conn, seat, _body(args), args.to, args.tag)
        who = (" -> @" + ", @".join(args.to)) if args.to else ""
        # The signing seat is echoed because signing as the wrong one is silent
        # otherwise, and it is easy: the seat comes from the working directory, so
        # a `cd` into another project carries you into its identity. The skill
        # warns about this in prose and tells you to check `stats` -- but prose
        # loses to a pipe, and `stats | grep cursors` drops the seat line. A value
        # printed by the operation itself cannot be filtered out of the operation.
        print(f"posted [{mid}] as {seat}{who}")
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
            seat = _seat(args)
            _show(store.read(conn, seat, mentions_only=args.mentions,
                             limit=args.limit, peek=args.peek, topic=args.topic,
                             catch_up=args.catch_up),
                  args.width)
            _show_position(conn, seat, mentions_only=args.mentions)
        except PermissionError as e:
            sys.exit(f"refused: {e}")
    elif args.cmd == "tail":
        _show(store.tail(conn, args.n_flag if args.n_flag is not None else args.n),
              args.width)
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
        _watch(conn, _seat(args), args.interval, args.width,
               mentions_only=args.mentions)
    elif args.cmd == "serve":
        from .serve import serve as _serve
        _serve(args.db, args.host, args.port)
        return 0
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
