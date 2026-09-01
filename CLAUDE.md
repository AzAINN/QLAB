# CLAUDE.md

Guidance for coding agents working in this repository.

qlab is a governed agentic quant research desk: research runs, referee-gated
decisions, and mandated paper trades share one auditable DuckDB registry. The
design boundary is strict — AI agents own judgment, algorithms own numbers,
deterministic code owns rigor (mandate, referee gate, execution idempotency).
Read README.md first; it is the authoritative overview.

## Commands

```bash
python -m pip install -e ".[operator,data,optimize,mcp,trader,dev]"  # what CI installs
python -m pytest                    # full offline suite (no network needed)
python -m pytest tests/test_ui.py  -q          # one module while iterating
qlab                                # the desk (owner + Atlas workstation; --offline; --restart warns, asks a tier, archives)
qlab owner                          # same owner runtime, headless
                                    # (either one's 30s beat books a proposal a
                                    # standing grant already covers, with no
                                    # click — invariant 3. No CLI verb grants:
                                    # POST /api/desk/authority does, and
                                    # Settings > AUTHORITY revokes on `R`)
qlab run-once --offline --dry-run   # one governed autopilot cycle, no orders
                                    # (proposal-only by default: it opens an
                                    # approval request. run-once and watch
                                    # book a fill only when the operator has
                                    # exported QLAB_AUTOPILOT_EXECUTE=1;
                                    # daily-ops and autopilot never trade)
qlab batch configs/specs/ablation_v1.yaml --offline   # staged ablation
qlab desk                           # one-card desk status (owner must be up)
qlab workforce run "GOAL"           # headless governed run, streamed live
qlab events                         # tail the owner's SSE audit bus
qlab cli                            # interactive Claude as Atlas (proxy tools + read-only web)
qlab build "add a heatmap visual"   # interactive Claude Code on this checkout
qlab news-setup                     # wizard for the news providers
python -m qlab.agents.loader sync   # regenerate .claude/agents + .bob/personas

cd clients/atlas-tui && cargo test  # the Ratatui client (needs no owner)
cd clients/atlas-tui && cargo run   # run it against the owner on QLAB_UI_PORT
```

`clients/atlas-tui` is the owner runtime's only client — the Textual and web
clients are retired. It reads `/api/tui` and never holds a registry handle.
Its default build now ships armed, so the by-construction claim narrows to the
`--no-default-features` monitoring artifact: that binary contains no
`net::write`, no confirm modal, no `Posture::Operator`, and — since the ATLAS
terminal pane landed — no pty, no spawn and no forwarded keystroke, so
invariant 3 holds there by absence. Verify it with `nm`, minding three traps:
build each leg to its own snapshot first (both write the same
`target/debug/atlas` and silently overwrite each other), give `nm` an ABSOLUTE
path (a relative one under a redirected `CARGO_TARGET_DIR` reports a false 0),
and count the mangled symbol path `5atlas3pty` — the loose `atlas.*pty` matches
`is_empty` and is nonzero on both legs. In the armed build,
`human_confirmed` comes from one click on a box that displays the last six of
the plan's own `targets_hash` and posts it, the referee PASS is pinned to that
same hash, and the owner re-validates every write and refuses without a
persisted approval. What the
armed build may *do* is not a launch flag: the owner persists a posture, serves
it on `/api/tui`, and the client re-derives its scope from every snapshot.
`POST /api/desk/posture` is exactly as unauthenticated as every other owner
route — `/api/desk_mode`, the approval decisions and the execute path all
predate this branch and are equally open to anyone who can reach the port. The
posture is an operator's stated intent, not a security boundary, and must never
be read as one: what protects a fill is the hash-bound confirm, the referee pin,
and the owner's own re-validation. The client is no longer the only door — under
a standing grant (invariant 3) the owner's own beat books with no click, and
`POST /api/desk/authority` is as unauthenticated as the rest. There, the
protection is the grant's own ceilings, the same referee pin, and the same
re-validation; the client's confirm box is not what stops that path.

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
  built-in tool is an allowlisted Agent dispatcher for eight roles — the six
  workflow phases (moments-analyst, contender-scout, challenger,
  optimization-runner, referee, reporter) and the advisory data-qa and
  signal-qa — plus the quarantined news-extractor when the goal names news or
  views (`qlab/tui/claude.py`). The rebalance graph is analyst → challenger →
  optimizer → referee → reporter; `portfolio_watch` is analyst → scout →
  reporter, and the scout is the only workforce role granted WebSearch and
  WebFetch. Phase state persists in the registry (`workflows`/`workflow_steps`)
  and is resumable. No Claude role has filesystem, shell, or execution tools.
- **The owner drives its own coordinator** (`qlab/operator/coordinator.py`).
  Registering a workflow is not running it — phases advance only while a
  coordinator walks them — so an Atlas dispatch spawns the same governed
  session a human would, pointed at the owner over HTTP. One at a time, and
  the driver's teardown is terminal so no tree outlives the runtime.
- **Atlas defaults to `research` mode with autonomy on.** Research is the
  highest mode that cannot create a paper plan; `check_startable` refuses every
  plan-creating template below `propose`. Widening what Atlas *researches* must
  never widen what it can *execute*.
- **The beat can book.** Under a standing grant the owner's heartbeat books a
  referee-passed proposal with no click (`book_under_grant` in
  `qlab/ui/server.py`, called from the LOCK phase of `build_owner_tick` in
  `qlab/operator/heartbeat.py`; the model is `qlab/governance/authority.py`).
  It is the operator's authority, never Atlas's — Atlas cannot see, create or
  consume a grant, and `qlab/operator/` reaching into `qlab/ui/` for this is
  the first such import (function-scoped and lazy, so no cycle).
- **The real Claude CLI opens on the desk**, two ways, and the argv for both is
  built in one place (`qlab/tui/claude.py`, tested in `tests/test_claude_cli.py`).
  `qlab cli` is interactive Claude wearing `agents/atlas.md`, its tool universe
  narrowed to `WebSearch,WebFetch` plus the owner-backed proxy — no shell, no
  filesystem. `qlab build "<request>"` is Claude Code on this checkout with its
  own default tools and its own interactive permission prompts, because the
  operator is in the loop. The workstation spawns those same two verbs by two
  different paths. `/build` takes `clients/atlas-tui/src/handoff.rs`: leave the
  alternate screen, disable mouse capture and raw mode, run the child on the
  inherited terminal, restore, repaint. `/cli` takes
  `clients/atlas-tui/src/pty.rs` and `src/pane.rs`: the child runs on a
  pseudoterminal inside the ATLAS column, the screen is never handed over, and
  the desk sidebar stays beside it. On both paths the child is `qlab cli` or
  `qlab build`, never `claude` directly — the tool universe is decided in
  `qlab/tui/claude.py` and nowhere else. A build that touched `qlab/` or
  `clients/atlas-tui/` is *offered* `qlab --restart runtime` and never given it.
- **Algorithm catalog** (`qlab/algorithms/catalog.py`): every method is
  `operational`, `research`, or `offline`. `algorithms.solve` enforces the
  stage in code — research/offline entries are visible but not agent-runnable.
  The paper policy is selected by `operational_policy` in `mandate.yaml`.

## Invariants — do not break these

1. **Never add a second DuckDB writer.** New functionality reaches the
   registry through the owner API (or the combined server when no owner runs).
2. **Tests never open `.lab/registry.duckdb`** — use `Registry(":memory:")`.
   Tests must pass fully offline; synthetic fixtures stand in for market data.
3. **Referee PASS is bound to the exact `targets_hash`** on every execution
   path, and execution always requires a persisted checked plan. There are
   **three** recorded forms of confirmation, and no fourth:
   - **the click** — `human_confirmed=True` from the client, one click on the
     hash-bound BOOK box, never zero and never a second one;
   - **a persisted standing grant** (`qlab/governance/authority.py`,
     `authority_grants`) — the operator writes ceilings once
     (`POST /api/desk/authority`), and the owner's own 30 s beat books a
     referee-passed proposal the grant covers, with no click. It is bounded
     (universe, notional, turnover, orders, `max_books_per_day`, ≤ 30 days),
     revocable in one keystroke, suspended by any anomaly, refused for any
     plan older than `MAX_AUTO_BOOK_AGE_S = 120` s or already started, and it
     reaches `approved` through the same `decide_approval` the click calls —
     two ways in, one way to execute. `PAPER_AUTO` is the only mode; no live
     mode is expressible;
   - **two out-of-band env hatches** — `QLAB_AUTOPILOT_EXECUTE=1` for
     `qlab run-once`/`qlab watch` (`qlab/autopilot/loop.py`) and
     `QLAB_HEADLESS_EXECUTE=1` for the headless MCP
     (`qlab/mcp/quant_trader.py`), each authorizing one process the operator
     started.

   None of the three skips anything else: the referee PASS pinned to the
   plan's own hash, the mandate, the cost gate, reconcile and execution-time
   revalidation all still run. No agent can set an env hatch, and **no agent
   can reach a grant** — no MCP tool, chat action tool or proxy verb creates,
   edits, reads or consumes one, and both write routes refuse chat origin.
   Never introduce a raw-order tool or an agent-reachable execution path.
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
