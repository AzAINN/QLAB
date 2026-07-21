"""Quiet-workstation TUI contracts and headless layout behavior."""

from __future__ import annotations

import asyncio
import json
import subprocess

import pytest
import yaml

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
            {"name": "reporter", "state": "idle", "authority": "PROPOSE"},
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
        "algorithms": [
            {"id": "hrp", "stage": "operational"},
            {"id": "mvsk_multistart", "stage": "research"},
            {"id": "dirac3_mvsk", "stage": "research"},
            {"id": "qaoa_selection", "stage": "offline"},
        ],
        "policy": {"id": "hrp", "label": "Hierarchical risk parity"},
        "workflows": [],
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
    assert {row["stage"] for row in snapshot["algorithms"]} == {
        "operational", "research", "offline"
    }


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


def test_workforce_claude_command_loads_only_coordinator_and_owner_proxy():
    from qlab.tui.claude import build_claude_argv, build_workforce_agents

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
    # Claude-facing names must be the sanitized (underscored) forms —
    # dotted grants match nothing in Claude Code.
    assert "workflow_rebalance_preview" in allowed
    assert "." not in allowed.replace("127.0.0.1", "")
    assert "execute" not in allowed and "order" not in allowed
    assert argv[argv.index("--tools") + 1] == "default"
    assert argv[argv.index("--agent") + 1] == "qlab-coordinator"
    assert "--agents" not in argv
    # npm's Windows launcher is a .cmd file and therefore inherits cmd.exe's
    # 8,191-character ceiling. Keep ample room for longer Windows paths.
    assert len(subprocess.list2cmdline(argv)) < 4_096

    agents = build_workforce_agents()
    assert set(agents) == {
        "qlab-coordinator", "moments-analyst", "challenger",
        "optimization-runner", "referee", "reporter",
    }
    assert agents["qlab-coordinator"]["tools"] == [
        "Agent(moments-analyst,challenger,optimization-runner,referee,reporter)",
        "mcp__qlab-operator__workflow_start",
        "mcp__qlab-operator__workflow_status",
    ]
    all_role_tools = {
        tool for name, definition in agents.items() if name != "qlab-coordinator"
        for tool in definition["tools"]
    }
    assert not ({"Read", "Write", "Edit", "Bash"} & all_role_tools)
    assert not any("execute" in tool or "order" in tool for tool in all_role_tools)


def test_session_agent_files_preserve_workforce_authority(tmp_path):
    from qlab.tui.claude import build_workforce_agents, write_session_agents

    written = write_session_agents(tmp_path, build_workforce_agents())
    assert {path.stem for path in written} == {
        "qlab-coordinator", "moments-analyst", "challenger",
        "optimization-runner", "referee", "reporter",
    }
    coordinator = tmp_path / ".claude" / "agents" / "qlab-coordinator.md"
    _, front, body = coordinator.read_text(encoding="utf-8").split("---", 2)
    metadata = yaml.safe_load(front)
    assert metadata["tools"].split(", ") == [
        "Agent(moments-analyst,challenger,optimization-runner,referee,reporter)",
        "mcp__qlab-operator__workflow_start",
        "mcp__qlab-operator__workflow_status",
    ]
    assert metadata["permissionMode"] == "dontAsk"
    assert "no filesystem, shell, browser, editing, or trading tools" in body


def test_claude_parser_identifies_spawned_workforce_agent():
    from qlab.tui.claude import parse_stream_line

    events = parse_stream_line(json.dumps({
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use", "name": "Agent",
            "input": {"subagent_type": "referee"},
        }]},
    }))
    assert events[0].tool == "Agent"
    assert events[0].agent == "referee"


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


class WorkforceReadyClient(StubClient):
    def get(self, path, **params):
        snap = _snapshot()
        snap["system"]["workforce_available"] = True
        snap["system"]["mcp_proxy_available"] = True
        return snap


def test_tui_offers_constrained_claude_workforce_once():
    from qlab.tui.app import ClaudeWorkforceScreen, QlabTui

    async def run():
        app = QlabTui(
            WorkforceReadyClient(), refresh_interval=0, claude_start="offer"
        )
        async with app.run_test(size=(140, 42)) as pilot:
            for _ in range(20):
                if isinstance(app.screen, ClaudeWorkforceScreen):
                    break
                await pilot.pause(0.05)
            assert isinstance(app.screen, ClaudeWorkforceScreen)
            await pilot.press("escape")
            await pilot.pause(0.05)
            assert not isinstance(app.screen, ClaudeWorkforceScreen)

    asyncio.run(run())


def test_workforce_view_renders_durable_phase_progress():
    from qlab.tui.app import QlabTui

    class WorkflowClient(StubClient):
        def get(self, path, **params):
            snap = _snapshot()
            snap["workflows"] = [{
                "workflow_id": "wf123", "kind": "portfolio_review",
                "status": "running", "current_phase": "challenger",
                "request": {"goal": "review risk", "as_of": "2026-07-19",
                            "universe": "core"},
                "steps": [
                    {"phase": "analyst", "agent": "moments-analyst",
                     "status": "done", "summary": "covariance ready"},
                    {"phase": "challenger", "agent": "challenger",
                     "status": "working", "summary": ""},
                ],
            }]
            return snap

    async def run():
        app = QlabTui(WorkflowClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("3")
            content = str(app.query_one("#workforce-content").content)
            assert app.active_view == "workforce"
            # the compact header still carries the run identity
            assert "wf123" in content
            # per-phase progress now lives on the flowchart, not in a text block
            assert app._flow_states["analyst"] == "done"
            assert app._flow_states["challenger"] == "working"
            # the step summary is revealed on hover, not dumped into the view
            assert "covariance ready" in app._flow_details["analyst"]
            assert str(app.query_one("#flow-analyst").tooltip).find(
                "covariance ready") >= 0

    asyncio.run(run())


class AuditClient(StubClient):
    def get(self, path, **params):
        snap = _snapshot()
        snap["decisions"] = [{
            "decision_id": "dec-pass-1",
            "created_at": "2026-07-17T14:30:00+00:00",
            "kind": "rebalance_gate",
            "choice": {"regime": "calm"},
            "rationale": "within mandate",
            "challenger_view": "turnover is acceptable given the calm regime",
            "reflection": "realized drawdown matched the projection",
            "realized_outcome": {"drawdown": 0.01},
            "verdict": {"verdict": "PASS", "source": "deterministic",
                        "reasons": ["turnover within cap", "weights within mandate"]},
        }]
        snap["system"]["data_source"] = "synthetic"
        snap["system"]["data_age_days"] = 0
        return snap


def test_audit_view_surfaces_verdict_reflection_and_data_token():
    from qlab.tui.app import QlabTui

    async def run():
        app = QlabTui(AuditClient(), refresh_interval=0)
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.pause(0.2)
            table = app.query_one("#audit-table")
            labels = [str(column.label) for column in table.columns.values()]
            assert "verdict" in labels
            assert "reflection" in labels

            row = [str(cell) for cell in table.get_row("dec-pass-1")]
            assert any("PASS" in cell for cell in row)                 # PASS row
            assert any("realized drawdown" in cell for cell in row)    # reflection

            # selected-row detail expands challenger_view + verdict reasons
            # into the work rail; the strip carries the verdict summary
            app._render_audit_detail("dec-pass-1")
            rail = str(app.query_one("#selected-work").content)
            assert "turnover is acceptable" in rail
            assert "turnover within cap" in rail
            assert "verdict PASS" in str(app.query_one("#event-strip").content)

            # status strip carries the one DATA provenance token
            assert "DATA synthetic·0d" in app.query_one("#system-status").content

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
                {"offline": True, "execute": False},
            )]

    asyncio.run(run())


def test_tui_cli_entry_is_registered():
    from qlab.autopilot.cli import build_parser

    args = build_parser().parse_args(["tui", "--refresh", "1.5", "--claude", "auto"])
    assert args.command == "tui"
    assert args.refresh == 1.5
    assert args.claude == "auto"


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

    assert "workflow_rebalance_preview" in app.tools
    assert {"workflow_start", "workflow_status", "workflow_analyst",
            "workflow_challenger", "workflow_optimizer", "workflow_referee",
            "workflow_reporter"} <= set(app.tools)
    assert not any("execute" in name or "order" in name for name in app.tools)
    app.tools["workflow_rebalance_preview"]({"GLD": 1.0}, "decision-1")
    assert client.calls[-1] == (
        "POST", "/api/rebalance_preview",
        {"offline": True, "targets": {"GLD": 1.0}, "decision_id": "decision-1"},
    )


def test_phase_elapsed_labels():
    from qlab.tui.formatting import phase_elapsed

    assert phase_elapsed(None, None) == ""
    assert phase_elapsed("garbage", "2026-07-19T10:00:00") == ""
    assert phase_elapsed(
        "2026-07-19T10:00:00", "2026-07-19T10:00:08") == "8s"
    assert phase_elapsed(
        "2026-07-19T10:00:00", "2026-07-19T10:02:10") == "2m10s"
    assert phase_elapsed(
        "2026-07-19T10:00:00", "2026-07-19T11:05:00") == "1h05m"
    # an open phase measures against now and never goes negative
    assert phase_elapsed("2999-01-01T00:00:00+00:00", None) == "0s"


def test_workforce_view_shows_result_card_and_timings():
    from qlab.tui.app import QlabTui

    class CompleteWorkflowClient(StubClient):
        def get(self, path, **params):
            snap = _snapshot()
            snap["workflows"] = [{
                "workflow_id": "wfdone", "kind": "portfolio_review",
                "status": "complete", "current_phase": "reporter",
                "request": {"goal": "review", "as_of": "2026-07-19",
                            "universe": "core"},
                "result": {"final_summary": "hold reviewed HRP targets"},
                "steps": [
                    {"phase": "referee", "agent": "referee", "status": "done",
                     "summary": "PASS", "started_at": "2026-07-19T10:00:00",
                     "completed_at": "2026-07-19T10:00:42",
                     "artifacts": {"verdict": "PASS"}},
                    {"phase": "reporter", "agent": "reporter", "status": "done",
                     "summary": "", "started_at": "2026-07-19T10:00:42",
                     "completed_at": "2026-07-19T10:01:00",
                     "artifacts": {}},
                ],
            }]
            return snap

    async def run():
        app = QlabTui(CompleteWorkflowClient(), refresh_interval=0,
                      claude_start="off")
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("3")
            content = str(app.query_one("#workforce-content").content)
            assert "RESULT" in content
            assert "referee PASS" in content
            assert "hold reviewed HRP targets" in content
            # phase timing is on the referee node's hover detail, not the block
            assert "42s" in app._flow_details["referee"]
            assert app._flow_states["referee"] == "done"

    asyncio.run(run())


def test_workforce_view_shows_failure_card_with_resume_hint():
    from qlab.tui.app import QlabTui

    class FailedWorkflowClient(StubClient):
        def get(self, path, **params):
            snap = _snapshot()
            snap["workflows"] = [{
                "workflow_id": "wfbad", "kind": "portfolio_review",
                "status": "blocked", "current_phase": "referee",
                "request": {"goal": "review", "as_of": "2026-07-19",
                            "universe": "core"},
                "steps": [
                    {"phase": "referee", "agent": "referee",
                     "status": "blocked", "summary": "no PASS: cap breach"},
                ],
            }]
            return snap

    async def run():
        app = QlabTui(FailedWorkflowClient(), refresh_interval=0,
                      claude_start="off")
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("3")
            content = str(app.query_one("#workforce-content").content)
            assert "BLOCKED at referee" in content
            assert "workforce resume wfbad" in content

    asyncio.run(run())


def test_workforce_mode_advances_flowchart_without_dumping_narrative():
    from qlab.tui.app import QlabTui
    from qlab.tui.claude import ClaudeEvent
    from textual.widgets import RichLog

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("3")  # console renders once the view is visible
            await pilot.pause(0.1)
            console = app.query_one("#workforce-console", RichLog)
            baseline = len(console.lines)

            app.claude.mode = "workforce"
            # coordinator/worker prose is never dumped into the console
            app._apply_claude_event(ClaudeEvent("text_delta", "thinking about"))
            app._apply_claude_event(ClaudeEvent("text_delta", " windows\n"))
            assert len(console.lines) == baseline  # no narrative block

            # a tool call advances the matching flowchart node and logs one line
            app._apply_claude_event(ClaudeEvent(
                "tool_start", "calling", "mcp__qlab-operator__workflow_analyst",
                agent="moments-analyst"))
            assert app._flow_states["analyst"] == "working"
            assert len(console.lines) == baseline + 1

            # a durable bus event adds one compact line
            app._apply_live_event({
                "event_id": "e9", "ts": "2026-07-20T09:00:00+00:00",
                "kind": "referee_verdict", "payload": {"verdict": "PASS"},
            })
            # on completion the run's final results — and only those — print
            app._apply_claude_event(ClaudeEvent(
                "result", "Recommendation: hold HRP targets.\nReferee PASS."))
            rendered = "\n".join(strip.text for strip in console.lines)
            assert "▮ RESULTS" in rendered
            assert "Recommendation: hold HRP targets." in rendered
            assert "Referee PASS." in rendered
            # the streamed narrative was still never dumped
            assert "thinking about windows" not in rendered

    asyncio.run(run())


def test_workforce_chat_sends_resumes_and_stops():
    from qlab.tui.app import QlabTui
    from qlab.tui.claude import ClaudeEvent

    class ClaudeStub:
        def __init__(self):
            self.calls = []
            self.stopped = 0
            self.running = False
            self.mode = "read-only"
            self.available = True

        def start(self, prompt, *, governed=False, resume_session=None,
                  chat=False):
            self.calls.append((prompt, governed, resume_session, chat))
            self.mode = ("workforce" if governed
                         else "chat" if chat else "read-only")
            return True

        def stop(self):
            self.stopped += 1

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            app.claude = ClaudeStub()
            await pilot.press("3")
            await pilot.pause(0.05)

            # first message starts a governed run with the goal
            app._chat_send("review the current risk")
            assert app.claude.calls[-1][0].startswith("GOAL: review the current risk")
            assert app.claude.calls[-1][1] is True
            assert app.claude.calls[-1][2] is None

            # the init event binds the CLI session for multi-turn chat
            app._apply_claude_event(ClaudeEvent(
                "session", "ready", raw={"session_id": "sess-1"}))
            assert app._chat_sessions["workforce"] == "sess-1"

            # later messages resume that session verbatim
            app._chat_send("now challenge the estimation window")
            assert app.claude.calls[-1] == (
                "now challenge the estimation window", True, "sess-1", False)

            # : chat switches to the read-only desk assistant with its own
            # session; nothing about the workforce session leaks into it
            app._handle_command("chat what is my current drawdown")
            assert app.claude.calls[-1] == (
                "what is my current drawdown", False, None, True)
            app._apply_claude_event(ClaudeEvent(
                "session", "ready", raw={"session_id": "chat-9"}))
            assert app._chat_sessions == {
                "workforce": "sess-1", "chat": "chat-9"}
            app._chat_send("and my kill switch distance?")
            assert app.claude.calls[-1] == (
                "and my kill switch distance?", False, "chat-9", True)

            # busy sessions refuse instead of double-starting
            app.claude.running = True
            n = len(app.claude.calls)
            app._chat_send("another")
            assert len(app.claude.calls) == n

            # the button stops a running session, and exits the view when idle
            from textual.widgets import Button
            app.on_button_pressed(
                Button.Pressed(app.query_one("#chat-exit", Button)))
            assert app.claude.stopped == 1
            app.claude.running = False
            app.on_button_pressed(
                Button.Pressed(app.query_one("#chat-exit", Button)))
            assert app.active_view == "desk"

    asyncio.run(run())


def test_chat_mode_argv_is_read_only_desk_assistant():
    from qlab.tui.claude import _chat_agent, build_claude_argv

    argv = build_claude_argv(
        "what is my drawdown?", governed=False, chat=True,
        runtime_url="http://127.0.0.1:9999", offline=True,
    )
    config = json.loads(argv[argv.index("--mcp-config") + 1])
    assert "qlab-operator" in config["mcpServers"]
    allowed = argv[argv.index("--allowedTools") + 1]
    assert "portfolio_state" in allowed and "market_snapshot" in allowed
    # no dispatch, no phase writes, no research writes, no verdicts
    for banned in ("Agent", "workflow_start", "workflow_analyst",
                   "log_decision", "log_verdict", "attach_challenge",
                   "algorithms_solve", "backtest_run"):
        assert banned not in allowed
    assert "--agents" not in argv
    agents = _chat_agent()
    assert set(agents) == {"qlab-desk"}
    assert argv[argv.index("--agent") + 1] == "qlab-desk"
    assert "Agent" not in agents["qlab-desk"]["tools"]


def test_claude_session_uses_resolved_launcher_and_isolated_agents(
    tmp_path, monkeypatch,
):
    from qlab.tui import claude as claude_module

    launched = {}

    class Process:
        def poll(self):
            return None

    class Thread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

    def popen(argv, **kwargs):
        launched["argv"] = argv
        launched.update(kwargs)
        return Process()

    monkeypatch.setattr(
        claude_module.shutil, "which", lambda command: r"C:\Tools\claude.cmd"
    )
    monkeypatch.setattr(claude_module.subprocess, "Popen", popen)
    monkeypatch.setattr(claude_module.threading, "Thread", Thread)
    session = claude_module.ClaudeSession(lambda event: None, cwd=tmp_path)

    assert session.start("inspect", governed=True)
    # On Windows a .cmd launcher is invoked through cmd.exe /c, so the resolved
    # launcher may sit after a shell prefix rather than at argv[0].
    assert r"C:\Tools\claude.cmd" in launched["argv"]
    assert "--agents" not in launched["argv"]
    process_cwd = launched["cwd"]
    assert process_cwd != tmp_path
    assert (process_cwd / ".claude" / "agents" / "qlab-coordinator.md").is_file()


def test_claude_session_reports_process_creation_failure(tmp_path, monkeypatch):
    from qlab.tui import claude as claude_module

    monkeypatch.setattr(
        claude_module.shutil, "which", lambda command: r"C:\Tools\claude.cmd"
    )

    def fail(*args, **kwargs):
        error = OSError(206, "The filename or extension is too long")
        error.winerror = 206
        raise error

    monkeypatch.setattr(claude_module.subprocess, "Popen", fail)
    session = claude_module.ClaudeSession(lambda event: None, cwd=tmp_path)

    assert not session.start("inspect", governed=True)
    assert "WinError 206" in session.last_error
    assert "too long" in session.last_error


def test_resolve_claude_executable_prefers_runnable_launcher_on_windows(monkeypatch):
    # Python 3.12.0's shutil.which returns npm's extensionless shell shim ahead
    # of claude.cmd; CreateProcess rejects that script with WinError 193.
    from qlab.tui import claude as claude_module

    resolved = {
        "claude": r"C:\Users\me\AppData\Roaming\npm\claude",
        "claude.cmd": r"C:\Users\me\AppData\Roaming\npm\claude.cmd",
    }
    monkeypatch.setattr(claude_module.os, "name", "nt")
    monkeypatch.setattr(
        claude_module.shutil, "which", lambda command: resolved.get(command)
    )

    assert (
        claude_module.resolve_claude_executable()
        == r"C:\Users\me\AppData\Roaming\npm\claude.cmd"
    )


def test_audit_row_select_expands_decision_into_work_rail():
    from qlab.tui.app import QlabTui

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            app._audit_decisions["d1"] = {
                "decision_id": "d1", "kind": "estimation_window",
                "as_of": "2026-07-19", "rationale": "stable covariance regime",
                "challenger_view": "shorter window reacts faster",
                "reflection": "realized vol matched",
                "choice": {"window": 504},
                "verdict": {"verdict": "PASS", "reasons": ["within mandate"]},
            }
            app._render_audit_detail("d1")
            rail = str(app.query_one("#selected-work").content)
            assert "DECISION d1" in rail
            assert "stable covariance regime" in rail
            assert "shorter window reacts faster" in rail
            assert "VERDICT  PASS" in rail
            assert "within mandate" in rail
            assert "realized vol matched" in rail

    asyncio.run(run())
