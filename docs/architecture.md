# Architecture

One writer, staged authority, and where every surface attaches. See the [README](../README.md) for the short version.

## One writer, always

DuckDB is both the research registry and the paper book. Only one process may
own it.

    qlab tui
        |
        +-- owner HTTP runtime ---- DuckDB registry and paper book
        |       |
        |       +-- the Atlas workstation and the CLI verbs observe over HTTP
        |       +-- qlab-operator gives the Claude workforce role-bound HTTP tools
        |
        +-- paper execution needs the operator's confirmation: one click on
            the hash-bound box, or a standing grant they signed in advance

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
regime-conditioned covariance, the volatility-target overlay,
`cardinal_min_variance` (exact k-of-N selection, then min-variance on the chosen
names), and the optional Dirac-3 adapter. `mandate.yaml` currently selects HRP
as the paper allocation policy because it is the robust benchmark supported by
the present evidence; the operator may choose another *operational* policy from
the desk, never a research one.

`cardinal_min_variance` met its pre-registered promotion gate on the ablation
and stayed at research anyway — one repeated basket is not 57 selection
decisions, and `OperationalPolicy.arm()` drops `params`, so promotion as
specified would have run plain min-variance under the cardinal name. The
numbers and the decision are in
[the record](../planning-docs/2026-08-31-a6-cardinality-not-promoted.md).

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
  count, `max_holdings` (how many names a plan may hold — checked by
  `Mandate.check_targets`, so every policy including HRP is held to it), and
  trailing-drawdown kill switch.
- The desk asks about **one proposal at a time**. A newer checked plan
  supersedes the older pending one and invalidates its approval with a reason;
  `GET /api/desk/proposal` is that one object, and
  `POST /api/desk/proposal/book` approves and executes it in a single call. The
  client posts the `targets_hash` its confirmation box displayed; the owner
  re-validates the approval, the plan, and the referee PASS pinned to that hash
  before any fill. One explicit human confirmation — one click, never two.
- **Standing authority** is the second way that proposal becomes a fill, and
  the only one with no click (`qlab/governance/authority.py`, the
  `authority_grants` table, `book_under_grant` in `qlab/ui/server.py`). The
  operator writes one grant with every ceiling explicit — universe, notional,
  turnover, order count, `max_books_per_day`, and a TTL of at most 30 days —
  through `POST /api/desk/authority`, which refuses a chat origin, a missing
  ceiling, and a non-finite number. While that grant stands, the owner's own
  30-second heartbeat books a referee-passed proposal the grant covers,
  reaching `approved` through the same `decide_approval` the click calls: two
  ways in, one way to execute. It refuses a plan outside any ceiling, a plan
  touching a symbol outside the grant's universe (refused **whole**, never
  trimmed), a plan older than `MAX_AUTO_BOOK_AGE_S = 120` s — the click is a
  freshness proof, and nothing else supplies one — a plan that has already
  started, and anything an anomaly suspends: a halted book, an unclean
  reconcile, no execution data permit, or a rejected or expired order in the
  recent window. An anomaly input the owner cannot compute is itself an
  anomaly. `PAPER_AUTO` is the only mode a grant can express. `R` on
  Settings ▸ AUTHORITY revokes at once, no box and nothing typed, and
  revocation is checked before anything else. No agent can create, read or
  consume a grant.
- Rebalances are two-phase, persisted by leg, transactional, and resumable
  without duplicate orders — except that a plan that **part-filled and then
  failed** is not resumable today: the approval survives, but
  `check_approval_for_execution`'s `book_revision` binding refuses the resume,
  so it needs a person. See
  [the completion record](../planning-docs/2026-09-01-standing-authority-completion.md).
- Every decision carries a challenger view and can be scored against realized
  outcomes by the reflection loop.
- No MCP tool accepts a raw order.
- Paper trading is the only execution mode.

### Operator and agent surfaces

- qlab tui is the terminal face of the owner runtime — ten views, Visuals on
  key `0`.
- qlab owner runs the same owner runtime headless, for a desk kept up as a
  service that workstations attach to.
- The combined qlab MCP server is for headless orchestration and refuses to
  start while the owner runtime is alive.
- The qlab-operator MCP proxy is the propose-only surface for Claude sessions
  launched under the TUI. It observes the owner over HTTP and cannot execute a
  paper trade or open DuckDB.
- Claude runs as an isolated, session-local qlab coordinator, not a developer.
  Its only built-in tool is an allowlisted Agent dispatcher for eight roles —
  the six workflow phases and the two advisory ones — plus the quarantined
  news-extractor on a goal that names news or views. The role files are
  generated into a temporary project at launch so the Windows command line
  stays short. No role receives Read, Bash, Edit, Write, raw-order, or
  paper-execution tools, and among them only contender-scout receives
  WebSearch and WebFetch.
- Workforce runs persist analyst → challenger → optimizer → referee → reporter
  phase state in the owner registry, so a stopped CLI session is inspectable and
  resumable from a new one.
- Eleven least-privilege roles are generated from one neutral source
  (`agents/*.md`) for Claude Code and IBM Bob. Five of them are the rebalance
  pipeline — moments analyst, challenger, optimization runner, referee,
  reporter — and five is the pipeline's length, not the roster's. The other six
  are `atlas` (the desk manager: it reads persisted facts, picks one registered
  template and writes the brief; it never trades and never approves),
  `contender-scout` (the `portfolio_watch` template's quarantined web role,
  whose whole grant is `WebSearch`, `WebFetch` and two registry decision
  tools), `news-analyst`, `news-extractor`, `data-qa` and `signal-qa`.
- Operator choices that are not the governance document live in state, merged
  by `load_mandate`: the operational method and `max_holdings` in
  `state_path("mandate_overrides.json")` (`GET`/`POST /api/desk/method`), and
  the three Atlas rights in `state_path("atlas_rights.json")`
  (`GET`/`POST /api/atlas/rights`). `mandate.yaml` stays the shipped document.
- `POST /api/research/predictors/run` runs one predictor lane and its baseline
  from the desk; the board is the same artifact the research surface reads.
- `GET /api/visuals` lists what this build can draw and `GET /api/visuals/<name>`
  renders one as text, which is what the VISUALS view paints.

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
      tui/          owner-client plumbing: API client, Claude session, theme
      ui/           owner HTTP runtime: the one DuckDB writer, no client
    agents/         source-of-truth role definitions
    clients/        atlas-tui, the Ratatui client for the same owner runtime
    docs/           operator setup guides
    configs/        universe and staged experiment specs
    planning-docs/  the published design records (the full dated record is local)
    tests/          offline-first regression suite

