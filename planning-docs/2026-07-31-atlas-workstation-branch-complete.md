# Atlas workstation branch complete — feat/atlas-ratatui-workstation

**Date:** 2026-07-31 · **Status:** build complete at `bf8718e`, awaiting soak + PR
**Plan:** planning-docs/2026-07-30-atlas-workstation-ratatui-plan.md (Tasks 1–21 of 23)

## What shipped

80 commits replacing the Textual workstation with the Ratatui client in
`clients/atlas-tui`: typed snapshot model + committed offline fixtures; tokio
runtime (drain-then-render, clock-as-argument pacing, three-exit terminal
restore); seven views; SSE with cursor resume + ts-ordered quote overlay +
flash/ticker; tachyonfx motion vocabulary with fast-wake and a 10 fps
halt-only lane; compile-time `operator` feature (glass rlib carries zero write
strings — verified repeatedly by independent reviewers), hash-bound
consent-consuming confirm modals, typed Executed/Refused/Err outcomes; slash
command palette + KEYMAP-verified help; in-repo pty QA (`scripts/qa.sh`);
`qlab tui` cutover (Rust default, `--classic` soak valve, `--operator`
forwarded, combo refused loud). Owner-side: approvals payload widened
(pending + approved-unconsumed, GET-only) with the Textual client guarded.
Final state: glass 540+/operator 630 cargo tests, ~1,067 pytest, fmt/clippy
clean both legs. Every task passed independent review; every finding fixed
and re-reviewed or parked with a recorded ruling.

Process note: 21 tasks ran through implementer → reviewer → fix → re-review
loops. Recurring bug classes this surfaced and closed: dead-contract reads
(4 instances across both clients), raw-threshold-beside-rounded-print
(4 instances; a dust-magnitude fixture row now trips it permanently),
wall-clock reads in decision logic (banned globally), sub-floor silent
truncation (shared refusal discipline), invisible armed states (picker floor).

## Merge notes (surface in the PR body)

- `qlab tui` now **execs** the Rust binary: a spawned owner **outlives the
  client**; no `qlab owner stop` verb exists; invariant 8 (restart owner
  after code changes) is now an operator responsibility. `--classic` still
  reaps. Next `qlab tui` attaches to the bound port.
- Commit `cfa5240` intentionally mixes this branch's Task-21 fix round with
  the owner-side reasoning-surface work done in a parallel session (user
  directive: never rewrite it). File-level split is in the session records.
- `z` zen / `f` fullscreen from the plan's Part I never shipped (keys
  reserved in KEYMAP, disclosed in-source).

## Task 22 entry checklist (post-soak: delete qlab/tui, docs rewrite)

- CLAUDE.md: rewrite the `clients/atlas-tui` paragraph (glass/operator
  postures; "read-only by construction" is true of the **default build**);
  repoint invariant 3's TUI sentence at the hash-bound modal; retire the
  flaky quote-repaint memory note (class extinct under time-as-data).
- Record the two owner gaps: no HTTP halt/resume (MCP-only); template phase
  graphs forced to WORKFORCE_PHASES for network callers (3 of 8 templates
  declare different graphs).
- Record the color-parity-by-design decision (Obsidian remap vs old Textual
  hexes).
- Ride items from the final review: refuse() legibility floor + pulse compact
  mode; fold_triggers extraction (lift like Task 18's dispatch); halt breath
  freeze (soak decides); STREAM/SSE toast naming; PLAN_CARDS band + glass
  "+N more" affordance; char-vs-cell widths; contradictory glass /mode
  messages; help "anywhere" q/Esc claim; catalog id clip; mcp_configured
  dust; tristate_spark/table_cell pub hygiene; fixture book-sum consistency
  (positions $4,638 vs equity $10,000 — captured-payload trim).
- The SDD ledger (`.superpowers/sdd/2026-07-30-.../progress.md`, gitignored)
  holds the full per-task record until Task 22 completes; delete after.

## Task 23 (stretch, unbuilt)

Flight-recorder replay over the audit bus (`atlas --replay <from-ts>`) —
the event-driven architecture makes it ~200 lines; glass-only by design.
