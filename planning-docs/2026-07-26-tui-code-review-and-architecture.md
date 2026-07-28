# qlab operator TUI: code review and target architecture

Date: 2026-07-26  
Status: proposed architecture; first hardening slice implemented in the same
review  
Scope: owner runtime, Atlas/workforce lifecycle, Textual TUI, Claude launcher,
event transport, and terminal product direction

## Executive decision

Keep the TUI in Python and Textual. Do not rewrite it in Ink, Ratatui, Bubble
Tea, or Qt now.

The current problem is not that Textual is incapable of a robust terminal
application. The problem is that `QlabTui` centralizes state, rendering,
networking, process ownership, and command dispatch in one class. A framework
rewrite would translate that coupling into another language before fixing it.

The target is:

1. the owner runtime owns every long-lived job, including the model
   coordinator;
2. the TUI is a reconnectable client, never the lifetime owner of reasoning;
3. immutable state projections enter one reducer/store;
4. feature widgets receive state and emit typed intents;
5. all blocking work is cancellable and supervised;
6. the command surface is contextual, discoverable, and keyboard-first;
7. governance remains server-side and unchanged.

The immediate migration should stay incremental. The operator remains usable
and the offline suite stays green after every slice.

## Correcting the framework premise

The premise “Claude Code is Node + TypeScript + Ink, so qlab should use Ink”
is not current enough to guide an architecture decision.

- The installed Claude Code is `2.1.220` and
  `/Users/azainmac/.local/bin/claude` resolves to a native arm64 Mach-O binary.
  Anthropic's public changelog says that since `2.1.113` the CLI spawns a
  native per-platform binary instead of bundled JavaScript. Anthropic publicly
  documents its features and extension points, but does not publish enough of
  the product's current TUI implementation to justify an Ink-specific claim.
- The installed Codex `0.144.6` command is a small Node launcher, but it selects
  a platform-native executable. The maintained Codex implementation is Rust.
  Its fullscreen TUI is Ratatui, and its app-server is a separate JSON-RPC
  interface used by richer clients.
- Ink itself is a sound React terminal renderer and uses Yoga-style flexbox
  layout. That proves Ink is viable; it does not make a Node rewrite the right
  boundary for a Python quant system.
- Textual already provides reactive attributes, watch methods, component
  messages, workers, CSS layout, partial repainting, and a headless Pilot test
  driver. Those are the capabilities qlab needs.

Primary references:

- [Claude Code changelog](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md)
- [Claude Code extension model](https://code.claude.com/docs/en/features-overview)
- [Codex Rust workspace and Ratatui TUI](https://github.com/openai/codex/blob/main/codex-rs/README.md)
- [Codex app-server protocol](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md)
- [Ink](https://github.com/vadimdemedes/ink)
- [Textual reactivity](https://textual.textualize.io/guide/reactivity/)
- [Textual workers](https://textual.textualize.io/guide/workers/)
- [Textual testing](https://textual.textualize.io/guide/testing/)

## What was reviewed

The review followed the live path rather than judging screenshots:

- owner acquisition and the one-DuckDB-writer boundary;
- `/api/tui`, `/api/stream`, Atlas heartbeat, and API locking;
- workflow creation, dependency enforcement, interruption, resume, and stale
  workflow recovery;
- Claude executable resolution, isolated session agents, stdout/stderr
  streaming, watchdogs, and process-tree termination;
- Textual state ownership, rendering, background threads, unmount behavior,
  command routing, and tests;
- Atlas templates, task lifecycle, autonomous dispatch, and qualitative news;
- current README claims and the actual registry dependency graph.

After the first hardening slice, `qlab/tui/app.py` is still approximately 4.4k
lines. `QlabTui` has more than 100 methods, roughly 50 mutable instance fields,
57 `query_one` calls, and five raw thread construction sites. The tests are
substantial, but most assert end-state content; lifecycle interleavings and
component boundaries are less well covered.

## Code-review findings

### P1 — Atlas records completion without running the workforce

Confirmed and reproduced.

`UISession.atlas_workflow_runner` creates a durable workflow row and returns its
identifier. It does not start Claude, Codex, or any other coordinator.
`AtlasSupervisor.start_task` treats any normal runner return as a completed
task. The resulting persisted state can therefore be:

```text
Atlas task: completed
Workflow:   running
Analyst:    queued
Challenger: queued
Optimizer:  queued
Referee:    queued
Reporter:   queued
```

This is the largest functional gap in the advertised autonomous mode. The
autonomy toggle automates workflow-row creation, not research completion.

There is a related contract mismatch: `WorkflowTemplate.phases` is not passed
to `start_workflow`. Atlas always creates the standard portfolio workflow.
Several declared reduced phase sets are not currently executable under the
registry's artifact/dependency contract, and `news_read` declares a
`news-analyst` phase the registry does not accept.

Required correction:

1. introduce an owner-managed `CoordinatorService`;
2. have Atlas dispatch a durable job and leave its task `running`;
3. transition the Atlas task only from workflow terminal state;
4. reconcile task/workflow state on owner startup;
5. either make template phase graphs executable contracts or remove the
   unsupported phase declarations;
6. never report `completed` for “a row was created.”

This was not papered over in the TUI. It requires the owner-side coordinator
slice described below.

### P1 — a news-scoped workforce could crash before Claude launched

Fixed in this review.

`build_workforce_agents()` correctly adds the quarantined `news-extractor` for
a news/view goal. `write_session_agents()` rejected that same allowlisted role,
raising `ValueError` before `subprocess.Popen`. The exception was outside the
old `OSError`-only failure path, so it escaped into Textual.

The session materializer now admits the quarantined role, its one-tool
authority is asserted in tests, and launcher/configuration failures become a
visible `last_error` instead of a TUI traceback.

### P1 — live news fetches held the global owner dispatch lock

Fixed in this review.

The Atlas heartbeat called `refresh_desk_read()` while holding the same lock
used by every HTTP request. The RSS provider has six configured feeds and a
five-second timeout per request. Network slowness could therefore freeze the
entire operator surface.

The heartbeat now fetches external news before taking the owner lock. It
grounds, combines, and persists the fetched window under the lock, preserving
the single-writer rule without making network latency part of the critical
section. The explicit Refresh Read HTTP path uses the same split.

### P1 — “Refresh Read” did not refresh the read

Fixed in this review.

The Atlas button called `/api/atlas/observe`, which consumes the cached
qualitative read. It now calls `/api/atlas/read?refresh=true`, and the HTTP
handler performs external fetch outside the registry lock.

### P1 — TUI startup mistook an open socket for a ready owner

Fixed in this review and found by the real-terminal smoke test.

The owner intentionally binds its port before opening DuckDB so another
process cannot race it into becoming a second writer. The TUI launcher treated
that early port bind as readiness and immediately called `/api/system`. On a
cold launch, the server had not started handling requests yet, so the client
reported “port is open but is not a compatible qlab runtime” and terminated
the healthy owner it had just spawned.

The owner now exposes a lightweight `/readyz` route. A spawned TUI waits for
that application-level acknowledgement (or a bounded startup failure) before
probing the full system contract.

### P2 — the TUI owns the coordinator process

Open.

The local `ClaudeSession` is created by `QlabTui`. Closing or crashing the
terminal therefore ends the reasoning worker. That conflicts with the intended
“Atlas is always there” product and makes a UI lifecycle event responsible for
a research lifecycle event.

The process controls themselves are comparatively careful: the launcher is
resolved exactly, Windows `.cmd` files use `ComSpec`, output is decoded as
UTF-8, stderr is drained, a process group is created, stop kills descendants,
and wall/silence watchdogs are bounded. The ownership boundary is still wrong.

Move this code behind the owner API. The TUI should issue `job.start`,
`job.interrupt`, and `job.resume`, then render durable events.

### P2 — blocking controls still run on the Textual message thread

Open.

`_control_workflow()` performs a synchronous HTTP request with up to a
five-second deadline. `ClaudeSession.stop()` can synchronously wait while
terminating a process group. Both can run from button/key handlers.

Five seconds is bounded, but it is still a frozen terminal. These operations
belong in supervised workers with immediate optimistic UI state (`stopping`,
not `stopped`) and a terminal acknowledgement from the owner.

### P2 — background work was only loosely owned

Partly fixed in this review.

Snapshot, bootstrap, reference, SSE, and action work used anonymous daemon
threads. The SSE generator had no cancellation input and an infinite read
timeout. Worker results could call back into an unmounted widget tree.

The first hardening slice adds:

- an explicit SSE cancellation event;
- heartbeat/read timeout bounded shutdown;
- one guarded worker-to-UI handoff;
- named threads;
- no late callback after unmount.

The remaining migration is from raw threads to Textual workers with named
groups, exclusivity, cancellation, and observable failure state.

### P2 — one app class still owns almost everything

Partly fixed in this review.

`QlabTui` still owns:

- snapshot state;
- command parsing;
- API/process lifecycle;
- nine feature views;
- chart formatting;
- Atlas and workforce presentation;
- navigation, focus, responsive layout, and modals.

This is the principal maintainability problem. It creates temporal coupling:
every mutation site must remember which render methods to call.

The first slice converts the navigation menu, agent rail, and flow nodes into
reactive components. Later snapshots now repaint only the visible canvas after
the initial full paint. This is a migration seam, not the completed
decomposition.

### P2 — long owner actions are requests, not jobs

Open.

The generic action client permits a 30-minute response. There is no job ID,
progress cursor, cancel token, or restart reconciliation at that boundary.
When a computation stalls, the UI can only wait for the HTTP call.

Use an owner job protocol:

```text
POST /api/jobs                  -> 202 {job_id, state:"queued"}
GET  /api/jobs/{id}             -> durable state/progress/result
POST /api/jobs/{id}/interrupt   -> accepted transition
SSE  job.*                      -> ordered deltas
```

The request that creates a job should be short. Research may be long; control
requests may not.

### P2 — snapshot construction and delivery are too broad

Open.

`/api/tui` constructs a large projection containing portfolio, market,
stress, Atlas, news read, tasks, approvals, quotes, agents, decisions, runs,
plans, orders, events, algorithms, policy, leaderboard, and performance. The
owner dispatch lock covers construction.

The TUI previously repainted every view every two seconds even if hidden. The
client-side repaint waste is fixed. The owner should next cache versioned
projection slices and return one envelope of changed slices:

```json
{
  "sequence": 1842,
  "versions": {"book": 31, "market": 884, "workforce": 76},
  "changed": {"market": {}, "workforce": {}}
}
```

Do not split this into many uncoordinated polling calls. Preserve a consistent
projection, but stop recomputing unchanged slices.

### P2 — stream failures are invisible

Open.

The SSE loop catches every exception and retries quietly. Quiet retries are
appropriate for one transient disconnect; repeated failure needs a connection
state, backoff, last error, and retry count visible in the status line.

### P3 — state and documentation drifted from the real DAG

Fixed in this review.

The registry correctly enforces:

```text
analyst -> challenger -> optimizer -> referee -> reporter
```

The optimizer waits for the bounded debate because an amended analyst decision
must replace the original optimizer inputs. TUI prose and README text still
described challenger and optimizer as parallel, and the README assigned the
workforce to key 3 instead of key 4. Those statements now match code.

Panel branches remain intentionally parallel; that is a different workflow
graph.

### P3 — event deduplication grew forever

Fixed in this review.

The in-memory event ID set was unbounded for a long-running terminal. It is now
a fixed 2,048-ID window with exact recent deduplication.

### P3 — deterministic Atlas actions polluted workforce state

Fixed in this review.

Atlas actions passed `active_agent=None` into a string-only path. That created
an internal `None` agent entry while painting the five real roles as queued or
done. Agent state is now left untouched for deterministic Atlas actions.

## What to copy from other products

### Claude Code

Copy the extension and lifecycle concepts, not an assumed rendering stack:

- built-in capabilities separated from MCP extensions;
- subagents with isolated context and explicit tool grants;
- skills for reusable instructions;
- hooks for deterministic lifecycle enforcement;
- persistent sessions, interrupt/resume, status line, and visible permissions;
- progressive disclosure of tool activity.

For qlab, every extension remains domain-scoped. There is no filesystem, shell,
raw-order, or paper-execution tool in the workforce.

### Codex CLI

The most valuable pattern is structural:

```text
core runtime <-> app-server protocol <-> TUI / IDE / automation clients
```

The app-server represents work as threads, turns, items, and streaming
lifecycle notifications. qlab should use the analogous domain nouns:

```text
workflow -> phase -> artifact/event
job      -> attempt -> progress/result
```

The TUI should not infer terminal state from prose. It should render state
machines emitted by the owner.

### Bloomberg Terminal

Copy:

- command-first navigation from anywhere;
- linked context: selecting a symbol, workflow, decision, or run updates the
  relevant panes;
- persistent workspaces and watch lists;
- alerts tied to provenance and time;
- dense information only after the user asks for it;
- one stable vocabulary for functions and objects.

Do not copy:

- cryptic command codes as the only discoverability mechanism;
- visual density without hierarchy;
- amber/black as decoration;
- direct execution adjacency to research suggestions.

Bloomberg Launchpad describes the useful product idea: a customizable
workspace joining monitors, alerts, charting, news, and portfolio context.
qlab should implement a governed, smaller version rather than imitate the
screen density.

### FinceptTerminal

FinceptTerminal is a C++20/Qt desktop application, not a terminal UI. A Qt
rewrite would be much larger than an Ink rewrite. Its useful patterns are:

- a thin `WindowFrame` shell;
- a separate screen router;
- lazy screen construction;
- a typed `ActionRegistry` and command parser;
- a `DataHub` for coalesced fan-out and topic policies;
- feature-owned modules rather than one window class.

The qlab analog is a thin Textual app, a typed command registry, a versioned
projection store, and independently testable feature widgets.

### Lazygit

Copy contextual key help, panel focus, reversible navigation, and the ability
to expose advanced operations without keeping them permanently on screen.
Destructive trading actions remain confirmation-gated and are not treated like
ordinary navigation commands.

## Framework decision matrix

| Option | Strength | Cost/risk for qlab | Decision |
|---|---|---|---|
| Textual/Python | Existing tests, CSS, reactive widgets, workers, same packaging and domain language | Requires disciplined decomposition; Python is not the bottleneck yet | Choose |
| Ink/TypeScript | React component model, Yoga layout, strong npm ecosystem | Full UI/test rewrite, Node or per-platform bundle, duplicated Python contracts, no governance benefit | Reject now |
| Ratatui/Rust | Native binary, explicit event loop, excellent performance and terminal control | Largest language boundary, generated protocol/types, cross-platform packaging, full test rewrite | Revisit only after profiling |
| Bubble Tea/Go | Clear Model/Update/View architecture, simple binaries | Same rewrite and protocol duplication without a demonstrated performance need | Reject now |
| Qt/C++ | Rich desktop docking, charts, multiple windows | Becomes a desktop product; very large rewrite and deployment surface | Separate future client only |
| prompt_toolkit | Excellent REPL, completion, history | Weaker fit for a multi-pane live workstation | Use ideas, not as the shell |
| raw curses/Rich | Maximum control / good formatting | Rebuilds layout, focus, workers, accessibility, and testing by hand | Reject |

JavaScript remains appropriate for the existing web client. It is not needed
to make the terminal client declarative.

## Target system architecture

```text
 Alpaca / RSS / research algorithms
                 |
                 v
 +--------------------------------------------------------------+
 | OwnerRuntime                                                  |
 |                                                              |
 |  Data services -> ProjectionService -> versioned desk state  |
 |                         |                                    |
 |  AtlasSupervisor -> JobSupervisor -> CoordinatorService      |
 |                         |              |                     |
 |                         +-> WorkflowService / registry        |
 |                                      (sole DuckDB writer)     |
 |                                                              |
 |  Command API + job API + ordered event stream                |
 +------------------------------+-------------------------------+
                                |
                     HTTP/SSE typed protocol
                                |
 +------------------------------v-------------------------------+
 | Textual operator client                                      |
 |                                                              |
 |  OwnerClient -> TuiController -> reducer -> immutable Store  |
 |                                     |                        |
 |             +-----------------------+-------------------+    |
 |             |                       |                   |    |
 |          shell widgets          feature views       overlays  |
 |             ^                       ^                   ^    |
 |             +----------- typed intents/messages --------+    |
 +--------------------------------------------------------------+
```

The rule is “state down, intents up.” A widget may render its own state and emit
a message. It may not reach into another feature and mutate its child widgets.

## Owner-side components

### `CoordinatorService`

Responsibilities:

- own Claude/Codex process lifetime independently of the TUI;
- accept a workflow ID and bounded goal;
- materialize the same least-privilege role definitions;
- stream normalized coordinator events;
- enforce one active coordinator per configured lane;
- interrupt the full process tree;
- apply wall, silence, and phase-progress leases;
- reconcile terminal process state with durable workflow state;
- survive client disconnects;
- fail loud when no model runtime is available.

It must not gain a raw trade or confirmation endpoint.

### `JobSupervisor`

Every long operation becomes a durable or recoverable job:

- queued, starting, running, stopping;
- completed, failed, interrupted, abandoned;
- attempt number and retry ceiling;
- progress heartbeat and last-progress timestamp;
- owning workflow/task IDs;
- process/thread cancellation handle;
- bounded output/error tail.

A request returning a job ID is not a completed task.

### `ProjectionService`

Build immutable, versioned slices:

- system;
- market;
- book;
- Atlas;
- workforce;
- research;
- audit;
- reference/settings.

It coalesces updates and publishes changed slice versions. It never writes
directly from a client thread outside the owner dispatch boundary.

### `CommandRegistry`

Define commands once with:

- canonical ID and aliases;
- label and description;
- context predicate;
- authority/mode;
- argument schema;
- confirmation policy;
- handler intent;
- shortcut.

The command row, command palette, help view, mouse actions, and web client can
all consume the same metadata.

## Textual client components

Target package shape:

```text
qlab/tui/
  app.py                    # composition and top-level message routing only
  state.py                  # frozen DeskState and slice types
  reducer.py                # pure event -> state transitions
  controller.py             # owner client, subscriptions, job commands
  messages.py               # typed UI intents
  commands/
    catalog.py
    parser.py
    palette.py
  widgets/
    nav.py
    status.py
    atlas_rail.py
    agent_rail.py
    evidence.py
    command_bar.py
  views/
    atlas.py
    dashboard.py
    market.py
    workforce.py
    research.py
    book.py
    audit.py
    reference.py
    settings.py
```

`app.py` should eventually be under roughly 500 lines. This is not a vanity
metric; it should contain composition, cross-feature navigation, and no
financial formatting or HTTP details.

## Product layout

Keep the calm, no-top-header workstation:

```text
┌──────────────┬──────────────────────────────────────┬──────────────────┐
│ qlab / views │ context canvas                       │ ATLAS            │
│              │                                      │ mode + state     │
│ Atlas        │ selected object title + provenance   │ next attention   │
│ Market       │                                      │                  │
│ Workforce    │ primary analysis / chart / workflow  │ WORKFORCE        │
│ Research     │                                      │ phase + elapsed  │
│ Book         │                                      │ stop / resume    │
│ Audit        │ evidence drawer on demand            │                  │
│              │                                      │ APPROVALS        │
│ Universe     │                                      │ explicit only    │
├──────────────┴──────────────────────────────────────┴──────────────────┤
│ : command / ask Atlas                                      DATA · PAPER │
└─────────────────────────────────────────────────────────────────────────┘
```

### Interaction model

- `:` opens commands; plain text in Atlas/Workforce is a question or goal.
- `Ctrl-P` opens a fuzzy palette containing only commands valid in the current
  context.
- Selecting a symbol sets shared `SymbolContext`.
- Selecting a workflow sets shared `WorkflowContext`.
- `evidence` opens the exact inputs, hashes, decisions, verdict, and outcome.
- `why` explains the selected status from persisted evidence.
- stop is always reachable and changes immediately to `stopping`.
- approvals live in a visually separate region and require an explicit modal.

### Fast mode

Fast mode is a workflow policy, not a faster animation:

- one analyst pass;
- no optional QA roles unless a data gate requires them;
- one challenger counter-case, no second rebuttal;
- operational solver only;
- compact reporter;
- same registry artifacts, mandate, referee binding, and human confirmation.

Label it `FAST RESEARCH`, never “fast trade.” It may reduce model depth and
rounds; it may not bypass deterministic gates.

## Migration plan

### Phase 0 — truth and lifecycle hardening

Status: partly completed in this review.

- fix news-scoped role materialization;
- make launcher/config errors visible;
- move heartbeat news I/O outside owner lock;
- make explicit read refresh real;
- wait for owner application readiness rather than raw port readiness;
- cancel SSE on unmount;
- bound event dedupe;
- stop painting Atlas actions as workforce work;
- align prose with the registry DAG.

Exit gate: focused tests plus full offline suite.

### Phase 1 — immutable state and reducer

1. Define frozen state slices and an envelope sequence.
2. Normalize snapshot and SSE payloads into typed events.
3. Implement a pure reducer.
4. Make the app hold one `DeskStore`.
5. Preserve current view output before moving features.

Tests:

- out-of-order/replayed event handling;
- snapshot then delta equivalence;
- malformed payload refusal;
- bounded event history;
- property test that terminal workflow states never return to working.

### Phase 2 — thin shell and typed commands

1. Extract nav, status, Atlas rail, agent rail, and command bar.
2. Introduce command metadata and contextual palette.
3. Replace direct cross-widget updates with messages.
4. Keep the existing view renderers temporarily behind adapters.

Exit gate:

- no shell child is mutated from a feature view;
- all shortcuts and buttons resolve through the command registry;
- navigation, resize, mouse, and command tests pass.

### Phase 3 — feature view extraction

Move one view at a time:

1. Workforce;
2. Atlas;
3. Book and Audit;
4. Market and Dashboard;
5. Research, Reference, Settings.

Workforce comes first because it has the most lifecycle state and the highest
cost of ambiguity.

Each view owns:

- its reactive input slice;
- pure formatting/view-model functions;
- local selection/focus state;
- typed intents;
- view-specific tests and CSS.

### Phase 4 — supervised workers and job API

1. Replace raw TUI threads with grouped Textual workers.
2. Add owner job start/status/interrupt endpoints.
3. Make all requests short except SSE.
4. Add retry/backoff and connection health.
5. Test owner restart and disconnect during every job state.

Exit gate:

- no raw `threading.Thread` in the TUI;
- no blocking HTTP/process call on the Textual message thread;
- stop acknowledgement visible within one second on a healthy owner;
- repeated stream failure is visible.

### Phase 5 — owner-managed coordinator

1. Move `ClaudeSession` into `qlab/operator/coordinator.py`.
2. Normalize Claude and future Codex backends behind `CoordinatorBackend`.
3. Bind Atlas tasks to coordinator jobs and workflows.
4. Reconcile on startup.
5. Keep session agents isolated and least-privilege.
6. Make the TUI attach/detach without stopping the job.

Exit gate:

- close and reopen TUI mid-analyst; the workflow continues;
- stop from a new TUI kills the entire coordinator tree;
- model exit with incomplete phase interrupts workflow and fails the Atlas
  task;
- Atlas task completes only when its workflow is terminal complete;
- templates either execute their declared graph or are rejected before a task
  starts.

### Phase 6 — terminal-product polish

- linked symbol/workflow/decision context;
- evidence drawer;
- contextual key help;
- command history and completions;
- fast research policy;
- saved layout preferences;
- visual regression fixtures at narrow/compact/wide sizes;
- real PTY smoke tests on Windows, macOS, and Linux.

## Test strategy

### Pure tests

- reducer transitions;
- command availability and authority;
- view-model formatting;
- task/workflow reconciliation;
- job retry and cancellation state machines.

### Textual Pilot tests

- navigation and focus;
- palette filtering;
- view updates from state changes;
- stop/resume/abandon;
- stale/disconnected state;
- responsive layouts.

### Protocol tests

- snapshot + SSE ordering;
- reconnect cursors;
- duplicate and out-of-order events;
- cancellation;
- owner overload/backpressure;
- slow RSS with concurrent `/api/tui`;
- coordinator output bursts.

### Real terminal tests

Pilot is necessary but insufficient. Run PTY smoke tests for:

- resize during streaming output;
- Unicode and Windows code pages;
- PowerShell launch and `.cmd` resolution;
- Ctrl-C/Ctrl-Q/escape behavior;
- terminal close during a running worker;
- mouse support and terminals without it.

### Governance regression tests

Every phase must retain:

- one DuckDB writer;
- no raw-order tool;
- exact `targets_hash` referee binding;
- explicit human paper confirmation;
- no synthetic fallback in a live data mode;
- research/offline algorithm stage enforcement;
- quantum isolation.

## Success criteria

- Atlas task state and workflow state cannot contradict each other.
- A TUI disconnect does not end owner work.
- Atlas heartbeat/read-refresh provider fetches do not hold the dispatch lock.
- No raw thread or blocking request remains in `qlab/tui`.
- `QlabTui` becomes a thin compositor, not a feature implementation.
- Hidden views do not repaint on ordinary deltas.
- Every status has a persisted or protocol source; no status is inferred from
  model prose.
- Connection and job failures are visible and recoverable.
- The full offline suite, visual fixtures, and real PTY smoke tests pass.

## Sizing

For one engineer:

- UI state/shell/view decomposition: roughly 8–12 engineering days;
- job protocol and owner coordinator: roughly 8–15 days;
- product polish and cross-platform PTY hardening: roughly 5–8 days.

That is approximately four to seven weeks for the complete target, with usable
checkpoints throughout. An Ink rewrite would still need the owner/job work and
would add a full UI, packaging, and test rewrite. A Ratatui rewrite should be
considered only if profiling after this migration proves Textual itself is the
limiting layer.
