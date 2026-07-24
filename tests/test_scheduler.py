"""Pure scheduler calendar and trigger contracts."""

from __future__ import annotations

from datetime import date

import pytest

from qlab.autopilot.scheduler import (
    evaluate_triggers,
    is_trading_day,
    next_rebalance_due,
)


def _state(**updates):
    targets = {"ACWI": 0.50, "BNDW": 0.50}
    state = {
        "as_of": date(2026, 7, 24),
        "last_rebalance_date": date(2026, 6, 30),
        "cadence": "quarterly",
        "current_weights": dict(targets),
        "target_weights": targets,
        "drift_band_pct": 0.05,
        "robust_regime": "calm",
        "regime_triggered": True,
        "defensive_targets": {"BNDW": 0.60, "GLD": 0.40},
    }
    state.update(updates)
    return state


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 7, 2), True),
        (date(2026, 7, 3), False),  # Independence Day observed.
        (date(2026, 7, 4), False),
        (date(2025, 1, 9), False),  # Carter day of mourning.
        (date(2027, 12, 24), False),  # Christmas observed.
    ],
)
def test_static_nyse_trading_days(day, expected):
    assert is_trading_day(day) is expected


def test_static_calendar_fails_loud_outside_documented_range():
    with pytest.raises(ValueError, match="2024-2027"):
        is_trading_day(date(2028, 1, 3))


@pytest.mark.parametrize(
    ("last", "cadence", "expected"),
    [
        (date(2024, 1, 31), "monthly", date(2024, 2, 29)),
        (date(2024, 1, 31), "quarterly", date(2024, 4, 30)),
        (date(2025, 11, 30), "quarterly", date(2026, 2, 28)),
        (date(2026, 10, 15), "quarterly", date(2027, 1, 15)),
    ],
)
def test_next_rebalance_due_uses_calendar_months(last, cadence, expected):
    assert next_rebalance_due(last, cadence) == expected


def test_evaluate_triggers_detects_one_drift_breach():
    triggers = evaluate_triggers(_state(
        current_weights={"ACWI": 0.56, "BNDW": 0.44},
    ))
    assert [trigger["kind"] for trigger in triggers] == ["drift"]
    assert triggers[0]["detail"]["breaches"] == [{
        "ticker": "ACWI",
        "current": 0.56,
        "target": 0.50,
        "absolute_drift": pytest.approx(0.06),
    }, {
        "ticker": "BNDW",
        "current": 0.44,
        "target": 0.50,
        "absolute_drift": pytest.approx(0.06),
    }]


@pytest.mark.parametrize("regime", ["stress", "uncertain"])
def test_evaluate_triggers_uses_robust_regime_for_defensive_proposal(regime):
    triggers = evaluate_triggers(_state(robust_regime=regime))
    assert [trigger["kind"] for trigger in triggers] == ["regime"]
    assert triggers[0]["detail"]["defensive_targets"] == {
        "BNDW": 0.60,
        "GLD": 0.40,
    }


def test_evaluate_triggers_detects_calendar_due():
    triggers = evaluate_triggers(_state(
        last_rebalance_date=date(2026, 4, 24),
    ))
    assert [trigger["kind"] for trigger in triggers] == ["calendar"]
    assert triggers[0]["detail"]["due_date"] == "2026-07-24"
