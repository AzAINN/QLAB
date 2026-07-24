# TUI Atlas & Portfolio Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an "atlas" view that catalogs the desk's components (arms, metrics, roles, governance) with the champion policy marked live, and upgrade the book view with an equity curve + realized metrics fed from a new registry table with Alpaca backfill — plus method-name humanization, a connection staleness chip, an ablation leaderboard, and position P&L.

**Architecture:** Curated prose lives in `qlab/core/atlas.py` (dataclasses, importable by any surface). The owner server (`qlab/ui/server.py`) assembles everything the TUI renders — new payloads `atlas`, `performance`, `leaderboard` follow the existing pattern where the server does joins and the TUI is a dumb renderer. Equity history is a new `equity_marks` registry table written only by the owner process (one-writer invariant intact), backfilled from Alpaca's portfolio-history endpoint when that broker is active.

**Tech Stack:** Python 3.11+, Textual (TUI), DuckDB (registry), pandas (resampling), alpaca-py (optional, paper only), pytest.

## Approved design decisions (from brainstorming)

1. **Hybrid atlas**: curated prose checked into the repo; live facts (champion, stage, ablation numbers) overlaid from the owner API.
2. **Registry + Alpaca backfill** for equity history; the TUI charts only from the registry.
3. **Navigation**: one new `atlas` view (placed between audit and settings, so settings stays last); performance goes *into* the existing `book` view.
4. **Master-detail layout** for the atlas: grouped list left, prose + overlay right.
5. **Method names lead everywhere.** Arm codes (B0…A4, A3t) never appear in list rows, titles, leaderboards, or the champion mark — only as one dim `ablation id: B2` footnote in the atlas detail pane (they remain the keys in configs and registry rows).

## Global Constraints

- **Never add a second DuckDB writer** — all registry writes happen inside the owner process (`qlab/ui/server.py` handlers / `UISession` methods).
- **Tests never open `.lab/registry.duckdb`** — always `Registry(":memory:")`; the full suite passes offline.
- **Fail loud** — no silent fallbacks. Missing Alpaca credentials, absent history, insufficient samples all produce explicit errors or explicit "absent" strings, never invented numbers.
- **No new execution paths** — nothing in this plan touches order submission or the referee gate.
- Commit messages: imperative, conventional prefix + scope, **no AI-attribution trailers**.
- Comment density matches the repo: comments state constraints code cannot show.
- **Restart the owner process** (`qlab tui` / `qlab ui`) after server-side changes — a long-lived owner serves pre-change imports.
- Never weaken an existing test assertion to make a change pass.

## File Map

| File | Change |
|---|---|
| `qlab/core/atlas.py` | **Create** — curated catalog + arm-name mapping |
| `tests/test_atlas.py` | **Create** |
| `qlab/state/registry.py` | Add `equity_marks` table to `_SCHEMA` + two accessors |
| `qlab/trader/broker.py` | `unrealized_pl` in both brokers; `portfolio_history()` on Alpaca |
| `qlab/ui/server.py` | `performance()`, `record_equity_mark()`, `backfill_equity_history()`, `atlas()`, `latest_ablation_metrics()`, `leaderboard()`; new routes; snapshot keys |
| `qlab/tui/formatting.py` | `connection_chip()` pure function |
| `qlab/tui/atlas_view.py` | **Create** — master-detail widget |
| `qlab/tui/app.py` | conn chip; book equity section + P&L column; atlas view wiring; leaderboard render; arm-name humanization |
| `qlab/tui/theme.py` | `APP_CSS` additions for `#conn-chip` and atlas layout |
| `tests/test_registry.py`, `tests/test_trader.py`, `tests/test_ui.py`, `tests/test_tui.py` | New tests per task |

---

### Task 1: Atlas content module (`qlab/core/atlas.py`)

The single source of truth for component prose *and* the arm-id → display-name mapping every other surface uses.

**Files:**
- Create: `qlab/core/atlas.py`
- Test: `tests/test_atlas.py`

**Interfaces:**
- Produces: `AtlasEntry` (frozen dataclass), `ATLAS_ENTRIES: tuple[AtlasEntry, ...]`, `ARM_NAMES: dict[str, str]` (e.g. `{"B2": "HRP"}`), `arm_display_name(arm_id: str) -> str` (unknown ids pass through unchanged — that is honest display, not a fallback), `arm_algorithm_key(arm_id: str) -> str | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_atlas.py
"""The curated component catalog stays in lockstep with the real system."""

from qlab.core.atlas import (
    ATLAS_ENTRIES, ARM_NAMES, arm_algorithm_key, arm_display_name)


def test_every_ablation_arm_has_a_display_name():
    assert set(ARM_NAMES) == {
        "B0", "B1", "B2", "B3", "B4", "A1", "A2", "A3", "A4", "A3t"}


def test_arm_algorithm_keys_exist_in_catalog():
    from qlab.algorithms import list_algorithms
    catalog_ids = {row["id"] for row in list_algorithms()}
    for entry in ATLAS_ENTRIES:
        if entry.algorithm_key is not None:
            assert entry.algorithm_key in catalog_ids, entry.entry_id


def test_entry_ids_unique_and_content_nonempty():
    ids = [entry.entry_id for entry in ATLAS_ENTRIES]
    assert len(ids) == len(set(ids))
    for entry in ATLAS_ENTRIES:
        assert entry.group in {"arm", "metric", "role", "governance"}
        assert entry.title and entry.one_liner and entry.body


def test_unknown_arm_id_passes_through_unchanged():
    assert arm_display_name("Z9") == "Z9"
    assert arm_algorithm_key("Z9") is None


def test_display_names_carry_no_arm_codes():
    for arm_id, name in ARM_NAMES.items():
        assert arm_id not in name
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_atlas.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'qlab.core.atlas'`

- [ ] **Step 3: Write the module**

```python
# qlab/core/atlas.py
"""Curated catalog of what the desk is made of, and the arm-id name map.

The prose here is the operator-facing explanation of every component; live
facts (champion policy, catalog stage, ablation numbers) are overlaid by the
owner server at request time, never stored here. Arm codes (B0…A4) stay the
machine keys in specs and the registry; every display surface goes through
``arm_display_name`` so operators read method names instead.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AtlasEntry:
    entry_id: str
    group: str                       # "arm" | "metric" | "role" | "governance"
    title: str                       # short display name, e.g. "HRP"
    one_liner: str
    body: str
    subtitle: str | None = None      # long-form name for the detail header
    arm_id: str | None = None        # ablation spec id, e.g. "B2"
    algorithm_key: str | None = None # catalog id, e.g. "hrp"


ATLAS_ENTRIES: tuple[AtlasEntry, ...] = (
    # -- research arms ----------------------------------------------------
    AtlasEntry(
        "b0", "arm", "60/40", "The institutional stock/bond benchmark.",
        "A fixed 60% stocks / 40% bonds split. No estimation, no "
        "optimization — the institutional default every arm must beat "
        "before it earns attention.",
        subtitle="Sixty/forty benchmark", arm_id="B0",
        algorithm_key="sixty_forty",
    ),
    AtlasEntry(
        "b1", "arm", "Equal weight", "1/N across the universe; famously hard to beat.",
        "Every asset gets the same weight. There are no estimated "
        "parameters, so nothing can be mis-estimated — which is exactly why "
        "naive 1/N routinely embarrasses sophisticated optimizers out of "
        "sample.",
        subtitle="Equal-weight benchmark", arm_id="B1",
        algorithm_key="equal_weight",
    ),
    AtlasEntry(
        "b2", "arm", "HRP", "Correlation clustering, then recursive risk allocation.",
        "Clusters assets by correlation and allocates risk down the cluster "
        "tree instead of inverting a covariance matrix, which makes it "
        "robust to estimation error. It is the current bar any candidate "
        "has to clear.",
        subtitle="Hierarchical risk parity", arm_id="B2", algorithm_key="hrp",
    ),
    AtlasEntry(
        "b3", "arm", "Equal risk contribution",
        "Every asset contributes the same share of portfolio risk.",
        "Weights are solved so each asset contributes equally to total "
        "portfolio volatility. The practitioner's risk-parity benchmark: "
        "sensitive to the covariance estimate, indifferent to expected "
        "returns.",
        subtitle="Equal risk contribution (risk parity)", arm_id="B3",
        algorithm_key="risk_parity",
    ),
    AtlasEntry(
        "a1", "arm", "Minimum variance", "Classical constrained min-variance baseline.",
        "Minimizes portfolio variance under long-only budget constraints. "
        "The classical baseline for estimation-error traps: without "
        "guardrails it concentrates into whatever looked calm in the "
        "sample.",
        subtitle="Classical minimum variance", arm_id="A1",
        algorithm_key="min_variance",
    ),
    AtlasEntry(
        "b4", "arm", "Regime min variance", "Min-variance on a stress-blended covariance.",
        "The same objective as minimum variance, but the covariance is "
        "blended toward the stressed-regime estimate when the deterministic "
        "regime signal says conditions deteriorated. Tests whether regime "
        "awareness helps a covariance-only policy.",
        subtitle="Regime-conditioned minimum variance", arm_id="B4",
        algorithm_key="regime_min_variance",
    ),
    AtlasEntry(
        "a2", "arm", "Scenario CVaR", "Optimizes the tail directly from historical scenarios.",
        "A Rockafellar–Uryasev linear program that minimizes expected loss "
        "in the worst tail of the historical scenario panel. The "
        "falsifiable rival to moment-based methods: it uses the return "
        "distribution directly instead of summarizing it.",
        subtitle="Scenario CVaR (Rockafellar–Uryasev LP)", arm_id="A2",
        algorithm_key="scenario_cvar",
    ),
    AtlasEntry(
        "a3", "arm", "MVSK", "Mean-variance-skew-kurtosis via classical multistart.",
        "Adds the third and fourth moments to the objective — penalize fat "
        "tails, reward positive skew. The objective claim under test: do "
        "higher moments improve the realized shape of returns after costs?",
        subtitle="Classical MVSK multistart", arm_id="A3",
        algorithm_key="mvsk_multistart",
    ),
    AtlasEntry(
        "a4", "arm", "MVSK (Dirac-3)", "The same MVSK objective on QCI's Dirac-3 solver.",
        "Identical objective to the classical MVSK arm, solved on the "
        "Dirac-3 continuous-HUBO hardware adapter. The solver claim under "
        "test: does the hardware find better optima than classical "
        "multistart? Requires QCI credentials.",
        subtitle="Dirac-3 MVSK solver adapter", arm_id="A4",
        algorithm_key="dirac3_mvsk",
    ),
    AtlasEntry(
        "a3t", "arm", "MVSK vol-target", "MVSK plus a de-risking overlay; research-only.",
        "MVSK with exposure scaled down when estimated volatility exceeds "
        "the target — the un-invested remainder stays in cash. It "
        "deliberately breaks the fully-invested mandate, so it can never "
        "reach the trader; it exists to measure the overlay's effect on "
        "realized volatility only.",
        subtitle="Volatility-targeted MVSK (research-only)", arm_id="A3t",
        algorithm_key="mvsk_vol_target",
    ),
    # -- metrics ----------------------------------------------------------
    AtlasEntry(
        "ann_return", "metric", "Annualized return",
        "The compounded return translated into a yearly rate.",
        "The compounded return translated into a yearly rate, so windows of "
        "different lengths can be compared on one scale.",
    ),
    AtlasEntry(
        "ann_vol", "metric", "Annualized volatility",
        "Typical variability of returns, scaled to a year.",
        "The typical variability of returns, scaled to a year. It measures "
        "movement, not specifically losses.",
    ),
    AtlasEntry(
        "sharpe", "metric", "Sharpe ratio", "Return divided by volatility.",
        "Annualized return divided by annualized volatility. This "
        "implementation subtracts no risk-free rate, so it is more "
        "precisely a return-to-volatility proxy.",
    ),
    AtlasEntry(
        "sortino", "metric", "Sortino ratio", "Return divided by downside volatility.",
        "Return divided by downside volatility, so upside movement is not "
        "penalized the way ordinary volatility penalizes it.",
    ),
    AtlasEntry(
        "max_drawdown", "metric", "Maximum drawdown",
        "The largest peak-to-trough decline.",
        "The largest peak-to-trough decline. If $10,000 rises to $12,000 "
        "and falls to $9,000, the drawdown from that peak is 25%.",
    ),
    AtlasEntry(
        "cvar_95", "metric", "CVaR 95%",
        "Average outcome among roughly the worst 5% of returns.",
        "The average outcome among approximately the worst 5% of returns — "
        "the severity of bad tail events, not just their frequency.",
    ),
    AtlasEntry(
        "realized_skew", "metric", "Realized skew",
        "The asymmetry actually observed in returns.",
        "The asymmetry actually observed in the portfolio's returns. "
        "Negative skew means the bad tail is the more severe one.",
    ),
    AtlasEntry(
        "realized_kurtosis", "metric", "Realized excess kurtosis",
        "How heavy the realized tails were versus normal.",
        "How heavy the realized tails were compared with a normal "
        "distribution. Zero is normal-like under the excess-kurtosis "
        "convention; positive values indicate more extreme observations.",
    ),
    AtlasEntry(
        "turnover", "metric", "Turnover", "How much the weights changed.",
        "How much the portfolio weights changed at rebalance. High turnover "
        "creates costs and operational burden.",
    ),
    AtlasEntry(
        "deflated_sharpe", "metric", "Deflated Sharpe",
        "Is the Sharpe still convincing after multiple testing?",
        "Asks whether an observed Sharpe is still convincing after "
        "accounting for the number of strategies tried, sample size, "
        "skewness and kurtosis. Reported as the probability that the true "
        "Sharpe exceeds zero.",
    ),
    AtlasEntry(
        "bootstrap_ci", "metric", "Bootstrap confidence interval",
        "A plausible range for a metric via block resampling.",
        "Repeatedly resamples blocks of historical returns to show a "
        "plausible range for a metric. Blocks are used because market "
        "returns are not independent through time.",
    ),
    # -- workforce roles --------------------------------------------------
    AtlasEntry(
        "moments-analyst", "role", "Moments analyst",
        "Chooses estimation window, shrinkage, and regime call.",
        "Reads the point-in-time price snapshot and five deterministic "
        "indicators — turbulence, absorption ratio, volatility term "
        "structure, drawdown, tail risk — then chooses an estimation "
        "window and shrinkage approach, constructs the numerical inputs, "
        "and records its reasoning. The indicators describe risk "
        "conditions; they do not predict which asset will rise. This is "
        "the primary judgment role.",
    ),
    AtlasEntry(
        "challenger", "role", "Challenger",
        "One adversarial case against the analyst's choices.",
        "Produces one adversarial case against the analyst's choices: is "
        "the window too long to represent current stress? Does a shorter "
        "window materially change covariance? Is one indicator "
        "contradicting the others? Are the conclusions sensitive to "
        "estimation assumptions? It runs concurrently with the optimizer — "
        "both depend on the analyst, not on each other.",
    ),
    AtlasEntry(
        "optimization-runner", "role", "Optimization runner",
        "Runs the configured operational policy; exercises no judgment.",
        "Runs the configured operational policy and produces exact target "
        "weights. It cannot substitute a different algorithm because it "
        "sounds more advanced — the staged catalog enforces that in code.",
    ),
    AtlasEntry(
        "referee", "role", "Referee", "The approval gate. A failed check blocks the run.",
        "Waits for both the optimizer and the challenger. Checks mandate "
        "compliance, data and point-in-time validity, that the algorithm "
        "is operational, that benchmarks were treated honestly, that the "
        "challenger exposed no unanswered serious weakness, and that the "
        "exact targets being approved are the ones produced. A failed "
        "referee phase blocks the workflow; a PASS is bound to the exact "
        "targets hash.",
    ),
    AtlasEntry(
        "reporter", "role", "Reporter",
        "Explains the result; may request a dry preview, never an order.",
        "Explains the result in human language. After a PASS it may "
        "request a checked dry-run preview, but it cannot submit an "
        "order — execution requires explicit human confirmation from the "
        "TUI.",
    ),
    # -- governance -------------------------------------------------------
    AtlasEntry(
        "proposal-gap", "governance", "Agents propose, human disposes",
        "A workforce run never moves the book.",
        "A workforce run produces a reviewed recommendation: the analyst "
        "judges, the challenger debates, the optimizer solves, the referee "
        "PASSes bound to exact targets, the reporter prepares a dry "
        "preview. Execution is a separate deliberate step — rebalance "
        "paper, the confirm modal, a human pressing execute. That gap is "
        "the design. The autopilot can book paper trades unattended; the "
        "interactive workforce path stops at the proposal.",
    ),
    AtlasEntry(
        "min-allocation", "governance", "Minimum-allocation constraint",
        "Why weights are forced to be real positions or zero.",
        "Two reasons. It prevents dust — a free solver hands assets 0.3% "
        "weights that cost turnover and change nothing, so min_weight "
        "forces a real position or zero. And it is a diversification "
        "floor: it stops the optimizer from collapsing into one or two "
        "'lowest-variance' names, the classic estimation-error trap where "
        "min-variance blows up out of sample. A robustness guardrail, not "
        "a return lever.",
    ),
    AtlasEntry(
        "the-method", "governance", "Where the edge comes from",
        "Risk estimation and risk shape, proven out of sample — not return forecasting.",
        "qlab deliberately does not chase return forecasting; past returns "
        "predict poorly and that is the classic overfit. The edge is on "
        "the risk side: better covariance and co-moment estimation "
        "(shrinkage, denoising, factor structure) so the optimizer is not "
        "fed garbage; risk-shaped objectives (variance, tails, skew, "
        "downside semivariance); regime conditioning so exposure drops in "
        "stress; and honest validation — deflated Sharpe, walk-forward, "
        "cost-aware gates — so only what survives out of sample is "
        "promoted. Candid current result: on the tested window the simple "
        "benchmarks (60/40, HRP) still beat the fancier MVSK arms out of "
        "sample, and that is recorded rather than hidden.",
    ),
)

ARM_NAMES: dict[str, str] = {
    entry.arm_id: entry.title
    for entry in ATLAS_ENTRIES if entry.arm_id is not None
}

_ARM_ALGORITHMS: dict[str, str | None] = {
    entry.arm_id: entry.algorithm_key
    for entry in ATLAS_ENTRIES if entry.arm_id is not None
}


def arm_display_name(arm_id: str) -> str:
    """Method name for an arm id; unknown ids pass through as-is."""
    return ARM_NAMES.get(str(arm_id), str(arm_id))


def arm_algorithm_key(arm_id: str) -> str | None:
    return _ARM_ALGORITHMS.get(str(arm_id))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_atlas.py -q`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add qlab/core/atlas.py tests/test_atlas.py
git commit -m "feat(atlas): curated component catalog with arm display names"
```

---

### Task 2: `equity_marks` registry table

**Files:**
- Modify: `qlab/state/registry.py` (`_SCHEMA` string ~line 104; new methods next to `list_orders` ~line 823)
- Test: `tests/test_registry.py`

**Interfaces:**
- Produces: `Registry.log_equity_mark(ts: str, equity: float, cash: float | None, source: str) -> bool` (True if inserted, False if duplicate), `Registry.equity_marks(limit: int = 5000) -> list[dict]` (ascending by ts; keys `ts, source, equity, cash`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_registry.py`:

```python
def test_equity_marks_are_idempotent_and_ordered():
    reg = Registry(":memory:")
    assert reg.log_equity_mark(
        "2026-06-02T21:00:00+00:00", 10_050.0, cash=500.0, source="daily")
    assert reg.log_equity_mark(
        "2026-06-01T21:00:00+00:00", 10_000.0, cash=500.0, source="daily")
    # Same (ts, source) is a silent no-op that keeps the first value.
    assert not reg.log_equity_mark(
        "2026-06-01T21:00:00+00:00", 99.0, cash=0.0, source="daily")
    # A different source at the same instant is a distinct observation.
    assert reg.log_equity_mark(
        "2026-06-01T21:00:00+00:00", 10_000.0, cash=None, source="alpaca_backfill")
    marks = reg.equity_marks()
    assert [m["equity"] for m in marks if m["source"] == "daily"] == [10_000.0, 10_050.0]
    assert marks[0]["ts"] == "2026-06-01T21:00:00+00:00"
```

(Match the module's existing import style — it already imports `Registry`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_registry.py -q -k equity_marks`
Expected: FAIL with `AttributeError: 'Registry' object has no attribute 'log_equity_mark'`

- [ ] **Step 3: Implement**

Add to `_SCHEMA` (before the closing `"""`):

```sql
CREATE TABLE IF NOT EXISTS equity_marks (
    ts VARCHAR, source VARCHAR, equity DOUBLE, cash DOUBLE,
    PRIMARY KEY (ts, source));
```

Add methods near the other book accessors (after `list_orders`):

```python
def log_equity_mark(self, ts: str, equity: float, cash: float | None,
                    source: str) -> bool:
    """One equity observation per (ts, source); duplicates are no-ops."""
    row = self.con.execute(
        "INSERT OR IGNORE INTO equity_marks (ts, source, equity, cash) "
        "VALUES (?, ?, ?, ?)",
        [str(ts), str(source), float(equity),
         None if cash is None else float(cash)],
    ).fetchone()
    return bool(row and row[0])

def equity_marks(self, limit: int = 5000) -> list[dict]:
    rows = self._rows(
        "SELECT ts, source, equity, cash FROM equity_marks "
        "ORDER BY ts DESC LIMIT ?", [int(limit)])
    return list(reversed(rows))
```

DuckDB returns the inserted-row count as the DML result; if the installed DuckDB version returns a different shape, adapt `log_equity_mark`'s return derivation until the test passes — the test is the contract, do not weaken it. Check `_rows`'s exact signature (used at ~line 726) and match it.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_registry.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add qlab/state/registry.py tests/test_registry.py
git commit -m "feat(registry): idempotent equity_marks table for realized performance"
```

---

### Task 3: Broker P&L + Alpaca portfolio history

**Files:**
- Modify: `qlab/trader/broker.py` (`SimulatedPaperBroker.portfolio_state` ~line 75; `AlpacaPaperBroker` ~line 116)
- Test: `tests/test_trader.py`

**Interfaces:**
- Produces: every position dict gains `"unrealized_pl": float`; `AlpacaPaperBroker.portfolio_history(period="1M", timeframe="1D") -> list[dict]` with rows `{"ts": iso8601-utc, "equity": float}`. `SimulatedPaperBroker` deliberately has **no** `portfolio_history` (the server hasattr-gates on it and refuses loudly).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_trader.py` (reuse its existing fixtures/imports for `Registry` and `SimulatedPaperBroker`):

```python
def test_simulated_positions_carry_unrealized_pl():
    reg = Registry(":memory:")
    broker = SimulatedPaperBroker(
        reg, price_provider=lambda tickers: {t: 110.0 for t in tickers},
        starting_cash=10_000.0)
    reg.apply_fill("ACWI", 10.0, 100.0, -1_000.0)  # bought at 100, marked 110
    state = broker.portfolio_state(["ACWI"])
    assert state["positions"]["ACWI"]["unrealized_pl"] == pytest.approx(100.0)


def test_simulated_broker_has_no_portfolio_history():
    reg = Registry(":memory:")
    broker = SimulatedPaperBroker(reg)
    assert not hasattr(broker, "portfolio_history")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_trader.py -q -k "unrealized or portfolio_history"`
Expected: first test FAILS with `KeyError: 'unrealized_pl'`; second passes already

- [ ] **Step 3: Implement**

`SimulatedPaperBroker.portfolio_state` — the holdings loop becomes:

```python
for t, p in positions.items():
    price = px.get(t, p["avg_price"])
    value = p["qty"] * price
    pos_value += value
    holdings[t] = {"qty": p["qty"], "price": price, "value": value,
                   "unrealized_pl": (price - p["avg_price"]) * p["qty"]}
```

`AlpacaPaperBroker.portfolio_state` — extend the positions comprehension:

```python
positions = {p.symbol: {"qty": float(p.qty), "price": float(p.current_price),
                        "value": float(p.market_value),
                        "unrealized_pl": float(p.unrealized_pl)}
             for p in self.trading.get_all_positions()}
```

Add to `AlpacaPaperBroker` (after `submit_notional`):

```python
def portfolio_history(self, period: str = "1M",
                      timeframe: str = "1D") -> list[dict]:
    """Account equity history from Alpaca, oldest first, UTC ISO stamps."""
    from datetime import datetime, timezone

    from alpaca.trading.requests import GetPortfolioHistoryRequest

    history = self.trading.get_portfolio_history(
        GetPortfolioHistoryRequest(period=period, timeframe=timeframe))
    rows = []
    for stamp, equity in zip(history.timestamp, history.equity):
        if equity is None:
            continue
        rows.append({
            "ts": datetime.fromtimestamp(int(stamp), tz=timezone.utc).isoformat(),
            "equity": float(equity),
        })
    return rows
```

Verify `get_portfolio_history` / `GetPortfolioHistoryRequest` against the installed `alpaca-py` version (`python -c "from alpaca.trading.requests import GetPortfolioHistoryRequest"`). If the installed version names them differently, use its names — and if the capability is absent, raise `RuntimeError` naming the version rather than returning an empty list.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_trader.py -q`
Expected: all pass (Alpaca path is import-guarded; no network in tests)

- [ ] **Step 5: Commit**

```bash
git add qlab/trader/broker.py tests/test_trader.py
git commit -m "feat(trader): position unrealized P&L and Alpaca portfolio history"
```

---

### Task 4: Owner server — performance payload, mark writers, backfill

**Files:**
- Modify: `qlab/ui/server.py` (`UISession` methods near `portfolio()` ~line 337; `tui_snapshot` ~line 646; `handle_api` route chain ~lines 839–961; `UISession.__init__` gains `self._last_poll_mark = 0.0`)
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: `Registry.log_equity_mark` / `Registry.equity_marks` (Task 2), `portfolio_history` (Task 3), `compute_metrics(returns: pd.Series) -> dict` from `qlab/core/metrics.py` (keys `ann_return, ann_vol, sharpe, sortino, max_drawdown, cvar_95, realized_skew, realized_kurtosis, deflated_sharpe, n_obs`; only `n_obs` when < 3 observations).
- Produces: `UISession.performance(offline) -> dict` with keys `series` (list of `{"ts": "YYYY-MM-DD", "equity": float}`), `metrics` (dict | None), `since_start` (float | None), `note` (str | None), `marks` (int); `UISession.record_equity_mark(source, offline)`; `UISession.backfill_equity_history(offline) -> {"backfilled": int}` (raises `RuntimeError` when the broker has no history); routes `GET /api/performance`, `POST /api/performance/backfill`; `tui_snapshot` gains `"performance"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ui.py`, reusing the module's existing `UISession`/`handle_api` construction helper if one exists (otherwise define `_session()` as below):

```python
def _session():
    return UISession(offline_default=True, registry=Registry(":memory:"))


def test_performance_payload_from_synthetic_marks():
    session = _session()
    equity = 10_000.0
    for day in range(1, 31):
        equity *= 1.001
        session.registry.log_equity_mark(
            f"2026-06-{day:02d}T21:00:00+00:00", equity, cash=500.0,
            source="daily")
    status, payload = handle_api(session, "GET", "/api/performance", {}, {})
    assert status == 200
    assert len(payload["series"]) == 30
    assert payload["metrics"]["sharpe"] > 0
    assert payload["since_start"] > 0
    assert payload["note"] is None


def test_performance_is_honest_about_insufficient_history():
    session = _session()
    session.registry.log_equity_mark(
        "2026-06-01T21:00:00+00:00", 10_000.0, cash=None, source="daily")
    status, payload = handle_api(session, "GET", "/api/performance", {}, {})
    assert status == 200
    assert payload["metrics"] is None
    assert "insufficient" in payload["note"]


def test_backfill_refuses_without_history_capable_broker():
    session = _session()
    status, payload = handle_api(
        session, "POST", "/api/performance/backfill", {}, {})
    assert status == 400
    assert "portfolio history" in payload["error"]


def test_backfill_merges_alpaca_history_idempotently(monkeypatch):
    session = _session()

    class StubBroker:
        name = "alpaca_paper"

        def portfolio_history(self):
            return [
                {"ts": "2026-06-01T20:00:00+00:00", "equity": 10_000.0},
                {"ts": "2026-06-02T20:00:00+00:00", "equity": 10_050.0},
            ]

    monkeypatch.setattr(
        "qlab.trader.broker.get_broker", lambda *args, **kwargs: StubBroker())
    status, payload = handle_api(
        session, "POST", "/api/performance/backfill", {}, {})
    assert (status, payload["backfilled"]) == (200, 2)
    status, payload = handle_api(
        session, "POST", "/api/performance/backfill", {}, {})
    assert payload["backfilled"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ui.py -q -k "performance or backfill"`
Expected: FAIL — `/api/performance` route unknown (assert on status)

- [ ] **Step 3: Implement**

`UISession` methods (place after `portfolio()`):

```python
def record_equity_mark(self, source: str, offline: bool) -> None:
    from datetime import datetime, timezone

    state = self.portfolio(offline)
    self.registry.log_equity_mark(
        datetime.now(timezone.utc).isoformat(),
        state["equity"], cash=state["cash"], source=source)

def performance(self, offline: bool) -> dict:
    import pandas as pd

    from qlab.core.metrics import compute_metrics

    rows = self.registry.equity_marks()
    if not rows:
        return {"series": [], "metrics": None, "since_start": None,
                "note": "no equity history yet", "marks": 0}
    frame = pd.DataFrame(rows)
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True, format="ISO8601")
    daily = (frame.set_index("ts").sort_index()["equity"]
             .resample("1D").last().dropna())
    series = [{"ts": stamp.date().isoformat(), "equity": float(value)}
              for stamp, value in daily.tail(365).items()]
    returns = daily.pct_change().dropna()
    metrics = compute_metrics(returns) if len(returns) >= 3 else None
    if metrics is not None and "sharpe" not in metrics:
        metrics = None
    start = float(daily.iloc[0])
    since_start = float(daily.iloc[-1] / start - 1.0) if start > 0 else None
    note = (None if metrics is not None
            else "insufficient history for realized metrics (need >=4 daily marks)")
    return {"series": series, "metrics": metrics, "since_start": since_start,
            "note": note, "marks": len(rows)}

def backfill_equity_history(self, offline: bool) -> dict:
    from qlab.trader.broker import get_broker

    broker = get_broker(
        self.registry, offline=offline,
        starting_cash=self.mandate.paper_capital, seed=self.seed,
        universe=self.mandate.universe_whitelist)
    if not hasattr(broker, "portfolio_history"):
        raise RuntimeError(
            f"broker {broker.name!r} exposes no portfolio history to backfill")
    inserted = sum(
        self.registry.log_equity_mark(
            row["ts"], row["equity"], cash=None, source="alpaca_backfill")
        for row in broker.portfolio_history())
    return {"backfilled": int(inserted)}
```

`tui_snapshot`: add `"performance": self.performance(offline),` to the returned dict, and before building it add the throttled poll mark (constraint comment included — a 2s poll must not spam the table):

```python
# One poll-sourced mark an hour keeps intraday granularity while the TUI
# is open without turning the 2s refresh into 43k rows a day.
if time.time() - self._last_poll_mark > 3600.0:
    self._last_poll_mark = time.time()
    self.record_equity_mark("poll", offline)
```

(`import time` at module top if absent; `self._last_poll_mark = 0.0` in `__init__`.)

`handle_api` additions, matching the existing route style:

```python
if method == "GET" and path == "/api/performance":
    return 200, session.performance(off)
```

```python
if method == "POST" and path == "/api/performance/backfill":
    try:
        return 200, session.backfill_equity_history(off)
    except RuntimeError as exc:
        return 400, {"error": str(exc)}
```

And two one-line hooks in existing routes — after `session.execute_checked_plan(body, off)` succeeds, and after `daily_ops(...)` returns:

```python
result = session.execute_checked_plan(body, off)
session.record_equity_mark("execution", off)
return 200, result
```

```python
summary = daily_ops(registry=session.registry, mandate=session.mandate,
                    offline=off, seed=session.seed)
session.record_equity_mark("daily", off)
return 200, summary
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_ui.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add qlab/ui/server.py tests/test_ui.py
git commit -m "feat(ui): performance payload, equity mark writers, alpaca backfill"
```

---

### Task 5: Owner server — atlas payload and ablation leaderboard

**Files:**
- Modify: `qlab/ui/server.py` (new `UISession` methods; `tui_snapshot`; one `GET` route)
- Test: `tests/test_ui.py`

**Interfaces:**
- Consumes: `ATLAS_ENTRIES`, `arm_display_name`, `arm_algorithm_key` (Task 1); `Registry.list_runs`, `Registry.report(run_id) -> {"backtests": [{"arm_id", "metrics"}, ...]}`; `list_algorithms()` rows with `id`/`stage`/`category`; `self.mandate.operational_policy`.
- Produces: `UISession.atlas() -> {"entries": [...], "champion_policy": str}` where each entry dict = dataclass fields + `stage` (str | None), `champion` (bool), `ablation` (dict | None); `UISession.latest_ablation_metrics() -> dict[str, dict]`; `UISession.leaderboard() -> list[dict]` rows `{"arm_id", "name", "champion", "benchmark", "sharpe", "ann_return", "max_drawdown", "cvar_95", "deflated_sharpe"}` sorted by sharpe desc; route `GET /api/atlas`; `tui_snapshot` gains `"leaderboard"`.

- [ ] **Step 1: Write the failing tests**

```python
def test_atlas_marks_champion_and_reports_absent_ablation():
    session = _session()
    status, payload = handle_api(session, "GET", "/api/atlas", {}, {})
    assert status == 200
    entries = payload["entries"]
    champions = [e for e in entries if e["champion"]]
    assert [e["algorithm_key"] for e in champions] == [
        session.mandate.operational_policy]
    by_id = {e["entry_id"]: e for e in entries}
    assert by_id["b2"]["stage"] == "operational"
    assert by_id["a3t"]["stage"] == "research"
    assert by_id["b2"]["ablation"] is None      # empty registry: explicit absence
    assert by_id["sharpe"]["stage"] is None     # metrics carry no stage


def test_leaderboard_reports_method_names_not_codes():
    session = _session()
    run_id = session.registry.log_run("ablation", {"note": "test"})
    session.registry.log_backtest(run_id, "B2", {
        "sharpe": 0.91, "ann_return": 0.062, "max_drawdown": -0.124,
        "cvar_95": -0.011, "deflated_sharpe": 0.83})
    session.registry.log_backtest(run_id, "B0", {
        "sharpe": 0.55, "ann_return": 0.050, "max_drawdown": -0.180,
        "cvar_95": -0.015, "deflated_sharpe": 0.60})
    rows = session.leaderboard()
    assert [row["name"] for row in rows] == ["HRP", "60/40"]
    assert rows[0]["champion"] and not rows[1]["champion"]
    assert rows[1]["benchmark"]
```

(Check `Registry.log_backtest`'s exact signature at `qlab/state/registry.py:249` and pass `artifact_hash` if it is required positionally.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_ui.py -q -k "atlas or leaderboard"`
Expected: FAIL — route unknown / `leaderboard` attribute missing

- [ ] **Step 3: Implement**

```python
def latest_ablation_metrics(self) -> dict[str, dict]:
    """Per-arm metrics from the newest run that recorded backtests."""
    runs = sorted(self.registry.list_runs(200),
                  key=lambda run: str(run.get("created_at", "")), reverse=True)
    for run in runs:
        backtests = self.registry.report(run["run_id"]).get("backtests") or []
        if backtests:
            return {bt["arm_id"]: bt["metrics"] for bt in backtests}
    return {}

def atlas(self) -> dict:
    from dataclasses import asdict

    from qlab.algorithms import list_algorithms
    from qlab.core.atlas import ATLAS_ENTRIES

    catalog = {row["id"]: row for row in list_algorithms()}
    champion = self.mandate.operational_policy
    ablation = self.latest_ablation_metrics()
    entries = []
    for entry in ATLAS_ENTRIES:
        row = asdict(entry)
        spec = catalog.get(entry.algorithm_key) if entry.algorithm_key else None
        row["stage"] = spec["stage"] if spec else None
        row["champion"] = bool(
            entry.group == "arm" and entry.algorithm_key == champion)
        row["ablation"] = (
            ablation.get(entry.arm_id) if entry.arm_id else None)
        entries.append(row)
    return {"entries": entries, "champion_policy": champion}

def leaderboard(self) -> list[dict]:
    from qlab.algorithms import list_algorithms
    from qlab.core.atlas import arm_algorithm_key, arm_display_name

    catalog = {row["id"]: row for row in list_algorithms()}
    champion = self.mandate.operational_policy
    rows = []
    for arm_id, metrics in self.latest_ablation_metrics().items():
        key = arm_algorithm_key(arm_id)
        spec = catalog.get(key) if key else None
        rows.append({
            "arm_id": arm_id,
            "name": arm_display_name(arm_id),
            "champion": bool(key == champion),
            "benchmark": bool(spec and spec["category"] == "benchmark"),
            "sharpe": metrics.get("sharpe"),
            "ann_return": metrics.get("ann_return"),
            "max_drawdown": metrics.get("max_drawdown"),
            "cvar_95": metrics.get("cvar_95"),
            "deflated_sharpe": metrics.get("deflated_sharpe"),
        })
    rows.sort(key=lambda row: -(row["sharpe"] if row["sharpe"] is not None else float("-inf")))
    return rows
```

Route (`GET` section of `handle_api`):

```python
if method == "GET" and path == "/api/atlas":
    return 200, session.atlas()
```

`tui_snapshot`: add `"leaderboard": self.leaderboard(),`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_ui.py tests/test_atlas.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add qlab/ui/server.py tests/test_ui.py
git commit -m "feat(ui): atlas payload with champion overlay and ablation leaderboard"
```

---

### Task 6: Connection staleness chip

**Files:**
- Modify: `qlab/tui/formatting.py` (new pure function), `qlab/tui/app.py` (compose `command-row` ~line 954; `__init__`; `_apply_snapshot`; `_start_refresh` failure path ~line 1020; `_tick_pulse`), `qlab/tui/theme.py` (`APP_CSS`)
- Test: `tests/test_tui.py`

**Interfaces:**
- Produces: `connection_chip(age_seconds: float | None, failures: int) -> tuple[str, str]` returning `(text, level)` with level in `{"ok", "warn", "down"}`; a `#conn-chip` Static in the command row.

- [ ] **Step 1: Write the failing tests**

```python
def test_connection_chip_states():
    from qlab.tui.formatting import connection_chip

    assert connection_chip(None, 0) == ("CONNECTING", "warn")
    assert connection_chip(2.0, 0) == ("LIVE", "ok")
    assert connection_chip(75.0, 1) == ("STALE 1:15", "warn")
    assert connection_chip(None, 3) == ("OWNER DOWN", "down")
    text, level = connection_chip(120.0, 3)
    assert level == "down" and "OWNER DOWN" in text


def test_owner_failure_surfaces_in_conn_chip():
    from qlab.tui.app import QlabTui

    class FailingClient(StubClient):
        def get(self, path, **params):
            raise RuntimeError("owner gone")

    async def run():
        app = QlabTui(FailingClient(), refresh_interval=0.05)
        async with app.run_test(size=(160, 42)) as pilot:
            await pilot.pause(0.6)
            chip = str(app.query_one("#conn-chip").content)
            assert "OWNER DOWN" in chip

    asyncio.run(run())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_tui.py -q -k conn`
Expected: FAIL with `ImportError` on `connection_chip`

- [ ] **Step 3: Implement**

`qlab/tui/formatting.py`:

```python
def connection_chip(age_seconds: float | None, failures: int) -> tuple[str, str]:
    """(text, level) for snapshot freshness; level is ok | warn | down.

    Three consecutive refresh failures mean the owner is gone, not merely a
    slow request — one timeout must not scream OWNER DOWN during an action.
    """
    if failures >= 3:
        if age_seconds is None:
            return "OWNER DOWN", "down"
        return f"OWNER DOWN · last {int(age_seconds)}s", "down"
    if age_seconds is None:
        return "CONNECTING", "warn"
    if age_seconds > 10:
        minutes, seconds = divmod(int(age_seconds), 60)
        return f"STALE {minutes}:{seconds:02d}", "warn"
    return "LIVE", "ok"
```

`app.py` wiring:
1. `__init__`: `self._last_snapshot_at: float | None = None` and `self._refresh_failures = 0`.
2. Compose (`command-row`): before `#system-status`, `yield Static("CONNECTING", id="conn-chip", markup=True)`.
3. `_apply_snapshot` (top): `self._last_snapshot_at = time.monotonic(); self._refresh_failures = 0; self._render_conn_chip()`.
4. `_start_refresh`'s `except` branch: also `self.call_from_thread(self._note_refresh_failure)` where

```python
def _note_refresh_failure(self) -> None:
    self._refresh_failures += 1
    self._render_conn_chip()

def _render_conn_chip(self) -> None:
    age = (None if self._last_snapshot_at is None
           else time.monotonic() - self._last_snapshot_at)
    text, level = connection_chip(age, self._refresh_failures)
    tone = {"ok": UP, "warn": AMBER, "down": DOWN}[level]
    self.query_one("#conn-chip", Static).update(f"[{tone}]{text}[/]")
```

5. `_tick_pulse` (already on a 0.25s interval): call `self._render_conn_chip()` so the STALE age ticks.
6. Import `connection_chip` in `app.py` alongside the other `formatting` imports.

`theme.py` `APP_CSS`: `#conn-chip { width: auto; padding: 0 1; }` styled consistently with `#system-status`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_tui.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add qlab/tui/formatting.py qlab/tui/app.py qlab/tui/theme.py tests/test_tui.py
git commit -m "feat(tui): connection staleness chip in the command row"
```

---

### Task 7: Book view — equity section and position P&L

**Files:**
- Modify: `qlab/tui/app.py` (compose book view ~line 881; `_render_book` ~line 2080), `tests/test_tui.py` (`_snapshot()` fixture gains `performance` + `unrealized_pl`)
- Test: `tests/test_tui.py`

**Interfaces:**
- Consumes: snapshot keys `performance` (Task 4 shape) and `positions[t]["unrealized_pl"]` (Task 3); `braille_chart(values, width, height) -> list[str]`, `sparkline`, `money`, `pct` from `qlab/tui/formatting.py`.

- [ ] **Step 1: Update the fixture and write the failing test**

In `_snapshot()` add `"unrealized_pl": 40.0` (ACWI), `-15.0` (BNDW), `29.4` (GLD) to each position dict, and a top-level key:

```python
"performance": {
    "series": [
        {"ts": f"2026-06-{day:02d}", "equity": 10_000.0 * (1.001 ** day)}
        for day in range(1, 31)
    ],
    "metrics": {"ann_return": 0.041, "ann_vol": 0.082, "sharpe": 0.50,
                "sortino": 0.71, "max_drawdown": -0.021, "cvar_95": -0.006,
                "realized_skew": -0.2, "realized_kurtosis": 0.8,
                "deflated_sharpe": 0.6, "n_obs": 29},
    "since_start": 0.0124, "note": None, "marks": 30,
},
```

Test:

```python
def test_book_renders_equity_curve_metrics_and_position_pnl():
    from qlab.tui.app import QlabTui

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0)
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("5")
            equity_panel = str(app.query_one("#book-equity").content)
            assert "sharpe" in equity_panel
            assert "+1.2% since start" in equity_panel
            positions = str(app.query_one("#book-positions").content)
            assert "P&L" in positions

    asyncio.run(run())


def test_book_is_honest_when_no_equity_history():
    from qlab.tui.app import QlabTui

    class NoHistoryClient(StubClient):
        def get(self, path, **params):
            snapshot = super().get(path, **params)
            if path == "/api/tui":
                snapshot["performance"] = {
                    "series": [], "metrics": None, "since_start": None,
                    "note": "no equity history yet", "marks": 0}
            return snapshot

    async def run():
        app = QlabTui(NoHistoryClient(), refresh_interval=0)
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("5")
            panel = str(app.query_one("#book-equity").content)
            assert "No equity history yet" in panel

    asyncio.run(run())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_tui.py -q -k book`
Expected: FAIL — no `#book-equity` widget

- [ ] **Step 3: Implement**

Compose — insert before the `"POSITIONS"` title in the book view:

```python
yield Static("EQUITY", classes="book-section-title")
yield Static(id="book-equity", classes="book-section", markup=True)
```

`_render_book` — prepend:

```python
performance = self.snapshot.get("performance") or {}
series = [row["equity"] for row in performance.get("series") or []]
equity_lines: list[str] = []
if series:
    since = performance.get("since_start")
    header = f"[bold {TEXT_HI}]{money(portfolio.get('equity'))}[/]"
    if since is not None:
        tone = UP if since >= 0 else DOWN
        header += f"   [{tone}]{pct(since):+}%"[:0] or ""  # see note below
    equity_lines.append(header)
    equity_lines.extend(braille_chart(series, width=56, height=4))
    metrics = performance.get("metrics")
    if metrics:
        equity_lines.append(
            f"[{LABEL_GOLD}]ret[/] {pct(metrics['ann_return'])}  "
            f"[{LABEL_GOLD}]vol[/] {pct(metrics['ann_vol'])}  "
            f"[{LABEL_GOLD}]sharpe[/] {metrics['sharpe']:.2f}  "
            f"[{LABEL_GOLD}]maxdd[/] {pct(metrics['max_drawdown'])}  "
            f"[{LABEL_GOLD}]cvar95[/] {pct(metrics['cvar_95'])}")
    else:
        equity_lines.append(f"[{MUTED}]{performance.get('note') or ''}[/]")
else:
    equity_lines.append(
        f"[{MUTED}]No equity history yet — marks are recorded by daily ops, "
        f"executions, and hourly polls.[/]")
self.query_one("#book-equity", Static).update("\n".join(equity_lines))
```

For the header's since-start segment use the repo's `pct` helper verbatim (check its output format at `formatting.py:204` first) and render e.g. `+1.2% since start` with `UP`/`DOWN` tone — the exact expression must match what `pct` produces; adjust the test's expected string to the helper's real formatting rather than weakening it to a substring that would pass on garbage.

Position rows — extend header and rows:

```python
position_lines = [
    f"[{DIM}]TICKER   WEIGHT        QUANTITY          VALUE          P&L[/]"
]
```

and per row, after the value column:

```python
pl = position.get("unrealized_pl")
pl_text = "—" if pl is None else money(pl)
pl_tone = MUTED if pl is None else (UP if pl >= 0 else DOWN)
# append: f"   [{pl_tone}]{pl_text:>10}[/]"
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_tui.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add qlab/tui/app.py tests/test_tui.py
git commit -m "feat(tui): book equity curve, realized metrics, position P&L"
```

---

### Task 8: Atlas master-detail view

**Files:**
- Create: `qlab/tui/atlas_view.py`
- Modify: `qlab/tui/app.py` (`_VIEWS` ~line 71; command map ~line 209; `BINDINGS` ~line 714; compose ContentSwitcher ~line 814; `action_view` ~line 2516 for lazy fetch), `qlab/tui/theme.py` (`APP_CSS`), `tests/test_tui.py` (`StubClient` answers `/api/atlas`)
- Test: `tests/test_tui.py`

**Interfaces:**
- Consumes: `GET /api/atlas` payload (Task 5 shape).
- Produces: `AtlasView(Vertical)` widget with `set_entries(entries: list[dict]) -> None`; view id `"atlas"`; nav order `dashboard, market, workforce, research, book, audit, atlas, settings` (settings stays last; keys: atlas=7, settings moves to 8).

- [ ] **Step 1: Update StubClient and write the failing test**

`StubClient.get` must answer `"/api/atlas"` with a real-shaped payload — build it from the content module so the fixture cannot drift:

```python
if path == "/api/atlas":
    from dataclasses import asdict

    from qlab.core.atlas import ATLAS_ENTRIES

    entries = []
    for entry in ATLAS_ENTRIES:
        row = asdict(entry)
        row["stage"] = "operational" if entry.algorithm_key == "hrp" else None
        row["champion"] = entry.algorithm_key == "hrp"
        row["ablation"] = (
            {"sharpe": 0.91, "max_drawdown": -0.124}
            if entry.arm_id == "B2" else None)
        entries.append(row)
    return {"entries": entries, "champion_policy": "hrp"}
```

Test:

```python
def test_atlas_view_leads_with_method_names_and_marks_champion():
    from qlab.tui.app import QlabTui

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0)
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("7")
            assert app.active_view == "atlas"
            await pilot.pause(0.3)
            list_text = str(app.query_one("#atlas-list").render())
            detail = str(app.query_one("#atlas-detail").content)
            # First arm renders in the detail pane by default.
            assert "60/40" in detail
            # Champion is starred in the list without exposing the arm code.
            assert "★" in list_text or "HRP" in list_text
            # Codes appear only as the dim footnote, never in titles.
            assert "ablation id: B0" in detail

    asyncio.run(run())
```

(If `ListView.render()` proves awkward to stringify, assert on the labels of `app.query_one("#atlas-list").children` instead — the contract is: names visible, champion starred, codes only in the footnote.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tui.py -q -k atlas`
Expected: FAIL — pressing "7" lands on settings, no atlas view exists

- [ ] **Step 3: Implement the widget**

```python
# qlab/tui/atlas_view.py
"""Master-detail catalog view: what the desk is made of, champion marked."""

from __future__ import annotations

from rich.markup import escape
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Label, ListItem, ListView, Static

from qlab.tui.theme import AMBER, DIM, LABEL_GOLD, MUTED, TEXT, TEXT_HI

_GROUP_TITLES = (
    ("arm", "RESEARCH ARMS"),
    ("metric", "METRICS"),
    ("role", "WORKFORCE ROLES"),
    ("governance", "GOVERNANCE"),
)


class AtlasView(Vertical):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # Row index -> entry dict; None rows are group headers.
        self._row_entries: list[dict | None] = []

    def compose(self):
        yield Static(f"[{AMBER}]▍[/] ATLAS", classes="canvas-title",
                     markup=True)
        with Horizontal(id="atlas-split"):
            yield ListView(id="atlas-list")
            yield VerticalScroll(
                Static(
                    f"[{MUTED}]Waiting for the owner's atlas payload…[/]",
                    id="atlas-detail", markup=True),
                id="atlas-detail-scroll")

    def set_entries(self, entries: list[dict]) -> None:
        view = self.query_one("#atlas-list", ListView)
        view.clear()
        self._row_entries = []
        items = []
        for group, group_title in _GROUP_TITLES:
            members = [e for e in entries if e.get("group") == group]
            if not members:
                continue
            items.append(ListItem(
                Label(f"[{DIM}]{group_title}[/]", markup=True),
                disabled=True))
            self._row_entries.append(None)
            for entry in members:
                star = f" [{AMBER}]★[/]" if entry.get("champion") else ""
                items.append(ListItem(Label(
                    f"[{TEXT}]{escape(entry['title'])}[/]{star}",
                    markup=True)))
                self._row_entries.append(entry)
        view.extend(items)
        first = next((e for e in self._row_entries if e), None)
        if first:
            self._render_detail(first)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        index = event.list_view.index
        if index is None or index >= len(self._row_entries):
            return
        entry = self._row_entries[index]
        if entry:
            self._render_detail(entry)

    def _render_detail(self, entry: dict) -> None:
        title = f"[bold {TEXT_HI}]{escape(entry['title'])}[/]"
        if entry.get("subtitle"):
            title += f"  [{MUTED}]{escape(entry['subtitle'])}[/]"
        if entry.get("champion"):
            title += f"  [{AMBER}]★ CHAMPION[/]"
        parts = [title]
        if entry.get("stage"):
            parts.append(f"[{LABEL_GOLD}]stage[/] [{TEXT}]{entry['stage']}[/]")
        parts.extend(["", f"[{TEXT}]{escape(entry['body'])}[/]"])
        if entry.get("group") == "arm":
            parts.append("")
            ablation = entry.get("ablation")
            if ablation:
                cells = "  ".join(
                    f"[{LABEL_GOLD}]{key}[/] {value:.3f}"
                    for key, value in sorted(ablation.items())
                    if isinstance(value, (int, float)))
                parts.append(f"latest ablation  {cells}")
            else:
                parts.append(f"[{MUTED}]no ablation recorded for this arm yet[/]")
        if entry.get("arm_id"):
            parts.extend(["", f"[{DIM}]ablation id: {entry['arm_id']}[/]"])
        self.query_one("#atlas-detail", Static).update("\n".join(parts))
```

(Verify the theme token names against the imports at the top of `app.py` and use exactly those; verify `ListView.extend` exists in the pinned Textual version — otherwise mount items via `view.mount(*items)`.)

- [ ] **Step 4: Wire it into the app**

1. `_VIEWS = ("dashboard", "market", "workforce", "research", "book", "audit", "atlas", "settings")`.
2. Command map: add `("view", "atlas"): "action_view",`.
3. `BINDINGS`: atlas gets `"7"`/`"f7"`; settings moves to `"8"`/`"f8"`.
4. Compose, inside the ContentSwitcher after the audit view: `yield AtlasView(id="atlas", classes="canvas-view")` (import `AtlasView` at top).
5. Lazy fetch, `_start_bootstrap` pattern — in `action_view`, when `view == "atlas"` call:

```python
def _start_atlas_fetch(self) -> None:
    if self._atlas_started:
        return
    self._atlas_started = True

    def run() -> None:
        try:
            payload = self.client.get("/api/atlas")
            self.call_from_thread(self._finish_atlas, payload, "")
        except Exception as exc:
            self.call_from_thread(self._finish_atlas, None, repr(exc))

    threading.Thread(target=run, daemon=True).start()

def _finish_atlas(self, payload: dict | None, error: str) -> None:
    view = self.query_one("#atlas", AtlasView)
    if payload is None:
        self.query_one("#atlas-detail", Static).update(
            f"[{DOWN}]atlas unavailable: {escape(error)}[/]")
        self._atlas_started = False  # allow retry on next visit
        return
    view.set_entries(payload.get("entries") or [])
```

(`self._atlas_started = False` in `__init__`.)

6. `theme.py` `APP_CSS`:

```css
#atlas-split { height: 1fr; }
#atlas-list { width: 34; }
#atlas-detail-scroll { padding: 0 2; }
```

7. Sweep `tests/test_tui.py` for digit presses that the reorder shifted: settings was `"7"`, is now `"8"` (e.g. adjust any `press("7")` that asserted settings). Do not touch assertions themselves — only the key that reaches the intended view.

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_tui.py -q`
Expected: all pass, including the pre-existing view-switch tests after the key sweep

- [ ] **Step 6: Commit**

```bash
git add qlab/tui/atlas_view.py qlab/tui/app.py qlab/tui/theme.py tests/test_tui.py
git commit -m "feat(tui): atlas master-detail view with live champion overlay"
```

---

### Task 9: Research leaderboard + arm-name humanization sweep

**Files:**
- Modify: `qlab/tui/app.py` (compose research view ~line 877; `_render_research` ~line 2019; audit detail arm display ~line 2308), `tests/test_tui.py` (`_snapshot()` gains `leaderboard`)
- Test: `tests/test_tui.py`

**Interfaces:**
- Consumes: snapshot key `leaderboard` (Task 5 row shape), `arm_display_name` (Task 1).

- [ ] **Step 1: Update fixture and write the failing test**

`_snapshot()` gains:

```python
"leaderboard": [
    {"arm_id": "B2", "name": "HRP", "champion": True, "benchmark": False,
     "sharpe": 0.91, "ann_return": 0.062, "max_drawdown": -0.124,
     "cvar_95": -0.011, "deflated_sharpe": 0.83},
    {"arm_id": "B0", "name": "60/40", "champion": False, "benchmark": True,
     "sharpe": 0.55, "ann_return": 0.050, "max_drawdown": -0.180,
     "cvar_95": -0.015, "deflated_sharpe": 0.60},
],
```

Test:

```python
def test_research_leaderboard_shows_method_names_not_codes():
    from qlab.tui.app import QlabTui

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0)
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("4")
            board = str(app.query_one("#leaderboard").content)
            assert "HRP" in board and "60/40" in board
            assert "★" in board            # champion marked
            assert "BENCH" in board        # benchmark tagged
            assert "B2" not in board       # codes never rendered here

    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tui.py -q -k leaderboard`
Expected: FAIL — no `#leaderboard` widget

- [ ] **Step 3: Implement**

Compose (research view, between `#research-summary` and `#runs-table`):

```python
yield Static("ABLATION LEADERBOARD", classes="book-section-title")
yield Static(id="leaderboard", classes="book-section", markup=True)
```

`_render_research` — after the summary update:

```python
board = self.snapshot.get("leaderboard") or []
if board:
    lines = [
        f"[{DIM}]METHOD                      SHARPE     RET    MAXDD   "
        f"CVAR95     DSR[/]"
    ]
    for row in board:
        mark = f" [{AMBER}]★[/]" if row.get("champion") else (
            f" [{DIM}]BENCH[/]" if row.get("benchmark") else "")
        def _cell(value, fmt="{:.2f}"):
            return "—" if value is None else fmt.format(value)
        lines.append(
            f"[{TEXT_HI}]{escape(str(row.get('name', ''))):<24}[/]{mark:<12} "
            f"[{TEXT}]{_cell(row.get('sharpe')):>6}  "
            f"{_cell(row.get('ann_return'), '{:+.1%}'):>6}  "
            f"{_cell(row.get('max_drawdown'), '{:.1%}'):>6}  "
            f"{_cell(row.get('cvar_95'), '{:.2%}'):>7}  "
            f"{_cell(row.get('deflated_sharpe')):>5}[/]")
    self.query_one("#leaderboard", Static).update("\n".join(lines))
else:
    self.query_one("#leaderboard", Static).update(
        f"[{MUTED}]No ablation recorded yet — run [bold]: batch[/] for the "
        f"staged comparison.[/]")
```

(Hoist `_cell` to module level next to `_book_state_style` — a nested def re-created per render is not the file's idiom.)

Humanization sweep — in the audit detail (`~line 2308`), route the arm through the name map:

```python
from qlab.core.atlas import arm_display_name  # with the other qlab imports
...
arm = decision.get("choice", {}).get("arm") if isinstance(decision.get("choice"), dict) else None
detail = choice.get("regime") or (arm_display_name(arm) if arm else "") or decision.get("rationale", "")
```

(Adapt to the actual expression at that line — the contract is: any `arm` value that reaches display text goes through `arm_display_name`.) Then grep the TUI for other raw arm renderings: `grep -n "arm" qlab/tui/app.py` — apply the same wrap anywhere an arm id reaches display text.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_tui.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add qlab/tui/app.py tests/test_tui.py
git commit -m "feat(tui): ablation leaderboard by method name; humanize arm ids"
```

---

### Task 10: Full-suite verification

- [ ] **Step 1: Run the entire suite offline**

Run: `python -m pytest`
Expected: all pass, no network access. Fix regressions without weakening any assertion.

- [ ] **Step 2: Manual smoke (operator)**

1. **Restart the owner** (`qlab tui`) — a long-lived owner serves pre-change imports (repo invariant 8).
2. Press `7` → atlas: arms listed by method name, HRP starred, stage badges present, `ablation id:` footnote dim at the bottom.
3. Press `5` → book: equity section renders (or the honest "No equity history yet" note), positions show P&L.
4. `: daily ops` then re-check book — a `daily` mark should appear in the curve.
5. With Alpaca creds set: `curl -X POST localhost:8765/api/performance/backfill` → `{"backfilled": N}`; without a history-capable broker it returns a 400 with a clear error.
6. Kill the owner while the TUI runs → chip flips LIVE → STALE → OWNER DOWN.

- [ ] **Step 3: Commit any smoke fixes**

```bash
git add -A -- qlab tests
git commit -m "fix(tui): smoke-test fixes for atlas and performance views"
```

(Skip if nothing changed.)

## Self-review notes

- Spec coverage: hybrid atlas (T1+T5+T8), registry+backfill performance (T2+T3+T4+T7), nav placement with settings last (T8), master-detail (T8), names-not-codes everywhere with dim footnote (T1, T5, T8, T9), staleness chip (T6), leaderboard (T5+T9), position P&L (T3+T7).
- Champion marking is derived from `mandate.operational_policy` at request time — promoting a new policy moves the star with no atlas edit.
- All new registry writes go through `UISession` methods invoked from `handle_api` — the owner process remains the single writer.
- Line-number anchors are as of commit 13f3d55 and drift as tasks land — locate by symbol name, not by number.
