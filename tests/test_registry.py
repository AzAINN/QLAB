"""DuckDB registry: round-trips, idempotency, trial counting, reflection loop."""

from __future__ import annotations

from datetime import date

from qlab.core.types import Decision, SolveResult, Weights


def test_run_logging_is_idempotent(reg):
    a = reg.log_run("ablation", {"x": 1})
    b = reg.log_run("ablation", {"x": 1})
    assert a == b                                   # content-hashed → same id
    assert len(reg.list_runs()) == 1


def test_moment_set_and_solution_roundtrip(reg, moment_set):
    mid = reg.log_moment_set(moment_set)
    assert mid == moment_set.content_hash()
    res = SolveResult(weights=Weights.equal(moment_set.tickers),
                      objective_value=0.01, solver="mock")
    sid = reg.log_solution("run1", "A1", res, objective_form="min_variance")
    assert sid
    assert reg.trial_count("min_variance") == 1
    assert reg.trial_count() == 1


def test_decision_and_reflection_loop(reg):
    dec = Decision(as_of=date(2022, 6, 30), kind="estimation_window",
                   choice={"window": 504}, rationale="calm regime, data-rich")
    did = reg.log_decision(dec)
    assert did
    reg.update_reflection(did, {"realized_vol": 0.09}, "504 was appropriate")
    rows = reg.recent_decisions(kind="estimation_window")
    assert rows and rows[0]["reflection"] == "504 was appropriate"


def test_account_and_fills(reg):
    reg.init_account(10000.0)
    reg.apply_fill("GLD", 5.0, 180.0, -900.0)
    acct = reg.get_account()
    assert abs(acct["cash"] - 9100.0) < 1e-6
    assert "GLD" in reg.get_positions()


def test_plan_state_machine(reg):
    reg.create_plan("p1", "d1", {"GLD": 1.0}, {"turnover": 1.0})
    assert reg.get_plan("p1")["state"] == "proposed"
    reg.set_plan_state("p1", "checked")
    assert reg.get_plan("p1")["state"] == "checked"
    assert reg.list_plans(1)[0]["plan_id"] == "p1"


def test_events_are_read_in_display_order_with_cursor(reg):
    first = reg.record_event("workflow.started", {"run": 1})
    second = reg.record_event("tool.completed", {"tool": "moments.estimate"})

    initial = reg.read_events()
    assert [row["event_id"] for row in initial] == [first, second]
    assert initial[0]["payload"] == {"run": 1}

    later = reg.read_events(after=initial[0]["ts"])
    assert any(row["event_id"] == second for row in later)


def test_backtest_trial_count_counts_distinct_arms(reg):
    reg.log_backtest("run1", "A1", {"sharpe": 0.5})
    reg.log_backtest("run1", "A2", {"sharpe": 0.6})
    reg.log_backtest("run2", "A1", {"sharpe": 0.7})   # same arm, different run
    assert reg.backtest_trial_count() == 2
