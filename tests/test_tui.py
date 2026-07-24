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
    # Every worker may read its own run's durable record — the referee checks
    # against what was persisted, not against ids retyped into its task, and a
    # garbled hand-off stays recoverable instead of stalling the phase.
    for name, definition in agents.items():
        if name == "qlab-coordinator":
            continue
        assert "mcp__qlab-operator__workflow_status" in definition["tools"], name
        # reading is not writing: no worker may touch another phase's update
        others = {f"mcp__qlab-operator__workflow_{other}"
                  for other in ("analyst", "challenger", "optimizer",
                                "referee", "reporter")}
        assert len(others & set(definition["tools"])) == 1, name


def test_news_tool_reaches_the_regime_roles_and_the_contract_has_five_regimes():
    """The macro-news read is granted to the regime-judgment roles, its Claude
    name matches the sanitized base, and the analyst contract lists five regimes.
    """
    from qlab.tui.claude import (
        _ANALYST_REGIMES, _PHASE_ARTIFACT_CONTRACT, build_workforce_agents)

    assert _ANALYST_REGIMES == (
        "crisis", "stress", "neutral", "calm", "expansion")
    contract = _PHASE_ARTIFACT_CONTRACT["analyst"]
    for regime in _ANALYST_REGIMES:
        assert regime in contract
    assert "regime_summary" in contract          # the news-driven description

    agents = build_workforce_agents()
    news = "mcp__qlab-operator__news_market"      # base news.market -> sanitized
    # only the two roles that actually judge the regime get news access
    assert news in agents["moments-analyst"]["tools"]
    assert news in agents["challenger"]["tools"]
    for role in ("optimization-runner", "referee", "reporter"):
        assert news not in agents[role]["tools"], role

    # analyst prompt: batch the reads for speed, treat headlines as untrusted,
    # and choose on the five-level ladder
    prompt = agents["moments-analyst"]["prompt"]
    assert "ONE turn" in prompt and "news_market" in prompt
    assert "untrusted" in prompt and "FIVE-level" in prompt
    # the analyst-only block must not leak into other roles
    assert "FIVE-level" not in agents["referee"]["prompt"]


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


def test_coordinator_dispatches_synchronously_and_fans_out_in_parallel():
    """The two prompt clauses that decide whether a run finishes at all.

    A backgrounded Agent call strands the coordinator — it holds no tool for
    collecting one — which is exactly how a run hangs on the first worker. The
    parallel clause is the time saving; both are contract, not style.
    """
    from qlab.tui.claude import build_workforce_agents

    coordinator = build_workforce_agents()["qlab-coordinator"]["prompt"]
    assert "run_in_background: false" in coordinator
    assert "SAME turn" in coordinator
    # bounded recovery: one re-dispatch, then stop — never an unbounded loop
    assert "ONCE" in coordinator and "do not loop" in coordinator

    analyst = build_workforce_agents()["moments-analyst"]["prompt"]
    assert "ONE turn" in analyst  # batch independent tool calls
    # bounded retry: a tool error is corrected, never repeated verbatim forever
    assert "Never repeat an identical failing call" in analyst


def test_session_watchdog_kills_a_stalled_run():
    """No run may last forever, whatever the model decides to do."""
    from qlab.tui.claude import ClaudeSession

    class FakeProcess:
        def __init__(self):
            self.terminated = False
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    events = []
    session = ClaudeSession(events.append)

    over_budget = FakeProcess()
    session._watchdog(over_budget, budget_s=0.0, silence_s=0.0)
    assert over_budget.terminated
    assert "no result after" in session._timed_out

    # a session that produces nothing for too long is stalled, not thinking
    session._timed_out = ""
    session._last_event_at = 0.0
    silent = FakeProcess()
    session._watchdog(silent, budget_s=3600.0, silence_s=0.001)
    assert silent.terminated
    assert "silent for" in session._timed_out


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


def test_owner_refusals_reach_the_worker_with_their_reason():
    """An opaque 500 is what turns one bad call into an unbounded retry loop.

    The owner already explains every refusal it raises (a missing artifact, a
    phase whose dependency is not done); httpx's own message throws that away,
    so the worker cannot correct itself and burns its turns repeating the call.
    """
    import httpx
    import pytest as _pytest

    from qlab.mcp.tui_proxy import OwnerRefused, RuntimeClient

    client = RuntimeClient("http://127.0.0.1:1")

    def raising(*_args, **_kwargs):
        request = httpx.Request("POST", "http://127.0.0.1:1/api/workflows/analyst")
        response = httpx.Response(
            500, json={"error": "ValueError(\"phase 'analyst' cannot complete "
                                "without artifacts ['objective_id']\")"},
            request=request)
        raise httpx.HTTPStatusError("boom", request=request, response=response)

    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(httpx, "post", raising)
        with _pytest.raises(OwnerRefused) as refused:
            client.post("/api/workflows/analyst", {})
    assert "objective_id" in str(refused.value)
    assert "/api/workflows/analyst" in str(refused.value)

    # an owner that is gone is terminal, and says so rather than inviting retries
    def unreachable(*_args, **_kwargs):
        raise httpx.ConnectError("connection refused")

    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(httpx, "get", unreachable)
        with _pytest.raises(OwnerRefused) as gone:
            client.get("/api/portfolio")
    assert "unreachable" in str(gone.value)
    assert "retrying will not help" in str(gone.value)


def test_nav_menu_rows_are_clickable():
    """Each 1–5 spine row switches to its view on click, not just Market.

    The row clicked is the click's y within the widget, so this pins the
    mapping as well as the fact that a Static-based menu is now clickable.
    """
    from qlab.tui.app import QlabTui

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.pause(0.2)
            for row, view in enumerate(
                    ("desk", "market", "workforce", "research", "audit")):
                # start elsewhere so each click is a genuine transition
                app.action_view("audit" if view != "audit" else "desk")
                await pilot.pause(0.02)
                await pilot.click("#nav", offset=(3, row))
                await pilot.pause(0.02)
                assert app.active_view == view, (row, app.active_view)

    asyncio.run(run())


def test_braille_chart_is_fixed_size_and_plots_the_trend():
    from qlab.tui.formatting import braille_chart

    # Exact grid: every row is `width` cells, and there are `height` of them —
    # so a caller can drop it into a fixed region without reflow.
    rows = braille_chart(list(range(40)), width=30, height=8)
    assert len(rows) == 8
    assert all(len(row) == 30 for row in rows)

    # A rising series lifts ink from the bottom-left toward the top-right.
    def inked(row: str) -> bool:
        return any(ch != "⠀" for ch in row)
    assert inked(rows[-1])            # bottom carries the low end
    assert inked(rows[0])             # top carries the high end
    top_left = rows[0][: len(rows[0]) // 2]
    assert all(ch == "⠀" for ch in top_left)  # nothing high on the left yet

    # Degenerate inputs are blank, never an exception, and still fixed-size.
    assert braille_chart([], 10, 3) == ["⠀" * 10] * 3
    assert braille_chart([5.0], 10, 3) == ["⠀" * 10] * 3
    flat = braille_chart([2.0] * 20, 12, 4)
    assert len(flat) == 4 and all(len(r) == 12 for r in flat)


def test_market_view_scales_the_chart_to_the_terminal():
    from qlab.tui.app import QlabTui

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(200, 52)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("2")
            await pilot.pause(0.1)
            content = str(app.query_one("#market-content").content)
            # the braille chart is present and spans many rows (a real plot,
            # not a one-line sparkline)
            chart_rows = [ln for ln in content.split("\n")
                          if any(c not in " ⠀" and ord(c) >= 0x2800 and ord(c) <= 0x28ff
                                 for c in ln)]
            assert len(chart_rows) >= 10
            # the readouts still render below it
            assert "portfolio weight" in content and "regime" in content

    asyncio.run(run())


def test_resize_sets_one_layout_tier():
    from qlab.tui.app import QlabTui

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(210, 50)) as pilot:
            await pilot.pause(0.2)
            assert "wide" in app.screen.classes

            # exactly one tier is active at each width; wide/compact/narrow and
            # the unnamed default are mutually exclusive
            for width, expected in ((160, set()), (130, {"compact"}), (95, {"narrow"})):
                await pilot.resize_terminal(width, 40)
                await pilot.pause(0.1)
                tiers = {"wide", "compact", "narrow"} & set(app.screen.classes)
                assert tiers == expected, (width, tiers)

    asyncio.run(run())


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


def test_demojibake_repairs_cp1252_misread_utf8():
    from qlab.tui.formatting import demojibake

    # the em dash the reporter uses, mis-decoded by a cp1252 pipe
    assert demojibake("fragile calm â€” a watch item") == "fragile calm — a watch item"
    assert demojibake("itâ€™s") == "it’s"
    assert demojibake("plain ascii stays put") == "plain ascii stays put"


def test_clean_report_line_strips_markdown_headers_ids_and_emphasis():
    from qlab.tui.formatting import clean_report_line

    # a markdown header returns is_heading=True with the '#' and trailing colon gone
    is_heading, text = clean_report_line(
        "### Uncertainty / watch items (non-blocking)")
    assert is_heading and text == "UNCERTAINTY / WATCH ITEMS (NON-BLOCKING)"

    # internal *_id audit keys and their id-looking values are removed from prose
    _, text = clean_report_line(
        "Handed the optimizer decision_id: dec_a1b2 and objective_id `obj-9`.")
    assert "decision_id" not in text and "objective_id" not in text
    assert "dec_a1b2" not in text and "obj-9" not in text
    assert text == "Handed the optimizer and."

    # markdown emphasis and back-ticks that render literally are stripped
    _, text = clean_report_line("**Result:** held the `checked plan`.")
    assert "*" not in text and "`" not in text
    assert text == "Result: held the checked plan."

    # a plain sentence with no markup is left untouched
    assert clean_report_line("Solved with HRP; the referee passed.") == (
        False, "Solved with HRP; the referee passed.")


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


def test_workforce_view_shows_the_selected_regime_and_reasoning():
    """The analyst's regime call and why it made it get their own TUI line."""
    from qlab.tui.app import QlabTui

    reason = ("Turbulence and absorption both stressed (95th pct); "
              "shortened the window to 252d.")

    class RegimeClient(StubClient):
        def get(self, path, **params):
            snap = _snapshot()
            snap["workflows"] = [{
                "workflow_id": "wfreg", "kind": "portfolio_review",
                "status": "running", "current_phase": "challenger",
                "request": {"goal": "review", "as_of": "2026-07-19",
                            "universe": "core"},
                "steps": [
                    {"phase": "analyst", "agent": "moments-analyst",
                     "status": "done", "summary": "252d window",
                     "artifacts": {"regime": "stress",
                                   "regime_reasoning": reason,
                                   "regime_summary": (
                                       "Rate-hike fears and a growth scare are "
                                       "driving cross-asset de-risking.")}},
                ],
            }]
            return snap

    async def run():
        app = QlabTui(RegimeClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("3")
            content = str(app.query_one("#workforce-content").content)
            assert "REGIME" in content and "STRESS" in content
            assert "Turbulence and absorption" in content
            # the news backdrop that drove the regime is shown too
            assert "news backdrop" in content
            assert "growth scare" in content

    asyncio.run(run())


def test_regime_line_covers_the_five_level_ladder_and_news():
    """Each of the five regimes gets its own colour, and the news summary is
    appended as a second line — pure, so the mapping is pinned without an app."""
    from qlab.tui.app import _REGIME_TONE, _regime_line

    assert set(_REGIME_TONE) == {
        "crisis", "stress", "neutral", "calm", "expansion"}

    steps = {"analyst": {"artifacts": {
        "regime": "expansion",
        "regime_reasoning": "All indicators benign; low turbulence.",
        "regime_summary": "Cooling inflation and easing bets lift risk assets.",
    }}}
    line = _regime_line(steps)
    assert "EXPANSION" in line
    assert _REGIME_TONE["expansion"] in line       # its own heat-scale colour
    assert "news backdrop" in line
    assert "Cooling inflation" in line

    # no analyst artifacts → no regime line, never an exception
    assert _regime_line({}) is None
    assert _regime_line({"analyst": {"artifacts": {}}}) is None


def test_new_run_clears_the_previous_run_from_the_flowchart_immediately():
    """A starting run must never wear the last run's finished nodes.

    The durable row for a new run only exists once the coordinator calls
    workflow.start; until then the snapshot still returns the previous run, so
    the view has to refuse it rather than repaint completed phases.
    """
    from qlab.tui.app import QlabTui

    previous = {
        "workflow_id": "wfold", "kind": "portfolio_review",
        "status": "complete", "current_phase": "reporter",
        "request": {"goal": "old run", "as_of": "2026-07-19", "universe": "core"},
        "result": {"final_summary": "old conclusion"},
        "steps": [{"phase": phase, "agent": agent, "status": "done",
                   "summary": f"{phase} finished"}
                  for phase, agent in (
                      ("analyst", "moments-analyst"), ("challenger", "challenger"),
                      ("optimizer", "optimization-runner"), ("referee", "referee"),
                      ("reporter", "reporter"))],
    }
    fresh = {
        "workflow_id": "wfnew", "kind": "portfolio_review",
        "status": "running", "current_phase": "analyst",
        "request": {"goal": "new run", "as_of": "2026-07-20", "universe": "core"},
        "steps": [{"phase": "analyst", "agent": "moments-analyst",
                   "status": "working", "summary": ""}],
    }

    class RollingClient(StubClient):
        workflows = [previous]

        def get(self, path, **params):
            snap = _snapshot()
            snap["workflows"] = list(self.workflows)
            return snap

    async def run():
        client = RollingClient()
        app = QlabTui(client, refresh_interval=0, claude_start="off")
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("3")
            assert app._flow_states["reporter"] == "done"  # showing the old run

            # the operator starts a new run; the coordinator has not yet
            # registered a workflow, so the snapshot still returns only wfold
            app._bind_run()
            assert app._flow_states == {
                phase: "queued" for phase in
                ("analyst", "challenger", "optimizer", "referee", "reporter")}
            assert "old conclusion" not in str(
                app.query_one("#workforce-content").content)

            # a stale snapshot arriving mid-launch must not resurrect it either
            app._apply_snapshot(client.get("/api/tui"))
            assert app._flow_states["reporter"] == "queued"
            assert app._active_workflow_id == ""

            # once the new run registers, the view adopts it — and only it
            client.workflows = [fresh, previous]
            app._apply_snapshot(client.get("/api/tui"))
            assert app._active_workflow_id == "wfnew"
            assert app._flow_states["analyst"] == "working"
            assert app._flow_states["reporter"] == "queued"
            content = str(app.query_one("#workforce-content").content)
            assert "wfnew" in content and "old conclusion" not in content

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

            # further tool traffic inside the same phase stays silent
            app._apply_claude_event(ClaudeEvent(
                "tool_start", "calling", "mcp__qlab-operator__moments_estimate",
                agent="moments-analyst"))
            assert len(console.lines) == baseline + 1

            # bus traffic that is not a phase transition never reaches the console
            app._apply_live_event({
                "event_id": "e9", "ts": "2026-07-20T09:00:00+00:00",
                "kind": "tool_call", "payload": {"tool": "moments.estimate"},
            })
            assert len(console.lines) == baseline + 1

            # on completion the run's final results — and only those — print
            app._apply_claude_event(ClaudeEvent(
                "result", "Recommendation: hold HRP targets.\nReferee PASS."))
            rendered = "\n".join(strip.text for strip in console.lines)
            assert "Recommendation: hold HRP targets." in rendered
            assert "Referee PASS." in rendered
            # the streamed narrative was still never dumped
            assert "thinking about windows" not in rendered

    asyncio.run(run())


def test_results_fallback_cleans_markdown_mojibake_and_ids():
    """With no durable workflow, the coordinator's closing text is the fallback,
    and it still reaches the console as plain, readable prose."""
    from qlab.tui.app import QlabTui
    from qlab.tui.claude import ClaudeEvent
    from textual.widgets import RichLog

    messy = (
        "Recommendation: hold the HRP targets.\n"
        "### Uncertainty / watch items (non-blocking)\n"
        "Vol-term-structure is closest to its flip â€” a fragile calm.\n"
        "Logged under decision_id: dec_a1b2 with the checked plan ready."
    )

    async def run():
        # StubClient's default snapshot carries no workflow → fallback path.
        app = QlabTui(StubClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("3")
            await pilot.pause(0.1)
            app.claude.mode = "workforce"
            app._apply_claude_event(ClaudeEvent("result", messy))
            rendered = "\n".join(
                strip.text for strip in app.query_one(
                    "#workforce-console", RichLog).lines)

            assert "###" not in rendered            # no literal markdown header
            assert "â€”" not in rendered and "—" in rendered   # mojibake repaired
            assert "decision_id" not in rendered and "dec_a1b2" not in rendered
            assert "UNCERTAINTY / WATCH ITEMS (NON-BLOCKING)" in rendered
            assert "Recommendation: hold the HRP targets." in rendered

    asyncio.run(run())


def test_completed_run_prints_friendly_structured_summary():
    """A completed run summarizes from the durable record — a plain-language
    banner, the regime, one line per agent, the recommendation, and its meaning —
    and never dumps the coordinator's raw model text."""
    from qlab.tui.app import QlabTui
    from qlab.tui.claude import ClaudeEvent
    from textual.widgets import RichLog

    targets = {"GLD": 0.22, "ACWI": 0.18, "BNDW": 0.16, "EMB": 0.14,
               "IGF": 0.12, "VNQ": 0.10, "GSG": 0.08}

    class CompleteClient(StubClient):
        def get(self, path, **params):
            snap = _snapshot()
            snap["workflows"] = [{
                "workflow_id": "wfdone", "kind": "portfolio_review",
                "status": "complete", "current_phase": "reporter",
                "request": {"goal": "review", "as_of": "2026-07-23",
                            "universe": "core"},
                "steps": [
                    {"phase": "analyst", "status": "done",
                     "artifacts": {"regime": "stress",
                                   "regime_reasoning": "Turbulence stressed; window 252d."}},
                    {"phase": "challenger", "status": "done"},
                    {"phase": "optimizer", "status": "done",
                     "artifacts": {"algorithm_id": "HRP", "targets": targets}},
                    {"phase": "referee", "status": "done", "summary": "PASS",
                     "artifacts": {"verdict": "PASS", "targets": targets}},
                    {"phase": "reporter", "status": "done",
                     "artifacts": {"plan_id": "plan-77"}},
                ],
            }]
            return snap

    async def run():
        app = QlabTui(CompleteClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(150, 46)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("3")
            await pilot.pause(0.1)
            app._active_workflow_id = "wfdone"
            app.claude.mode = "workforce"
            # a raw, markdown/id-laden coordinator note that must NOT be dumped
            app._apply_claude_event(ClaudeEvent(
                "result", "### Notes\nLogged decision_id: dec_9 â€” watch vol."))
            # collapse the RichLog's visual word-wrap so substring checks are
            # about content, not where the terminal broke a line
            flat = " ".join(" ".join(
                strip.text for strip in app.query_one(
                    "#workforce-console", RichLog).lines).split())

            assert "WORKFORCE COMPLETE" in flat
            assert "referee-approved recommendation" in flat
            assert "STRESS" in flat                       # regime call
            # every agent gets a plain-language line
            assert "WHAT EACH AGENT DID" in flat
            for name in ("Analyst", "Challenger", "Optimizer", "Referee", "Reporter"):
                assert name in flat
            assert "using HRP" in flat and "approved it" in flat
            # the final output and the one human action
            assert "GLD 22.0%" in flat
            assert ": rebalance paper" in flat
            assert "WHAT THIS MEANS" in flat
            assert "confirm the paper trade yourself" in flat
            # the raw coordinator note is NOT dumped — no markdown, ids, mojibake
            assert "###" not in flat
            assert "decision_id" not in flat and "dec_9" not in flat
            assert "â€”" not in flat
            # and no leftover terminal-centric section from the old format
            assert "PIPELINE" not in flat

    asyncio.run(run())


def test_blocked_run_summary_names_the_gate_and_what_it_means():
    """A blocked run says where it stopped, why, and that nothing traded."""
    from qlab.tui.app import QlabTui
    from qlab.tui.claude import ClaudeEvent
    from textual.widgets import RichLog

    class BlockedClient(StubClient):
        def get(self, path, **params):
            snap = _snapshot()
            snap["workflows"] = [{
                "workflow_id": "wfblk", "kind": "portfolio_review",
                "status": "blocked", "current_phase": "referee",
                "request": {"goal": "review", "as_of": "2026-07-23",
                            "universe": "core"},
                "steps": [
                    {"phase": "analyst", "status": "done",
                     "artifacts": {"regime": "calm",
                                   "regime_reasoning": "All indicators calm."}},
                    {"phase": "optimizer", "status": "done",
                     "artifacts": {"algorithm_id": "HRP"}},
                    {"phase": "referee", "status": "blocked",
                     "summary": "no PASS: single-name cap breach on GLD"},
                ],
            }]
            return snap

    async def run():
        app = QlabTui(BlockedClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(150, 46)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("3")
            await pilot.pause(0.1)
            app._active_workflow_id = "wfblk"
            app.claude.mode = "workforce"
            app._apply_claude_event(ClaudeEvent("result", ""))
            flat = " ".join(" ".join(
                strip.text for strip in app.query_one(
                    "#workforce-console", RichLog).lines).split())

            assert "STOPPED AT A SAFETY GATE" in flat
            assert "cap breach on GLD" in flat          # why, cleaned
            assert "did not run" in flat                # reporter never ran
            assert "Nothing was traded" in flat or "nothing has traded" in flat

    asyncio.run(run())


def test_completed_agent_prints_one_note_with_what_is_next():
    """Each finished agent earns a two-line account; nothing else is printed."""
    from qlab.tui.app import QlabTui
    from textual.widgets import RichLog

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("3")
            await pilot.pause(0.1)
            written: list[str] = []
            app._console_write = written.append  # count entries, not wrapped rows
            app._bind_run()  # only a run this session launched narrates itself

            phase_done = {
                "event_id": "p1", "ts": "2026-07-20T09:00:00+00:00",
                "kind": "workflow_phase",
                "payload": {"workflow_id": "wf1", "phase": "analyst",
                            "status": "done", "summary": "756d window, LW shrinkage"},
            }
            app._apply_live_event(phase_done)
            assert len(written) == 2
            rendered = "\n".join(written)
            assert "analyst done" in rendered
            assert "756d window, LW shrinkage" in rendered
            # the parallel stage is announced, because that is what happens next
            assert "in parallel" in rendered

            # a replayed event (SSE reconnect primer) never double-prints
            app._apply_live_event(dict(phase_done))
            assert len(written) == 2

    asyncio.run(run())


def test_a_stopped_session_still_reports_where_the_run_reached():
    """A watchdog stop or a crashed CLI owes the operator the durable state."""
    from qlab.tui.app import QlabTui
    from qlab.tui.claude import ClaudeEvent

    class PartialRunClient(StubClient):
        def get(self, path, **params):
            snap = _snapshot()
            snap["workflows"] = [{
                "workflow_id": "wfhalf", "kind": "portfolio_review",
                "status": "running", "current_phase": "optimizer",
                "request": {"goal": "review", "as_of": "2026-07-20",
                            "universe": "core"},
                "steps": [
                    {"phase": "analyst", "agent": "moments-analyst",
                     "status": "done", "summary": "756d window"},
                    {"phase": "optimizer", "agent": "optimization-runner",
                     "status": "working", "summary": ""},
                ],
            }]
            return snap

    async def run():
        app = QlabTui(PartialRunClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("3")
            written: list[str] = []
            app._console_write = written.append
            app._active_workflow_id = "wfhalf"
            app.claude.mode = "workforce"

            app._apply_claude_event(ClaudeEvent(
                "error", "session stopped by the qlab watchdog: no result after "
                         "30 minutes."))
            rendered = "\n".join(written)
            assert "watchdog" in rendered
            assert "ENDED EARLY" in rendered
            assert "Analyst" in rendered                    # the phase that finished
            assert "still running when the run stopped" in rendered  # where it stopped

    asyncio.run(run())


def test_workforce_note_follows_the_dependency_graph():
    from qlab.tui.app import workforce_note

    head, nxt = workforce_note("analyst", "done", "window chosen", {"analyst"})
    assert head.startswith("analyst done")
    assert "in parallel" in nxt

    # the first of the parallel pair waits for the other, not for the referee
    _, nxt = workforce_note("optimizer", "done", "", {"analyst", "optimizer"})
    assert "challenger" in nxt
    _, nxt = workforce_note(
        "optimizer", "done", "", {"analyst", "challenger", "optimizer"})
    assert "referee" in nxt

    head, nxt = workforce_note("referee", "blocked", "cap breach", {"analyst"})
    assert "blocked" in head and "cap breach" in head
    assert "Nothing was traded" in nxt


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
