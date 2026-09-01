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

import pathlib
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
    # A5 and A6 share A1's operational (objective, solver) pair; what makes
    # each a research arm is a params key — `views_source` for A5,
    # `cardinality` for A6 — and `backtest.run` has no parameter for either.
    # Running them here therefore runs a plain unconditioned, full-universe
    # min-variance: the conditioning and the k-of-N selection are unreachable
    # from this tool by construction, which is exactly the claim.
    assert [arm["id"] for arm in arms] == [
        "B0", "B1", "B2", "B3", "A1", "B4", "A5", "A6", "A2",
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


def test_qualitative_matrix_honours_its_as_of_and_its_universe(reg):
    """A read tool that validates ``as_of`` and then ignores it is look-ahead."""
    import pytest as _pytest

    from qlab.mcp.guardrails import LabState
    from qlab.mcp.quant_lab import register_lab_tools

    app = StubApp()
    register_lab_tools(app, LabState(offline=True, registry=reg, seed=7))
    matrix = app.tools["research.qualitative_matrix"]

    assert matrix(as_of="2022-06-30")["status"] == "never_built"

    def log(as_of, window, rows):
        reg.log_run("qualitative_matrix", {"source": "desk", "matrix": {
            "as_of": as_of, "window_hash": window,
            "rows": {t: {"ticker": t, "coverage": c, "publishers": 1,
                         "corroborated": 0, "primary_docs": 0,
                         "days_to_next_release": None, "claim_keys": [t]}
                     for t, c in rows.items()}}})

    log("2022-06-30", "w1", {"ACWI": 3, "NOTINUNIVERSE": 9})
    log("2022-09-30", "w2", {"ACWI": 7})

    # The newest matrix at or before the asked-for date, not the newest logged.
    early = matrix(as_of="2022-08-01")
    assert early["status"] == "ok" and early["as_of"] == "2022-06-30"
    # ... with rows filtered to the requested universe.
    assert set(early["rows"]) == {"ACWI"}
    assert matrix(as_of="2022-12-31")["as_of"] == "2022-09-30"
    assert matrix(as_of="2022-01-01")["status"] == "never_built"

    log("2023-01-31", "w3", {"NOTINUNIVERSE": 4})
    with _pytest.raises(ValueError, match="no row"):
        matrix(as_of="2023-06-30")


def test_qualitative_matrix_resolves_an_old_window_on_a_busy_registry(reg):
    """A bounded scan on the read tool is the same cliff as on the arm."""
    from qlab.mcp.guardrails import LabState
    from qlab.mcp.quant_lab import register_lab_tools

    app = StubApp()
    register_lab_tools(app, LabState(offline=True, registry=reg, seed=7))

    def log(as_of, ticker, key):
        reg.log_run("qualitative_matrix", {"source": "desk", "matrix": {
            "as_of": as_of, "window_hash": key,
            "rows": {ticker: {"ticker": ticker, "coverage": 1,
                              "publishers": 1, "corroborated": 0,
                              "primary_docs": 0, "days_to_next_release": None,
                              "claim_keys": [key]}}}})

    log("2020-06-30", "ACWI", "target")
    for i in range(250):
        log(f"2024-01-{i % 28 + 1:02d}", "SPY", f"noise{i}")

    out = app.tools["research.qualitative_matrix"](as_of="2020-12-31")
    assert out["status"] == "ok" and out["as_of"] == "2020-06-30"
    assert set(out["rows"]) == {"ACWI"}


def test_qualitative_matrix_is_in_owner_proxy_and_analyst_scopes():
    """A grant nothing forwards is a grant that silently disappears.

    `research.qualitative_matrix` was granted in agents/*.md and served by the
    owner, but the proxy never registered it and `_LAB_TOOL_BASES` never
    listed it — so `_proxy_tool` returned None and `build_workforce_agents`
    dropped it from the role's list without a word. Four things have to agree
    for a grant to reach an agent, so all four are asserted together.
    """
    from qlab.mcp.tui_proxy import register_proxy_tools
    from qlab.tui.claude import (
        _LAB_TOOL_BASES,
        _PROXY_TOOLS,
        _claude_tool,
        build_workforce_agents,
    )
    from qlab.ui.server import OWNER_LAB_TOOLS

    base = "research.qualitative_matrix"
    tool = _claude_tool(base)
    assert base in OWNER_LAB_TOOLS
    assert base in _LAB_TOOL_BASES
    assert tool in _PROXY_TOOLS

    proxy = StubApp()
    register_proxy_tools(proxy, object())
    assert "research_qualitative_matrix" in proxy.names

    agents = build_workforce_agents()
    assert tool in agents["moments-analyst"]["tools"]
    # Reading the record is not conditioning on it: the roles that neither
    # choose estimators nor manage the desk stay out.
    for role in ("optimization-runner", "referee", "reporter", "news-extractor"):
        assert tool not in agents.get(role, {"tools": []})["tools"]


def test_qualitative_matrix_serves_the_desks_own_record_not_an_arms(reg):
    """The ablation writes matrices to the same registry; they are not the desk's.

    `matrix_runs(source=None, ...)` let an `ablation_a5` window — built over
    the arm's universe, from the arm's rules, for a research walk — answer as
    the desk's record of what the press said. The stamp is what separates them.
    """
    from qlab.ui.server import UISession

    session = UISession(offline_default=True, seed=7, registry=reg)
    row = {"ticker": "ACWI", "coverage": 1, "publishers": 1, "corroborated": 1,
           "primary_docs": 1, "days_to_next_release": None, "claim_keys": []}
    desk = reg.log_run("qualitative_matrix", {
        "source": "desk",
        "matrix": {"as_of": "2021-06-28", "window_hash": "desk",
                   "rows": {"ACWI": dict(row, coverage=3)}}})
    reg.log_run("qualitative_matrix", {
        "source": "ablation_a5",
        "matrix": {"as_of": "2021-06-30", "window_hash": "arm",
                   "rows": {"ACWI": dict(row, coverage=99)}}})

    out = session.call_lab_tool(
        "research.qualitative_matrix", {"as_of": "2021-06-30"}, offline=True)
    assert out["run_id"] == desk
    assert out["source"] == "desk"
    assert out["window_hash"] == "desk"
    assert out["rows"]["ACWI"]["coverage"] == 3


def test_the_chat_atlas_action_tools_are_wired_all_four_ways():
    """A grant nothing forwards is a grant that silently disappears.

    Four things have to agree for the chat Atlas to reach one of these: the
    owner has to serve the route, the proxy has to register the tool, the chat
    allowlist has to name it, and the persona has to grant it. Asserted
    together, because each one alone is silent when it is the one that is
    missing.
    """
    from qlab.agents.loader import load_agents
    from qlab.mcp.tui_proxy import register_proxy_tools
    from qlab.tui.claude import _CHAT_TOOLS, _claude_tool

    proxy = StubApp()
    register_proxy_tools(proxy, object())
    atlas = {a.name: a for a in load_agents()}["atlas"]

    for base, proxy_name, route in (
        ("workflow.start", "workflow_start", "/api/workflows/start"),
        ("workflow.resume", "workflow_resume", "/api/workflows/<id>/resume"),
        ("atlas.task.create", "atlas_task_create", "/api/atlas/tasks"),
        ("approvals.list", "approvals_list", "/api/approvals"),
    ):
        assert proxy_name in proxy.names, (base, route)
        assert _claude_tool(base) in _CHAT_TOOLS, base
        assert f"mcp__qlab__{base}" in atlas.tools, base


def test_the_owner_serves_every_route_the_chat_action_tools_call():
    from qlab.state.registry import Registry
    from qlab.ui.server import UISession, handle_api

    session = UISession(offline_default=True, registry=Registry(":memory:"))
    session.coordinator_status = lambda: {"driving": False, "workflow_id": ""}
    session.drive_workflow = lambda wid, goal, roles=(): {
        "driving": False, "reason": "pinned off in tests"}
    session.atlas.set_mode("research")

    status, started = handle_api(
        session, "POST", "/api/workflows/start", {},
        {"template_id": "regime_review", "offline": True})
    assert status == 200
    workflow_id = started["workflow_id"]

    status, _ = handle_api(session, "GET", "/api/approvals", {}, {})
    assert status == 200

    status, task = handle_api(
        session, "POST", "/api/atlas/tasks", {},
        {"kind": "regime_flip", "reason": "the panel flipped"})
    assert status == 200 and task["template_id"] == "regime_review"

    session.registry.interrupt_workflow(workflow_id, "stopped for the test")
    status, resumed = handle_api(
        session, "POST", f"/api/workflows/{workflow_id}/resume", {}, {})
    assert status == 200 and resumed["status"] == "running"


def test_the_one_click_book_is_on_no_agent_surface():
    """A census, not a spot check. `POST /api/desk/proposal/book` approves and
    executes in one call, so it is exactly the raw-order tool invariant 3
    forbids — the comment on the route says it is client-only, and this is what
    makes that a fact. Every name a book could plausibly wear is checked
    against all three agent-facing surfaces at once."""
    from qlab.mcp.quant_lab import register_lab_tools
    from qlab.mcp.quant_trader import register_trader_tools, TraderState
    from qlab.mcp.guardrails import LabState
    from qlab.mcp.tui_proxy import register_proxy_tools
    from qlab.state.registry import Registry
    from qlab.tui.claude import _LAB_TOOL_BASES, _PROXY_TOOLS
    from qlab.ui.server import OWNER_LAB_TOOLS

    reg = Registry(":memory:")
    combined = StubApp()
    register_lab_tools(combined, LabState(offline=True, registry=reg))
    register_trader_tools(combined, TraderState(registry=reg, offline=True))
    owner_side = StubApp()
    register_lab_tools(owner_side, LabState(offline=True, registry=reg),
                       owner_only=True)
    proxy = StubApp()
    register_proxy_tools(proxy, object())

    surfaces = {
        "OWNER_LAB_TOOLS": set(OWNER_LAB_TOOLS),
        "the chat's lab bases": set(_LAB_TOOL_BASES),
        "the chat's proxy tools": set(_PROXY_TOOLS),
        "the qlab-operator proxy": set(proxy.names),
        "the combined server": set(combined.names),
        "the combined server, owner-only": set(owner_side.names),
    }
    for where, names in surfaces.items():
        booking = sorted(n for n in names if "book" in n.lower()
                         or "proposal" in n.lower())
        assert booking == [], f"{where} exposes {booking}"
        # And by every spelling the route itself could be registered under.
        for spelling in ("desk.proposal.book", "desk_proposal_book",
                         "proposal_book", "book_proposal",
                         "/api/desk/proposal/book"):
            assert spelling not in names, f"{where} exposes {spelling}"


def test_no_agent_surface_can_reach_a_standing_grant():
    """A census, not a spot check — and the property the whole design rests on.

    A standing grant replaces the per-plan human confirmation, so an agent that
    could create, read around, or withdraw one would have handed itself the one
    thing invariant 3 keeps out of its reach. The routes are the operator's and
    refuse chat origin; this is what makes "no MCP tool, no chat action tool
    and no proxy verb names a grant" a fact rather than an intention.

    Every surface an agent can see at once, by every name a grant could
    plausibly wear — including the personas, which the design record's surface
    table says are unchanged *deliberately*.
    """
    from qlab.agents.loader import load_agents
    from qlab.mcp.quant_lab import register_lab_tools
    from qlab.mcp.quant_trader import register_trader_tools, TraderState
    from qlab.mcp.guardrails import LabState
    from qlab.mcp.tui_proxy import register_proxy_tools
    from qlab.state.registry import Registry
    from qlab.tui.claude import (
        CHAT_ACTION_BASES, _CHAT_TOOLS, _LAB_TOOL_BASES, _PROXY_TOOLS)
    from qlab.ui.server import OWNER_LAB_TOOLS

    reg = Registry(":memory:")
    # `qlab/mcp/server.py` mounts exactly these two registrations over one
    # registry, so the combined surface below IS the headless server's.
    combined = StubApp()
    register_lab_tools(combined, LabState(offline=True, registry=reg))
    register_trader_tools(combined, TraderState(registry=reg, offline=True))
    owner_side = StubApp()
    register_lab_tools(owner_side, LabState(offline=True, registry=reg),
                       owner_only=True)
    proxy = StubApp()
    register_proxy_tools(proxy, object())

    surfaces = {
        "OWNER_LAB_TOOLS": set(OWNER_LAB_TOOLS),
        "the chat's lab bases": set(_LAB_TOOL_BASES),
        "the chat's proxy tools": set(_PROXY_TOOLS),
        "the chat's action tools": set(_CHAT_TOOLS) | set(CHAT_ACTION_BASES),
        "the qlab-operator proxy": set(proxy.names),
        "the combined server": set(combined.names),
        "the combined server, owner-only": set(owner_side.names),
    }
    for agent in load_agents():
        surfaces[f"the {agent.name} persona"] = set(agent.tools)

    for where, names in surfaces.items():
        assert names, f"{where} is empty; the census would prove nothing"
        reaching = sorted(
            name for name in names
            if any(word in name.lower()
                   for word in ("grant", "authority", "standing", "revoke")))
        assert reaching == [], f"{where} exposes {reaching}"
        # And by every spelling the three routes could be registered under.
        for spelling in ("desk.authority", "desk_authority",
                         "authority_grant", "grant_authority",
                         "revoke_authority", "authority.revoke",
                         "/api/desk/authority", "/api/desk/authority/revoke"):
            assert spelling not in names, f"{where} exposes {spelling}"

    # Names are not reach. A surface calling the route from a tool named
    # `desk_settings_read` passes every assertion above, so the routes are also
    # pinned absent from the SOURCE of every agent-facing module: the proxy
    # forwards owner routes by path, and the chat's grant is built there too.
    import qlab.mcp
    import qlab.tui.claude

    sources = sorted(pathlib.Path(qlab.mcp.__file__).parent.glob("*.py"))
    sources.append(pathlib.Path(qlab.tui.claude.__file__))
    assert len(sources) >= 5, sources
    for source in sources:
        text = source.read_text()
        for route in ("/api/desk/authority", "/api/desk/authority/revoke"):
            assert route not in text, f"{source.name} names {route}"
