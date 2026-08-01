"""The combined single-process MCP server (R1).

One FastMCP app mounts both the lab and trader tool namespaces over one shared
Registry, so a single process is the only DuckDB writer. A startup guard keeps
the headless orchestrator server from racing a live owner UI runtime for the
paper book: if the owner API answers on the UI port, the combined server
refuses to open DuckDB and points operators at the ``qlab-operator`` proxy.

Registries here are always ``:memory:`` — a live owner process may hold the real
``.lab/registry.duckdb`` right now, and the guard is exercised by monkeypatching
urllib, never by binding a real socket.
"""

from __future__ import annotations

import urllib.error

import pytest


class StubApp:
    """A framework-independent stand-in for FastMCP: records tool names."""

    def __init__(self):
        self.names = []
        self.tools = {}

    def tool(self, name: str):
        def deco(fn):
            self.names.append(name)
            self.tools[name] = fn
            return fn

        return deco


def test_combined_registration_exposes_both_namespaces():
    from qlab.mcp.quant_lab import register_lab_tools
    from qlab.mcp.quant_trader import register_trader_tools
    from qlab.mcp.guardrails import LabState
    from qlab.mcp.quant_trader import TraderState
    from qlab.state.registry import Registry

    reg = Registry(":memory:")
    app = StubApp()
    register_lab_tools(app, LabState(offline=True, registry=reg))
    register_trader_tools(app, TraderState(registry=reg, offline=True))
    assert "moments.estimate" in app.names
    assert "qa.data_integrity" in app.names
    assert "research.apply_views" in app.names
    assert "research.equilibrium_returns" in app.names
    assert "research.window_evidence" in app.names
    assert "research.predict_vol" not in app.names
    # Research-stage executables are owner-only: agent-facing surfaces —
    # headless included — must not mount them (catalog stage boundary).
    assert "selection.run" not in app.names

    owner_app = StubApp()
    register_lab_tools(
        owner_app, LabState(offline=True, registry=reg), owner_only=True)
    assert "selection.run" in owner_app.names
    assert "qa.data_integrity" in owner_app.names
    assert "research.apply_views" in owner_app.names
    assert "research.equilibrium_returns" in owner_app.names
    assert "research.predict_vol" in owner_app.names
    assert "research.window_evidence" in owner_app.names
    assert "selection_run" not in owner_app.names
    assert {"algorithms.list", "algorithms.describe", "algorithms.solve"} <= set(app.names)
    assert "registry.log_verdict" in app.names
    assert "propose_rebalance" in app.names and "execute_plan" in app.names
    assert not any("place" in n and "order" in n for n in app.names)  # still no raw order tool
    assert not any(n in app.names for n in (
        "solve.quantum", "solve.compare", "solve.qubo_resource_count",
        "solve.constructed_resource_count",
    ))


def test_window_evidence_is_in_owner_and_moments_analyst_scopes():
    from qlab.tui.claude import (
        _LAB_TOOL_BASES,
        _claude_tool,
        build_workforce_agents,
    )
    from qlab.ui.server import OWNER_LAB_TOOLS

    base = "research.window_evidence"
    assert base in OWNER_LAB_TOOLS
    assert base in _LAB_TOOL_BASES
    analyst_tools = build_workforce_agents()["moments-analyst"]["tools"]
    assert _claude_tool(base) in analyst_tools


def test_prediction_is_owner_scoped_to_signal_qa_and_moments_analyst():
    from qlab.mcp.tui_proxy import register_proxy_tools
    from qlab.tui.claude import (
        _LAB_TOOL_BASES,
        _PROXY_TOOLS,
        _claude_tool,
        build_workforce_agents,
    )
    from qlab.ui.server import OWNER_LAB_TOOLS

    base = "research.predict_vol"
    tool = _claude_tool(base)
    assert base in OWNER_LAB_TOOLS
    assert base in _LAB_TOOL_BASES
    assert tool in _PROXY_TOOLS

    proxy = StubApp()
    register_proxy_tools(proxy, object())
    assert "research_predict_vol" in proxy.names

    agents = build_workforce_agents()
    assert tool in agents["signal-qa"]["tools"]
    assert tool in agents["moments-analyst"]["tools"]
    for role in (
        "data-qa",
        "challenger",
        "optimization-runner",
        "referee",
        "reporter",
    ):
        assert tool not in agents[role]["tools"]


def test_prediction_tool_round_trip_is_offline_and_dsr_excluded(reg):
    from qlab.mcp.guardrails import LabState
    from qlab.mcp.quant_lab import register_lab_tools

    app = StubApp()
    register_lab_tools(
        app,
        LabState(offline=True, registry=reg, seed=17),
        owner_only=True,
    )
    trials_before = (reg.trial_count(), reg.backtest_trial_count())

    result = app.tools["research.predict_vol"](
        as_of="2024-12-31",
        universe="core",
        lookback_days=504,
    )

    assert {
        "run_id",
        "mean_ic",
        "ic_stability",
        "usable",
        "chosen_alpha",
        "per_fold",
        "caveats",
    } <= set(result)
    assert result["caveats"] == [
        "risk prediction only",
        "research stage",
    ]
    assert len(result["per_fold"]) == 5
    assert all(
        fold["test_start_index"] - fold["train_end_index"] - 1 == 21
        for fold in result["per_fold"]
    )
    report = reg.report(result["run_id"])
    assert report["run"][0]["kind"] == "prediction"
    assert report["run"][0]["spec"]["algorithm_id"] == "vol_prediction_ridge"
    assert report["run"][0]["spec"]["dsr_trial_counted"] is False
    assert (reg.trial_count(), reg.backtest_trial_count()) == trials_before


def test_objective_build_refuses_research_only_forms_but_runs_operational(reg):
    """The analyst->optimizer handoff completes for an operational objective,
    and a research-only form (mvsk) is refused loudly at build time — so the
    dead-end that used to hard-block the optimizer never reaches it.
    """
    from qlab.algorithms.catalog import operational_objective_forms
    from qlab.mcp.guardrails import LabState
    from qlab.mcp.quant_lab import register_lab_tools

    app = StubApp()
    register_lab_tools(
        app, LabState(offline=True, registry=reg, seed=7), owner_only=True)

    moment_set_id = app.tools["moments.estimate"](
        as_of="2022-06-30", universe="core", lookback_days=504,
        higher_moments=False,
    )["moment_set_id"]

    # Only forms an operational prepared-objective algorithm can solve are
    # buildable on the staged surface; this is the catalog, not a hard-coded set.
    assert operational_objective_forms() == {"min_variance", "max_utility"}

    # The exact staged pipeline the workforce optimizer runs: build the
    # operational objective, then solve it through the configured policy. No
    # block.
    policy = app.tools["policy.current"]()
    built = app.tools["objective.build"](
        moment_set_id=moment_set_id, form="min_variance")
    assert built["form"] == "min_variance"
    solved = app.tools["algorithms.solve"](
        objective_id=built["objective_id"],
        algorithm_id=policy["algorithm_id"],
        max_weight=policy["constraints"]["max_weight"],
    )
    assert solved["status"] in ("optimal", "suboptimal")
    assert abs(sum(solved["weights"].values()) - 1.0) < 1e-6

    # max_utility is also operationally solvable (the min_variance algorithm),
    # so it builds too.
    assert app.tools["objective.build"](
        moment_set_id=moment_set_id, form="max_utility")["form"] == "max_utility"

    # mvsk has no operational solver: refuse at build, in the analyst phase,
    # instead of letting the optimizer phase block the whole run.
    with pytest.raises(PermissionError, match="no operational solver"):
        app.tools["objective.build"](moment_set_id=moment_set_id, form="mvsk")


def test_data_integrity_is_in_every_agent_visible_registration_scope():
    from qlab.tui.claude import (
        _LAB_TOOL_BASES,
        _PROXY_TOOLS,
        _claude_tool,
        build_workforce_agents,
    )
    from qlab.ui.server import OWNER_LAB_TOOLS

    base = "qa.data_integrity"
    tool = _claude_tool(base)
    assert base in OWNER_LAB_TOOLS
    assert base in _LAB_TOOL_BASES
    assert tool in _PROXY_TOOLS
    assert tool in build_workforce_agents()["data-qa"]["tools"]
    assert tool not in build_workforce_agents()["signal-qa"]["tools"]


def test_equilibrium_returns_is_in_agent_visible_owner_scope():
    from qlab.tui.claude import (
        _LAB_TOOL_BASES,
        _PROXY_TOOLS,
        _claude_tool,
    )
    from qlab.ui.server import OWNER_LAB_TOOLS

    base = "research.equilibrium_returns"
    assert base in OWNER_LAB_TOOLS
    assert base in _LAB_TOOL_BASES
    assert _claude_tool(base) in _PROXY_TOOLS


def test_apply_views_is_owner_proxy_visible_and_extractor_only():
    from qlab.mcp.tui_proxy import register_proxy_tools
    from qlab.tui.claude import (
        _LAB_TOOL_BASES,
        _PROXY_TOOLS,
        _claude_tool,
        build_workforce_agents,
    )
    from qlab.ui.server import OWNER_LAB_TOOLS

    base = "research.apply_views"
    tool = _claude_tool(base)
    assert base in OWNER_LAB_TOOLS
    assert base in _LAB_TOOL_BASES
    assert tool in _PROXY_TOOLS

    proxy = StubApp()
    register_proxy_tools(proxy, object())
    assert "research_apply_views" in proxy.names

    # Unrelated goals do not even materialize the quarantined session role.
    assert "news-extractor" not in build_workforce_agents()
    agents = build_workforce_agents(
        "Apply views from these pasted news excerpts."
    )
    extractor = agents["news-extractor"]
    assert extractor["tools"] == [tool]
    assert not any(
        token in granted
        for granted in extractor["tools"]
        for token in (
            "data_", "registry_", "solve", "backtest", "workflow_", "market",
            "portfolio", "web",
        )
    )
    coordinator = agents["qlab-coordinator"]
    assert "news-extractor" in coordinator["tools"][0]
    assert "CONTEXT — DRY NEWS VIEWS" in coordinator["prompt"]
    assert "ordinary unconditioned moment set and objective" in (
        coordinator["prompt"]
    )
    assert "downstream solver conditioning is future work" in (
        coordinator["prompt"]
    )


def test_data_integrity_reports_an_injected_stale_ticker(reg, monkeypatch):
    from datetime import date

    import numpy as np
    import pandas as pd

    import qlab.mcp.quant_lab as quant_lab
    from qlab.core.types import DataSnapshot
    from qlab.core.universe import load_universe
    from qlab.mcp.guardrails import LabState

    tickers = load_universe().tickers("core")
    index = pd.bdate_range(end="2020-02-28", periods=40)
    base = np.linspace(100.0, 110.0, len(index))
    prices = pd.DataFrame({
        ticker: base * (1.0 + offset / 100.0)
        for offset, ticker in enumerate(tickers)
    }, index=index)
    stale_ticker = tickers[-1]
    prices.loc[index[-8]:, stale_ticker] = np.nan
    snapshot = DataSnapshot(
        tickers=tickers,
        prices=prices,
        as_of=date(2020, 2, 28),
        source="synthetic",
    )

    def injected_snapshot(requested, as_of, **kwargs):
        assert requested == tickers
        assert as_of == date(2020, 2, 28)
        assert kwargs["lookback_days"] == 40
        return snapshot

    monkeypatch.setattr(quant_lab.market, "snapshot", injected_snapshot)
    app = StubApp()
    quant_lab.register_lab_tools(
        app, LabState(offline=True, registry=reg),
    )

    result = app.tools["qa.data_integrity"](
        as_of="2020-02-28",
        universe="core",
        lookback_days=40,
    )

    assert result["clean"] is False
    assert result["flagged_tickers"] == [stale_ticker]
    assert result["thresholds"] == {
        "max_last_bar_age_days": 4,
        "max_longest_gap_days": 5,
        "max_abs_1d_return": 0.35,
        "max_missing_bars": 0,
        "min_span_coverage": 0.95,
    }
    stale = next(
        row for row in result["findings"]
        if row["ticker"] == stale_ticker
    )
    assert stale["last_bar_age_days"] > 4
    assert stale["missing_bars"] == 8
    assert stale["n_obs"] == 32
    assert stale["span_coverage"] == 0.8
    assert {"missing_bars", "stale_series", "insufficient_span"} <= set(
        stale["issues"]
    )


def test_backtest_run_refuses_mislabeled_objective_solver_pair(
    reg, monkeypatch,
):
    import qlab.mcp.quant_lab as quant_lab
    from qlab.mcp.guardrails import LabState

    app = StubApp()
    register = quant_lab.register_lab_tools
    register(app, LabState(offline=True, registry=reg))

    def unexpected_prices(*_args, **_kwargs):
        pytest.fail("pair validation must run before market data is loaded")

    monkeypatch.setattr(quant_lab.market, "get_prices", unexpected_prices)
    with pytest.raises(PermissionError) as refused:
        app.tools["backtest.run"](
            objective="target_semivariance",
            solver="hrp",
        )

    message = str(refused.value)
    assert "objective/solver mismatch" in message
    assert "target_semivariance" in message
    assert "hrp" in message
    assert reg.backtest_trial_count() == 0


def test_backtest_run_accepts_every_operational_pair_in_ablation_spec(reg):
    import qlab.mcp.quant_lab as quant_lab
    from qlab.algorithms.catalog import list_algorithms
    from qlab.experiment import _load_spec
    from qlab.mcp.guardrails import LabState
    from qlab.paths import data_path

    spec = _load_spec(data_path("configs", "specs", "ablation_v1.yaml"))
    catalog = list_algorithms(stage="operational")

    def is_operational_pair(arm):
        catalog_solver = None if arm["solver"] == "none" else arm["solver"]
        return any(
            row["solver"] == catalog_solver
            and (
                arm["objective"] == row["id"]
                or arm["objective"] in row["objective_forms"]
            )
            for row in catalog
        )

    arms = [arm for arm in spec["arms"] if is_operational_pair(arm)]
    assert [arm["id"] for arm in arms] == [
        "B0", "B1", "B2", "B3", "A1", "B4", "A2",
    ]

    app = StubApp()
    quant_lab.register_lab_tools(
        app, LabState(offline=True, registry=reg),
    )
    for arm in arms:
        result = app.tools["backtest.run"](
            objective=arm["objective"],
            solver=arm["solver"],
            start="2019-01-01",
            end="2020-12-31",
            lookback_days=60,
        )
        assert result["arm"] == f"{arm['objective']}:{arm['solver']}"
        assert result["metrics"]["n_obs"] > 0


def test_lab_and_trader_share_one_registry():
    from qlab.mcp.guardrails import LabState
    from qlab.mcp.quant_trader import TraderState
    from qlab.state.registry import Registry

    reg = Registry(":memory:")
    lab, trader = LabState(offline=True, registry=reg), TraderState(registry=reg, offline=True)
    lab.registry.record_event("x", {})
    assert trader.registry.read_events(5)[0]["kind"] == "x"


# -- startup guard: never two DuckDB writers -------------------------------
def test_owner_runtime_alive_true_when_api_responds(monkeypatch):
    import qlab.mcp.server as server

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(url, timeout=None):
        # The probe must use the lock-free readiness route: /api/system is
        # served under the owner's dispatch lock, so a long action made the
        # probe time out and the guard concluded "no owner".
        assert "/readyz" in url
        return FakeResp()

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)
    assert server.owner_runtime_alive(8765) is True


def test_a_slow_owner_is_alive_not_absent(monkeypatch):
    # Guessing "no owner" from silence is the dangerous direction: it lets a
    # second DuckDB writer start against a book someone already owns.
    import qlab.mcp.server as server

    def timeout_urlopen(url, timeout=None):
        raise TimeoutError("owner busy under its dispatch lock")

    monkeypatch.setattr(server.urllib.request, "urlopen", timeout_urlopen)
    assert server.owner_runtime_alive(8765) is True


def test_a_refused_connection_is_a_real_absence(monkeypatch):
    import urllib.error

    import qlab.mcp.server as server

    def refused(url, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError("nothing listening"))

    monkeypatch.setattr(server.urllib.request, "urlopen", refused)
    assert server.owner_runtime_alive(8765) is False


def test_owner_runtime_alive_false_when_refused(monkeypatch):
    import qlab.mcp.server as server

    def fake_urlopen(url, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)
    assert server.owner_runtime_alive(8765) is False


def test_main_refuses_to_start_when_owner_runtime_alive(monkeypatch, capsys):
    import qlab.mcp.server as server

    monkeypatch.setattr(server, "owner_runtime_alive", lambda port: True)
    with pytest.raises(SystemExit) as excinfo:
        server.main()
    assert excinfo.value.code == 3
    err = capsys.readouterr().err
    assert "qlab-operator" in err  # points operators at the proxy


@pytest.mark.parametrize("module_name", ["quant_lab", "quant_trader"])
def test_retired_standalone_module_mains_delegate_to_guarded_server(
    module_name, monkeypatch
):
    import importlib
    import qlab.mcp.server as server

    called = []
    monkeypatch.setattr(server, "main", lambda: called.append(True))
    importlib.import_module(f"qlab.mcp.{module_name}").main()

    assert called == [True]


def test_predictor_board_is_owner_only_and_fully_allowlisted():
    from qlab.mcp.guardrails import LabState
    from qlab.mcp.quant_lab import register_lab_tools
    from qlab.state.registry import Registry
    from qlab.tui.claude import _LAB_TOOL_BASES, _PROXY_TOOLS, _claude_tool
    from qlab.ui.server import OWNER_LAB_TOOLS

    base = "research.predictor_board"
    assert base in OWNER_LAB_TOOLS
    assert base in _LAB_TOOL_BASES
    assert _claude_tool(base) in _PROXY_TOOLS

    reg = Registry(":memory:")
    agent_app = StubApp()
    register_lab_tools(agent_app, LabState(offline=True, registry=reg))
    # Research-stage executable: absent from every agent-facing surface.
    assert base not in agent_app.names

    owner_app = StubApp()
    register_lab_tools(
        owner_app, LabState(offline=True, registry=reg), owner_only=True)
    assert base in owner_app.names
    reg.close()


def test_predictor_board_logs_one_dsr_exempt_run_and_no_backtest():
    from qlab.mcp.guardrails import LabState
    from qlab.mcp.quant_lab import register_lab_tools
    from qlab.state.registry import Registry

    reg = Registry(":memory:")
    owner_app = StubApp()
    register_lab_tools(
        owner_app, LabState(offline=True, registry=reg), owner_only=True)
    result = owner_app.tools["research.predictor_board"](
        as_of="2022-06-30", universe="core", lookback_days=420)

    board = result["board"]
    assert board["baseline"] == "ridge:none"
    assert [entry["model_id"] for entry in board["models"]] == board["ranking"]
    assert board["champion"] is None or board["champion"] in board["ranking"]

    runs = reg.list_runs(limit=5)
    assert runs and runs[0]["kind"] == "predictor_board"
    spec = runs[0]["spec"]
    assert spec["dsr_trial_counted"] is False
    assert spec["source"] == "synthetic"
    assert spec["board"]["admission"] == board["admission"]

    report = reg.report(result["run_id"])
    assert report["backtests"] == []
    assert report["solutions"] == []
    reg.close()


def test_predictor_board_accepts_tuning_and_records_the_search():
    from qlab.mcp.guardrails import LabState
    from qlab.mcp.quant_lab import register_lab_tools
    from qlab.state.registry import Registry

    reg = Registry(":memory:")
    owner_app = StubApp()
    register_lab_tools(
        owner_app, LabState(offline=True, registry=reg), owner_only=True)
    result = owner_app.tools["research.predictor_board"](
        as_of="2022-06-30",
        universe="core",
        lookback_days=420,
        models=["ridge:none", "kernel:zz"],
        alphas=[0.5, 2.0],
        map_weights=[0.5],
        n_splits=4,
    )

    search = result["board"]["search"]
    assert search["models"] == ["ridge:none", "kernel:zz"]
    assert search["alphas"] == [0.5, 2.0]
    assert search["map_weights"] == [0.5]
    assert search["n_splits"] == 4
    # The persisted run carries the same record: a tuned run is reproducible
    # from its own row, not from whoever remembers the call.
    runs = reg.list_runs(limit=1)
    assert runs[0]["spec"]["board"]["search"] == search
    reg.close()


def test_predictor_board_refuses_a_bad_grid_loudly():
    import pytest as _pytest

    from qlab.mcp.guardrails import LabState
    from qlab.mcp.quant_lab import register_lab_tools
    from qlab.state.registry import Registry

    reg = Registry(":memory:")
    owner_app = StubApp()
    register_lab_tools(
        owner_app, LabState(offline=True, registry=reg), owner_only=True)
    board = owner_app.tools["research.predictor_board"]

    with _pytest.raises(ValueError, match="alphas"):
        board(as_of="2022-06-30", lookback_days=420, alphas=[-1.0])
    with _pytest.raises(ValueError, match="baseline"):
        board(as_of="2022-06-30", lookback_days=420, models=["kernel:zz"])
    with _pytest.raises(ValueError, match="unknown model"):
        board(as_of="2022-06-30", lookback_days=420,
              models=["ridge:none", "forest:deep"])
    # Nothing was logged for refused runs.
    assert reg.list_runs(limit=5) == []
    reg.close()


def test_predictor_board_catalog_entry_is_research_only():
    from qlab.algorithms import get_algorithm

    spec = get_algorithm("predictor_board")
    assert spec.category == "prediction"
    assert spec.stage == "research"
    assert spec.agent_tool == "research.predictor_board"
    assert spec.agent_usable is False
