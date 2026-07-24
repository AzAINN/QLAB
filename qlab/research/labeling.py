"""Research-only labeling and deterministic confidence sizing primitives.

This module is a library scaffold for a future prediction lane. It produces no
signal and has no registration, agent, staged-solver, or execution path.
"""

from __future__ import annotations

from collections.abc import Iterable
import math
from numbers import Real
from typing import Literal

import numpy as np
import pandas as pd


SizingScheme = Literal["linear", "threshold", "convex"]
_SIDES = np.array([-1.0, 0.0, 1.0])


def _positive_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a real number")
    converted = float(value)
    if not math.isfinite(converted) or converted <= 0.0:
        raise ValueError(f"{name} must be finite and positive")
    return converted


def _positive_int(value: object, name: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _validated_prices(prices: pd.Series) -> pd.Series:
    if not isinstance(prices, pd.Series):
        raise TypeError("prices must be a pandas Series")
    if prices.empty:
        raise ValueError("prices must not be empty")
    if not prices.index.is_unique or not prices.index.is_monotonic_increasing:
        raise ValueError("prices index must be unique and increasing")
    if prices.index.hasnans:
        raise ValueError("prices index must not contain missing labels")
    try:
        clean = prices.astype(float)
    except (TypeError, ValueError) as exc:
        raise TypeError("prices must be numeric") from exc
    values = clean.to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError(
            "prices must be complete and finite; NaN values cannot be labeled"
        )
    if np.any(values <= 0.0):
        raise ValueError("prices must be strictly positive")
    return clean


def _validated_events(
    events_index: Iterable[object],
    price_index: pd.Index,
) -> tuple[pd.Index, np.ndarray]:
    if isinstance(events_index, (str, bytes)):
        raise TypeError("events_index must be an iterable of price-index labels")
    try:
        events = pd.Index(events_index)
    except (TypeError, ValueError) as exc:
        raise TypeError(
            "events_index must be an iterable of price-index labels"
        ) from exc
    if events.empty:
        raise ValueError("events_index must contain at least one event")
    if events.hasnans:
        raise ValueError("events_index must not contain missing labels")
    if not events.is_unique:
        raise ValueError("events_index must contain unique labels")
    positions = price_index.get_indexer(events)
    if np.any(positions < 0):
        missing = events[positions < 0].tolist()
        raise ValueError(f"events are absent from the price index: {missing!r}")
    return events, positions


def triple_barrier_labels(
    prices: pd.Series,
    events_index: Iterable[object],
    k_up: float = 2.0,
    k_down: float = 1.0,
    horizon_days: int = 21,
    vol_lookback: int = 20,
) -> pd.DataFrame:
    """Label events by the first volatility-scaled barrier touched.

    Volatility at an event is the sample standard deviation of the trailing
    ``vol_lookback`` one-session percentage returns, including the return on
    the event date. Horizontal barriers are fixed around that date's entry
    price. Prices from the next observation through exactly ``horizon_days``
    observations later are scanned in order; if neither horizontal barrier is
    touched, that final observation is the vertical-barrier touch.

    Every event must have a complete volatility lookback and complete forward
    horizon. The function refuses the entire request rather than returning
    partially conditioned labels.
    """
    clean = _validated_prices(prices)
    up_multiple = _positive_float(k_up, "k_up")
    down_multiple = _positive_float(k_down, "k_down")
    horizon = _positive_int(horizon_days, "horizon_days")
    lookback = _positive_int(vol_lookback, "vol_lookback", minimum=2)
    events, positions = _validated_events(events_index, clean.index)

    volatility = (
        clean.pct_change(fill_method=None)
        .rolling(lookback, min_periods=lookback)
        .std(ddof=1)
    )
    values = clean.to_numpy(dtype=float)
    records: list[dict[str, object]] = []
    for event, position_value in zip(events, positions):
        position = int(position_value)
        if position < lookback:
            raise ValueError(
                f"event {event!r} has insufficient history for a "
                f"{lookback}-return volatility lookback"
            )
        vertical_position = position + horizon
        if vertical_position >= len(clean):
            raise ValueError(
                f"event {event!r} has insufficient forward history for a "
                f"{horizon}-day vertical barrier"
            )

        sigma = float(volatility.iloc[position])
        if not math.isfinite(sigma):
            raise ValueError(
                f"event {event!r} has an undefined volatility estimate"
            )
        if sigma <= 0.0:
            raise ValueError(
                f"event {event!r} has non-positive volatility; barriers "
                "would be degenerate"
            )

        entry_price = values[position]
        upper_barrier = entry_price * (1.0 + up_multiple * sigma)
        lower_barrier = entry_price * (1.0 - down_multiple * sigma)
        label = 0
        touch_position = vertical_position
        for candidate in range(position + 1, vertical_position + 1):
            candidate_price = values[candidate]
            if candidate_price >= upper_barrier:
                label = 1
                touch_position = candidate
                break
            if candidate_price <= lower_barrier:
                label = -1
                touch_position = candidate
                break

        records.append({
            "label": label,
            "touch_date": clean.index[touch_position],
            "return_at_touch": values[touch_position] / entry_price - 1.0,
        })

    result = pd.DataFrame.from_records(
        records,
        index=events,
        columns=["label", "touch_date", "return_at_touch"],
    )
    result["label"] = result["label"].astype(int)
    result["return_at_touch"] = result["return_at_touch"].astype(float)
    return result


def meta_labels(
    primary_side: pd.Series,
    realized_labels: pd.DataFrame,
) -> tuple[pd.Series, dict[str, float | int]]:
    """Score whether an aligned primary side agreed and made money.

    A long is profitable only when the realized return is positive; a short is
    profitable only when it is negative. A zero side never receives a positive
    meta-label.
    """
    if not isinstance(primary_side, pd.Series):
        raise TypeError("primary_side must be a pandas Series")
    if not isinstance(realized_labels, pd.DataFrame):
        raise TypeError("realized_labels must be a pandas DataFrame")
    required = {"label", "return_at_touch"}
    missing_columns = required.difference(realized_labels.columns)
    if missing_columns:
        raise ValueError(
            "realized_labels is missing required columns: "
            f"{sorted(missing_columns)!r}"
        )
    if primary_side.empty or realized_labels.empty:
        raise ValueError("primary_side and realized_labels must not be empty")
    if not primary_side.index.is_unique or not realized_labels.index.is_unique:
        raise ValueError("primary_side and realized_labels indexes must be unique")
    if primary_side.index.hasnans or realized_labels.index.hasnans:
        raise ValueError(
            "primary_side and realized_labels indexes must not contain "
            "missing labels"
        )

    missing_sides = realized_labels.index[
        ~realized_labels.index.isin(primary_side.index)
    ]
    extra_sides = primary_side.index[
        ~primary_side.index.isin(realized_labels.index)
    ]
    if len(missing_sides) or len(extra_sides):
        raise ValueError(
            "primary_side and realized_labels must cover the same events; "
            f"missing sides={missing_sides.tolist()!r}, "
            f"extra sides={extra_sides.tolist()!r}"
        )

    try:
        aligned_side = primary_side.reindex(realized_labels.index).astype(float)
        outcomes = realized_labels["label"].astype(float)
        returns = realized_labels["return_at_touch"].astype(float)
    except (TypeError, ValueError) as exc:
        raise TypeError(
            "primary sides, realized labels, and realized returns must be numeric"
        ) from exc
    side_values = aligned_side.to_numpy(dtype=float)
    outcome_values = outcomes.to_numpy(dtype=float)
    return_values = returns.to_numpy(dtype=float)
    if not (
        np.isfinite(side_values).all()
        and np.isfinite(outcome_values).all()
        and np.isfinite(return_values).all()
    ):
        raise ValueError(
            "primary sides, realized labels, and realized returns must be finite"
        )
    if not np.isin(side_values, _SIDES).all():
        raise ValueError("primary_side values must be in {-1, 0, 1}")
    if not np.isin(outcome_values, _SIDES).all():
        raise ValueError("realized label values must be in {-1, 0, 1}")

    agreed = side_values == outcome_values
    profitable = side_values * return_values > 0.0
    meta = pd.Series(
        (agreed & profitable).astype(int),
        index=realized_labels.index,
        name="meta_label",
    )
    summary: dict[str, float | int] = {
        "hit_rate": float(meta.mean()),
        "n": int(len(meta)),
    }
    return meta, summary


def confidence_to_size(
    confidence: float | pd.Series,
    scheme: SizingScheme,
    floor: float = 0.0,
    cap: float = 1.0,
    threshold: float = 0.6,
) -> float | pd.Series:
    """Map model confidence to size in deterministic code.

    This function is the only sanctioned path from model confidence to a
    position size. Models may express confidence; they never choose size.
    Linear sizing uses confidence directly, threshold sizing maps values below
    ``threshold`` to zero, and convex sizing squares confidence. The resulting
    size is always clamped to ``[floor, cap]``.
    """
    if scheme not in {"linear", "threshold", "convex"}:
        raise ValueError(
            "scheme must be one of {'linear', 'threshold', 'convex'}"
        )
    for value, name in (
        (floor, "floor"),
        (cap, "cap"),
        (threshold, "threshold"),
    ):
        if isinstance(value, bool) or not isinstance(value, Real):
            raise TypeError(f"{name} must be a real number")
        if not math.isfinite(float(value)):
            raise ValueError(f"{name} must be finite")
    lower = float(floor)
    upper = float(cap)
    cutoff = float(threshold)
    if not 0.0 <= lower <= upper <= 1.0:
        raise ValueError("floor and cap must satisfy 0 <= floor <= cap <= 1")
    if not 0.0 <= cutoff <= 1.0:
        raise ValueError("threshold must be between 0 and 1")

    is_series = isinstance(confidence, pd.Series)
    if is_series:
        if confidence.empty:
            raise ValueError("confidence Series must not be empty")
        try:
            values = confidence.to_numpy(dtype=float)
        except (TypeError, ValueError) as exc:
            raise TypeError("confidence values must be numeric") from exc
    else:
        if isinstance(confidence, bool) or not isinstance(confidence, Real):
            raise TypeError("confidence must be a real number or pandas Series")
        values = np.asarray([float(confidence)], dtype=float)
    if not np.isfinite(values).all():
        raise ValueError("confidence values must be finite")

    if scheme == "linear":
        mapped = values
    elif scheme == "threshold":
        mapped = np.where(values < cutoff, 0.0, values)
    else:
        mapped = np.square(values)
    sized = np.clip(mapped, lower, upper)

    if is_series:
        return pd.Series(
            sized,
            index=confidence.index,
            name=confidence.name,
            dtype=float,
        )
    return float(sized[0])
