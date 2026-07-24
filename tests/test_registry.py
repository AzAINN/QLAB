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
                    "decision_id": "d1"},
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
        artifacts={"moment_set_id": "m1", "objective_id": "o1", "decision_id": "d1"},
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
        artifacts={"moment_set_id": "m1", "objective_id": "o1", "decision_id": "d1"},
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
        artifacts={"moment_set_id": "m1", "objective_id": "o1", "decision_id": "d1"},
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
                   "decision_id": "d2"})
    reg.update_workflow_phase(workflow_id, "optimizer-2", "working")
    with pytest.raises(RuntimeError, match="cannot start"):
        reg.update_workflow_phase(workflow_id, "judge", "working")

    targets = {1: {"GLD": 1.0}, 2: {"GLD": 0.5, "EMB": 0.5},
               3: {"EMB": 1.0}}
    for i in (1, 3):
        reg.update_workflow_phase(
            workflow_id, f"analyst-{i}", "done",
            artifacts={"moment_set_id": f"m{i}", "objective_id": f"o{i}",
                       "decision_id": f"d{i}"})
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
                   "decision_id": "d"})
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
                   "decision_id": "d-this"})
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
