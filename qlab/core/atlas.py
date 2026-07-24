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
