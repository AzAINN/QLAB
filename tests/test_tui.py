"""Quiet-workstation TUI contracts and headless layout behavior."""

from __future__ import annotations

import asyncio
import json

import pytest

from qlab.state.registry import Registry
from qlab.ui.server import UISession, handle_api


pytest.importorskip("textual")


class InProcessClient:
    def __init__(self):
        self.session = UISession(offline_default=True, registry=Registry(":memory:"))

    def get(self, path, **params):
        query = {key: [str(value)] for key, value in params.items()}
        status, obj = handle_api(self.session, "GET", path, query, {})
        assert status == 200, obj
        return obj

    def post(self, path, body=None):
        status, obj = handle_api(self.session, "POST", path, {}, body or {})
        assert status == 200, obj
        return obj


def _snapshot():
    assets = []
    for index, ticker in enumerate(["ACWI", "BNDW", "GSG", "IGF", "GLD", "VNQ", "EMB"]):
        assets.append({
            "ticker": ticker,
            "price": 100.0 + index,
            "change_1d": 0.001 * (index + 1),
            "change_20d": 0.01,
            "realized_vol": 0.10,
            "history": [100.0 + index + point * 0.1 for point in range(40)],
        })
    return {
        "portfolio": {
            "equity": 10_100.0,
            "cash": 1_000.0,
            "drawdown": 0.01,
            "kill_switch_at": 0.15,
            "weights": {"ACWI": 0.4, "BNDW": 0.3, "GLD": 0.3},
            "target_weights": {"ACWI": 0.35, "BNDW": 0.35, "GLD": 0.3},
        },
        "market": {
            "source": "synthetic",
            "as_of": "2026-07-17",
            "bar_age_days": 0,
            "frequency": "daily",
            "regime": {
                "regime": "calm", "signal": 0.08, "threshold": 0.12,
                "method": "realized_vol_threshold",
            },
            "assets": assets,
        },
        "agents": [
            {"name": "moments-analyst", "state": "idle", "authority": "RESEARCH"},
            {"name": "challenger", "state": "idle", "authority": "CHALLENGE"},
            {"name": "optimization-runner", "state": "idle", "authority": "SOLVE"},
            {"name": "referee", "state": "idle", "authority": "VETO"},
            {"name": "reporter", "state": "idle", "authority": "PAPER"},
        ],
        "decisions": [],
        "runs": [],
        "plans": [],
        "orders": [],
        "events": [{
            "event_id": "event-1", "ts": "2026-07-17T14:31:09+00:00",
            "kind": "demo", "payload": {},
        }],
        "system": {
            "mode": "paper", "mcp_configured": True, "claude_available": True,
            "governed_available": False,
            "governed_lock_reason": "single-owner runtime required",
        },
        "quantum": {"total_logical_qubits": 434, "dirac3_continuous_variables": 7},
    }


class StubClient:
    def __init__(self):
        self.posts = []

    def get(self, path, **params):
        assert path == "/api/tui"
        return _snapshot()

    def post(self, path, body=None):
        self.posts.append((path, body or {}))
        return {
            "decision_id": "decision-1",
            "regime": {"regime": "calm"},
            "trade": {"executed": False},
        }


def test_gather_snapshot_uses_single_tui_contract():
    from qlab.tui.client import gather_snapshot

    snapshot = gather_snapshot(InProcessClient(), offline=True)
    assert snapshot["system"]["mode"] == "paper"
    assert snapshot["market"]["frequency"] == "daily"
    assert snapshot["quantum"]["total_logical_qubits"] == 434


def test_claude_stream_parser_emits_text_and_hides_thinking():
    from qlab.tui.claude import parse_stream_line

    delta = parse_stream_line(
        '{"type":"stream_event","event":{"type":"content_block_delta",'
        '"delta":{"type":"text_delta","text":"hello"}}}'
    )
    hidden = parse_stream_line(
        '{"type":"assistant","message":{"content":['
        '{"type":"thinking","thinking":"private chain"}]}}'
    )
    tool = parse_stream_line(
        '{"type":"assistant","message":{"content":['
        '{"type":"tool_use","name":"mcp__qlab__moments.estimate"}]}}'
    )

    assert [(event.kind, event.text) for event in delta] == [("text_delta", "hello")]
    assert hidden == []
    assert tool[0].kind == "tool_start"
    assert tool[0].tool == "mcp__qlab__moments.estimate"


def test_governed_claude_command_loads_only_propose_proxy():
    from qlab.tui.claude import build_claude_argv

    argv = build_claude_argv(
        "inspect", governed=True,
        runtime_url="http://127.0.0.1:9999/", offline=True,
    )
    config = json.loads(argv[argv.index("--mcp-config") + 1])
    server = config["mcpServers"]["qlab-operator"]
    allowed = argv[argv.index("--allowedTools") + 1]

    assert config.keys() == {"mcpServers"}
    assert server["args"] == ["-m", "qlab.mcp.tui_proxy"]
    assert server["env"]["QLAB_RUNTIME_URL"] == "http://127.0.0.1:9999"
    assert "workflow.rebalance_preview" in allowed
    assert "execute" not in allowed and "order" not in allowed
    assert argv[argv.index("--tools") + 1] == ""


def test_headless_shell_has_no_header_and_switches_context():
    from qlab.tui.app import QlabTui

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0)
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            assert len(app.query("Header")) == 0
            assert app.query_one("#spine") is not None
            assert app.query_one("#agent-rail") is not None
            assert app.query_one("#system-status").content.startswith("PAPER")

            await pilot.press("2")
            await pilot.press("j")
            assert app.active_view == "market"
            assert app.active_ticker == "BNDW"

            await pilot.press("~")
            assert app.query_one("#timeline").styles.display == "block"

    asyncio.run(run())


def test_dry_rebalance_routes_through_owner_api():
    from qlab.tui.app import QlabTui

    async def run():
        client = StubClient()
        app = QlabTui(client, refresh_interval=0)
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause(0.1)
            app._handle_command("rebalance dry")
            for _ in range(20):
                if client.posts:
                    break
                await pilot.pause(0.05)
            assert client.posts == [(
                "/api/run_once",
                {"offline": True, "execute": False, "qaoa": False},
            )]

    asyncio.run(run())


def test_tui_cli_entry_is_registered():
    from qlab.autopilot.cli import build_parser

    args = build_parser().parse_args(["tui", "--refresh", "1.5"])
    assert args.command == "tui"
    assert args.refresh == 1.5


def test_operator_mcp_proxy_is_propose_only_and_never_executes():
    from qlab.mcp.tui_proxy import register_proxy_tools

    class ToolApp:
        def __init__(self):
            self.tools = {}

        def tool(self, name):
            def decorate(fn):
                self.tools[name] = fn
                return fn
            return decorate

    class ToolClient:
        offline = True

        def __init__(self):
            self.calls = []

        def get(self, path, **params):
            self.calls.append(("GET", path, params))
            return {"events": [], "runs": [], "decisions": []}

        def post(self, path, body=None):
            self.calls.append(("POST", path, body or {}))
            return {"ok": True}

    app, client = ToolApp(), ToolClient()
    register_proxy_tools(app, client)

    assert "workflow.rebalance_preview" in app.tools
    assert not any("execute" in name or "order" in name for name in app.tools)
    app.tools["workflow.rebalance_preview"]()
    assert client.calls[-1] == (
        "POST", "/api/run_once",
        {"offline": True, "execute": False, "qaoa": False},
    )
