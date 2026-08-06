# The armed desk — completion note (2026-08-06)

Branch `worktree-armed-desk`, over `64f129b..HEAD`. Plan:
`planning-docs/2026-08-06-armed-desk-plan.md`. Spec:
`planning-docs/2026-08-06-armed-desk-design.md`. Ledger:
`.superpowers/sdd/2026-08-06-armed-desk-plan/progress.md`.

This is the record after the whole-branch final review and its fix wave. It is
written per the repo convention — a new dated file, no rewriting of the design
or plan documents above.

## What shipped

- **Posture is a persisted choice, not a launch flag.** The owner keeps it in
  `state_path("posture.json")` (`qlab/core/posture.py`), serves it on
  `/api/tui` as `{armed, chosen}`, and records `desk.posture_chosen`.
  `POST /api/desk/posture` is the one route that sets it.
- **The startup door asks once.** The arming question is the first step in
  `clients/atlas-tui/src/ui/door.rs`; the client never latches the answer, it
  re-derives its scope from every snapshot (`store::Posture::from_desk`).
- **`--glass` survives** as a one-session, sticky read-only veto. `--operator`
  is retired everywhere: `qlab tui --operator` refuses loudly by name, and the
  Rust binary now refuses it too (see the residual, below — now closed).
- **The cargo default flipped to the armed build.** `cargo test` is the ARMED
  leg; `cargo test --no-default-features` is the GLASS leg. The
  by-construction claim narrows to the `--no-default-features` artifact, and
  CLAUDE.md, `README.md` and `clients/atlas-tui/README.md` all say so now.
- **WORKFORCE reports silence rather than animating it.** `atlas_coordinator_event`
  joined the console; the activity line says `no progress for {n}s` past the
  threshold, derived from `tool_start`/`text` recency alone.

## Ratified deviations from the plan

1. **`Command::Posture` is exempted at the dispatch seam.** `Writes::dispatch`
   carries exactly one `matches!(cmd, Command::Posture { .. })` carve-out above
   the write gate, because otherwise the arming question is unanswerable by the
   window that has to answer it. The `--glass` gate still refuses it; the
   variant carries only a bool, routes solely to `/api/desk/posture`, and does
   not exist at all in the glass build.
2. **The arming step is FIRST in the door, not after the model step.** The
   brief put it last. That ordering cannot exist without a client-side latch —
   the mode and model answers are themselves writes that an unarmed window's
   chokepoint refuses, so a door that reached arming last would spend two
   questions emitting writes nothing could accept. Esc still exits to
   read-only.
3. **The silence wording was changed from the spec's literal string.** "no word
   for 45s" → "no progress for 45s", user-approved, because the old line is
   literally false while an erroring run's prose is on screen above it. The
   design doc carries a superseded banner at that line rather than a rewrite.
4. **`qlab tui --operator` keeps the flag registered but SUPPRESS-hidden**, so
   the refusal can name a remedy (the startup door, `--glass`) instead of
   argparse's remedy-free "unrecognized arguments". `--glass` was added to the
   Python `tui` subparser for the same reason, and `--classic --glass` is
   refused rather than silently dropped.

## The residual from Task 5 — now closed

The ledger recorded, deliberately unfixed: *"the Rust binary still ignores
unknown argv, so running it directly with a stale flag is silent."* Deleting
`#[cfg(not(feature = "operator"))] fn posture()` deleted its `exit(2)` too, so
`atlas --operator` — and every typo of every remaining flag — became a no-op.
That is the same mechanism that made the original `--operator` bug invisible.

**Closed** by `unknown_args` in `clients/atlas-tui/src/main.rs`: a whitelist
(`--live`, `--glass`, `--pick`, `-v`), refused before any flag is read, exiting
2 and naming both the offending argument and what is accepted. Four tests pin
it, including the retired flag by name.

## Fixed in the final wave (things that were wrong, not merely missing)

- **An old owner got a door it could not answer.** `Store::asking_posture` used
  `posture_chosen() != Some(true)`, so `None` — an owner too old to serve the
  posture block, and therefore too old to serve `POST /api/desk/posture` —
  opened a blocking modal whose every answer 404s and whose Enter re-asks it.
  Now `== Some(false)`, with the case pinned at the rendered surface. The doc
  comment claiming "asking it is harmless" was factually wrong and is gone.
- **The honesty argument was half-applied.** CLAUDE.md and Cargo.toml were
  rewritten when the default flipped; `README.md` and
  `clients/atlas-tui/README.md` still claimed read-only-by-construction, "paper
  execution stays in the Textual client", and a status line that always says
  `GLASS`. All five sentences now narrow the claim to the
  `--no-default-features` artifact and name what protects the armed build: the
  hash-bound confirm modal, the referee PASS pinned to the same `targets_hash`,
  and the owner re-validating every write.
- **Invariant 9.** `set_posture` wrote to disk outside `_posture_lock`; two
  concurrent POSTs could leave the file and `self._posture` disagreeing. The
  disk write moved inside the lock, keeping the disk-before-memory ordering.
- **Invariant 10, two dead seams.** `workforce::activity_line` is `#[cfg(test)]`
  (production renders from `activity`, which it needs the `Silence` half of);
  `Posture.label` was deleted for want of any caller, and `DEFAULT_POSTURE` got
  the one it was missing inside `posture_payload`.
- **A weak assertion.** `golden_door.rs` pinned the post-arming landing with
  `contains("SYNTHETIC")`, which the status-line badge satisfies in every
  frame. It now asserts the mode question's own panel header.

## Two things stated because they were not

- **The posture is not a security boundary.** `POST /api/desk/posture` is
  exactly as unauthenticated as every other owner route — `/api/desk_mode`, the
  approval decisions, the execute path — all of which predate this branch. It
  is an operator's stated intent. What protects a fill is unchanged: the
  hash-bound confirm, the referee pin, and the owner's own re-validation.
  Recorded in CLAUDE.md so no reader infers otherwise.
- **Answering READ-ONLY closes the door.** A desk that has never chosen a desk
  mode *and* answers read-only is therefore never asked the mode question. That
  is consistent with what a `--glass` window already sees — a statement rather
  than questions whose answers would be refused — but it was unstated. Now in
  the `door.rs` header.

## Known and unfixed (invariant 11: a residual is a deliverable)

- `set_posture`'s `registry.record_event` is not fault-atomic with the
  disk/memory write. Brief-mandated ordering; the event is an audit row, not the
  authority. Revisit if the posture ever becomes authority-bearing.
- `qa.sh`'s `|| posture="unreachable"` fallback is dead code — the embedded
  Python always exits 0. Inert, not wrong.
- `qa_capture.py:392`'s help string still mentions `--operator` (cosmetic).
- `workforce.rs`'s pane floor (`lines.len() <= room`) drops the activity row
  when the pane is full, with no test. A short-terminal golden would pin it.
- `golden_door.rs`'s `answered()` helper now sets `forced_glass = true`, which
  re-pointed several pre-existing door tests from "featured but unarmed" to
  "vetoed window" without their names changing. The coverage is real; the names
  now describe a different subject than they did.
- The concurrency fix is witnessed by a lock-held probe, not by a real race.
  Two threads genuinely interleaving is not a test this suite can make
  deterministic; the probe fails if the disk write leaves the critical section,
  which is the property that was broken.
