"""Optional Gaussian-HMM regime detection.

The model is deliberately isolated from the deterministic signal path:
``hmmlearn`` is imported only when :func:`fit_regime_hmm` is called.  A core
installation can therefore keep using the hard-signal ensemble, while an
explicit HMM request fails loudly with the name of the required extra.
"""
from __future__ import annotations

from math import sqrt

import numpy as np
import pandas as pd

_TRADING_DAYS = 252
_VOL_WINDOW = 21
_STATE_NAMES = {
    2: ("calm", "stress"),
    3: ("calm", "normal", "stress"),
}


def _gaussian_hmm_class():
    try:
        from hmmlearn.hmm import GaussianHMM
    except ImportError as exc:
        raise RuntimeError(
            "Gaussian HMM regime detection requires the optional 'hmm' extra; "
            "install it with `python -m pip install -e '.[hmm]'`"
        ) from exc
    return GaussianHMM


def _feature_frame(returns: pd.DataFrame | pd.Series) -> pd.DataFrame:
    if isinstance(returns, pd.Series):
        frame = returns.to_frame("portfolio")
    elif isinstance(returns, pd.DataFrame):
        frame = returns
    else:
        raise TypeError("returns must be a pandas Series or DataFrame")
    if frame.empty or frame.shape[1] == 0:
        raise ValueError("returns must contain at least one asset and observation")

    try:
        values = frame.to_numpy(dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError("returns must be numeric") from exc
    if not np.isfinite(values).all():
        raise ValueError("returns must contain only finite, complete observations")

    portfolio = pd.Series(
        values.mean(axis=1),
        index=frame.index,
        name="portfolio_return",
    )
    realized_vol = (
        portfolio.rolling(_VOL_WINDOW).std()
        * sqrt(_TRADING_DAYS)
    ).rename("realized_vol")
    return pd.concat([portfolio, realized_vol], axis=1).dropna()


def fit_regime_hmm(
    returns: pd.DataFrame | pd.Series,
    n_states: int = 3,
    seed: int = 7,
) -> dict[str, object]:
    """Fit a Gaussian HMM to portfolio return and realised-volatility features.

    The returned ``posteriors`` frame has one integer column per fitted state.
    ``state_labels`` maps those state ids to calm/normal/stress by ascending
    posterior-weighted realised volatility, avoiding any reliance on HMM state
    numbering.  ``random_state=seed`` makes repeated fits deterministic.
    """
    if isinstance(n_states, bool) or n_states not in _STATE_NAMES:
        raise ValueError("n_states must be 2 or 3")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be an integer")

    features = _feature_frame(returns)
    minimum = max(30, n_states * 10)
    if len(features) < minimum:
        raise ValueError(
            f"Gaussian HMM requires at least {minimum} complete feature "
            f"observations after the {_VOL_WINDOW}-day volatility window"
        )

    raw = features.to_numpy(dtype=float)
    center = raw.mean(axis=0)
    scale = raw.std(axis=0)
    if np.any(scale <= 1e-12):
        raise ValueError(
            "Gaussian HMM features have insufficient variation to fit a regime"
        )
    standardized = (raw - center) / scale

    GaussianHMM = _gaussian_hmm_class()
    model = GaussianHMM(
        n_components=n_states,
        covariance_type="full",
        n_iter=300,
        tol=1e-6,
        min_covar=1e-6,
        random_state=seed,
    )
    model.fit(standardized)
    probabilities = model.predict_proba(standardized)
    if not np.isfinite(probabilities).all():
        raise RuntimeError("Gaussian HMM produced non-finite state posteriors")

    posteriors = pd.DataFrame(
        probabilities,
        index=features.index,
        columns=list(range(n_states)),
    )
    observed_vol = features["realized_vol"].to_numpy(dtype=float)
    state_volatility = {
        state: float(np.average(observed_vol, weights=probabilities[:, state]))
        for state in range(n_states)
    }
    ordered_states = sorted(
        state_volatility,
        key=lambda state: (state_volatility[state], state),
    )
    state_labels = {
        state: label
        for state, label in zip(ordered_states, _STATE_NAMES[n_states])
    }

    return {
        "posteriors": posteriors,
        "transition_matrix": np.asarray(model.transmat_, dtype=float).copy(),
        "state_labels": state_labels,
        "state_volatility": state_volatility,
    }
