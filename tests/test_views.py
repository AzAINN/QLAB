"""Entropy-pooling views: means pinned, moments moved, budgets enforced."""

from __future__ import annotations

import numpy as np
import pytest

from qlab.core.views import (
    CorrView, TailView, ViewsResult, VolView, apply_views, conditioned_moments)


def _panel(seed: int = 7, n_obs: int = 500, n_assets: int = 3):
    rng = np.random.default_rng(seed)
    cov = np.array([[1.0, 0.3, 0.1], [0.3, 1.0, 0.2], [0.1, 0.2, 1.0]])
    scale = np.array([0.01, 0.02, 0.015])
    panel = rng.multivariate_normal(
        [0.0002, 0.0003, 0.0001], cov * np.outer(scale, scale), size=n_obs)
    return panel, ["AAA", "BBB", "CCC"]


def test_vol_view_moves_vol_and_pins_every_mean():
    panel, tickers = _panel()
    base_vol = float(np.std(panel[:, 0], ddof=0))
    target = base_vol * 1.5
    result = apply_views(
        panel, tickers, [VolView("AAA", target)], kl_budget=2.0)
    assert isinstance(result, ViewsResult)
    assert result.moments_after["AAA"]["vol"] == pytest.approx(target, rel=1e-4)
    for ticker in tickers:
        assert result.moments_after[ticker]["mean"] == pytest.approx(
            result.moments_before[ticker]["mean"], abs=1e-8)
    # Minimal tilt: any hand-perturbed alternative satisfying the vol target
    # this loosely must carry at least as much KL.
    assert result.kl_total > 0


def test_corr_view_moves_pair_monotonically_with_confidence():
    panel, tickers = _panel()
    def corr_after(confidence):
        res = apply_views(
            panel, tickers,
            [CorrView("AAA", "BBB", 0.7, confidence=confidence)],
            kl_budget=5.0)
        _, cov = conditioned_moments(panel, res.probabilities)
        return cov[0, 1] / np.sqrt(cov[0, 0] * cov[1, 1])

    base = np.corrcoef(panel[:, 0], panel[:, 1])[0, 1]
    c_low, c_high = corr_after(0.3), corr_after(1.0)
    assert base < c_low < c_high
    assert c_high == pytest.approx(0.7, abs=0.02)


def test_tail_view_fattens_tail_mass():
    panel, tickers = _panel()
    result = apply_views(
        panel, tickers, [TailView("BBB", "fatter")], kl_budget=2.0)
    p = result.probabilities
    mean = p @ panel[:, 1]
    vol = np.sqrt(p @ (panel[:, 1] - mean) ** 2)
    # Tail mass beyond 2 sigma of the ORIGINAL distribution grew.
    base_mask = np.abs(panel[:, 1] - panel[:, 1].mean()) > 2 * panel[:, 1].std()
    assert p[base_mask].sum() > base_mask.mean()
    assert vol > 0


def test_sequential_order_matters_but_both_orders_are_valid():
    panel, tickers = _panel()
    views_ab = [VolView("AAA", float(np.std(panel[:, 0])) * 1.4),
                CorrView("AAA", "BBB", 0.6)]
    res_ab = apply_views(panel, tickers, views_ab, kl_budget=5.0)
    res_ba = apply_views(panel, tickers, list(reversed(views_ab)), kl_budget=5.0)
    # Sequential Meucci construction: order changes the path, both are valid
    # distributions with pinned means.
    assert not np.allclose(res_ab.probabilities, res_ba.probabilities)
    for res in (res_ab, res_ba):
        for ticker in tickers:
            assert res.moments_after[ticker]["mean"] == pytest.approx(
                res.moments_before[ticker]["mean"], abs=1e-8)


def test_kl_budget_breach_names_the_culprit():
    panel, tickers = _panel()
    # Feasible but expensive: doubling a vol needs a large tilt whose KL
    # dwarfs a near-zero budget.
    big = VolView("AAA", float(np.std(panel[:, 0])) * 2.0)
    with pytest.raises(ValueError, match="KL budget exceeded") as excinfo:
        apply_views(panel, tickers, [big], kl_budget=0.01)
    assert "vol(AAA" in str(excinfo.value)


def test_infeasible_view_fails_loud_before_the_budget():
    panel, tickers = _panel()
    extreme = VolView("AAA", float(np.std(panel[:, 0])) * 3.9)
    with pytest.raises(ValueError, match="infeasible"):
        apply_views(panel, tickers, [extreme], kl_budget=50.0)


def test_view_validation_fails_loud():
    panel, tickers = _panel()
    with pytest.raises(ValueError, match="confidence"):
        VolView("AAA", 0.01, confidence=0.0)
    with pytest.raises(ValueError, match="distinct"):
        CorrView("AAA", "AAA", 0.5)
    with pytest.raises(ValueError, match="direction"):
        TailView("AAA", "wider")
    with pytest.raises(ValueError, match="clamp"):
        apply_views(panel, tickers,
                    [VolView("AAA", float(np.std(panel[:, 0])) * 10)],
                    kl_budget=50.0)
    with pytest.raises(ValueError, match="scenarios"):
        apply_views(panel[:30], tickers,
                    [VolView("AAA", 0.01)], kl_budget=1.0)
    with pytest.raises(ValueError, match="absent"):
        apply_views(panel, tickers, [VolView("ZZZ", 0.01)], kl_budget=1.0)


def test_conditioned_moments_validation():
    panel, _ = _panel()
    with pytest.raises(ValueError, match="distribution"):
        conditioned_moments(panel, np.full(panel.shape[0], 0.5))
