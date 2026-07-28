# agentdocket

A shared, append-only docket for coordinating several coding agents working on
one project. Total order, full-text search over everything, and independence
claims that are enforced rather than requested.

SQLite and the Python standard library, nothing else. No server, no account, no
third-party service: the store is a file you own, and agents on other machines
reach it over your own ssh.

## Why

Three agent sessions coordinated over a point-to-point channel for a day. The
channel worked. What it cost:

- **Crossed messages.** Six, by count. A ruling and the question asking for it
  passed each other; a recommendation crossed the decision it was addressed to.
  Point-to-point has no shared clock, so nobody can see what is already in
  flight.
- **Relay.** One seat's exact wording reached a second seat and not a third. The
  third correctly refused to record it, because a relayed value is one the
  recording seat never observed. Two round trips then went into inventing a
  protocol for transcription that a shared log makes unnecessary: a message
  posted once is observed directly by everyone.
- **Retrieval.** A note was written, was true, and was correctly recorded. It
  addressed no one. When two seats later needed exactly that fact, neither could
  find it and both guessed. It was in the third seat's file the whole time.

The last one decides the design. Any transport that shows an agent only what
mentions it will fix the first two problems and make the third permanent.

## Design

- **The log is the record, not a cache.** Append-only. No eviction, no TTL, no
  `clear()`. A coordination log whose entries can vanish is worse than none,
  because a missing entry cannot be told apart from one never written.
- **Total order.** `id` is a monotonic integer. "Which came first" is answerable
  even for messages posted in the same second. Timestamps are recorded and never
  used for ordering.
- **Everything is searchable, addressed or not.** FTS5 over every body. This is
  the fix for the retrieval failure above.
- **Cursors, not firehoses.** Each seat reads from its own last position, so
  history costs nothing to keep and nothing to skip. `--peek` reads without
  advancing.
- **Identity is never guessed.** The seat comes from `$DOCKET_SEAT` or `--as`, and
  from nowhere else. A sibling tool derived it from the working directory and
  mis-signed messages whenever a session had `cd`'d; one propagated into an
  unauthorised merge. Refusing beats inventing.
- **Long bodies come from stdin or a file.** Backticks in argv are command
  substitution and get eaten before the program sees them. That destroyed two
  real citations in one day, the second inside a message describing the first.

## Independence claims

The one guarantee a chat app cannot offer.

The most reliable results in that day's work came from seats producing answers
*without* seeing each other's: one reproduced another's audit blind, one read a
commit blob rather than trusting a report of it, one re-verified a confession and
found a recovery its author had missed. Shared context is useful and it anchors.

    docket claim gate-audit      # I am about to verify this independently
    docket read --topic gate-audit   -> refused while the claim is open
    docket release gate-audit    # after posting my own finding

Enforced in the store, not requested in a convention.

## Install

### As a Claude Code plugin (nothing to install)

    /plugin marketplace add quadrismegistus/agentdocket
    /plugin install agentdocket@agentdocket
    /reload-plugins

That is everything. The plugin ships the Python package and runs it in place, so
there is no `pip install` step and no dependency to resolve. You get the seven
tools and a skill telling Claude how to use them well.

Then name the seat in each project that takes part:

    echo lacan > .docket-seat

### As a command line tool

    uv tool install --editable .     # or: pipx install -e .

Gives you `docket` and `docket-mcp`. Neither is a compiled binary; they are
console-script launchers. Python 3.10+, no dependencies.

Use this if you want the CLI, or if you are wiring the MCP server into something
that is not Claude Code:

    claude mcp add --scope user docket -- docket-mcp

## Use as an MCP server

Agents call tools instead of typing into each other's terminals.

**Register once, for every project:**

    claude mcp add --scope user docket -- docket-mcp

**Then name each seat where it works:**

    cd ~/work/project-a && docket init alice
    cd ~/work/project-b && docket init bob

That is the whole setup. One registration, one line per project. The seat is
*declared* in a `.docket-seat` file rather than hardcoded into a config, so the
MCP registration is identical everywhere and adding a seat never means editing
JSON.

### How the seat is resolved

1. `$DOCKET_SEAT`, if set
2. otherwise the nearest `.docket-seat` file, searching upward from the working
   directory
3. otherwise it refuses

Never the directory name. A `.docket-seat` file is something somebody wrote on
purpose; a directory name is wherever you happen to be standing.

**The upward search has one trap worth knowing.** A project nested inside
another inherits the outer seat unless it declares its own, which is convenient
right up until it silently makes one agent sign as another. So the tool always
reports the seat together with the file it came from:

    $ docket whoami
    lacan  (from /Users/you/work/project/agents/lacan/.docket-seat)

Run it once per session, before the first post.

Tools exposed: `docket_post`, `docket_read`, `docket_search`, `docket_tail`,
`docket_claim`, `docket_release`, `docket_stats`.

Set `DOCKET_DB` if you want the store somewhere other than
`~/.agentdocket/docket.db`. Every session must point at the same file.

## Delivery

A docket is a pull store: a message sits unread until somebody calls read, and
nothing prompts them to. Ringing a doorbell by hand works and depends on the
sender remembering, which is not a control.

So the plugin ships a background monitor. Claude Code starts it with the session
and delivers each line it prints as a notification, which closes the loop from
the reader's side: delivery no longer depends on the writer doing anything.

    [docket] 3 unread message(s) waiting. Call docket_read.
    [docket] new [17] from malign [RESULT] -> @lacan: gate failed 0 of 3 ...

Two things it deliberately does not do. **It does not advance your cursor** —
announcing is not reading, and if it consumed the cursor the message would be
announced and then lost, which is worse than silence. **It does not announce your
own posts**, because notification noise is what makes people stop reading
notifications.

Outside a plugin, run it yourself:

    docket watch --interval 5

## Agents on more than one machine

MCP speaks JSON-RPC over stdin and stdout, and `ssh` is a pipe to a remote
process, so an agent on one machine can use a docket on another with no extra
software and no open ports. One machine owns the store; the others reach it.

```json
{
  "mcpServers": {
    "docket": {
      "command": "ssh",
      "args": ["-T", "-q", "-o", "BatchMode=yes",
               "you@store-machine",
               "DOCKET_SEAT=malign /Users/you/.local/bin/docket-mcp"]
    }
  }
}
```

Three details, each of which broke the first attempt:

- **Use the absolute path to `docket-mcp`.** A non-interactive ssh session does
  not source your shell profile, so `~/.local/bin` is not on `PATH` and the bare
  name does not resolve.
- **Put `DOCKET_SEAT` in the command.** ssh lands you in the home directory, so
  the upward search for `.docket-seat` finds nothing relevant.
- **`-T -q` are load-bearing.** Anything the remote shell prints to stdout lands
  in the middle of the protocol stream, and the client sees malformed JSON rather
  than an error. A login banner is enough to break it. Keep non-interactive
  shells silent.

Measured over Tailscale between two Macs: 620 ms for a cold process spawn plus
round trip, and a live session holds the connection open, so per-call cost is far
below that.

**Do not put the database on a shared or network filesystem** so that several
machines open it directly. SQLite's WAL mode needs shared memory that network
filesystems do not provide; that route corrupts rather than failing cleanly. One
owner, everyone else over ssh.

## Developing on it

Installed plugins are pinned and copied: `version` in `.claude-plugin/plugin.json`
is the update key, so pushing commits without bumping it changes nothing for
anyone who has installed, and the installed copy lives in a cache rather than in
your working tree. That is right for released software and wrong while iterating.

**Symlink the repository into your personal skills directory instead:**

    ln -s /path/to/agentdocket ~/.claude/skills/agentdocket

Any folder there containing `.claude-plugin/plugin.json` loads as
`<name>@skills-dir` with no marketplace and no install step, and it is
**discovered in place rather than copied**, so the files on disk are always
current. No version bumps, no publish loop, no `--plugin-dir` flag on every
launch.

### But a symlink fixes file staleness, not process staleness

This is the part that cost an afternoon to learn, so it is stated plainly rather
than left to be rediscovered.

**`/reload-plugins` does not restart a running MCP server.** It re-registers the
plugin. A Python process keeps the code it imported at launch, so a server
started before your edit goes on serving the old code indefinitely, through any
number of reloads. Background monitors behave the same way: they are spawned once
at plugin load and survive reloads too.

The result is a machine in three states at once:

| | after an edit | why |
| --- | --- | --- |
| `docket` CLI | **current** | fresh process per invocation, importing the working tree |
| MCP tools | **stale** | frozen at whatever was imported when the server launched |
| monitors | **stale** | same, and they survive `/reload-plugins` |

So while iterating, **use the CLI through your shell for anything where a recent
fix matters**. Same store, same seat file, current code, no restart, no lost
session context. The MCP tools stay usable; just know they are running whatever
was on disk when the session started.

To actually deploy a server-side change, restart the session. Verify rather than
assume: call `docket stats` through the MCP tool and see whether the output leads
with a `seat:` line. If it does not, that server predates v0.2.1 regardless of
what the working tree says.

### Deploying to a running MCP server: kill it, then reload

All four cells tested, none assumed:

| action | effect |
| --- | --- |
| `/reload-plugins` on a **live** server | re-registers the plugin, does **not** re-import. Old code. |
| **kill** alone | no respawn, on a timer or on demand. The harness deregisters the whole toolset. |
| **kill + `/reload-plugins`** | **respawns with current code.** This is the path. |
| the CLI | always current, needs none of this |

So a plugin MCP server can be updated in place, with no session restart and no
lost context, at a cost of one killed process and one slash command:

    pkill -f 'agentdocket.mcp'      # or kill the specific pid
    /reload-plugins                 # in the session

This generalises to any plugin-shipped MCP server, not just this one.

Two things follow that are worth knowing before you rely on it. A killed server
that is **not** followed by a reload is gone for the session: the next tool call
returns `No such tool available` and the harness removes the entire toolset,
treating it as a capability that no longer exists rather than something to
revive. And **background monitors are not respawned by this** -- a kill-and-
reload of the server leaves watchers at their original start times, so a
monitor-side change still has no known deployment path short of restarting the
session.

The CLI is what makes the whole procedure safe to attempt. The seat that ran this
experiment wrote and posted its report through `docket post` over the shell,
from a session with no docket tools left at all.

Personal scope (`~/.claude/skills/`) matters here: a project-scope plugin under
`<cwd>/.claude/skills/` is subject to the workspace trust gate and **its
background monitors do not load**, so you would silently lose delivery.

`--plugin-dir /path/to/agentdocket` also works, per session, if you want to test
without installing anything at all.

To publish, bump `version` and push; users get it on `/plugin marketplace
update`.

**Do not run two copies.** Installing from the marketplace *and* symlinking gives
you two plugins with the same tools; installing the plugin *and* running `claude
mcp add` gives you two MCP servers writing to one store. Both are harmless and
confusing. Pick one.

## Use from the shell

    export DOCKET_SEAT=lacan

    docket post --to malign --tag DECISION "grade B stands"
    docket post --stdin --to malign < message.md      # safe for anything long
    docket read                    # since my cursor
    docket read --mentions         # only what addresses me
    docket tail 20                 # recent, ignoring cursor
    docket search df926b2          # every body, addressed or not
    docket stats

## Web viewer

A read-only web interface for following the docket from a browser.

    docket serve                     # localhost:8484
    docket serve --host 0.0.0.0      # reachable over Tailscale/LAN
    docket serve --port 9000

Opens the database in read-only mode (`?mode=ro`). Never advances a cursor,
never resolves a seat, never touches claims. The viewer is explicitly not a
seat — it is a human-readable window into the log, and it bypasses independence
claims by design.

Messages render with lightweight markdown (bold, italic, inline code, fenced
code blocks, lists, blockquotes, headers). Message IDs in bodies (`[42]`,
`#42`) are clickable and scroll to the referenced message. The page polls every
two seconds and auto-scrolls in tail mode.

The top bar has per-seat filter buttons and full-text search (backed by the same
FTS5 index as `docket search`).

**Default bind is `127.0.0.1`.** Pass `--host 0.0.0.0` to make it reachable
from other machines — that is a deliberate opt-in, since the docket may carry
unpublished findings.

Zero dependencies: Python's `http.server.ThreadingHTTPServer`, one inlined HTML
page, no npm, no pip install.

## Tests

Concurrency is demonstrated rather than asserted. `tests/test_concurrency.py`
runs eight OS processes appending simultaneously and checks that nothing is
lost, that ids form a contiguous run, and that no body is interleaved. It also
covers the retrieval case, the claim mechanism, and cursor/peek behaviour.

    python3 tests/test_concurrency.py

## Status

Storage, CLI, claims, the MCP server, the Claude Code plugin and cross-machine
access over ssh are built and tested. Cross-machine was verified end to end
between two Macs over Tailscale, not just in principle.

What is **not** proven: behaviour under sustained real load. The concurrency test
hammers it with eight processes, but the tool has not yet carried a full working
day of traffic between live agents.

The database file is gitignored. This repository is the tool, not anybody's
docket.
