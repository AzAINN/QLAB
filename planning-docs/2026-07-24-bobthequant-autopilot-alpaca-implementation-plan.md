# BobTheQuant + Alpaca-Required Operations — Implementation Plan

**Status:** PROPOSAL / IMPLEMENTATION HANDOFF
**Date:** 2026-07-24
**Scope:** Operational market data, Alpaca paper execution, persistent desk
manager, autonomous research/proposal orchestration, and TUI integration.
**Non-goal:** This plan does not add live-money trading.
**Baseline first inspected:** local `main` at `65971f7`; the moving checkout
had advanced through `aded939` by the final validation pass. Re-audit the actual
HEAD before implementing.

---

## 0. Handoff contract: read this before changing code

This document is designed to be handed to a coding agent after the current
coding leg finishes. The next agent must not treat the baseline commit or file
line numbers as immutable.

Before implementation:

1. Read `README.md` and `AGENTS.md` completely.
2. Run `git status --short --branch` and `git log --oneline -20`.
3. Do not overwrite or fold in unrelated dirty changes.
4. Read:
   - `planning-docs/2026-07-24-governance-review-findings.md`
   - `planning-docs/2026-07-24-product-roadmap-v2.md`
   - `planning-docs/2026-07-24-news-signal-plan.md`
5. Determine which governance findings have already been fixed by the current
   coding leg. Reproduce or test every unresolved critical finding.
6. Run the full offline suite and record the baseline result.
7. Implement this plan in independent, reviewable slices. Do not combine the
   data-plane, execution, Bob runtime, and TUI work into one commit.
8. Restart the owner process after every change to code it serves.

The worktree observed while writing this plan initially contained in-flight
changes in news calibration, the registry, mandate, trader plan, and tests.
Several governance/news commits then landed while planning continued. Those
changes belong to another coding leg and must be accepted or challenged on
their tests, not overwritten. This plan intentionally changes no product code.

---

## 1. Product outcome

The desired product is:

> BobTheQuant is always at the qlab desk. It monitors verified Alpaca data,
> understands the portfolio and regime, starts governed research when evidence
> warrants it, coordinates specialist agents, interprets their persisted
> results, and prepares a checked paper action. It asks the human before the
> action is submitted.

This is not a request for an unrestricted trading LLM. It is a request for an
autonomous desk manager operating above qlab's existing deterministic
controls.

The distinction is load-bearing:

- Bob decides **what deserves investigation**, **which governed workflow to
  start**, and **how to explain the result**.
- Algorithms calculate estimates, targets, backtests, metrics, and costs.
- The registry and deterministic services enforce dependencies, the mandate,
  target binding, data eligibility, reconciliation, cost gates, approvals, and
  execution idempotency.
- The human approves or rejects each paper plan in the first release.

Autonomy should remove repetitive human coordination. It must not remove
evidence, control boundaries, or accountability.

---

## 2. Settled decisions

These are implementation decisions, not open brainstorming items.

### 2.1 Operational market data

- Alpaca is the required provider for the operator/paper path.
- Operational mode has no synthetic fallback.
- A matching, fresh Alpaca cache may be used according to an explicit
  freshness policy; it remains Alpaca data and retains its provenance.
- Synthetic data stays in the repository for offline tests, deterministic
  demos, and explicitly offline research.
- A synthetic snapshot is never eligible to produce an executable paper plan.
- IEX versus SIP is explicit configuration and is always displayed. The
  runtime must not label either one merely as `live`.

### 2.2 Execution

- Paper trading remains enforced in code.
- Alpaca paper is an explicit broker selection, not inferred from the presence
  of credentials.
- A requested Alpaca broker never silently becomes the simulator.
- `run-once`, owner HTTP, headless MCP, `watch`, and Bob all stop at a proposal
  unless a separate human-approval path authorizes the exact checked plan.
- The first release requires per-plan human approval.
- Standing paper authorization is a later phase with a separate, expiring
  authority record. It is not represented by a boolean.
- Live-money trading is out of scope.

### 2.3 BobTheQuant

- Display name: `BobTheQuant`.
- Stable role id: `bob-the-quant`.
- Internal runtime component name: `DeskManager` or `BobSupervisor`, avoiding
  ambiguous imports such as `qlab.bob`.
- Bob is owner-managed, not owned by a particular TUI view.
- Bob starts asynchronously after the owner is healthy.
- Bob is event-driven. It is visibly present at all times, but the LLM is not
  continuously running.
- Bob uses explicit workflow templates and budgets. It cannot invent arbitrary
  execution paths.
- Bob can observe, autonomously research, and prepare proposals. Bob receives
  no execution tool.
- The prompt/role source belongs under `agents/` and is synchronized through
  `python -m qlab.agents.loader sync`.

The repository already uses `.bob/personas/` for IBM Bob adapters. Product
persona `BobTheQuant` must not be confused with that adapter format. The
internal `DeskManager` name and the stable `bob-the-quant` role id make the
distinction explicit.

### 2.4 TUI

- Keep Textual for the terminal application.
- Do not embed JavaScript in the TUI.
- Use the existing owner APIs/event stream so the web client can later build a
  richer JavaScript surface without duplicating business logic.
- Preserve the quiet, no-top-header workstation.
- Bob occupies the persistent right rail on every screen.
- `Ctrl+B` focuses Bob from anywhere.
- The existing Workforce view remains the detailed delegated-work surface.
- Approval is deliberate and inspectable; there is no one-key unconfirmed
  execution.

### 2.5 Universe

- Real Alpaca data and a larger trading universe are separate changes.
- Keep the current cross-asset ETF core as the first operational universe so
  governance, estimation, order lifecycle, and Bob behavior can be validated
  without also changing the statistical problem.
- Subscribe to the core, configured benchmarks, current holdings, and pending
  order symbols. Bob may monitor a broader research watchlist without making
  it paper-eligible.
- The existing candidate ETF and stock pools remain research-only unless a
  separate universe-promotion review accepts history length, liquidity,
  spreads, corporate-action handling, concentration, mandate metadata, and
  out-of-sample evidence.
- Bob may recommend a universe change and open a research task. It may not edit
  the operational universe or promote an asset.

This sequencing is not an argument that seven ETFs are permanently optimal.
It prevents a data-provider migration, autonomy rollout, execution rewrite,
and universe expansion from becoming one untestable change. Once the initial
Alpaca/Bob phases are stable, universe promotion can proceed asset class by
asset class with evidence and explicit paper eligibility.

---

## 3. Requirements

### 3.1 Functional requirements

#### Data

1. Fetch adjusted historical daily bars from Alpaca.
2. Receive current stock quotes/trades through Alpaca's market-data stream.
3. Expose provider, feed, timestamps, freshness, missing symbols, and health.
4. Refuse operational research/proposals when required data is absent, stale,
   cross-provider, malformed, or synthetic.
5. Keep point-in-time snapshots and content-addressed provenance.
6. Preserve explicit offline synthetic fixtures for the default test suite.

#### Bob

1. Start with the owner runtime without blocking owner startup.
2. Recover durable state after restart.
3. Produce a startup/desk brief.
4. Monitor data, broker, portfolio, regime, drift, drawdown, workflows,
   research runs, and approvals.
5. Wake on deterministic events and schedules.
6. Select an allowed workflow template.
7. Start/resume the existing governed workforce.
8. Track dependencies and bounded retries.
9. Synthesize conclusions from persisted artifacts, not unverified agent prose.
10. Create a checked rebalance preview only after the existing gates.
11. Create an expiring approval request.
12. Ask the human to approve, reject, inspect, or ask a follow-up.
13. Explain why it acted, why it did not act, and what evidence it used.
14. Support pause/resume and authority-mode changes.

#### Execution

1. Make paper-broker selection explicit.
2. Revalidate the book, mandate, data, costs, referee PASS, and approval at
   submission time.
3. Submit the exact persisted plan only.
4. Use marketable-limit orders if Alpaca capabilities and asset eligibility
   permit them.
5. Refuse rather than silently downgrade order type or venue.
6. Track accepted, partial, filled, rejected, canceled, and expired states.
7. Reconcile broker truth before declaring a plan complete.
8. Persist slippage, actual fills, fees, target error, and broker identifiers.

#### TUI

1. Show Bob's state and current assessment on all screens.
2. Show Alpaca provider/feed and data age at the point of action.
3. Show pending approval count and degraded/blocked state.
4. Provide a global Bob input.
5. Provide a detailed task/evidence drawer.
6. Provide a deliberate approval surface with plan changes, costs, controls,
   provenance, expiry, and exact target binding.
7. Keep existing navigation, linked ticker context, Workforce, Book, and Audit.

### 3.2 Non-functional requirements

- **Safety:** fail closed on unknown, stale, malformed, or unavailable state.
- **Auditability:** every wake, task, workflow, conclusion, proposal, approval,
  invalidation, order update, and refusal is persisted or emitted as a
  durable audit event.
- **Determinism at gates:** no prompt decides whether hard constraints pass.
- **Availability:** Bob failure must not take down the owner, TUI, registry, or
  data display.
- **Bounded cost:** no continuous LLM loop; enforce daily/workflow/turn budgets,
  cooldowns, and one corrected retry per failed phase.
- **Responsiveness:** TUI rendering must not wait on Bob, research, Alpaca REST,
  or an order stream.
- **Portability:** preserve Windows launcher behavior and the short command-line
  session-agent design.
- **Testability:** the full default test suite remains network-free.
- **Single writer:** only the owner process opens the real DuckDB registry.
- **Secrets:** keys never appear in registry payloads, events, model prompts,
  logs, snapshots, or TUI copy.

### 3.3 Non-goals

- Live-money execution.
- Intraday alpha generation.
- Letting news directly create expected returns or trades.
- Letting Bob choose an arbitrary security outside the mandate.
- Replacing HRP merely because Bob recommends a novel method.
- Distributing this small desk over Kafka, Redis, Celery, or multiple database
  writers.
- Rewriting Textual in JavaScript.
- Persisting every market tick in DuckDB.

---

## 4. Invariants and authority matrix

The implementation is incomplete if any of these invariants can be bypassed
from another surface.

1. Only the owner opens `.lab/registry.duckdb`.
2. Operational snapshots must be Alpaca-sourced, provider-consistent, and
   fresh enough for their purpose.
3. Synthetic data may run tests/research but cannot produce an execution-
   eligible plan.
4. AI roles never receive a raw-order or execute-plan tool.
5. Research/offline algorithms remain non-operational until separately
   promoted.
6. Optimizer targets, referee PASS, checked plan, approval, and submitted plan
   refer to the same canonical target hash and persisted plan digest.
7. Approval is granted to one exact plan version and expires.
8. Execution revalidates current broker/account/book state; preview-time checks
   alone are insufficient.
9. Reconciliation and data health must be clean at submission.
10. A cost-gate refusal is terminal for that plan version.
11. Repeating submission or an event delivery must never duplicate an order.
12. Missing credentials, missing packages, feed errors, unsupported assets,
    malformed limits, and unknown order states refuse loudly.
13. `paper=True` is not configurable through Bob, the TUI, prompts, or normal
    configuration.
14. No direct CLI or generic HTTP body may impersonate human approval with
    `{"human_confirmed": true}`.

### Authority matrix

| Actor | Observe | Start research | Write judgment | Solve operational policy | Create checked preview | Approve | Execute |
|---|---:|---:|---:|---:|---:|---:|---:|
| Human/TUI | yes | yes | no | no | request | yes | request through owner |
| BobTheQuant | yes | yes | manager conclusion only | no | request | no | no |
| Data-QA / signal-QA | scoped | no | advisory evidence | no | no | no | no |
| Analyst/challenger | scoped | no | scoped decision/challenge | no | no | no | no |
| Optimizer | scoped | no | solver artifacts | yes | no | no | no |
| Referee | scoped | no | PASS/FAIL | no | no | no | no |
| Reporter | scoped | no | recommendation | no | request | no | no |
| Deterministic owner services | yes | schedule | audit records | invoke allowed algorithm | yes | validate | yes, after approval |
| Broker gateway | account/quotes | no | broker events | no | no | no | submit exact legs |

---

## 5. High-level architecture

```text
                         Textual TUI
                     Web client (later)
                             |
                  HTTP + server-sent events
                             |
┌──────────────────────── OWNER RUNTIME ──────────────────────────┐
│                                                                │
│  Owner API / serialized dispatch                               │
│      |                                                         │
│      +-- Registry + paper book (only DuckDB writer)             │
│      +-- MarketDataService                                     │
│      │     +-- Alpaca historical bars                          │
│      │     +-- Alpaca quote/trade stream                        │
│      │     +-- data-health / snapshot permits                   │
│      +-- PaperBrokerService                                    │
│      │     +-- Alpaca paper REST                               │
│      │     +-- Alpaca trade/order update stream                 │
│      +-- Workflow service / research tools                     │
│      +-- Plan, mandate, referee, cost, approval services        │
│      +-- Owner event bus                                       │
│      +-- BobSupervisor                                         │
│            +-- deterministic trigger evaluator                 │
│            +-- task queue / budgets / cooldowns                │
│            +-- coordinator backend (Claude first)              │
│            +-- propose-only owner MCP proxy                    │
│                                                                │
└────────────────────────────────────────────────────────────────┘
                             |
                session-local worker processes
         analyst / challenger / optimizer / referee / reporter
                             |
                  owner-backed proxy only
```

### Why Bob belongs under the owner

The current TUI owns `ClaudeSession`; unmounting the TUI stops it. That is
correct for an interactive chat turn but wrong for a desk manager that must:

- remain present across views;
- continue while a client reconnects;
- react to owner events;
- recover durable work;
- serve both TUI and web clients;
- never open a second registry.

Bob should be owner-lifetime persistent. In v1, closing an owner process stops
Bob; restarting qlab starts Bob again and resumes durable state. A platform
service/daemon that survives all clients is a later operational concern.

### Threading and the one-writer rule

The owner currently uses a `ThreadingHTTPServer` plus a global dispatch lock.
Background Alpaca and Bob threads must not write DuckDB directly.

Introduce an owner command/dispatch seam:

```text
background producer
    -> immutable event/command
    -> owner dispatch queue
    -> serialized owner handler
    -> Registry write
```

Acceptable first implementation:

- background streams keep only in-memory latest values;
- they enqueue durable events or tasks;
- owner-side code drains the queue under the same serialization discipline as
  HTTP dispatch.

Do not pass a Registry object to a stream callback, Bob callback, or agent
process.

---

## 6. Operational data plane

### 6.1 Replace boolean `offline` with an explicit policy internally

The current `offline: bool` cannot express the required distinctions:

- Alpaca required, no fallback;
- matching Alpaca cache accepted if fresh;
- cached historical backtest;
- deterministic demo;
- test fixture.

Introduce an internal immutable `DataPolicy`:

```python
@dataclass(frozen=True)
class DataPolicy:
    mode: Literal["operational", "historical", "demo", "test"]
    provider: Literal["alpaca", "yfinance", "synthetic"]
    feed: Literal["iex", "sip", "delayed_sip"] | None
    allow_network: bool
    allow_cache: bool
    allow_synthetic: bool
    require_fresh: bool
    execution_eligible: bool
```

Required constructors:

```text
DataPolicy.alpaca_operational(feed)
DataPolicy.alpaca_historical(feed)
DataPolicy.demo(seed)
DataPolicy.test(seed)
```

Keep existing `offline` CLI compatibility initially, translating it at the
boundary. New owner services and gates consume `DataPolicy`, not a naked
boolean.

### 6.2 Provider behavior

For `alpaca_operational`:

1. Validate package, credentials, feed selection, and account/data
   connectivity at startup.
2. Read only a cache written by the selected Alpaca provider/feed.
3. Evaluate cache freshness against the exchange calendar and intended use.
4. Fetch Alpaca if refresh is required.
5. If fetch fails:
   - return a fresh matching cache only if policy permits it;
   - otherwise raise a typed `DataUnavailable`;
   - never return yfinance;
   - never return synthetic.
6. Preserve source/feed/retrieval timestamps across normalization and caching.

For `demo`/`test`:

- synthetic is explicit and prominently labeled;
- `execution_eligible` is always false.

### 6.3 Provenance object and data permit

Replace execution decisions based only on `snapshot.source` with a structured,
content-addressed permit:

```json
{
  "permit_id": "sha256-prefix",
  "snapshot_id": "sha256-prefix",
  "purpose": "paper_proposal",
  "provider": "alpaca",
  "feed": "sip",
  "as_of": "2026-07-24",
  "retrieved_at": "2026-07-24T20:15:03Z",
  "last_completed_bar": "2026-07-23T20:00:00Z",
  "quote_as_of": "2026-07-24T20:15:02Z",
  "quote_age_seconds": 1.2,
  "bar_age_sessions": 0,
  "universe": ["ACWI", "BNDW", "GSG", "IGF", "GLD", "VNQ", "EMB"],
  "missing_tickers": [],
  "integrity_verdict": "PASS",
  "eligible_for_research": true,
  "eligible_for_paper_proposal": true,
  "eligible_for_execution": true
}
```

The permit is produced deterministically after data-QA. Plans store its id.
Execution rechecks current quote/broker freshness and verifies the referenced
snapshot remains an eligible Alpaca snapshot.

### 6.4 Freshness

Freshness must be session-aware, not `today - last_bar` alone.

- Before the market opens, the prior completed trading session is current.
- During the session, the daily allocation policy still uses the latest
  completed daily bar.
- After close, allow a configurable finalization grace period before requiring
  that session's bar.
- Weekends and exchange holidays do not make a prior-session bar stale.
- Current quotes have a separate seconds-level threshold used for display and
  execution.

The scheduler's limited hard-coded calendar should be replaced or extended
before relying on it beyond its supported range. Prefer one exchange-calendar
service shared by data freshness, Bob schedules, and order-session rules.

### 6.5 Alpaca streams

Add two distinct supervisors:

1. `MarketStreamSupervisor`
   - quotes/trades/minute bars for the active universe;
   - bounded exponential reconnect with jitter;
   - latest-value cache;
   - feed and connection health;
   - no registry writes from callbacks;
   - publish throttled owner events.

2. `TradeUpdateSupervisor`
   - accepted, partial fill, fill, cancel, reject, expire;
   - maps Alpaca order/client ids to persisted plan legs;
   - queues owner commands for state transitions;
   - reconnects and performs REST recovery after a gap.

Do not persist every quote. Persist:

- connection transitions;
- stale/recovered transitions;
- data permits;
- quotes used to price an order;
- material alert events.

### 6.6 IEX/SIP clarity

Require an explicit feed setting. The operator surface must display it:

```text
ALPACA · IEX
ALPACA · SIP
ALPACA · DELAYED SIP
```

IEX is not equivalent to consolidated SIP coverage. The difference affects
quotes, volumes, and marketability assumptions. No code or UI should collapse
them into the word `live`. Default a new paper setup to IEX because it is the
widely available Alpaca feed; use SIP only when the account is entitled to it.
Startup must verify the configured entitlement and block with a clear message
instead of silently changing feeds.

---

## 7. Paper execution plane

This phase depends on the governance review's critical execution findings being
fixed. Re-test every entry point after the refactor.

### 7.1 Explicit broker configuration

Introduce:

```text
QLAB_BROKER=simulated|alpaca_paper
```

Rules:

- `alpaca_paper` requires package, paper credentials, connectivity, and account
  validation.
- Any failure raises a typed error and marks execution unavailable.
- `simulated` is valid only when explicitly configured or in test/demo mode.
- Operator mode does not infer venue from credentials.
- Status and every plan show the broker identity.

### 7.2 Close all unconfirmed execution paths

Required changes:

- `qlab run-once` defaults to proposal-only.
- Remove or replace `--dry-run` inversion with an explicit
  `--execute-paper` path that still consumes a valid approval.
- `qlab watch` and scheduled autopilot never execute directly.
- `POST /api/run_once` defaults `execute` to false and ignores/rejects direct
  execution without an approval id.
- Headless MCP exposes no execution tool.
- Owner-backed agent proxy exposes no execution tool.
- The web client cannot post a generic `execute:true`.
- Replace `human_confirmed: true` with a persisted approval record.

### 7.3 Approval binding

An approval is valid only for:

```text
approval_id
plan_id
plan_digest
decision_id
targets_hash
data_permit_id
broker
expected_cost_version
book_revision
expires_at
```

Changing any bound input invalidates approval.

The approval route must not be registered as an MCP/agent tool. The TUI obtains
an owner-issued challenge nonce when it opens the approval panel and returns it
after deliberate confirmation. The owner creates the durable approval record.

Do not rely on a caller-supplied boolean to prove a human acted.

### 7.4 Execution-time revalidation

Immediately before submitting the first leg:

1. Load the persisted checked plan by id.
2. Reconstruct it only from persisted content.
3. Verify its digest and complete leg count.
4. Verify the latest PASS covers exact targets.
5. Verify approval is pending-approved, unexpired, unconsumed, and bound to the
   same plan/digest/hash.
6. Fetch current Alpaca paper account and positions.
7. Reconcile external broker truth with the registry.
8. Verify the book revision or regenerate/reapprove the plan.
9. Re-run mandate, drawdown, gross exposure, turnover, order count, and
   liquidation rules.
10. Re-run the cost gate using current quotes/spreads.
11. Verify data permit and quote freshness.
12. Atomically consume approval and move plan to `submitting`.
13. Submit idempotent legs.

If the current book differs from preview, do not "adjust" the plan silently.
Invalidate it and let Bob explain that a new preview and approval are required.

### 7.5 Order type

Implement actual marketable-limit behavior rather than recording
`marketable_limit` while submitting `MarketOrderRequest`.

Proposed pricing:

```text
buy limit  = current ask × (1 + configured buffer)
sell limit = current bid × (1 - configured buffer)
```

Deterministic checks:

- quote provider/feed and age;
- finite positive bid/ask;
- non-crossed market;
- maximum spread;
- maximum configured price buffer;
- asset is tradable and fractionable when needed;
- regular/extended-hours rules;
- notional/quantity and order-type support.

Alpaca's current documentation reports fractional and notional limit-order
support, but documentation surfaces have historically disagreed. Add a startup
capability check and a paper integration test. If an asset/account/API
combination does not support the required order, refuse. Do not silently submit
a market order.

Notional orders may not support replacement. Use cancel-confirm-resubmit with a
new versioned client id when replacement is unavailable.

### 7.6 Order state machine

Use broker truth:

```text
checked
  -> approved
  -> submitting
  -> submitted
  -> partially_filled
  -> filled
  -> reconciled

submitted -> rejected
submitted -> canceled
submitted -> expired
partially_filled -> canceled
```

Do not mark `filled` or `reconciled` immediately after REST acceptance.

The stable idempotency key should include:

```text
plan_id | plan_version | ticker | leg_version
```

REST recovery by client order id is mandatory after timeout or stream
disconnect. Never assume a timeout means the broker did not accept an order.

---

## 8. BobTheQuant control plane

### 8.1 Bob is a supervisor plus an interpreting agent

Split responsibilities:

#### Deterministic `BobSupervisor`

- lifecycle;
- event subscription;
- scheduling;
- deduplication;
- trigger evaluation;
- cooldowns;
- budgets;
- task persistence;
- coordinator process start/stop/resume;
- timeout/retry;
- status projection;
- approval expiry;
- health and blocked states.

#### Interpreting `bob-the-quant` agent

- reads owner facts and persisted evidence;
- chooses among allowed workflow templates;
- provides the reason for selection;
- coordinates specialist agents through the existing dispatcher;
- synthesizes results;
- requests a checked preview;
- produces an operator brief;
- never executes.

This split means basic health monitoring and "nothing changed" operation do not
need an LLM call.

### 8.2 Orchestrator backend

Refactor the current TUI-bound `ClaudeSession` into a UI-agnostic process
runner:

```python
class CoordinatorBackend(Protocol):
    def available(self) -> bool: ...
    def start(self, prompt, *, resume_session=None) -> RunHandle: ...
    def stop(self, run_id) -> None: ...
    def status(self, run_id) -> CoordinatorStatus: ...
```

First backend: existing Claude CLI behavior.

Preserve:

- executable resolution;
- Windows `.cmd`/`.bat` wrapper;
- UTF-8 pipe decoding;
- temporary isolated project;
- short session-local agent files;
- disabled built-in/background agents;
- wall-clock and silence watchdogs;
- streaming event parser;
- durable workflow resume.

Later backends may use IBM Bob or another orchestrator without changing Bob's
authority or owner contracts.

### 8.3 Bob modes

```text
observe
research
propose
paused
```

#### Observe

- health and portfolio monitoring;
- briefs and alerts;
- no autonomous workforce launch.

#### Research

- all Observe behavior;
- may start approved workflow templates;
- may not create a paper plan.

#### Propose

- all Research behavior;
- may request a checked preview after PASS;
- creates an approval request;
- cannot approve or execute.

#### Paused

- no new autonomous tasks;
- data/broker monitoring and approval expiry continue;
- active execution/order monitoring continues;
- user questions remain available.

Recommended rollout default is `observe`. Once Alpaca and Bob shadow behavior
are validated, the user explicitly selects and persists `propose`.

### 8.4 Bob state machine

```text
starting
  -> observing
  -> investigating
  -> coordinating
  -> synthesizing
  -> awaiting_approval
  -> observing

any nonterminal state -> blocked
any nonterminal state -> paused
coordinator failure   -> degraded
```

State definitions:

- `starting`: owner dependencies are being checked.
- `observing`: healthy, idle, listening for deterministic triggers.
- `investigating`: assembling facts and selecting a workflow.
- `coordinating`: a workforce task is active.
- `synthesizing`: all required artifacts exist; Bob is preparing the brief.
- `awaiting_approval`: checked proposal exists and is valid.
- `blocked`: deterministic prerequisite failed; no blind retry.
- `degraded`: Bob/model unavailable, while owner/data/book may remain usable.
- `paused`: human suspended autonomous work.

### 8.5 Trigger catalog

| Trigger | Deterministic precondition | Default action |
|---|---|---|
| Owner startup | owner + data + broker health evaluated | desk brief |
| Data degraded | required Alpaca source stale/unavailable | block + alert |
| Data recovered | fresh eligible permit exists | recovery brief |
| Prior close available | completed daily bar final | daily regime/portfolio review |
| Regime flip | robust detector confirmation rules pass | regime workflow |
| Drift breach | mandate drift threshold exceeded | rebalance review |
| Drawdown warning/control | tier transition | risk-event workflow |
| Kill switch | breaker threshold | halt, risk brief, liquidation-only analysis |
| New research run | terminal and relevant | research review |
| Workflow completion | all required artifacts persisted | synthesize |
| Referee PASS | exact targets and cost gate eligible | preview if mode=`propose` |
| Referee FAIL | persisted reasons | explain, no proposal |
| Approval pending | valid plan | notify human |
| Approval expiry | no decision before expiry | invalidate |
| Order anomaly | reject/partial/stale submission | pause proposals + reconcile |
| User message | always | answer or start explicit task |

Do not wake Bob on every quote. Quote events update display and execution
health. The daily/quarterly strategy does not become an intraday strategy.

### 8.6 Dedupe, cooldowns, and budgets

Each autonomous task has a dedupe key:

```text
trigger_kind | trading_date | universe | relevant_state_hash
```

Suggested limits:

- one startup brief per owner boot;
- one normal daily review per completed trading session;
- one active standard workflow at a time;
- one corrected retry per failed phase;
- configurable regime/drift cooldown;
- maximum autonomous workflows per day;
- maximum Bob turns per task;
- maximum tool calls inherited from owner call budgets;
- no automatic loop after a second failure.

If budget is exhausted, Bob becomes `blocked` and explains what would be
required to continue.

### 8.7 Workflow templates

Bob selects only registered templates.

#### `desk_brief`

```text
data health
broker/account health
positions/cash/drawdown
current targets and drift
regime indicators
open workflows
pending approvals
```

No workforce unless an actionable inconsistency appears.

#### `regime_review`

```text
data-QA
  -> deterministic indicator panel
  -> analyst/challenger bounded debate
  -> optimizer (operational policy only)
  -> referee
  -> reporter
  -> Bob synthesis
```

#### `estimation_panel`

```text
data-QA
  -> bounded analyst variants
  -> branch optimizers
  -> walk-forward evidence
  -> judge
  -> referee
  -> Bob synthesis
```

#### `research_review`

```text
collect completed runs
  -> compare champion + benchmarks
  -> inspect DSR/CIs/costs
  -> classify reject / continue / promotion candidate
```

Classification does not promote an algorithm. Promotion remains a separate
human-reviewed catalog change.

#### `risk_event`

```text
data and broker health
  -> drawdown/correlation/regime evidence
  -> current vs defensive targets
  -> mandate and cost analysis
  -> referee
  -> urgent brief / optional checked proposal
```

#### `news_risk_review`

Research-only:

```text
provenance-tagged news
  -> quarantined extractor
  -> typed risk views
  -> corroboration and calibration
  -> research result
```

Never creates an operational target or paper proposal unless that lane is
separately promoted in the catalog in the future.

---

## 9. Research-workforce intelligence upgrades

These are the five boundary-safe improvements previously identified from the
TradingAgents comparison. They are part of this handoff, not optional ideas.
However, the checkout moved while this plan was being written and portions of
the first four appeared to land. The implementation agent must begin with an
acceptance audit and harden or complete the existing work; it must not build a
second competing implementation.

### 9.1 Reconcile the moving checkout first

At the time of the final planning pass, the checkout already appeared to
contain:

- deterministic realized return and alpha versus configured 60/40 in
  `qlab/governance/reflection.py`;
- regime-fingerprint recall in `qlab/state/registry.py`;
- five typed regime tools plus `data-qa` and `signal-qa`;
- a prompt-level bounded analyst/challenger debate;
- role-model routing in `qlab/tui/claude.py`.

Those observations are not substitutes for acceptance tests. The next agent
must classify each gap as:

```text
absent | partial | complete-but-unproven | accepted
```

For each item, record:

```text
current code path
artifact contract
authority boundary
tests that prove it
remaining failure cases
decision: keep / harden / replace
```

Only `absent` or demonstrably unsafe behavior justifies a new implementation.
For `partial`, extend the existing contract with additive migrations and
compatibility behavior.

### 9.2 Use precise language: champion policy is not a benchmark

The implementation and UI must stop using the phrase `champion benchmark`
without qualification because it collapses three different concepts:

1. **Operational champion policy** — the allocation method currently allowed
   to produce paper targets. Today that is configured HRP. It can change only
   through evidence, catalog promotion, mandate configuration, and review.
2. **Strategic benchmark** — a stable external comparison such as configured
   60/40 or equal weight. It answers whether the portfolio added value relative
   to a simple alternative.
3. **Research champion comparator** — the operational policy frozen at the
   time a research candidate is evaluated. It answers whether the candidate
   beat the method it would need to replace.

The phrases answer different questions:

| Term | Question |
|---|---|
| operational policy | What method is the paper desk allowed to use? |
| 60/40 benchmark | Did the resulting portfolio beat a simple stock/bond allocation? |
| equal-weight benchmark | Did estimation add value over 1/N? |
| frozen champion comparator | Did a candidate earn consideration for promotion? |

Do not store a field called `champion_benchmark`. Use explicit ids:

```text
operational_policy_id_at_decision
strategic_benchmark_ids
research_champion_policy_id
```

All ids are frozen with the decision or run. Never rescore old outcomes against
whatever happens to be champion today.

For an ordinary operational decision, score at least:

```text
realized_portfolio_return
realized_volatility
realized_regime_consistency
realized_return_vs_60_40
realized_return_vs_equal_weight
```

For a research candidate, additionally score:

```text
candidate_return
frozen_champion_return
candidate_alpha_vs_champion
```

If the operational target and champion counterfactual are identical, alpha
versus champion is structurally zero and should be labeled `not_applicable`,
not presented as evidence of success. A useful estimator-level counterfactual
may be scored only if its parameters and targets were computed and persisted
at decision time. Do not manufacture a hindsight counterfactual after the
outcome is known.

### 9.3 Gap 1 — outcome-scored reflections and similarity recall

#### Goal

Turn each material judgment into a closed learning loop:

```text
point-in-time judgment
  -> frozen context and comparison set
  -> realized deterministic outcome
  -> plain-language lesson
  -> retrieval for a genuinely similar future regime
```

#### Decision-time contract

Every scored decision must reference immutable context:

```text
decision_id
decision_kind
as_of
universe_id
snapshot_id
snapshot_content_hash
target_hash
operational_policy_id_at_decision
strategic_benchmark_ids
research_champion_policy_id NULL
estimator_config_hash
regime_fingerprint
regime_fingerprint_version
outcome_horizon_days
```

The point-in-time snapshot is essential. Without it, a later split correction,
universe edit, benchmark edit, or provider change could silently change the
question that the original agent answered.

#### Deterministic outcome scorer

Keep all realized numbers out of model code. A scorer:

1. selects observations strictly after `as_of`;
2. waits until the full configured horizon exists;
3. uses the same adjusted-price and missing-data semantics for the chosen
   target and every comparator;
4. applies the recorded starting weights without hindsight re-optimization;
5. applies a documented cost convention where comparison requires turnover;
6. calculates raw return, volatility, drawdown, benchmark deltas, and regime
   consistency;
7. records window start/end, provider, snapshot ids, and formula version;
8. updates exactly once, or produces the same result idempotently on retry.

Missing assets, insufficient history, invalid prices, or an unavailable frozen
benchmark must leave the decision pending or explicitly blocked. They must
never be dropped from only one side of the comparison.

Suggested realized-outcome shape:

```json
{
  "schema_version": 2,
  "horizon_days": 63,
  "window_start": "YYYY-MM-DD",
  "window_end": "YYYY-MM-DD",
  "portfolio": {
    "return": 0.0,
    "annualized_vol": 0.0,
    "max_drawdown": 0.0
  },
  "comparisons": [
    {
      "kind": "strategic_benchmark",
      "id": "sixty_forty",
      "return": 0.0,
      "alpha": 0.0
    }
  ],
  "regime": {
    "called": "calm",
    "realized": "stress",
    "consistent": false
  },
  "data": {
    "snapshot_id": "...",
    "provider": "alpaca",
    "formula_version": "outcome-v2"
  }
}
```

#### Similarity retrieval

Recency is only a tie-breaker. Retrieval must be deterministic and
point-in-time safe:

1. filter to the same decision kind and compatible universe/fingerprint
   version;
2. include only reflections whose outcome was resolved before the new
   decision's `as_of`;
3. compare normalized regime features, not prose;
4. return distance plus a human explanation of why the record matched;
5. use stable ordering for equal scores;
6. apply a minimum-similarity threshold so an unrelated record is not forced
   into context.

A versioned fingerprint should start small:

```text
realized-vol percentile
turbulence percentile
absorption percentile
drawdown percentile
tail-risk percentile
term-structure percentile
robust regime label / uncertainty
```

Do not add embeddings merely to appear sophisticated. Numeric, versioned
features are inspectable and protect against prose-driven leakage. If the
fingerprint schema changes, old fingerprints remain readable and are either
mapped explicitly or excluded as incompatible.

#### Acceptance

- the scorer cannot see prices after the outcome window;
- changing the current mandate/policy does not change an old comparison;
- an outcome resolves once and retries idempotently;
- a candidate is compared with the champion frozen at run time;
- a current operational decision never claims useful alpha against itself;
- similarity retrieval excludes future-resolved lessons;
- ranking is by regime distance, with recency only as a stable tie-break;
- every recalled item exposes the matched features and persisted decision id.

### 9.4 Gap 2 — a typed regime-indicator panel

“Adopt an indicator library” does not mean importing hundreds of technical
signals or letting an agent mine them for trades. It means giving the
estimation judgment a consistent, extensible, deterministic input shape.

Build or accept one panel service over the existing tools:

```text
regime.panel(snapshot_id, as_of, universe)
```

It computes all enabled readings from the same point-in-time snapshot:

```text
turbulence
absorption ratio
volatility term structure
drawdown/trend
tail risk/skew
optional HMM posterior, if already validated
```

Each reading uses one schema:

```text
indicator_id
version
as_of
snapshot_id
raw_value
normalized_percentile
threshold
state=calm|stress|uncertain
confidence
lookback
quality_flags
reasoning_facts
```

The panel then calculates a robust deterministic summary:

```text
robust_state
agreement_count
disagreement_count
uncertainty_reason
fingerprint
```

Rules:

- all indicators share the same `as_of` and snapshot;
- insufficient history produces `uncertain`, not a default calm result;
- disagreement remains visible;
- thresholds and versions are persisted;
- indicators inform window, shrinkage, and regime judgment only;
- no indicator directly emits an order, position, target, or directional
  expected-return signal;
- do not wake Bob for every indicator tick; use confirmed state transitions
  and cooldowns.

The analyst receives the panel once instead of independently calling a menu of
tools and accidentally mixing dates. The challenger receives the exact same
panel and may emphasize dissenting readings. The referee sees both the panel
and the competing claims.

Acceptance:

- identical snapshot and configuration produce an identical panel;
- every indicator has point-in-time/no-look-ahead tests;
- mixed snapshot ids are rejected;
- one failed indicator cannot silently disappear;
- widespread disagreement yields `uncertain`;
- neither the MCP schema nor agent prompt describes the panel as a trading
  signal.

### 9.5 Gap 3 — registry-enforced bounded debate and adjudication

Prompt text alone is not enough to prove a debate is bounded. Move the protocol
into workflow state while keeping model judgment constrained to the
underdetermined estimation call.

Allowed claims:

```text
estimation window
shrinkage intensity/method
regime classification
```

Forbidden debate subjects:

```text
computed objective values
constraint truth
target weights
order construction
whether mandate checks should be bypassed
```

Persist a debate session:

```text
debate_id
workflow_id
original_decision_id
status
max_rounds=2
material_claims JSON
panel_snapshot_id
started_at
closed_at
```

Each turn stores:

```text
turn_id
round
role=challenger|analyst
claim_id
position=defend|amend|rebut
argument
evidence_refs
created_at
```

Protocol:

1. analyst logs the original judgment;
2. challenger identifies only material, typed disputed claims;
3. analyst defends or amends each claim with cited evidence;
4. if material disagreement remains, one challenger rebuttal and one final
   analyst response are allowed;
5. coordinator closes the exchange mechanically at `max_rounds`;
6. an amended judgment creates a new decision and new dependent artifacts;
   it never edits the old record;
7. referee adjudicates the surviving material disagreement and records which
   evidence carried;
8. optimizer/reporter may continue only from the adjudicated decision id.

The coordinator must reject an unexpected role, round overflow, missing
evidence reference, or a claim outside the allowlist. A failed debate blocks
the workflow instead of falling back to the original answer without notice.

Panel/tournament variants count as statistical trials. Their walk-forward
results must continue feeding the existing deflated-Sharpe trial count; debate
must not become a loophole for uncounted repeated experimentation.

Acceptance:

- a third round is impossible in code;
- a target-weight argument is rejected;
- amendment preserves the old decision and invalidates stale dependents;
- adjudication references the panel and exact decision ids;
- a reporter cannot run from an unadjudicated material disagreement;
- restart resumes the next legal debate step without duplicating a turn.

### 9.6 Gap 4 — auditable deep/quick model routing

Model routing is an efficiency and reliability policy, not an authority
policy. A stronger model receives no stronger tools.

Keep the role definition in `agents/*.md`. Use its existing `model:` field for
a stable tier or explicit override:

```text
deep   -> moments-analyst, challenger, referee, Bob synthesis
quick  -> optimization-runner narration, reporter, routine summaries
none   -> deterministic trigger/scorer/solver/executor
```

Resolve stable tiers to provider-specific model ids in operator configuration,
not by scattering ids through TUI code. Persist for every agent turn:

```text
role
requested_tier
resolved_model
backend
prompt_version
tool_policy_version
started_at
latency
token/cost usage when available
result status
fallback reason NULL
```

Failure policy:

- analyst, challenger, referee, and approval synthesis block if their required
  tier is unavailable unless an explicit reviewed fallback is configured;
- reporter wording may fall back to a quick compatible model;
- deterministic runners continue to compute without a model, but the workflow
  does not pretend a missing judgment phase succeeded;
- every fallback emits an audit event and is shown in the task detail.

Do not use model brand names as architecture. Define a backend protocol and
test routing with fakes. Current Claude-specific process code can implement
that protocol first; another backend should not require changes to workflow,
registry, or authority rules.

Acceptance:

- each role resolves the configured tier predictably;
- an explicit role override wins;
- a model change never changes tools;
- resolved model and fallback are auditable;
- a required deep-role failure cannot be mislabeled PASS;
- token/task budgets stop loops deterministically.

### 9.7 Gap 5 — LLM-phrased lessons over immutable outcomes

The current deterministic reflection text is useful but formulaic. Add an
optional language layer that explains the scored outcome in a memorable,
actionable way without altering any fact.

Keep two separate artifacts:

```text
realized_outcome    deterministic, authoritative, immutable
reflection_lesson  model-authored, explanatory, non-authoritative
```

Suggested lesson schema:

```text
lesson_id
decision_id
outcome_hash
prompt_version
model_record_id
summary
what_worked
what_failed
next_time
uncertainty
evidence_refs
created_at
```

Generation input is limited to:

- the original rationale and alternatives;
- challenger/adjudication artifacts;
- the deterministic realized outcome;
- the frozen policy/benchmark labels;
- no later market data and no unrelated portfolio state.

Output requirements:

- short plain language;
- distinguish observation from recommendation;
- cite the decision and outcome;
- never invent a number;
- never say one outcome proves a strategy;
- never change policy, targets, catalog stage, referee verdict, or mandate;
- never be required for deterministic outcome resolution.

Validate that every numeric token in a lesson is present in the supplied
facts, or omit numbers entirely from model prose and render them beside the
lesson from structured data. Bind the lesson to `outcome_hash`; if the
authoritative outcome is corrected, mark the lesson stale and regenerate.

Similarity retrieval chooses records using deterministic fingerprints first.
Only after selection may their explanatory lessons be placed in agent context.
The LLM never ranks its own prose.

Acceptance:

- outcome resolution succeeds when the model is unavailable;
- lesson generation cannot mutate the outcome;
- unsupported numeric claims are rejected;
- a changed outcome invalidates its lesson;
- recalled lessons are selected by deterministic similarity;
- Bob and the TUI label lessons as interpretation, not performance truth.

### 9.8 Workforce-upgrade persistence and API additions

Prefer additive records over overloading free-form JSON:

```text
decision_contexts
debates
debate_turns
reflection_lessons
model_invocations
```

Existing decisions and outcomes remain readable. JSON fields that stay in the
existing table need explicit schema versions.

Owner-backed endpoints/tools:

```text
GET  /api/regime/panel
GET  /api/decisions/{id}/outcome
GET  /api/decisions/{id}/lesson
GET  /api/decisions/similar
GET  /api/workflows/{id}/debate
POST /api/decisions/{id}/lesson/generate
```

The generation route is an owner action with task budgets. It is not required
for execution and is not exposed to a role that could recursively generate
lessons.

Add durable events:

```text
reflection.outcome_resolved
reflection.lesson_generated
reflection.lesson_stale
debate.started
debate.turn_recorded
debate.closed
debate.adjudicated
model.route_resolved
model.fallback_used
```

---

## 10. Persistence model

Reuse existing workflows, decisions, runs, verdicts, plans, orders, and events.
Do not duplicate their artifacts in Bob tables.

### 10.1 `bob_state`

One logical current-state record:

```text
manager_id          bob-the-quant
mode                observe|research|propose|paused
state               starting|observing|...
current_task_id
last_wake_reason
last_brief_at
blocked_reason
coordinator_session_id
updated_at
```

### 10.2 `bob_tasks`

```text
task_id
dedupe_key UNIQUE
trigger_kind
trigger_payload JSON
template_id
status
workflow_id NULL
conclusion JSON
error
attempt_count
created_at
started_at
completed_at
updated_at
```

Task status:

```text
queued -> running -> completed
                 -> awaiting_approval
                 -> blocked
                 -> failed
queued/running   -> canceled
awaiting_approval -> completed|expired|invalidated
```

### 10.3 `approval_requests`

```text
approval_id
task_id
plan_id
plan_digest
decision_id
targets_hash
data_permit_id
broker
book_revision
expected_cost JSON
summary JSON
status
challenge_digest
expires_at
decided_at
consumed_at
invalidated_reason
created_at
```

Status:

```text
pending -> approved -> consumed
pending -> rejected
pending -> expired
pending/approved -> invalidated
```

### 10.4 `data_permits`

```text
permit_id
snapshot_id
purpose
provider
feed
as_of
provenance JSON
integrity JSON
eligibility JSON
expires_at
created_at
```

### 10.5 Future `authority_grants`

Not part of initial execution:

```text
grant_id
mode=paper_auto
allowed_universe
max_notional
max_turnover
max_orders
allowed_policy
valid_from
expires_at
revoked_at
```

A future standing authorization is checked by deterministic code and can be
revoked immediately. Bob never edits it.

---

## 11. Owner APIs and events

### 11.1 Bob API

```text
GET  /api/bob/status
GET  /api/bob/tasks
GET  /api/bob/tasks/{task_id}
POST /api/bob/message
POST /api/bob/mode
POST /api/bob/pause
POST /api/bob/resume
POST /api/bob/tasks/{task_id}/cancel
```

`/api/bob/message` can ask a question or explicitly request an allowed
workflow. It does not grant authority.

### 11.2 Data API

```text
GET /api/data/health
GET /api/data/permit/current?purpose=paper_proposal
GET /api/quotes?symbols=...
```

The existing `/api/market` and `/api/tui` include compact projections of the
same data.

### 11.3 Approval API

```text
GET  /api/approvals
GET  /api/approvals/{approval_id}
POST /api/approvals/{approval_id}/challenge
POST /api/approvals/{approval_id}/approve
POST /api/approvals/{approval_id}/reject
```

These endpoints are human-client surfaces and are not included in the owner MCP
tool allowlist.

### 11.4 Plan execution API

```text
POST /api/plans/{plan_id}/execute
{
  "approval_id": "...",
  "challenge_response": "..."
}
```

The owner ignores caller-supplied plan content and loads the exact persisted
plan.

### 11.5 Event kinds

Add durable event kinds:

```text
data.health_changed
data.permit_issued
data.permit_refused
alpaca.stream_connected
alpaca.stream_disconnected
broker.order_update
bob.started
bob.state_changed
bob.triggered
bob.task_started
bob.task_completed
bob.task_blocked
bob.brief_ready
approval.requested
approval.approved
approval.rejected
approval.expired
approval.invalidated
execution.revalidation_failed
```

Market quote updates remain transient/throttled. Durable events and transient
market events continue through the existing SSE surface.

---

## 12. TUI implementation

### 12.1 Preserve the current shell

Keep:

```text
left context spine | switchable center canvas | persistent right rail
--------------------------------------------------------------------
event strip / timeline
global input                            execution + data + Bob status
```

Keep the seven existing screens:

1. Dashboard
2. Market
3. Workforce
4. Research
5. Book
6. Audit
7. Settings

Bob is not hidden behind an eighth screen. Its compact state remains visible on
all seven screens.

### 12.2 Right rail

Replace the top of the generic agent rail with:

```text
BOBTHEQUANT
● OBSERVING

ASSESSMENT
Calm regime; absorption rising.
No mandate trigger.

NEXT
Review after completed close.

APPROVALS
1 waiting · expires 14m

WORKFORCE
analyst       idle
challenger    idle
optimizer     idle
referee       idle
reporter      idle
```

Rules:

- no raw chain-of-thought;
- short attributed assessment;
- current state, wake reason, next action, blocker, and approval count;
- clicking/focusing opens details;
- degraded data or broker state takes visual priority;
- working workforce roles remain visible below Bob.

### 12.3 Bob detail drawer

`Ctrl+B` opens/focuses:

```text
Current brief
Why Bob woke
Evidence citations
Active task and workflow
Recent completed tasks
Pending approvals
Mode and pause controls
Conversation input
```

Use existing persisted ids to link into Research, Workforce, Book, and Audit.
Do not duplicate entire reports.

### 12.4 Global input

The bottom input should accept:

```text
plain language          -> message Bob
: command               -> existing command handler
```

Commands:

```text
: bob why
: bob status
: bob observe
: bob research
: bob propose
: bob pause
: bob resume
: bob tasks
: approvals
```

Buttons and commands call the same handlers.

### 12.5 Approval panel

Required sections:

```text
PAPER PLAN

WHY NOW
Trigger, regime, drift, or user request.

RESEARCH CONCLUSION
Concise result with workflow/run citations.

PORTFOLIO CHANGES
Current weight, target weight, notional delta for every leg.

COST AND EXECUTION
Expected cost, spread, quote source/feed/age, order type.

CONTROLS
Data permit, reconciliation, mandate, referee PASS, cost gate.

RISKS / DISSENT
Challenger and uncertainty.

BINDING
Plan id, target hash abbreviation, approval expiry.

[APPROVE PAPER PLAN] [REJECT] [ASK BOB] [INSPECT EVIDENCE]
```

Approval opens a second deliberate confirmation or requires an explicit typed
phrase such as `APPROVE PAPER <short-plan-id>`. It must not be a bare `y`
hotkey.

### 12.6 Status line

Example:

```text
PAPER · ALPACA SIP · QUOTE 2s · BAR 2026-07-23 · BOB PROPOSE
```

Degraded:

```text
PAPER · ALPACA SIP STALE 74s · EXECUTION BLOCKED · BOB WATCHING
```

Never use an ambiguous `LIVE` label.

### 12.7 Bloomberg lessons

Borrow:

- command-first navigation;
- linked instrument context;
- saved workspace/context later;
- high-information alerts;
- morning/event briefs;
- transparent attribution;
- research-to-action continuity;
- persistent conversational assistant.

Do not copy:

- visual noise;
- decorative density;
- cryptic function overload;
- an order-entry aesthetic;
- constant animation;
- a top banner.

### 12.8 JavaScript decision

No JavaScript in Textual.

The future web client can use JavaScript for detachable panels, browser
notifications, richer charts, and saved layouts. It must consume the same Bob,
approval, market, and event APIs. Business logic remains owner-side.

---

## 13. Configuration

Proposed operator configuration:

```yaml
market_data:
  mode: operational
  provider: alpaca
  feed: iex
  allow_cache: true
  allow_synthetic: false
  quote_max_age_seconds: 10
  completed_bar_grace_minutes: 30

broker:
  provider: alpaca_paper
  order_type: marketable_limit
  marketable_limit_buffer_bps: 5
  max_spread_bps: 25
  order_timeout_seconds: 120

bob:
  enabled: true
  startup_mode: observe
  startup_brief: true
  daily_review: true
  max_autonomous_workflows_per_day: 3
  trigger_cooldown_minutes: 30
  approval_ttl_minutes: 15

research_workforce:
  outcome_horizon_days: 63
  strategic_benchmarks: [sixty_forty, equal_weight]
  similarity_min_score: 0.65
  debate_max_rounds: 2
  generate_reflection_lessons: true
  model_tiers:
    deep: configured-deep-model
    quick: configured-quick-model
```

Environment:

```text
ALPACA_API_KEY
ALPACA_API_SECRET
QLAB_DATA_PROVIDER=alpaca
QLAB_ALPACA_FEED=iex|sip|delayed_sip
QLAB_BROKER=alpaca_paper
QLAB_AGENT_MODEL_DEEP
QLAB_AGENT_MODEL_QUICK
```

Do not put credentials in YAML. Redact all authentication-bearing exception
strings before events or model context.

Maintain explicit CLI modes:

```text
qlab tui                  operational config
qlab tui --demo           synthetic, no execution eligibility
qlab batch ... --offline  deterministic research fixture
```

Deprecate the ambiguous idea that ordinary `qlab tui` defaults to synthetic.
If operational configuration is incomplete, the desk starts visibly blocked
rather than substituting data.

---

## 14. File-level implementation map

Exact filenames may be adjusted after re-audit, but preserve these boundaries.

### Data

- `qlab/core/data.py`
  - `DataPolicy`
  - no-fallback operational behavior
  - structured provenance
- `qlab/core/types.py`
  - provenance/permit types or compatible snapshot extension
- `qlab/data/alpaca.py` or `qlab/market/alpaca.py`
  - historical adapter extracted from core
  - market stream supervisor
- `qlab/data/health.py`
  - freshness and eligibility
- `qlab/autopilot/scheduler.py`
  - shared exchange-calendar semantics

### Research workforce

- `qlab/governance/reflection.py`
  - deterministic, versioned outcome resolution only
  - frozen policy/benchmark comparisons
- `qlab/governance/lessons.py`
  - optional grounded language lesson over an outcome hash
- `qlab/signals/indicators.py`
  - individual point-in-time indicator implementations
- `qlab/signals/panel.py`
  - same-snapshot panel, robust state, fingerprint
- `qlab/workflows/debate.py` or the existing workflow module
  - persisted transition rules, round limit, adjudication dependency
- `qlab/operator/model_routing.py`
  - role tier resolution and invocation audit records
- `agents/moments-analyst.md`
- `agents/challenger.md`
- `agents/referee.md`
- `agents/optimization-runner.md`
- `agents/reporter.md`
  - authority-neutral role/model/debate/lesson instructions

Do not add a second reflection engine, regime-tool family, or coordinator if
the moving checkout already contains an acceptable implementation. Extend the
existing modules after the Phase 5 matrix.

### Broker/execution

- `qlab/trader/broker.py`
  - explicit broker factory
  - Alpaca paper adapter without fallback
- `qlab/trader/alpaca_orders.py`
  - quote-to-limit pricing, asset capability checks, REST recovery
- `qlab/trader/plan.py`
  - persisted digest/version and execution-time revalidation seam
- `qlab/trader/approvals.py`
  - approval validation/state machine
- `qlab/trader/reconcile.py`
  - external order/position reconciliation
- `qlab/governance/referee.py`
  - data permit and current-cost gates as deterministic checks

### Bob

- `agents/bob-the-quant.md`
  - neutral prompt/authority source
- `qlab/operator/supervisor.py`
  - deterministic Bob lifecycle and triggers
- `qlab/operator/backend.py`
  - coordinator backend protocol
- `qlab/operator/claude_backend.py`
  - refactored current Claude process behavior
- `qlab/operator/templates.py`
  - allowed workflow templates
- `qlab/operator/types.py`
  - task/state projections
- `qlab/tui/claude.py`
  - reduce to compatibility/imports or UI adapter after extraction

### Owner/API

- `qlab/ui/server.py`
  - service composition, APIs, serialized command queue
- `qlab/mcp/tui_proxy.py`
  - Bob-safe observe/workflow/proposal tools only
- `qlab/state/registry.py`
  - schema and methods; still owner-only

### TUI

Prefer splitting the current large `qlab/tui/app.py` while adding Bob:

- `qlab/tui/widgets/bob_rail.py`
- `qlab/tui/widgets/bob_drawer.py`
- `qlab/tui/screens/approval.py`
- `qlab/tui/actions.py`
- `qlab/tui/app.py`
  - composition and routing, not all rendering logic

Do not mechanically rewrite unrelated TUI code during this feature.

### Tests

- `tests/test_data_policy.py`
- `tests/test_alpaca_data.py`
- `tests/test_broker_alpaca.py`
- `tests/test_approvals.py`
- `tests/test_bob_supervisor.py`
- `tests/test_bob_api.py`
- `tests/test_reflection.py` or focused additions to `tests/test_autopilot.py`
- `tests/test_regime_panel.py`
- `tests/test_debate.py`
- `tests/test_model_routing.py`
- `tests/test_reflection_lessons.py`
- targeted additions to:
  - `tests/test_autopilot.py`
  - `tests/test_trader.py`
  - `tests/test_ui.py`
  - `tests/test_tui.py`
  - `tests/test_mcp_server.py`

---

## 15. Phased implementation

Each phase must end with tests, `git diff --check`, review, and a conventional
imperative commit. Do not begin a later authority phase with an earlier phase
red.

### Phase 0 — Governance baseline and merge audit

**Purpose:** Do not put Bob on top of unsafe execution surfaces.

Tasks:

1. Let the current coding leg finish and obtain a clean inventory.
2. Re-run all 18 governance findings.
3. Verify critical fixes for:
   - headless unconfirmed execution;
   - CLI/owner `run_once` execution defaults;
   - stale checked-plan execution;
   - forged in-memory plan content;
   - omitted held positions;
   - current drawdown/mandate revalidation;
   - Alpaca persistent HWM;
   - broker fallback;
   - malformed/negative costs.
4. Add regression tests for every remaining reproduced path.
5. Make all current execution paths proposal-only unless approved.

Exit criteria:

- no agent-reachable execution tool;
- no direct default execution from CLI/web/owner;
- exact persisted plan only;
- revalidation at execution;
- full suite green.

Suggested commit:

```text
fix(governance): close unconfirmed paper execution paths
```

### Phase 1 — Alpaca-required historical data

Tasks:

1. Add `DataPolicy`.
2. Split operational/historical/demo/test behavior.
3. Make operational provider configuration explicit.
4. Remove online synthetic fallback under operational policy.
5. Preserve matching-provider cache provenance.
6. Add session-aware bar freshness.
7. Add typed data errors.
8. Add data permit generation without quote eligibility yet.
9. Surface blocked state through owner/TUI.

Exit criteria:

- an Alpaca outage never returns synthetic in operational mode;
- a yfinance cache cannot satisfy an Alpaca request;
- demo/test synthetic remains green;
- stale/missing Alpaca data blocks paper proposals;
- default suite remains offline.

Suggested commits:

```text
feat(data): add explicit operational data policy
fix(data): refuse synthetic fallback for Alpaca operations
```

### Phase 2 — Alpaca market stream and data health

Tasks:

1. Add quote/trade stream supervisor.
2. Add reconnect/backoff and health state.
3. Add latest-value cache and throttled owner events.
4. Add feed identity.
5. Extend data permits with quote freshness.
6. Add TUI market/status projections.
7. Ensure daily strategy remains based on completed daily bars.

Exit criteria:

- stream reconnect tested with a fake client;
- stale quote transition is visible and blocks execution;
- no quote callback writes DuckDB directly;
- IEX/SIP appears in owner and TUI state;
- no per-tick DuckDB flood.

Suggested commit:

```text
feat(data): stream Alpaca quotes through the owner
```

### Phase 3 — Real Alpaca paper order lifecycle

Tasks:

1. Add explicit broker selection and fail-loud startup.
2. Add asset/tradability/fractionability checks.
3. Add actual limit order requests and deterministic pricing.
4. Add trade-update stream.
5. Add plan/order state transitions.
6. Add REST recovery and cancel-confirm-resubmit.
7. Add slippage/fill/fee persistence.
8. Add execution-time revalidation and approval consumption.
9. Add Alpaca paper integration tests behind an explicit environment gate.

Exit criteria:

- REST acceptance does not mark a plan filled;
- partial/reject/cancel/timeout flows are tested;
- repeated execution is idempotent;
- unsupported marketable-limit request refuses;
- no simulator fallback;
- no live account path.

Suggested commits:

```text
feat(trader): add Alpaca paper order lifecycle
fix(trader): revalidate approved plans at submission
```

### Phase 4 — Bob owner runtime in Observe mode

Tasks:

1. Extract UI-independent coordinator backend.
2. Add Bob prompt source and regenerate adapters.
3. Add Bob tables/migrations.
4. Add deterministic supervisor/state machine.
5. Add startup health/brief task.
6. Add event subscription, dedupe, budgets, cooldowns, pause/resume.
7. Add owner Bob APIs/events.
8. Start asynchronously after owner initialization.
9. Leave workforce/proposal authority disabled.

Exit criteria:

- owner starts even if Claude is unavailable;
- Bob reports `degraded`, not owner failure;
- Bob survives TUI view changes/client reconnect;
- restart restores state;
- no LLM call for unchanged health polling;
- Bob has no execution/proposal tool in Observe.

Suggested commits:

```text
refactor(agents): extract coordinator process backend
feat(operator): add BobTheQuant observer supervisor
```

### Phase 5 — Research-workforce intelligence acceptance and hardening

**Purpose:** Finish the five TradingAgents-inspired upgrades without
duplicating work that landed during the preceding coding leg.

Tasks:

1. Produce the `absent | partial | complete-but-unproven | accepted` matrix
   from Section 9.
2. Clarify and persist operational policy, strategic benchmark, and frozen
   research-champion ids.
3. Accept or harden deterministic outcome scoring:
   - raw realized portfolio outcome;
   - strategic benchmark comparisons;
   - candidate-versus-frozen-champion comparison where applicable;
   - immutable point-in-time context and formula version.
4. Accept or harden regime-fingerprint similarity recall:
   - same-kind/universe filtering;
   - no future-resolved lessons;
   - explicit feature-match explanation and threshold.
5. Consolidate the deterministic regime tools into one same-snapshot typed
   panel with robust uncertainty.
6. Move bounded debate from prompt-only convention into persisted workflow
   state, with two-round enforcement and adjudication.
7. Move role-based model routing behind the UI-independent backend, log the
   resolved model, and test required-role failure behavior.
8. Add the optional grounded lesson artifact over immutable outcomes.
9. Update `agents/*.md`, regenerate adapters, and preserve every role's
   existing tool authority.
10. Add migrations and compatibility reads for already-persisted decisions.

Exit criteria:

- old decisions remain readable;
- current policy/benchmark terminology is unambiguous;
- outcome metrics are point-in-time, deterministic, and idempotent;
- similarity recall cannot leak a future outcome;
- all regime readings share one snapshot and expose uncertainty;
- code makes a third debate round impossible;
- model routing is auditable and does not change tools;
- a missing lesson model cannot block outcome resolution;
- every accepted pre-existing component has a focused regression test;
- no research-workforce change grants proposal or execution authority.

Suggested commits should follow the audit result rather than force unnecessary
rewrites. Likely slices:

```text
fix(reflection): freeze outcome comparators at decision time
feat(signals): expose a point-in-time regime panel
feat(workflows): persist bounded estimation debate
refactor(agents): make role model routing auditable
feat(reflection): generate grounded outcome lessons
```

### Phase 6 — Autonomous research mode

Tasks:

1. Register workflow templates.
2. Add deterministic trigger evaluator.
3. Allow Bob to start/resume templates according to mode.
4. Bind tasks to durable workflow ids.
5. Add synthesis grounded in persisted artifacts.
6. Add one corrected retry and terminal block behavior.
7. Add `why`, tasks, and evidence APIs.
8. Prove research/offline catalog entries remain non-operational.

Exit criteria:

- a synthetic test event starts exactly one bounded workflow;
- duplicate events do not duplicate workflows;
- second failure blocks;
- every conclusion cites registry artifacts;
- no proposal or order is created in Research mode.

Suggested commit:

```text
feat(operator): orchestrate governed research from Bob triggers
```

### Phase 7 — Propose mode and approvals

Tasks:

1. Allow Bob to request checked previews after PASS.
2. Add approval request state machine and expiry.
3. Bind approval to plan/digest/hash/data/broker/book/cost.
4. Add human-only challenge/approve/reject API.
5. Invalidate on any bound-state change.
6. Add notification/event behavior.
7. Connect approved plan to execution-time revalidation.

Exit criteria:

- Bob can create a pending approval but cannot approve it;
- changed targets/book/cost/data invalidate approval;
- expired approval cannot execute;
- same approval cannot be consumed twice;
- headless MCP cannot approve or execute.

Suggested commit:

```text
feat(governance): add bound paper-plan approvals
feat(operator): let Bob prepare governed paper proposals
```

### Phase 8 — Persistent Bob TUI

Tasks:

1. Add persistent Bob rail.
2. Add detail drawer and global focus.
3. Add global natural-language input routing.
4. Add mode/pause/task controls.
5. Add approval panel.
6. Add provider/feed/freshness/broker/Bob status.
7. Link evidence to existing screens.
8. Verify responsive tiers and Windows terminal.

Exit criteria:

- Bob remains visible across all seven screens;
- rail shows no hidden reasoning;
- all actions have button and command parity;
- approval exposes evidence, risks, costs, source, expiry;
- no single key executes;
- Textual Pilot and real-terminal smoke pass at wide/compact/narrow widths.

Suggested commit:

```text
feat(tui): add persistent BobTheQuant operator rail
feat(tui): add bound paper approval workflow
```

### Phase 9 — Shadow rollout and optional standing paper authority

Do not begin automatically.

First run Bob in shadow:

- Bob produces research/proposals;
- operator decisions are recorded;
- no automatic execution;
- compare alert usefulness, duplication, false triggers, model cost, and
  proposal validity over multiple sessions.

Only then consider standing paper grants. A separate design review must approve
the grant schema, revocation, anomaly pauses, and user experience.

---

## 16. Verification strategy

### 16.1 Unit tests

#### Data

- operational Alpaca never returns synthetic;
- provider mismatch cache refused;
- synthetic cache never masquerades as Alpaca;
- fresh Alpaca cache accepted only within policy;
- holiday/weekend freshness correct;
- stale quote/bar eligibility false;
- permit hash stable and changes with content/provenance.

#### Bob

- state transitions;
- dedupe keys;
- cooldown and daily budgets;
- one-retry rule;
- pause behavior;
- trigger-to-template mapping;
- approval expiry scheduling;
- startup with unavailable model;
- no trigger on every quote.

#### Approval/execution

- exact plan binding;
- stale book invalidation;
- data permit invalidation;
- cost version invalidation;
- approval one-time consumption;
- quote-to-limit price;
- spread/age refusal;
- order-event transitions;
- timeout recovery;
- partial fill;
- cancel-confirm-resubmit.

#### Research workforce

- decision context freezes policy, benchmark, snapshot, targets, and formula
  version;
- changing today's champion cannot alter an old outcome;
- operational target versus itself is `not_applicable`, not claimed alpha;
- candidate alpha uses the champion frozen when the run began;
- reflection resolution is idempotent;
- a missing comparator blocks rather than changes the compared universe;
- similarity recall excludes outcomes unavailable at the query date;
- distance ordering and threshold are deterministic;
- regime panel rejects mixed snapshot ids;
- indicator insufficiency/disagreement produces explicit uncertainty;
- individual indicators pass no-look-ahead fixtures;
- debate state rejects a third round and forbidden claims;
- amended decisions preserve originals and invalidate stale dependents;
- adjudication gates the next workflow step;
- every role resolves and logs its model tier;
- required-role model failure blocks rather than silently downgrades;
- lessons cannot mutate outcomes or introduce unsupported numbers;
- model outage does not block deterministic outcome resolution.

### 16.2 Owner/API tests

- `/api/run_once` proposal-only default;
- approval route absent from MCP allowlist;
- Bob route cannot execute;
- background command queue serializes registry writes;
- SSE carries Bob/data/approval events;
- reconnect cursor does not duplicate terminal actions;
- model/Bob error does not break `/api/tui`;
- regime panel, debate, outcome, and lesson routes remain owner-backed;
- lesson generation cannot recursively trigger itself.

### 16.3 Adversarial governance tests

Attempt:

- prompt asks Bob to execute;
- forged `human_confirmed`;
- direct `execute:true`;
- approval for another plan;
- modified targets after PASS;
- modified persisted leg count;
- second plan from stale cash snapshot;
- synthetic operational snapshot;
- stale Alpaca quote;
- broken Alpaca constructor;
- unknown feed;
- negative/NaN limits or cost;
- replayed approval;
- timeout followed by accepted broker order;
- order stream duplicate/out-of-order event.

Every attempt must refuse safely and emit an understandable audit event.

### 16.4 TUI tests

- Bob rail exists in every view;
- switching F1–F7 preserves Bob/task state;
- Ctrl+B focuses drawer;
- narrow mode keeps access;
- approval displays required fields;
- approval cannot be triggered with one bare key;
- data degraded status is prominent;
- no `LIVE` ambiguity;
- no startup modal;
- stream updates repaint without flooding;
- stale owner snapshot does not resurrect obsolete Bob task/approval state.

### 16.5 Network integration tests

Default suite: no network.

Separate opt-in suite:

```text
QLAB_RUN_ALPACA_INTEGRATION=1
```

Validate against a dedicated Alpaca paper account:

- historical bars;
- selected IEX/SIP entitlement;
- quote stream;
- paper account identity;
- tradable/fractionable asset checks;
- small limit order;
- partial/cancel if reproducible;
- trade-update stream;
- reconciliation;
- cleanup/reset instructions.

Never run paid or externally mutating integration tests implicitly.

### 16.6 Closeout

For every phase:

```text
python -m pytest <targeted modules> -q
python -m pytest
python -m compileall -q qlab
git diff --check
python -m qlab.agents.loader sync   # when agents change
```

Then:

- restart owner;
- run real-terminal TUI smoke;
- verify exact data/broker/Bob status;
- inspect audit events;
- ensure no secret appears in output.

---

## 17. Failure-mode behavior

| Failure | Required behavior |
|---|---|
| Alpaca credentials absent | owner starts blocked; no synthetic substitution |
| Alpaca package absent | explicit setup error; execution unavailable |
| Historical fetch fails | fresh permitted Alpaca cache or blocked |
| Quote stream disconnects | display stale countdown; block new execution |
| Trade stream disconnects | stop new submissions; REST-recover open orders |
| Bob/Claude unavailable | owner and TUI continue; Bob=`degraded` |
| Bob timeout/silence | stop process; preserve task/workflow; allow resume |
| Workflow phase fails once | one corrected retry |
| Workflow phase fails twice | task blocked; human-readable reason |
| Referee FAIL | no preview; Bob explains dissent |
| Cost gate FAIL | plan terminally refused |
| Approval expires | invalidate; no execution |
| Book changes after approval | invalidate; regenerate/reapprove |
| Order partially fills | monitor/reconcile; do not submit conflicting plan |
| Order rejected | pause new proposals; explain broker reason |
| Owner restarts | recover tasks/approvals/open orders before new work |
| Unknown state | fail closed and page/alert operator |

---

## 18. Rollout and rollback

### Rollout

1. Land governance repair.
2. Land data policy with operational behavior behind configuration.
3. Run Alpaca market data in display-only mode.
4. Run Alpaca paper lifecycle manually from approved TUI plans.
5. Enable Bob Observe.
6. Accept/harden the five research-workforce upgrades.
7. Enable Bob Research.
8. Run Bob Propose in shadow; compare with human decisions.
9. Enable real approval requests.
10. Consider standing paper authority only after a separate review.

### Feature controls

```text
BOB_ENABLED
BOB_MODE
QLAB_DATA_PROVIDER
QLAB_ALPACA_FEED
QLAB_BROKER
```

Do not implement a control that changes paper to live.

### Rollback

- Bob can be disabled without disabling the owner.
- Bob mode can be set to Observe or Paused.
- Alpaca stream can be disabled while historical Alpaca remains required.
- If broker lifecycle is unhealthy, paper execution is disabled; do not
  silently use simulation.
- Schema migrations are additive and old records remain readable.
- Open broker orders are reconciled before rollback; never abandon them because
  a feature flag changed.

---

## 19. Trade-offs and rationale

### Keep synthetic fixtures, block operational fallback

Deleting synthetic code would weaken offline tests and reproducibility.
Allowing operational fallback would weaken trust. Purpose-specific policy gives
both.

### Owner-managed Bob versus TUI-managed Bob

Owner-managed is more work because process output must flow through owner
events, but it is the only design consistent with persistence, web/TUI parity,
and the one-writer boundary.

### Human approval versus full paper auto

Human approval limits initial autonomy, but it makes Bob's research behavior,
execution correctness, and alert quality observable before granting standing
authority. Research automation delivers most of the product value without
premature order autonomy.

### Streams versus polling

Streams provide quote and order timeliness. They require reconnect/recovery
logic. Use streams for current state and REST for recovery; never assume either
alone is complete.

### Textual versus JavaScript

Textual keeps the current cross-platform operator console coherent. Shared
owner APIs preserve the option for a richer JS web client later. Embedding JS
inside the TUI creates complexity without strengthening the desk.

### One owner versus a distributed worker system

The seven-ETF/daily desk does not need distributed infrastructure. A serialized
owner command queue is sufficient and protects DuckDB. Revisit only if universe
or workload growth demonstrates a real bottleneck.

### Visible Bob versus continuously running LLM

Persistent presence is a UI/state property. Event-driven model use is cheaper,
more reliable, easier to audit, and less likely to generate meaningless
activity.

---

## 20. What to revisit as the system grows

- dedicated owner daemon/service independent of TUI lifetime;
- multi-user authentication and approval identity;
- encrypted secret manager;
- broader universe stream fanout;
- SIP subscription/cost management;
- persistent high-volume market store outside DuckDB;
- multiple portfolios/accounts;
- standing paper authority;
- notification integrations;
- richer web/JS Launchpad layouts;
- live-money broker design only after a separate security, legal, operational,
  and risk review.

---

## 21. Definition of done

The feature is complete only when all statements are true:

1. Ordinary qlab operator mode requires Alpaca data.
2. Operational data failure never creates synthetic/cross-provider output.
3. The TUI shows provider, feed, and freshness.
4. Alpaca paper selection is explicit and fail-loud.
5. No CLI, HTTP, MCP, workforce, or Bob path executes without a valid bound
   approval.
6. Execution revalidates the current book and all gates.
7. Alpaca order state is driven by real broker updates and reconciliation.
8. Reflections score immutable point-in-time outcomes against explicitly named,
   frozen comparators.
9. Similar reflections are recalled by versioned regime fingerprints without
   future-outcome leakage.
10. Every regime indicator in a judgment comes from one typed snapshot panel.
11. Debate is bounded and adjudication-gated in workflow code, not only prompts.
12. Role-model routing is auditable and never changes tool authority.
13. Model-phrased lessons remain optional interpretations over immutable facts.
14. Bob starts with the owner and remains visible across the TUI.
15. Bob can autonomously monitor and run governed research.
16. Bob can prepare a checked proposal and approval request.
17. Bob cannot approve or execute.
18. Conclusions cite persisted evidence.
19. Duplicate triggers/events do not duplicate workflows/orders.
20. Model/data/broker failures degrade safely.
21. The full offline suite passes.
22. Opt-in Alpaca paper integration passes.
23. Real-terminal smoke passes on macOS and Windows.
24. README, configuration docs, commands, and operator status accurately
    describe the behavior.

---

## 22. Suggested relay prompt for the implementation agent

Use this document as the implementation contract:

> Implement `planning-docs/2026-07-24-bobthequant-autopilot-alpaca-implementation-plan.md`.
> First read `AGENTS.md`, `README.md`, the governance findings, product roadmap,
> and current worktree. Do not overwrite unrelated/in-flight changes. Reconcile
> the plan with code that landed after baseline `65971f7`, and report any
> already-completed or conflicting items before editing. Work phase by phase,
> starting with Phase 0. Keep the one-writer boundary, offline default test
> suite, agent tool restrictions, exact-target referee binding, and paper-only
> execution. Each phase must land with targeted tests, full-suite validation,
> diff check, owner restart/runtime smoke where applicable, and a focused
> conventional commit. In Phase 5, audit the five TradingAgents-inspired gaps
> before writing code: accept and harden work that already landed instead of
> duplicating it. Do not begin Bob proposal authority until governance, Alpaca
> data eligibility, paper execution revalidation, and research-workforce
> acceptance are green.

---

## 23. Primary external references

- Alpaca real-time stock data:
  https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data
- Alpaca market-data feed differences:
  https://docs.alpaca.markets/us/docs/market-data-faq
- Alpaca paper/live authentication boundary:
  https://docs.alpaca.markets/us/v1.1/docs/authentication-1
- Alpaca order behavior and streaming recommendation:
  https://docs.alpaca.markets/us/docs/orders-at-alpaca
- Alpaca fractional trading:
  https://docs.alpaca.markets/us/docs/fractional-trading
- Alpaca trade-update stream:
  https://docs.alpaca.markets/us/v1.4.2/docs/websocket-streaming
- Bloomberg Terminal workspace/Launchpad direction:
  https://professional.bloomberg.com/products/bloomberg-terminal/
- Bloomberg conversational/agentic research direction:
  https://professional.bloomberg.com/products/bloomberg-terminal/ai/
