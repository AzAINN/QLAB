# atlas-tui

A Ratatui client for the qlab owner runtime. Atlas-first: the desk manager and
its read are on screen in every view, in a rail that never goes away — rather
than being one tab among peers that an operator has to remember to open.

```bash
cargo run                 # synthetic lane, against the owner on QLAB_UI_PORT
cargo run -- --live       # live data lane
cargo test                # fully offline: committed fixtures, no owner required
```

## What it is

It speaks HTTP to the owner and **never holds a registry handle, in any
build** — every write it can make goes through the owner's governed API.

The by-construction claim narrows to one artifact: built with
`--no-default-features`, this crate contains no `net::write`, no confirm modal
and no `Posture::Operator`, so invariant 3 holds there by absence rather than by
a check someone could later remove. That is the build a monitoring box runs, and
it cannot be argued, configured or flagged into writing.

The default build — what `cargo build --release` and `qlab tui` produce — is
armed, and is protected the way the Textual client is: a fill needs the last six
of the plan's own `targets_hash` typed into `ui::widgets::confirm`, the referee
PASS is pinned to that same hash, and the owner re-validates every request and
refuses without a persisted approval. What the armed build may *do* is the
desk's answer rather than a launch flag: the owner persists a posture, serves it
on `/api/tui`, and the client re-derives its scope from every snapshot.
`--glass` is this window declining that authority for one session.

It runs *alongside* the Textual TUI. Both read the same `/api/tui` snapshot, so
there is no cutover cliff and no window in which the desk has two faces that
disagree about it.

## The glyph

Atlas has a face (`src/glyph.rs`) — a braille automaton in four moods, derived
from desk facts rather than set by a caller:

| mood | when | tempo |
|---|---|---|
| `WORKING` | a coordinator is walking a workflow's phases | 12 fps |
| `WATCHING` | mode permits work, nothing has fired | 4 fps |
| `DORMANT` | Observe or Paused | 2 fps |
| `HALTED` | the kill switch is tripped | 8 fps, blinking |

Mood is *derived* (`Mood::from_desk`) so the animation can never say "working"
while the desk is halted. An animation that can disagree with the status field
is worse than no animation.

## The honest ceiling

This is a terminal cell grid at ~10 fps. Braille gives 2×4 subpixels per cell,
which is enough for the glyph to read as motion and enough for charts — and it
is the same ceiling Textual has. **Rust did not buy a better animation ceiling.**

What it does buy: no GC pauses in the render path, a single static binary, and
render code that is testable frame-by-frame against a `TestBackend` rather than
only by rasterising SVG. If the goal ever becomes genuinely fluid graphics, the
answer is a GPU surface or a web client, not a faster TUI framework — and
pretending otherwise would just produce a worse Textual.

## The shell

One frame is five regions: a ticker row, an eight-cell nav rail, the active
view, the pulse rail, and a status line that always states the posture — `GLASS`
when this window holds no writer, whether because the build has none, because
`--glass` declined it, or because the desk is not armed. Keys `1`–`7` and `Tab`/`BackTab`
switch views, `r` jumps the poll queue, `q` quits.

`shell::draw` is a pure function of the store — no clock read, no socket, no
client — so `tests/golden_shell.rs` pins the whole frame as text through a
`TestBackend`. Every view task adds its golden beside it.

## What is not here yet

Of the seven views only DESK has content, and its tiles are placeholders; the
rest name the task that builds them. Not yet here: the market chart, the book
detail, the workforce flowchart, the audit trail, Settings, live quotes, the
effect manager, and the command line. The Textual client remains the complete
surface.
