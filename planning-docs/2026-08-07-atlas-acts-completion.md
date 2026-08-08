# Atlas acts — what shipped, and what did not

**Date:** 2026-08-07 · **Status:** complete, offered for merge
**Spec:** `2026-08-06-atlas-acts-design.md` · **Plan:** `2026-08-06-atlas-acts-plan.md`
**Branch:** `worktree-atlas-acts`, 21 commits off `8ee2866` (stream A merged as PR #4).

Sub-project B of three. A (the armed desk) merged. C (the predictor lane) follows.

## What shipped

`/ask` on the command line puts the question to the desk: the owner walks the
registered template menu, gate-checks every entry against the current mode and
facts, and answers with a ranked list. Each permitted entry becomes a
`proposal`-origin queued task; each refusal is recorded with the gate's own
sentence. The Rust desk draws both halves in the ATLAS sidebar, and `/do
<template>` approves one — through the same write chokepoint every other write
uses, so an unarmed or `--glass` window can read the list and cannot act on it.
Approval POSTs the owner's existing start route, which re-runs `check_startable`
before anything begins. A heartbeat sweep then drives an approved workflow that
could not be driven when it landed, because registering a workflow is not
running it.

## The envelope, as built

**Proposals require explicit approval; the existing trigger-driven autonomy was
not touched.** `atlas_tasks` gained one `origin` column, and
`atlas_run_startable` — the beat's starter — skips anything whose origin is not
`trigger`. That one line is the envelope, and it is the line deleted to prove
the guard. A NULL origin reads as `trigger`, because every row written before
the column existed was trigger work; an empty origin is refused at the writer
rather than resolved into a permit.

The envelope began positional (the beat declines to start proposals) and was
made evidential: `atlas_start_task` writes an `atlas_proposal_approved` row
before it starts a proposal-origin task, so "which route was hit" is no longer
the only record that a human approved.

## Ratified deviations

- **The arming question of this stream is `/ask`, not a key.** The ATLAS pane is
  chat-first: every printable character belongs to the question row, so a letter
  key would be stolen from the operator's sentence. `/ask` sits in the same
  grammar as `/do` and hides from an unarmed picker identically.
- **`check_startable` was split** into `check_authority(template_id, mode)` and
  the facts-dependent half, and `_task_age` became public `task_age`, so the
  snapshot path can rule on registration, status, age and mode **without**
  calling `atlas_facts`. That prohibition is load-bearing:
  `_atlas_regime_facts` latches `_last_robust_state`, and a two-second poll that
  called it would consume every regime flip before the observe tick saw it,
  silently suppressing the desk's own `regime_flip` trigger.
- **The snapshot's `startable` is tri-state and `true` is unreachable there.**
  `null` means nothing refused the item and the verdict lives at the POST;
  `false` carries the gate's reason. The snapshot never asserts a verdict it did
  not compute — the precedent is `news_search`, which carries no `offer` field
  for the same reason.
- **Proposals are minted as `proposal:<template_id>`**, deliberately outside
  `_WORKFLOW_TRIGGERS`, so an unapproved proposal cannot consume the unattended
  desk's daily workflow budget. Two independent locks hold that property: the
  kind, and an `origin="trigger"` filter in the budget's own SQL.
- **The dedupe key keeps the existing shape** `kind|trading_date|universe|state_hash`,
  because `task_age` parses the trading date out of it and a key it cannot parse
  reads as *age unknown* — which the gate refuses. A proposal minted any other
  way is refused by the very gate meant to permit it.
- **The sweep drives one workflow per call and screens candidates first.**
  `available(roles)` is asked before `drive()`, so a candidate the driver would
  refuse costs no attempt and cannot starve the approvals behind it.

## Known and unfixed

Recorded rather than silently carried, per invariant 11.

1. **A merged refusal is the ask's verdict as of the ask.** Widen the mode after
   asking and the panel replays "requires Propose mode; Atlas is in Research"
   until the next `/ask`. It fails safe — a stale refusal never becomes an
   approve affordance — but the mode half is recomputable and is not recomputed,
   and the client decodes no `trading_date`, so the replayed verdict is undated
   on screen. A `check_authority` re-check on merge would close it.
2. **The sweep's pre-screen costs one `available()` per parked task per beat**,
   each ending in an uncached `shutil.which("claude")` when the graph routes
   there. Previously bounded at three. Bounded above by `TASK_SCAN_WINDOW = 200`.
3. **A coordinator that dies fast is respawned once per beat** until the
   35-minute stale lease expires — with three parked tasks that is roughly 210
   attempts, not the ~70 first estimated. The beat now reaps, so it is bounded
   in time even on a headless desk; a drive-side retry budget with its own
   persisted counter is the real fix.
4. **The classic Textual TUI is proposal-blind.** Its task panel is now served
   `origin="trigger"` so proposals stop crowding out real autonomous work, and
   it never read the `actionables` block — so a classic-only operator sees
   nothing of proposals another client asked for.
5. **The audit row's `refused` count and `refusals` list can disagree**: the
   count includes spent-proposal refusals, the list carries menu refusals only.
6. **Before the first ask of a day the panel offers yesterday's answer**, and
   after `max_task_age_days` with no ask every item reads `stale` with no
   self-clearing path. `_expire_stale_proposals` runs only at mint time.
7. **`/ask` costs two Enters** (one accepts the scope, one acts), like `/mode`.
   It is the first argument-less scope.
8. **Stale `server.py:NNNN` citations in the Rust client are repo-wide**, not
   the two this branch fixed. The class is worth one sweep; line numbers drift
   and naming the function does not.

## Verification

Python full offline suite 1550 passed / 10 skipped. Rust ARMED 853, GLASS 652,
`cargo clippy --all-targets` and `cargo fmt --check` clean on both legs.
Every new guard was deleted, witnessed failing, and restored.

Nothing was exercised against a live owner. **Restart the owner after merge**
(invariant 8) — a long-lived owner keeps serving pre-change imports, and the
`ALTER TABLE atlas_tasks ADD COLUMN IF NOT EXISTS origin` migration has been
tested against a pre-column table but not against the desk's real registry.
