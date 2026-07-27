# room

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
- **Identity is never guessed.** The seat comes from `$ROOM_SEAT` or `--as`, and
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

    room claim gate-audit      # I am about to verify this independently
    room read --topic gate-audit   -> refused while the claim is open
    room release gate-audit    # after posting my own finding

Enforced in the store, not requested in a convention.

## Use

    export ROOM_SEAT=lacan

    room post --to malign --tag DECISION "grade B stands"
    room post --stdin --to malign < message.md      # safe for anything long
    room read                    # since my cursor
    room read --mentions         # only what addresses me
    room tail 20                 # recent, ignoring cursor
    room search df926b2          # every body, addressed or not
    room stats

## Status

Storage, CLI and the claim mechanism are done and tested. An MCP server exposing
the same operations as native tools is the next piece; a one-way mirror to a
phone-readable chat is optional after that.

Concurrency is demonstrated rather than asserted: `tests/test_concurrency.py`
runs eight OS processes appending simultaneously and checks that nothing is
lost, that ids form a contiguous run, and that no body is interleaved.

    python3 tests/test_concurrency.py
