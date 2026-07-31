# SSE cursor property: a quote can advance past a late-committed audit row

**Date:** 2026-07-30 · **Status:** accepted owner-contract property, documented per invariant 11
**Found during:** atlas-tui Ratatui rewrite, Task 7 (resumable SSE client), confirmed by task review against owner source.

Both TUI clients resume `/api/stream` with a cursor (`after` + `after_id`) taken
from the last event seen — durable audit events and transient quote events both
carry `ts` + `event_id` (`qlab/ui/server.py:2148-2151`), so a quote can advance
the cursor past an audit row that commits with an earlier timestamp after the
quote was published.

Client-side defense is not possible: the owner reads audit rows under `_LOCK`
but market events outside it (`server.py:2957-2965`), and inside one live
subscription it advances its **own** in-connection cursor on any event
(`server.py:3021-3030`) — the skip happens server-side where the client's
cursor is never consulted. Holding the client cursor back to the last *audit*
event would not close the window and would replay quotes on every reconnect.

Decision: the Rust client keeps parity with `qlab/tui/client.py` semantics.
The exposure window is one lock handoff; a missed audit event is re-served by
the periodic `/api/tui` aggregate refetch (3 s cadence, immediate on
state-changing SSE kinds), so the desk converges even when the stream skips.
If the skip ever needs to be closed properly, it is an owner change: publish
market events through the same cursor-ordered path as audit rows, or exclude
transient kinds from cursor advancement in `read_market_events` consumers.
