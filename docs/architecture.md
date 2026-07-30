# Architecture

One writer, staged authority, and where every surface attaches. See the [README](../README.md) for the short version.

## One writer, always

DuckDB is both the research registry and the paper book. Only one process may
own it.

    qlab tui
        |
        +-- owner HTTP runtime ---- DuckDB registry and paper book
        |       |
        |       +-- TUI and web clients observe or request actions over HTTP
        |       +-- qlab-operator gives the Claude workforce role-bound HTTP tools
        |
        +-- explicit human confirmation is required for paper execution

When no owner runtime is running, Claude Code can launch the combined qlab MCP
server from .mcp.json. That server mounts the research and trader namespaces
over one Registry instance. Per-agent tool allowlists enforce role authority.

## Algorithm catalog

qlab/algorithms is the deployment inventory used by the owner UI and AI agents.
Every entry has a category and one of three stages.

| Stage | Meaning | Agent execution |
|---|---|---|
| operational | Approved for the staged MCP surface | Allowed through algorithms.solve or its declared tool |
| research | Evaluated in controlled experiments but not promoted | Visible, not directly runnable by agents |
| offline | Isolated implementation retained outside the desk | No CLI, HTTP, TUI, or MCP execution path |

Operational examples include HRP, equal risk contribution, minimum variance,
and scenario CVaR. Research entries include classical multistart MVSK,
regime-conditioned covariance, the volatility-target overlay, and the optional
Dirac-3 adapter. `mandate.yaml` currently selects HRP as the paper allocation
policy because it is the robust benchmark supported by the present evidence.

### Why MVSK is still here

MVSK means mean–variance–skewness–kurtosis. qlab's version deliberately omits an
expected-return forecast: it minimizes variance, rewards positive portfolio
skewness, and penalizes portfolio kurtosis. It entered the project as the
falsifiable higher-moment research thesis and as a useful degree-four polynomial
for comparing classical and continuous-HUBO compilation—not as an already proven
trading edge. The tested 2018–2026 walk-forward result did not beat HRP/ERC/60-40,
so MVSK remains visible in ablations but is no longer deployable by agents.

The optimization-runner agent follows this flow:

    algorithms.list(stage="operational")
        -> algorithms.describe(algorithm_id)
        -> algorithms.solve(objective_id, algorithm_id)
        -> referee and reporter consume the persisted result

Offline QAOA and Ising-encoding experiments are retained under
qlab/algorithms/offline for isolated research. They are not loaded by normal
solver discovery, are excluded from the default install and ablation, and have
no IBM hardware adapter. Install their dependencies only when working on that
offline lane:

    python -m pip install -e ".[offline-quantum]"
    python -m pytest tests/test_quantum.py tests/test_algorithms.py

No offline quantum result is part of the staged trading or submission claim.

## What is implemented

### Research substrate

- Seven-ETF cross-asset core and a wider candidate pool.
- Point-in-time snapshots with explicit provenance and synthetic offline
  fixtures.
- Ledoit–Wolf linear shrinkage, LW2020 nonlinear shrinkage, Marchenko–Pastur
  denoising, and shrunk co-moment tensors.
- One MVSK polynomial compiled for the classical and optional external solver
  paths.
- Cash-carry-correct walk-forward backtests.
- Benchmarks and challengers including 60/40, 1/N, HRP, equal risk
  contribution, minimum variance, scenario CVaR, MVSK, regime conditioning,
  and a research-only volatility target.
- Deflated Sharpe over cumulative registry trial counts and stationary
  bootstrap confidence intervals.

### Regime indicators

The moments-analyst does not read one regime number; it is given five
deterministic, price-only indicators — each a different face of market
variability — and must synthesize them into one defended regime call before it
sets the estimation window and shrinkage. Every indicator returns the same
schema (`regime` calm/stress, the `signal`, its own trailing `threshold` and
`percentile`, and a one-line `reasoning`), so unlike readings compare directly.
"Stress" always means *unusual for this market* — the tail of the indicator's
own history — never a hard-coded level. None forecasts returns.

| Tool | Face of variability | Logic |
| --- | --- | --- |
| `regime.turbulence` | statistical unusualness of the latest joint move | Chow–Kritzman Mahalanobis turbulence, per degree of freedom |
| `regime.absorption` | systemic fragility / coupling | Kritzman absorption ratio — variance share in the top eigenvectors |
| `regime.volatility_term_structure` | acceleration of variance | short- over long-horizon realised-vol ratio |
| `regime.drawdown` | directional stress | equal-weight peak-to-trough depth with a trend filter |
| `regime.tail_risk` | downside asymmetry | rolling downside/upside semi-deviation ratio with recent skew |

The five are exposed to the analyst through the `qlab-operator` proxy and, in
headless mode, the combined server; the primitives live in `qlab/signals` and
the agent-facing readings in `qlab.signals.indicators`. The selected regime and
its reasoning are persisted on the analyst phase and shown on their own line in
the TUI workforce view.

### Governance and paper execution

- Referee PASS is bound to the exact reviewed targets.
- A mandate enforces the whitelist, long-only weights, caps, turnover, order
  count, and trailing-drawdown kill switch.
- Rebalances are two-phase, persisted by leg, transactional, and resumable
  without duplicate orders.
- Every decision carries a challenger view and can be scored against realized
  outcomes by the reflection loop.
- No MCP tool accepts a raw order.
- Paper trading is the only execution mode.

### Operator and agent surfaces

- qlab tui is the terminal face of the owner runtime.
- qlab ui starts the same owner runtime with a local web client.
- The combined qlab MCP server is for headless orchestration and refuses to
  start while the owner runtime is alive.
- The qlab-operator MCP proxy is the propose-only surface for Claude sessions
  launched under the TUI. It observes the owner over HTTP and cannot execute a
  paper trade or open DuckDB.
- Claude runs as an isolated, session-local qlab coordinator, not a developer.
  Its only built-in tool is an allowlisted Agent dispatcher for the five domain
  roles. The role files are generated into a temporary project at launch so the
  Windows command line stays short. No role receives Read, Bash, Edit, Write,
  browser, raw-order, or paper-execution tools.
- Workforce runs persist analyst → challenger → optimizer → referee → reporter
  phase state in the owner registry, so a stopped CLI session is inspectable and
  resumable from a new one.
- Five least-privilege roles are generated from one neutral source for Claude
  Code and IBM Bob: moments analyst, challenger, optimization runner, referee,
  and reporter.

## Repository map

    qlab/
      algorithms/   categorized deployment catalog and offline research boundary
      agents/       neutral-definition loader and Claude/Bob adapters
      core/         data, moments, objective, metrics, backtest, types
      governance/   deterministic referee and reflection loop
      mcp/          combined server, tool namespaces, propose-only owner proxy
      signals/      deterministic regime signals, agent-facing indicators, conditioning
      solvers/      uniform implementation adapters
      state/        DuckDB registry and content-addressed artifacts
      trader/       mandate, broker adapters, plans, reconcile
      operator/     Atlas supervisor, heartbeat, model routing, coordinator driver
      research/     purged walk-forward prediction and quantum-inspired features
      tui/          Textual operator workstation
      ui/           owner HTTP runtime and local web client
    agents/         source-of-truth role definitions
    clients/        atlas-tui, the Ratatui client for the same owner runtime
    docs/           operator setup guides
    configs/        universe and staged experiment specs
    planning-docs/  current status, delivery map, and archived plans
    tests/          offline-first regression suite

