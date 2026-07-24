"""Research-only factor covariance estimation for stock return panels."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from qlab.core.moments import ledoit_wolf

_FACTOR_CONDITION_CAP = 1e10
_VARIANCE_FLOOR = 1e-12


@dataclass(frozen=True)
class FactorModel:
    """Estimated factor loadings and covariance components in input-column order."""

    B: np.ndarray
    Sigma_f: np.ndarray
    D: np.ndarray
    stock_names: tuple[str, ...] = ()
    factor_names: tuple[str, ...] = ()

    @property
    def covariance(self) -> np.ndarray:
        """Return the symmetric positive-semidefinite stock covariance."""
        covariance = self.B @ self.Sigma_f @ self.B.T + np.diag(self.D)
        return _psd_floor(covariance)


def factor_covariance(
    stock_returns: pd.DataFrame,
    factor_returns: pd.DataFrame,
    min_obs: int = 120,
) -> FactorModel:
    """Estimate a linear factor covariance model on overlapping observations."""
    if not isinstance(min_obs, int) or isinstance(min_obs, bool) or min_obs < 2:
        raise ValueError("min_obs must be an integer of at least 2")
    _validate_panel(stock_returns, "stock_returns")
    _validate_panel(factor_returns, "factor_returns")

    stocks, factors = stock_returns.align(factor_returns, join="inner", axis=0)
    observations = len(stocks)
    if observations < min_obs:
        raise ValueError(
            "insufficient overlapping observations: "
            f"found {observations}, require at least {min_obs}"
        )

    stock_values = _finite_values(stocks, "stock_returns")
    factor_values = _finite_values(factors, "factor_returns")
    n_factors = factor_values.shape[1]
    residual_dof = observations - n_factors - 1
    if residual_dof <= 0:
        raise ValueError(
            "insufficient overlapping observations for factor OLS: "
            f"{observations} observations for {n_factors} factors"
        )

    centered_factors = factor_values - factor_values.mean(axis=0, keepdims=True)
    centered_stocks = stock_values - stock_values.mean(axis=0, keepdims=True)
    _validate_factor_condition(centered_factors)

    coefficients, _, rank, _ = np.linalg.lstsq(
        centered_factors, centered_stocks, rcond=None
    )
    if rank < n_factors:
        raise ValueError("factor columns are collinear; OLS design is rank deficient")

    residuals = centered_stocks - centered_factors @ coefficients
    residual_variances = np.sum(residuals**2, axis=0) / residual_dof
    residual_variances = np.maximum(residual_variances, _VARIANCE_FLOOR)
    factor_cov, _ = ledoit_wolf(factor_values)

    return FactorModel(
        B=coefficients.T,
        Sigma_f=np.atleast_2d(factor_cov),
        D=residual_variances,
        stock_names=tuple(str(column) for column in stocks.columns),
        factor_names=tuple(str(column) for column in factors.columns),
    )


def _validate_panel(panel: pd.DataFrame, name: str) -> None:
    if not isinstance(panel, pd.DataFrame):
        raise TypeError(f"{name} must be a pandas DataFrame")
    if panel.shape[1] == 0:
        raise ValueError(f"{name} must contain at least one column")
    if panel.columns.has_duplicates:
        raise ValueError(f"{name} columns must be unique")
    if panel.index.has_duplicates:
        raise ValueError(f"{name} index must be unique")


def _finite_values(panel: pd.DataFrame, name: str) -> np.ndarray:
    nan_columns = [str(column) for column in panel.columns[panel.isna().any()]]
    if nan_columns:
        raise ValueError(
            f"{name} contains NaN values in columns: {', '.join(nan_columns)}"
        )
    try:
        values = panel.to_numpy(dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain numeric returns") from exc
    if not np.isfinite(values).all():
        raise ValueError(f"{name} must contain only finite returns")
    return values


def _validate_factor_condition(centered_factors: np.ndarray) -> None:
    scales = np.linalg.norm(centered_factors, axis=0)
    if np.any(scales <= np.finfo(float).eps):
        condition_number = float("inf")
    else:
        condition_number = float(np.linalg.cond(centered_factors / scales))
    if (
        not np.isfinite(condition_number)
        or condition_number > _FACTOR_CONDITION_CAP
    ):
        raise ValueError(
            "factor columns are collinear or ill-conditioned: "
            f"condition number {condition_number:.3e} exceeds "
            f"{_FACTOR_CONDITION_CAP:.1e}"
        )


def _psd_floor(covariance: np.ndarray) -> np.ndarray:
    symmetric = (covariance + covariance.T) / 2.0
    eigenvalues, eigenvectors = np.linalg.eigh(symmetric)
    floored = (
        eigenvectors * np.clip(eigenvalues, _VARIANCE_FLOOR, None)
    ) @ eigenvectors.T
    return (floored + floored.T) / 2.0
