"""Desk-CLI rendering contracts (pure functions; no network, no owner)."""

from __future__ import annotations

import pytest

pytest.importorskip("rich")

from qlab.desk_cli import (
    format_event, make_console, phase_rows, render_desk, render_workflow)


def _recording_console():
    return make_console(record=True, width=100)


def test_format_event_compact_line_and_tone():
    style, line = format_event({
        "ts": "2026-07-19T14:31:09+00:00", "kind": "workflow_phase",
        "payload": {"phase": "analyst", "status": "working"},
    })
    assert style == "accent"
    assert line.startswith("14:31:09")
    assert "workflow_phase" in line and "phase=analyst" in line

    style, _ = format_event({
        "ts": "2026-07-19T14:31:09+00:00", "kind": "referee_verdict",
        "payload": {"verdict": "FAIL"},
    })
    assert style == "bad"

    style, _ = format_event({"kind": "referee_verdict",
                             "payload": {"verdict": "PASS"}})
    assert style == "ok"


def test_phase_rows_map_states_to_glyphs():
    rows = phase_rows({"steps": [
        {"phase": "analyst", "status": "done", "summary": "windows chosen"},
        {"phase": "challenger", "status": "working", "summary": ""},
        {"phase": "referee", "status": "blocked", "summary": "no PASS"},
    ]})
    assert [(r[1], r[2]) for r in rows] == [
        ("✓", "analyst"), ("●", "challenger"), ("!", "referee")]
    assert rows[0][0] == "ok" and rows[1][0] == "info" and rows[2][0] == "gold"


def test_render_workflow_shows_id_phases_and_result():
    console = _recording_console()
    render_workflow(console, {
        "workflow_id": "wf42", "status": "complete",
        "request": {"goal": "review the desk", "as_of": "2026-07-19",
                    "universe": "core"},
        "steps": [{"phase": "analyst", "status": "done", "summary": "ok"}],
        "result": {"final_summary": "hold reviewed HRP targets"},
    })
    text = console.export_text()
    assert "wf42" in text and "COMPLETE" in text
    assert "analyst" in text and "hold reviewed HRP targets" in text


def test_render_desk_covers_portfolio_policy_and_counts():
    console = _recording_console()
    render_desk(console, {
        "portfolio": {"equity": 10_000.0, "cash": 500.0, "drawdown": 0.02,
                      "kill_switch_at": 0.15,
                      "weights": {"ACWI": 0.6, "BNDW": 0.4},
                      "target_weights": {"ACWI": 0.55}},
        "market": {"source": "synthetic", "as_of": "2026-07-19",
                   "bar_age_days": 0, "regime": {"regime": "calm"}},
        "system": {"workforce_available": True},
        "policy": {"id": "hrp", "label": "Hierarchical risk parity"},
        "decisions": [1], "plans": [], "orders": [1, 2],
        "algorithms": [{"id": "hrp"}],
        "workflows": [],
    })
    text = console.export_text()
    assert "qlab desk" in text and "Hierarchical risk parity" in text
    assert "ACWI" in text and "60.0%" in text and "55.0%" in text
    assert "1 decisions" in text and "2 orders" in text
    assert "workforce" in text and "ready" in text


def test_workforce_cli_controls_require_an_id_and_call_the_owner(monkeypatch):
    from types import SimpleNamespace

    import qlab.desk_cli as desk_cli

    class Client:
        def __init__(self):
            self.posts = []

        def post(self, path, body=None):
            self.posts.append((path, body or {}))
            return {
                "workflow_id": "wf42",
                "status": path.rsplit("/", 1)[-1] + "ed",
                "request": {"goal": "review"},
                "steps": [],
                "result": {},
            }

    client = Client()
    console = _recording_console()
    monkeypatch.setattr(desk_cli, "_client", lambda args: client)
    monkeypatch.setattr(desk_cli, "_console", lambda: console)

    missing = SimpleNamespace(action="abandon", id=None)
    assert desk_cli.cmd_workforce(missing) == 2
    assert client.posts == []

    interrupt = SimpleNamespace(action="interrupt", id="wf42")
    assert desk_cli.cmd_workforce(interrupt) == 0
    assert client.posts == [(
        "/api/workflows/wf42/interrupt",
        {"reason": "operator interrupted the workflow from the desk CLI"},
    )]
    assert "durable writes are fenced" in console.export_text()
