"""Desk-CLI rendering contracts and the launchers' desk-mode flags.

Pure functions, plus `qlab tui` / `qlab ui` driven with the port probe, owner
process, HTTP client and Textual app stubbed: no network, no owner process.
"""

from __future__ import annotations

import os

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
        base = {"offline": False, "alpaca_book": False}
        base.update(kw)
        return argparse.Namespace(**base)

    # No flag resolves later (persisted choice, then the live default).
    assert desk_mode_from_args(ns()) is None
    # Synthetic is the lane that now takes the explicit word.
    assert desk_mode_from_args(ns(offline=True)) == DeskMode("synthetic", "simulated")
    # Reaching the real book always takes the extra word.
    assert desk_mode_from_args(ns(alpaca_book=True)) == DeskMode("live", "alpaca")


def test_both_launchers_accept_the_desk_mode_flags():
    from qlab.autopilot.cli import build_parser, desk_mode_from_args
    from qlab.core.desk_mode import DeskMode

    parser = build_parser()
    for command in ("tui", "owner"):
        assert desk_mode_from_args(parser.parse_args([command])) is None
        assert desk_mode_from_args(
            parser.parse_args([command, "--offline"])) == DeskMode("synthetic", "simulated")
        assert desk_mode_from_args(
            parser.parse_args([command, "--alpaca-book"])) == DeskMode("live", "alpaca")


def _flag_help(command: str, flag: str) -> str:
    import argparse

    from qlab.autopilot.cli import build_parser

    sub = next(action for action in build_parser()._actions
               if isinstance(action, argparse._SubParsersAction))
    for action in sub.choices[command]._actions:
        if flag in action.option_strings:
            return (action.help or "").lower()
    raise AssertionError(f"{command} has no {flag}")


@pytest.mark.parametrize("command", ["tui", "owner"])
def test_the_retired_lane_words_still_parse_so_the_refusal_can_name_a_remedy(command):
    """``--live``/``--online`` retired when live became the default.

    They stay registered — hidden from ``--help`` — for one reason: an operator
    with the old command in a script gets the sentence naming the new default
    and ``--offline``, rather than argparse's bare "unrecognized arguments".
    """
    from qlab.autopilot.cli import build_parser

    import argparse

    args = build_parser().parse_args([command, "--live"])
    assert args.live is True
    assert _flag_help(command, "--live") == argparse.SUPPRESS.lower()


def test_desk_mode_argv_reproduces_the_mode_for_the_owner():
    from qlab.autopilot.cli import build_parser, desk_mode_argv, desk_mode_from_args
    from qlab.core.desk_mode import DeskMode

    parser = build_parser()
    for mode in (DeskMode("synthetic", "simulated"), DeskMode("live", "alpaca")):
        argv = ["owner", *desk_mode_argv(mode)]
        assert desk_mode_from_args(parser.parse_args(argv)) == mode
    # Live with the simulated book is the default on both sides: no word. Its
    # round trip through the parser is None, which resolves to the same desk.
    assert desk_mode_argv(DeskMode("live", "simulated")) == []
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
    # Never chosen: the live desk on the credential-free provider, silently —
    # every operation available without a flag.
    assert startup_desk_mode(no_flag) == DeskMode("live", "simulated")

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


def _drive_cmd_tui(monkeypatch, tmp_path, argv, *, owner_running):
    """Run `qlab tui` with everything outside the CLI stubbed out.

    Returns what each stub was handed: the owner's argv, the client's calls,
    and the workstation exec's argv. The exec is captured rather than run — a
    real `os.execvpe` would replace the pytest process with the workstation
    and the whole run would disappear mid-file.
    """
    from qlab.autopilot import cli
    from qlab.tui import client as tui_client

    binary = tmp_path / "atlas"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("QLAB_ATLAS_BIN", str(binary))

    record: dict = {"spawned": None, "gets": [], "posts": [], "exec": None}
    monkeypatch.setattr(
        cli.os, "execvpe",
        lambda _path, exec_argv, _env: record.update(exec=list(exec_argv))
        or (_ for _ in ()).throw(SystemExit(0)))
    # The Windows leg runs the client as a child instead of exec'ing; capture
    # it the same way so this harness holds on every CI platform.
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda exec_argv, **_kw: record.update(exec=list(exec_argv))
        or type("Done", (), {"returncode": 0})())
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

        def probe(self, *args, **kwargs):
            # Readiness is probe-based since the launcher stopped trusting an
            # open port; a fake owner is ready the moment it exists.
            return {}

        def get(self, path, **params):
            record["gets"].append((path, params))
            return {}

        def post(self, path, body=None):
            record["posts"].append((path, body))
            return {}

    monkeypatch.setattr(cli.socket, "socket", FakeSocket)
    monkeypatch.setattr(cli.subprocess, "Popen", FakeOwner)
    monkeypatch.setattr(tui_client, "ApiClient", FakeClient)
    # Both platforms end in SystemExit(0): the exec stub raises it, and the
    # Windows leg raises the child's returncode after subprocess.run.
    with pytest.raises(SystemExit) as exit_info:
        cli.main(argv)
    assert exit_info.value.code == 0
    assert record["exec"], "the workstation was never handed off to"
    return record


@pytest.mark.parametrize("argv", [
    ["run-once", "--offline"],
    ["daily-ops", "--offline"],
    ["batch", "configs/specs/ablation_v1.yaml", "--offline"],
    ["recommend", "--offline"],
])
def test_direct_registry_commands_refuse_while_an_owner_owns_the_book(
    monkeypatch, argv,
):
    # These construct their own Registry, and DuckDB allows one writer. Running
    # the documented invocation in a second shell while `qlab tui` is up used
    # to die on a raw lock error naming a pid, with no mention of the owner.
    from qlab.autopilot import cli

    monkeypatch.setattr(
        "qlab.mcp.server.owner_runtime_alive", lambda port: True)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv)
    message = str(excinfo.value)
    assert "already owns the paper book" in message
    assert "qlab desk" in message  # and it names the way through


def test_the_owner_port_reaches_the_guard_in_child_processes(monkeypatch):
    # The combined MCP server reads QLAB_UI_PORT and nothing set it, so on a
    # non-default port its guard probed 8765, found nothing, and opened the
    # registry as a second writer.
    import os

    from qlab.autopilot import cli

    monkeypatch.delenv("QLAB_UI_PORT", raising=False)
    cli._publish_owner_port(9123)
    assert os.environ["QLAB_UI_PORT"] == "9123"


def test_cmd_tui_spawns_its_owner_with_the_same_mode(monkeypatch, tmp_path):
    from qlab.core.desk_mode import DeskMode, load_desk_mode

    record = _drive_cmd_tui(
        monkeypatch, tmp_path, ["tui", "--alpaca-book"], owner_running=False)

    assert record["spawned"][1:4] == ["-m", "qlab.autopilot.cli", "owner"]
    # The owner is launched with the words the TUI is about to display; live
    # is the default, so the book is the only word left to say.
    assert record["spawned"][-1] == "--alpaca-book"
    assert "--live" not in record["spawned"] and "--offline" not in record["spawned"]
    # The client polls the live view of the owner it was handed.
    assert record["exec"][-1] == "--live"
    assert record["gets"] == [("/api/system", {"offline": 0})]
    assert record["posts"] == []            # argv already told the owner
    assert load_desk_mode() == DeskMode("live", "alpaca")   # next launch is silent


def test_bare_qlab_defaults_to_the_live_desk(monkeypatch, tmp_path):
    """`qlab`, no words: the desk, live data, simulated book, no flags.

    The whole point of the default flip — every operation available without
    naming one — asserted at the spawn seam where it becomes the owner's lane.
    """
    record = _drive_cmd_tui(monkeypatch, tmp_path, [], owner_running=False)

    assert record["spawned"][1:4] == ["-m", "qlab.autopilot.cli", "owner"]
    assert "--offline" not in record["spawned"]
    assert record["exec"][-1] == "--live"
    assert record["gets"] == [("/api/system", {"offline": 0})]


def test_qlab_offline_is_the_synthetic_demo(monkeypatch, tmp_path):
    record = _drive_cmd_tui(monkeypatch, tmp_path, ["--offline"], owner_running=False)

    assert record["spawned"][-1] == "--offline"
    assert record["exec"][-1] != "--live"
    assert record["gets"] == [("/api/system", {"offline": 1})]


def test_cmd_tui_tells_a_running_owner_only_what_a_flag_asked_for(monkeypatch, tmp_path):
    from qlab.core.desk_mode import DeskMode, save_desk_mode

    # An owner we did not spawn read its mode at construction, so an explicit
    # flag has to reach it over the API or the two disagree about whose book it is.
    record = _drive_cmd_tui(
        monkeypatch, tmp_path, ["tui", "--alpaca-book"], owner_running=True)
    assert record["spawned"] is None
    assert record["posts"] == [
        ("/api/desk_mode", {"data": "live", "book": "alpaca"})]

    # Without a flag nothing is pushed: a persisted synthetic desk must not
    # silently downgrade an owner someone else started live.
    save_desk_mode(DeskMode("synthetic", "simulated"))
    record = _drive_cmd_tui(monkeypatch, tmp_path, ["tui"], owner_running=True)
    assert record["posts"] == []
    assert record["exec"][-1] != "--live"


def test_cmd_owner_hands_the_resolved_mode_to_the_session(monkeypatch):
    from qlab.autopilot import cli
    from qlab.core.desk_mode import DeskMode
    from qlab.ui import server as ui_server

    calls: dict = {}
    monkeypatch.setattr(ui_server, "serve", lambda **kwargs: calls.update(kwargs))

    assert cli.main(["owner", "--alpaca-book"]) == 0
    assert calls["desk_mode"] == DeskMode("live", "alpaca")
    assert calls["offline"] is False

    calls.clear()
    # startup_desk_mode persisted the flag above; a bare owner reuses it —
    # provided the credential it needs still resolves (env pair; no network).
    monkeypatch.setenv("ALPACA_API_KEY", "PKFAKEKEYFORTESTS")
    monkeypatch.setenv("ALPACA_API_SECRET", "fake-secret-for-tests")
    assert cli.main(["owner"]) == 0
    assert calls["desk_mode"] == DeskMode("live", "alpaca")


def test_cmd_owner_persists_a_flag_and_never_the_default(monkeypatch):
    """A flag is the operator speaking and is persisted; the never-chosen live
    default is not — persisting it would quietly convert "never chose" into
    "chose live" on disk.
    """
    from qlab.autopilot import cli
    from qlab.core.desk_mode import DeskMode, load_desk_mode
    from qlab.ui import server as ui_server

    calls: dict = {}
    monkeypatch.setattr(ui_server, "serve", lambda **kwargs: calls.update(kwargs))

    assert cli.main(["owner"]) == 0
    assert calls["desk_mode"] == DeskMode("live", "simulated")
    assert calls["offline"] is False
    assert load_desk_mode() is None             # the default invents no choice

    assert cli.main(["owner", "--offline"]) == 0
    assert load_desk_mode() == DeskMode("synthetic", "simulated")


def test_cmd_owner_refuses_a_live_choice_whose_credential_is_gone(monkeypatch):
    """Headless, so the one question startup can raise has nobody to ask.

    conftest points the Alpaca config at nothing and clears the env keys, so a
    persisted live/alpaca mode cannot resolve — and a headless owner must
    refuse with the remedy rather than guess a lane.
    """
    from qlab.autopilot import cli
    from qlab.core.desk_mode import DeskMode, save_desk_mode
    from qlab.ui import server as ui_server

    monkeypatch.setattr(
        ui_server, "serve",
        lambda **kwargs: pytest.fail("an unresolvable desk must not serve"))
    save_desk_mode(DeskMode("live", "alpaca"))

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["owner"])
    message = str(exit_info.value)
    assert "no Alpaca credential" in message
    assert "--offline" in message


def test_the_ui_command_is_gone(capsys):
    """`qlab ui` was the web client; the web client is retired whole.

    argparse exits 2 on an unknown subcommand — the sentence is its, but the
    exit is the contract: nothing web-shaped starts.
    """
    from qlab.autopilot import cli

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["ui"])
    assert exit_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err

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


def test_restart_stops_the_previous_listener():
    """`--restart` kills whatever holds the port, then the launch proceeds.

    A real child process on a real socket, because the seam under test is
    exactly the boundary between this launcher and a process it did not start.
    The no-op leg — nothing on the port — must return without complaint.
    """
    import socket as socket_module
    import subprocess
    import sys
    import time

    from qlab.autopilot import cli

    probe = socket_module.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    # Nothing on the port: nothing to do, loudly nothing to refuse.
    cli._stop_listener_on_port(port)

    child = subprocess.Popen([
        sys.executable, "-c",
        "import socket, time\n"
        f"s = socket.socket()\n"
        f"s.bind((\"127.0.0.1\", {port}))\n"
        "s.listen(1)\n"
        "time.sleep(60)\n",
    ])
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            with socket_module.socket() as check:
                check.settimeout(0.2)
                if check.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        else:
            raise AssertionError("the fixture listener never came up")

        cli._stop_listener_on_port(port)

        with socket_module.socket() as check:
            check.settimeout(0.2)
            assert check.connect_ex(("127.0.0.1", port)) != 0, (
                "the listener survived --restart")
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def test_restart_dialog_takes_the_tier_typed_back_as_the_agreement(monkeypatch, tmp_path):
    from qlab.autopilot import cli

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    answers = iter(["everything", "everything"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert cli._restart_dialog("ask", False, 8765, tmp_path) == "everything"

    # A mistyped agreement stops everything, and says so.
    answers = iter(["book", "yes"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    with pytest.raises(SystemExit) as exit_info:
        cli._restart_dialog("ask", False, 8765, tmp_path)
    assert "declined" in str(exit_info.value)

    # runtime is the old --restart: chosen, not re-typed.
    monkeypatch.setattr("builtins.input", lambda _prompt: "runtime")
    assert cli._restart_dialog("ask", False, 8765, tmp_path) == "runtime"


def test_a_destructive_restart_is_never_defaulted_without_a_terminal(monkeypatch, tmp_path):
    from qlab.autopilot import cli

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as exit_info:
        cli._restart_dialog("ask", False, 8765, tmp_path)
    assert "--yes" in str(exit_info.value)
    with pytest.raises(SystemExit):
        cli._restart_dialog("everything", False, 8765, tmp_path)
    # The scripted spelling is equally explicit and needs no terminal.
    assert cli._restart_dialog("everything", True, 8765, tmp_path) == "everything"


def test_everything_archives_the_desk_state_rather_than_deleting_it(tmp_path):
    from qlab.autopilot import cli

    root = tmp_path / ".lab"
    root.mkdir()
    (root / "registry.duckdb").write_bytes(b"x" * 10)
    (root / "posture.json").write_text("{}")
    dest = cli._archive_state(root)
    assert not root.exists()
    assert dest.parent == tmp_path / ".lab-archive"
    assert (dest / "registry.duckdb").read_bytes() == b"x" * 10
    assert (dest / "posture.json").exists()
    # Nothing to archive is not an error: a first open has no desk yet.
    assert cli._archive_state(tmp_path / "absent") == tmp_path / "absent"


def test_the_book_tier_resets_through_the_fresh_owner(monkeypatch, tmp_path):
    """One writer: the reset rides the owner's own route after readiness, and
    the launcher never opens the registry itself."""
    from qlab.autopilot import cli

    # The listener stop has its own test against a real socket; here it would
    # consume the harness's port probe and read the fake owner as already up.
    monkeypatch.setattr(cli, "_stop_listener_on_port", lambda _port: None)
    record = _drive_cmd_tui(
        monkeypatch, tmp_path, ["tui", "--restart=book", "--yes"], owner_running=False)
    assert record["spawned"][1:4] == ["-m", "qlab.autopilot.cli", "owner"]
    assert ("/api/reset", {}) in record["posts"]


def test_the_restart_parser_takes_a_tier_or_asks():
    from qlab.autopilot.cli import build_parser

    p = build_parser()
    assert p.parse_args(["tui"]).restart is None
    assert p.parse_args(["tui", "--restart"]).restart == "ask"
    assert p.parse_args(["tui", "--restart=everything", "--yes"]).restart == "everything"
    with pytest.raises(SystemExit):
        p.parse_args(["tui", "--restart=all"])


def _forget_env(monkeypatch, *names):
    """Unset names for one test AND restore them afterwards.

    `monkeypatch.delenv(raising=False)` records nothing when the name is
    already absent, so a test that then *sets* it leaks into the next module —
    which is how a `news-setup` test made `test_news_grounding` see a stack it
    never configured.
    """
    for name in names:
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name, raising=False)


def _no_prompts(monkeypatch):
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": pytest.fail(f"the scripted path prompted: {prompt}"))


def test_news_setup_without_a_terminal_refuses_and_names_the_flags(monkeypatch):
    """No tty and no flags is a question nothing can answer; refuse, don't guess."""
    from qlab.autopilot import cli

    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["news-setup"])
    assert "--providers" in str(exit_info.value)


def test_news_setup_with_flags_persists_without_prompting(monkeypatch, tmp_path):
    from qlab.autopilot import cli
    from qlab.env import parse_env

    monkeypatch.setenv("QLAB_WORKSPACE", str(tmp_path))
    _forget_env(monkeypatch, "QLAB_NEWS_PROVIDERS", "QLAB_NEWS_PROVIDER",
                "QLAB_EDGAR_CONTACT")
    _no_prompts(monkeypatch)

    assert cli.main([
        "news-setup", "--providers", "edgar,macro",
        "--edgar-contact", "Jane <j@x.io>", "--no-verify"]) == 0

    written = parse_env((tmp_path / ".env").read_text(encoding="utf-8"))
    assert written == {"QLAB_NEWS_PROVIDERS": "edgar,macro",
                       "QLAB_EDGAR_CONTACT": "Jane <j@x.io>"}
    # The process the operator is in sees it too, or the next command lies.
    assert os.environ["QLAB_NEWS_PROVIDERS"] == "edgar,macro"


def test_news_setup_refuses_edgar_without_a_contact_and_an_unknown_provider(
        monkeypatch, tmp_path):
    from qlab.autopilot import cli

    monkeypatch.setenv("QLAB_WORKSPACE", str(tmp_path))
    _forget_env(monkeypatch, "QLAB_EDGAR_CONTACT")
    _no_prompts(monkeypatch)

    with pytest.raises(SystemExit) as missing:
        cli.main(["news-setup", "--providers", "edgar", "--no-verify"])
    assert "--edgar-contact" in str(missing.value)

    with pytest.raises(SystemExit) as unknown:
        cli.main(["news-setup", "--providers", "bloomberg", "--no-verify"])
    assert "bloomberg" in str(unknown.value)
    assert not (tmp_path / ".env").exists(), "a refusal wrote configuration"


def _door_env(monkeypatch, tmp_path, *, providers=None, contact=None):
    monkeypatch.setenv("QLAB_WORKSPACE", str(tmp_path))
    _forget_env(monkeypatch, "QLAB_NEWS_PROVIDER", "QLAB_NEWS_PROVIDERS",
                "QLAB_EDGAR_CONTACT")
    if providers is not None:
        monkeypatch.setenv("QLAB_NEWS_PROVIDERS", providers)
    if contact is not None:
        monkeypatch.setenv("QLAB_EDGAR_CONTACT", contact)


def test_the_startup_door_offers_news_setup_and_takes_no_for_an_answer(
        monkeypatch, tmp_path, capsys):
    from qlab.autopilot import cli

    _door_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    asked: list[str] = []

    def ask(prompt=""):
        asked.append(prompt)
        return "n"

    monkeypatch.setattr("builtins.input", ask)
    record = _drive_cmd_tui(monkeypatch, tmp_path, ["tui"], owner_running=False)

    assert len(asked) == 1 and "Set up news sources now?" in asked[0]
    assert not (tmp_path / ".env").exists(), "declining wrote configuration"
    # Declining is not a dead end: say where the same choice lives afterwards.
    printed = capsys.readouterr().out
    assert "qlab news-setup" in printed and "Settings" in printed
    assert record["spawned"][1:4] == ["-m", "qlab.autopilot.cli", "owner"]


def test_the_startup_door_runs_the_wizard_and_persists_the_answer(
        monkeypatch, tmp_path):
    from qlab.autopilot import cli
    from qlab.env import parse_env

    _door_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    # Yes to the door, then no to reading real news: the shortest walk that
    # still ends in a written stack.
    answers = iter(["y", "n"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    _drive_cmd_tui(monkeypatch, tmp_path, ["tui"], owner_running=False)

    assert parse_env((tmp_path / ".env").read_text(encoding="utf-8")) == {
        "QLAB_NEWS_PROVIDERS": "synthetic"}
    assert os.environ["QLAB_NEWS_PROVIDERS"] == "synthetic"


def test_the_startup_door_drops_edgar_for_one_run_without_persisting_it(
        monkeypatch, tmp_path, capsys):
    from qlab.autopilot import cli

    _door_env(monkeypatch, tmp_path, providers="edgar,macro")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    asked: list[str] = []

    def ask(prompt=""):
        asked.append(prompt)
        return "drop"

    monkeypatch.setattr("builtins.input", ask)
    _drive_cmd_tui(monkeypatch, tmp_path, ["tui"], owner_running=False)

    assert asked and "drop" in asked[0]
    assert "qlab news-setup" in capsys.readouterr().out
    assert os.environ["QLAB_NEWS_PROVIDERS"] == "macro"
    assert not (tmp_path / ".env").exists(), "a one-run drop was persisted"


def test_the_startup_door_takes_the_contact_and_writes_only_that_line(
        monkeypatch, tmp_path):
    from qlab.autopilot import cli
    from qlab.env import parse_env

    _door_env(monkeypatch, tmp_path, providers="edgar,macro")
    (tmp_path / ".env").write_text("QLAB_NEWS_PROVIDERS=edgar,macro\n",
                                   encoding="utf-8")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    answers = iter(["enter", "Jane Doe <jane@x.io>"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    _drive_cmd_tui(monkeypatch, tmp_path, ["tui"], owner_running=False)

    assert parse_env((tmp_path / ".env").read_text(encoding="utf-8")) == {
        "QLAB_NEWS_PROVIDERS": "edgar,macro",
        "QLAB_EDGAR_CONTACT": "Jane Doe <jane@x.io>"}
    assert os.environ["QLAB_EDGAR_CONTACT"] == "Jane Doe <jane@x.io>"


@pytest.mark.parametrize("argv,tty", [
    (["tui"], False),          # no terminal to ask on
    (["tui", "--offline"], True),   # the demo desk reads fixtures by design
    (["tui", "--yes"], True),       # scripted: never stop to ask
])
def test_the_startup_door_stays_quiet_when_it_must(monkeypatch, tmp_path, argv, tty):
    from qlab.autopilot import cli

    _door_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: tty)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": pytest.fail(f"the door asked anyway: {prompt}"))
    _drive_cmd_tui(monkeypatch, tmp_path, argv, owner_running=False)
    assert not (tmp_path / ".env").exists()


def test_a_scripted_setup_saves_the_stack_but_the_exit_code_follows_the_check(
        monkeypatch, tmp_path):
    """The caller named these providers; saving them is right, exit 0 is not.

    A CI-style caller reads the status, not the printout — reporting success
    for a stack whose only member is NOT WORKING is the silent failure this
    desk refuses everywhere else.
    """
    from qlab.autopilot import cli
    from qlab.env import parse_env

    monkeypatch.setenv("QLAB_WORKSPACE", str(tmp_path))
    _forget_env(monkeypatch, "QLAB_NEWS_PROVIDERS")
    _no_prompts(monkeypatch)
    monkeypatch.setattr(
        "qlab.news.check.check_news",
        lambda universe, provider=None, **kw: {
            "ok": False, "provider": provider,
            "members": {"macro": {"ok": False, "provider": "macro",
                                  "error": "HTTPError: 503"}}})

    assert cli.main(["news-setup", "--providers", "macro"]) == 1
    assert parse_env((tmp_path / ".env").read_text(encoding="utf-8")) == {
        "QLAB_NEWS_PROVIDERS": "macro"}


def test_the_verb_refuses_a_malformed_contact_in_this_files_own_voice(
        monkeypatch, tmp_path):
    from qlab.autopilot import cli

    monkeypatch.setenv("QLAB_WORKSPACE", str(tmp_path))
    _no_prompts(monkeypatch)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["news-setup", "--providers", "edgar",
                  "--edgar-contact", "Jane Doe", "--no-verify"])
    assert "@" in str(exit_info.value)
    assert not (tmp_path / ".env").exists()


def test_the_verb_turns_a_ctrl_d_into_a_sentence_not_a_traceback(
        monkeypatch, tmp_path):
    from qlab.autopilot import cli

    monkeypatch.setenv("QLAB_WORKSPACE", str(tmp_path))
    _forget_env(monkeypatch, "QLAB_NEWS_PROVIDERS")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": (_ for _ in ()).throw(
        EOFError()))
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["news-setup"])
    assert "nothing was" in str(exit_info.value).lower()
    assert not (tmp_path / ".env").exists()


def test_the_door_takes_a_ctrl_d_and_starts_the_desk_anyway(monkeypatch, tmp_path):
    """A launch that had nothing wrong with it is never aborted by the door."""
    from qlab.autopilot import cli

    _door_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt="": (_ for _ in ()).throw(
        EOFError()))
    record = _drive_cmd_tui(monkeypatch, tmp_path, ["tui"], owner_running=False)
    assert record["spawned"][1:4] == ["-m", "qlab.autopilot.cli", "owner"]
    assert not (tmp_path / ".env").exists()


def test_the_door_gives_up_on_the_wizard_without_taking_the_launch_with_it(
        monkeypatch, tmp_path):
    from qlab.autopilot import cli

    _door_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    # Yes to the offer, then three answers the wizard cannot read.
    answers = iter(["y", "maybe", "perhaps", "dunno"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    record = _drive_cmd_tui(monkeypatch, tmp_path, ["tui"], owner_running=False)
    assert record["spawned"][1:4] == ["-m", "qlab.autopilot.cli", "owner"]
    assert not (tmp_path / ".env").exists()


def test_the_wrong_word_at_the_edgar_door_re_asks_then_starts_as_configured(
        monkeypatch, tmp_path, capsys):
    from qlab.autopilot import cli

    _door_env(monkeypatch, tmp_path, providers="edgar,macro")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    asked: list[str] = []

    def ask(prompt=""):
        asked.append(prompt)
        return "wat"

    monkeypatch.setattr("builtins.input", ask)
    _drive_cmd_tui(monkeypatch, tmp_path, ["tui"], owner_running=False)

    assert len(asked) == 3, asked
    assert os.environ["QLAB_NEWS_PROVIDERS"] == "edgar,macro", (
        "a mistyped word must not quietly change the stack")
    assert not (tmp_path / ".env").exists()
    assert "as configured" in capsys.readouterr().out


def test_qlab_owner_never_reaches_the_door(monkeypatch, tmp_path):
    """Headless: there is nobody to ask, and a prompt would hang a service."""
    from qlab.autopilot import cli
    from qlab.ui import server as ui_server

    _door_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": pytest.fail(f"qlab owner prompted: {prompt}"))
    monkeypatch.setattr(ui_server, "serve", lambda **kwargs: None)
    monkeypatch.setattr(cli, "_publish_owner_port", lambda _port: None)
    assert cli.main(["owner", "--offline"]) == 0
    assert not (tmp_path / ".env").exists()
