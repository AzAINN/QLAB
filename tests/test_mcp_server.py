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

    def tool(self, name: str):
        def deco(fn):
            self.names.append(name)
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
    assert "research.equilibrium_returns" in app.names
    assert "research.window_evidence" in app.names
    # Research-stage executables are owner-only: agent-facing surfaces —
    # headless included — must not mount them (catalog stage boundary).
    assert "selection.run" not in app.names

    owner_app = StubApp()
    register_lab_tools(
        owner_app, LabState(offline=True, registry=reg), owner_only=True)
    assert "selection.run" in owner_app.names
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
