"""Walk-forward evidence for estimation-window and shrinkage judgments.

This module deliberately stays outside the registry.  It runs a bounded
sensitivity sweep through the existing backtest engine and returns descriptive
evidence; the MCP tool decides how to persist that evidence without turning
each row into a deflated-Sharpe trial.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from qlab.algorithms import get_operational_policy
from qlab.arms import MomentsConfig, build_policy
from qlab.core.backtest import BacktestResult, run_backtest
from qlab.core.types import DataSnapshot

_SUPPORTED_SHRINKAGES = frozenset({"ledoit_wolf", "nonlinear"})


def window_evidence(
    snapshot: DataSnapshot,
    windows: Iterable[int] = (252, 504, 756),
    shrinkages: Iterable[str] = ("ledoit_wolf", "nonlinear"),
    policy_solver: str | None = None,
    cadence: str = "quarterly",
) -> list[dict[str, Any]]:
    """Rank walk-forward evidence for window/shrinkage combinations.

    ``policy_solver`` is an operational policy id (for example ``"hrp"``).
    When omitted, the mandate-configured operational policy is used.  Every
    row is the same policy and walk-forward engine with only the estimation
    window and covariance shrinkage changed.

    Ranking is deterministic and intentionally transparent: higher Sortino
    first, then lower annualized realized volatility, shallower maximum
    drawdown, and lower turnover.  The stable window/shrinkage ordering breaks
    an otherwise exact tie.
    """
    window_values = _validated_windows(windows)
    shrinkage_values = _validated_shrinkages(shrinkages)
    policy = _resolve_policy(policy_solver)

    rows: list[dict[str, Any]] = []
    for window in window_values:
        for shrinkage in shrinkage_values:
            result = run_backtest(
                snapshot.prices,
                build_policy(
                    policy.arm(),
                    moments=MomentsConfig(
                        lookback_days=window,
                        shrinkage=shrinkage,
                    ),
                ),
                arm_id=f"{policy.arm_id}:window={window}:shrinkage={shrinkage}",
                cadence=cadence,
                lookback_days=window,
            )
            rows.append(_evidence_row(result, window, shrinkage, policy.id, cadence))

    rows.sort(key=_ranking_key)
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


def _validated_windows(windows: Iterable[int]) -> tuple[int, ...]:
    values = tuple(windows)
    if not values:
        raise ValueError("windows must contain at least one lookback")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise TypeError("every window must be an integer")
    if any(value < 3 for value in values):
        raise ValueError("every window must be at least 3 observations")
    if len(set(values)) != len(values):
        raise ValueError("windows must be unique")
    return values


def _validated_shrinkages(shrinkages: Iterable[str]) -> tuple[str, ...]:
    values = tuple(shrinkages)
    if not values:
        raise ValueError("shrinkages must contain at least one estimator")
    if any(not isinstance(value, str) for value in values):
        raise TypeError("every shrinkage must be a string")
    unsupported = sorted(set(values) - _SUPPORTED_SHRINKAGES)
    if unsupported:
        raise ValueError(
            f"unsupported shrinkage(s) {unsupported}; "
            f"choose from {sorted(_SUPPORTED_SHRINKAGES)}"
        )
    if len(set(values)) != len(values):
        raise ValueError("shrinkages must be unique")
    return values


def _resolve_policy(policy_solver: str | None):
    if policy_solver is None:
        from qlab.trader.mandate import load_mandate

        policy_solver = load_mandate().operational_policy
    return get_operational_policy(policy_solver)


def _evidence_row(
    result: BacktestResult,
    window: int,
    shrinkage: str,
    policy_id: str,
    cadence: str,
) -> dict[str, Any]:
    metrics = result.metrics
    realized = result.returns
    if realized.empty:
        span = {"start": None, "end": None, "n_obs": 0}
    else:
        span = {
            "start": realized.index[0].date().isoformat(),
            "end": realized.index[-1].date().isoformat(),
            "n_obs": int(len(realized)),
        }
    return {
        "window": window,
        "shrinkage": shrinkage,
        "policy": policy_id,
        "cadence": cadence,
        "ann_vol": _finite_metric(metrics, "ann_vol"),
        "sortino": _finite_metric(metrics, "sortino"),
        "max_drawdown": _finite_metric(metrics, "max_drawdown"),
        "turnover": _finite_value(result.total_turnover, "turnover"),
        "n_rebalances": int(result.diagnostics["n_rebalances"]),
        "span": span,
    }


def _finite_metric(metrics: dict[str, float], name: str) -> float:
    if name not in metrics:
        raise ValueError(f"backtest did not produce required metric {name!r}")
    return _finite_value(metrics[name], name)


def _finite_value(value: float, name: str) -> float:
    scalar = float(value)
    if not math.isfinite(scalar):
        raise ValueError(f"backtest produced non-finite {name}: {scalar!r}")
    return scalar


def _ranking_key(row: dict[str, Any]) -> tuple:
    return (
        -row["sortino"],
        row["ann_vol"],
        -row["max_drawdown"],
        row["turnover"],
        row["window"],
        row["shrinkage"],
    )
