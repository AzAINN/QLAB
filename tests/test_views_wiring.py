"""Quarantined news-view wiring: strict schema, owner run, and proxy seam."""

from __future__ import annotations

import pytest


class StubApp:
    def __init__(self):
        self.names: list[str] = []
        self.tools: dict[str, object] = {}

    def tool(self, name: str):
        def register(fn):
            self.names.append(name)
            self.tools[name] = fn
            return fn

        return register


def _tail_view(**overrides) -> dict:
    view = {
        "type": "tail",
        "ticker": "ACWI",
        "direction": "fatter",
        "confidence": 0.25,
        "source_quote": "Options markets imply unusually wide outcomes.",
    }
    view.update(overrides)
    return view


def test_apply_views_round_trips_offline_and_records_only_a_research_run(reg):
    from qlab.ui.server import UISession, handle_api

    session = UISession(offline_default=True, seed=7, registry=reg)
    solution_trials = reg.trial_count()
    backtest_trials = reg.backtest_trial_count()

    status, payload = handle_api(
        session,
        "POST",
        "/api/lab/research.apply_views",
        {},
        {
            "offline": True,
            "as_of": "2022-06-30",
            "universe": "core",
            "views": [_tail_view()],
            "kl_budget": 0.25,
            "dry": True,
        },
    )

    assert status == 200
    result = payload["result"]
    assert set(result) == {
        "run_id",
        "kl_total",
        "kl_per_view",
        "moments_before",
        "moments_after",
        "applied_labels",
    }
    assert result["applied_labels"] == ["tail(ACWI fatter)"]
    assert 0.0 <= result["kl_total"] <= 0.25
    assert set(result["moments_before"]) == set(result["moments_after"])
    for ticker in result["moments_before"]:
        assert result["moments_after"][ticker]["mean"] == pytest.approx(
            result["moments_before"][ticker]["mean"],
            abs=1e-8,
        )

    report = reg.report(result["run_id"])
    assert len(report["run"]) == 1
    run = report["run"][0]
    assert run["kind"] == "views"
    assert run["spec"]["algorithm_id"] == "entropy_pooling_views"
    assert run["spec"]["source"] == "synthetic"
    assert run["spec"]["dry"] is True
    assert run["spec"]["downstream_conditioning"] is False
    assert run["spec"]["dsr_trial_counted"] is False
    assert run["spec"]["views"][0]["source_quote"] == (
        "Options markets imply unusually wide outcomes."
    )
    assert report["solutions"] == []
    assert report["backtests"] == []
    assert reg.trial_count() == solution_trials
    assert reg.backtest_trial_count() == backtest_trials


@pytest.mark.parametrize(
    ("views", "message"),
    [
        ([
            {
                "type": "vol",
                "ticker": "ACWI",
                "target_vol": 0.02,
                "target_return": 0.10,
                "confidence": 0.4,
                "source_quote": "Volatility may rise.",
            }
        ], "forbidden return/price"),
        ([_tail_view(confidence=0.71)], "confidence"),
        ([
            {
                "type": "tail",
                "ticker": "ACWI",
                "direction": "fatter",
                "confidence": 0.4,
            }
        ], "source_quote"),
        ([_tail_view() for _ in range(4)], "at most 3"),
    ],
)
def test_apply_views_refuses_malformed_or_return_flavored_payloads_before_data(
    reg,
    monkeypatch,
    views,
    message,
):
    import qlab.mcp.quant_lab as quant_lab
    from qlab.ui.server import UISession

    session = UISession(offline_default=True, seed=7, registry=reg)

    def unexpected_snapshot(*_args, **_kwargs):
        pytest.fail("schema validation must run before loading market data")

    monkeypatch.setattr(quant_lab.market, "snapshot", unexpected_snapshot)
    with pytest.raises((TypeError, ValueError), match=message):
        session.call_lab_tool(
            "research.apply_views",
            {
                "as_of": "2022-06-30",
                "universe": "core",
                "views": views,
                "dry": True,
            },
            True,
        )
    assert reg.list_runs() == []
    assert reg.trial_count() == 0
    assert reg.backtest_trial_count() == 0


def test_apply_views_refuses_non_dry_mode_before_data(reg, monkeypatch):
    import qlab.mcp.quant_lab as quant_lab
    from qlab.ui.server import UISession

    session = UISession(offline_default=True, seed=7, registry=reg)

    def unexpected_snapshot(*_args, **_kwargs):
        pytest.fail("dry-mode validation must run before loading market data")

    monkeypatch.setattr(quant_lab.market, "snapshot", unexpected_snapshot)
    with pytest.raises(PermissionError, match="dry=true only"):
        session.call_lab_tool(
            "research.apply_views",
            {
                "as_of": "2022-06-30",
                "universe": "core",
                "views": [_tail_view()],
                "dry": False,
            },
            True,
        )
    assert reg.list_runs() == []


def test_tui_proxy_forwards_apply_views_to_the_owner():
    from qlab.mcp.tui_proxy import register_proxy_tools

    calls: list[tuple[str, dict]] = []

    class RecordingClient:
        offline = True

        def post(self, path: str, body: dict) -> dict:
            calls.append((path, body))
            return {"result": {"run_id": "views-run"}}

    app = StubApp()
    register_proxy_tools(app, RecordingClient())
    result = app.tools["research_apply_views"](
        as_of="2022-06-30",
        universe="core",
        views=[_tail_view()],
    )

    assert result == {"run_id": "views-run"}
    assert calls == [(
        "/api/lab/research.apply_views",
        {
            "as_of": "2022-06-30",
            "universe": "core",
            "views": [_tail_view()],
            "kl_budget": 0.25,
            "dry": True,
            "offline": True,
        },
    )]
