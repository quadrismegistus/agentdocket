"""MCP stdio server exposing the docket as native tools.

Run it from an agent's MCP config; it speaks JSON-RPC 2.0 over stdin/stdout.
Written against the standard library on purpose: this gets installed into other
people's agent configs, and a coordination tool that drags in a dependency tree
is one more thing to break at the moment three sessions need to talk.

THE ONE RULE THAT BREAKS EVERYTHING IF VIOLATED: stdout carries protocol frames
and nothing else. Every diagnostic goes to stderr. A stray print() here does not
produce a warning, it produces a client that cannot parse the stream and a
server that appears to hang.

Seat identity comes from $DOCKET_SEAT, set per session in the MCP config, e.g.

    {"mcpServers": {"docket": {
        "command": "docket-mcp",
        "env": {"DOCKET_SEAT": "lacan"}}}}

It is not inferred from anything. A server that guesses which seat it is speaks
in someone else's name.
"""
from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any

from . import store

PROTOCOL_DEFAULT = "2025-06-18"
SERVER_INFO = {"name": "agentdocket", "version": "0.1.0"}


def _log(msg: str) -> None:
    print(f"[docket-mcp] {msg}", file=sys.stderr, flush=True)


def _seat() -> str:
    """$DOCKET_SEAT, else the nearest .docket-seat file. Never a guess.

    Resolving from a file is what lets one user-scoped MCP registration serve
    every project: each project declares its own seat instead of each config
    hardcoding one.
    """
    return store.resolve_seat()[0]


def _db() -> str:
    return os.environ.get("DOCKET_DB", store.DEFAULT_DB)


TOOLS: list[dict[str, Any]] = [
    {
        "name": "docket_post",
        "description": (
            "Append a message to the shared docket. Use `to` to @mention seats "
            "whose response you need; everyone can read everything regardless, so "
            "mentions are routing, not access control. Tag DECISION/RESULT/STATUS/"
            "QUESTION when the message is one of those, so it can be found later."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "body": {"type": "string", "description": "Message text. Any length."},
                "to": {"type": "array", "items": {"type": "string"},
                       "description": "Seats to mention, e.g. ['malign','desktop']"},
                "tag": {"type": "string",
                        "enum": ["DECISION", "RESULT", "STATUS", "QUESTION", "CORRECTION"]},
            },
            "required": ["body"],
        },
    },
    {
        "name": "docket_read",
        "description": (
            "Messages posted since you last read. Advances your cursor unless "
            "peek is true. If you hold an open independence claim on `topic`, this "
            "is refused until you release it: post your own finding first.\n\n"
            "With `limit` this returns the OLDEST unread, so in a busy docket "
            "small limits fall further behind on every call. Every response ends "
            "with how many remain and where the head is; if that number is large "
            "you are reading superseded state, and `catch_up: true` jumps you to "
            "the head instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mentions_only": {
                    "type": "boolean", "default": False,
                    "description": "Show only messages addressing you. NEVER advances your "
                                   "cursor -- a filtered read has not handed you everything, "
                                   "so moving the cursor would step over the untagged "
                                   "messages for good. Consequence: consecutive mention "
                                   "reads repeat, and your unread count does not fall. That "
                                   "is accurate, not broken -- you have looked at your "
                                   "mentions, not read the docket. Do a plain read to "
                                   "actually catch up."},
                "limit": {"type": "integer"},
                "catch_up": {"type": "boolean", "default": False,
                             "description": "With limit: return the NEWEST unread "
                                            "and move the cursor to the head, "
                                            "skipping the middle."},
                "peek": {"type": "boolean", "default": False,
                         "description": "Read without advancing the cursor."},
                "topic": {"type": "string",
                          "description": "Refuse the read if you hold a claim on it."},
            },
        },
    },
    {
        "name": "docket_search",
        "description": (
            "Full-text search over EVERY message, including ones addressed to no "
            "one. This is the reason the docket exists rather than a mention-only "
            "channel: the fact you need was often written down by someone who was "
            "not talking to you."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"},
                           "limit": {"type": "integer", "default": 50}},
            "required": ["query"],
        },
    },
    {
        "name": "docket_tail",
        "description": "The last N messages regardless of your cursor. For orientation.",
        "inputSchema": {"type": "object",
                        "properties": {"n": {"type": "integer", "default": 20}}},
    },
    {
        "name": "docket_claim",
        "description": (
            "Open an independence claim on a topic before verifying something "
            "independently. While it is open your reads on that topic are refused, "
            "so your answer cannot be anchored by someone else's. Release it after "
            "you post."
        ),
        "inputSchema": {"type": "object", "properties": {"topic": {"type": "string"}},
                        "required": ["topic"]},
    },
    {
        "name": "docket_release",
        "description": "Close an independence claim, restoring reads on that topic.",
        "inputSchema": {"type": "object", "properties": {"topic": {"type": "string"}},
                        "required": ["topic"]},
    },
    {
        "name": "docket_stats",
        "description": "Message count, per-sender counts, cursors, and your open claims.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _render(msgs) -> str:
    if not msgs:
        return "(nothing new)"
    return "\n\n".join(m.format() for m in msgs)


def _position(conn, seat: str, mentions_only: bool = False) -> str:
    # Always the WIDE count, even after a mention-filtered read: it reports what
    # a plain read would still hand back. A count that shrank to match the filter
    # would flatter the reader in exactly the case this line exists to warn about.
    remaining = store.unread_count(conn, seat)
    head = store.head_id(conn)
    at = store.cursor_of(conn, seat)
    note = ("\n[docket] mentions_only does not advance your cursor: this was a look at "
            "your mentions, not a read of the docket.") if mentions_only else ""
    if not remaining:
        return f"[docket] up to date at [{head}].{note}"
    return (f"[docket] {remaining} unread remaining; you are at [{at}], head is "
            f"[{head}]. You are reading behind the head -- pass catch_up: true "
            f"with a limit to jump to current state.{note}")


def _dispatch(name: str, args: dict) -> str:
    conn = store.connect(_db())
    try:
        if name == "docket_post":
            to = args.get("to") or []
            unknown = store.unknown_mentions(conn, to)
            mid = store.post(conn, _seat(), args["body"], to, args.get("tag"))
            out = f"posted [{mid}]" + (" -> @" + ", @".join(to) if to else "")
            if unknown:
                # Surfaced in the tool result rather than logged to stderr,
                # because the model is the one who can act on it.
                #
                # It states a FACT and not a CONSEQUENCE. The earlier wording
                # said "reaches nobody", which is false whenever the seat is
                # real but has not arrived yet: reads are not mention-filtered,
                # so it receives this by cursor the moment it shows up. During a
                # new docket's first hour that is the common case, not the edge.
                out += (f"\n\nNOTE: {', '.join(repr(u) for u in unknown)} not yet "
                        f"known to this docket. Either a typo, or a seat that has "
                        f"not arrived yet -- it will receive this by cursor if it "
                        f"shows up.\nSeats seen so far: "
                        f"{', '.join(sorted(store.known_seats(conn))) or '(none)'}")
            return out
        if name == "docket_read":
            seat, mentions = _seat(), bool(args.get("mentions_only"))
            msgs = store.read(conn, seat,
                              mentions_only=mentions,
                              limit=args.get("limit"),
                              peek=bool(args.get("peek")),
                              topic=args.get("topic"),
                              catch_up=bool(args.get("catch_up")))
            # The footer is the whole fix: without it a limited read looks
            # identical to a complete one, and a reader that is hundreds behind
            # has no signal saying so. It is not a new measurement -- `watch`
            # has always computed this number; it simply never reached here.
            return _render(msgs) + "\n\n" + _position(conn, seat, mentions)
        if name == "docket_search":
            return _render(store.search(conn, args["query"], args.get("limit", 50)))
        if name == "docket_tail":
            return _render(store.tail(conn, args.get("n", 20)))
        if name == "docket_claim":
            store.claim(conn, _seat(), args["topic"])
            return (f"Claimed '{args['topic']}'. Reads on it are refused until you "
                    "release. Post your own finding first.")
        if name == "docket_release":
            store.release(conn, _seat(), args["topic"])
            return f"Released '{args['topic']}'."
        if name == "docket_stats":
            s = store.stats(conn)
            # The seat and its SOURCE lead, because the skill tells agents to
            # verify identity here before posting and this tool did not report
            # it -- caught independently by two seats, which is how it should
            # have been caught. An instruction that names a check the tool
            # cannot perform is worse than no instruction: it reads as done.
            seat, src = store.resolve_seat()
            lines = [f"seat: {seat}  (from {src})",
                     f"cwd:  {os.getcwd()}",
                     f"{s['messages']} messages  {s['first']} .. {s['last']}"]
            lines += [f"  {k:<22} {v}" for k, v in s["by_sender"].items()]
            if s["cursors"]:
                lines.append("cursors: " + ", ".join(f"{k}@{v}" for k, v in s["cursors"].items()))
            oc = store.open_claims(conn, _seat())
            lines.append("your open claims: " + (", ".join(oc) if oc else "(none)"))
            return "\n".join(lines)
        raise ValueError(f"unknown tool: {name}")
    finally:
        conn.close()


def _handle(req: dict) -> dict | None:
    method, rid = req.get("method"), req.get("id")

    if method == "initialize":
        ver = (req.get("params") or {}).get("protocolVersion") or PROTOCOL_DEFAULT
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": ver,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        }}

    if method in ("notifications/initialized", "initialized"):
        return None  # notification: no reply

    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            text = _dispatch(name, args)
            return {"jsonrpc": "2.0", "id": rid,
                    "result": {"content": [{"type": "text", "text": text}]}}
        except PermissionError as e:
            # An independence claim refusing a read is a normal outcome, not a
            # crash: report it as tool-level error text the model can act on.
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": f"REFUSED: {e}"}], "isError": True}}
        except Exception as e:
            _log(f"tool {name} failed: {e}\n{traceback.format_exc()}")
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": f"error: {e}"}], "isError": True}}

    if rid is None:
        return None  # unknown notification: ignore
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> int:
    try:
        seat, src = store.resolve_seat()
        _log(f"seat={seat} (from {src}) db={_db()} cwd={os.getcwd()}")
    except store.SeatUnknown:
        _log(f"NO SEAT RESOLVED from cwd={os.getcwd()}; tool calls will fail "
             f"until $DOCKET_SEAT is set or a {store.SEAT_FILE} file exists")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            _log(f"bad JSON: {e}")
            continue
        try:
            resp = _handle(req)
        except Exception as e:
            _log(f"handler error: {e}\n{traceback.format_exc()}")
            resp = {"jsonrpc": "2.0", "id": req.get("id"),
                    "error": {"code": -32603, "message": str(e)}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
