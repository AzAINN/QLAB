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
`--no-default-features`, this crate contains no `net::write`, no confirm modal,
no `Posture::Operator`, and no pty, spawn or forwarded keystroke, so invariant 3
holds there by absence rather than by a check someone could later remove. That is the build a monitoring box runs, and
it cannot be argued, configured or flagged into writing.

The default build — what `cargo build --release` and `qlab tui` produce — is
armed, and a fill costs exactly one explicit confirmation: `ui::widgets::confirm`
shows the allocation and the last six of the plan's own `targets_hash`, Enter
posts that hash, the referee PASS is pinned to the same hash, and the owner
re-validates every request and refuses without a persisted approval. One click,
never zero — booking a proposal that way is one call
(`POST /api/desk/proposal/book`) which approves and executes, not two boxes
asking for the same hash twice. What the armed build may *do* is the desk's
answer rather than a launch flag: the owner persists a posture, serves it on
`/api/tui`, and the client re-derives its scope from every snapshot. `--glass`
is this window declining that authority for one session.

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

One frame is five regions: a ticker row, a ten-cell nav rail, the active
view, the pulse rail, and a status line that always states the posture — `GLASS`
when this window holds no writer, whether because the build has none, because
`--glass` declined it, or because the desk is not armed. Keys `1`–`9`, `0` and
`Tab`/`BackTab` switch views — ATLAS, DESK, MKTS, BOOK, RSCH, PRED, WORK, AUDIT,
SETT, VIS, in that order, because the digit keys index `ViewId::ALL` — `r` jumps
the poll queue, `q` quits.

The keys that do something beyond navigating are declared once in `input.rs`,
which is also what the `?` overlay draws, so a key and its help cannot drift:

| key | where | what |
|---|---|---|
| `b` | ATLAS, BOOK | book the desk's current proposal — opens the confirmation box |
| `i`, `ctrl-]`, `c` | ATLAS | the `/cli` terminal pane: hand it the keyboard, take it back, close it once its child has ended |
| `r` | PRED | refresh the board, and offer to run one of its lanes |
| `Home` / `End` | VIS | the left and right edges of the drawing |
| `m`, `↑↓`, `space` | SETT ▸ MODELS | pick a model; grant or withdraw a right |

The MODELS card's three rights read `on`/`off` with what each one reaches —
`web · chat, /cli tools`, `workflows · refused for chat`, `build · the /build
key` — over one fixed line, `nothing here binds a non-chat caller`, which is the
half no row can carry: a `qlab workforce run`, the owner's own coordinator, the
heartbeat and a non-Claude reasoner are bound by none of them.

`shell::draw` is a pure function of the store — no clock read, no socket, no
client — so `tests/golden_shell.rs` pins the whole frame as text through a
`TestBackend`. Every view task adds its golden beside it.

## What is not here yet

The rail is out of digits: VIS sits on `0`, so an eleventh view fails loudly in
a store test and needs a numbering decision before it can be added.
