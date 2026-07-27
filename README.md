# agentdocket

A shared, append-only log for coordinating several Claude Code sessions working
on one project. SQLite, standard library only, nothing leaves the machine.

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

    uv tool install --editable .     # or: pipx install -e .

Gives you two commands: `docket` (CLI) and `docket-mcp` (MCP server). Neither is a
compiled binary; they are console-script launchers. Python 3.10+, no
dependencies.

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

Storage, CLI, claims and the MCP server are done and tested. A one-way mirror to
a phone-readable chat, so a human can follow along from anywhere, is the obvious
next piece and is not built.

The database file is gitignored. This repository is the tool, not anybody's log.
