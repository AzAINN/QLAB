# TUI / owner-surface code review — 2026-07-27

## Scope

This document records a review pass over the nine commits in
`b495aa6..fec88d4`, the TUI/owner change set that was split out of a single
working tree and merged without a dedicated review. The range covers the
design-token system (`qlab/tui/design/`), TUI lifecycle and threading in
`qlab/tui/app.py`, the owner runtime `qlab/ui/server.py`, Atlas dispatch honesty
in `qlab/operator/atlas.py`, and phase-graph dependency validation in
`qlab/state/registry.py`.

Every finding below was reproduced before it was recorded. Findings that were
fixed carry the commit; the rest are open and ranked.

The dominant failure class is the one CLAUDE.md names first: **a real failure
becoming a plausible-looking answer**. Six of the eleven findings have that
shape, and one of them could move the book. Nothing found was a missing feature;
everything was an error path returning a confident wrong result.

## Fixed on `fix/review-findings`

### 1. An unparseable POST body was replaced with `{}`, which could book a trade

`do_POST` substituted `{}` for any body it failed to parse. Route defaults are
permissive by design — `/api/run_once` reads `bool(body.get("execute", True))`,
so **an empty body means execute**. A caller that sent `{"execute": false}` and
lost bytes in transit got no error: the desk booked the paper trade and answered
`200` as though that had been asked for. Every other POST had the same shape at
lower stakes — `/api/recommend` returning an allocation computed from default
`skew`/`kurt` rather than the ones sent, indistinguishable from a correct answer.

Fixed in `4ca83db`: an unparseable or non-object body is a `400` naming the
fault; a genuinely absent body still means the defaults; an unusable
`Content-Length` closes the connection so the next keep-alive request is not
framed against unread bytes.

### 2. A dollar amount in console text crashed the write

`markup.resolve` substituted `$name` across the whole console line and its name
pattern accepted digits, so `equity $100,000.00` parsed `$100` as a theme
variable and raised `KeyError` out of `_console_write`. On this desk that is most
lines, and the text can originate in the operator's own chat message. Introduced
with the design-token system; the design tests only ever exercised well-formed
markup.

Fixed in `a490503`: substitution is scoped to inside `[...]` tags — the only
place a colour can apply — and names must start with a letter. A mistyped token
inside a tag still fails loud.

### 3. A recomposed flow board reverted to the default theme

`set_flow` recomposes the board on a worker, and `_paint_flow_node` pushed
`state`, `pulse` and `detail` but never `theme_name`. `action_theme` only reaches
nodes that existed when it ran, so a workflow respec after a theme switch left a
single board rendering two themes at once.

Fixed in `b55e596`, by pushing the theme where the other state is pushed —
`on_mount` paints through that method, so mount-time is correct.

### 4. A failed desk-read recompose kept passing as current

The heartbeat swallows a recompose failure so the supervisor keeps observing,
which is right — but the previous tick's payload stayed cached, and
`atlas_facts` derives `news_window_items` from exactly that. The `news_read`
precondition then admitted a read against a window that was no longer current.

Narrower than first reported: a news *outage* was already handled correctly
(`compose_desk_read` records `news_error` and says the qualitative side is
missing rather than quiet). Only a *raising* recompose — e.g. `ground()` on a
malformed record — reached the stale path.

Fixed in `1ac1334`, giving the raising case the same treatment as the outage.

### 5. The event stream went silent while a long action held the lock

A stream poll waited indefinitely for `_LOCK`, and the idle ping is emitted
*after* that wait. While a long owner action held the lock the socket went
silent, the client's 15 s read deadline expired, and the replacement connection
blocked on the same lock — each retry stranding a thread that would later wake
only to write to a closed socket.

Fixed in `e09e217`: the poll bounds its wait and pings instead of blocking. The
client's read deadline is now a named constant because it and the server's wait
are one contract across two modules; a test pins the relationship rather than the
timing, which would only have produced another load-sensitive test.

### 6. Stale docstring contradicting a passing test

`_register_design_themes` still claimed chrome was not theme-reactive. It has
been since the stylesheet moved to token references, and
`test_switching_theme_repaints_chrome_not_just_content` pins it. Corrected in
`b55e596`.

## Open — ranked

### O1. `desk_read()` does network I/O under the owner dispatch lock on a cold cache

`655b86f` moved the fetch outside the lock for the heartbeat and the explicit
refresh path only. `tui_snapshot`, `/api/atlas/read` (non-refresh),
`/api/atlas/startable` and `POST /api/atlas/observe` all reach `desk_read` under
`_LOCK`; when `_desk_read` is still `None` that calls `refresh_desk_read` →
`fetch_desk_news` → six RSS feeds at 5 s each. A TUI attaching to a cold
`--online` owner can freeze the whole desk for ~30 s.

Suggested fix: cache the last fetched news window, populated only outside the
lock, and have `desk_read` compose from it — emitting the existing `news_error`
observation when nothing has been fetched yet. Same shape as the fix already
applied to the heartbeat.

### O2. `?offline=0` is parsed as offline on the POST path

`qlab/ui/server.py:1882` computes `bool(query.get("offline", ...)[0])`, and
`bool("0")` is `True`. A POST asking for **live** data silently gets synthetic
data and returns `200`. `_qbool` already exists and handles this correctly; the
POST path never adopted it. Same silent-wrong-answer class as finding 1, and the
distinction between live and synthetic is not cosmetic here.

### O3. The TUI never drains the owner's stderr pipe

`_cmd_tui` spawns the owner with `stderr=subprocess.PIPE` and reads it only on
the failure paths. Once the 64 KB pipe buffer fills the owner blocks on write —
a hard wedge with no diagnostic at all. Suggested fix: drain into a bounded ring
buffer on a daemon thread, which also gives the failure paths better output.

### O4. `ApiClient.stream` loses its resume cursor across a reconnect

The outer retry in `_start_live_stream` constructs a fresh `stream()` call, so
`request_params` carries no `after` and the new connection gets a 25-event
primer. Any outage spanning more than 25 events drops them silently. Related:
`if last_cursor is None: return` gives up permanently if a connection closes
before delivering a cursored event.

### O5. One malformed quote frame kills the stream permanently and invisibly

`_apply_quote_event` raises `ValueError` on a malformed payload. That propagates
through `call_from_thread` into the SSE loop's `except Exception: pass`, so a
single bad frame drops the desk into a silent 2 s reconnect loop with no
surfaced error.

## Notes on method

Two initially-reported findings were narrower or wrong once reproduced: the
desk-read staleness (finding 4 — real only for a raising recompose, not for a
news outage), and a claim that bottom dashboard tiles were clipped, which was
false — `.canvas-view` sets `overflow-y: auto` and the content scrolls. The real
issue there is density, not overflow. Reproducing before recording is what
separated these; the review is more useful for having dropped the overstated ones.

Test policy here is deliberately narrow: one test per fixed edge case pinning the
failure mode, and no test where the only available assertion would be
timing-dependent.
`tests/test_tui.py::test_quote_event_repaints_only_market_pulse_and_universe` is
the cautionary example already in the suite — it waits `pilot.pause(1.05)`
against a 1.0 s `_QUOTE_REPAINT_INTERVAL` and fails under load. Making it await
the repaint rather than sleep past it is worth doing.
