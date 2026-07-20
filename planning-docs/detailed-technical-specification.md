# Quantum-Enhanced Trading Agent Lab

> **Status: ARCHIVED ORIGINAL SPEC.** It predates the owner-runtime refactor and
> the 2026-07-19 algorithm staging boundary. It is design history, not a claim
> about the current product surface.

## Context

The working directory is empty — this is a from-scratch build. The user wants a **concise, understandable, elegant** multi-agent trading research system that uses **quantum computing (Qiskit)** alongside classical optimization, inspired by two reference patterns they linked:

- **[tauricresearch/tradingagents](https://github.com/tauricresearch/tradingagents)** — hierarchical multi-agent trading firm simulation (analysts → researchers → trader → risk → portfolio manager), orchestrated with structured agent communication.
- **[polymarket-autopilot.md](https://github.com/hesamsheikh/awesome-openclaw-usecases/blob/main/usecases/polymarket-autopilot.md)** — a "skill" pattern: a scheduled loop (poll → analyze → paper-trade → log → summarize) against paper capital, with an explicit "never real money" guardrail.

The user also supplied their own brainstorm: a **Claude Code session as orchestrator**, delegating to **subagents** that talk to a **FastMCP "lab" server** over MCP (namespaced tools for data/moments/objective-solve/backtest/registry), backed by a **pure-Python quant core**, **pluggable solver adapters**, and a **DuckDB registry** for runs/artifacts. They confirmed via Q&A: keep this MCP-based lean multi-agent shape (don't flatten it into a single script, don't over-build it either), use **open-source SciPy/CVXPY** for the classical optimizer (Gurobi-swappable interface, not Gurobi itself), use the **Qiskit Aer local simulator** for the Quantum Research Agent (no IBM Quantum account needed), and make the Market Data Agent **open-source and "autopilot" style** — i.e. runnable as a standalone scheduled loop, not just an interactive MCP tool.

**Outcome**: a working local system where (a) inside a Claude Code session, the orchestrator delegates to subagents that call MCP tools on a local "lab" server to research and optimize a portfolio (classical vs. quantum comparison), and (b) a standalone autopilot loop can run the same pipeline unattended on an interval, paper-trading and logging results — matching the demo workflow: user asks for a recommendation → market data gathered → strategy proposes allocation → classical + quantum optimization compared → recommendation compiled → paper trade recorded.

## Architecture

```
Claude Code session (this app)
  orchestrator (main thread) — plans, delegates via Agent tool, synthesizes, talks to user
      │
      ├─▶ moments-analyst   (subagent, tools: data.*, strategy.*)
      ├─▶ optimization-runner (subagent, tools: optimize.*)
      ├─▶ referee            (subagent, read-only: registry.*, backtest.*)
      └─▶ reporter           (subagent, tools: paper.*, registry.*)
                │
                │  MCP (stdio, FastMCP) — namespaced tools, ref-passing only
                ▼
     trading_lab MCP server
     ┌───────────┬───────────┬────────────┬───────────┬───────────────┐
     │ data.*    │ strategy.*│ optimize.* │ backtest.*│ paper.*/      │
     │ (market   │ (signals/ │ (classical │ (evaluate │ registry.*    │
     │  data,    │  moments) │  + quantum │  a run)   │ (execute sim  │
     │  universe)│           │  solve +   │           │  trades,      │
     │           │           │  compare)  │           │  report)      │
     └─────┬─────┴─────┬─────┴──────┬─────┴─────┬─────┴───────┬───────┘
           │            guardrails: pydantic schemas · as_of tripwire ·
           │            constraint checks · call-budget ledger
           ▼            ▼            ▼           ▼             ▼
     quant core    solver adapters              DuckDB registry (.lab/registry.duckdb)
     (pure Python, - classical: SciPy/CVXPY     runs, moment_sets, objectives, solutions,
      no MCP         mean-variance              backtests, paper_trades, portfolio, decisions
      imports)     - quantum: Qiskit Aer QAOA   + content-addressed JSON artifacts (.lab/artifacts/)
                     (qiskit-optimization QUBO)
                   - mock (fast/deterministic, for tests)

Standalone autopilot (no Claude session needed):
  python -m trading_agents.autopilot.cli watch --interval 15m
    → calls the SAME quant core + solver adapters + registry directly (no MCP hop)
    → poll market data → strategy signal → classical solve (+ periodic quantum compare)
      → paper-trade → log to DuckDB ledger → write daily summary
    → safety constraint: paper capital only, hard-coded, never touches real orders
```

## Directory layout

```
IBM-Trading-Agent-v1/
├── README.md                        architecture, setup, how to run (chat + autopilot)
├── pyproject.toml                   package + deps (editable install)
├── .env.example                     optional: ALPHAVANTAGE_KEY etc. (all optional; yfinance needs none)
├── .mcp.json                        registers trading_lab MCP server for this project
├── .claude/agents/
│   ├── moments-analyst.md           subagent: data.* + strategy.* tools only
│   ├── optimization-runner.md       subagent: optimize.* tools only
│   ├── referee.md                   subagent: read-only registry.*/backtest.* (sanity/constraint check)
│   └── reporter.md                  subagent: paper.* + registry.* (final write-up + paper trade)
├── trading_agents/
│   ├── __init__.py
│   ├── core/
│   │   ├── types.py                 dataclasses: PriceBar, Signal, MomentSet, Objective, Weights, SolveResult
│   │   ├── universe.py              default ticker universe (small, liquid, free via yfinance)
│   │   ├── indicators.py            momentum/volatility signal calc → MomentSet (expected returns, cov)
│   │   └── objective.py             build a mean-variance QuadraticProgram-ready objective from a MomentSet
│   ├── data/
│   │   └── market_data.py           yfinance fetch + normalize OHLCV, local parquet cache
│   ├── solvers/
│   │   ├── base.py                  SolverAdapter Protocol (solve(objective, constraints) -> SolveResult)
│   │   ├── classical.py             CVXPY mean-variance solver (OSQP), long-only + budget constraint
│   │   ├── quantum.py               QAOA via qiskit-optimization QuadraticProgramToQubo + Aer Sampler
│   │   └── mock.py                  deterministic fast solver for tests/dry-runs
│   ├── registry/
│   │   ├── db.py                    DuckDB schema + typed helpers (runs, moment_sets, objectives,
│   │   │                            solutions, backtests, paper_trades, portfolio, decisions)
│   │   └── artifacts.py             content-addressed JSON artifact store (.lab/artifacts/<hash>.json)
│   ├── mcp_server/
│   │   ├── server.py                FastMCP app wiring all namespaces + guardrails (schema/as_of/budget)
│   │   ├── tools_data.py            data.fetch_universe, data.fetch_prices, data.get_moments
│   │   ├── tools_strategy.py        strategy.compute_signals, strategy.propose_allocation
│   │   ├── tools_optimize.py        optimize.solve_classical, optimize.solve_quantum, optimize.compare
│   │   ├── tools_backtest.py        backtest.run, backtest.evaluate
│   │   └── tools_registry.py        paper.execute, paper.portfolio_status, registry.list_runs, registry.report
│   └── autopilot/
│       ├── loop.py                  poll→analyze→paper-trade→log→summarize loop (in-process, no MCP)
│       └── cli.py                   `run-once` / `watch --interval` entrypoints
└── tests/
    ├── test_core.py                 indicators/objective math
    ├── test_solvers.py              classical vs quantum vs mock on a tiny synthetic universe (n≤6)
    └── test_registry.py             DuckDB round-trip (write run → read back)
```

## Key implementation notes

- **Quant core stays MCP-free**: `core/`, `solvers/`, `data/` never import the MCP server — the server is a thin wrapper that calls these and persists to the registry. This lets `autopilot/loop.py` call the same functions directly without spinning up MCP, and keeps unit tests fast/simple.
- **Solver interface parity with the brainstorm's "Gurobi Agent"**: `solvers/base.py` defines one `SolverAdapter.solve()` contract. `classical.py` implements it with CVXPY/OSQP (open-source, no license). A Gurobi backend could later satisfy the same interface — documented in README as an extension point, not built now (no license available).
- **Quantum solver**: cardinality-constrained binary portfolio selection (choose k of n assets maximizing expected return − λ·variance) expressed as a `qiskit_optimization.QuadraticProgram`, converted to QUBO, solved with `MinimumEigenOptimizer(QAOA(sampler=AerSampler(), optimizer=COBYLA(), reps=p))` — 100% local, no IBM Quantum account. Universe kept small (≤ ~8-10 assets) for tractable local simulation. `optimize.compare` runs classical + quantum on the same MomentSet and returns objective value, weights, and wall-clock time for both, so the recommendation engine (the `reporter` subagent) can present a real comparison.
- **Guardrails** (in `mcp_server/server.py`): pydantic-validated tool I/O, an `as_of` timestamp check on every data/strategy call (no lookahead into future prices), constraint validation on solver inputs (weights sum to 1, box bounds), and a simple per-session tool-call budget counter to keep it "lab"-like rather than unbounded.
- **DuckDB registry** doubles as both the brainstorm's "registry" and the polymarket-autopilot's `paper_trades`/`portfolio` tables — one local embedded file, no server process, free.
- **Autopilot loop** mirrors the polymarket-autopilot pattern directly: scheduled poll (configurable interval), strategy → optimize → paper-trade → DuckDB log → periodic text summary printed/written to `.lab/summaries/`. Hard-coded paper capital, explicit comment that it never places real orders (matches the linked skill's "never real money" guardrail).
- **Claude Code integration**: `.mcp.json` registers the server via stdio (`python -m trading_agents.mcp_server.server`). Four `.claude/agents/*.md` subagent defs give each subagent a narrow tool allowlist (namespace-scoped), so `moments-analyst` can't call `optimize.*`, etc. — mirrors the diagram's separation of concerns.
- **Dependencies** (`pyproject.toml`): `fastmcp`, `duckdb`, `yfinance`, `numpy`, `pandas`, `scipy`, `cvxpy`, `qiskit`, `qiskit-aer`, `qiskit-optimization`, `qiskit-algorithms`, `pydantic`. All free/open-source, no paid API keys required to run the full demo.

## Revisions from what past implementations actually taught us

| Discovery during past builds | Plan revision |
|---|---|
| Yahoo Finance intermittently 429-rate-limits; a hung fetch stalled the pipeline for minutes before we added a 15s timeout | **Demo resilience:** pre-warm the parquet cache immediately before any demo/judging session; add a `--offline` flag to the autopilot that refuses network and serves cache only, so the live demo cannot be taken down by a 429 |
| FastMCP runs sync tools in a worker-thread pool; Qiskit's Rust/BLAS internals corrupt state off the main thread on macOS | Keep `run_in_thread=False` on quantum tools + the retry guard; **call this out in the submission write-up** — a real, root-caused integration finding is judge-friendly material |
| Aer's deprecated V1 Sampler is incompatible with current `qiskit-algorithms`; QAOA ansatz needs explicit transpilation for Aer | Pin the working version set in `pyproject.toml` (qiskit 2.5 / aer 0.17 / algorithms 0.4 / optimization 0.7) so the judges' install reproduces ours |
| Machine had only Python 3.9; MCP SDKs require ≥3.10 | README setup section states Python ≥3.10 explicitly with the Homebrew one-liner (done — keep) |
| QAOA at n=8, k=4 reaches 100% approx ratio consistently | Add one chart to the submission: approx ratio + runtime vs. universe size (n = 4…10), generated by a small script — turns "it works" into "we measured it" |
| Registry singleton portfolio made tests order-dependent until isolated | Keep per-test DB isolation; no further action |

## Demo workflow (what "done" looks like)

1. `pip install -e .` in the project.
2. Interactively: user asks Claude Code (in this project) for a portfolio recommendation → orchestrator delegates to `moments-analyst` → `optimization-runner` (classical + quantum compare) → `referee` (sanity check) → `reporter` (final recommendation + paper trade executed + DuckDB-logged).
3. Standalone: `python -m trading_agents.autopilot.cli run-once` runs the same pipeline end-to-end without any chat session and prints a summary; `watch --interval 15m` runs it on a loop.
4. `pytest` runs core/solver/registry tests offline (mock solver + tiny synthetic data, no network).

## Verification

- `pytest tests/` — core math, classical vs. quantum vs. mock solver agreement/sanity on a small synthetic case, DuckDB round-trip.
- `python -m trading_agents.autopilot.cli run-once` — exercises the full pipeline against live yfinance data end-to-end and prints a recommendation + simulated fill.
- Start the MCP server standalone (`python -m trading_agents.mcp_server.server`) and confirm it lists the expected namespaced tools, then (in a fresh Claude Code session in this project) ask for a portfolio recommendation and confirm the four subagents are invoked in sequence and a paper trade lands in the DuckDB registry.
