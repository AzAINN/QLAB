# The armed desk — posture as a choice, and a desk that shows its work

**Date:** 2026-08-06 · **Status:** design accepted, implementation next
**Scope:** sub-project A of three. B (Atlas proposes and runs actionables) and
C (the predictor lane: train/test, vivid regression, allocation actionables)
each get their own spec after this one lands.

## Why

Two complaints, one theme — the desk makes the operator repeat themselves and
then leaves them guessing.

1. Arming the workstation takes a flag on every launch (`--operator`), which is
   a decision the operator already made and the desk refuses to remember.
2. When a governed workflow runs, the client shows a status word. Whether the
   thing is *thinking* — and whether it can run at all — is invisible until it
   finishes or doesn't.

The data for both already exists. Neither needs a new subsystem.

## A1 · Posture is a remembered choice

### Today

Two gates in series (`clients/atlas-tui/Cargo.toml` `[features] operator = []`,
`clients/atlas-tui/src/main.rs:126-161`):

1. the cargo feature decides whether write code is compiled into the artifact
   at all — no `net::write` call site, no `Posture::Operator` variant, no write
   `Command` variants;
2. `--operator` decides whether a compiled-capable binary is armed.

Only the second gate annoys anyone. Everything downstream already asks
`Posture::writes()` (`store.rs:349`), never `cfg!`, so the arming decision has
exactly one consumer to change.

### The change

- **The owner persists it** in its own file, `state_path("posture.json")`,
  written and read by a small module mirroring `qlab/core/desk_mode.py`
  (frozen dataclass; an unreadable file means "not chosen yet", never an
  error). Its own file rather than a key inside `desk_mode.json`, because the
  two answer different questions and a desk-mode write must not be able to
  disarm the client as a side effect. Served on `/api/tui` as
  `{armed: bool, chosen: bool}` — `chosen` false on a desk never asked (the
  `desk_mode.chosen` precedent), set true by the POST that answers it.
  New route: `POST /api/desk/posture {armed: bool}`.
- **The door asks once.** Posture becomes the door's fourth question, shown
  only when `chosen` is false. Esc means read-only — the safe answer, matching
  the door's existing rule that Esc never lands on the live book.
- **The client obeys the snapshot, not its arguments.** `Posture` is derived
  from the first snapshot: `Operator` iff the binary is operator-featured
  **and** the owner reports `armed: true`. `--glass` remains as a one-session
  override forcing read-only; `--operator` is retired (a flag that can no
  longer widen anything is a lie about where the decision lives).
- **The build default flips.** `default = ["operator"]`, so
  `cargo build --release` produces the desk binary in daily use. The
  by-construction artifact survives as `--no-default-features`.

### The honesty consequence, stated not buried

`CLAUDE.md` invariant 3 and the `Cargo.toml` comment currently claim the client
is "read-only by construction — the write code was never compiled." After this
change that sentence is true of the `--no-default-features` build, not of the
desk. Both texts are rewritten to say exactly that: the by-construction
guarantee narrows to the monitoring artifact, and the armed desk's protection
is what it always actually was — the hash-bound confirm modal, the referee pin,
and the owner validating every write regardless of what the client believes.

A desk that lies about its own guarantees is worse than one with a smaller
guarantee honestly described.

### Why snapshot-derived posture is safe

The owner is the thing being written to. It gains nothing by claiming a client
is armed: every write is validated owner-side, `human_confirmed` is still
required for execution, and the referee pin is enforced in the routing layer,
not the client. The client's posture governs what it *offers the operator*,
which is a UX fact, not an authority fact.

## A2 · The desk shows its work

### Today

`atlas_coordinator_event` rows are durable, `_head`-redacted and one-line
bounded at the source, and already stream to the Rust client over the SSE feed
it reads (`coordinator.py:89,543`; `net/sse.rs`). The WORKFORCE console filters
them out (`workforce.rs:59-66` `CONSOLE_KINDS`). `coordinator_status()`
(`server.py:3105`) carries `driving` — the only evidence distinguishing a
*registered* workflow from a *running* one, as the view's own doc comment says.

### The change

- **Live transcript.** `atlas_coordinator_event` joins `CONSOLE_KINDS`. The
  console renders the agent's own words and tool traffic as they arrive —
  `moments-analyst · calling moments_estimate`, its text, its results — with
  kind-derived tone and the established flash-on-arrival.
- **An activity line that is derived, never faked.** The active phase node
  carries liveness computed from the newest coordinator event's age against the
  loop's `now` (time-as-data, mocked-clock tested), plus driving-vs-parked from
  `coordinator_status().driving`.
- **Silence is reported.** Driving with no event for ~45s renders
  `no word for 45s`, not an animation implying activity. A spinner that spins
  regardless is the failure this design refuses.
- **The suppressed kinds stay suppressed.** `session`/`task_progress` were
  removed from the durable bus deliberately (42 of 60 rows were liveness noise
  that buried real reasoning). Liveness here is derived from `tool_start`/`text`
  instead — the fix is not undone to build a progress bar.

## Testing

- Posture: the three-state matrix (never asked / armed / read-only) × client
  arms-or-not; `--glass` overrides an armed owner; a glass-featured binary
  ignores `armed: true` (the cargo gate still wins); the door asks only when
  `chosen` is false and Esc leaves it read-only; the owner round-trips the
  choice across a restart.
- Working view: console renders coordinator kinds with the right tone; the
  activity line's age arithmetic at mocked instants; the silence threshold on
  both sides; parked-vs-driving; goldens for the WORKFORCE view in both
  postures. Guard-deletion proof per new guard, a case per side of every
  comparison, per the standing rules.
- Both matrix legs green (`cargo test`, `cargo test --features operator` — note
  the legs swap meaning: the default leg is now the armed one, and the glass leg
  becomes `--no-default-features`), plus the touched Python modules.

## Out of scope (named, so it is not silently assumed)

Atlas proposing or running actionables (sub-project B); anything predictor
(sub-project C); re-admitting suppressed event kinds; a progress percentage
derived from anything other than declared phases; changing what the owner
validates.
