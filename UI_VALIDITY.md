# UI validity and deployment boundary

> Updated 2026-08-21. Describes the owner runtime and the Atlas workstation
> (`clients/atlas-tui`), the desk's one client since the Textual TUI and the
> web client were retired. The former staged Quantum panel and QAOA comparison
> were removed earlier.

Use `qlab` for the desk. The workstation talks to one owner process that
exclusively holds the DuckDB research registry and paper book. Confirming a
paper trade lives in the workstation's hash-bound confirm modal, on an armed
desk only; the `--no-default-features` build has no order path at all.

## Interpret every number in context

There are three separate questions:

1. **Is the implementation internally checked?** Property and regression tests
   cover objective representations, point-in-time data access, mandate checks,
   idempotent execution, accounting, and API boundaries.
2. **Does a displayed number describe real market history?** Only when its
   provenance says the data came from the online/cache path. Offline synthetic
   results are deterministic fixtures, not investment evidence.
3. **Is a method deployed?** The Algorithms panel is authoritative. Visibility
   does not imply execution authority: only `operational` entries can run through
   staged agent tools.

Implementation tests establish behavior against the encoded specification. They
do not, by themselves, establish economic usefulness or future performance.

## Data and execution modes

| Mode | Prices | What conclusions are valid? |
|---|---|---|
| Offline with cache | Previously cached daily bars | Historical only, with the displayed as-of and staleness metadata |
| Offline without cache | Deterministic synthetic fixture | Pipeline behavior and reproducibility only |
| Online | Daily yfinance or Alpaca history refreshed into cache | Historical research only; this is not a streaming quote feed |

A synthetic fixture is identified by its seed as well as its universe and dates:
two seeds are two different samples and no longer share a cache entry. That
matters for validity, not just tidiness — a robustness sweep that silently
re-served one sample would report perfect stability for a spurious result.

The default broker is simulated paper accounting. Alpaca support is paper-only
and partial: it does not yet provide a complete streaming market-data and order-
lifecycle integration. No current surface is authorized for live-money trading.

## Surface-by-surface validity

### Dashboard

The dashboard shows the marked paper book, current targets, risk headroom, and
positions. Cash and position values come from the owner process. Their accounting
is deterministic, but their economic value is only as current and real as the
displayed market-data provenance.

Paper deployment is a human-facing action. The TUI requires an explicit
confirmation; agents attached through the owner proxy can only inspect state and
request a dry preview.

### Recommend

Recommend estimates moments, builds the configured objective, and runs the
operational catalog method under the mandate constraints. Its diagnostics are
the actual estimator and optimizer outputs for that snapshot.

The returned weights are a research allocation, not a forecast. Synthetic input
produces a methodological fixture. Historical input produces a historical
result with the usual estimation and sample-size uncertainty.

There is no staged QAOA comparison on this surface.

### Atlas and autonomous runs

Atlas starts governed research by itself and the owner drives a coordinator
through its phases, so runs now appear without a human having pressed start.
This changes who *initiates* work and nothing about what work may do:

- Atlas's own tools are read-only in every mode. It holds no tool that builds a
  plan, approves one, or books a fill.
- `research` mode — the default — cannot start a plan-creating template at all.
  Reaching a paper plan needs `propose` mode, and reaching a fill additionally
  needs explicit human confirmation from the TUI.
- An autonomously started run produces exactly the artifacts a human-started run
  produces, under the same referee gate and the same `targets_hash` binding.

Read an autonomous run's conclusions the way you would read any other run's:
the coordinator's reasoning is judgment, the referee verdict is a deterministic
check, and neither is evidence about future returns.

### Autopilot

One iteration analyzes the regime, solves the operational champion, records a
challenger view and referee result, builds a mandate-checked plan, and either
previews or books paper legs. Plans are persisted and leg-idempotent so an
interrupted paper rebalance can resume without duplicating completed legs.

These controls make the paper workflow auditable; they do not turn its allocation
policy into a validated source of alpha.

### Experiment

The experiment matrix runs point-in-time walk-forward arms, carries cash between
rebalances, applies configured costs, and stores metrics and provenance. Deflated
Sharpe uses cumulative research-trial counts, and bootstrap intervals expose
sampling uncertainty.

The quick UI experiment is a workflow smoke test. Scientific conclusions should
use the full specification, inspect intervals and failure modes, and preserve the
current honest result: tested benchmarks still beat MVSK out of sample.

### Algorithms

The catalog separates method availability from authority:

| Stage | Meaning |
|---|---|
| `operational` | May run through its declared staged tool |
| `research` | May be evaluated in controlled experiments but is not directly agent-runnable |
| `offline` | Retained outside the desk with no CLI, HTTP, TUI, default-ablation, or MCP execution path |

Offline QAOA and Ising-construction work is retained for isolated research. Its
parameterized construction reports are not hardware-fit claims, and no IBM
hardware adapter is implemented.

### Registry

The registry is the provenance and audit surface for runs, decisions,
reflections, paper plans, and fills. Empty reflections normally mean their
evaluation horizon has not resolved yet; they should not be read as successful
outcomes.

### About and governance

Agent roles are generated from the neutral definitions in `agents/`, and the
mandate is loaded from `mandate.yaml`. Tool allowlists constrain each role. The
server also enforces algorithm stages and execution boundaries, so authority is
not prompt-only.

## Claims the current UI supports

| Claim | Supported? |
|---|---|
| The paper book and mandate checks follow the encoded deterministic rules | Yes, with regression coverage |
| A displayed run used a specific data snapshot and configuration | Yes, through persisted provenance |
| An operational method is available to staged agents | Yes, when the catalog declares its tool |
| Offline QAOA is a deployed trading capability | No |
| A constructed Ising dimension proves quantum hardware applicability | No |
| MVSK beats the benchmarks | No; the current tested result is the opposite |
| Quantum-inspired feature augmentation improves the vol forecast | No; measured to hurt across twelve samples |
| An autonomous Atlas run can reach a fill without a human | No; confirmation is required and unchanged |
| Historical or synthetic performance predicts live returns | No |

The short version: trust the UI as an auditable paper-research workstation, read
every market number with its provenance, and treat the algorithm catalog—not an
implementation's mere presence—as the deployment boundary.
