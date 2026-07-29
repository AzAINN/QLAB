# qlab — a governed agentic quant research desk

qlab turns research questions into reproducible portfolio experiments, promotes
only reviewed decisions, and books approved paper trades through one auditable
runtime.

The design boundary is simple:

- AI agents own judgment: estimation windows, challenge cases, explanations,
  and research proposals.
- Algorithms own numbers: estimation, optimization, backtesting, and metrics.
- Deterministic code owns rigor: point-in-time checks, the mandate, referee
  approval, order construction, execution idempotency, and audit events.

The honest current result is that simple benchmarks still beat the MVSK arms
out of sample on the tested 2018–2026 window. qlab records that result instead
of turning an experiment into a marketing claim.

## Start the desk

Python 3.10 or newer is required.

    python -m pip install -e ".[operator]"
    qlab tui

The terminal opens as a quiet, no-top-header workstation. It starts or connects
to one owner HTTP process; the TUI itself never opens DuckDB. The command row
carries two chips only — owner connection, and the desk mode that says whose
book is live; the system and service detail sits in Settings (`8`).

The complete screen map is available from either the digit or matching function
key:

- `1` / `F1` — **Atlas**: the desk manager's read — what the signals, the news,
  and the research add up to, and where they disagree. This is the default view.
- `2` / `F2` — Dashboard: portfolio, regime, allocation, and alerts.
- `3` / `F3` — Market: daily price context and provenance.
- `4` / `F4` — Workforce: governed coordinator chat and phase progress.
- `5` / `F5` — Research: experiment runs and algorithm evidence.
- `6` / `F6` — Book: positions, checked plans, confirmation, and paper orders.
- `7` / `F7` — Audit: decisions, challenges, verdicts, and reflections.
- `8` / `F8` — Atlas: what every arm, metric, role, and rule is, with the live
  champion and its latest ablation numbers.
- `9` / `F9` — Settings: read-only mandate, data, agent, and theme bulletins.

### Atlas, the desk manager

Atlas runs continuously inside the owner on a heartbeat (`QLAB_BOB_INTERVAL_S`,
default 30s). Each tick it evaluates deterministic triggers against owner facts
and recomposes its **read**: one view across the regime panel, the news record,
and what the workforce concluded.

The read leads with the part a number cannot express — the **tensions**, where
the evidence disagrees with itself. "Prices are calm but the coverage is not"
is the case Atlas exists to surface. Conviction describes how much the evidence
agrees, never how likely a price move is.

Atlas escalates a material disagreement into the same registry-enforced debate
the workforce uses — allowlisted claim, two-round ceiling, adjudication the
reporter waits on. It holds read-only tools and cannot trade, approve, or
create a paper plan in any mode; `Ctrl-B` opens its detail drawer from any view.

Modes: `observe` (monitor and brief), `research` (may start approved research
workflows), `propose` (may request a checked plan for human approval), and
`paused`. The mode is the authority statement and is shown wherever Atlas is.

    qlab tui --claude offer   # default: show readiness, never prompt
    qlab tui --claude auto    # start the workforce after the first snapshot
    qlab tui --claude off     # start only when : workforce GOAL requests it

Inside the desk, the workforce view (key `4`) is a chat: type to the
coordinator and it deploys the five governed roles. Progress is a flowchart —
hover a node for that phase's live summary, elapsed time, and artifacts — and
the console stays quiet, printing one short note per agent (what it settled,
what runs next) and the run's results at the end. Full tool traffic remains on
the timeline (`~`). Follow-up messages continue the same session, the `■ stop`
button interrupts without losing durable phase state, and a run that stalls is
stopped by a watchdog rather than hanging the desk. Stop terminates the entire
Claude/coordinator/Agent/MCP child-process tree, marks the active durable phase
`interrupted`, and fences late child writes until an explicit resume. A
successful Claude exit that leaves a phase open is treated the same way rather
than leaving the desk painted `working`. Owner startup recovers orphaned
`running` rows, and the owner also expires rows older than the coordinator
lease. `: workforce GOAL`, `: workforce status`, `: workforce resume ID`,
`: workforce stop`, and `: workforce abandon [ID]` drive the same machinery
from the command row. Abandon permanently closes unfinished phases but retains
completed evidence, events, and the audit record; it does not delete registry
state.

`: chat MESSAGE` switches the same chat box to a read-only desk assistant —
ask about the portfolio, market, runs, or audit trail conversationally; it
holds observation tools only (no agents, no writes, no execution) and keeps
its own session, separate from the workforce coordinator.

For a core-only install:

    python -m pip install .
    qlab run-once --offline --dry-run

A normal wheel includes the default mandate, universe, experiment spec, and
agent definitions. Runtime state is never written into site-packages.

### Real market data and whose book

The desk opens on offline synthetic data with a simulated book, so it runs with
no account at all. Two independent things can be switched on: the data lane and
the book.

    alpaca profile login      # browser OAuth; paper-only by construction
    qlab tui --live           # online market data, qlab's own simulated book
    qlab tui --alpaca-book    # online market data and your Alpaca paper book

`--live` (equivalently `--online`) only takes the data lane online; which
provider serves it is `QLAB_DATA_PROVIDER`, and that defaults to `yfinance`.
Alpaca market data is a separate, additional choice: it needs
`QLAB_DATA_PROVIDER=alpaca` **and** exported `ALPACA_API_KEY` /
`ALPACA_API_SECRET`, because the daily-bar provider reads those environment
variables directly. The `alpaca profile login` session reaches the **book**
lane only — with an OAuth-only login, `--alpaca-book` trades your real Alpaca
paper account while prices still come from yfinance.

`--alpaca-book` implies `--live`: reaching the real paper account is never a
side effect of asking for real prices. The same three modes are offered by the
startup modal on first launch — the flags only skip the question — and the
choice persists, so later launches reopen the desk in the mode you left it in.
The desk-mode chip in the command row names the mode currently in force.

Every mode is paper-only; there is no live-trading path to select, and the
browser login cannot grant one (the Alpaca CLI puts live behind its separate
`--api-key --live` flow). That login is preferred over `ALPACA_API_KEY` /
`ALPACA_API_SECRET` because it leaves no secret to paste or store. If you use
keys anyway, either export them or put them in `.env`, which the CLI loads at
startup — an already-exported variable outranks the file, and a blank entry in
the file is ignored rather than treated as empty credentials. Note that qlab's
`ALPACA_API_SECRET` is spelled `ALPACA_SECRET_KEY` by the Alpaca CLI.

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

## Claude Code deployment

The neutral role definitions live in agents/. Regenerate both orchestration
formats with:

    python -m qlab.agents.loader sync

This writes:

- .claude/agents/*.md for Claude Code.
- .bob/personas/*.yaml for IBM Bob.

The generated optimization-runner receives algorithm discovery and staged solve
tools. It can inspect research and offline catalog entries, but the server
rejects attempts to execute them through the staged path.

There are two valid Claude modes:

1. Headless: Claude Code launches the combined qlab server from .mcp.json when
   no owner UI process is running.
2. Workforce desk: qlab tui owns the book and optionally launches a session-local
   `qlab-coordinator` against the qlab-operator proxy. The coordinator deploys
   only the five qlab roles, following the dependency graph the registry
   enforces: analyst, bounded challenger debate, optimizer on the final
   persisted decision, referee gate, then reporter. They can inspect, research,
   persist judgments, and request a dry rebalance preview; only the human-facing
   TUI can confirm paper execution.

Starting a retired standalone quant-lab or quant-trader module now delegates to
the guarded combined server, so those module paths cannot recreate the old
two-writer topology.

## IBM Bob

Bob is IBM's agentic SDLC environment — the Bob IDE, and Bob Shell for the
terminal. In qlab it has one job, stated precisely because the whole project is
an argument about authority: **Bob is a governed client of the desk, never an
authority inside it.**

That is not a limitation imposed on Bob. It is the same boundary every
orchestrator here lives behind. qlab's rigor is enforced by deterministic code —
the mandate, the referee gate, execution idempotency, one DuckDB writer — so
that swapping the model or the IDE driving the desk cannot change what the desk
is allowed to do. Bob inherits that guarantee by construction.

### Why Bob is a good fit for this desk

A quant desk is an SDLC problem wearing a trading hat. The repetitive work here
is not placing orders — it is regenerating role adapters after a prompt edit,
restarting the owner after changing code it serves, keeping the offline suite
green, and adding a catalog entry at the right stage. Those are the invariants
most easily broken by moving fast, and they are exactly what Bob's rules and
skills exist to hold. Meanwhile the judgment work — which estimation window,
which regime call, what the news actually supports — stays with the governed
roles and their evidence trail.

Bob also brings an approval model that composes with qlab's rather than
competing with it. Bob asks before it acts; qlab refuses unless a human
confirms. Two independent gates on the same action is the correct number for a
desk that touches a book.

### The connection: `.bob/mcp.json`

Bob attaches to the running desk through the same propose-only MCP proxy the
Claude workforce uses. Start an owner runtime, then open the project in Bob:

    qlab tui          # or: qlab ui --no-browser

`.bob/mcp.json` points Bob at `qlab.mcp.tui_proxy`, which never opens DuckDB and
talks to the owner over HTTP only. Bob can read the portfolio, the regime panel,
the audit trail, the research runs, and the algorithm catalog; it can request a
*dry* rebalance preview. It cannot execute a paper trade, because no MCP tool
here accepts a raw order and execution requires `human_confirmed=True` from the
TUI.

The `alwaysAllow` list in that file is the governance boundary made concrete:
pure observation is auto-approved, and every tool that persists a decision,
starts a workflow, runs a solver, fetches from the network, or previews a plan
stops for an explicit human click. The second entry, the combined `qlab` server,
ships `disabled: true` — it is the no-owner headless path, and enabling it while
an owner is alive is refused by the port guard rather than quietly creating a
second writer.

### Roles from one source

`agents/*.md` is the single source of truth for the org chart.
`python -m qlab.agents.loader sync` projects it into `.claude/agents/*.md` for
Claude Code and `.bob/personas/*.yaml` for Bob, so the two orchestrators cannot
drift apart on what a role is or what it may touch.

Bob's own mode system is a close match for this: a custom mode is a role
definition plus instructions plus a deterministic set of permitted tools, and
its tool groups (`Read`, `Edit`, `Execute`, `MCP`, `Skill`, …) make an
**MCP-only** mode expressible directly — no filesystem, no editor, no shell,
which is precisely what every qlab role already asserts about itself.

Being straight about current state: `.bob/personas/*.yaml` is qlab's neutral
projection of a role, not yet a file Bob loads. Emitting real
`.bob/custom_modes.yaml` from the same source is the next step, and it is
tracked with the open schema questions in
[planning-docs/2026-07-26-ibm-bob-integration-options.md](planning-docs/2026-07-26-ibm-bob-integration-options.md)
alongside the seams a Bob Shell coordinator backend would attach to. The model
invocation record already carries a `backend` column, so a Bob-served phase is
auditable the day it exists.

## Commands

    qlab run-once --offline --dry-run
    qlab daily-ops --offline
    qlab batch configs/specs/ablation_v1.yaml --offline
    qlab recommend --offline
    qlab prewarm --universe core
    qlab ui --no-browser
    qlab tui --claude offer
    qlab desk
    qlab workforce run "Review the desk and challenge the estimation window"
    qlab workforce status
    qlab workforce interrupt --id WORKFLOW_ID
    qlab workforce abandon --id WORKFLOW_ID
    qlab events --kind workflow_phase

run-once performs analysis, solves the configured operational policy, logs the decision,
runs the deterministic referee, reconciles the book, and proposes a
mandate-checked paper plan.

**run-once is proposal-only.** A plan that clears the referee and the net-alpha
cost gate becomes a pending, exact-plan-bound approval request — an autonomous
cycle never books a fill on its own. A human grants the approval (through the
TUI or `POST /api/approvals/<id>/approve`), and executing it consumes that
approval. Booking directly from the cycle requires the operator to authorize
that process out of band with `QLAB_AUTOPILOT_EXECUTE=1`, which nothing
connected to the process can set for itself.

daily-ops only reconciles, reports risk, resolves reflections, and checks
triggers. It cannot trade.

The desk verbs are the research-CLI face of the owner runtime: `qlab desk`
prints one status card, `qlab workforce run` drives a governed run headless
and streams the coordinator plus the owner's live audit bus, and `qlab
events` tails that bus (`GET /api/stream`, server-sent events). Ctrl-C during a
headless run interrupts its durable workflow and terminates the full reasoning
process tree. `workforce interrupt` fences an orphan for later resumption;
`workforce abandon` closes it while retaining its audit. The TUI
subscribes to the same stream, so phase changes and verdicts land on every
surface the moment they are recorded. None of these verbs can execute a
paper trade.

batch runs the reproducible staged experiment matrix. Specs containing the
legacy quantum_arms block are rejected as offline research.

## Data and broker limits

The current operator surface is research and paper-first:

- Online mode uses cached, adjusted daily bars from `QLAB_DATA_PROVIDER`;
  `yfinance` is the default and `alpaca` is optional.
- Offline mode uses cache or deterministic synthetic fixtures.
- Market provenance records the producing provider (`yfinance`, `alpaca`, or
  `synthetic`), and the as-of date and bar age are shown to the operator.
- Alpaca support requires the trader extra plus one credential source: an
  `alpaca profile login` session, or `ALPACA_API_KEY` and `ALPACA_API_SECRET`
  either exported or set in `.env`, which the CLI loads at startup. The browser
  login
  currently reaches the **broker** only — the `alpaca` daily-bar provider reads
  the two environment variables directly, so Alpaca market data still needs
  exported keys. It remains paper-only and daily-bar-only: there is no
  streaming quote tape or complete order-lifecycle integration.
- Selecting Alpaca without its package or credentials fails loudly; qlab does
  not silently switch the request back to yfinance.
- The simulated broker remains the zero-account default.

## Configuration and state

Editable checkout configuration:

- configs/universe.yaml — core and candidate universes.
- configs/specs/ablation_v1.yaml — staged experiment matrix.
- mandate.yaml — deterministic paper-trading limits and operational policy.
- agents/ — neutral role definitions.

Optional path overrides:

| Variable | Purpose |
|---|---|
| QLAB_WORKSPACE | Project-local output and adapter root |
| QLAB_STATE_DIR | Registry, cache, artifacts, and summaries |
| QLAB_CONFIG_ROOT | Alternate mandate/config/agent bundle |
| QLAB_UI_PORT | Owner-runtime guard port |
| QLAB_OFFLINE | Default MCP data mode |
| QLAB_DATA_PROVIDER | Online daily-bar provider (`yfinance` or `alpaca`) |

An installed wheel defaults its writable state to .lab under the current
workspace, not the Python environment.

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
      tui/          Textual operator workstation
      ui/           owner HTTP runtime and local web client
    agents/         source-of-truth role definitions
    configs/        universe and staged experiment specs
    planning-docs/  current status, delivery map, and archived plans
    tests/          offline-first regression suite

## Verify

    python -m pytest

For a public-install check, build a wheel, install it into an empty environment,
and run an offline command from outside the repository. The packaged data and
runtime-path tests cover the same boundary in the suite.

## Current direction

The next research work should explain why MVSK loses before adding more solver
complexity: lambda sweeps, estimator sensitivity, and bounded,
provenance-carrying news views. The next operations work is real Alpaca paper
integration, market-calendar scheduling, and exercising the Bob adapters.

Offline algorithm experiments can continue independently, but promotion into
the desk requires evidence, a catalog stage change, tool review, and new
governance tests.

## License

MIT. See LICENSE.
