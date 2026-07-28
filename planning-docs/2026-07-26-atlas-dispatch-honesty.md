# Atlas dispatch honesty and executable template graphs

Date: 2026-07-26
Status: implemented; full offline suite green at 843 passed, 9 skipped
Scope: the P1 from `2026-07-26-tui-code-review-and-architecture.md` — Atlas
recording completion for work it had not done — plus a related defect that
review did not catch

Companion to `2026-07-26-tui-code-review-and-architecture.md`, which owns the
target architecture and the migration phases. This file records what was
actually implemented and one finding that changes the picture.

## The reported defect

`AtlasSupervisor.start_task` completed a task from any normal runner return.
`UISession.atlas_workflow_runner` returned a workflow *handle*. So creating a
durable workflow row was recorded as the research having been done, producing
the contradiction the review reproduced:

```text
Atlas task: completed
Workflow:   running
Analyst:    queued
```

## The finding the review did not catch

Four templates — `regime_review`, `research_review`, `risk_event`, and
`news_risk_review` — declared the phase graph
`(analyst, challenger, referee, reporter)`.

That graph **can never terminate**. The registry's DAG has
`referee: (challenger, optimizer)`, and the dependency gate refuses a phase
whose dependency is not in the workflow at all:

```text
referee BLOCKED -> phase 'referee' cannot start before 'optimizer' is done
workflow status: running
```

Creation succeeded, so the deadlock only appeared later, mid-run. The review
reported only that `news_read` named a phase (`news-analyst`) the registry does
not accept; in fact five of eight templates declared graphs that could not run.

This was masked because `atlas_workflow_runner` **ignored `template.phases`
entirely** and always created the standard portfolio graph. The declarations
were decorative. That also means the defect and the fix are coupled: passing the
declared graphs through without fixing them would have produced workflows that
never terminate and Atlas tasks that wait on them forever.

A research-only graph that stops before the optimizer is not expressible: the
referee checks targets, so it structurally depends on the optimizer producing
them. Templates that want a referee therefore declare the full chain, which is
what already ran.

## What was implemented

**Dispatch is not a conclusion.** `qlab/operator/atlas.py` gains a frozen
`Dispatched(workflow_id, detail)`. A runner returning it has started durable
work; the task stays `running` and is bound to the workflow. A runner returning
a plain dict is asserting the work finished inline, which is true only for
deterministic templates such as `desk_brief`.

**Only the workflow may resolve the task.** `AtlasSupervisor.reconcile_tasks()`
transitions a running, workflow-bound task from that workflow's terminal state:
`complete` completes it, and `failed`/`blocked`/`interrupted`/`abandoned` fail
it. A task whose bound workflow does not exist is failed rather than left
running forever. A running task with no binding is left alone — it belongs to a
deterministic template.

**Reconciliation runs at two points.** On every observe tick, and at owner
startup in `UISession.__init__` after `interrupt_running_workflows`. Startup
matters in both directions: a workflow that completed while nothing was watching
resolves its task, and a workflow still live at restart is interrupted, so its
task fails rather than hanging.

**Dispatch failure is loud.** `atlas_workflow_runner` raises when no workflow
could be started, instead of returning a handle with `workflow_id=None` — which
was the path by which a failed dispatch became a completed task.

**Phase graphs are validated for dependency closure.**
`qlab/state/registry.py` gains `validate_phase_graph(phases, deps)`, which
rejects a graph omitting any dependency, and `start_workflow` now routes through
it for non-panel kinds. Panels are validated against their own instance DAG,
where branch optimizers depend on their own analysts. This is a hardening in its
own right: creation used to accept a graph that would deadlock.

**`news-analyst` is a real phase type.** Registered with no dependencies and one
required artifact (`news_view`), mapped to the existing `news-analyst` agent. It
produces a qualitative view and no targets, so it reaches no gate and cannot
approach the approval path. `news_read` is now executable rather than a
declaration that would have raised.

**Declared graphs actually run.** `atlas_workflow_runner` passes
`template.phases` through. `phases` is an in-process keyword argument on
`UISession.start_workflow` and is never read from the HTTP body: letting a
network caller shape the graph would let it drop a gate phase.

## Governance position

Nothing here widens authority. Authority is still checked in `check_startable`
before any runner is called; the retry budget, the mode gate, and the
plan-creation boundary are untouched. The referee binding to `targets_hash` and
the persisted-human-approval requirement are unchanged — a test that tried to
drive a workflow to `complete` without a bound PASS verdict was correctly
refused by the registry, and the test was rewritten rather than the invariant
weakened.

## Verification

- 843 passed, 9 skipped, fully offline
- `python -m compileall -q qlab tests` clean; `git diff --check` clean
- the reported contradiction no longer reproduces: an autonomous start now
  leaves `Atlas task: running` alongside `Workflow: running`, and the workflow
  carries the template's declared phases
- one existing test (`test_atlas_task_start_respects_mode_authority`) asserted
  `completed is True` and encoded the defect; its authority assertions were kept
  and its completion assertion corrected

## Still open from the review's P1 list

- **`CoordinatorService` is not implemented.** The TUI still owns the Claude
  process, so closing the terminal still ends the reasoning worker. Atlas now
  reports that state honestly — a dispatched task stays `running` and its
  workflow is interrupted at restart — but the ownership boundary is unchanged.
- The durable job protocol (`POST /api/jobs` → `202 {job_id}`, status,
  interrupt, ordered `job.*` events) is not implemented; long owner actions are
  still requests.
