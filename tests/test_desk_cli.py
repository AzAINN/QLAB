"""Desk-CLI rendering contracts and the launchers' desk-mode flags.

Pure functions, plus `qlab tui` / `qlab ui` driven with the port probe, owner
process, HTTP client and Textual app stubbed: no network, no owner process.
"""

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


# -- the desk-mode flags on `qlab tui` / `qlab ui` --------------------------
def test_desk_mode_from_args_maps_the_flags():
    import argparse

    from qlab.autopilot.cli import desk_mode_from_args
    from qlab.core.desk_mode import DeskMode

    def ns(**kw):
        return argparse.Namespace(live=False, alpaca_book=False, online=False, **kw)

    assert desk_mode_from_args(ns()) is None          # no flag: ask or persist
    assert desk_mode_from_args(argparse.Namespace(
        live=True, alpaca_book=False, online=False)) == DeskMode("live", "simulated")
    # --alpaca-book implies live; reaching the real book always takes the extra word.
    assert desk_mode_from_args(argparse.Namespace(
        live=False, alpaca_book=True, online=False)) == DeskMode("live", "alpaca")
    # legacy --online keeps working as "live data, simulated book"
    assert desk_mode_from_args(argparse.Namespace(
        live=False, alpaca_book=False, online=True)) == DeskMode("live", "simulated")


def test_both_launchers_accept_the_desk_mode_flags():
    from qlab.autopilot.cli import build_parser, desk_mode_from_args
    from qlab.core.desk_mode import DeskMode

    parser = build_parser()
    for command in ("tui", "ui"):
        assert desk_mode_from_args(parser.parse_args([command])) is None
        assert desk_mode_from_args(
            parser.parse_args([command, "--live"])) == DeskMode("live", "simulated")
        assert desk_mode_from_args(
            parser.parse_args([command, "--alpaca-book"])) == DeskMode("live", "alpaca")
        assert desk_mode_from_args(
            parser.parse_args([command, "--online"])) == DeskMode("live", "simulated")


def _flag_help(command: str, flag: str) -> str:
    import argparse

    from qlab.autopilot.cli import build_parser

    sub = next(action for action in build_parser()._actions
               if isinstance(action, argparse._SubParsersAction))
    for action in sub.choices[command]._actions:
        if flag in action.option_strings:
            return (action.help or "").lower()
    raise AssertionError(f"{command} has no {flag}")


@pytest.mark.parametrize("command", ["tui", "ui"])
def test_the_live_flags_do_not_promise_alpaca_market_data(command):
    """``--live`` only clears ``offline``; it does not pick a data provider.

    The provider still comes from ``QLAB_DATA_PROVIDER`` (yfinance by default),
    and the Alpaca provider reads ``ALPACA_API_KEY``/``ALPACA_API_SECRET`` from
    the environment directly — the browser login reaches the *book* lane only.
    Help promising "live Alpaca market data" describes something the desk does
    not do, and an OAuth-only operator cannot make true.
    """
    live = _flag_help(command, "--live")
    assert "alpaca market data" not in live and "alpaca prices" not in live
    assert "qlab_data_provider" in live
    assert "simulated book" in live
    # --online resolves to the same desk mode, so it must not read as another.
    assert "same as --live" in _flag_help(command, "--online")


def test_desk_mode_argv_reproduces_the_mode_for_the_owner():
    from qlab.autopilot.cli import build_parser, desk_mode_argv, desk_mode_from_args
    from qlab.core.desk_mode import DeskMode

    parser = build_parser()
    for mode in (DeskMode("live", "simulated"), DeskMode("live", "alpaca")):
        argv = ["ui", "--no-browser", *desk_mode_argv(mode)]
        assert desk_mode_from_args(parser.parse_args(argv)) == mode
    # Synthetic is the default on both sides, so it needs no word.
    assert desk_mode_argv(DeskMode("synthetic", "simulated")) == []
    assert desk_mode_argv(None) == []


def test_a_persisted_mode_makes_startup_silent():
    """Startup asks only when nobody has chosen — not on every launch.

    A persisted *synthetic* mode is the case that separates "the operator chose
    synthetic" from "the operator has never chosen"; both look identical in the
    resolved mode, so only persistence can tell them apart.
    """
    from qlab.autopilot.cli import (
        build_parser, desk_mode_from_args, startup_desk_mode)
    from qlab.core.desk_mode import DeskMode, save_desk_mode

    no_flag = desk_mode_from_args(build_parser().parse_args(["tui"]))
    assert startup_desk_mode(no_flag) is None         # never chosen: ask

    save_desk_mode(DeskMode("synthetic", "simulated"))
    assert startup_desk_mode(no_flag) == DeskMode("synthetic", "simulated")


def test_a_flag_beats_a_conflicting_persisted_mode():
    from qlab.autopilot.cli import (
        build_parser, desk_mode_from_args, startup_desk_mode)
    from qlab.core.desk_mode import DeskMode, load_desk_mode, save_desk_mode

    save_desk_mode(DeskMode("synthetic", "simulated"))
    chosen = startup_desk_mode(
        desk_mode_from_args(build_parser().parse_args(["tui", "--alpaca-book"])))
    assert chosen == DeskMode("live", "alpaca")
    # The flag is the operator speaking now, so it also becomes the persisted one.
    assert load_desk_mode() == DeskMode("live", "alpaca")


def test_a_persisted_live_mode_without_credentials_asks_again(monkeypatch):
    """A credential that has gone away hands the choice back, silently neither
    starting live nor rewriting the persisted mode to something safer-looking."""
    from qlab.autopilot.cli import (
        build_parser, desk_mode_from_args, startup_desk_mode)
    from qlab.core.desk_mode import DeskMode, load_desk_mode, save_desk_mode

    no_flag = desk_mode_from_args(build_parser().parse_args(["tui"]))
    save_desk_mode(DeskMode("live", "alpaca"))

    # conftest points ALPACA_CONFIG_DIR at a directory that does not exist and
    # clears the env keys, so nothing resolves here.
    assert startup_desk_mode(no_flag) is None
    assert load_desk_mode() == DeskMode("live", "alpaca")   # untouched

    # A resolvable credential (env pair; file/env only, never a network call)
    # makes the same persisted mode silent again.
    monkeypatch.setenv("ALPACA_API_KEY", "PKFAKEKEYFORTESTS")
    monkeypatch.setenv("ALPACA_API_SECRET", "fake-secret-for-tests")
    assert startup_desk_mode(no_flag) == DeskMode("live", "alpaca")


def _drive_cmd_tui(monkeypatch, argv, *, owner_running):
    """Run `qlab tui` with everything outside the CLI stubbed out.

    Returns what each stub was handed: the owner's argv, the client's calls and
    the keywords the Textual app was constructed with.
    """
    from qlab.autopilot import cli
    from qlab.tui import app as tui_app
    from qlab.tui import client as tui_client

    record: dict = {"spawned": None, "gets": [], "posts": [], "app": None}
    # Closed once, then open: the spawn path's wait loop sees its owner come up.
    probe = [0] if owner_running else [1, 0]

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def settimeout(self, _timeout):
            pass

        def connect_ex(self, _address):
            return probe.pop(0) if len(probe) > 1 else probe[0]

    class FakeOwner:
        stderr = None

        def __init__(self, spawn_argv, **_kwargs):
            record["spawned"] = list(spawn_argv)

        def poll(self):
            return None

        def terminate(self):
            record["terminated"] = True

    class FakeClient:
        def __init__(self, base_url):
            record["base_url"] = base_url

        def get(self, path, **params):
            record["gets"].append((path, params))
            return {}

        def post(self, path, body=None):
            record["posts"].append((path, body))
            return {}

    class FakeApp:
        def __init__(self, client, **kwargs):
            record["app"] = kwargs

        def run(self):
            record["ran"] = True

    monkeypatch.setattr(cli.socket, "socket", FakeSocket)
    monkeypatch.setattr(cli.subprocess, "Popen", FakeOwner)
    monkeypatch.setattr(tui_client, "ApiClient", FakeClient)
    monkeypatch.setattr(tui_app, "QlabTui", FakeApp)
    assert cli.main(argv) == 0
    return record


def test_cmd_tui_spawns_its_owner_with_the_same_mode(monkeypatch):
    pytest.importorskip("textual")
    from qlab.core.desk_mode import DeskMode, load_desk_mode

    record = _drive_cmd_tui(monkeypatch, ["tui", "--alpaca-book"], owner_running=False)

    assert record["spawned"][1:4] == ["-m", "qlab.autopilot.cli", "ui"]
    # The owner is launched with the words the TUI is about to display.
    assert record["spawned"][-2:] == ["--live", "--alpaca-book"]
    assert record["app"]["desk_mode"] == DeskMode("live", "alpaca")
    assert record["app"]["offline"] is False
    assert record["gets"] == [("/api/system", {"offline": 0})]
    assert record["posts"] == []            # argv already told the owner
    assert load_desk_mode() == DeskMode("live", "alpaca")   # next launch is silent


def test_cmd_tui_tells_a_running_owner_only_what_a_flag_asked_for(monkeypatch):
    pytest.importorskip("textual")
    from qlab.core.desk_mode import DeskMode, save_desk_mode

    # An owner we did not spawn read its mode at construction, so an explicit
    # flag has to reach it over the API or the two disagree about whose book it is.
    record = _drive_cmd_tui(monkeypatch, ["tui", "--alpaca-book"], owner_running=True)
    assert record["spawned"] is None
    assert record["posts"] == [
        ("/api/desk_mode", {"data": "live", "book": "alpaca"})]

    # Without a flag nothing is pushed: a persisted synthetic desk must not
    # silently downgrade an owner someone else started live.
    save_desk_mode(DeskMode("synthetic", "simulated"))
    record = _drive_cmd_tui(monkeypatch, ["tui"], owner_running=True)
    assert record["posts"] == []
    assert record["app"]["desk_mode"] == DeskMode("synthetic", "simulated")


def test_cmd_ui_hands_the_flagged_mode_to_the_owner_session(monkeypatch):
    from qlab.autopilot import cli
    from qlab.core.desk_mode import DeskMode
    from qlab.ui import server as ui_server

    calls: dict = {}
    monkeypatch.setattr(ui_server, "serve", lambda **kwargs: calls.update(kwargs))

    assert cli.main(["ui", "--alpaca-book", "--no-browser"]) == 0
    assert calls["desk_mode"] == DeskMode("live", "alpaca")
    assert calls["offline"] is False

    calls.clear()
    assert cli.main(["ui", "--no-browser"]) == 0
    # No flag: the session loads the persisted mode itself. `qlab ui` has no
    # modal, so it must not be handed a guess.
    assert calls["desk_mode"] is None
    assert calls["offline"] is True


def test_cmd_ui_persists_an_explicitly_flagged_mode(monkeypatch):
    """`qlab ui` is a first-class entry point, not only the helper `qlab tui`
    spawns, and the session holds its mode in memory only. If the flag never
    reaches desk_mode.json, a later bare `qlab tui` attaching to this owner
    reads an absent or stale file and displays a safer-looking desk than the one
    actually being traded.
    """
    from qlab.autopilot import cli
    from qlab.core.desk_mode import DeskMode, load_desk_mode
    from qlab.ui import server as ui_server

    monkeypatch.setattr(ui_server, "serve", lambda **kwargs: None)

    assert cli.main(["ui", "--no-browser"]) == 0
    assert load_desk_mode() is None             # no flag invents no choice

    assert cli.main(["ui", "--alpaca-book", "--no-browser"]) == 0
    assert load_desk_mode() == DeskMode("live", "alpaca")
