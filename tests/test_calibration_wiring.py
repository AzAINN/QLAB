"""Applied-view persistence, realized calibration, and research-arm wiring."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from qlab.core.views import TailView
from qlab.experiment import news_conditioned_arm


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


def _fatter_tail_prices() -> pd.DataFrame:
    realized = np.zeros(100)
    realized[:5] = 1.0
    realized[5:10] = -1.0
    levels = 100.0 * np.exp(np.r_[0.0, np.cumsum(realized)])
    return pd.DataFrame(
        {"AAA": levels},
        index=pd.bdate_range("2024-01-02", periods=len(levels)),
    )


def test_applied_view_persistence_round_trips_and_is_idempotent(reg):
    run_id = reg.log_run("views", {"case": "round-trip"})
    payload = {
        "type": "tail",
        "ticker": "AAA",
        "direction": "fatter",
        "confidence": 0.4,
        "source_quote": "risk distribution may widen",
        "horizon_days": 5,
    }
    baseline = {"pre_view_tail_mass": 0.05}

    view_id = reg.log_applied_view(
        "2024-01-02",
        "test",
        payload,
        baseline,
        run_id,
    )
    assert reg.log_applied_view(
        "2024-01-02",
        "test",
        payload,
        baseline,
        run_id,
    ) == view_id

    rows = reg.con.execute(
        """
        SELECT view_id, as_of, universe, view_kind, horizon_days,
               view_payload, pre_view_baseline, run_id, score, resolved_at
        FROM applied_views
        """
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row[:5] == (
        view_id,
        "2024-01-02",
        "test",
        "tail",
        5,
    )
    assert _json(row[5]) == payload
    assert _json(row[6]) == baseline
    assert row[7] == run_id
    assert row[8:] == (None, None)


def test_resolver_scores_elapsed_view_skips_pending_and_preserves_dsr(reg):
    prices = _fatter_tail_prices()
    run_id = reg.log_run("views", {"case": "resolve"})
    elapsed_id = reg.log_applied_view(
        prices.index[0].date(),
        "test",
        {
            "type": "tail",
            "ticker": "AAA",
            "direction": "fatter",
            "horizon_days": 100,
        },
        {"pre_view_tail_mass": 0.05},
        run_id,
    )
    pending_id = reg.log_applied_view(
        prices.index[-2].date(),
        "test",
        {
            "type": "tail",
            "ticker": "AAA",
            "direction": "fatter",
            "horizon_days": 2,
        },
        {"pre_view_tail_mass": 0.05},
        run_id,
    )
    dsr_before = (
        reg.trial_count(),
        reg.backtest_trial_count(),
        reg.backtest_arm_ids(),
    )

    summary = reg.resolve_view_calibration(prices)

    assert summary["n"] == 1
    assert summary["hit_rate"] == pytest.approx(1.0)
    assert summary["mean_magnitude"] > 0.0
    assert summary["by_view_kind"]["tail"]["n"] == 1

    elapsed = reg.con.execute(
        "SELECT score, resolved_at FROM applied_views WHERE view_id=?",
        [elapsed_id],
    ).fetchone()
    elapsed_score = _json(elapsed[0])
    assert elapsed_score["view_label"] == "tail(AAA fatter)"
    assert elapsed_score["direction_correct"] is True
    assert elapsed_score["realized_value"] == pytest.approx(0.10)
    assert elapsed_score["magnitude_score"] > 0.0
    assert elapsed[1] is not None

    pending = reg.con.execute(
        "SELECT score, resolved_at FROM applied_views WHERE view_id=?",
        [pending_id],
    ).fetchone()
    assert pending == (None, None)
    assert reg.resolve_view_calibration(prices) == summary
    assert reg.con.execute(
        "SELECT COUNT(*) FROM events WHERE kind='view_calibration_resolved'"
    ).fetchone()[0] == 1
    assert (
        reg.trial_count(),
        reg.backtest_trial_count(),
        reg.backtest_arm_ids(),
    ) == dsr_before


def _research_prices() -> pd.DataFrame:
    rng = np.random.default_rng(47)
    n_returns = 1565
    returns = rng.normal(0.0, 0.004, size=(n_returns, 2))
    for start in range(0, n_returns - 1, 20):
        returns[start, 0] = 0.04
        returns[start + 1, 0] = -0.04
    levels = 100.0 * np.exp(
        np.vstack([np.zeros(2), np.cumsum(returns, axis=0)])
    )
    prices = pd.DataFrame(
        levels,
        index=pd.bdate_range("2017-01-02", periods=len(levels)),
        columns=["AAA", "BBB"],
    )
    prices.attrs.update({"source": "synthetic", "synthetic": True})
    return prices


def test_news_conditioned_arm_runs_offline_with_means_pinned():
    result = news_conditioned_arm(
        _research_prices(),
        [TailView("AAA", "fatter", confidence=0.2)],
        cadence="annual",
        lookback_days=252,
        cost_bps=0.0,
        n_trials=2,
    )

    assert not result.returns.empty
    assert result.diagnostics["stage"] == "research"
    assert result.diagnostics["research_only"] is True
    assert result.diagnostics["operational"] is False
    assert result.diagnostics["objective"] == "min_variance"
    assert result.diagnostics["dsr_trial_counted"] is False
    assert result.diagnostics["mean_pinning_max_abs"] <= 1e-8
    assert result.diagnostics["mean_pinning"]
    for record in result.diagnostics["mean_pinning"]:
        assert record["means_conditioned"] == pytest.approx(
            record["means_unconditioned"],
            abs=1e-8,
        )
