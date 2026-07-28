"""Quiet-workstation TUI contracts and headless layout behavior."""

from __future__ import annotations

import asyncio
import json
import re
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
            "positions": {
                "ACWI": {"qty": 40.4, "price": 100.0, "value": 4_040.0,
                         "unrealized_pl": 40.0},
                "BNDW": {"qty": 30.0, "price": 101.0, "value": 3_030.0,
                         "unrealized_pl": -15.0},
                "GLD": {"qty": 29.7, "price": 102.0, "value": 3_029.4,
                        "unrealized_pl": 29.4},
            },
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
        "stress": {
            "drawdown_tier": "none",
            "drawdown_thresholds": {
                "warning": 0.05,
                "control": 0.10,
                "breaker": 0.15,
            },
            "gross_exposure": 1.0,
            "max_gross_exposure": 1.0,
            "leverage_headroom": 0.0,
            "stressed_vol": 0.10,
            "stress_vol_limit": 0.30,
            "replays": {
                label: {
                    "available": False,
                    "start": start,
                    "end": end,
                    "return": None,
                    "reason": (
                        "unavailable: synthetic snapshot is not historical "
                        "replay data"
                    ),
                }
                for label, start, end in (
                    ("2008", "2008-09-01", "2009-03-09"),
                    ("2020", "2020-02-19", "2020-03-23"),
                    ("2022", "2022-01-03", "2022-10-12"),
                )
            },
            "cost_gate_refusals": [],
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
            "autopilot": {"last_run_at": None, "triggers_fired": 0},
        },
        "algorithms": [
            {"id": "hrp", "stage": "operational"},
            {"id": "mvsk_multistart", "stage": "research"},
            {"id": "dirac3_mvsk", "stage": "research"},
            {"id": "qaoa_selection", "stage": "offline"},
        ],
        "policy": {"id": "hrp", "label": "Hierarchical risk parity"},
        "performance": {
            "series": [
                {"ts": f"2026-06-{day:02d}", "equity": 10_000.0 * (1.001 ** day)}
                for day in range(1, 31)
            ],
            "metrics": {"ann_return": 0.041, "ann_vol": 0.082, "sharpe": 0.50,
                        "sortino": 0.71, "max_drawdown": -0.021,
                        "cvar_95": -0.006, "realized_skew": -0.2,
                        "realized_kurtosis": 0.8, "deflated_sharpe": 0.6,
                        "n_obs": 29},
            "since_start": 0.0124,
            # Measured across `series` — the window the chart actually draws.
            "window_change": 0.0294,
            "cadence": {"periods_per_year": 365.25, "observed_span_days": 29.0,
                        "mean_step_days": 1.0, "basis": "observed mark cadence"},
            "note": None, "marks": 30, "marks_total": 30, "mark_limit": 5000,
            "marks_capped": False, "excluded_marks": 0,
            "book": "simulated_paper",
        },
        "equilibrium_returns": {
            "run_id": "eq-run-1",
            "as_of": "2026-07-17",
            "portfolio": {"mu": 0.034, "lo": -0.012, "hi": 0.067},
            "caveats": {
                "interpretation": "equilibrium prior, not a forecast",
                "uncertainty": "bands are parameter uncertainty",
            },
        },
        "workflows": [],
        "leaderboard": [
            {"arm_id": "B2", "name": "HRP", "champion": True, "benchmark": False,
             "sharpe": 0.91, "ann_return": 0.062, "max_drawdown": -0.124,
             "cvar_95": -0.011, "deflated_sharpe": 0.83},
            {"arm_id": "B0", "name": "60/40", "champion": False, "benchmark": True,
             "sharpe": 0.55, "ann_return": 0.050, "max_drawdown": -0.180,
             "cvar_95": -0.015, "deflated_sharpe": 0.60},
            {"arm_id": "B3", "name": "Risk Parity", "champion": False,
             "benchmark": False, "sharpe": 0.74, "ann_return": 0.048,
             "max_drawdown": -0.150, "cvar_95": -0.013, "deflated_sharpe": 0.71},
            {"arm_id": "B4", "name": "Min Variance", "champion": False,
             "benchmark": False, "sharpe": None, "ann_return": None,
             "max_drawdown": None, "cvar_95": None, "deflated_sharpe": None},
        ],
    }


def _bootstrap():
    return {
        "mandate": {
            "paper_capital": 10_000.0,
            "whitelist": ["ACWI", "BNDW", "GSG", "IGF", "GLD", "VNQ", "EMB"],
            "max_weight_per_asset": 0.40,
            "max_turnover_per_rebalance": 0.50,
            "trailing_drawdown_pct": 0.15,
            "cadence": "quarterly",
            "order_type": "marketable_limit",
            "operational_policy": "hrp",
        },
        "universe": {"core": [], "candidates": [], "selection_k": 0},
        "agents": [
            {"name": row["name"], "tools": []}
            for row in _snapshot()["agents"]
        ],
    }


def _reference():
    """Owner-shaped reference payload, built from the real curated catalog.

    Deriving the fixture from ``REFERENCE_ENTRIES`` means the stub cannot drift
    away from the content the owner actually serves.
    """
    from dataclasses import asdict

    from qlab.core.reference import REFERENCE_ENTRIES

    # A full compute_metrics bundle, the shape the owner really serves: 13 keys,
    # n_obs first, and a None where a metric could not be computed.
    metrics = {
        "n_obs": 504,
        "ann_return": 0.0732,
        "ann_vol": 0.0805,
        "sharpe": 0.91,
        "sortino": 1.243,
        "downside_deviation": 0.0589,
        "omega_ratio": 1.412,
        "max_drawdown": -0.124,
        "cvar_95": -0.0187,
        "realized_skew": -0.312,
        "realized_kurtosis": 4.118,
        "deflated_sharpe": 0.634,
        "turnover": None,
    }

    entries = []
    for entry in REFERENCE_ENTRIES:
        row = asdict(entry)
        row["stage"] = "operational" if entry.algorithm_key == "hrp" else None
        row["champion"] = entry.algorithm_key == "hrp"
        row["ablation"] = dict(metrics) if entry.arm_id == "B2" else None
        entries.append(row)
    return {"entries": entries, "champion_policy": "hrp"}


class StubClient:
    def __init__(self):
        self.posts = []

    def get(self, path, **params):
        if path == "/api/tui":
            return _snapshot()
        if path == "/api/bootstrap":
            return _bootstrap()
        if path == "/api/reference":
            return _reference()
        raise AssertionError(path)

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
    assert snapshot["stress"]["drawdown_tier"] in {
        "none", "warning", "control", "breaker",
    }
    assert {row["stage"] for row in snapshot["algorithms"]} == {
        "operational", "research", "offline"
    }


def test_owner_stress_payload_surfaces_latest_cost_gate_refusal():
    client = InProcessClient()
    client.session.registry.record_event(
        "cost_gate_refusal",
        {
            "plan_id": "refused-plan",
            "reasons": ["net-alpha gate did not clear expected cost"],
        },
    )

    snapshot = client.get("/api/tui", offline=True)

    refusal = snapshot["stress"]["cost_gate_refusals"][0]
    assert refusal["plan_id"] == "refused-plan"
    assert refusal["reasons"] == ["net-alpha gate did not clear expected cost"]


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
        "qlab-coordinator", "data-qa", "signal-qa",
        "moments-analyst", "challenger",
        "optimization-runner", "referee", "reporter",
    }
    assert agents["qlab-coordinator"]["tools"] == [
        "Agent(data-qa,signal-qa,moments-analyst,challenger,"
        "optimization-runner,referee,reporter)",
        "mcp__qlab-operator__workflow_start",
        "mcp__qlab-operator__workflow_status",
        "mcp__qlab-operator__workflow_phase",
    ]
    all_role_tools = {
        tool for name, definition in agents.items() if name != "qlab-coordinator"
        for tool in definition["tools"]
    }
    assert not ({"Read", "Write", "Edit", "Bash"} & all_role_tools)
    assert not any("execute" in tool or "order" in tool for tool in all_role_tools)
    # Every gated worker may read its own run's durable record — the referee checks
    # against what was persisted, not against ids retyped into its task, and a
    # garbled hand-off stays recoverable instead of stalling the phase.
    gated = {
        "moments-analyst", "challenger", "optimization-runner",
        "referee", "reporter",
    }
    for name in gated:
        definition = agents[name]
        assert "mcp__qlab-operator__workflow_status" in definition["tools"], name
        # The named tools stay role-specific. Only roles used for dynamic panel
        # phases receive the generic route; their prompt binds it to the exact
        # branch while the registry enforces dependencies and artifacts.
        others = {f"mcp__qlab-operator__workflow_{other}"
                  for other in ("analyst", "challenger", "optimizer",
                                "referee", "reporter")}
        assert len(others & set(definition["tools"])) == 1, name
        generic = "mcp__qlab-operator__workflow_phase"
        if name in {"moments-analyst", "optimization-runner", "referee"}:
            assert generic in definition["tools"]
            assert "Update only the assigned phase" in definition["prompt"]
        else:
            assert generic not in definition["tools"]

    for name in {"data-qa", "signal-qa"}:
        definition = agents[name]
        assert not any(
            "__workflow_" in tool for tool in definition["tools"]
        ), name
        assert "mcp__qlab-operator__registry_log_decision" in definition["tools"]
        assert "sole permitted" in definition["prompt"]
        for banned in (
            "registry_log_verdict", "algorithms_solve", "solve_classical",
            "backtest_run", "workflow_phase",
        ):
            assert not any(banned in tool for tool in definition["tools"]), (
                name, banned,
            )


def test_workforce_agents_route_models_with_source_override_precedence(monkeypatch):
    from dataclasses import replace

    import qlab.agents.loader as loader
    from qlab.tui import claude

    expected = {
        "moments-analyst": "inherit",
        "challenger": "inherit",
        "referee": "inherit",
        "optimization-runner": "sonnet",
        "reporter": "sonnet",
        "data-qa": "sonnet",
        "signal-qa": "sonnet",
    }
    assert claude._ROLE_MODEL == expected

    agents = claude.build_workforce_agents()
    assert {name: agents[name]["model"] for name in expected} == expected

    source_agents = loader.load_agents()
    overridden = [
        replace(source, model="opus")
        if source.name == "optimization-runner" else source
        for source in source_agents
    ]
    monkeypatch.setattr(loader, "load_agents", lambda: overridden)
    routed = claude.build_workforce_agents()
    # A concrete model in the agent source overrides the routing table.
    assert routed["optimization-runner"]["model"] == "opus"
    assert routed["reporter"]["model"] == "sonnet"


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
        "qlab-coordinator", "data-qa", "signal-qa",
        "moments-analyst", "challenger",
        "optimization-runner", "referee", "reporter",
    }
    coordinator = tmp_path / ".claude" / "agents" / "qlab-coordinator.md"
    _, front, body = coordinator.read_text(encoding="utf-8").split("---", 2)
    metadata = yaml.safe_load(front)
    assert metadata["tools"].split(", ") == [
        "Agent(data-qa,signal-qa,moments-analyst,challenger,"
        "optimization-runner,referee,reporter)",
        "mcp__qlab-operator__workflow_start",
        "mcp__qlab-operator__workflow_status",
        "mcp__qlab-operator__workflow_phase",
    ]
    assert metadata["permissionMode"] == "dontAsk"
    assert "no filesystem, shell, browser, editing, or trading tools" in body
    for name in {
        "optimization-runner", "reporter", "data-qa", "signal-qa",
    }:
        path = tmp_path / ".claude" / "agents" / f"{name}.md"
        _, front, _ = path.read_text(encoding="utf-8").split("---", 2)
        assert yaml.safe_load(front)["model"] == "sonnet"
    for name in {"moments-analyst", "challenger", "referee"}:
        path = tmp_path / ".claude" / "agents" / f"{name}.md"
        _, front, _ = path.read_text(encoding="utf-8").split("---", 2)
        assert "model" not in yaml.safe_load(front)


def test_news_scoped_session_materializes_quarantined_extractor(tmp_path):
    """A news goal may opt into the extractor without widening its authority."""
    from qlab.tui.claude import build_workforce_agents, write_session_agents

    written = write_session_agents(
        tmp_path,
        build_workforce_agents("review the grounded market news"),
    )
    assert "news-extractor" in {path.stem for path in written}

    extractor = (
        tmp_path / ".claude" / "agents" / "news-extractor.md"
    ).read_text(encoding="utf-8")
    _, front, body = extractor.split("---", 2)
    metadata = yaml.safe_load(front)
    assert metadata["tools"] == "mcp__qlab-operator__research_apply_views"
    assert "workflow" not in metadata["tools"]
    assert "execute" not in metadata["tools"]
    assert "OWNER-WORKFORCE QUARANTINE MODE" in body


def test_coordinator_dispatches_synchronously_with_bounded_debate_and_panel_fanout():
    """Prompt clauses that bound standard review and keep panels parallel.

    A backgrounded Agent call strands the coordinator — it holds no tool for
    collecting one — which is exactly how a run hangs on the first worker.
    """
    from qlab.tui.claude import build_workforce_agents

    coordinator = build_workforce_agents()["qlab-coordinator"]["prompt"]
    assert "run_in_background: false" in coordinator
    assert "IN PARALLEL" in coordinator and "ONE message" in coordinator
    assert "kind='panel'" in coordinator
    assert "analyst-1" in coordinator and "optimizer-1" in coordinator
    assert "exact workflow phase 'judge'" in coordinator
    assert "walk-forward evidence" in coordinator
    assert "Optionally dispatch data-qa as the FIRST Agent" in coordinator
    assert "before a panel workflow" in coordinator
    assert "Pass its exact clean flag" in coordinator
    assert "dispatch signal-qa after the analyst" in coordinator
    assert "Neither QA role updates or completes a workflow phase" in coordinator
    assert "one focused counter-case" in coordinator
    assert "DEBATE_FOLLOW_UP" in coordinator
    assert "NEW decision record" in coordinator
    assert "never edit the old decision" in coordinator
    assert "maximum of two challenger↔analyst exchanges" in coordinator
    assert "never dispatch a third" in coordinator
    assert "which argument carried and why" in coordinator
    assert "coordinator prompt policy only" in coordinator
    assert "never target weights, orders, trades" in coordinator
    assert coordinator.index("challenger alone") < coordinator.index(
        "optimization-runner uses the analyst's final decision"
    )
    assert "Panel branches use evidence adjudication" in coordinator
    # bounded recovery: one re-dispatch, then stop — never an unbounded loop
    assert "ONCE" in coordinator and "do not loop" in coordinator

    analyst = build_workforce_agents()["moments-analyst"]["prompt"]
    assert "ONE turn" in analyst  # batch independent tool calls
    # bounded retry: a tool error is corrected, never repeated verbatim forever
    assert "Never repeat an identical failing call" in analyst
    assert "Do not call workflow phase" in analyst

    challenger = build_workforce_agents()["challenger"]["prompt"]
    assert "one rebuttal" in challenger
    assert "Do not call workflow phase" in challenger

    referee = build_workforce_agents()["referee"]["prompt"]
    assert "which argument carried and why" in referee


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


def test_process_group_options_and_tree_stop_cover_windows_and_posix(monkeypatch):
    from qlab.tui import claude as claude_module

    monkeypatch.setattr(claude_module.os, "name", "posix")
    assert claude_module._process_group_options() == {
        "start_new_session": True,
    }

    calls = []

    class PosixProcess:
        pid = 421

        def poll(self):
            return None

        def wait(self, timeout=None):
            return -15

    def killpg(pid, sig):
        calls.append((pid, sig))
        if sig == 0:
            raise ProcessLookupError

    monkeypatch.setattr(claude_module.os, "killpg", killpg)
    claude_module._terminate_process_tree(PosixProcess(), grace_s=0.1)
    assert calls[0] == (421, claude_module.signal.SIGTERM)
    assert all(sig != claude_module.signal.SIGKILL for _pid, sig in calls)

    monkeypatch.setattr(claude_module.os, "name", "nt")
    assert "creationflags" in claude_module._process_group_options()
    taskkill = []

    class WindowsProcess:
        pid = 422

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 1

        def kill(self):
            raise AssertionError("taskkill should stop the process tree")

    monkeypatch.setattr(
        claude_module.subprocess,
        "run",
        lambda argv, **kwargs: taskkill.append((argv, kwargs)),
    )
    claude_module._terminate_process_tree(WindowsProcess(), grace_s=0.1)
    assert taskkill[0][0] == [
        "taskkill", "/PID", "422", "/T", "/F",
    ]


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
            assert app.active_view == "atlas"

            await pilot.press("3")
            await pilot.press("j")
            assert app.active_view == "market"
            assert app.active_ticker == "BNDW"

            await pilot.press("2")
            assert app.active_view == "dashboard"

            await pilot.press("f6")
            assert app.active_view == "book"

            await pilot.press("7")
            assert app.active_view == "audit"

            await pilot.press("~")
            assert app.query_one("#timeline").styles.display == "block"

    asyncio.run(run())


def test_owner_system_status_reads_latest_autopilot_event():
    client = InProcessClient()
    client.session.registry.record_event(
        "autopilot_trigger",
        {"kind": "drift", "detail": {}},
    )
    client.session.registry.record_event(
        "daily_ops",
        {
            "triggers": [
                {"kind": "drift", "detail": {}},
                {"kind": "regime", "detail": {}},
            ],
        },
    )

    status = client.get("/api/system", offline=True)
    assert status["autopilot"]["last_run_at"]
    assert status["autopilot"]["triggers_fired"] == 2


def test_status_strip_shows_autopilot_last_run_and_trigger_count():
    from qlab.tui.app import QlabTui

    class AutopilotClient(StubClient):
        def get(self, path, **params):
            snapshot = super().get(path, **params)
            if path == "/api/tui":
                snapshot["system"]["autopilot"] = {
                    "last_run_at": "2026-07-24T16:30:00+00:00",
                    "triggers_fired": 2,
                }
            return snapshot

    async def run():
        app = QlabTui(AutopilotClient(), refresh_interval=0)
        async with app.run_test(size=(160, 42)) as pilot:
            await pilot.pause(0.2)
            status = str(app.query_one("#system-status").content)
            assert "AUTO 07-24 16:30·2" in status

    asyncio.run(run())


def test_owner_failure_surfaces_in_conn_chip():
    from qlab.tui.app import QlabTui

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0)
        async with app.run_test(size=(160, 42)) as pilot:
            await pilot.pause(0.2)
            # Drive the failure path directly rather than through real
            # background-thread polling: three consecutive failures is the
            # documented threshold, not a timing accident.
            app._note_refresh_failure()
            app._note_refresh_failure()
            app._note_refresh_failure()
            chip = str(app.query_one("#conn-chip").content)
            assert "OWNER DOWN" in chip

    asyncio.run(run())


def test_view_switches_release_workforce_focus():
    from qlab.tui.app import QlabTui

    async def run():
        client = StubClient()
        app = QlabTui(client, refresh_interval=0, claude_start="off")
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)

            await pilot.press("4")
            assert app.active_view == "workforce"
            assert app.focused is app.query_one("#chat-input")

            await pilot.press("f2")
            assert app.active_view == "dashboard"
            assert app.focused is None
            posts_before_enter = list(client.posts)
            await pilot.press("enter")
            assert client.posts == posts_before_enter

            await pilot.press("4")
            assert app.focused is app.query_one("#chat-input")
            await pilot.press("escape")
            assert app.focused is None
            await pilot.press("7")
            assert app.active_view == "audit"

    asyncio.run(run())


def test_dashboard_renders_all_tiles_and_latest_verdict():
    from qlab.tui.app import QlabTui

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            tile_ids = {
                "tile-equity",
                "tile-allocation",
                "tile-regime",
                "tile-market-pulse",
                "tile-verdict",
                "tile-run",
                "tile-alerts",
                "tile-stress",
            }
            assert {tile.id for tile in app.query(".dashboard-tile")} == tile_ids
            equity = str(app.query_one("#tile-equity-content").content)
            assert "$10,100.00" in equity
            assert "• EQUITY" in equity and "• KILL-SWITCH DISTANCE" in equity
            assert "ACWI" in str(
                app.query_one("#tile-allocation-content").content)
            regime = str(app.query_one("#tile-regime-content").content)
            assert "CALM" in regime and "• REGIME" in regime
            assert "• EQ RETURN (1Y)" in regime
            assert "-1.2%–6.7%" in regime
            assert "3.4%" not in regime  # never render the bare equilibrium point
            assert "ACWI" in str(
                app.query_one("#tile-market-pulse-content").content)
            empty_verdict = str(
                app.query_one("#tile-verdict-content").content)
            assert "—" in empty_verdict and "no verdicts yet" in empty_verdict
            assert "no runs" in str(app.query_one("#tile-run-content").content)
            alerts = str(app.query_one("#tile-alerts-content").content)
            assert "• DRAWDOWN TIER" in alerts and "NONE" in alerts
            assert "• LEVERAGE HEADROOM" in alerts
            assert "• STRESSED VOL / LIMIT" in alerts
            assert "clear · no recent refusals" in alerts
            replays = str(app.query_one("#tile-stress-content").content)
            assert "• 2008 REPLAY" in replays
            assert "unavailable (synthetic)" in replays

            app.snapshot["decisions"] = [{
                "decision_id": "decision-pass",
                "rationale": (
                    "**within mandate** decision_id: decision-hidden"
                ),
                "verdict": {"verdict": "PASS"},
            }]
            app._render_dashboard()
            verdict = str(app.query_one("#tile-verdict-content").content)
            assert "PASS" in verdict
            assert "• within mandate" in verdict
            assert "**" not in verdict
            assert "decision_id" not in verdict
            assert "decision-hidden" not in verdict

            app.action_view("market")
            app._handle_command("view desk")
            assert app.active_view == "dashboard"

    asyncio.run(run())


def test_dashboard_stress_alerts_tile_renders_all_drawdown_tiers():
    from qlab.tui.app import QlabTui

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            for tier_name in ("warning", "control", "breaker"):
                app.snapshot["stress"]["drawdown_tier"] = tier_name
                app._render_dashboard()
                content = str(app.query_one("#tile-alerts-content").content)
                assert tier_name.upper() in content

            app.snapshot["stress"].update({
                "leverage_headroom": -0.05,
                "gross_exposure": 1.05,
                "stressed_vol": 0.35,
                "cost_gate_refusals": [{
                    "ts": "2026-07-24T12:00:00+00:00",
                    "plan_id": "plan-refused",
                    "reasons": ["net-alpha gate did not clear expected cost"],
                }],
            })
            app._render_dashboard()
            content = str(app.query_one("#tile-alerts-content").content)
            assert "• LEVERAGE HEADROOM" in content and "-5.0%" in content
            assert "35.0% / 30.0%" in content
            assert "REFUSED · net-alpha gate did not clear expected cost" in content
            assert "• 2022 REPLAY" in str(
                app.query_one("#tile-stress-content").content
            )

    asyncio.run(run())


def test_dashboard_regime_tile_renders_hmm_posterior_and_uncertain_state():
    from qlab.tui.app import QlabTui

    snapshot = _snapshot()
    snapshot["market"]["regime"].update({
        "posterior": {"calm": 0.62, "normal": 0.30, "stress": 0.08},
        "robust_state": "uncertain",
        "confidence": 0.62,
        "effective_risk_fraction": 0.5,
    })

    class PosteriorClient(StubClient):
        def get(self, path, **params):
            if path == "/api/tui":
                return snapshot
            return super().get(path, **params)

    async def run():
        app = QlabTui(
            PosteriorClient(),
            refresh_interval=0,
            claude_start="off",
        )
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            regime = str(app.query_one("#tile-regime-content").content)
            assert "calm 62" in regime
            assert "normal 30" in regime
            assert "stress 8" in regime
            assert "UNCERTAIN" in regime

    asyncio.run(run())


def test_quote_event_repaints_only_market_pulse_and_universe():
    from qlab.tui.app import QlabTui
    from textual.widgets import Label, ListView

    async def run():
        client = StubClient()
        app = QlabTui(client, refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            other_tiles = {
                tile_key: str(app.query_one(
                    f"#tile-{tile_key}-content"
                ).content)
                for tile_key in (
                    "equity", "allocation", "regime", "verdict", "run", "alerts",
                    "stress",
                )
            }
            posts_before = list(client.posts)
            refreshes = []
            app._start_refresh = lambda: refreshes.append("refresh")

            app._apply_live_event({
                "event_id": "quote-1",
                "ts": "2026-07-24T12:00:00+00:00",
                "kind": "quote",
                "payload": {"rows": [{
                    "ticker": "ACWI",
                    "price": 123.45,
                    "change_1d": -0.0123,
                }]},
            })

            pulse = str(
                app.query_one("#tile-market-pulse-content").content
            )
            universe = app.query_one("#universe", ListView)
            first_label = universe.children[0].query_one(Label)
            assert "ACWI" in pulse and "123.45" in pulse and "-1.2%" in pulse
            assert "123.45" in str(first_label.content)
            assert refreshes == []
            assert client.posts == posts_before
            assert {
                tile_key: str(app.query_one(
                    f"#tile-{tile_key}-content"
                ).content)
                for tile_key in other_tiles
            } == other_tiles

            app._apply_live_event({
                "event_id": "quote-2",
                "ts": "2026-07-24T12:00:00.100000+00:00",
                "kind": "quote",
                "payload": {"rows": [{
                    "ticker": "ACWI",
                    "price": 130.0,
                    "change_1d": 0.02,
                }]},
            })
            assert "130.00" not in str(
                app.query_one("#tile-market-pulse-content").content
            )
            await pilot.pause(1.05)
            assert "130.00" in str(
                app.query_one("#tile-market-pulse-content").content
            )
            assert refreshes == []
            assert client.posts == posts_before

    asyncio.run(run())


def test_dashboard_sparse_payload_updates_every_tile():
    from qlab.tui.app import QlabTui

    tile_keys = {
        "equity",
        "allocation",
        "regime",
        "market-pulse",
        "verdict",
        "run",
        "alerts",
        "stress",
    }

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            for tile_key in tile_keys:
                app.query_one(
                    f"#tile-{tile_key}-content"
                ).update(f"stale {tile_key}")

            app.snapshot = {
                "portfolio": {},
                "market": {
                    "regime": {},
                    "assets": [{
                        "ticker": "ACWI",
                        "history": [None],
                    }],
                },
                "decisions": [None],
                "workflows": [{
                    "workflow_id": "wf-sparse",
                    "steps": [None],
                }],
            }
            app._render_dashboard()

            contents = {
                tile_key: str(app.query_one(
                    f"#tile-{tile_key}-content"
                ).content)
                for tile_key in tile_keys
            }
            assert all("stale" not in content for content in contents.values())
            assert "—" in contents["equity"]
            assert "—" in contents["allocation"]
            assert "—" in contents["regime"]
            assert "—" in contents["market-pulse"]
            assert "no verdicts yet" in contents["verdict"]
            assert "wf-sparse" in contents["run"]
            assert "—" in contents["run"]
            assert "no alerts" in contents["alerts"]
            assert "no replay data" in contents["stress"]

    asyncio.run(run())


def test_dashboard_refresh_replaces_full_snapshot_with_unavailable_state():
    from qlab.tui.app import QlabTui

    tile_keys = {
        "equity",
        "allocation",
        "regime",
        "market-pulse",
        "verdict",
        "run",
        "alerts",
        "stress",
    }

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            app._apply_snapshot(_snapshot())
            assert "$10,100.00" in str(
                app.query_one("#tile-equity-content").content)
            assert "ACWI" in str(
                app.query_one("#tile-market-pulse-content").content)

            app._apply_snapshot({})

            contents = {
                tile_key: str(app.query_one(
                    f"#tile-{tile_key}-content"
                ).content)
                for tile_key in tile_keys
            }
            assert all(
                "owner snapshot unavailable" in content
                for content in contents.values()
            )
            assert all(
                "$10,100.00" not in content and "ACWI" not in content
                for content in contents.values()
            )

    asyncio.run(run())


def test_later_snapshots_repaint_only_the_visible_canvas():
    from qlab.tui.app import QlabTui

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            app._apply_snapshot(_snapshot())
            app.active_view = "market"
            calls = {"market": 0, "dashboard": 0, "workforce": 0}
            app._render_market = lambda: calls.__setitem__(
                "market", calls["market"] + 1)
            app._render_dashboard = lambda: calls.__setitem__(
                "dashboard", calls["dashboard"] + 1)
            app._render_workforce = lambda: calls.__setitem__(
                "workforce", calls["workforce"] + 1)

            app._apply_snapshot(_snapshot())

            assert calls == {"market": 1, "dashboard": 0, "workforce": 0}

    asyncio.run(run())


class WorkforceReadyClient(StubClient):
    def get(self, path, **params):
        snap = _snapshot()
        snap["system"]["workforce_available"] = True
        snap["system"]["mcp_proxy_available"] = True
        return snap


def test_tui_offer_mode_never_pushes_a_startup_modal():
    from qlab.tui.app import QlabTui

    async def run():
        app = QlabTui(
            WorkforceReadyClient(), refresh_interval=0, claude_start="offer"
        )
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            assert app.snapshot
            assert len(app.screen_stack) == 1
            assert app.screen is app.screen_stack[0]
            assert "CLAUDE READY" in app.query_one("#system-status").content

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
            await pilot.press("4")
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


def test_workforce_view_renders_panel_branches_and_judge_from_steps():
    from qlab.tui.app import FlowNode, QlabTui

    class PanelWorkflowClient(StubClient):
        def get(self, path, **params):
            snap = _snapshot()
            snap["workflows"] = [{
                "workflow_id": "wf-panel",
                "kind": "panel",
                "status": "running",
                "current_phase": "analyst-2",
                "request": {
                    "goal": "compare estimator variants",
                    "as_of": "2026-07-24",
                    "universe": "core",
                    "variants": [
                        {"label": "responsive", "window": 252},
                        {"label": "stable", "window": 756},
                    ],
                },
                # Registry order is dependency-oriented. The board should pair
                # each analyst chip with its matching optimizer chip.
                "steps": [
                    {"phase": "analyst-1", "agent": "moments-analyst",
                     "status": "done", "summary": "responsive estimate"},
                    {"phase": "analyst-2", "agent": "moments-analyst",
                     "status": "working", "summary": "stable estimate"},
                    {"phase": "optimizer-1", "agent": "optimization-runner",
                     "status": "queued", "summary": ""},
                    {"phase": "optimizer-2", "agent": "optimization-runner",
                     "status": "queued", "summary": ""},
                    {"phase": "judge", "agent": "referee",
                     "status": "queued", "summary": ""},
                    {"phase": "referee", "agent": "referee",
                     "status": "queued", "summary": ""},
                    {"phase": "reporter", "agent": "reporter",
                     "status": "queued", "summary": ""},
                ],
            }]
            return snap

    async def run():
        app = QlabTui(
            PanelWorkflowClient(), refresh_interval=0, claude_start="off",
        )
        async with app.run_test(size=(160, 42)) as pilot:
            await pilot.pause(0.4)
            await pilot.press("4")
            await pilot.pause(0.1)

            nodes = list(app.query(FlowNode))
            assert [node.phase for node in nodes] == [
                "analyst-1", "optimizer-1",
                "analyst-2", "optimizer-2",
                "judge", "referee", "reporter",
            ]
            assert nodes[0].short == "v1 analyst"
            assert nodes[1].short == "v1 optimizer"
            assert app.query_one("#flow-analyst-2", FlowNode).phase == "analyst-2"
            assert app.query_one("#flow-judge", FlowNode).phase == "judge"
            assert app._flow_states["analyst-1"] == "done"
            assert app._flow_states["analyst-2"] == "working"
            assert "challenger" not in app._flow_states

    asyncio.run(run())


class BookClient(StubClient):
    def get(self, path, **params):
        if path == "/api/bootstrap":
            return super().get(path, **params)
        snap = _snapshot()
        snap["plans"] = [
            {
                "plan_id": "plan-checked-newest",
                "state": "checked",
                "decision_id": "decision-newest",
                "pre_trade": {"turnover": 0.125},
                "created_at": "2026-07-24T12:30:00+00:00",
            },
            {
                "plan_id": "plan-proposed-older",
                "state": "proposed",
                "decision_id": "decision-older",
                "pre_trade": {"turnover": 0.075},
                "created_at": "2026-07-23T09:15:00+00:00",
            },
        ]
        snap["orders"] = [{
            "client_order_id": "order-1",
            "plan_id": "plan-checked-newest",
            "ticker": "ACWI",
            "side": "buy",
            "notional": 1_250.0,
            "state": "filled",
            "created_at": "2026-07-24T12:35:00+00:00",
        }]
        return snap


def test_book_view_renders_positions_plan_cards_and_specific_execute_flow():
    from textual.widgets import Button

    from qlab.tui.app import PaperConfirmScreen, QlabTui

    async def run():
        app = QlabTui(BookClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 54)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("f6")
            assert app.active_view == "book"

            positions = str(app.query_one("#book-positions").content)
            checked = str(app.query_one("#book-plan-copy-0").content)
            proposed = str(app.query_one("#book-plan-copy-1").content)
            orders = str(app.query_one("#book-orders").content)
            assert "ACWI" in positions and "40.0%" in positions
            assert "plan-checked-newest" in checked
            assert "CHECKED" in checked and "12.5%" in checked
            assert "plan-proposed-older" in proposed
            assert "BUY" in orders and "ACWI" in orders and "$1,250.00" in orders

            execute = app.query_one("#execute-plan-0", Button)
            unavailable = app.query_one("#execute-plan-1", Button)
            assert str(execute.label) == "execute"
            assert not execute.disabled
            assert unavailable.disabled

            await pilot.click("#execute-plan-0")
            await pilot.pause(0.05)
            assert isinstance(app.screen, PaperConfirmScreen)
            assert app.screen.plan_id == "plan-checked-newest"
            assert app._pending_plan_id == "plan-checked-newest"
            await pilot.click("#cancel-paper")

    asyncio.run(run())


def test_book_renders_equity_curve_metrics_and_position_pnl():
    from qlab.tui.app import QlabTui

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("6")
            equity_panel = str(app.query_one("#book-equity").content)
            assert "sharpe" in equity_panel and "0.50" in equity_panel
            # The headline is the live broker equity and says so; the percentage
            # is measured over the charted window, from its first mark's date.
            assert "$10,100.00" in equity_panel and "live" in equity_panel
            assert "+2.9% since 2026-06-01" in equity_panel
            assert "since start" not in equity_panel
            # The annualization convention is disclosed, never assumed.
            assert "365/yr" in equity_panel and "observed" in equity_panel
            positions = str(app.query_one("#book-positions").content)
            assert "P&L" in positions
            assert "$40.00" in positions and "$-15.00" in positions

    asyncio.run(run())


def test_book_equity_discloses_a_capped_history_and_a_foreign_book():
    """A capped window must not read as the whole book, nor mix two books."""
    from qlab.tui.app import QlabTui

    class CappedClient(StubClient):
        def get(self, path, **params):
            snapshot = super().get(path, **params)
            if path == "/api/tui":
                snapshot["performance"] = {
                    **snapshot["performance"],
                    "marks": 5000, "marks_total": 7321, "marks_capped": True,
                    "excluded_marks": 42,
                    "note": "42 mark(s) from another book excluded; this series "
                            "is simulated_paper only; history capped at the "
                            "newest 5000 marks of 7321",
                }
            return snapshot

    async def run():
        app = QlabTui(CappedClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("6")
            panel = str(app.query_one("#book-equity").content)
            assert "5,000 of 7,321 marks" in panel
            assert "capped" in panel
            assert "another book excluded" in panel

    asyncio.run(run())


def test_book_is_honest_when_no_equity_history():
    from qlab.tui.app import QlabTui

    class NoHistoryClient(StubClient):
        def get(self, path, **params):
            snapshot = super().get(path, **params)
            if path == "/api/tui":
                snapshot["performance"] = {
                    "series": [], "metrics": None, "since_start": None,
                    "note": "no equity history yet", "marks": 0}
            return snapshot

    async def run():
        app = QlabTui(NoHistoryClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("6")
            panel = str(app.query_one("#book-equity").content)
            assert "No equity history yet" in panel
            assert "daily ops" in panel

    asyncio.run(run())


def test_book_equity_reports_note_when_metrics_are_unavailable():
    """A short mark history is a contractual state: note, never NaN cells."""
    from qlab.tui.app import QlabTui

    class ThinHistoryClient(StubClient):
        def get(self, path, **params):
            snapshot = super().get(path, **params)
            if path == "/api/tui":
                snapshot["performance"] = {
                    "series": [{"ts": "2026-07-23", "equity": 10_000.0},
                               {"ts": "2026-07-24", "equity": 10_100.0}],
                    "metrics": None, "since_start": 0.01,
                    "note": "insufficient history for realized metrics "
                            "(need >=4 daily marks)",
                    "marks": 2}
                for position in snapshot["portfolio"]["positions"].values():
                    position.pop("unrealized_pl")
            return snapshot

    async def run():
        app = QlabTui(ThinHistoryClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("6")
            panel = str(app.query_one("#book-equity").content)
            assert "insufficient history for realized metrics" in panel
            assert "nan" not in panel.lower()
            positions = str(app.query_one("#book-positions").content)
            # No unrealized P&L on the position: an em dash, never a zero.
            assert "$40.00" not in positions and "—" in positions

    asyncio.run(run())


class AuditClient(StubClient):
    def get(self, path, **params):
        if path == "/api/bootstrap":
            return super().get(path, **params)
        snap = _snapshot()
        snap["decisions"] = [{
            "decision_id": "dec-pass-1",
            "created_at": "2026-07-17T14:30:00+00:00",
            "kind": "rebalance_gate",
            "choice": {"regime": "calm"},
            "rationale": (
                "**within mandate** decision_id: decision-hidden"
            ),
            "challenger_view": (
                "`turnover` is acceptable given the calm regime"
            ),
            "reflection": "realized drawdown matched the projection",
            "realized_outcome": {"drawdown": 0.01},
            "verdict": {"verdict": "PASS", "source": "deterministic",
                        "reasons": [
                            "turnover within cap objective_id: obj-hidden",
                            "weights within mandate",
                        ]},
        }]
        snap["plans"] = [{
            "plan_id": "plan-must-not-appear",
            "state": "checked",
            "decision_id": "dec-pass-1",
            "pre_trade": {"turnover": 0.1},
            "created_at": "2026-07-17T14:31:00+00:00",
        }]
        snap["orders"] = [{
            "client_order_id": "order-must-not-appear",
            "ticker": "ACWI",
            "side": "buy",
            "notional": 500.0,
            "state": "filled",
            "created_at": "2026-07-17T14:32:00+00:00",
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
            assert table.row_count == 1

            row = [str(cell) for cell in table.get_row("dec-pass-1")]
            assert any("PASS" in cell for cell in row)                 # PASS row
            assert any("realized drawdown" in cell for cell in row)    # reflection
            assert not any("plan-must-not-appear" in cell for cell in row)
            assert "plans and orders are in Book" in str(
                app.query_one("#audit-summary").content)

            # selected-row detail expands challenger_view + verdict reasons
            # into the work rail; the strip carries the verdict summary
            app._render_audit_detail("dec-pass-1")
            rail = str(app.query_one("#selected-work").content)
            assert "turnover is acceptable" in rail
            assert "turnover within cap" in rail
            assert "• within mandate" in rail
            assert "**" not in rail and "`" not in rail
            assert "decision_id: decision-hidden" in rail
            assert "objective_id: obj-hidden" in rail
            assert "verdict PASS" in str(app.query_one("#event-strip").content)

            # status strip carries the one DATA provenance token
            assert "DATA synthetic·0d" in app.query_one("#system-status").content

    asyncio.run(run())


def test_dry_rebalance_button_routes_through_owner_api():
    from qlab.tui.app import QlabTui

    async def run():
        client = StubClient()
        app = QlabTui(client, refresh_interval=0)
        async with app.run_test(size=(140, 60)) as pilot:
            await pilot.pause(0.1)
            # The desk opens on Atlas now; the dashboard action lives one view over.
            app.action_view("dashboard")
            await pilot.pause(0.05)
            await pilot.click("#btn-rebalance-dry")
            for _ in range(20):
                if client.posts:
                    break
                await pilot.pause(0.05)
            assert client.posts == [(
                "/api/run_once",
                {"offline": True, "execute": False},
            )]

    asyncio.run(run())


def test_command_table_covers_help_and_preserves_owner_action_routes():
    from qlab.tui.app import COMMAND_TABLE, QlabTui

    async def run():
        client = StubClient()
        app = QlabTui(client, refresh_interval=0)
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause(0.1)
            app.action_help()
            help_text = str(app.query_one("#selected-work").content)
            command_lines = help_text.split("\n\n", 2)[1].splitlines()
            help_keys = set()
            for line in command_lines:
                words = line.split()
                command = words[0]
                if len(words) == 1:
                    help_keys.add((command, None))
                elif "|" in words[1]:
                    help_keys.update(
                        (command, subword) for subword in words[1].split("|")
                    )
                elif words[1].isupper():
                    help_keys.add((command, None))
                else:
                    help_keys.add((command, words[1]))
            assert help_keys
            assert help_keys <= set(COMMAND_TABLE)

            app._handle_command("daily")
            for _ in range(20):
                if client.posts and not app._action_running:
                    break
                await pilot.pause(0.05)
            assert client.posts == [
                ("/api/daily_ops", {"offline": True}),
            ]

            app._handle_command("rebalance dry")
            for _ in range(20):
                if len(client.posts) == 2:
                    break
                await pilot.pause(0.05)
            assert client.posts == [
                ("/api/daily_ops", {"offline": True}),
                ("/api/run_once", {"offline": True, "execute": False}),
            ]

    asyncio.run(run())


def test_tui_cli_entry_is_registered():
    from qlab.autopilot.cli import build_parser

    args = build_parser().parse_args(["tui", "--refresh", "1.5", "--claude", "auto"])
    assert args.command == "tui"
    assert args.refresh == 1.5
    assert args.claude == "auto"


def test_operator_mcp_proxy_is_propose_only_and_never_executes():
    from qlab.mcp.tui_proxy import register_proxy_tools
    from qlab.tui.claude import _LAB_TOOL_BASES

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
    assert {"workflow_start", "workflow_status", "workflow_phase",
            "workflow_analyst",
            "workflow_challenger", "workflow_optimizer", "workflow_referee",
            "workflow_reporter"} <= set(app.tools)
    assert {
        base.replace(".", "_") for base in _LAB_TOOL_BASES
    } <= set(app.tools)
    assert not any("execute" in name or "order" in name for name in app.tools)
    app.tools["workflow_rebalance_preview"]({"GLD": 1.0}, "decision-1")
    assert client.calls[-1] == (
        "POST", "/api/rebalance_preview",
        {"offline": True, "targets": {"GLD": 1.0}, "decision_id": "decision-1"},
    )
    variants = [
        {"label": "responsive", "window": 252},
        {"label": "stable", "window": 756},
    ]
    app.tools["workflow_start"](
        "compare windows", kind="panel", variants=variants,
    )
    assert client.calls[-1] == (
        "POST", "/api/workflows/start",
        {
            "goal": "compare windows", "as_of": "", "universe": "core",
            "kind": "panel", "offline": True, "variants": variants,
        },
    )
    app.tools["workflow_phase"](
        "analyst-2",
        "workflow-panel",
        "working",
        "estimating stable branch",
    )
    assert client.calls[-1] == (
        "POST", "/api/workflows/analyst-2",
        {
            "workflow_id": "workflow-panel", "status": "working",
            "summary": "estimating stable branch", "artifacts": {},
        },
    )
    app.tools["research_window_evidence"](
        "2026-07-17", cadence="annual",
    )
    assert client.calls[-1] == (
        "POST", "/api/lab/research.window_evidence",
        {
            "as_of": "2026-07-17", "universe": "core",
            "cadence": "annual", "offline": True,
        },
    )
    app.tools["qa_data_integrity"](
        "2026-07-17", lookback_days=504,
    )
    assert client.calls[-1] == (
        "POST", "/api/lab/qa.data_integrity",
        {
            "as_of": "2026-07-17", "universe": "core",
            "lookback_days": 504, "offline": True,
        },
    )
    app.tools["research_equilibrium_returns"](
        "2026-07-17", lookback_days=504,
    )
    assert client.calls[-1] == (
        "POST", "/api/lab/research.equilibrium_returns",
        {
            "as_of": "2026-07-17", "universe": "core",
            "lookback_days": 504, "offline": True,
        },
    )
    app.tools["research_predict_vol"](
        "2026-07-17", lookback_days=504,
    )
    assert client.calls[-1] == (
        "POST", "/api/lab/research.predict_vol",
        {
            "as_of": "2026-07-17", "universe": "core",
            "lookback_days": 504, "offline": True,
        },
    )


def test_research_view_renders_latest_prediction_admission_with_tone():
    from qlab.tui.app import QlabTui
    from qlab.tui.theme import DOWN, UP

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.2)
            app.snapshot["runs"] = [
                {
                    "run_id": "prediction-new",
                    "kind": "prediction",
                    "created_at": "2026-07-17T12:00:00+00:00",
                    "spec": {
                        "mean_ic": 0.0412,
                        "ic_stability": 0.81,
                        "usable": True,
                    },
                },
                {
                    "run_id": "prediction-old",
                    "kind": "prediction",
                    "created_at": "2026-07-16T12:00:00+00:00",
                    "spec": {
                        "mean_ic": -0.2,
                        "ic_stability": -1.0,
                        "usable": False,
                    },
                },
            ]
            app._render_research()
            summary = str(app.query_one("#research-summary").content)
            expected = "vol forecast IC 0.041 (stable) — usable"
            assert expected in summary
            assert f"[{UP}]{expected}[/]" in summary

            app.snapshot["runs"][0]["spec"] = {
                "mean_ic": 0.029,
                "ic_stability": 0.72,
                # The view must independently enforce the strict IC threshold
                # before displaying a persisted result as usable.
                "usable": True,
            }
            app._render_research()
            summary = str(app.query_one("#research-summary").content)
            expected = "vol forecast IC 0.029 (stable) — not usable"
            assert expected in summary
            assert f"[{DOWN}]{expected}[/]" in summary

    asyncio.run(run())


def test_research_leaderboard_shows_method_names_not_codes():
    from qlab.tui.app import QlabTui

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0)
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("5")
            board = str(app.query_one("#leaderboard").content)
            assert "HRP" in board and "60/40" in board
            assert "★" in board            # champion marked
            assert "BENCH" in board        # benchmark tagged
            assert "B2" not in board       # codes never rendered here

    asyncio.run(run())


def test_research_leaderboard_columns_align_on_visible_width():
    """Markup tags occupy no cells, so a field width must pad the plain text.

    Champion (``★``), benchmark (``BENCH``) and unmarked rows carry different
    amounts of markup; padding the tagged string lands the metric block at a
    different offset in every row and the board stops being readable.
    """
    from qlab.tui.app import QlabTui

    columns = ["SHARPE", "RET", "MAXDD", "CVAR95", "DSR"]
    cells = {
        "HRP": ["0.91", "+6.2%", "-12.4%", "-1.10%", "0.83"],
        "60/40": ["0.55", "+5.0%", "-18.0%", "-1.50%", "0.60"],
        "Risk Parity": ["0.74", "+4.8%", "-15.0%", "-1.30%", "0.71"],
    }

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0)
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("5")
            board = str(app.query_one("#leaderboard").content)
            plain = re.sub(r"\[[^\]]*\]", "", board).splitlines()
            header, rows = plain[0], plain[1:]

            def row(name):
                matches = [line for line in rows if line.startswith(name)]
                assert len(matches) == 1, f"{name!r} in {rows}"
                return matches[0]

            edges = [header.index(token) + len(token) for token in columns]
            starts = []
            for name, values in cells.items():
                line = row(name)
                for token, value, edge in zip(columns, values, edges):
                    assert line.index(value) + len(value) == edge, (
                        f"{name} {token} off its column: {line!r}")
                starts.append(line.index(values[0]))
            assert len(set(starts)) == 1, f"metric block offsets differ: {starts}"

            absent = row("Min Variance")
            assert [m.end() for m in re.finditer("—", absent)] == edges, absent

    asyncio.run(run())


def test_research_leaderboard_renders_absent_metrics_as_em_dash():
    """An unscored arm must read as absent — never blank, never ``nan``."""
    from qlab.tui.app import QlabTui

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0)
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("5")
            board = str(app.query_one("#leaderboard").content)
            plain = re.sub(r"\[[^\]]*\]", "", board).splitlines()
            absent = [line for line in plain if line.startswith("Min Variance")]
            assert len(absent) == 1, plain
            assert absent[0].count("—") == 5, absent[0]
            assert "nan" not in absent[0].lower(), absent[0]
            assert "None" not in absent[0], absent[0]

    asyncio.run(run())


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


def test_settings_view_fetches_bootstrap_once_and_renders_read_only_bulletins():
    from qlab.tui.app import QlabTui

    class SettingsClient(StubClient):
        def __init__(self):
            super().__init__()
            self.bootstrap_calls = 0

        def get(self, path, **params):
            if path == "/api/bootstrap":
                self.bootstrap_calls += 1
            return super().get(path, **params)

    async def run():
        client = SettingsClient()
        app = QlabTui(client, refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("9")
            for _ in range(20):
                if app.bootstrap is not None:
                    break
                await pilot.pause(0.02)

            mandate = str(app.query_one("#settings-mandate").content)
            data = str(app.query_one("#settings-data").content)
            agents = str(app.query_one("#settings-agents").content)
            theme = str(app.query_one("#settings-theme").content)
            assert app.active_view == "settings"
            assert "$10,000.00" in mandate
            assert "40.0%" in mandate and "50.0%" in mandate
            assert "15.0%" in mandate and "hrp" in mandate
            assert "SYNTHETIC" in data and "2026-07-17" in data
            assert "moments-analyst" in agents and "RESEARCH" in agents
            assert "qlab amber phosphor" in theme and "amber" in theme
            assert "• paper capital" in mandate
            assert "• snapshot source" in data
            assert "• moments-analyst" in agents
            assert "• palette" in theme

            await pilot.press("2")
            await pilot.press("f9")
            await pilot.pause(0.05)
            assert client.bootstrap_calls == 1

    asyncio.run(run())


def test_settings_bootstrap_error_is_capped_with_an_explicit_ellipsis():
    from qlab.tui.app import QlabTui

    tail = "tail-must-not-render"
    detail = "owner bootstrap failed: " + ("x" * 700) + tail

    class FailingSettingsClient(StubClient):
        def get(self, path, **params):
            if path == "/api/bootstrap":
                raise RuntimeError(detail)
            return super().get(path, **params)

    async def run():
        app = QlabTui(
            FailingSettingsClient(),
            refresh_interval=0,
            claude_start="off",
        )
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("9")
            for _ in range(20):
                if app._bootstrap_error:
                    break
                await pilot.pause(0.02)

            mandate = app.query_one("#settings-mandate")
            rendered = mandate.render().plain
            error_line = next(
                line for line in rendered.splitlines()
                if "RuntimeError" in line
            )
            assert "OWNER UNREACHABLE" in rendered
            assert error_line.endswith("…")
            assert len(error_line.removeprefix("• ")) == 600
            assert tail not in rendered

    asyncio.run(run())


def test_reference_view_leads_with_method_names_and_marks_champion():
    from textual.widgets import Label

    from qlab.tui.app import QlabTui

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("8")
            assert app.active_view == "reference"
            await pilot.pause(0.3)
            list_text = "\n".join(
                str(item.query_one(Label).content)
                for item in app.query_one("#reference-list").children)
            detail = str(app.query_one("#reference-detail").content)
            # First arm renders in the detail pane by default.
            assert "60/40" in detail
            # Champion is starred in the list without exposing the arm code.
            assert "★" in list_text and "HRP" in list_text
            assert "B2" not in list_text
            # Codes appear only as the dim footnote, never in titles.
            assert "ablation id: B0" in detail

    asyncio.run(run())


def test_reference_detail_states_absent_ablation_and_shows_champion_numbers():
    from qlab.tui.app import QlabTui

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("8")
            await pilot.pause(0.3)
            # 60/40 has no ablation row in the fixture: say so, never blank.
            benchmark_detail = str(app.query_one("#reference-detail").content)
            assert "no ablation recorded" in benchmark_detail

            # The index has focus on arrival, so arrows walk the catalog and
            # the detail pane follows the highlight.
            assert app.focused is app.query_one("#reference-list")
            await pilot.press("down")
            await pilot.pause(0.1)
            assert "Equal-weight benchmark" in str(
                app.query_one("#reference-detail").content)

            # Third arm is HRP, the champion in this fixture.
            await pilot.press("down")
            await pilot.pause(0.1)
            detail = str(app.query_one("#reference-detail").content)
            assert "Hierarchical risk parity" in detail
            assert "★ CHAMPION" in detail
            assert "operational" in detail
            assert "ablation id: B2" in detail
            # The overlay is a curated five in a fixed reading order, not an
            # alphabetical dump of the whole metric bundle.
            assert "sharpe" in detail and "0.910" in detail
            assert "deflated_sharpe" in detail and "0.634" in detail
            assert "n_obs" not in detail
            assert "omega_ratio" not in detail
            assert "realized_kurtosis" not in detail
            overlay = next(
                line for line in detail.splitlines()
                if "latest ablation" in line)
            curated = (
                "sharpe", "ann_return", "max_drawdown", "cvar_95",
                "deflated_sharpe")
            # `]name[` matches the label markup only, so deflated_sharpe cannot
            # be mistaken for sharpe.
            positions = [overlay.index(f"]{name}[") for name in curated]
            assert positions == sorted(positions), overlay

            # A focused list must not swallow view navigation.
            await pilot.press("2")
            assert app.active_view == "dashboard"

    asyncio.run(run())


def test_reference_ablation_without_curated_numbers_reads_as_absent():
    from qlab.tui.app import QlabTui

    class ShortWindowClient(StubClient):
        def get(self, path, **params):
            payload = super().get(path, **params)
            if path == "/api/reference":
                for row in payload["entries"]:
                    if row["ablation"]:
                        # compute_metrics returns n_obs alone on a short series,
                        # and a degenerate run can leave a non-finite behind.
                        row["ablation"] = {
                            "n_obs": 2,
                            "sharpe": float("nan"),
                            "ann_return": float("inf"),
                        }
            return payload

    async def run():
        app = QlabTui(
            ShortWindowClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("8")
            await pilot.pause(0.3)
            await pilot.press("down")
            await pilot.press("down")
            await pilot.pause(0.1)
            detail = str(app.query_one("#reference-detail").content)
            assert "Hierarchical risk parity" in detail
            # No finite curated metric is honest absence, not a bare header.
            assert "no ablation recorded" in detail
            assert "latest ablation" not in detail
            assert "nan" not in detail.lower() and "inf" not in detail.lower()

    asyncio.run(run())


def test_reference_fetch_failure_is_visible_and_retried_on_the_next_visit():
    from qlab.tui.app import QlabTui

    class FailingReferenceClient(StubClient):
        def __init__(self):
            super().__init__()
            self.reference_calls = 0
            self.fail = True

        def get(self, path, **params):
            if path == "/api/reference":
                self.reference_calls += 1
                if self.fail:
                    raise RuntimeError("owner reference unavailable")
            return super().get(path, **params)

    async def run():
        client = FailingReferenceClient()
        app = QlabTui(client, refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("8")
            for _ in range(40):
                if "unavailable" in str(app.query_one("#reference-detail").content):
                    break
                await pilot.pause(0.02)
            assert "reference unavailable" in str(app.query_one("#reference-detail").content)

            client.fail = False
            await pilot.press("2")
            await pilot.press("8")
            for _ in range(40):
                if "60/40" in str(app.query_one("#reference-detail").content):
                    break
                await pilot.pause(0.02)
            assert client.reference_calls == 2
            assert "60/40" in str(app.query_one("#reference-detail").content)

    asyncio.run(run())


def test_reference_refetches_on_every_visit_so_a_new_ablation_is_never_stale():
    """`: batch` writes new ablation numbers mid-session; the reference must follow.

    The leaderboard refreshes on the next 2s tick, so a once-per-session reference
    would keep asserting "latest ablation" with superseded numbers — two
    surfaces reporting different states of the same evidence.
    """
    from qlab.tui.app import QlabTui

    class ChangingReferenceClient(StubClient):
        def __init__(self):
            super().__init__()
            self.reference_calls = 0

        def get(self, path, **params):
            if path == "/api/reference":
                self.reference_calls += 1
                payload = _reference()
                if self.reference_calls > 1:
                    for row in payload["entries"]:
                        if row["ablation"]:
                            row["ablation"]["sharpe"] = 1.77
                return payload
            return super().get(path, **params)

    async def run():
        client = ChangingReferenceClient()
        app = QlabTui(client, refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("8")
            await pilot.pause(0.3)
            await pilot.press("down")
            await pilot.press("down")
            await pilot.pause(0.1)
            assert "0.910" in str(app.query_one("#reference-detail").content)
            assert client.reference_calls == 1

            await pilot.press("2")
            await pilot.press("8")
            for _ in range(60):
                if client.reference_calls == 2:
                    break
                await pilot.pause(0.02)
            await pilot.pause(0.3)
            await pilot.press("down")
            await pilot.press("down")
            await pilot.pause(0.1)
            detail = str(app.query_one("#reference-detail").content)
            assert client.reference_calls == 2
            assert "Hierarchical risk parity" in detail
            assert "1.770" in detail and "0.910" not in detail

    asyncio.run(run())


def test_nav_menu_rows_are_clickable():
    """Each of the eight spine rows switches to its matching view on click.

    The row clicked is the click's y within the widget, so this pins the
    mapping as well as the fact that a Static-based menu is now clickable.
    """
    from qlab.tui.app import QlabTui

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.pause(0.2)
            for row, view in enumerate(
                    ("atlas", "dashboard", "market", "workforce", "research",
                     "book", "audit", "reference", "settings")):
                # start elsewhere so each click is a genuine transition
                app.action_view("audit" if view != "audit" else "dashboard")
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
            await pilot.press("3")
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


def test_connection_chip_states():
    from qlab.tui.formatting import connection_chip

    assert connection_chip(None, 0) == ("CONNECTING", "warn")
    assert connection_chip(2.0, 0) == ("LIVE", "ok")
    assert connection_chip(10.0, 0) == ("LIVE", "ok")  # exactly 10s is still LIVE
    assert connection_chip(75.0, 1) == ("STALE 1:15", "warn")
    assert connection_chip(75.0, 2)[1] == "warn"  # 2 failures is not down yet
    assert connection_chip(None, 3) == ("OWNER DOWN", "down")
    text, level = connection_chip(120.0, 3)
    assert level == "down" and "OWNER DOWN" in text


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

    # Evidence surfaces retain ids while applying the same text cleanup.
    _, text = clean_report_line(
        "**Rejected** plan_id: `plan-7` â€” retry.",
        strip_ids=False,
    )
    assert text == "Rejected plan_id: plan-7 — retry."

    # markdown emphasis and back-ticks that render literally are stripped
    _, text = clean_report_line("**Result:** held the `checked plan`.")
    assert "*" not in text and "`" not in text
    assert text == "Result: held the checked plan."

    # a plain sentence with no markup is left untouched
    assert clean_report_line("Solved with HRP; the referee passed.") == (
        False, "Solved with HRP; the referee passed.")


def test_verdict_chip_returns_semantic_token_names_and_compact_text():
    from qlab.tui.formatting import verdict_chip

    assert verdict_chip({"verdict": "PASS"}) == ("UP", "PASS")
    assert verdict_chip({"verdict": "fail"}) == ("DOWN", "FAIL")
    assert verdict_chip(None) == ("MUTED", "—")


def test_key_number_lines_aligns_every_value_column():
    from qlab.tui.formatting import key_number_lines

    assert key_number_lines([
        ("cash", "$2.00"),
        ("drawdown", "1.5%"),
    ]) == [
        "cash      $2.00",
        "drawdown  1.5%",
    ]
    assert key_number_lines([]) == []


def test_bulletin_cleans_markdown_ids_empty_lines_and_length():
    from qlab.tui.formatting import bulletin

    assert bulletin([
        "### Result:",
        "",
        "**Held** the `checked plan` (plan_id: plan-7).",
    ]) == [
        "RESULT",
        "Held the checked plan.",
    ]
    assert bulletin(
        ["**Held** the `checked plan` (plan_id: plan-7)."],
        strip_ids=False,
    ) == ["Held the checked plan (plan_id: plan-7)."]
    assert bulletin(["abcdefgh"], max_len=4) == ["abcd"]


def test_report_lines_normalizes_agent_markdown():
    from qlab.tui.formatting import report_lines

    out = report_lines([
        "## How news is currently used",
        "",
        "",
        "News is a quarantined research input, not a trading signal.",
        "- first point",
        "* second point",
        "1. numbered step",
        "### The extractor cannot trade",
        "| source | quality |",
        "    conditioned = reweight(scenarios)",
    ])
    kinds = [kind for kind, _ in out]
    texts = {text for _, text in out}
    assert ("h1", "How news is currently used") in out
    assert ("h2", "The extractor cannot trade") in out
    assert kinds.count("bullet") == 3
    assert ("bullet", "first point") in out
    assert ("bullet", "1. numbered step") in out       # numbering preserved
    assert ("table", "| source | quality |") in out    # tables pass untruncated
    assert ("code", "    conditioned = reweight(scenarios)") in out
    assert kinds.count("blank") == 1                   # blank runs collapse
    assert not any("##" in text for text in texts)     # markers consumed


def test_report_lines_wraps_long_paragraphs_without_truncating():
    from qlab.tui.formatting import report_lines

    long = "word " * 100
    out = report_lines([long])
    assert all(kind == "text" for kind, _ in out)
    assert len(out) > 1                                 # wrapped, not cut
    assert sum(len(text.split()) for _, text in out) == 100


def test_report_lines_keeps_fenced_code_and_deep_headers_verbatim():
    """A fenced block is code even when it is not indented, and a deeper header
    is still a header — otherwise both reach the console as literal markdown."""
    from qlab.tui.formatting import report_lines

    out = report_lines([
        "#### Deeper section",
        "```python",
        "weights = solve(objective)   # alignment must survive",
        "```",
        "**Held** the `checked plan` â€” watch vol.",
    ])
    assert ("h2", "Deeper section") in out
    assert ("code", "weights = solve(objective)   # alignment must survive") in out
    assert not any("```" in text for _kind, text in out)
    # inline emphasis and back-ticks render literally in a RichLog; mojibake too
    assert ("text", "Held the checked plan — watch vol.") in out


def test_report_lines_reads_indented_list_items_as_bullets_not_code():
    """A nested list item is indented markdown; dimming it as code would read as
    a broken report."""
    from qlab.tui.formatting import report_lines

    out = report_lines([
        "- outer point",
        "    - nested point",
        "\t2. nested step",
    ])
    assert [kind for kind, _ in out] == ["bullet", "bullet", "bullet"]
    assert ("bullet", "nested point") in out
    assert ("bullet", "2. nested step") in out


def test_is_numbered_item_separates_ordinals_from_counts():
    """Only a real ordinal marker replaces the bullet glyph — a bullet that
    merely opens with a count still needs its marker."""
    from qlab.tui.formatting import is_numbered_item

    assert is_numbered_item("1. bind the targets")
    assert is_numbered_item("2) challenge the window")
    assert not is_numbered_item("3 of 7 arms admitted")
    assert not is_numbered_item("2026 was the calm year")
    assert not is_numbered_item("referee passed")
    assert not is_numbered_item("")


def test_fence_state_after_carries_across_streamed_chunks():
    """The console renders one line per call, so fence state cannot live inside
    report_lines; the caller threads it and report_lines accepts it."""
    from qlab.tui.formatting import fence_state_after, report_lines

    assert fence_state_after(["```python"]) is True
    assert fence_state_after(["```python", "code", "```"]) is False
    assert fence_state_after(["plain prose"], True) is True   # still open
    assert fence_state_after(["```"], True) is False          # closed

    # one unindented code line, arriving alone mid-block, is still code
    assert report_lines(["weights = solve(`obj`)"], fenced=True) == [
        ("code", "weights = solve(`obj`)")]
    # and the closing fence is read as a close, not a fresh open
    assert report_lines(["```", "back to prose"], fenced=True) == [
        ("text", "back to prose")]


def test_tui_app_uses_theme_tokens_instead_of_literal_hex_colors():
    import re
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "qlab" / "tui" / "app.py"
    ).read_text(encoding="utf-8")
    assert re.search(r"#[0-9a-fA-F]{3,8}\b", source) is None


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
                "result": {
                    "final_summary": (
                        "### Recommendation\n"
                        "**hold** reviewed HRP targets "
                        "decision_id: decision-hidden"
                    ),
                },
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
            await pilot.press("4")
            content = str(app.query_one("#workforce-content").content)
            assert "RESULT" in content
            assert "referee PASS" in content
            assert "• RECOMMENDATION" in content
            assert "• hold reviewed HRP targets" in content
            assert "###" not in content and "**" not in content
            assert "decision_id: decision-hidden" in content
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
            await pilot.press("4")
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
            await pilot.press("4")
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
            await pilot.press("4")
            content = str(app.query_one("#workforce-content").content)
            assert "BLOCKED at referee" in content
            assert "workforce resume wfbad" in content

    asyncio.run(run())


def test_workforce_failure_card_preserves_id_bearing_reason():
    from qlab.tui.app import QlabTui

    reason = "cannot complete without artifacts ['objective_id']"

    class FailedWorkflowClient(StubClient):
        def get(self, path, **params):
            snap = _snapshot()
            snap["workflows"] = [{
                "workflow_id": "wffailed",
                "kind": "portfolio_review",
                "status": "failed",
                "current_phase": "reporter",
                "request": {
                    "goal": "review",
                    "as_of": "2026-07-19",
                    "universe": "core",
                },
                "steps": [{
                    "phase": "reporter",
                    "agent": "reporter",
                    "status": "failed",
                    "summary": reason,
                }],
            }]
            return snap

    async def run():
        app = QlabTui(
            FailedWorkflowClient(),
            refresh_interval=0,
            claude_start="off",
        )
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("4")
            content = app.query_one("#workforce-content").render().plain
            assert "FAILED at reporter" in content
            assert reason in content

    asyncio.run(run())


def test_workforce_mode_advances_flowchart_without_dumping_narrative():
    from qlab.tui.app import QlabTui
    from qlab.tui.claude import ClaudeEvent
    from textual.widgets import RichLog

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("4")  # console renders once the view is visible
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
            await pilot.press("4")
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


def test_console_renders_markdown_report_styled():
    """A streamed agent report reaches the console as sections, aligned bullets
    and verbatim tables/code — never literal markdown or truncated prose."""
    from qlab.tui.app import QlabTui
    from textual.widgets import RichLog

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("4")
            await pilot.pause(0.1)
            app._console_stream_text(
                "## How news is currently used\n"
                "News is a quarantined research input, not a trading signal.\n"
                "- referee passed\n"
                "1. bind the targets\n"
                "| source | quality |\n"
                "```python\n"
                "conditioned = reweight(scenarios)\n"
                "```\n"
            )
            log = app.query_one("#workforce-console", RichLog)
            rendered = "\n".join(strip.text for strip in log.lines)
            flat = " ".join(rendered.split())

            assert "##" not in rendered and "```" not in rendered
            assert "How news is currently used" in flat
            # plain prose still reads as prose: whole, unbulleted, untruncated
            assert ("News is a quarantined research input, not a trading signal."
                    in flat)
            assert "• referee passed" in flat
            assert "1. bind the targets" in flat
            assert "• 1. bind the targets" not in flat   # numbering is the marker
            assert "| source | quality |" in rendered    # table alignment kept
            assert "conditioned = reweight(scenarios)" in flat

    asyncio.run(run())


def test_console_keeps_fenced_code_verbatim_across_streamed_chunks():
    """The real caller streams one token at a time, so a fenced block arrives
    across many calls. Fence state must survive between them: unindented code
    stays verbatim, and a chunk that closes the fence must not reopen it."""
    from qlab.tui.app import QlabTui
    from qlab.tui.theme import DIM, TEXT

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            written: list[str] = []
            app._console_write = written.append
            for chunk in (
                "Here is the guard:\n",
                "```python\n",
                "weights = solve(`obj`)  # **kept**\n",
                "    if weights.sum() != 1:\n",
                # a close plus the next prose line inside one delta
                "```\nThat guard is deterministic.\n",
            ):
                app._console_stream_text(chunk)

            rendered = "\n".join(written)
            assert "```" not in rendered                    # fences consumed
            # code is verbatim: back-ticks, ** and indentation all survive
            assert f"[{DIM}]weights = solve(`obj`)  # **kept**[/]" in written
            assert f"[{DIM}]    if weights.sum() != 1:[/]" in written
            # prose on both sides of the block is prose, never dim code
            assert f"[{TEXT}]Here is the guard:[/]" in written
            assert f"[{TEXT}]That guard is deterministic.[/]" in written
            assert app._console_fenced is False             # block closed

    asyncio.run(run())


def test_console_bullet_glyph_survives_a_count_leading_bullet():
    """A bullet that opens with a number is still a bullet; only a real ordinal
    marker ("1.") stands in for the glyph."""
    from qlab.tui.app import QlabTui
    from textual.widgets import RichLog

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            await pilot.press("4")
            await pilot.pause(0.1)
            app._console_stream_text(
                "- 3 of 7 arms admitted\n"
                "1. bind the targets\n"
            )
            flat = " ".join(" ".join(
                strip.text for strip in app.query_one(
                    "#workforce-console", RichLog).lines).split())

            assert "• 3 of 7 arms admitted" in flat
            assert "1. bind the targets" in flat
            assert "• 1. bind the targets" not in flat

    asyncio.run(run())


def test_console_flush_writes_the_partial_line_and_never_leaks_fence_state():
    """Text that never got its newline still reaches the console when the turn
    ends, and an unclosed fence dies with the turn."""
    from qlab.tui.app import QlabTui

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            written: list[str] = []
            app._console_write = written.append

            app._console_stream_text("Bottom line: ")
            app._console_stream_text("nothing traded.")
            assert written == []                    # an incomplete line waits
            app._console_flush()
            assert any("Bottom line: nothing traded." in line for line in written)
            assert app._console_partial == ""

            # a turn that ends mid-block must not render the next turn as code
            app._console_stream_text("```python\n")
            assert app._console_fenced is True
            app._console_flush()
            assert app._console_fenced is False

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
            await pilot.press("4")
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
            await pilot.press("4")
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
            await pilot.press("4")
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
            # the debate stage is announced, because that is what happens next
            assert "bounded debate" in rendered

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
            await pilot.press("4")
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


def test_tui_stop_resume_and_abandon_use_owner_lifecycle_controls():
    from qlab.tui.app import QlabTui

    async def run():
        client = InProcessClient()
        workflow = client.session.registry.start_workflow(
            "portfolio_review", {"goal": "lifecycle"})
        workflow_id = workflow["workflow_id"]
        client.session.registry.update_workflow_phase(
            workflow_id, "analyst", "working")

        app = QlabTui(client, refresh_interval=0, claude_start="off")
        async with app.run_test(size=(140, 42)) as pilot:
            for _ in range(100):
                if app.snapshot and not app._refreshing:
                    break
                await pilot.pause(0.05)
            app._active_workflow_id = workflow_id
            app._launched_workflow_id = workflow_id
            app.claude.mode = "workforce"

            app.action_workforce_stop()
            for _ in range(100):
                if not app._refreshing:
                    break
                await pilot.pause(0.05)
            row = client.session.registry.get_workflow(workflow_id)
            assert row["status"] == "interrupted"
            assert row["steps"][0]["status"] == "interrupted"

            app._start_claude = lambda *args, **kwargs: True
            app.action_workforce_resume(workflow_id)
            for _ in range(100):
                if not app._refreshing:
                    break
                await pilot.pause(0.05)
            assert client.session.registry.get_workflow(
                workflow_id)["status"] == "running"

            app.action_workforce_abandon(workflow_id)
            for _ in range(100):
                if not app._refreshing:
                    break
                await pilot.pause(0.05)
            row = client.session.registry.get_workflow(workflow_id)
            assert row["status"] == "abandoned"
            assert all(
                step["status"] == "abandoned"
                for step in row["steps"]
            )
            assert "audit trail" in str(
                app.query_one("#selected-work").content).lower()

    asyncio.run(run())


def test_successful_coordinator_exit_interrupts_an_unfinished_durable_run():
    from qlab.tui.app import QlabTui
    from qlab.tui.claude import ClaudeEvent

    async def run():
        client = InProcessClient()
        workflow = client.session.registry.start_workflow(
            "portfolio_review", {"goal": "early exit"})
        workflow_id = workflow["workflow_id"]
        client.session.registry.update_workflow_phase(
            workflow_id, "analyst", "working")

        app = QlabTui(client, refresh_interval=0, claude_start="off")
        async with app.run_test(size=(140, 42)) as pilot:
            for _ in range(100):
                if app.snapshot and not app._refreshing:
                    break
                await pilot.pause(0.05)
            app._active_workflow_id = workflow_id
            app._launched_workflow_id = workflow_id
            app.claude.mode = "workforce"

            app._apply_claude_event(ClaudeEvent(
                "result", "coordinator says it is done"))
            for _ in range(100):
                if not app._refreshing:
                    break
                await pilot.pause(0.05)

            row = client.session.registry.get_workflow(workflow_id)
            assert row["status"] == "interrupted"
            assert row["steps"][0]["status"] == "interrupted"
            assert "returned before" in row["steps"][0]["summary"]
            app._render_workforce()
            assert "working" not in app._flow_states.values()

    asyncio.run(run())


def test_workforce_note_follows_the_dependency_graph():
    from qlab.tui.app import workforce_note

    head, nxt = workforce_note(
        "analyst",
        "done",
        "**window chosen** decision_id: decision-hidden",
        {"analyst"},
    )
    assert head.startswith("analyst done") and "window chosen" in head
    assert "**" not in head and "decision_id" not in head
    assert "decision-hidden" not in head
    assert "debate" in nxt and "optimizer" in nxt

    _, nxt = workforce_note(
        "challenger", "done", "", {"analyst", "challenger"})
    assert "optimizer" in nxt and "final persisted decision" in nxt

    _, nxt = workforce_note("optimizer", "done", "", {"analyst", "optimizer"})
    assert "referee" in nxt

    head, nxt = workforce_note("referee", "blocked", "cap breach", {"analyst"})
    assert "blocked" in head and "cap breach" in head
    assert "Nothing was traded" in nxt


def test_tui_event_dedupe_window_is_bounded():
    from qlab.tui.app import QlabTui, _EVENT_ID_LIMIT

    app = QlabTui(StubClient(), refresh_interval=0, claude_start="off")
    appended = []
    app._append_event = appended.append
    events = [
        {"event_id": f"event-{index}", "kind": "audit", "payload": {}}
        for index in range(_EVENT_ID_LIMIT + 5)
    ]

    app._ingest_events(events)

    assert len(app._event_ids) == _EVENT_ID_LIMIT
    assert "event-0" not in app._event_ids
    assert f"event-{_EVENT_ID_LIMIT + 4}" in app._event_ids
    assert len(appended) == len(events)


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

        def stop(self, reason="operator requested stop"):
            self.stopped += 1

    async def run():
        app = QlabTui(StubClient(), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(140, 42)) as pilot:
            await pilot.pause(0.2)
            app.claude = ClaudeStub()
            await pilot.press("4")
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
            assert app.active_view == "dashboard"

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
    assert launched["start_new_session"] is True
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


def test_claude_session_reports_agent_configuration_failure(tmp_path, monkeypatch):
    from qlab.tui import claude as claude_module

    monkeypatch.setattr(
        claude_module.shutil, "which", lambda command: "/usr/local/bin/claude"
    )
    monkeypatch.setattr(
        claude_module,
        "build_workforce_agents",
        lambda prompt="": {
            "untrusted-role": {
                "description": "must be refused",
                "prompt": "no",
                "tools": [],
            }
        },
    )
    session = claude_module.ClaudeSession(lambda event: None, cwd=tmp_path)

    assert not session.start("inspect", governed=True)
    assert session.process is None
    assert session._session_dir is None
    assert "Could not configure Claude Code session" in session.last_error
    assert "unexpected session agent name" in session.last_error


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


def _atlas_snapshot(**over):
    """The standard snapshot plus the Atlas/approval/quote projections (P8)."""
    snap = _snapshot()
    snap.update({
        "atlas": {"manager_id": "atlas", "mode": "observe",
                "state": "observing", "blocked_reason": None,
                "coordinator_available": True},
        "atlas_tasks": [{"task_id": "task-1", "status": "queued",
                       "trigger_kind": "regime_flip"}],
        "approvals": [],
        "quotes": {"live_stream": False, "quotes": {}, "health": None},
    })
    snap.update(over)
    return snap


class _AtlasStubClient(StubClient):
    def __init__(self, snapshot):
        super().__init__()
        self._snapshot = snapshot

    def get(self, path, **params):
        if path == "/api/tui":
            return self._snapshot
        return super().get(path, **params)


def test_status_line_shows_atlas_mode_and_feed_identity():
    from qlab.tui.app import QlabTui

    snap = _atlas_snapshot(quotes={
        "live_stream": True, "feed": "sip",
        "quotes": {"ACWI": {"price": 101.0, "age_seconds": 0.4}},
        "health": {"fresh": True, "state": "live"}})

    async def run():
        app = QlabTui(_AtlasStubClient(snap), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(200, 48)) as pilot:
            await pilot.pause(0.2)
            status = str(app.query_one("#system-status").content)
            # IEX/SIP is never collapsed into the word "live".
            assert "ALPACA·SIP" in status
            assert "ATLAS OBSERVE/OBSERVING" in status

    asyncio.run(run())


def test_status_line_marks_a_stale_quote_feed():
    from qlab.tui.app import QlabTui

    snap = _atlas_snapshot(quotes={
        "live_stream": True, "feed": "iex", "quotes": {},
        "health": {"fresh": False, "state": "stale"}})

    async def run():
        app = QlabTui(_AtlasStubClient(snap), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(200, 48)) as pilot:
            await pilot.pause(0.2)
            status = str(app.query_one("#system-status").content)
            assert "ALPACA·IEX STALE" in status

    asyncio.run(run())


def test_audit_view_shows_atlas_panel_and_pending_approvals():
    from qlab.tui.app import QlabTui

    snap = _atlas_snapshot(
        atlas={"manager_id": "atlas", "mode": "propose",
             "state": "blocked", "blocked_reason": "data plane is blocked",
             "coordinator_available": True},
        approvals=[{"approval_id": "appr-abc123", "plan_id": "plan-xyz789",
                    "expires_at": "2026-07-24T18:45:00+00:00",
                    "status": "pending"}])

    async def run():
        app = QlabTui(_AtlasStubClient(snap), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            app.action_view("audit")
            await pilot.pause(0.2)
            panel = str(app.query_one("#audit-summary").content)
            assert "ATLAS · DESK MANAGER" in panel
            assert "PROPOSE" in panel and "BLOCKED" in panel
            assert "data plane is blocked" in panel
            assert "PENDING APPROVALS" in panel
            assert "appr-abc" in panel and "plan-xyz789" in panel
            # Viewing never approves; the panel says how approval happens.
            assert "approve or reject through the owner approvals API" in panel

    asyncio.run(run())


def test_atlas_panel_reports_degraded_coordinator_without_failing():
    from qlab.tui.app import QlabTui

    snap = _atlas_snapshot(atlas={
        "manager_id": "atlas", "mode": "observe", "state": "degraded",
        "blocked_reason": None, "coordinator_available": False})

    async def run():
        app = QlabTui(_AtlasStubClient(snap), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 48)) as pilot:
            await pilot.pause(0.2)
            app.action_view("audit")
            await pilot.pause(0.2)
            panel = str(app.query_one("#audit-summary").content)
            assert "DEGRADED" in panel
            assert "coordinator unavailable" in panel
            assert "monitoring continues" in panel

    asyncio.run(run())


def test_atlas_rail_is_present_in_every_view():
    from qlab.tui.app import QlabTui

    snap = _atlas_snapshot(
        atlas={"manager_id": "atlas", "mode": "research",
             "state": "observing", "blocked_reason": None,
             "coordinator_available": True},
        approvals=[{"approval_id": "appr-1", "plan_id": "plan-1",
                    "expires_at": "2026-07-24T18:45:00+00:00"}])

    async def run():
        app = QlabTui(_AtlasStubClient(snap), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(180, 48)) as pilot:
            await pilot.pause(0.2)
            for view in ("dashboard", "market", "book", "audit"):
                app.action_view(view)
                await pilot.pause(0.05)
                rail = str(app.query_one("#atlas-rail").content)
                assert "RESEARCH" in rail and "OBSERVING" in rail
                assert "ctrl+b for detail" in rail

    asyncio.run(run())


def test_ctrl_b_opens_and_closes_the_atlas_drawer():
    from qlab.tui.app import AtlasDrawerScreen, QlabTui

    snap = _atlas_snapshot(
        atlas={"manager_id": "atlas", "mode": "propose",
             "state": "observing", "blocked_reason": None,
             "coordinator_available": True},
        atlas_tasks=[{"task_id": "task-1", "status": "completed",
                    "trigger_kind": "regime_flip",
                    "created_at": "2026-07-24T10:00:00+00:00"},
                   {"task_id": "task-2", "status": "failed",
                    "trigger_kind": "drift_breach", "error": "coordinator died",
                    "created_at": "2026-07-24T11:00:00+00:00"}])

    async def run():
        app = QlabTui(_AtlasStubClient(snap), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(180, 48)) as pilot:
            await pilot.pause(0.2)
            app.action_atlas_drawer()
            await pilot.pause(0.1)
            assert isinstance(app.screen, AtlasDrawerScreen)
            body = str(app.screen.query_one("#atlas-drawer-body").content)
            # The mode's authority is stated plainly, never left to inference.
            assert "may request a checked plan" in body
            assert "Atlas never executes" in body
            assert "regime_flip" in body and "drift_breach" in body
            assert "coordinator died" in body
            # Ctrl+B toggles it closed again.
            app.action_atlas_drawer()
            await pilot.pause(0.1)
            assert not isinstance(app.screen, AtlasDrawerScreen)

    asyncio.run(run())


def test_atlas_drawer_states_authority_for_each_mode():
    from qlab.tui.app import QlabTui

    async def run():
        for mode, expected in (
            ("observe", "Starts no workflows"),
            ("research", "May not create a paper plan"),
            ("propose", "Cannot approve or execute"),
            ("paused", "no new autonomous work"),
        ):
            snap = _atlas_snapshot(atlas={
                "manager_id": "atlas", "mode": mode,
                "state": "observing", "blocked_reason": None,
                "coordinator_available": True})
            app = QlabTui(_AtlasStubClient(snap), refresh_interval=0,
                          claude_start="off")
            async with app.run_test(size=(180, 48)) as pilot:
                await pilot.pause(0.2)
                body = app._atlas_drawer_content()
                assert expected.lower() in body.lower(), mode

    asyncio.run(run())


def test_desk_opens_on_atlas_and_renders_the_read():
    """Atlas is the front door: the desk opens on the manager's read."""
    from qlab.tui.app import QlabTui

    snap = _atlas_snapshot(atlas_read={
        "as_of": "2026-07-25",
        "quantitative_state": "calm",
        "news": {"tone": "risk_off", "intensity": 0.4, "item_count": 5,
                 "risk_off_hits": 2, "risk_on_hits": 0, "top_tickers": ["ACWI"],
                 "headlines": [{"headline": "Selloff deepens on rate fear",
                                "source": "wire", "tickers": ["ACWI"],
                                "tone": "risk_off"}]},
        "agreement": "divergent",
        "conviction": 0.42,
        "tensions": ["Prices are calm but the qualitative record is not."],
        "observations": ["Indicator panel reads calm with 5 of 5 agreeing."],
        "would_change_my_mind": ["A turbulence reading moving into its tail."],
        "evidence_refs": ["snapshot:abc"], "read_hash": "h1", "advisory": True},
        atlas_heartbeat={"running": True, "ticks": 12})

    async def run():
        app = QlabTui(_AtlasStubClient(snap), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(180, 50)) as pilot:
            await pilot.pause(0.2)
            assert app.active_view == "atlas"
            read = str(app.query_one("#atlas-read").content)
            # Conclusion first, then what makes it interesting.
            assert "THE READ" in read
            assert "DIVERGENT" in read
            assert "TENSIONS" in read
            assert "qualitative record is not" in read
            assert "WOULD CHANGE THIS" in read
            assert "Selloff deepens" in read
            # The heartbeat is visible, so a dead Atlas is obvious.
            assert "heartbeat live" in read and "12 ticks" in read
            # And it never reads as an instruction.
            assert "advisory, never an instruction" in read
            assert "Atlas cannot trade" in read

    asyncio.run(run())


def test_atlas_view_says_so_when_no_read_exists_yet():
    from qlab.tui.app import QlabTui

    snap = _atlas_snapshot()
    snap.pop("atlas_read", None)

    async def run():
        app = QlabTui(_AtlasStubClient(snap), refresh_interval=0, claude_start="off")
        async with app.run_test(size=(160, 44)) as pilot:
            await pilot.pause(0.2)
            read = str(app.query_one("#atlas-read").content)
            assert "has not composed a read yet" in read

    asyncio.run(run())


def test_atlas_view_actions_route_through_the_owner():
    from qlab.tui.app import QlabTui

    async def run():
        client = _AtlasStubClient(_atlas_snapshot())
        app = QlabTui(client, refresh_interval=0, claude_start="off")
        async with app.run_test(size=(180, 50)) as pilot:
            await pilot.pause(0.1)
            await pilot.click("#btn-atlas-escalate")
            for _ in range(20):
                if client.posts:
                    break
                await pilot.pause(0.05)
            assert client.posts and client.posts[0][0] == "/api/atlas/escalate"

    asyncio.run(run())
