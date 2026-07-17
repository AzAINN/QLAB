"""Hard-signals layer (roadmap Amendment A / §3): turbulence, absorption ratio,
FRED vol-index fetch, and the composite price-only regime lambda.

Every signal here is deterministic and injection-immune — computed from prices
or fixed public index series, never from text or an LLM. The ``_two_regime_returns``
helper is module-level on purpose: later signal tasks reuse it.
"""

import numpy as np
import pandas as pd


def _two_regime_returns(seed=7, n=6):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2018-01-01", periods=1000)
    calm = rng.normal(0, 0.006, (500, n))
    common = rng.normal(0, 0.02, (500, 1))
    stress = 0.8 * common + rng.normal(0, 0.009, (500, n))   # high vol + high corr
    return pd.DataFrame(np.vstack([calm, stress]), index=idx,
                        columns=[f"A{i}" for i in range(n)])


def test_turbulence_spikes_in_stress():
    from qlab.signals.hard import turbulence
    r = _two_regime_returns()
    t = turbulence(r, lookback=252)
    # Chow-Kritzman turbulence is a regime-RELATIVE Mahalanobis distance: it
    # spikes at the ONSET of the high-vol/high-corr regime (row ~500, while the
    # trailing covariance window is still calm) and reverts toward its
    # chi-squared baseline once the window fills with stress. So it fires in the
    # transition band, not deep-stress. Positions map to data rows via 252+i.
    onset = t.iloc[248:360].mean()      # stress onset (data rows 500-611)
    calm = t.iloc[:240].mean()          # sustained-calm baseline (rows 252-491)
    assert onset > 2 * calm


def test_absorption_rises_when_correlation_concentrates():
    from qlab.signals.hard import absorption_ratio
    r = _two_regime_returns()
    a = absorption_ratio(r, window=300)   # step=5 -> ~140 pts, position i = row 300+5i
    # Low (~0.4) in calm where variance spreads across uncorrelated assets;
    # high (~0.84) in stress where one common factor dominates.
    assert a.iloc[-1] > a.iloc[30] + 0.1  # deep-stress vs a calm-regime (row 450) value


def test_fred_series_offline_is_deterministic_without_network(tmp_path):
    from qlab.signals.hard import fred_series
    s1 = fred_series("VIXCLS", cache_dir=tmp_path, offline=True, seed=3)
    s2 = fred_series("VIXCLS", cache_dir=tmp_path, offline=True, seed=3)
    assert len(s1) > 100 and (s1 == s2).all() and (s1 > 0).all()


def test_composite_regime_lambda_higher_in_stress():
    from qlab.core.types import DataSnapshot
    from qlab.signals.hard import composite_regime
    r = _two_regime_returns()
    px = (1 + r).cumprod() * 100
    calm_snap = DataSnapshot(list(px.columns), px, px.index[480].date())
    stress_snap = DataSnapshot(list(px.columns), px, px.index[-1].date())
    lam_calm = composite_regime(calm_snap, offline=True)["regime_lambda"]
    lam_stress = composite_regime(stress_snap, offline=True)["regime_lambda"]
    # Measured deterministic gap is ~0.51: turbulence + trailing-vol percentiles
    # separate the regimes cleanly, and absorption_pct no longer degenerates
    # (window shrinks with history length, so both snapshots get a real
    # absorption series instead of the old near-1-point degenerate case).
    assert lam_stress > lam_calm + 0.2
    assert composite_regime(stress_snap, offline=True)["regime"] == "stress"


def test_composite_regime_drops_degenerate_absorption():
    from qlab.signals.hard import composite_regime
    from qlab.core.types import DataSnapshot
    r = _two_regime_returns().iloc[:400]          # short history band
    px = (1 + r).cumprod() * 100
    snap = DataSnapshot(list(px.columns), px, px.index[-1].date())
    out = composite_regime(snap, offline=True)
    comps = out["components"]
    assert ("absorption_pct" not in comps) or (
        comps["absorption_pct"] < 1.0), "degenerate absorption must not pin lambda"
    assert 0.0 <= out["regime_lambda"] <= 1.0


def test_conditioned_covariance_scales_with_lambda():
    from qlab.signals.condition import condition_covariance, regime_labels
    r = _two_regime_returns()
    X = r.to_numpy()
    labels = regime_labels(r)
    cov0 = condition_covariance(X, labels, 0.0)
    cov1 = condition_covariance(X, labels, 1.0)
    assert np.trace(cov1) > 1.5 * np.trace(cov0)          # stress cov is hotter
    for c in (cov0, cov1):
        assert np.all(np.linalg.eigvalsh(c) > -1e-10)     # PSD


def test_b4_arm_runs_regime_conditional():
    from datetime import date
    from qlab.arms import Arm, MomentsConfig, solve_arm
    from qlab.core.types import DataSnapshot
    r = _two_regime_returns()
    px = (1 + r).cumprod() * 100
    snap = DataSnapshot(list(px.columns), px, px.index[-1].date())
    arm = Arm("B4", "min_variance", "classical", {"regime_conditional": True})
    w, diag = solve_arm(arm, snap, moments=MomentsConfig(lookback_days=750))
    assert abs(sum(w.values) - 1.0) < 1e-6
    assert diag["moments"].get("regime_lambda") is not None


def test_regime_conditional_rejects_higher_moments():
    import pytest
    from qlab.arms import Arm, MomentsConfig, solve_arm
    from qlab.core.types import DataSnapshot
    r = _two_regime_returns()
    px = (1 + r).cumprod() * 100
    snap = DataSnapshot(list(px.columns), px, px.index[-1].date())
    arm = Arm("X", "mvsk", "classical_multistart",
              {"skew_lambda": 0.5, "kurt_lambda": 0.5, "regime_conditional": True})
    with pytest.raises(ValueError, match="covariance-only"):
        solve_arm(arm, snap, moments=MomentsConfig(lookback_days=750))
