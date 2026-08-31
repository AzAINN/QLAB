"""`qlab cli` and `qlab build` — the real Claude CLI, opened from the desk.

Two argv builders and two refusals, all pure: what reaches `execvp` is exactly
what these functions return, so the authority each hand-off carries is a value
a test can read rather than a claim in a docstring. `cli` grants the owner-backed
proxy tools plus read-only web and nothing else; `build` grants Claude Code's own
defaults and leans on its own interactive permission prompts, because the
operator is sitting in front of it.
"""

from __future__ import annotations

import json

import pytest

from qlab.tui import claude as cc


# -- qlab cli: Claude wearing the Atlas persona -----------------------------

def test_the_atlas_cli_argv_is_exactly_this():
    argv = cc.build_atlas_cli_argv(
        runtime_url="http://127.0.0.1:8765/", offline=True)
    assert argv == [
        "claude",
        "--strict-mcp-config",
        "--mcp-config", json.dumps(cc.proxy_mcp_config(
            "http://127.0.0.1:8765", offline=True)),
        "--tools", "WebSearch,WebFetch",
        "--allowedTools", ",".join([*cc._PROXY_TOOLS, "WebSearch", "WebFetch"]),
        "--append-system-prompt", cc.atlas_persona(),
    ]


def test_the_cli_grants_the_proxy_tools_and_read_only_web_and_nothing_else():
    argv = cc.build_atlas_cli_argv(
        runtime_url="http://127.0.0.1:8765", offline=False)
    granted = argv[argv.index("--allowedTools") + 1].split(",")
    assert set(granted) == {*cc._PROXY_TOOLS, "WebSearch", "WebFetch"}
    # The tool *universe*, not just the allowlist: an allowlist narrows what is
    # pre-approved, while `--tools` is what exists to be prompted for at all.
    universe = argv[argv.index("--tools") + 1].split(",")
    assert universe == ["WebSearch", "WebFetch"]
    for forbidden in ("Bash", "Write", "Edit", "Read", "Agent", "Task"):
        assert forbidden not in universe
        assert forbidden not in granted


def test_the_cli_speaks_to_the_same_proxy_the_workforce_writes():
    # One config, not a second copy that can drift: the workforce's governed
    # argv and this one must name the same server, command and environment.
    governed = cc.build_claude_argv(
        "goal", governed=True, runtime_url="http://127.0.0.1:9", offline=True)
    mine = cc.build_atlas_cli_argv(
        runtime_url="http://127.0.0.1:9", offline=True)
    assert (json.loads(governed[governed.index("--mcp-config") + 1])
            == json.loads(mine[mine.index("--mcp-config") + 1]))


def test_the_cli_carries_the_desk_managers_own_persona():
    argv = cc.build_atlas_cli_argv(
        runtime_url="http://127.0.0.1:8765", offline=True)
    persona = argv[argv.index("--append-system-prompt") + 1]
    assert persona == cc.atlas_persona()
    assert "Atlas" in persona and persona.strip()


def test_the_cli_is_interactive_and_never_a_headless_print_run():
    argv = cc.build_atlas_cli_argv(
        runtime_url="http://127.0.0.1:8765", offline=True)
    for headless in ("--print", "--output-format", "--permission-mode"):
        assert headless not in argv


# -- qlab build: Claude Code on the repo ------------------------------------

def test_the_builder_argv_is_exactly_this():
    assert cc.build_builder_argv("add a heatmap visual") == [
        "claude",
        "--append-system-prompt", cc.builder_brief(),
        "--", "add a heatmap visual",
    ]


def test_the_builder_gets_claude_codes_own_tools_and_own_prompts():
    argv = cc.build_builder_argv("do a thing")
    # No allowlist, no MCP config, and above all no permission mode: the
    # operator answering Claude Code's own prompts IS the gate here.
    for narrowing in ("--allowedTools", "--tools", "--mcp-config",
                      "--strict-mcp-config", "--permission-mode"):
        assert narrowing not in argv


def test_the_builder_brief_says_where_a_visual_goes_and_how_to_rebuild():
    brief = cc.builder_brief()
    assert "qlab/visuals/" in brief
    assert "TITLE" in brief and "render(params)" in brief
    assert "cd clients/atlas-tui && cargo build --release" in brief
    assert "qlab --restart runtime" in brief
    # The conventions a build must not break, named rather than assumed.
    assert "one DuckDB writer" in brief.lower() or "one duckdb writer" in brief.lower()
    assert "qlab/paths.py" in brief


def test_a_request_that_opens_with_a_dash_is_still_a_request():
    argv = cc.build_builder_argv("--help me name this")
    assert argv[-2:] == ["--", "--help me name this"]


def test_an_empty_request_is_refused_rather_than_opening_an_aimless_session():
    with pytest.raises(ValueError):
        cc.build_builder_argv("   ")


# -- the two refusals -------------------------------------------------------

def test_an_absent_claude_binary_refuses_with_a_named_remedy(monkeypatch):
    from qlab.autopilot import cli as verbs

    monkeypatch.setattr(cc, "resolve_claude_executable", lambda: None)
    for verb, args in (
        (verbs._cmd_cli, verbs.build_parser().parse_args(["cli"])),
        (verbs._cmd_build, verbs.build_parser().parse_args(["build", "x"])),
    ):
        with pytest.raises(SystemExit) as exc:
            verb(args)
        said = str(exc.value)
        assert "claude" in said.lower()
        assert "npm install -g @anthropic-ai/claude-code" in said


def test_a_desk_with_no_owner_refuses_the_cli_by_name(monkeypatch):
    from qlab.autopilot import cli as verbs

    monkeypatch.setattr(cc, "resolve_claude_executable", lambda: "/bin/claude")
    monkeypatch.setattr(verbs, "_owner_answers", lambda url: False)
    args = verbs.build_parser().parse_args(["cli", "--port", "8799"])
    with pytest.raises(SystemExit) as exc:
        verbs._cmd_cli(args)
    said = str(exc.value)
    assert "8799" in said
    assert "qlab owner" in said


def test_the_builder_needs_no_owner_at_all(monkeypatch):
    # A build edits the checkout; it has nothing to ask a runtime. Probing one
    # would make "the desk is down" a reason not to fix the desk.
    from qlab.autopilot import cli as verbs

    seen: list[list[str]] = []
    monkeypatch.setattr(cc, "resolve_claude_executable", lambda: "/bin/claude")
    monkeypatch.setattr(verbs, "_owner_answers",
                        lambda url: pytest.fail("the builder probed the owner"))
    monkeypatch.setattr(verbs, "_run_interactive",
                        lambda argv, cwd: seen.append(list(argv)) or 0)
    args = verbs.build_parser().parse_args(["build", "add a visual"])
    assert verbs._cmd_build(args) == 0
    assert seen[0][0] == "/bin/claude"
    assert seen[0][-1] == "add a visual"


def test_the_cli_runs_the_resolved_binary_in_the_checkout(monkeypatch):
    from qlab.autopilot import cli as verbs
    from qlab.paths import workspace_root

    seen: list[tuple[list[str], object]] = []
    monkeypatch.setattr(cc, "resolve_claude_executable", lambda: "/bin/claude")
    monkeypatch.setattr(verbs, "_owner_answers", lambda url: True)
    monkeypatch.setattr(
        verbs, "_run_interactive",
        lambda argv, cwd: seen.append((list(argv), cwd)) or 3)
    args = verbs.build_parser().parse_args(["cli", "--port", "8765"])
    assert verbs._cmd_cli(args) == 3
    argv, cwd = seen[0]
    assert argv[0] == "/bin/claude"
    assert cwd == workspace_root()


# -- the verbs exist --------------------------------------------------------

def test_both_verbs_are_registered_on_the_desk_parser():
    from qlab.autopilot import cli as verbs

    parser = verbs.build_parser()
    assert parser.parse_args(["cli"]).func is verbs._cmd_cli
    built = parser.parse_args(["build", "add a heatmap"])
    assert built.func is verbs._cmd_build
    assert built.request == "add a heatmap"
