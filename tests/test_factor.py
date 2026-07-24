"""Research-only stock factor covariance estimation."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from qlab.algorithms import get_algorithm
from qlab.core.factor import factor_covariance


def test_single_factor_betas_are_recovered() -> None:
    rng = np.random.default_rng(21)
    observations = 1500
    index = pd.date_range("2020-01-01", periods=observations, freq="B")
    factor = rng.normal(0.0004, 0.012, observations)
    true_betas = np.array([0.55, 0.9, 1.2, 1.6])
    noise = rng.normal(0.0, 0.003, (observations, len(true_betas)))
    stock_values = 0.0001 + np.outer(factor, true_betas) + noise

    stocks = pd.DataFrame(
        stock_values, index=index, columns=["LOW", "MID", "HIGH", "TOP"]
    )
    factors = pd.DataFrame({"market": factor}, index=index)
    model = factor_covariance(stocks, factors)

    assert model.B.shape == (len(true_betas), 1)
    np.testing.assert_allclose(model.B[:, 0], true_betas, atol=0.025)
    assert model.Sigma_f.shape == (1, 1)
    assert np.all(model.D >= 1e-12)
    assert model.stock_names == tuple(stocks.columns)
    assert model.factor_names == tuple(factors.columns)


def test_covariance_is_psd_with_a_small_panel_above_minimum() -> None:
    rng = np.random.default_rng(22)
    observations = 14
    factors = pd.DataFrame(
        rng.normal(0.0, 0.01, (observations, 2)),
        columns=["market", "value"],
    )
    loadings = rng.normal(0.8, 0.3, (18, 2))
    stocks = pd.DataFrame(
        factors.to_numpy() @ loadings.T
        + rng.normal(0.0, 0.002, (observations, len(loadings))),
        columns=[f"S{index:02d}" for index in range(len(loadings))],
    )

    covariance = factor_covariance(stocks, factors, min_obs=12).covariance

    assert covariance.shape == (len(loadings), len(loadings))
    assert np.allclose(covariance, covariance.T, atol=1e-12)
    assert np.linalg.eigvalsh(covariance).min() >= -1e-12


def test_short_overlap_fails_loudly() -> None:
    stocks = pd.DataFrame(
        {"A": np.linspace(-0.01, 0.01, 140)},
        index=pd.RangeIndex(0, 140),
    )
    factors = pd.DataFrame(
        {"market": np.linspace(-0.02, 0.02, 140)},
        index=pd.RangeIndex(40, 180),
    )

    with pytest.raises(ValueError, match="insufficient overlapping observations"):
        factor_covariance(stocks, factors, min_obs=120)


def test_collinear_factors_fail_loudly() -> None:
    rng = np.random.default_rng(23)
    market = rng.normal(0.0, 0.01, 150)
    factors = pd.DataFrame({"market": market, "duplicate": 2.0 * market})
    stocks = pd.DataFrame(
        {
            "A": 0.8 * market + rng.normal(0.0, 0.002, len(market)),
            "B": 1.2 * market + rng.normal(0.0, 0.002, len(market)),
        }
    )

    with pytest.raises(ValueError, match="collinear.*condition number"):
        factor_covariance(stocks, factors)


@pytest.mark.parametrize("panel_name", ["stocks", "factors"])
def test_nan_columns_fail_loudly(panel_name: str) -> None:
    rng = np.random.default_rng(24)
    factor = rng.normal(0.0, 0.01, 130)
    stocks = pd.DataFrame({"A": factor + rng.normal(0.0, 0.002, len(factor))})
    factors = pd.DataFrame({"market": factor})
    panel = stocks if panel_name == "stocks" else factors
    panel.loc[10, panel.columns[0]] = np.nan

    with pytest.raises(ValueError, match=r"NaN values in columns"):
        factor_covariance(stocks, factors)


def test_catalog_entry_is_visible_but_not_agent_runnable() -> None:
    spec = get_algorithm("stock_factor_covariance")

    assert spec.category == "estimation"
    assert spec.stage == "research"
    assert spec.solver is None
    assert spec.agent_tool is None
    assert spec.agent_usable is False
    assert "not an allocation" in spec.description
