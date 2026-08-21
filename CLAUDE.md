# CLAUDE.md

Guidance for coding agents working in this repository.

qlab is a governed agentic quant research desk: research runs, referee-gated
decisions, and mandated paper trades share one auditable DuckDB registry. The
design boundary is strict — AI agents own judgment, algorithms own numbers,
deterministic code owns rigor (mandate, referee gate, execution idempotency).
Read README.md first; it is the authoritative overview.

## Commands

```bash
python -m pip install -e ".[operator,data,optimize,mcp,dev]"   # dev setup
python -m pytest                    # full offline suite (no network needed)
python -m pytest tests/test_ui.py  -q          # one module while iterating
qlab                                # the desk (owner + Atlas workstation; --restart, --offline)
qlab owner                          # same owner runtime, headless
qlab run-once --offline --dry-run   # one governed autopilot cycle, no orders
                                    # (run-once is proposal-only: it opens an
                                    # approval request, never books a fill)
qlab batch configs/specs/ablation_v1.yaml --offline   # staged ablation
qlab desk                           # one-card desk status (owner must be up)
qlab workforce run "GOAL"           # headless governed run, streamed live
qlab events                         # tail the owner's SSE audit bus
python -m qlab.agents.loader sync   # regenerate .claude/agents + .bob/personas

cd clients/atlas-tui && cargo test  # the Ratatui client (needs no owner)
cd clients/atlas-tui && cargo run   # run it against the owner on QLAB_UI_PORT
```

`clients/atlas-tui` is a second client for the same owner runtime, not a
replacement. Both read `/api/tui`, and neither ever holds a registry handle.
Its default build now ships armed, so the by-construction claim narrows to the
`--no-default-features` monitoring artifact: that binary contains no
`net::write`, no confirm modal and no `Posture::Operator`, so invariant 3 holds
there by absence. In the armed build,
`human_confirmed` comes from a modal bound to the last six of the plan's own
`targets_hash`, the referee PASS is pinned to that same hash, and the owner
re-validates every write and refuses without a persisted approval. What the
armed build may *do* is not a launch flag: the owner persists a posture, serves
it on `/api/tui`, and the client re-derives its scope from every snapshot.
`POST /api/desk/posture` is exactly as unauthenticated as every other owner
route — `/api/desk_mode`, the approval decisions and the execute path all
predate this branch and are equally open to anyone who can reach the port. The
posture is an operator's stated intent, not a security boundary, and must never
be read as one: what protects a fill is the hash-bound confirm, the referee pin,
and the owner's own re-validation.

Quantum research is an isolated offline lane: `pip install -e
".[offline-quantum]"` then `python -m pytest tests/test_quantum.py`. It is
excluded from `[all]`, the staged runtime, and the default ablation.

## Architecture in sixty seconds

- **One DuckDB writer, always.** The owner HTTP runtime (`qlab/ui/server.py`,
  started by `qlab` or `qlab owner`) is the only process that opens
  `.lab/registry.duckdb`. Every other surface — the Atlas workstation, the
  CLI verbs, and the `qlab-operator` MCP proxy (`qlab/mcp/tui_proxy.py`) —
  talks to it over HTTP only. The Textual client and the web client are
  retired; `qlab/tui/` retains only client plumbing (ApiClient, the Claude
  session, theme constants).
- The combined `qlab` MCP server (`qlab/mcp/server.py`) is for headless
  sessions and refuses to start while an owner runtime is alive (port guard).
- **Claude workforce**: the TUI can launch Claude as a coordinator whose only
  built-in tool is an allowlisted Agent dispatcher for five roles
  (moments-analyst → challenger → optimization-runner → referee → reporter).
  Phase state persists in the registry (`workflows`/`workflow_steps`) and is
  resumable. No Claude role has filesystem, shell, or execution tools.
- **The owner drives its own coordinator** (`qlab/operator/coordinator.py`).
  Registering a workflow is not running it — phases advance only while a
  coordinator walks them — so an Atlas dispatch spawns the same governed
  session a human would, pointed at the owner over HTTP. One at a time, and
  the driver's teardown is terminal so no tree outlives the runtime.
- **Atlas defaults to `research` mode with autonomy on.** Research is the
  highest mode that cannot create a paper plan; `check_startable` refuses every
  plan-creating template below `propose`. Widening what Atlas *researches* must
  never widen what it can *execute*.
- **Algorithm catalog** (`qlab/algorithms/catalog.py`): every method is
  `operational`, `research`, or `offline`. `algorithms.solve` enforces the
  stage in code — research/offline entries are visible but not agent-runnable.
  The paper policy is selected by `operational_policy` in `mandate.yaml`.

## Invariants — do not break these

1. **Never add a second DuckDB writer.** New functionality reaches the
   registry through the owner API (or the combined server when no owner runs).
2. **Tests never open `.lab/registry.duckdb`** — use `Registry(":memory:")`.
   Tests must pass fully offline; synthetic fixtures stand in for market data.
3. **Referee PASS is bound to the exact `targets_hash`** and plan execution
   requires a persisted checked plan plus explicit human confirmation
   (`human_confirmed=True` from the TUI). Never introduce a raw-order tool or
   an agent-reachable execution path.
4. **Fail loud.** No silent fallbacks for missing data, credentials, or
   unconditioned tensors; refuse with a clear error instead.
5. **`agents/*.md` is the single source of truth for roles.** After editing,
   run `python -m qlab.agents.loader sync`; never hand-edit `.claude/agents/`
   or `.bob/personas/`.
6. **Resolve files through `qlab/paths.py`** (`data_path`, `state_path`,
   `workspace_root`) — never `Path(__file__).parents[...]`; wheels install
   config under `share/qlab` and state must stay out of site-packages.
7. **Quantum stays offline.** Promotion into any staged surface requires
   evidence, a catalog stage change, tool-authority review, and tests.
8. **Restart the owner process after changing code it serves** — a long-lived
   owner keeps serving pre-change imports and will invalidate results.
9. **The owner is threaded.** `ThreadingHTTPServer` plus a heartbeat thread, so
   any shared mutable state on `UISession` needs a lock — a lazy build without
   one silently hands out N objects, which is how "one coordinator at a time"
   was lost once already.
10. **Anything reachable must have a caller.** Three real bugs shipped as code
    that existed but nothing invoked: `adjudicate()` (permanent reporter
    deadlock), fast-mode routing nothing passed `True` to, and `list_debates()`
    whose first bug proved it had never run. A new seam needs a call site and a
    test that exercises it, not just a definition.
11. **A negative result is a deliverable.** Record what did not work and why, in
    `planning-docs/`, including claims that did not survive reproduction. A
    measurement that looks too good is evidence of a bug until shown otherwise —
    and a robustness sweep with zero variance is a broken sweep.

## Conventions

- Commit messages: imperative, conventional prefix + scope
  (`fix(solvers): …`, `docs(readme): …`). No AI-attribution trailers.
- Match the existing comment density: comments state constraints the code
  cannot show, not narration.
- Property-style assertions in tests are deliberate (e.g. compiler agreement
  to 1e-10); never weaken an assertion to make a change pass — fix the change.
- `planning-docs/` is historical record; new status goes in a new dated file
  rather than rewriting old ones (they carry superseded banners).
