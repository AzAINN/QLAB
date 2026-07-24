"""Deterministic market-calendar and autopilot trigger evaluation.

The calendar is deliberately dependency-free.  It covers full-day NYSE
closures for 2024--2027 (including the 2025 Carter day of mourning) plus
weekends.  Dates outside that documented window are refused rather than
silently treated as trading days; extend the static table before using the
scheduler beyond 2027.  Early-close sessions are still trading days.
"""

from __future__ import annotations

import calendar
import math
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


_NYSE_TZ = ZoneInfo("America/New_York")
_SUPPORTED_YEARS = frozenset(range(2024, 2028))
_NYSE_HOLIDAYS = frozenset({
    # 2024
    date(2024, 1, 1),
    date(2024, 1, 15),
    date(2024, 2, 19),
    date(2024, 3, 29),
    date(2024, 5, 27),
    date(2024, 6, 19),
    date(2024, 7, 4),
    date(2024, 9, 2),
    date(2024, 11, 28),
    date(2024, 12, 25),
    # 2025
    date(2025, 1, 1),
    date(2025, 1, 9),  # National day of mourning for President Carter.
    date(2025, 1, 20),
    date(2025, 2, 17),
    date(2025, 4, 18),
    date(2025, 5, 26),
    date(2025, 6, 19),
    date(2025, 7, 4),
    date(2025, 9, 1),
    date(2025, 11, 27),
    date(2025, 12, 25),
    # 2026
    date(2026, 1, 1),
    date(2026, 1, 19),
    date(2026, 2, 16),
    date(2026, 4, 3),
    date(2026, 5, 25),
    date(2026, 6, 19),
    date(2026, 7, 3),
    date(2026, 9, 7),
    date(2026, 11, 26),
    date(2026, 12, 25),
    # 2027
    date(2027, 1, 1),
    date(2027, 1, 18),
    date(2027, 2, 15),
    date(2027, 3, 26),
    date(2027, 5, 31),
    date(2027, 6, 18),
    date(2027, 7, 5),
    date(2027, 9, 6),
    date(2027, 11, 25),
    date(2027, 12, 24),
})


def _as_date(value: date | datetime | str, name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be an ISO date") from exc
    raise TypeError(f"{name} must be a date or ISO date string")


_NYSE_REGULAR_CLOSE = time(16, 0)


def is_trading_day(day: date | datetime) -> bool:
    """Return whether ``day`` is a full or early-close NYSE session."""
    day = _as_date(day, "day")
    if day.year not in _SUPPORTED_YEARS:
        raise ValueError(
            f"NYSE calendar supports 2024-2027, not {day.year}; "
            "extend _NYSE_HOLIDAYS before scheduling this date"
        )
    return day.weekday() < 5 and day not in _NYSE_HOLIDAYS


def _is_session(day: date) -> bool:
    """Lenient session predicate for counting across the calendar boundary.

    Unlike :func:`is_trading_day` it never raises outside 2024-2027 — it falls
    back to a weekday check so freshness math on an old bar degrades to "very
    stale" instead of crashing.
    """
    if day.year in _SUPPORTED_YEARS:
        return day.weekday() < 5 and day not in _NYSE_HOLIDAYS
    return day.weekday() < 5


def last_completed_session(
    now: datetime | None = None,
    *,
    close: time = _NYSE_REGULAR_CLOSE,
    finalization_grace: timedelta = timedelta(minutes=20),
) -> date:
    """The most recent NYSE session whose close (+grace) is at or before ``now``.

    Session-aware freshness (plan §6.4): before today's close+grace the prior
    completed session is current, and weekends/holidays never make a
    prior-session bar stale. ``now`` must be timezone-aware.
    """
    current = now or datetime.now(_NYSE_TZ)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(_NYSE_TZ)
    day = current.date()
    for _ in range(10):
        if _is_session(day):
            session_end = (
                datetime.combine(day, close, tzinfo=_NYSE_TZ) + finalization_grace
            )
            if current >= session_end:
                return day
        day -= timedelta(days=1)
    raise ValueError("no completed NYSE session found within 10 days of now")


def sessions_between(
    start_exclusive: date | datetime | str,
    end_inclusive: date | datetime | str,
) -> int:
    """Count NYSE sessions in ``(start_exclusive, end_inclusive]`` (>= 0)."""
    start = _as_date(start_exclusive, "start_exclusive")
    end = _as_date(end_inclusive, "end_inclusive")
    if end <= start:
        return 0
    count = 0
    day = start
    for _ in range(400):  # bound the walk; older than this reads as "very stale"
        day += timedelta(days=1)
        if day > end:
            break
        if _is_session(day):
            count += 1
    return count


def next_rebalance_due(
    last_rebalance_date: date | datetime | str,
    cadence: str,
) -> date:
    """Return the next monthly or quarterly due date, clamped month-end."""
    last = _as_date(last_rebalance_date, "last_rebalance_date")
    normalized = str(cadence).strip().lower()
    months = {"monthly": 1, "quarterly": 3}.get(normalized)
    if months is None:
        raise ValueError("rebalance cadence must be 'monthly' or 'quarterly'")

    month_index = last.year * 12 + last.month - 1 + months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    clamped_day = min(last.day, calendar.monthrange(year, month)[1])
    return date(year, month, clamped_day)


def _state_value(
    state: Mapping[str, object],
    key: str,
    *,
    mandate_attr: str | None = None,
    default: object = None,
) -> object:
    if key in state:
        return state[key]
    mandate = state.get("mandate")
    attr = mandate_attr or key
    if mandate is not None and hasattr(mandate, attr):
        return getattr(mandate, attr)
    return default


def _weights(value: object, name: str) -> dict[str, float]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    parsed: dict[str, float] = {}
    for raw_ticker, raw_weight in value.items():
        ticker = str(raw_ticker)
        try:
            weight = float(raw_weight)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name}.{ticker} must be numeric") from exc
        if not math.isfinite(weight):
            raise ValueError(f"{name}.{ticker} must be finite")
        parsed[ticker] = weight
    return parsed


def _regime_label(value: object) -> str:
    if isinstance(value, Mapping):
        value = value.get("robust_state", value.get("regime"))
    elif hasattr(value, "regime"):
        value = getattr(value, "regime")
    label = str(value or "").strip().lower()
    if label not in {"calm", "normal", "stress", "uncertain"}:
        raise ValueError(
            "robust_regime must be calm, normal, stress, or uncertain"
        )
    return label


def evaluate_triggers(state: Mapping[str, object]) -> list[dict[str, object]]:
    """Evaluate calendar, allocation-drift, and robust-regime triggers.

    ``state`` may carry a ``mandate`` object or the equivalent flattened
    fields.  The output order is stable: calendar, drift, then regime.
    """
    if not isinstance(state, Mapping):
        raise TypeError("scheduler state must be a mapping")
    if "as_of" not in state:
        raise ValueError("scheduler state requires as_of")
    as_of = _as_date(state["as_of"], "as_of")

    cadence = str(_state_value(state, "cadence", default="")).strip().lower()
    if cadence not in {"monthly", "quarterly"}:
        raise ValueError("rebalance cadence must be 'monthly' or 'quarterly'")
    last_raw = state.get("last_rebalance_date")

    triggers: list[dict[str, object]] = []
    if last_raw is None:
        triggers.append({
            "kind": "calendar",
            "detail": {
                "as_of": as_of.isoformat(),
                "cadence": cadence,
                "last_rebalance_date": None,
                "due_date": as_of.isoformat(),
            },
        })
    else:
        last = _as_date(last_raw, "last_rebalance_date")
        due = next_rebalance_due(last, cadence)
        if as_of >= due:
            triggers.append({
                "kind": "calendar",
                "detail": {
                    "as_of": as_of.isoformat(),
                    "cadence": cadence,
                    "last_rebalance_date": last.isoformat(),
                    "due_date": due.isoformat(),
                },
            })

    band_raw = _state_value(state, "drift_band_pct")
    try:
        band = float(band_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("drift_band_pct must be numeric") from exc
    if not math.isfinite(band) or band < 0.0:
        raise ValueError("drift_band_pct must be finite and non-negative")
    current = _weights(state.get("current_weights"), "current_weights")
    targets = _weights(state.get("target_weights"), "target_weights")
    breaches = []
    for ticker in sorted(current.keys() | targets.keys()):
        current_weight = current.get(ticker, 0.0)
        target_weight = targets.get(ticker, 0.0)
        drift = abs(current_weight - target_weight)
        if drift > band:
            breaches.append({
                "ticker": ticker,
                "current": current_weight,
                "target": target_weight,
                "absolute_drift": drift,
            })
    if breaches:
        triggers.append({
            "kind": "drift",
            "detail": {"band_pct": band, "breaches": breaches},
        })

    regime = _regime_label(
        state.get("robust_regime", state.get("regime"))
    )
    regime_enabled = bool(_state_value(state, "regime_triggered", default=False))
    if regime_enabled and regime in {"stress", "uncertain"}:
        defensive = _weights(
            _state_value(state, "defensive_targets"),
            "defensive_targets",
        )
        if not defensive:
            raise ValueError(
                "regime trigger fired without validated defensive_targets"
            )
        triggers.append({
            "kind": "regime",
            "detail": {
                "regime": regime,
                "defensive_targets": defensive,
            },
        })
    return triggers


def next_trading_morning(
    now: datetime | None = None,
    *,
    morning: time = time(9, 30),
) -> datetime:
    """Return the next supported NYSE trading morning in New York time."""
    current = now or datetime.now(_NYSE_TZ)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(_NYSE_TZ)
    candidate = datetime.combine(current.date(), morning, tzinfo=_NYSE_TZ)
    if current >= candidate:
        candidate += timedelta(days=1)
    while not is_trading_day(candidate.date()):
        candidate += timedelta(days=1)
    return candidate
