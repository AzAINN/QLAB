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
        assert "/api/system" in url
        return FakeResp()

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)
    assert server.owner_runtime_alive(8765) is True


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
