"""Deterministic Black--Litterman equilibrium returns with uncertainty bands.

The equilibrium prior is reverse-optimized from covariance and a reference
portfolio; it is not a return forecast.  Market-cap weights would be the usual
reference portfolio, but qlab deliberately has no market-cap data dependency.
Callers may supply configured tier weights when available.  Otherwise the
documented substitution is inverse-volatility weights over the requested
universe.

``MomentSet.cov`` is a daily covariance matrix.  :func:`equilibrium_returns`
annualizes it with qlab's 252-trading-day convention before producing the
one-year return and parameter-uncertainty bands.  :func:`implied_returns`
itself is unit-preserving: its output has the same horizon as its covariance
input, which also lets the max-utility objective consume daily coefficients.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np

TRADING_DAYS = 252


def implied_returns(
    cov: np.ndarray,
    weights_prior: Sequence[float] | np.ndarray,
    risk_aversion: float = 2.5,
) -> np.ndarray:
    """Return the Black--Litterman equilibrium prior ``delta * Sigma @ w``.

    ``cov`` and the returned vector share units.  Pass daily covariance for a
    daily objective coefficient or annual covariance for annual reporting.
    The reference weights must be long-only and fully invested.
    """
    covariance = _covariance(cov)
    weights = _prior_weights(weights_prior, covariance.shape[0])
    delta = _positive_float(risk_aversion, "risk_aversion")
    return delta * (covariance @ weights)


def inverse_vol_weights(cov: np.ndarray) -> np.ndarray:
    """Return the market-cap-free default prior: normalized inverse volatility."""
    covariance = _covariance(cov)
    variances = np.diag(covariance)
    if np.any(variances <= 0.0):
        raise ValueError(
            "inverse-volatility prior requires strictly positive asset variances"
        )
    inverse_vol = 1.0 / np.sqrt(variances)
    return inverse_vol / inverse_vol.sum()


def equilibrium_returns(
    cov: np.ndarray,
    tickers: Sequence[str],
    n_obs: int,
    *,
    weights_prior: Mapping[str, float] | Sequence[float] | np.ndarray | None = None,
    target_weights: Mapping[str, float] | Sequence[float] | np.ndarray | None = None,
    risk_aversion: float = 2.5,
    annualization: int = TRADING_DAYS,
) -> dict:
    """Return annualized per-asset and portfolio equilibrium return bands.

    Per-asset parameter uncertainty follows the Black--Litterman prior
    covariance ``tau * Sigma`` with ``tau = 1 / n_obs``.  Portfolio
    uncertainty uses the matching full covariance contraction, rather than
    adding marginal errors as if every asset were perfectly correlated.

    ``target_weights`` may sum to less than one so the current paper book's
    cash allocation remains explicit; cash has zero equilibrium return and
    zero parameter uncertainty.  When omitted, the reference prior is also the
    aggregation portfolio.
    """
    covariance = _covariance(cov)
    names = [str(ticker) for ticker in tickers]
    if len(names) != covariance.shape[0]:
        raise ValueError(
            "ticker count must match covariance dimensions: "
            f"got {len(names)}, expected {covariance.shape[0]}"
        )
    if not names or any(not ticker for ticker in names):
        raise ValueError("tickers must be non-empty strings")
    if len(set(names)) != len(names):
        raise ValueError("tickers must be unique")
    if isinstance(n_obs, bool) or not isinstance(n_obs, (int, np.integer)):
        raise TypeError("n_obs must be an integer")
    if int(n_obs) <= 0:
        raise ValueError("n_obs must be positive")
    if isinstance(annualization, bool) or not isinstance(
        annualization, (int, np.integer)
    ):
        raise TypeError("annualization must be an integer")
    if int(annualization) <= 0:
        raise ValueError("annualization must be positive")

    if weights_prior is None:
        prior = inverse_vol_weights(covariance)
        prior_source = "inverse_volatility"
    else:
        prior = _named_weights(
            weights_prior, names, fully_invested=True, label="weights_prior"
        )
        prior_source = "configured"

    annual_covariance = covariance * float(annualization)
    mu = implied_returns(
        annual_covariance, prior, risk_aversion=risk_aversion
    )
    tau = 1.0 / int(n_obs)
    parameter_covariance = tau * annual_covariance
    sigma_mu = np.sqrt(np.maximum(np.diag(parameter_covariance), 0.0))

    returns = {
        ticker: {
            "mu": float(mu[index]),
            "lo": float(mu[index] - sigma_mu[index]),
            "hi": float(mu[index] + sigma_mu[index]),
        }
        for index, ticker in enumerate(names)
    }

    target = (
        prior
        if target_weights is None
        else _named_weights(
            target_weights, names, fully_invested=False, label="target_weights"
        )
    )
    portfolio_mu = float(target @ mu)
    portfolio_sigma = float(
        np.sqrt(max(float(target @ parameter_covariance @ target), 0.0))
    )
    portfolio = {
        "mu": portfolio_mu,
        "lo": portfolio_mu - portfolio_sigma,
        "hi": portfolio_mu + portfolio_sigma,
        "weights": {
            ticker: float(target[index])
            for index, ticker in enumerate(names)
        },
    }

    return {
        "returns": returns,
        "portfolio": portfolio,
        "prior_weights": {
            ticker: float(prior[index])
            for index, ticker in enumerate(names)
        },
        "prior_weight_source": prior_source,
        "risk_aversion": float(risk_aversion),
        "n_obs": int(n_obs),
        "tau": tau,
        "annualization": int(annualization),
    }


def _covariance(cov: np.ndarray) -> np.ndarray:
    covariance = np.asarray(cov, dtype=float)
    if covariance.ndim != 2 or covariance.shape[0] != covariance.shape[1]:
        raise ValueError("covariance must be a square matrix")
    if covariance.shape[0] == 0:
        raise ValueError("covariance must contain at least one asset")
    if not np.isfinite(covariance).all():
        raise ValueError("covariance must contain only finite values")
    if not np.allclose(covariance, covariance.T, rtol=1e-10, atol=1e-12):
        raise ValueError("covariance must be symmetric")
    if np.any(np.diag(covariance) < 0.0):
        raise ValueError("covariance diagonal cannot contain negative variances")
    return covariance


def _prior_weights(
    weights: Sequence[float] | np.ndarray,
    size: int,
) -> np.ndarray:
    values = np.asarray(weights, dtype=float)
    if values.ndim != 1 or values.shape[0] != size:
        raise ValueError(
            f"weights_prior must contain {size} values, got shape {values.shape}"
        )
    _validate_weights(values, fully_invested=True, label="weights_prior")
    return values


def _named_weights(
    weights: Mapping[str, float] | Sequence[float] | np.ndarray,
    tickers: list[str],
    *,
    fully_invested: bool,
    label: str,
) -> np.ndarray:
    if isinstance(weights, Mapping):
        unknown = sorted(set(weights) - set(tickers))
        if unknown:
            raise ValueError(
                f"{label} contains tickers outside the covariance universe: "
                f"{unknown}"
            )
        values = np.asarray(
            [float(weights.get(ticker, 0.0)) for ticker in tickers],
            dtype=float,
        )
    else:
        values = np.asarray(weights, dtype=float)
        if values.ndim != 1 or values.shape[0] != len(tickers):
            raise ValueError(
                f"{label} must contain {len(tickers)} values, "
                f"got shape {values.shape}"
            )
    _validate_weights(values, fully_invested=fully_invested, label=label)
    return values


def _validate_weights(
    weights: np.ndarray,
    *,
    fully_invested: bool,
    label: str,
) -> None:
    if not np.isfinite(weights).all():
        raise ValueError(f"{label} must contain only finite values")
    if np.any(weights < -1e-12):
        raise ValueError(f"{label} must be long-only")
    total = float(weights.sum())
    if fully_invested and not np.isclose(total, 1.0, rtol=0.0, atol=1e-9):
        raise ValueError(f"{label} must sum to one, got {total}")
    if not fully_invested and total > 1.0 + 1e-9:
        raise ValueError(f"{label} cannot sum above one, got {total}")


def _positive_float(value: float, label: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{label} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{label} must be numeric") from exc
    if not np.isfinite(result) or result <= 0.0:
        raise ValueError(f"{label} must be positive and finite")
    return result
