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
        "provenance_verified",
        "hard_regime",
        "corroboration",
    }
    assert result["applied_labels"] == ["tail(ACWI fatter)"]
    assert result["provenance_verified"] is False  # no excerpt supplied
    assert len(result["corroboration"]) == 1  # one corroboration entry per view
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
            "excerpt": "",
            "offline": True,
        },
    )]


def test_apply_views_provenance_gate_when_excerpt_supplied(reg):
    from qlab.ui.server import UISession

    session = UISession(offline_default=True, registry=reg)
    excerpt = ("Dealers report options markets imply unusually wide outcomes "
               "for global equities into year end.")
    grounded = {
        "type": "vol", "ticker": "ACWI", "target_vol": 0.02, "confidence": 0.5,
        "source_quote": "options markets imply unusually wide outcomes",
    }
    ok = session.call_lab_tool(
        "research.apply_views",
        {"as_of": "2021-06-30", "universe": "core", "views": [grounded],
         "excerpt": excerpt}, offline=True)
    assert ok["provenance_verified"] is True

    fabricated = dict(grounded, source_quote="ACWI will rally hard next week")
    with pytest.raises(ValueError, match="not found in the supplied excerpt"):
        session.call_lab_tool(
            "research.apply_views",
            {"as_of": "2021-06-30", "universe": "core", "views": [fabricated],
             "excerpt": excerpt}, offline=True)


def test_apply_views_marks_unverified_without_excerpt(reg):
    from qlab.ui.server import UISession

    session = UISession(offline_default=True, registry=reg)
    view = {"type": "vol", "ticker": "ACWI", "target_vol": 0.02,
            "confidence": 0.5, "source_quote": "options imply wide outcomes"}
    out = session.call_lab_tool(
        "research.apply_views",
        {"as_of": "2021-06-30", "universe": "core", "views": [view]},
        offline=True)
    assert out["provenance_verified"] is False


def test_apply_views_haircuts_confidence_when_view_contradicts_regime(reg):
    """A calm-flavored view in a stress regime (or vice versa) loses confidence."""
    from qlab.ui.server import UISession

    session = UISession(offline_default=True, registry=reg)
    # A fatter-tail (stress-flavored) view: corroboration depends on the
    # snapshot's detected regime, which the summary reports back.
    out = session.call_lab_tool(
        "research.apply_views",
        {"as_of": "2021-06-30", "universe": "core",
         "views": [{"type": "tail", "ticker": "ACWI", "direction": "fatter",
                    "confidence": 0.6, "source_quote": "x"}]},
        offline=True)
    assert "hard_regime" in out and out["hard_regime"] in {"calm", "stress"}
    entry = out["corroboration"][0]
    assert entry["flavor"] == "stress"
    if entry["corroborated"]:
        assert entry["confidence_after"] == pytest.approx(0.6)
    else:
        assert entry["confidence_after"] == pytest.approx(0.3)  # halved
    # A thinner-tail view is the opposite flavor, so exactly one of the two
    # corroborates against a given regime.
    out2 = session.call_lab_tool(
        "research.apply_views",
        {"as_of": "2021-06-30", "universe": "core",
         "views": [{"type": "tail", "ticker": "ACWI", "direction": "thinner",
                    "confidence": 0.6, "source_quote": "x"}]},
        offline=True)
    assert (out["corroboration"][0]["corroborated"]
            != out2["corroboration"][0]["corroborated"])
