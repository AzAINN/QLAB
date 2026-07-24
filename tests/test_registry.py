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


def test_tool_calls_emit_events(reg):
    from qlab.mcp.guardrails import LabState

    state = LabState(offline=True, registry=reg)
    state.budget.charge("data.fetch_universe")

    events = reg.read_events(10)
    assert any(
        event["kind"] == "tool_call"
        and event["payload"] == {"tool": "data.fetch_universe"}
        for event in events
    )


def test_backtest_trial_count_counts_distinct_arms(reg):
    reg.log_backtest("run1", "A1", {"sharpe": 0.5})
    reg.log_backtest("run1", "A2", {"sharpe": 0.6})
    reg.log_backtest("run2", "A1", {"sharpe": 0.7})   # same arm, different run
    assert reg.backtest_trial_count() == 2


def test_verdicts_for_returns_latest_verdict_per_decision(reg):
    dec = Decision(as_of=date(2022, 6, 30), kind="rebalance_gate",
                   choice={"targets": {"GLD": 1.0}}, rationale="calm regime")
    did = reg.log_decision(dec)
    # two verdicts on the same decision; the second (higher seq) must win
    reg.log_verdict(did, "FAIL", ["turnover too high"],
                    source="deterministic", targets={"GLD": 1.0})
    reg.log_verdict(did, "PASS", ["within mandate"],
                    source="deterministic", targets={"GLD": 1.0})

    out = reg.verdicts_for([did])
    assert out[did]["verdict"] == "PASS"                 # latest wins (seq DESC)
    assert out[did]["reasons"] == ["within mandate"]      # reasons parsed to list
    assert out[did]["source"] == "deterministic"

    assert reg.verdicts_for([]) == {}                     # empty input → {}
    assert reg.verdicts_for(["unknown"]) == {}            # no verdict → absent


def test_workforce_is_durable_ordered_and_role_bound(reg):
    workflow = reg.start_workflow("portfolio_review", {"goal": "review"})
    workflow_id = workflow["workflow_id"]
    assert [step["phase"] for step in workflow["steps"]] == [
        "analyst", "challenger", "optimizer", "referee", "reporter"
    ]
    assert workflow["steps"][0]["agent"] == "moments-analyst"

    import pytest

    with pytest.raises(RuntimeError, match="cannot start"):
        reg.update_workflow_phase(workflow_id, "optimizer", "working")

    targets = {"GLD": 1.0}
    verdict_id = reg.log_verdict("d1", "PASS", ["within mandate"], targets=targets)
    artifacts = {
        "analyst": {"moment_set_id": "m1", "objective_id": "o1",
                    "decision_id": "d1", "regime": "neutral",
                    "regime_summary": "mixed data; news backdrop balanced"},
        "challenger": {"challenger_view": "shorter window may react faster"},
        "optimizer": {"targets": targets, "algorithm_id": "hrp"},
        "referee": {"verdict": "PASS", "verdict_id": verdict_id,
                    "targets": targets},
        "reporter": {"recommendation": "hold reviewed HRP targets"},
    }
    for phase in ("analyst", "challenger", "optimizer", "referee", "reporter"):
        reg.update_workflow_phase(workflow_id, phase, "working")
        workflow = reg.update_workflow_phase(
            workflow_id, phase, "done", summary=f"{phase} complete",
            artifacts=artifacts[phase],
        )

    assert workflow["status"] == "complete"
    assert workflow["result"] == {
        "final_summary": "reporter complete",
        "artifacts": artifacts["reporter"],
    }
    assert reg.get_workflow(workflow_id)["steps"][-1]["status"] == "done"

    # A retried/replayed "done" on a finished workflow is an idempotent no-op:
    # it must not flip the workflow back to running or wipe the result.
    replay = reg.update_workflow_phase(
        workflow_id, "referee", "done", summary="replayed",
        artifacts=artifacts["referee"],
    )
    assert replay["status"] == "complete"
    assert replay["result"]["final_summary"] == "reporter complete"


def test_workforce_challenger_and_optimizer_run_in_parallel_after_analyst(reg):
    """Challenger and optimizer depend only on the analyst, so the optimizer may
    start without waiting for the challenger — but the referee still waits for
    both, so nothing is *used* before the judgment is defended."""
    import pytest

    workflow = reg.start_workflow("portfolio_review", {"goal": "review"})
    workflow_id = workflow["workflow_id"]

    # Nothing downstream may start before the analyst is done.
    with pytest.raises(RuntimeError, match="cannot start"):
        reg.update_workflow_phase(workflow_id, "optimizer", "working")

    reg.update_workflow_phase(
        workflow_id, "analyst", "done",
        artifacts={"moment_set_id": "m1", "objective_id": "o1", "decision_id": "d1",
                   "regime": "neutral", "regime_summary": "offline synthetic backdrop"},
    )

    # With the analyst done, challenger and optimizer are concurrent: the
    # optimizer starts and finishes without the challenger having begun.
    reg.update_workflow_phase(workflow_id, "optimizer", "working")
    out_of_order = reg.update_workflow_phase(
        workflow_id, "optimizer", "done",
        artifacts={"targets": {"GLD": 1.0}, "algorithm_id": "hrp"},
    )
    # Finishing out of seq order must report what is still open, not seq+1.
    assert out_of_order["current_phase"] == "challenger"
    assert out_of_order["status"] == "running"

    # The referee is the join point and cannot start until BOTH are done.
    with pytest.raises(RuntimeError, match="cannot start before 'challenger'"):
        reg.update_workflow_phase(workflow_id, "referee", "working")

    reg.update_workflow_phase(
        workflow_id, "challenger", "done", artifacts={"challenger_view": "c"},
    )
    referee_ready = reg.update_workflow_phase(workflow_id, "referee", "working")
    states = {step["phase"]: step["status"] for step in referee_ready["steps"]}
    assert states["challenger"] == "done" and states["optimizer"] == "done"
    assert states["referee"] == "working"


def test_workforce_refuses_unstructured_completion_and_referee_fail(reg):
    import pytest

    workflow = reg.start_workflow("portfolio_review", {"goal": "review"})
    workflow_id = workflow["workflow_id"]
    reg.update_workflow_phase(workflow_id, "analyst", "working")
    with pytest.raises(ValueError, match="cannot complete without artifacts"):
        reg.update_workflow_phase(workflow_id, "analyst", "done")

    reg.update_workflow_phase(
        workflow_id, "analyst", "done",
        artifacts={"moment_set_id": "m1", "objective_id": "o1", "decision_id": "d1",
                   "regime": "neutral", "regime_summary": "offline synthetic backdrop"},
    )
    reg.update_workflow_phase(
        workflow_id, "challenger", "done",
        artifacts={"challenger_view": "challenge"},
    )
    reg.update_workflow_phase(
        workflow_id, "optimizer", "done",
        artifacts={"targets": {"GLD": 1.0}, "algorithm_id": "hrp"},
    )
    with pytest.raises(ValueError, match="use blocked for FAIL"):
        reg.update_workflow_phase(
            workflow_id, "referee", "done",
            artifacts={"verdict": "FAIL", "verdict_id": "v1",
                       "targets": {"GLD": 1.0}},
        )


def test_workforce_referee_pass_is_bound_to_optimizer_targets(reg):
    """A PASS whose targets differ from the optimizer's cannot complete."""
    import pytest

    workflow = reg.start_workflow("portfolio_review", {"goal": "review"})
    workflow_id = workflow["workflow_id"]
    reg.update_workflow_phase(
        workflow_id, "analyst", "done",
        artifacts={"moment_set_id": "m1", "objective_id": "o1", "decision_id": "d1",
                   "regime": "neutral", "regime_summary": "offline synthetic backdrop"},
    )
    reg.update_workflow_phase(
        workflow_id, "challenger", "done", artifacts={"challenger_view": "c"},
    )
    optimizer_targets = {"GLD": 0.5, "EMB": 0.5}
    reg.update_workflow_phase(
        workflow_id, "optimizer", "done",
        artifacts={"targets": optimizer_targets, "algorithm_id": "hrp"},
    )

    # PASS logged for a different, still-in-mandate vector: refuse completion.
    rogue = {"GLD": 1.0}
    rogue_vid = reg.log_verdict("d1", "PASS", ["looks fine"], targets=rogue)
    with pytest.raises(ValueError, match="do not match the optimizer"):
        reg.update_workflow_phase(
            workflow_id, "referee", "done",
            artifacts={"verdict": "PASS", "verdict_id": rogue_vid, "targets": rogue},
        )

    # Referee claims the optimizer's targets but its verdict row is bound to
    # a different hash: refuse.
    with pytest.raises(ValueError, match="persisted PASS bound"):
        reg.update_workflow_phase(
            workflow_id, "referee", "done",
            artifacts={"verdict": "PASS", "verdict_id": rogue_vid,
                       "targets": optimizer_targets},
        )

    # A fabricated verdict_id with no persisted row: refuse.
    with pytest.raises(ValueError, match="persisted PASS bound"):
        reg.update_workflow_phase(
            workflow_id, "referee", "done",
            artifacts={"verdict": "PASS", "verdict_id": "nope",
                       "targets": optimizer_targets},
        )

    good_vid = reg.log_verdict(
        "d1", "PASS", ["within mandate"], targets=optimizer_targets)
    done = reg.update_workflow_phase(
        workflow_id, "referee", "done",
        artifacts={"verdict": "PASS", "verdict_id": good_vid,
                   "targets": optimizer_targets},
    )
    assert done["current_phase"] == "reporter"


def test_challenger_view_attaches_to_existing_decision(reg):
    dec = Decision(
        as_of=date(2022, 6, 30), kind="estimation_window",
        choice={"window": 504}, rationale="stable covariance",
    )
    decision_id = reg.log_decision(dec)
    reg.attach_challenger_view(decision_id, "A shorter window may catch the regime.")
    assert reg.recent_decisions(limit=1)[0]["challenger_view"].startswith("A shorter")
