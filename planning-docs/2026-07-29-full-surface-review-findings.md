# Full-surface review — 2026-07-29

> **Historical, corrected 2026-09-01.** The findings below are stated in the
> present tense of the `77c5fe9` tree and are not current: G1's bare-flag
> execution path was closed by `0dccd91` and `ee55606` (execution now consumes
> a persisted, plan-bound approval), and the T-series describes the retired
> Textual client.

Three parallel deep reviews over the whole runtime (registry/operator core,
owner API/lifecycle, TUI/Claude session), run against the tree at `77c5fe9`
just before the desk-mode merge. Every finding below survived an explicit
attempt to disprove it; the registry/operator set was reproduced against
`Registry(":memory:")` before being recorded. Line numbers reference that
tree and will have drifted slightly after the desk-mode merge.

Fixed the same day, before this doc landed: the owner stderr drain, the
self-healing stream cursor, and malformed-frame surfacing (`harden/o3-o5`,
folded into the desk-mode branch).

## Governance and correctness — fix first

- **G1 (HIGH) `qlab/ui/server.py` `execute_checked_plan`** books a paper fill
  on a bare `body["human_confirmed"] is True` with no persisted approval and
  no book-revision binding — the MCP twin refuses this exact shape as
  self-attestation. One unauthenticated local POST executes and the audit
  trail records it as human-confirmed. Route it through the approval path or
  remove it.
- **G2 (HIGH) `qlab/state/registry.py` `transition_approval`** is an
  unguarded UPDATE: a consumed, rejected, or expired approval can be revived
  to `pending` via the challenge route and re-approved; ghost ids answer 200.
  The state machine needs edges, not writes.
- **G3 (HIGH) `qlab/state/registry.py` `reset_book`** deletes *every* book's
  equity marks, positions, and orders, not the current book's — an Alpaca
  book's realized history vanishes when the operator resets the simulated
  one. Now that the desk-mode merge makes two books routine, this is live.
- **G4 (MED) `equity_marks` PK `(ts, source)`** predates the `book` column: a
  second book's backfill at the same timestamps is silently discarded
  (`{"backfilled": 0}`), indistinguishable from "already up to date".
- **G5 (MED) `/api/run_once`** still coerces `execute` with a raw
  `bool(body.get("execute", True))` — `_flagbool` exists and is used for
  `offline` in the same handler. `{"execute": "false"}` reads as True.
- **G6 (MED) flat-judge verdict binding** (`registry.py` `_require_pass_verdict`):
  in a non-panel graph containing `judge`, the winner lookup misses and the
  "PASS must review this workflow's own decision" check is skipped. Latent
  (nothing dispatches that graph today) but the gate should not depend on
  reachability.

## Owner lifecycle

- **L1 (HIGH) CLI second-writer crashes**: `run-once`, `watch`, `daily-ops`,
  `autopilot`, and `batch` open the registry directly with no owner check —
  the documented `qlab batch` invocation dies on a raw DuckDB lock error
  while `qlab tui` runs. `desk_cli` shows the HTTP-first pattern to adopt.
- **L2 (HIGH) MCP port guard races the dispatch lock**: the guard probes
  `/api/system` (served under `_LOCK`) with a 1 s timeout and reads expiry as
  "no owner", then dies on the DuckDB lock. It should probe the lock-free
  `/readyz`, and honour non-default ports.
- **L3 (HIGH) heartbeat failures are invisible**: heartbeat/producer errors
  go to a stdout the launcher points at DEVNULL, and
  `AtlasHeartbeat.status()["errors"]` is rendered nowhere — a permanently
  failing supervisor renders as a healthy quiet desk.
- **L4 (MED) request framing**: `do_GET` never consumes a request body and
  `do_POST` accepts a negative `Content-Length`, leaving bytes in `rfile`
  that HTTP/1.1 keep-alive parses as the next request.
- **L5 (MED) `/api/atlas/read?refresh=1`** writes `_desk_news` outside the
  lock, racing the heartbeat's fetch-compose; the drawer can show a read
  older than the one just requested.
- **L6 (MED) malformed `.mcp.json`** is reported as "not configured" — a
  parse error surfaced as an absence.

## Atlas supervisor

- **A1 (HIGH) `blocked` tasks have no exit**: an authority refusal writes a
  status nothing ever reads back to `queued`; the day's dedupe key is burned,
  so the trigger is gone until tomorrow.
- **A2 (HIGH) `drift_breach` dedupe churns**: the trigger payload carries the
  live drift float, so each heartbeat mints a new task until the daily budget
  is exhausted — then real triggers are refused.
- **A3 (MED) `COORDINATING` is overwritten** by the next observe tick while
  the workflow still runs; `INVESTIGATING`/`SYNTHESIZING`/`AWAITING_APPROVAL`
  are declared, never assigned.
- **A4 (MED) `atlas_facts` hardcodes** `regime.flip=False` and
  `pending_approvals=0` — `regime_review` is autonomously unreachable and the
  desk brief reports a confident wrong number.

## TUI

- **T1 (HIGH) `: workforce stop now` launches a run**: any malformed
  workforce/governed subcommand falls through to `action_workforce_new` with
  the subcommand text as the goal.
- **T2 (HIGH) `resolve` vs escaped brackets**: `_TAG` does not honour the
  `\[` escape `rich.markup.escape` emits, so chat text like `buy [$SPY] now`
  raises out of the input handler and kills the TUI. The 07-27 fix covered
  unbracketed text only.
- **T3 (HIGH) Claude reader thread dies unclean**: `_read`'s `on_event` is
  unguarded; an app-side render error kills the reader before cleanup — temp
  dir leaks, the process tree blocks on a full pipe until the 420 s watchdog.
- **T4 (HIGH) second workforce turn prints nothing**: after a completed run,
  `_results_printed` / `_active_workflow_id` stay pinned to the finished run
  and the next turn's events are all suppressed.
- **T5 (HIGH) theme switch leaves the console unreadable**: `#workforce-console`
  history was resolved to hex at write time; after `: theme qlab-light` the
  run narrative sits at ~1:1 contrast on the light canvas.
- **T6 (MED) `_format_targets`** trusts agent-authored artifact values
  (`-float(kv[1])`) that the registry never type-checks; a string weight on
  the failed-run path raises into T3.
- **T7 (MED) refresh failure attribution**: renderer exceptions inside
  `_apply_snapshot` count toward `_refresh_failures`, so a repaint bug reads
  as `OWNER DOWN` against a healthy owner.

## Checked clean (both reviewers, so future reviews can skip re-deriving)

Registry SQL parameterisation; `targets_hash` canonicalisation and its
single-definition use; panel instance-DAG ownership and re-validation;
`update_workflow_phase` replay/reopen guards; `execute_plan`'s registry-truth
checks at the `/api/plans/<id>/execute` boundary; the proxy's propose-only
surface; SSE merged-cursor ordering under the bounded lock; worker-thread
marshalling in the TUI; the workforce tool allowlist and quarantine; process
tree teardown and watchdogs; event dedup capping.
