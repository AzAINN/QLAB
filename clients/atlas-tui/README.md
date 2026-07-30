# atlas-tui

A Ratatui client for the qlab owner runtime. Atlas-first: the desk manager and
its reasoning own the frame, and the book supports it — rather than Atlas being
one rail beside eight peer views.

```bash
cargo run                 # synthetic lane, against the owner on QLAB_UI_PORT
cargo run -- --live       # live data lane
cargo test                # 22 tests, no owner required
```

## What it is

Read-only by construction. It speaks HTTP to the owner and has **no order path,
no registry handle, and no way to acquire either** — invariant 3 is preserved by
absence, not by a check someone could later remove. Paper execution stays in the
Textual client, where the confirm dialog lives.

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

## What is not here yet

The first slice covers the owner client, readiness, the Atlas-first layout, one
live view, and the glyph. Not yet ported: the market chart, the book detail, the
workforce flowchart, the audit trail, Settings, and the command palette. The
Textual client remains the complete surface.
