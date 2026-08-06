# Atlas acts — proposals you approve, and a desk that can press the button

**Date:** 2026-08-06 · **Status:** design accepted, implementation next
**Scope:** sub-project B of three. A (the armed desk) merged as PR #4; C (the
predictor lane) gets its own spec after this one lands.

## Why

The ask was: *"I want Atlas to be able to start the workflow itself and not tell
me what should be done but when I ask it, it should give me actionable items
that it thinks need to be done and then it does those things."*

Reading the code first changed what this project is. **Atlas already starts
workflows unattended.** `qlab/operator/heartbeat.py:169` calls
`atlas_run_startable(offline)` on every beat, one task per beat, and autonomy
defaults on (`QLAB_ATLAS_AUTONOMOUS` unless `"0"`). The whole governed chain
exists and works: `startable_tasks()` gate-checks each queued task and returns
its refusal reason, `start_task()` re-checks authority before any runner is
called, `atlas_workflow_runner` dispatches the same governed workflow a human
would start, and `reconcile_tasks()` resolves it from the workflow's terminal
state rather than from the fact of dispatch.

So the missing thing is not autonomy. It is three specific gaps:

1. **Asking Atlas produces prose, not actionables.** `POST /api/atlas/ask`
   returns a `ReasonedView` whose only actionable field is `offer` — one
   gate-checked template id, singular, with no ranking and no reason attached
   to it as an *item*.
2. **An offer cannot be acted on.** `start_task` needs a queued task row, and
   those are minted only by the observe tick's triggers. An offer produced by
   answering a question has nothing to start, so every answer is a dead end by
   construction.
3. **The desk cannot see any of it.** The Rust client references no
   `startable`, no task-start command, and never renders `offer`. That absence
   is the whole reason the desk feels like a thing that only talks.

## The envelope, decided

**Proposals require explicit approval. Every one, every time.**

The existing trigger-driven heartbeat autonomy keeps running exactly as it does
today — it was not turned off, and this project does not touch it. What is new
is a second origin for tasks, and that origin is *attended by construction*.

This has one sharp consequence that the design must handle rather than
discover: `atlas_run_startable` starts **any** startable queued task. If
proposals land in the same table with no distinction, the very next heartbeat
will start them, and the approval gate this project exists to build would be
decorative on arrival.

## B1 · A proposal is a task with a different origin

`atlas_tasks` gains one column, `origin VARCHAR`, via the file's established
`ALTER TABLE … ADD COLUMN IF NOT EXISTS` migration pattern (`registry.py:365`
and following). Two values: `trigger` (everything that exists today, and the
default a NULL row reads as) and `proposal`.

- **`atlas_run_startable` filters to `origin = 'trigger'`.** This is the
  envelope in one line, and it is the line to delete when proving the guard.
  A proposal is never started by the heartbeat, in any mode.
- **`startable_tasks()` is unchanged.** It already reports per-task startable
  and the refusal reason, and a proposal is subject to exactly the same gate:
  mode, staleness, retry budget, plan-creation boundary.
- **The dedupe key keeps its shape** — `kind|trading_date|universe|state_hash`.
  This is not cosmetic: `_task_age` reads the trading date out of that key, and
  a proposal minted with any other shape reads as *age unknown*, which
  `startable_tasks` refuses. Asking the same question twice on the same day
  against the same facts is the same proposal, not two.
- **The daily autonomous workflow budget does not bound approvals.**
  `_within_daily_budget` bounds *unattended* launches, and an approved proposal
  is attended by definition. It remains bound by the retry budget, the mode
  gate, and the plan-creation boundary — the things that are about authority
  rather than about how many workflows a sleeping desk may start.

## B2 · Asking Atlas yields a ranked list

`atlas_reason` keeps returning what it returns. Alongside it, a new owner
method composes **actionables**: for each candidate template the current mode
and facts permit, an item carrying the template id, a one-line reason drawn
from the facts that make it worth doing, and the gate verdict.

- Candidates come from `startable_templates(mode, facts)` — the same catalog
  the authority gate composes. Nothing invents a template id.
- Each item is gate-checked at proposal time **and again at execution time**.
  The second check is not redundant: the mode can change, a plan can appear,
  and a proposal made in Research must not execute on a permit it no longer
  holds. `start_task` already re-checks; this design's contribution is to never
  let an id skip it.
- Refused candidates are shown with their refusal, not hidden. A desk that
  silently omits what it will not do teaches the operator nothing about why.
- **No item can create a paper plan below Propose.** That is not a new rule;
  it is `check_startable` doing what it already does, and the actionables list
  inherits it because it asks the same function.

New route: `POST /api/atlas/actionables` returns the ranked list and persists
each item as a `proposal`-origin queued task, so approving one is the existing
`POST /api/atlas/tasks/<id>/start` and no second execution path is introduced.

## B3 · The desk shows them and can press the button

The Rust client grows the surface it never had:

- The ATLAS view renders the actionables list — item, reason, and either
  *approve* or the refusal that stands in its place.
- Approving dispatches through `Writes::dispatch`, the single write chokepoint
  established in sub-project A. Approval is a write: an unarmed or `--glass`
  desk sees the list and cannot act on it, which is the correct reading of
  "read-only" and needs no new mechanism.
- Once approved, the run is already visible — the WORKFORCE console carries
  the agent's own words and the activity line reports silence honestly, both
  shipped in sub-project A. This project adds no second progress surface.

## Testing

- The envelope: a `proposal`-origin task is never started by
  `atlas_run_startable`, in every mode; a `trigger`-origin task still is.
  Delete the origin filter and confirm the first test fails.
- The dedupe shape: a proposal's key parses to a trading date, so `_task_age`
  reads it as fresh rather than unknown; asking twice on one day against
  identical facts creates one task.
- The gate: an actionable refused at proposal time is listed with its reason;
  an actionable permitted at proposal time and refused at execution time is
  refused by `start_task` — the mode-changed-underneath case, tested on both
  sides.
- The boundary: no actionable that creates a plan is offered below Propose.
- The client: the list renders with reasons; approve is offered only when the
  desk is armed; a refused item offers no approve affordance.

## Out of scope (named, so it is not silently assumed)

Changing or disabling the existing heartbeat autonomy; standing per-template
approvals (the envelope explicitly chosen against); anything predictor
(sub-project C); a new execution path of any kind; widening what any mode
permits.
