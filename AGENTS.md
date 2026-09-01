# AGENTS.md

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
qlab tui                            # terminal workstation (starts/attaches owner)
qlab run-once --offline --dry-run   # one governed autopilot cycle, no orders
qlab batch configs/specs/ablation_v1.yaml --offline   # staged ablation
python -m qlab.agents.loader sync   # regenerate .claude/agents + .bob/personas
```

Quantum research is an isolated offline lane: `pip install -e
".[offline-quantum]"` then `python -m pytest tests/test_quantum.py`. It is
excluded from `[all]`, the staged runtime, and the default ablation.

## Architecture in sixty seconds

- **One DuckDB writer, always.** The owner HTTP runtime (`qlab/ui/server.py`,
  started by `qlab` or `qlab owner`) is the only process that opens
  `.lab/registry.duckdb`. Every other surface — the Atlas workstation
  (`clients/atlas-tui`), the CLI verbs, and the `qlab-operator` MCP proxy
  (`qlab/mcp/tui_proxy.py`) — talks to it over HTTP only. The Textual and web
  clients are retired; `qlab/tui/` retains only client plumbing.
- The combined `qlab` MCP server (`qlab/mcp/server.py`) is for headless
  sessions and refuses to start while an owner runtime is alive (port guard).
- **Claude workforce**: the TUI can launch Claude as a coordinator whose only
  built-in tool is an allowlisted Agent dispatcher for eight roles
  — the six workflow phases (moments-analyst, contender-scout, challenger,
  optimization-runner, referee, reporter) and the advisory data-qa and
  signal-qa. Only contender-scout holds WebSearch/WebFetch.
  Phase state persists in the registry (`workflows`/`workflow_steps`) and is
  resumable. No Claude role has filesystem, shell, or execution tools.
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
   path, and execution always requires a persisted checked plan. Three
   recorded forms of confirmation, and no fourth: (a) the click —
   `human_confirmed=True` from the client, on the hash-bound BOOK box; (b) a
   persisted standing grant (`qlab/governance/authority.py`), written once by
   the operator with every ceiling explicit, under which the owner's own 30 s
   beat books with no click — bounded, revocable in one keystroke, suspended
   by any anomaly, and refused for a plan older than 120 s or already started;
   (c) two out-of-band hatches — `QLAB_AUTOPILOT_EXECUTE=1` for `qlab
   run-once`/`qlab watch` and `QLAB_HEADLESS_EXECUTE=1` for the headless MCP —
   each authorizing one process the operator started. None of the three skips
   the referee, the mandate, the cost gate, reconcile, or execution-time
   revalidation. No agent can set an env hatch, and no agent can reach a
   grant: no MCP tool, chat action tool or proxy verb creates, edits, reads or
   consumes one, and both write routes refuse chat origin. Never introduce a
   raw-order tool or an agent-reachable execution path.
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

## Conventions

- Commit messages: imperative, conventional prefix + scope
  (`fix(solvers): …`, `docs(readme): …`). No AI-attribution trailers.
- Match the existing comment density: comments state constraints the code
  cannot show, not narration.
- Property-style assertions in tests are deliberate (e.g. compiler agreement
  to 1e-10); never weaken an assertion to make a change pass — fix the change.
- `planning-docs/` is historical record; new status goes in a new dated file
  rather than rewriting old ones (they carry superseded banners).
