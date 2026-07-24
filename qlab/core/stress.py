"""Deterministic portfolio stress helpers.

These are scenario calculations, not forecasts. They stay pure so the referee,
owner runtime, and offline tests can apply exactly the same arithmetic.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

import numpy as np
import pandas as pd


DEFAULT_REPLAY_WINDOWS: dict[str, tuple[str, str]] = {
    "2008": ("2008-09-01", "2009-03-09"),
    "2020": ("2020-02-19", "2020-03-23"),
    "2022": ("2022-01-03", "2022-10-12"),
}


def stress_correlation_to_one(
    weights: Mapping[str, float] | Sequence[float],
    vols: Mapping[str, float] | Sequence[float],
) -> float:
    """Return portfolio volatility under the bound where every correlation is 1."""
    if isinstance(weights, Mapping):
        if not isinstance(vols, Mapping):
            raise TypeError("mapping weights require mapping vols")
        missing = sorted(set(weights) - set(vols))
        if missing:
            raise ValueError(f"vols missing weights for: {', '.join(missing)}")
        keys = list(weights)
        weight_values = np.asarray([weights[key] for key in keys], dtype=float)
        vol_values = np.asarray([vols[key] for key in keys], dtype=float)
    else:
        if isinstance(vols, Mapping):
            raise TypeError("sequence weights require sequence vols")
        weight_values = np.asarray(weights, dtype=float)
        vol_values = np.asarray(vols, dtype=float)

    if weight_values.ndim != 1 or vol_values.ndim != 1:
        raise ValueError("weights and vols must be one-dimensional")
    if len(weight_values) != len(vol_values):
        raise ValueError("weights and vols length mismatch")
    if not np.all(np.isfinite(weight_values)) or not np.all(np.isfinite(vol_values)):
        raise ValueError("weights and vols must be finite")
    if np.any(vol_values < 0):
        raise ValueError("vols must be non-negative")
    return float(np.dot(np.abs(weight_values), vol_values))


def replay_scenarios(
    weights: Mapping[str, float] | Sequence[float],
    prices: pd.DataFrame,
    windows: Mapping[str, tuple[str | date, str | date]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Replay fixed-weight portfolio returns over covered historical windows.

    A synthetic panel is useful for exercising the desk, but it is not historical
    evidence. Such panels therefore return explicit unavailable rows rather than
    plausible-looking scenario numbers.
    """
    if not isinstance(prices, pd.DataFrame):
        raise TypeError("prices must be a pandas DataFrame")
    if prices.empty or not len(prices.columns):
        raise ValueError("prices must contain at least one asset and observation")

    requested = DEFAULT_REPLAY_WINDOWS if windows is None else windows
    if not isinstance(requested, Mapping):
        raise TypeError("windows must be a mapping")

    weight_values = _weights_for_columns(weights, list(prices.columns))
    normalized_windows = {
        str(label): _window_bounds(label, bounds)
        for label, bounds in requested.items()
    }

    source = str(prices.attrs.get("source") or "").strip().lower()
    is_synthetic = bool(prices.attrs.get("synthetic")) or source == "synthetic"
    if is_synthetic:
        return {
            label: _unavailable(
                start,
                end,
                "unavailable: synthetic snapshot is not historical replay data",
            )
            for label, (start, end) in normalized_windows.items()
        }

    panel = prices.copy(deep=False)
    try:
        panel.index = pd.to_datetime(panel.index)
    except (TypeError, ValueError) as exc:
        raise ValueError("prices index must contain dates") from exc
    if panel.index.hasnans:
        raise ValueError("prices index contains invalid dates")
    panel = panel.sort_index()

    first_panel_date = panel.index[0].normalize()
    last_panel_date = panel.index[-1].normalize()
    results: dict[str, dict[str, Any]] = {}
    for label, (start, end) in normalized_windows.items():
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        if first_panel_date > start_ts or last_panel_date < end_ts:
            results[label] = _unavailable(
                start,
                end,
                "unavailable: snapshot span does not cover this window",
            )
            continue

        window = panel.loc[(panel.index >= start_ts) & (panel.index <= end_ts)]
        window = window.dropna(how="any")
        if len(window) < 2:
            results[label] = _unavailable(
                start,
                end,
                "unavailable: fewer than two complete observations",
            )
            continue
        first = window.iloc[0].to_numpy(dtype=float)
        last = window.iloc[-1].to_numpy(dtype=float)
        if (
            not np.all(np.isfinite(first))
            or not np.all(np.isfinite(last))
            or np.any(first <= 0)
        ):
            raise ValueError(f"prices contain invalid endpoints for {label}")
        asset_returns = last / first - 1.0
        results[label] = {
            "available": True,
            "start": window.index[0].date().isoformat(),
            "end": window.index[-1].date().isoformat(),
            "return": float(np.dot(weight_values, asset_returns)),
            "reason": None,
        }
    return results


def _weights_for_columns(
    weights: Mapping[str, float] | Sequence[float],
    columns: list[object],
) -> np.ndarray:
    if isinstance(weights, Mapping):
        unknown = sorted(str(key) for key in set(weights) - set(columns))
        if unknown:
            raise ValueError(f"weights reference missing price columns: {', '.join(unknown)}")
        values = np.asarray([weights.get(column, 0.0) for column in columns], dtype=float)
    else:
        values = np.asarray(weights, dtype=float)
        if values.ndim != 1 or len(values) != len(columns):
            raise ValueError("sequence weights must match price columns")
    if values.ndim != 1 or not np.all(np.isfinite(values)):
        raise ValueError("weights must be a finite one-dimensional vector")
    return values


def _window_bounds(
    label: object,
    bounds: tuple[str | date, str | date],
) -> tuple[str, str]:
    if not isinstance(bounds, (tuple, list)) or len(bounds) != 2:
        raise ValueError(f"window {label!r} must contain start and end dates")
    try:
        start = pd.Timestamp(bounds[0]).date()
        end = pd.Timestamp(bounds[1]).date()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"window {label!r} contains an invalid date") from exc
    if start >= end:
        raise ValueError(f"window {label!r} must start before it ends")
    return start.isoformat(), end.isoformat()


def _unavailable(start: str, end: str, reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "start": start,
        "end": end,
        "return": None,
        "reason": reason,
    }
