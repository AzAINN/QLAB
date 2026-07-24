"""Estimation-window evidence and its DSR-honest registry tool."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from qlab.core import data as market
from qlab.core.types import DataSnapshot
from qlab.core.window_evidence import window_evidence


class ToolApp:
    def __init__(self):
        self.tools = {}

    def tool(self, name: str):
        def decorate(fn):
            self.tools[name] = fn
            return fn

        return decorate


def _synthetic_snapshot() -> DataSnapshot:
    tickers = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    prices = market.synthetic_prices(
        tickers,
        start="2018-01-01",
        end="2022-12-30",
        seed=19,
    )
    return DataSnapshot(
        tickers=tickers,
        prices=prices,
        as_of=date(2022, 12, 30),
        source="synthetic",
    )


def test_sweep_returns_every_combo_with_stable_ranking() -> None:
    snapshot = _synthetic_snapshot()
    windows = (126, 252, 378)
    shrinkages = ("ledoit_wolf", "nonlinear")

    first = window_evidence(
        snapshot,
        windows=windows,
        shrinkages=shrinkages,
        policy_solver="hrp",
    )
    reordered = window_evidence(
        snapshot,
        windows=reversed(windows),
        shrinkages=reversed(shrinkages),
        policy_solver="hrp",
    )

    assert first == reordered
    assert {
        (row["window"], row["shrinkage"])
        for row in first
    } == {
        (window, shrinkage)
        for window in windows
        for shrinkage in shrinkages
    }
    assert [row["rank"] for row in first] == list(range(1, 7))
    for row in first:
        assert {
            "ann_vol",
            "sortino",
            "max_drawdown",
            "turnover",
            "n_rebalances",
            "span",
        } <= set(row)
        assert row["n_rebalances"] > 0
        assert row["span"]["start"] <= row["span"]["end"]
        assert row["span"]["n_obs"] > 0


def test_tool_persists_one_evidence_run_without_adding_dsr_trials(reg) -> None:
    from qlab.mcp.guardrails import LabState
    from qlab.mcp.quant_lab import register_lab_tools

    baseline_run = reg.log_run("ablation", {"name": "baseline"})
    reg.log_backtest(
        baseline_run,
        "A1",
        {"ann_vol": 0.1, "sortino": 1.0},
        objective="min_variance",
    )
    count_before = reg.backtest_trial_count()
    arms_before = reg.backtest_arm_ids()

    app = ToolApp()
    state = LabState(registry=reg, offline=True, seed=23)
    register_lab_tools(app, state)
    result = app.tools["research.window_evidence"](
        as_of="2012-12-31",
        universe="core",
        cadence="quarterly",
    )

    assert set(result) == {"run_id", "table", "best", "caveats"}
    assert result["best"] == result["table"][0]
    assert len(result["table"]) == 6
    assert result["caveats"]["dsr_trial_counted"] is False
    assert reg.backtest_trial_count() == count_before
    assert reg.backtest_arm_ids() == arms_before
    assert state.budget.by_tool["research.window_evidence"] == 1

    evidence_runs = [
        row for row in reg.list_runs()
        if row["kind"] == "window_evidence"
    ]
    assert len(evidence_runs) == 1
    assert evidence_runs[0]["run_id"] == result["run_id"]
    assert evidence_runs[0]["spec"]["table"] == result["table"]
    report = reg.report(result["run_id"])
    assert report["run"][0]["spec"]["best"] == result["best"]
    assert report["backtests"] == []


def test_moments_analyst_requires_cited_window_evidence() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "agents"
        / "moments-analyst.md"
    ).read_text(encoding="utf-8")

    assert "research.window_evidence" in source
    assert "Cite the evidence table" in source
