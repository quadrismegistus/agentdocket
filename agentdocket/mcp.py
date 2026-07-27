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
    seat = os.environ.get("DOCKET_SEAT", "").lstrip("@").strip()
    if not seat:
        raise RuntimeError(
            "DOCKET_SEAT is not set. Set it in this server's MCP config env block. "
            "It is deliberately not inferred: a guessed identity signs messages "
            "as somebody else."
        )
    return seat


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
            "is refused until you release it: post your own finding first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "mentions_only": {"type": "boolean", "default": False},
                "limit": {"type": "integer"},
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


def _dispatch(name: str, args: dict) -> str:
    conn = store.connect(_db())
    try:
        if name == "docket_post":
            mid = store.post(conn, _seat(), args["body"],
                             args.get("to", []) or [], args.get("tag"))
            to = args.get("to") or []
            return f"posted [{mid}]" + (" -> @" + ", @".join(to) if to else "")
        if name == "docket_read":
            msgs = store.read(conn, _seat(),
                              mentions_only=bool(args.get("mentions_only")),
                              limit=args.get("limit"),
                              peek=bool(args.get("peek")),
                              topic=args.get("topic"))
            return _render(msgs)
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
            lines = [f"{s['messages']} messages  {s['first']} .. {s['last']}"]
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
    _log(f"seat={os.environ.get('DOCKET_SEAT', '(unset)')} db={_db()}")
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
