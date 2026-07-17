# Seven-Asset Decision and R0 Handoff Audit

**Archived:** 2026-07-17  
**Purpose:** Preserve the preceding Codex response before implementation resumed.

## The short answer

We did **not** decide that the application may only know about seven assets.
The repository defines two different universes for two different problems:

- a **19-instrument candidate universe** for binary asset selection; and
- a **seven-instrument cross-asset core** for continuous portfolio weighting,
  paper trading, and the first trustworthy end-to-end operating loop.

The current core is:

| Ticker | Exposure | Role in the core |
|---|---|---|
| `ACWI` | Global public equity | Broad global growth beta |
| `BNDW` | Global investment-grade bonds | Duration and defensive ballast |
| `GSG` | Broad commodities | Inflation and real-asset exposure |
| `IGF` | Global listed infrastructure | Defensive real assets and regulated cash flows |
| `GLD` | Gold | Crisis, monetary, and inflation diversifier |
| `VNQ` | US listed real estate | Rate-sensitive real assets and income |
| `EMB` | USD emerging-market sovereign debt | Credit, spread, and emerging-market exposure |

This is encoded in `configs/universe.yaml` and enforced for paper execution in
`mandate.yaml`.

## Why seven, specifically

### 1. Seven distinct economic risk sleeves

The design is deliberately cross-asset. A basket of sector equity ETFs would
mostly reproduce the same equity factor with different labels. The chosen core
contains growth, duration, inflation, commodities, real estate, infrastructure,
gold, and emerging-market credit exposures. Those relationships can change sign
across regimes, which makes correlation and tail-risk modelling meaningful.

### 2. Higher moments become expensive quickly

The primary objective estimates covariance, co-skewness, and co-kurtosis. Their
raw tensor sizes grow as `n²`, `n³`, and `n⁴`:

| Assets | Covariance | Co-skewness | Co-kurtosis |
|---:|---:|---:|---:|
| 7 | 49 | 343 | 2,401 |
| 19 | 361 | 6,859 | 130,321 |
| 50 | 2,500 | 125,000 | 6,250,000 |

Seven is large enough to express a real cross-asset allocation problem but small
enough that higher-moment estimates, optimizer comparisons, and paper-trading
results can be inspected and falsified. More assets without much more history
would primarily add estimation error.

### 3. The objective claim and solver claim are intentionally separated

The research plan makes two different claims:

- At **seven assets**, test whether higher-moment portfolio construction adds
  value relative to recognizable classical benchmarks.
- At **15–19 candidates**, test the binary asset-selection solver where the
  combinatorial search is actually meaningful.

At seven assets, classical multistart may tie the specialized optimizer. That is
acceptable and should be reported honestly. Asset selection is where cardinality
belongs; continuous weighting should not be made artificially binary just to
manufacture a harder problem.

### 4. It matches the quantum architecture honestly

The continuous MVSK problem maps to seven continuous variables on the Dirac-3
path. The 19-candidate selection problem maps naturally to a 19-variable QUBO
that can also be exact-checked at this size. Those are two legitimate but
different jobs.

### 5. It is operationally sensible for the initial $10,000 paper book

Seven holdings are easy to inspect, reconcile, and explain. Fractional notional
orders make the optimized weights executable without allowing share-price
granularity to dominate the result. Expanding the mandate before idempotency,
reflection, data quality, and execution auditability are correct would multiply
failure modes rather than improve the system.

## The 19-instrument candidate universe

The wider research pool is:

`ACWI, SPY, EFA, EEM, IWM, BNDW, AGG, BNDX, TLT, EMB, HYG, TIP, GLD, GSG, DBC, VNQ, RWO, IGF, USO`

It spans:

- global, US, developed ex-US, emerging-market, and small-cap equities;
- global, US, international, long-duration, inflation-linked, high-yield, and
  emerging-market bonds;
- gold, broad commodities, and oil;
- US/global real estate; and
- global infrastructure.

The selection layer should be allowed to rediscover or challenge the core. The
seven-asset core is a controlled starting portfolio, not a permanent ceiling.

## Are these all ETFs?

They are exchange-traded portfolio instruments, but legal structure matters.
Most are ETFs; `GLD` and `GSG` use commodity-trust/pool structures rather than
the same Investment Company Act structure as a conventional stock or bond ETF.
The future instrument master should therefore store `instrument_type`, legal
structure, issuer, expense ratio, liquidity, distribution behavior, tax notes,
underlying exposure, and data entitlements instead of treating every ticker as
identical.

## “Making our own ETFs” means three different things

These should not be conflated:

1. **Model portfolio / synthetic ETF** — the application produces a transparent,
   rules-based basket and rebalances a paper or separately managed account. This
   is the right near-term product.
2. **Custom index** — publish methodology, constituents, rebalance rules, and a
   benchmark history. This can become licensable intellectual property but
   needs governance and calculation controls.
3. **Registered exchange-traded fund** — a legal fund with sponsor, board,
   custodian, administrator, authorized participants, market makers, filings,
   compliance, tax, distribution, and exchange-listing obligations. This is a
   separate regulated business, not a software feature.

The application should first become an **ETF portfolio and index laboratory**:
construct baskets, explain exposures, simulate rebalances, estimate costs,
compare against benchmarks, and publish an auditable methodology. That creates
the research and operational evidence needed before considering a fund wrapper.

## What FinceptTerminal contributes

Fincept is most valuable as a reference for financial breadth and screen
taxonomy, not as code to copy or as the final visual style. Useful ideas are:

- persistent instrument context across research, portfolio, risk, and execution;
- global areas with a smaller local navigation level;
- market, news, fundamentals, portfolio, backtest, workflow, and agent services
  behind one workstation;
- a boundary between UI code and Python analytics using structured messages;
- workflow inspection for triggers, data, calculations, agents, gates, and
  execution; and
- local and hosted model support through tools rather than agents manipulating
  the screen.

For this project, those ideas should be combined with a quieter, border-light
terminal interface, a shared typed command layer, explicit agent permissions,
and deterministic risk/execution gates.

## Financial data and intelligence direction

The product needs a data spine before it needs a large catalog of screens:

- canonical instrument identities and corporate-action-aware price history;
- provider-neutral market, macro, filings, news, portfolio, order, and event
  schemas;
- ingestion timestamps, source timestamps, data age, licensing, lineage,
  revisions, and quality flags on every record;
- raw news storage, URL/content deduplication, event clustering, entity/ticker
  resolution, and structured LLM extraction;
- a strict separation between source facts, extracted events, agent
  interpretation, proposed portfolio views, risk checks, and executable orders;
- point-in-time replay so any signal can be evaluated using only information
  available at that moment; and
- read-only MCP tools for market/news research, with proposal and execution
  remaining separate typed operations.

News should not flow directly from prose to a trade. The safe path is:

```text
raw source -> normalized document -> deduplicated event cluster
           -> structured facts with citations and timestamps
           -> bounded risk/regime view -> deterministic validation
           -> research experiment -> proposal -> risk gate -> paper execution
```

## R0 handoff: what Claude completed and what remained

The API/model usage limit stopped Claude's session; it was not evidence of a
market-data API failure in the application.

At the handoff, repository code and commits showed Tasks 1–6 implemented:

| R0 task | State at handoff |
|---|---|
| Merge/promote application | Complete |
| R0.1 objective scaling | Complete |
| R0.2 canonical polynomial/compiler parity | Complete |
| R0.2 measured QUBO/Ising resources | Complete |
| R0.3 deflated Sharpe and bootstrap intervals | Complete |
| R0.4 referee/reconcile gate | Complete |
| R0-T7 leg-level idempotency | **Next; reproduced defect** |
| R0-T8 challenger in autopilot | Open |
| R0-T9 reflection closure | Open |
| R0-T10 events API/tool emission | Partial: read API present, emission open |
| R0-T11 data-driven co-moment shrinkage | Open |
| R0-T12 exit gate | Open |

The complete existing suite passed at the audit point, but a focused replay
probe exposed the T7 defect: replaying a checked plan could apply all seven
fills again, drive cash from `$0` to `-$10,000`, change positions, and create
orders without their `plan_id`. That is why T7 is the first continuation task.

## Recommended order of work

1. Finish R0 trust repair and its exit gate.
2. Build the canonical data/instrument foundation with source health visible in
   the TUI.
3. Add evidence-linked news/event intelligence in read-only mode.
4. Add point-in-time signal research, calibration, and promotion gates.
5. Only then add a continuously running streaming owner and bounded live
   operations.

The interface should remain the face of those contracts: quiet, keyboard-first,
no decorative top banner, and always explicit about data age, mode, authority,
risk, agent state, and pending actions.
