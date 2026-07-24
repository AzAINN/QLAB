"""Equilibrium expected returns, uncertainty bands, and DSR accounting."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from qlab.core.equilibrium import (
    TRADING_DAYS,
    equilibrium_returns,
    implied_returns,
    inverse_vol_weights,
)
from qlab.core.types import MomentSet


class ToolApp:
    def __init__(self):
        self.tools = {}

    def tool(self, name: str):
        def decorate(fn):
            self.tools[name] = fn
            return fn

        return decorate


def test_two_asset_implied_returns_match_hand_calculation() -> None:
    covariance = np.array([
        [0.04, 0.01],
        [0.01, 0.09],
    ])
    weights = np.array([0.60, 0.40])

    actual = implied_returns(covariance, weights, risk_aversion=2.5)

    # Sigma @ w = [0.028, 0.042]; delta=2.5.
    np.testing.assert_allclose(actual, np.array([0.070, 0.105]), atol=1e-12)


def test_parameter_bands_shrink_with_larger_moment_sample() -> None:
    annual_covariance = np.array([
        [0.04, 0.01],
        [0.01, 0.09],
    ])
    daily_covariance = annual_covariance / TRADING_DAYS
    prior = {"AAA": 0.60, "BBB": 0.40}
    target = {"AAA": 0.25, "BBB": 0.50}

    short = equilibrium_returns(
        daily_covariance,
        ["AAA", "BBB"],
        100,
        weights_prior=prior,
        target_weights=target,
    )
    long = equilibrium_returns(
        daily_covariance,
        ["AAA", "BBB"],
        400,
        weights_prior=prior,
        target_weights=target,
    )

    for ticker in ("AAA", "BBB"):
        assert set(short["returns"][ticker]) == {"mu", "lo", "hi"}
        short_width = (
            short["returns"][ticker]["hi"] - short["returns"][ticker]["lo"]
        )
        long_width = (
            long["returns"][ticker]["hi"] - long["returns"][ticker]["lo"]
        )
        assert long_width == pytest.approx(short_width / 2.0)
        assert (
            short["returns"][ticker]["lo"]
            < short["returns"][ticker]["mu"]
            < short["returns"][ticker]["hi"]
        )

    short_portfolio_width = (
        short["portfolio"]["hi"] - short["portfolio"]["lo"]
    )
    long_portfolio_width = long["portfolio"]["hi"] - long["portfolio"]["lo"]
    assert long_portfolio_width == pytest.approx(short_portfolio_width / 2.0)
    assert short["portfolio"]["weights"] == {"AAA": 0.25, "BBB": 0.50}
    assert short["annualization"] == TRADING_DAYS


def test_inverse_vol_prior_is_the_documented_no_market_cap_default() -> None:
    covariance = np.diag([0.04, 0.16])
    weights = inverse_vol_weights(covariance)
    np.testing.assert_allclose(weights, np.array([2.0 / 3.0, 1.0 / 3.0]))

    result = equilibrium_returns(
        covariance / TRADING_DAYS,
        ["LOW", "HIGH"],
        252,
    )
    assert result["prior_weight_source"] == "inverse_volatility"
    np.testing.assert_allclose(
        list(result["prior_weights"].values()),
        weights,
    )


def test_max_utility_consumes_one_deterministic_equilibrium_mu() -> None:
    from qlab.core.objective import build_objective

    covariance = np.array([
        [0.0002, 0.00005],
        [0.00005, 0.0004],
    ])
    moments = MomentSet(
        tickers=["AAA", "BBB"],
        as_of=date(2026, 7, 24),
        cov=covariance,
    )

    objective = build_objective("max_utility", moments)
    expected = implied_returns(
        covariance,
        inverse_vol_weights(covariance),
    )

    np.testing.assert_allclose(objective.mu, expected)
    assert objective.extra["expected_returns_source"] == (
        "black_litterman_equilibrium_inverse_volatility"
    )


def test_tool_persists_one_run_without_adding_dsr_trials(reg) -> None:
    from qlab.mcp.guardrails import LabState
    from qlab.mcp.quant_lab import register_lab_tools

    baseline_run = reg.log_run("ablation", {"name": "baseline"})
    reg.log_backtest(
        baseline_run,
        "A1",
        {"ann_vol": 0.1, "sortino": 1.0},
        objective="min_variance",
    )
    reg.init_account(10_000.0)
    backtest_count_before = reg.backtest_trial_count()
    solution_count_before = reg.trial_count()

    app = ToolApp()
    state = LabState(registry=reg, offline=True, seed=29)
    register_lab_tools(app, state)
    result = app.tools["research.equilibrium_returns"](
        as_of="2012-12-31",
        universe="core",
        lookback_days=126,
    )

    assert result["caveats"]["interpretation"] == (
        "equilibrium prior, not a forecast"
    )
    assert result["caveats"]["uncertainty"] == (
        "bands are parameter uncertainty"
    )
    assert result["caveats"]["dsr_trial_counted"] is False
    assert result["table"]
    assert all(
        row["lo"] <= row["mu"] <= row["hi"]
        for row in result["table"]
    )
    assert reg.backtest_trial_count() == backtest_count_before
    assert reg.trial_count() == solution_count_before
    assert state.budget.by_tool["research.equilibrium_returns"] == 1

    runs = [
        run for run in reg.list_runs()
        if run["kind"] == "equilibrium"
    ]
    assert len(runs) == 1
    assert runs[0]["run_id"] == result["run_id"]
    assert runs[0]["spec"]["table"] == result["table"]
    report = reg.report(result["run_id"])
    assert report["backtests"] == []
    assert report["solutions"] == []


def test_tui_snapshot_exposes_latest_equilibrium_summary(reg) -> None:
    from qlab.ui.server import UISession

    older = {
        "as_of": "2026-07-23",
        "portfolio": {"mu": 0.02, "lo": -0.01, "hi": 0.05},
        "caveats": {"interpretation": "equilibrium prior, not a forecast"},
    }
    run_id = reg.log_run("equilibrium", older)
    session = UISession(offline_default=True, registry=reg)
    session.portfolio = lambda offline: {}
    session.market = lambda offline: {}
    session.agents = lambda: []
    session.system_status = lambda offline: {}
    session.allocation_policy = lambda: {}

    snapshot = session.tui_snapshot(True)

    assert snapshot["equilibrium_returns"] == {
        "run_id": run_id,
        "as_of": "2026-07-23",
        "portfolio": older["portfolio"],
        "caveats": older["caveats"],
    }


def test_inverse_vol_prior_refuses_stale_series():
    import numpy as np
    import pytest

    from qlab.core.equilibrium import inverse_vol_weights

    with pytest.raises(ValueError, match="stale"):
        inverse_vol_weights(np.diag([1e-24, 0.04]))
