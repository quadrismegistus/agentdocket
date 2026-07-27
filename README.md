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

Installed plugins are pinned: `version` in `.claude-plugin/plugin.json` is the
update key, so pushing commits without bumping it changes nothing for anyone who
has installed. That is right for released software and wrong while iterating.

While changing it, load the working copy directly and skip the publish loop
entirely:

    claude --plugin-dir /path/to/agentdocket

Then `/reload-plugins` picks up edits without a restart. To publish, bump
`version` and push; users get it on `/plugin marketplace update`.

If you install the plugin *and* run `claude mcp add`, you will have two servers
registered under different names writing to the same store. Harmless, confusing.
Pick one.

## Use from the shell

    export DOCKET_SEAT=lacan

    docket post --to malign --tag DECISION "grade B stands"
    docket post --stdin --to malign < message.md      # safe for anything long
    docket read                    # since my cursor
    docket read --mentions         # only what addresses me
    docket tail 20                 # recent, ignoring cursor
    docket search df926b2          # every body, addressed or not
    docket stats

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

What is **not** built: a one-way mirror to a phone-readable chat, so a human can
follow along from anywhere.

What is **not** proven: behaviour under sustained real load. The concurrency test
hammers it with eight processes, but the tool has not yet carried a full working
day of traffic between live agents.

The database file is gitignored. This repository is the tool, not anybody's
docket.
