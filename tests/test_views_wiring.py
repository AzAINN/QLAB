"""Quarantined news-view wiring: strict schema, owner run, and proxy seam."""

from __future__ import annotations

import json

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


def test_apply_views_persists_applied_target_only_when_requested(reg):
    from qlab.ui.server import UISession

    session = UISession(offline_default=True, seed=7, registry=reg)
    solution_trials = reg.trial_count()
    backtest_trials = reg.backtest_trial_count()
    dsr_arms = reg.backtest_arm_ids()

    result = session.call_lab_tool(
        "research.apply_views",
        {
            "as_of": "2022-06-30",
            "universe": "core",
            "views": [_tail_view()],
            "kl_budget": 0.25,
            "dry": True,
            "persist": True,
        },
        offline=True,
    )

    assert len(result["persisted_view_ids"]) == 1
    row = reg.con.execute(
        """
        SELECT view_id, as_of, universe, view_kind, horizon_days,
               view_payload, pre_view_baseline, run_id, score
        FROM applied_views
        """
    ).fetchone()
    assert row[:5] == (
        result["persisted_view_ids"][0],
        "2022-06-30",
        "core",
        "tail",
        21,
    )
    payload = json.loads(row[5])
    baseline = json.loads(row[6])
    assert payload["direction"] == "fatter"
    assert payload["ticker"] == "ACWI"
    assert payload["horizon_days"] == 21
    assert 0.0 <= baseline["pre_view_tail_mass"] <= 1.0
    assert row[7] == result["run_id"]
    assert row[8] is None

    report = reg.report(result["run_id"])
    assert report["run"][0]["spec"]["persist_applied_views"] is True
    assert report["solutions"] == []
    assert report["backtests"] == []
    assert reg.trial_count() == solution_trials
    assert reg.backtest_trial_count() == backtest_trials
    assert reg.backtest_arm_ids() == dsr_arms


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
        ([{
            "type": "tail", "ticker": "ACWI", "direction": "fatter",
            "confidence": 0.3, "source_claims": [],
        }], "source_claims' must be a non-empty list"),
        ([{
            "type": "tail", "ticker": "ACWI", "direction": "fatter",
            "confidence": 0.3, "source_claims": "k1",
        }], "source_claims' must be a non-empty list"),
        ([{
            "type": "tail", "ticker": "ACWI", "direction": "fatter",
            "confidence": 0.3, "source_claims": [1],
        }], "source_claims' must be a non-empty string"),
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


def test_apply_views_non_dry_runs_and_persists_only_a_research_arm(
    reg,
    monkeypatch,
):
    import pandas as pd

    import qlab.mcp.quant_lab as quant_lab
    from qlab.core.backtest import BacktestResult
    from qlab.ui.server import UISession

    session = UISession(offline_default=True, seed=7, registry=reg)
    calls = []

    def fake_news_arm(prices, views, **kwargs):
        calls.append((prices, views, kwargs))
        means = {ticker: 0.0 for ticker in prices.columns}
        return BacktestResult(
            arm_id="news_conditioned_min_variance",
            returns=pd.Series(
                [0.0, 0.001],
                index=pd.to_datetime(["2022-06-29", "2022-06-30"]),
            ),
            metrics={"ann_vol": 0.01},
            diagnostics={
                "means_unconditioned": means,
                "means_conditioned": means,
                "mean_pinning_max_abs": 0.0,
            },
        )

    monkeypatch.setattr(quant_lab, "news_conditioned_arm", fake_news_arm)
    result = session.call_lab_tool(
        "research.apply_views",
        {
            "as_of": "2022-06-30",
            "universe": "core",
            "views": [_tail_view()],
            "dry": False,
        },
        offline=True,
    )

    assert len(calls) == 1
    assert calls[0][2]["n_trials"] == 2
    arm = result["research_arm"]
    assert arm["arm_id"] == "news_conditioned_min_variance"
    assert arm["stage"] == "research"
    assert arm["operational"] is False
    assert arm["dsr_trial_counted"] is False
    assert arm["means_conditioned"] == arm["means_unconditioned"]

    objective = reg.con.execute(
        "SELECT objective FROM backtests WHERE run_id=?",
        [result["run_id"]],
    ).fetchone()[0]
    assert objective == "min_variance:research"
    assert reg.backtest_trial_count() == 1
    assert reg.backtest_arm_ids() == set()
    assert reg.trial_count() == 0


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


def test_news_fetch_owner_tool_and_extractor_injection(reg):
    from qlab.ui.server import UISession

    session = UISession(offline_default=True, registry=reg)
    out = session.call_lab_tool(
        "news.fetch",
        {"as_of": "2021-06-30", "universe": "core", "lookback_hours": 72},
        offline=True)
    assert out["n_items"] >= 1
    assert isinstance(out["excerpt"], str) and out["excerpt"]
    assert all("provider" in it for it in out["items"])
    # A quote drawn from the fetched excerpt passes the provenance gate,
    # closing the loop feed -> extractor -> apply_views.
    quote = out["items"][0]["headline"]
    view = {"type": "tail", "ticker": "ACWI", "direction": "fatter",
            "confidence": 0.5, "source_quote": quote}
    applied = session.call_lab_tool(
        "research.apply_views",
        {"as_of": "2021-06-30", "universe": "core", "views": [view],
         "excerpt": out["excerpt"]}, offline=True)
    assert applied["provenance_verified"] is True


def test_news_fetch_reaches_extractor_only_via_owner_not_the_extractor_role():
    from qlab.tui.claude import build_workforce_agents, _claude_tool

    agents = build_workforce_agents("react to the latest news")
    # The extractor still holds exactly one tool — it never fetches.
    assert agents["news-extractor"]["tools"] == [_claude_tool("research.apply_views")]
    # The coordinator is the one granted the feed, to fetch and inject.
    assert _claude_tool("news.fetch") in agents["qlab-coordinator"]["tools"]


def test_news_fetch_returns_a_partial_window_rather_than_failing_the_agent(
        reg, monkeypatch):
    # A provider short one feed is a smaller window, not a tool error. Raising
    # here told the agent the news lane was down while real records sat in the
    # exception.
    from qlab.news import feed
    from qlab.news.feed import NewsItem
    from qlab.ui.server import UISession

    def partial(as_of, universe):
        raise feed.PartialWindow(
            [NewsItem(source="Bureau of Economic Analysis",
                      published="2021-06-29T09:00:00+00:00",
                      headline="GDP, second estimate", summary="",
                      url="https://apps.bea.gov/x", tickers=("ACWI",),
                      provider="macro")],
            {"BLS": "HTTP Error 403: Forbidden"})

    monkeypatch.setitem(feed.PROVIDERS, "macro", partial)
    monkeypatch.setenv("QLAB_NEWS_PROVIDER", "macro")
    session = UISession(offline_default=False, registry=reg)
    out = session.call_lab_tool(
        "news.fetch",
        {"as_of": "2021-06-30", "universe": "core", "lookback_hours": 72},
        offline=False)
    assert out["n_items"] == 1
    assert out["items"][0]["headline"] == "GDP, second estimate"
    assert out["excerpt"]
    assert out["partial"] == {"BLS": "HTTP Error 403: Forbidden"}


def test_a_view_may_cite_archive_claims_instead_of_an_excerpt(reg):
    """Provenance can trace to logged claim keys, not only a pasted quote."""
    from qlab.ui.server import UISession

    session = UISession(offline_default=True, registry=reg)
    reg.log_run("qualitative_matrix", {"matrix": {
        "as_of": "2021-06-30", "window_hash": "w",
        "rows": {"ACWI": {"ticker": "ACWI", "coverage": 1, "publishers": 1,
                          "corroborated": 1, "primary_docs": 1,
                          "days_to_next_release": None,
                          "claim_keys": ["k1"]}}}})

    cited = {"type": "tail", "ticker": "ACWI", "direction": "fatter",
             "confidence": 0.3, "source_claims": ["k1"]}
    ok = session.call_lab_tool(
        "research.apply_views",
        {"as_of": "2021-06-30", "universe": "core", "views": [cited]},
        offline=True)
    assert ok["provenance_verified"] is True

    invented = dict(cited, source_claims=["nope"])
    with pytest.raises(ValueError, match="not in the archive"):
        session.call_lab_tool(
            "research.apply_views",
            {"as_of": "2021-06-30", "universe": "core", "views": [invented]},
            offline=True)


def test_a_view_carrying_neither_quote_nor_claims_is_refused(reg):
    from qlab.ui.server import UISession

    session = UISession(offline_default=True, registry=reg)
    with pytest.raises(ValueError,
                       match="must carry source_quote or source_claims"):
        session.call_lab_tool(
            "research.apply_views",
            {"as_of": "2021-06-30", "universe": "core",
             "views": [{"type": "tail", "ticker": "ACWI",
                        "direction": "fatter", "confidence": 0.3}]},
            offline=True)


def test_cited_claims_are_unverified_when_no_matrix_has_been_logged(reg):
    """No archive to check against is 'unverified', as a missing excerpt is."""
    from qlab.ui.server import UISession

    session = UISession(offline_default=True, registry=reg)
    out = session.call_lab_tool(
        "research.apply_views",
        {"as_of": "2021-06-30", "universe": "core",
         "views": [{"type": "tail", "ticker": "ACWI", "direction": "fatter",
                    "confidence": 0.3, "source_claims": ["k1"]}]},
        offline=True)
    assert out["provenance_verified"] is False


def test_a_quote_riding_alongside_claims_is_still_checked_against_the_excerpt(reg):
    """Citing the archive must not launder a fabricated quote into the spec."""
    from qlab.ui.server import UISession

    session = UISession(offline_default=True, registry=reg)
    reg.log_run("qualitative_matrix", {"matrix": {
        "as_of": "2021-06-30", "window_hash": "w",
        "rows": {"ACWI": {"claim_keys": ["k1"]}}}})
    excerpt = "Dealers report options markets imply unusually wide outcomes."
    both = {"type": "tail", "ticker": "ACWI", "direction": "fatter",
            "confidence": 0.3, "source_claims": ["k1"],
            "source_quote": "options markets imply unusually wide outcomes"}
    ok = session.call_lab_tool(
        "research.apply_views",
        {"as_of": "2021-06-30", "universe": "core", "views": [both],
         "excerpt": excerpt}, offline=True)
    assert ok["provenance_verified"] is True

    fabricated = dict(both, source_quote="ACWI will rally hard next week")
    with pytest.raises(ValueError, match="not found in the supplied excerpt"):
        session.call_lab_tool(
            "research.apply_views",
            {"as_of": "2021-06-30", "universe": "core", "views": [fabricated],
             "excerpt": excerpt}, offline=True)
