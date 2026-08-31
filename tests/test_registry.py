"""DuckDB registry: round-trips, idempotency, trial counting, reflection loop."""

from __future__ import annotations

from datetime import date

import pytest

from qlab.core.types import Decision, SolveResult, Weights
from qlab.state.registry import Registry


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


def test_recall_similar_decisions_ranks_nearest_reflected_regime_first(reg):
    def reflected(
        as_of: date,
        vol_percentile: float,
        turbulence_percentile: float,
        regime_label: str,
        reflection: str,
    ) -> str:
        decision_id = reg.log_decision(Decision(
            as_of=as_of,
            kind="regime",
            choice={
                "vol_percentile": vol_percentile,
                "turbulence_percentile": turbulence_percentile,
                "regime_label": regime_label,
                "regime": regime_label,
            },
            rationale=f"fixture {reflection}",
        ))
        reg.update_reflection(
            decision_id,
            {"regime_call": regime_label},
            reflection,
        )
        return decision_id

    nearest_id = reflected(
        date(2022, 1, 31), 0.78, 0.74, "stress", "nearest lesson"
    )
    farther_id = reflected(
        date(2022, 2, 28), 0.20, 0.15, "calm", "farther lesson"
    )
    reg.log_decision(Decision(
        as_of=date(2022, 3, 31),
        kind="regime",
        choice={
            "vol_percentile": 0.80,
            "turbulence_percentile": 0.75,
            "regime_label": "stress",
        },
        rationale="unresolved exact match must not be recalled",
    ))

    recalled = reg.recall_similar_decisions(
        {
            "vol_percentile": 0.80,
            "turbulence_percentile": 0.75,
            "regime_label": "stress",
        },
        kind="regime",
        limit=2,
    )

    assert [row["decision_id"] for row in recalled] == [nearest_id, farther_id]
    assert recalled[0]["reflection"] == "nearest lesson"
    assert recalled[0]["similarity_score"] > recalled[1]["similarity_score"]


def test_recall_is_point_in_time_and_excludes_future_outcomes(reg):
    def reflected(as_of, window_end, label="stress"):
        did = reg.log_decision(Decision(
            as_of=as_of, kind="regime",
            choice={"vol_percentile": 0.80, "turbulence_percentile": 0.75,
                    "regime_label": label, "regime": label},
            rationale="fixture"))
        reg.update_reflection(did, {"regime_call": label, "window_end": window_end},
                              "lesson")
        return did

    past = reflected(date(2022, 1, 31), "2022-02-15")     # resolved before query
    reflected(date(2022, 2, 28), "2022-03-30")            # outcome closes AFTER query
    reflected(date(2022, 3, 15), "2022-03-20")            # decided AFTER query

    recalled = reg.recall_similar_decisions(
        {"vol_percentile": 0.80, "turbulence_percentile": 0.75,
         "regime_label": "stress"},
        kind="regime", limit=5, as_of="2022-03-01")
    # Only the fully-resolved-before-query decision may be recalled.
    assert [row["decision_id"] for row in recalled] == [past]


def test_recall_min_similarity_drops_weak_matches(reg):
    did = reg.log_decision(Decision(
        as_of=date(2022, 1, 31), kind="regime",
        choice={"vol_percentile": 0.10, "turbulence_percentile": 0.05,
                "regime_label": "calm", "regime": "calm"},
        rationale="far"))
    reg.update_reflection(did, {"regime_call": "calm"}, "lesson")
    # Query a very different (stress) regime with a high threshold.
    recalled = reg.recall_similar_decisions(
        {"vol_percentile": 0.95, "turbulence_percentile": 0.95,
         "regime_label": "stress"},
        kind="regime", limit=5, min_similarity=0.6)
    assert recalled == []


def test_reflection_outcome_is_write_once(reg):
    did = reg.log_decision(Decision(
        as_of=date(2022, 1, 31), kind="regime",
        choice={"regime": "stress"}, rationale="fixture"))
    reg.update_reflection(did, {"realized_vol": 0.20}, "first")
    # A second resolution must not overwrite the immutable outcome.
    reg.update_reflection(did, {"realized_vol": 0.99}, "tampered")
    row = reg.get_decision(did)
    assert row["realized_outcome"]["realized_vol"] == 0.20
    assert row["reflection"] == "first"


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


def test_workforce_interrupt_requires_explicit_resume_and_preserves_evidence(reg):
    import pytest

    workflow = reg.start_workflow("portfolio_review", {"goal": "review"})
    workflow_id = workflow["workflow_id"]
    reg.update_workflow_phase(workflow_id, "analyst", "working")

    interrupted = reg.interrupt_workflow(
        workflow_id, "operator stopped a stuck analyst")
    by_phase = {step["phase"]: step for step in interrupted["steps"]}
    assert interrupted["status"] == "interrupted"
    assert interrupted["current_phase"] == "analyst"
    assert by_phase["analyst"]["status"] == "interrupted"
    assert "stuck analyst" in by_phase["analyst"]["summary"]
    assert by_phase["analyst"]["completed_at"] is not None
    assert by_phase["challenger"]["status"] == "queued"

    # A surviving child cannot make a late write and silently resurrect the run.
    with pytest.raises(RuntimeError, match="resume it explicitly"):
        reg.update_workflow_phase(workflow_id, "analyst", "working")
    with pytest.raises(RuntimeError, match="already running"):
        reg.resume_workflow(reg.start_workflow(
            "portfolio_review", {"goal": "other"})["workflow_id"])

    resumed = reg.resume_workflow(workflow_id)
    assert resumed["status"] == "running"
    assert resumed["current_phase"] == "analyst"
    restarted = reg.update_workflow_phase(
        workflow_id, "analyst", "working", summary="re-estimating")
    analyst = restarted["steps"][0]
    assert analyst["status"] == "working"
    assert analyst["completed_at"] is None
    assert analyst["summary"] == "re-estimating"

    kinds = [event["kind"] for event in reg.read_events(limit=20)]
    assert "workflow_interrupted" in kinds
    assert "workflow_resumed" in kinds


def test_workforce_abandon_closes_unfinished_phases_without_deleting_done_work(reg):
    import pytest

    workflow = reg.start_workflow("portfolio_review", {"goal": "review"})
    workflow_id = workflow["workflow_id"]
    reg.update_workflow_phase(
        workflow_id,
        "analyst",
        "done",
        summary="estimation persisted",
        artifacts={
            "moment_set_id": "m1",
            "objective_id": "o1",
            "decision_id": "d1",
            "regime": "neutral",
            "regime_summary": "mixed backdrop",
        },
    )
    reg.update_workflow_phase(workflow_id, "challenger", "working")

    abandoned = reg.abandon_workflow(
        workflow_id, "operator closed obsolete research")
    by_phase = {step["phase"]: step for step in abandoned["steps"]}
    assert abandoned["status"] == "abandoned"
    assert abandoned["result"]["control"]["status"] == "abandoned"
    assert by_phase["analyst"]["status"] == "done"
    assert by_phase["analyst"]["summary"] == "estimation persisted"
    assert all(
        by_phase[phase]["status"] == "abandoned"
        for phase in ("challenger", "optimizer", "referee", "reporter")
    )
    assert "obsolete research" in by_phase["challenger"]["summary"]

    with pytest.raises(RuntimeError, match="cannot be resumed"):
        reg.resume_workflow(workflow_id)
    with pytest.raises(RuntimeError, match="resume it explicitly"):
        reg.update_workflow_phase(workflow_id, "challenger", "working")
    assert reg.abandon_workflow(
        workflow_id, "idempotent retry")["status"] == "abandoned"


def test_bulk_interrupt_only_reaps_running_workflows_older_than_cutoff(reg):
    old = reg.start_workflow("portfolio_review", {"goal": "old"})
    fresh = reg.start_workflow("portfolio_review", {"goal": "fresh"})
    reg.con.execute(
        "UPDATE workflows SET updated_at=? WHERE workflow_id=?",
        ["2000-01-01T00:00:00+00:00", old["workflow_id"]],
    )

    changed = reg.interrupt_running_workflows(
        "coordinator lease expired",
        updated_before="2001-01-01T00:00:00+00:00",
    )
    assert [row["workflow_id"] for row in changed] == [old["workflow_id"]]
    assert reg.get_workflow(old["workflow_id"])["status"] == "interrupted"
    assert reg.get_workflow(fresh["workflow_id"])["status"] == "running"


def test_workforce_optimizer_waits_for_the_debate(reg):
    """The bounded debate makes the challenger a true upstream of the
    optimizer: an amendment recorded in the challenger phase must be able to
    replace the optimizer's inputs, so the optimizer cannot start (let alone
    finish) until the debate has settled."""
    import pytest

    workflow = reg.start_workflow("portfolio_review", {"goal": "review"})
    workflow_id = workflow["workflow_id"]

    with pytest.raises(RuntimeError, match="cannot start"):
        reg.update_workflow_phase(workflow_id, "optimizer", "working")

    reg.update_workflow_phase(
        workflow_id, "analyst", "done",
        artifacts={"moment_set_id": "m1", "objective_id": "o1", "decision_id": "d1",
                   "regime": "neutral", "regime_summary": "offline synthetic backdrop"},
    )
    # Analyst done is not enough — the debate has not settled.
    with pytest.raises(RuntimeError, match="cannot start before 'challenger'"):
        reg.update_workflow_phase(workflow_id, "optimizer", "working")

    reg.update_workflow_phase(
        workflow_id, "challenger", "done",
        artifacts={"challenger_view": "c",
                   "amended_decision_id": "d2"},
    )
    reg.update_workflow_phase(workflow_id, "optimizer", "working")
    ready = reg.update_workflow_phase(
        workflow_id, "optimizer", "done",
        artifacts={"targets": {"GLD": 1.0}, "algorithm_id": "hrp"},
    )
    states = {step["phase"]: step["status"] for step in ready["steps"]}
    assert states["challenger"] == "done" and states["optimizer"] == "done"
    # The amendment survives in persisted artifacts for resumes to read.
    by_phase = {s["phase"]: s for s in ready["steps"]}
    assert by_phase["challenger"]["artifacts"]["amended_decision_id"] == "d2"


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


def test_panel_workflow_runs_a_judged_tournament(reg):
    import pytest

    variants = [{"window": 252}, {"window": 504}, {"window": 756}]
    workflow = reg.start_workflow("panel", {"goal": "tournament",
                                            "variants": variants})
    workflow_id = workflow["workflow_id"]
    phases = [step["phase"] for step in workflow["steps"]]
    assert phases == ["analyst-1", "analyst-2", "analyst-3",
                      "optimizer-1", "optimizer-2", "optimizer-3",
                      "judge", "referee", "reporter"]
    assert workflow["steps"][6]["agent"] == "referee"  # judge = comparison hat

    # Branches are independent: optimizer-2 may run once analyst-2 is done,
    # regardless of branch 1; the judge must wait for every branch.
    with pytest.raises(RuntimeError, match="cannot start"):
        reg.update_workflow_phase(workflow_id, "optimizer-2", "working")
    reg.update_workflow_phase(
        workflow_id, "analyst-2", "done",
        artifacts={"moment_set_id": "m2", "objective_id": "o2",
                   "decision_id": "d2", "regime": "neutral",
                   "regime_summary": "offline synthetic backdrop"})
    reg.update_workflow_phase(workflow_id, "optimizer-2", "working")
    with pytest.raises(RuntimeError, match="cannot start"):
        reg.update_workflow_phase(workflow_id, "judge", "working")

    targets = {1: {"GLD": 1.0}, 2: {"GLD": 0.5, "EMB": 0.5},
               3: {"EMB": 1.0}}
    for i in (1, 3):
        reg.update_workflow_phase(
            workflow_id, f"analyst-{i}", "done",
            artifacts={"moment_set_id": f"m{i}", "objective_id": f"o{i}",
                       "decision_id": f"d{i}", "regime": "neutral",
                       "regime_summary": "offline synthetic backdrop"})
    for i in (1, 2, 3):
        reg.update_workflow_phase(
            workflow_id, f"optimizer-{i}", "done",
            artifacts={"targets": targets[i], "algorithm_id": "hrp"})

    # The judge cannot crown targets no branch produced, a non-optimizer
    # phase, or a mismatched winner/targets pair.
    with pytest.raises(ValueError, match="winning branch"):
        reg.update_workflow_phase(
            workflow_id, "judge", "done",
            artifacts={"winner_phase": "optimizer-2",
                       "winning_targets": {"GLD": 0.9, "EMB": 0.1},
                       "evidence": "fabricated"})
    with pytest.raises(ValueError, match="optimizer branch"):
        reg.update_workflow_phase(
            workflow_id, "judge", "done",
            artifacts={"winner_phase": "analyst-1",
                       "winning_targets": targets[2],
                       "evidence": "wrong phase"})
    reg.update_workflow_phase(
        workflow_id, "judge", "done",
        artifacts={"winner_phase": "optimizer-2",
                   "winning_targets": targets[2],
                   "evidence": "branch 2 best walk-forward sortino"})

    # The referee binds to the judge's winner, not any other branch.
    vid_wrong = reg.log_verdict("d1", "PASS", ["?"], targets=targets[1])
    with pytest.raises(ValueError, match="judge's winning targets"):
        reg.update_workflow_phase(
            workflow_id, "referee", "done",
            artifacts={"verdict": "PASS", "verdict_id": vid_wrong,
                       "targets": targets[1]})
    vid = reg.log_verdict("d2", "PASS", ["winner ok"], targets=targets[2])
    reg.update_workflow_phase(
        workflow_id, "referee", "done",
        artifacts={"verdict": "PASS", "verdict_id": vid,
                   "targets": targets[2]})
    done = reg.update_workflow_phase(
        workflow_id, "reporter", "done",
        artifacts={"recommendation": "adopt branch 2"})
    assert done["status"] == "complete"


def test_panel_workflow_validates_variants(reg):
    import pytest

    with pytest.raises(ValueError, match="variants"):
        reg.start_workflow("panel", {"goal": "x"})
    with pytest.raises(ValueError, match="2\\.\\.5"):
        reg.start_workflow("panel", {"goal": "x", "variants": [{"w": 1}]})


def test_referee_binding_judge_takes_precedence_over_literal_optimizer(reg):
    """A mixed workflow (judge + literal optimizer phase) binds to the judge."""
    workflow = reg.start_workflow(
        "portfolio_review", {"goal": "mixed"},
        phases=("analyst", "challenger", "optimizer", "judge", "referee",
                "reporter"))
    workflow_id = workflow["workflow_id"]
    reg.update_workflow_phase(
        workflow_id, "analyst", "done",
        artifacts={"moment_set_id": "m", "objective_id": "o",
                   "decision_id": "d", "regime": "neutral",
                   "regime_summary": "offline synthetic backdrop"})
    reg.update_workflow_phase(
        workflow_id, "challenger", "done",
        artifacts={"challenger_view": "c"})
    a, b = {"GLD": 1.0}, {"EMB": 1.0}
    reg.update_workflow_phase(
        workflow_id, "optimizer", "done",
        artifacts={"targets": b, "algorithm_id": "hrp"})
    reg.update_workflow_phase(
        workflow_id, "judge", "done",
        artifacts={"winner_phase": "optimizer", "winning_targets": b,
                   "evidence": "only branch"})
    # Judge crowned B; a PASS for B must complete even though a literal
    # optimizer phase also exists (the checks are exclusive, not additive).
    vid = reg.log_verdict("d", "PASS", ["ok"], targets=b)
    done = reg.update_workflow_phase(
        workflow_id, "referee", "done",
        artifacts={"verdict": "PASS", "verdict_id": vid, "targets": b})
    by_phase = {s["phase"]: s for s in done["steps"]}
    assert by_phase["referee"]["status"] == "done"


def test_request_deps_key_is_registry_owned(reg):
    """A caller-supplied _deps cannot reorder the standard pipeline."""
    workflow = reg.start_workflow(
        "portfolio_review",
        {"goal": "x", "_deps": {"reporter": []}})
    assert "_deps" not in workflow["request"]
    import pytest
    with pytest.raises(RuntimeError, match="cannot start"):
        reg.update_workflow_phase(workflow["workflow_id"], "reporter", "working")


def test_referee_verdict_must_bind_the_workflows_own_decision(reg):
    """A PASS logged for a DIFFERENT decision (same targets) cannot complete
    the referee phase of this workflow."""
    import pytest

    workflow = reg.start_workflow("portfolio_review", {"goal": "bind"})
    workflow_id = workflow["workflow_id"]
    targets = {"GLD": 1.0}
    reg.update_workflow_phase(
        workflow_id, "analyst", "done",
        artifacts={"moment_set_id": "m", "objective_id": "o",
                   "decision_id": "d-this", "regime": "neutral",
                   "regime_summary": "offline synthetic backdrop"})
    reg.update_workflow_phase(
        workflow_id, "challenger", "done", artifacts={"challenger_view": "c"})
    reg.update_workflow_phase(
        workflow_id, "optimizer", "done",
        artifacts={"targets": targets, "algorithm_id": "hrp"})

    # A PASS bound to the right targets but a DIFFERENT decision must not pass.
    other = reg.log_verdict("d-other", "PASS", ["ok"], targets=targets)
    with pytest.raises(ValueError, match="different decision"):
        reg.update_workflow_phase(
            workflow_id, "referee", "done",
            artifacts={"verdict": "PASS", "verdict_id": other, "targets": targets})

    # The workflow's own decision passes.
    mine = reg.log_verdict("d-this", "PASS", ["ok"], targets=targets)
    done = reg.update_workflow_phase(
        workflow_id, "referee", "done",
        artifacts={"verdict": "PASS", "verdict_id": mine, "targets": targets})
    assert {s["phase"]: s["status"] for s in done["steps"]}["referee"] == "done"


def test_equity_marks_are_idempotent_and_ordered():
    reg = Registry(":memory:")
    assert reg.log_equity_mark(
        "2026-06-02T21:00:00+00:00", 10_050.0, cash=500.0, source="daily")
    assert reg.log_equity_mark(
        "2026-06-01T21:00:00+00:00", 10_000.0, cash=500.0, source="daily")
    # Same (ts, source) is a silent no-op that keeps the first value.
    assert not reg.log_equity_mark(
        "2026-06-01T21:00:00+00:00", 99.0, cash=0.0, source="daily")
    # A different source at the same instant is a distinct observation.
    assert reg.log_equity_mark(
        "2026-06-01T21:00:00+00:00", 10_000.0, cash=None, source="alpaca_backfill")
    marks = reg.equity_marks()
    assert [m["equity"] for m in marks if m["source"] == "daily"] == [10_000.0, 10_050.0]
    assert marks[0]["ts"] == "2026-06-01T21:00:00+00:00"


def test_reset_book_discards_the_equity_marks_of_the_discarded_book():
    """Resetting the book wipes its history: a reset is not a market move."""
    reg = Registry(":memory:")
    reg.init_account(10_000.0)
    reg.apply_fill("ACWI", 10.0, 50.0, -500.0)
    reg.add_order("order-1", "plan-1", "ACWI", "buy", 500.0)
    reg.log_equity_mark("2026-06-01T21:00:00+00:00", 10_500.0, cash=500.0,
                        source="daily", book="simulated_paper")

    reg.reset_book(10_000.0, book="simulated_paper")

    assert reg.get_positions() == {}
    assert reg.list_orders() == []
    assert reg.equity_marks() == []
    assert reg.count_equity_marks() == 0


def test_resetting_one_book_leaves_another_books_history_intact():
    # The delete was unqualified, so discarding the simulated paper book also
    # destroyed the Alpaca account's backfilled equity curve — history no
    # reset here can rebuild, since that account lives at the broker.
    reg = Registry(":memory:")
    reg.init_account(10_000.0)
    reg.log_equity_mark("2026-06-01T21:00:00+00:00", 10_500.0, cash=500.0,
                        source="daily", book="simulated_paper")
    reg.log_equity_mark("2026-06-01T21:00:00+00:00", 98_000.0, cash=1_000.0,
                        source="alpaca_backfill", book="alpaca_paper")

    reg.reset_book(10_000.0, book="simulated_paper")

    surviving = reg.equity_marks(book="alpaca_paper")
    assert [m["equity"] for m in surviving] == [98_000.0]
    assert reg.equity_marks(book="simulated_paper") == []


def test_a_flat_judge_graph_still_binds_the_verdict_to_its_own_decision():
    # A panel winner is "optimizer-<branch>"; a flat graph's is bare
    # "optimizer". rpartition on that returned "optimizer" itself, so the
    # lookup asked for "analyst-optimizer", found nothing, and dropped the
    # expected decision — skipping the check that a PASS reviewed THIS run.
    registry = Registry(":memory:")
    try:
        targets = {"ACWI": 0.6, "BNDW": 0.4}
        workflow = registry.start_workflow(
            "portfolio_review", {"goal": "g"},
            phases=("analyst", "challenger", "optimizer", "judge",
                    "referee", "reporter"))
        wid = workflow["workflow_id"]

        foreign = registry.log_decision(Decision(
            as_of=date.today(), kind="rebalance_gate",
            choice={"targets": targets}, rationale="a different run entirely"))
        verdict_id = registry.log_verdict(
            foreign, "PASS", ["looked fine over there"],
            source="referee-agent", targets=targets)

        registry.update_workflow_phase(
            wid, "analyst", "done", summary="s",
            artifacts={"decision_id": "this-runs-own-decision",
                       "regime": "calm", "regime_summary": "calm",
                       "moment_set_id": "m", "objective_id": "o"})
        registry.update_workflow_phase(
            wid, "challenger", "done", summary="s",
            artifacts={"challenger_view": "the window is too short"})
        registry.update_workflow_phase(
            wid, "optimizer", "done", summary="s",
            artifacts={"targets": targets, "algorithm_id": "hrp"})
        registry.update_workflow_phase(
            wid, "judge", "done", summary="s",
            artifacts={"winner_phase": "optimizer", "winning_targets": targets,
                       "evidence": "e"})

        # The PASS belongs to a foreign decision, so the referee must refuse.
        with pytest.raises(ValueError):
            registry.update_workflow_phase(
                wid, "referee", "done", summary="s",
                artifacts={"verdict": "PASS", "verdict_id": verdict_id,
                           "targets": targets})
    finally:
        registry.close()


def test_a_book_switch_cannot_fabricate_a_drawdown():
    # This is the bug that halted a real desk. The account was ONE row shared by
    # every venue, and both brokers ratcheted its high-water mark. An Alpaca
    # paper account near $32.6k set the mark; the next read of the $10k
    # simulated book computed a 69% drawdown, tripped the kill switch, halted
    # trading and blocked the reporter — with nothing having lost money.
    reg = Registry(":memory:")
    try:
        reg.init_account(10_000.0, book="simulated_paper")
        reg.init_account(32_626.0, book="alpaca_paper")
        reg.update_high_water_mark(32_626.0, book="alpaca_paper")

        sim = reg.get_account("simulated_paper")
        assert sim["high_water_mark"] == 10_000.0, (
            "the other venue's peak leaked into this book")
        drawdown = 1.0 - sim["cash"] / sim["high_water_mark"]
        assert drawdown == 0.0

        # And a halt is per-venue: one book breaching must not stop the other.
        reg.set_halt(True, book="alpaca_paper")
        assert reg.get_account("alpaca_paper")["halted"] is True
        assert reg.get_account("simulated_paper")["halted"] is False
    finally:
        reg.close()


def test_a_legacy_shared_account_row_is_migrated_without_its_false_peak(tmp_path):
    # A database written before the partitioning carries the corrupted mark. It
    # must not survive the migration, or the false drawdown crosses with it.
    import duckdb

    path = tmp_path / "legacy.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE account (id INTEGER PRIMARY KEY, cash DOUBLE, "
        "high_water_mark DOUBLE, halted BOOLEAN, updated_at VARCHAR)")
    # $10k cash against a $32.6k peak, halted — exactly the observed state.
    con.execute("INSERT INTO account VALUES (1, 10000.0, 32626.0, TRUE, 'x')")
    con.close()

    reg = Registry(str(path))
    try:
        acct = reg.get_account("simulated_paper")
        assert acct["cash"] == 10_000.0          # the cash is real, keep it
        assert acct["high_water_mark"] == 10_000.0  # the peak was not this book's
        assert acct["halted"] is False            # so the halt was not either
    finally:
        reg.close()


def test_two_books_can_mark_the_same_timestamp_and_source():
    # The primary key predated the book column, so a second book's backfill
    # collided row-for-row and was silently dropped: the route answered
    # {"backfilled": 0}, indistinguishable from "already up to date", while
    # that book's series stayed empty.
    reg = Registry(":memory:")
    ts, source = "2026-06-01T21:00:00+00:00", "daily"
    assert reg.log_equity_mark(ts, 10_500.0, cash=500.0, source=source,
                               book="simulated_paper") is True
    assert reg.log_equity_mark(ts, 98_000.0, cash=1_000.0, source=source,
                               book="alpaca_paper") is True

    assert [m["equity"] for m in reg.equity_marks(book="simulated_paper")] == [
        10_500.0]
    assert [m["equity"] for m in reg.equity_marks(book="alpaca_paper")] == [
        98_000.0]
    # The same book at the same instant is still one fact, not two.
    assert reg.log_equity_mark(ts, 10_600.0, cash=400.0, source=source,
                               book="simulated_paper") is False


def test_an_existing_database_is_rekeyed_without_losing_marks(tmp_path):
    # An ALTER cannot widen a primary key, so a database created before the
    # book column keeps the old (ts, source) key until it is rebuilt.
    import duckdb

    path = tmp_path / "legacy.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE equity_marks (ts VARCHAR, source VARCHAR, "
        "equity DOUBLE, cash DOUBLE, PRIMARY KEY (ts, source))")
    con.execute("INSERT INTO equity_marks VALUES "
                "('2026-06-01T21:00:00+00:00', 'daily', 10_500.0, 500.0)")
    con.close()

    reg = Registry(str(path))
    try:
        assert [m["equity"] for m in reg.equity_marks()] == [10_500.0]
        # And the widened key now admits the second book.
        assert reg.log_equity_mark(
            "2026-06-01T21:00:00+00:00", 98_000.0, cash=1_000.0,
            source="daily", book="alpaca_paper") is True
    finally:
        reg.close()


def test_reset_book_refuses_an_unnamed_book():
    # The book is required precisely because the default used to be "all".
    reg = Registry(":memory:")
    reg.init_account(10_000.0)
    with pytest.raises(ValueError, match="requires the book"):
        reg.reset_book(10_000.0, book="")


def test_equity_marks_are_partitioned_by_book():
    """One book's marks are readable without another book's equity level."""
    reg = Registry(":memory:")
    reg.log_equity_mark("2026-06-01T21:00:00+00:00", 10_000.0, cash=None,
                        source="daily", book="simulated_paper")
    reg.log_equity_mark("2026-06-02T21:00:00+00:00", 250_000.0, cash=None,
                        source="daily", book="alpaca_paper")

    assert [m["equity"] for m in reg.equity_marks(book="simulated_paper")] == [10_000.0]
    assert [m["equity"] for m in reg.equity_marks(book="alpaca_paper")] == [250_000.0]
    assert reg.count_equity_marks() == 2
    assert reg.count_equity_marks(book="simulated_paper") == 1
    # An unattributed mark belongs to no book and is never claimed by one.
    reg.log_equity_mark("2026-06-03T21:00:00+00:00", 1.0, cash=None,
                        source="daily")
    assert reg.count_equity_marks(book="simulated_paper") == 1
    assert reg.count_equity_marks() == 3


def test_equity_marks_gains_the_book_column_on_an_existing_table(tmp_path):
    """An equity_marks table created before `book` existed must be migrated.

    _SCHEMA never touches an already-created table, so without the explicit
    ALTER a pre-`book` registry breaks every mark write and every read.
    """
    path = tmp_path / "registry.duckdb"
    reg = Registry(str(path))
    reg.con.execute("DROP TABLE equity_marks")
    reg.con.execute(
        "CREATE TABLE equity_marks (ts VARCHAR, source VARCHAR, "
        "equity DOUBLE, cash DOUBLE, PRIMARY KEY (ts, source))")
    reg.close()

    reg = Registry(str(path))
    try:
        assert reg.log_equity_mark("2026-06-01T21:00:00+00:00", 10_000.0,
                                   cash=500.0, source="daily",
                                   book="alpaca_paper")
        assert [m["equity"] for m in reg.equity_marks(book="alpaca_paper")] \
            == [10_000.0]
        assert reg.count_equity_marks(book="alpaca_paper") == 1
    finally:
        reg.close()


# --- a task's origin decides who may start it --------------------------------

def test_an_empty_origin_is_refused_at_the_writer(reg):
    """`origin` decides whether the heartbeat may start a task unattended, so
    the one value that reads as neither NULL nor a written choice must never
    reach the column. Refuse it at the writer instead of resolving it later.
    """
    with pytest.raises(ValueError, match="origin must be a non-empty string"):
        reg.create_atlas_task("t-empty", "k|2026-08-06|SPY|a", "regime_shift",
                              {}, "regime_review", origin="")
    with pytest.raises(ValueError, match="origin must be a non-empty string"):
        reg.create_atlas_task("t-blank", "k|2026-08-06|SPY|b", "regime_shift",
                              {}, "regime_review", origin="   ")
    assert reg.get_atlas_task("t-empty") is None
    assert reg.get_atlas_task("t-blank") is None


def test_atlas_tasks_gains_the_origin_column_on_an_existing_table(tmp_path):
    """An atlas_tasks table created before `origin` existed must be migrated.

    _SCHEMA never touches an already-created table, so without the explicit
    ALTER the user's real dev DB is the only place this fails — every test
    would pass on a fresh registry while the desk broke on the one that matters.
    """
    path = tmp_path / "registry.duckdb"
    reg = Registry(str(path))
    reg.con.execute("DROP TABLE atlas_tasks")
    # The pre-column DDL, verbatim from the parent commit.
    reg.con.execute(
        "CREATE TABLE atlas_tasks (task_id VARCHAR PRIMARY KEY, "
        "dedupe_key VARCHAR UNIQUE, trigger_kind VARCHAR, trigger_payload JSON, "
        "template_id VARCHAR, status VARCHAR, workflow_id VARCHAR, "
        "conclusion JSON, error VARCHAR, attempt_count INTEGER, "
        "created_at VARCHAR, started_at VARCHAR, completed_at VARCHAR, "
        "updated_at VARCHAR)")
    reg.con.execute(
        "INSERT INTO atlas_tasks (task_id, dedupe_key, trigger_kind, "
        "trigger_payload, template_id, status, attempt_count, created_at, "
        "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ["old-1", "regime_shift|2026-08-06|SPY|old", "regime_shift", "{}",
         "regime_review", "queued", 0, "2026-08-06T00:00:00Z",
         "2026-08-06T00:00:00Z"])
    reg.close()

    reg = Registry(str(path))
    try:
        old = reg.get_atlas_task("old-1")
        # The column exists (the read would KeyError otherwise) and the
        # pre-column row is NULL, which the reader owes an answer for.
        assert "origin" in old
        assert old["origin"] is None
        assert reg.create_atlas_task("new-1", "regime_shift|2026-08-06|SPY|new",
                                     "regime_shift", {}, "regime_review")
        assert reg.get_atlas_task("new-1")["origin"] == "trigger"
    finally:
        reg.close()


def test_a_filtered_task_scan_keeps_a_legacy_null_origin_as_trigger_work(reg):
    """`origin='trigger'` in SQL would drop every pre-column row, and those are
    all trigger work — the desk's own autonomy, filtered out by a scan that was
    added to protect it. The status filter is exact by comparison: `status` has
    never been NULL."""
    reg.con.execute(
        "INSERT INTO atlas_tasks (task_id, dedupe_key, trigger_kind, "
        "trigger_payload, template_id, status, attempt_count, created_at, "
        "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ["legacy", "regime_flip|2026-08-06|SPY|old", "regime_flip", "{}",
         "regime_review", "queued", 0, "2026-08-06T00:00:00Z",
         "2026-08-06T00:00:00Z"])
    reg.create_atlas_task("fresh", "regime_flip|2026-08-07|SPY|new",
                          "regime_flip", {}, "regime_review")
    reg.create_atlas_task("offered", "proposal:desk_brief|2026-08-07|SPY|d",
                          "proposal:desk_brief", {}, "desk_brief",
                          origin="proposal")
    reg.update_atlas_task("fresh", status="running")

    trigger_work = {t["task_id"] for t in reg.list_atlas_tasks(50, origin="trigger")}
    assert trigger_work == {"legacy", "fresh"}
    assert {t["task_id"] for t in reg.list_atlas_tasks(50, status="queued")} == {
        "legacy", "offered"}
    assert [t["task_id"] for t in reg.list_atlas_tasks(
        50, status="queued", origin="proposal")] == ["offered"]


def test_a_task_is_found_by_its_dedupe_key_without_scanning(reg):
    """The dedupe key is UNIQUE, so the lookup belongs in SQL. Reading it out of
    a bounded scan meant a table deeper than the window answered 'no such task'
    for a key that exists — and the caller then minted a duplicate id."""
    reg.create_atlas_task("only", "proposal:desk_brief|2026-08-07|SPY|d",
                          "proposal:desk_brief", {}, "desk_brief",
                          origin="proposal")
    for i in range(60):
        reg.create_atlas_task(f"noise-{i}", f"regime_flip|2026-08-07|SPY|{i}",
                              "regime_flip", {}, "regime_review")

    found = reg.get_atlas_task_by_dedupe("proposal:desk_brief|2026-08-07|SPY|d")

    assert found["task_id"] == "only"
    assert reg.get_atlas_task_by_dedupe("nothing|like|this|key") is None


# --- phase graphs must be executable, not merely well-named ------------------

def test_validate_phase_graph_rejects_an_unknown_phase_type():
    from qlab.state.registry import validate_phase_graph

    with pytest.raises(ValueError, match="unknown workforce phases"):
        validate_phase_graph(("news-analyst-typo",))


def test_validate_phase_graph_rejects_a_graph_whose_dependency_is_absent():
    # This is the real defect: the set is individually well-named, every phase
    # exists, and creation succeeds -- but referee depends on optimizer, so
    # omitting optimizer orphans the referee and the run can never terminate.
    from qlab.state.registry import validate_phase_graph

    with pytest.raises(ValueError, match="referee.*optimizer"):
        validate_phase_graph(("analyst", "challenger", "referee", "reporter"))


def test_validate_phase_graph_accepts_the_standard_workforce_graph():
    from qlab.state.registry import WORKFORCE_PHASES, validate_phase_graph

    validate_phase_graph(WORKFORCE_PHASES)


def test_start_workflow_refuses_a_graph_that_could_never_complete():
    # Creation used to succeed and the deadlock only surfaced later, when the
    # referee refused to start and the workflow sat running forever.
    registry = Registry(":memory:")
    try:
        with pytest.raises(ValueError, match="referee"):
            registry.start_workflow(
                "portfolio_review", {"goal": "g"},
                phases=("analyst", "challenger", "referee", "reporter"))
    finally:
        registry.close()


def test_a_panel_graph_is_validated_against_its_own_dependencies():
    # Panels carry an instance DAG where branch optimizers depend on their own
    # analysts; validating them against the static map would wrongly reject them.
    registry = Registry(":memory:")
    try:
        workflow = registry.start_workflow(
            "panel", {"goal": "g", "variants": [{"window": 252}, {"window": 504}]})
        assert workflow["workflow_id"]
    finally:
        registry.close()


def test_a_news_analyst_phase_runs_as_a_single_phase_workflow():
    # Atlas's qualitative helper reads a window it is handed. It produces a view
    # and nothing else: no targets, so no referee and no approval path.
    registry = Registry(":memory:")
    try:
        workflow = registry.start_workflow(
            "portfolio_review", {"goal": "read the window"},
            phases=("news-analyst",))
        registry.update_workflow_phase(
            workflow["workflow_id"], "news-analyst", "done",
            summary="record supports a narrow reading",
            artifacts={"news_view": "two primary filings, one single-source take"})

        stored = registry.get_workflow(workflow["workflow_id"])
        assert stored["status"] == "complete"
    finally:
        registry.close()


def test_a_news_analyst_phase_cannot_complete_without_its_view():
    registry = Registry(":memory:")
    try:
        workflow = registry.start_workflow(
            "portfolio_review", {"goal": "g"}, phases=("news-analyst",))
        with pytest.raises(ValueError, match="news_view"):
            registry.update_workflow_phase(
                workflow["workflow_id"], "news-analyst", "done",
                summary="empty", artifacts={})
    finally:
        registry.close()


def test_runs_of_kind_is_not_limited_by_how_much_else_ran(reg):
    """A desk logs solves and backtests continuously. Scanning the newest N
    runs of ANY kind to find the last matrix stops finding it as soon as N
    other runs have landed since — and the caller then re-logs a window it
    already has."""
    older = reg.log_run("qualitative_matrix", {"matrix": {"window_hash": "old"}})
    newer = reg.log_run("qualitative_matrix", {"matrix": {"window_hash": "new"}})
    for i in range(150):
        reg.log_run("backtest", {"i": i})

    row = reg.runs_of_kind("qualitative_matrix", 1)[0]
    assert row["run_id"] == newer and row["run_id"] != older
    # `spec` comes back parsed, like list_runs: a caller reading it must not
    # have to know whether this path went through the JSON helper.
    assert row["spec"]["matrix"]["window_hash"] == "new"
    assert reg.runs_of_kind("never_logged_anything", 1) == []


def test_runs_of_kind_returns_the_newest_first_and_only_that_kind(reg):
    """Views built from a matrix need the PREVIOUS window too, not just the newest."""
    first = reg.log_run("qualitative_matrix", {"matrix": {"window_hash": "w1"}})
    second = reg.log_run("qualitative_matrix", {"matrix": {"window_hash": "w2"}})
    reg.log_run("backtest", {"arm": "A1"})

    rows = reg.runs_of_kind("qualitative_matrix", 2)
    assert [r["run_id"] for r in rows] == [second, first]
    assert rows[0]["spec"]["matrix"]["window_hash"] == "w2"
    assert reg.runs_of_kind("never_logged", 5) == []


def test_get_run_reads_one_run_by_id_with_its_spec_parsed(reg):
    run_id = reg.log_run("views", {"kl_total": 0.1, "kl_budget": 0.25})
    row = reg.get_run(run_id)
    assert row["kind"] == "views" and row["spec"]["kl_total"] == 0.1
    assert reg.get_run("not-a-run") is None


def test_a_moment_sets_lineage_survives_the_round_trip(reg):
    """The referee reads provenance back out; a write-only column proves nothing."""
    import numpy as np

    from qlab.core.types import MomentSet

    ms = MomentSet(tickers=["A", "B"], as_of=date(2021, 6, 30),
                   cov=np.eye(2), provenance={"parent": "p", "views_run_id": "v"})
    h = reg.log_moment_set(ms)
    row = reg.moment_set(h)
    assert row["provenance"] == {"parent": "p", "views_run_id": "v"}
    assert reg.moment_set("nope") is None

    plain = MomentSet(tickers=["A", "B"], as_of=date(2021, 6, 30), cov=np.eye(2) * 2)
    assert reg.moment_set(reg.log_moment_set(plain))["provenance"] == {}

    # Every row an existing desk already holds predates the ALTER, so the
    # column is NULL there. The reader must answer "no lineage", not crash the
    # referee on the first pre-migration moment set it meets.
    reg.con.execute("UPDATE moment_sets SET provenance = NULL")
    assert reg.moment_set(h)["provenance"] == {}


def test_matrix_runs_filters_in_sql_so_no_caller_scans_a_fixed_window(reg):
    """The predicates that pick a window are SQL, not a Python pass over N rows."""
    def log(source, as_of, ticker):
        spec = {"matrix": {"as_of": as_of, "window_hash": as_of,
                           "rows": {ticker: {"ticker": ticker}}}}
        if source:
            spec["source"] = source
        reg.log_run("qualitative_matrix", spec)

    log("ablation_a5", "2015-06-30", "ACWI")
    log("ablation_a5", "2016-06-30", "ACWI")
    log(None, "2015-09-30", "SPY")
    for i in range(400):          # foreign traffic, newer than the target
        log(None, f"2017-01-{i % 28 + 1:02d}", "SPY")
    # A run of the same kind that carries no matrix at all is not a window.
    reg.log_run("qualitative_matrix", {"note": "no matrix here"})

    arm = reg.matrix_runs(source="ablation_a5", as_of_before="2015-09-30",
                          limit=10)
    assert [r["spec"]["matrix"]["as_of"] for r in arm] == ["2015-06-30"]

    # Unfiltered by source, bounded at or before the date, newest first.
    any_source = reg.matrix_runs(source=None, as_of_at_or_before="2015-09-30",
                                 limit=10)
    assert [r["spec"]["matrix"]["as_of"] for r in any_source] == [
        "2015-09-30", "2015-06-30"]
    assert reg.matrix_runs(source=None, as_of_at_or_before="2015-09-30",
                           limit=1)[0]["spec"]["matrix"]["as_of"] == "2015-09-30"
    assert reg.matrix_runs(source="ablation_a5",
                           as_of_before="2015-01-01", limit=10) == []


def test_a_stale_workflow_can_still_be_resumed():
    """`stale` is a diagnosis, not a grave.

    Marking a week-idle run stale must not close the operator's only route
    back to it: the phases are all still there, and resuming is exactly what
    an operator who sees the mark would want to do.
    """
    from datetime import datetime, timedelta, timezone

    from qlab.state.registry import WORKFLOW_RESUMABLE_STATUSES, Registry

    assert "stale" in WORKFLOW_RESUMABLE_STATUSES

    reg = Registry(":memory:")
    try:
        workflow_id = reg.start_workflow(
            "portfolio_review", {"goal": "[regime_review] stalled"})["workflow_id"]
        long_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        reg.con.execute("UPDATE workflows SET updated_at=? WHERE workflow_id=?",
                        [long_ago, workflow_id])
        assert reg.mark_idle_workflows_stale(
            "no phase progress in 7 days", updated_before=(
                datetime.now(timezone.utc) - timedelta(days=7)).isoformat())
        assert reg.get_workflow(workflow_id)["status"] == "stale"

        resumed = reg.resume_workflow(workflow_id)
        assert resumed["status"] == "running"
        assert resumed["current_phase"] == "analyst"
    finally:
        reg.close()


def test_an_answered_universe_change_is_found_under_a_deep_pile(reg):
    """Selection in SQL, not a window over the newest rows.

    `_check_not_already_answered` used to scan the 500 newest terminal
    approvals in Python, so a desk with a busy approval queue could lose an old
    answer out of view and re-ask a question the operator had already refused.
    600 newer terminal rows here is the shape that failed.
    """
    from qlab.governance.approval import build_universe_change_request
    from qlab.state.registry import APPROVAL_KIND_UNIVERSE_CHANGE

    answered = build_universe_change_request("ACWI", memo_decision_id="dec-1")
    reg.create_approval_request(answered)
    reg.transition_approval(answered["approval_id"], "rejected")
    for i in range(600):
        other = build_universe_change_request(
            "BNDW", memo_decision_id=f"noise-{i}")
        reg.create_approval_request(other)
        reg.transition_approval(other["approval_id"], "rejected")

    found = reg.answered_universe_change("ACWI", "dec-1")
    assert found is not None
    assert found["approval_id"] == answered["approval_id"]
    assert found["status"] == "rejected"
    assert found["kind"] == APPROVAL_KIND_UNIVERSE_CHANGE
    # Bound to the pair: a later memo about the same name is a new question.
    assert reg.answered_universe_change("ACWI", "dec-2") is None
    # And a still-pending question is not an answer.
    pending = build_universe_change_request("GLD", memo_decision_id="dec-3")
    reg.create_approval_request(pending)
    assert reg.answered_universe_change("GLD", "dec-3") is None
