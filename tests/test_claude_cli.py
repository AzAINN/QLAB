"""`qlab cli` and `qlab build` — the real Claude CLI, opened from the desk.

Two argv builders and two refusals, all pure: what reaches `execvp` is exactly
what these functions return, so the authority each hand-off carries is a value
a test can read rather than a claim in a docstring. `cli` grants the atlas role's own
tools plus read-only web and nothing else; `build` grants Claude Code's own
defaults and leans on its own interactive permission prompts, because the
operator is sitting in front of it.
"""

from __future__ import annotations

import json
import os

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
        "--allowedTools", ",".join(cc.atlas_cli_tools()),
        "--append-system-prompt", cc.atlas_persona(),
    ]


def test_the_cli_grants_the_atlas_grant_and_read_only_web_and_nothing_else():
    argv = cc.build_atlas_cli_argv(
        runtime_url="http://127.0.0.1:8765", offline=False)
    granted = argv[argv.index("--allowedTools") + 1].split(",")
    assert set(granted) == set(cc.atlas_cli_tools())
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


# -- what `qlab cli` may actually reach -------------------------------------
#
# The first cut allowlisted `_PROXY_TOOLS` — the union of every workforce
# role's grant — with only the persona in front of it. A persona is not a gate:
# it is text the model may decline to follow, and that list carries
# `workflow.referee` (mint a PASS), `registry.log_verdict` (write one down) and
# `algorithms.solve`. Atlas's authority is now derived from the same file the
# persona comes from, which is the whole point of invariant 5.

def _atlas_role_tools():
    from qlab.agents.loader import load_agents

    return next(s for s in load_agents() if s.name == "atlas").tools


def test_the_cli_grants_the_atlas_roles_own_tools_and_not_the_workforces():
    argv = cc.build_atlas_cli_argv(
        runtime_url="http://127.0.0.1:8765", offline=True)
    granted = set(argv[argv.index("--allowedTools") + 1].split(","))

    # The referee's two tools are the sharp end: one mints a PASS and the other
    # persists it, and a PASS is what a fill is bound to.
    for forbidden in ("workflow_referee", "registry_log_verdict",
                      "algorithms_solve", "backtest_run",
                      "research_apply_views"):
        assert f"mcp__qlab-operator__{forbidden}" not in granted, forbidden

    # And what the role does name is all there.
    for tool in _atlas_role_tools():
        resolved = cc._proxy_tool(tool)
        assert resolved in granted, f"{tool} -> {resolved}"

    assert {"WebSearch", "WebFetch"} <= granted
    # Nothing else at all: the set is exactly the role plus the web.
    assert granted == {
        *(cc._proxy_tool(t) for t in _atlas_role_tools()),
        "WebSearch", "WebFetch",
    }


def test_a_role_tool_the_proxy_cannot_serve_refuses_rather_than_vanishes():
    assert cc._proxy_tool("mcp__qlab__not.a.tool") is None
    from qlab.agents.loader import AgentDef

    bogus = AgentDef(name="atlas", description="", body="x",
                     tools=["mcp__qlab__not.a.tool"])
    monkey = lambda: [bogus]  # noqa: E731
    import qlab.agents.loader as loader

    original = loader.load_agents
    loader.load_agents = monkey
    try:
        with pytest.raises(RuntimeError, match="not.a.tool"):
            cc.build_atlas_cli_argv(
                runtime_url="http://127.0.0.1:8765", offline=True)
    finally:
        loader.load_agents = original


# -- the port the desk is actually on ---------------------------------------

def test_the_cli_verb_defaults_to_the_port_the_launcher_published(monkeypatch):
    from qlab.autopilot import cli as verbs

    monkeypatch.setenv("QLAB_UI_PORT", "9000")
    assert verbs.build_parser().parse_args(["cli"]).port == 9000
    # And an explicit flag still wins over the environment.
    assert verbs.build_parser().parse_args(["cli", "--port", "8123"]).port == 8123


def test_without_a_published_port_the_cli_verb_falls_back_to_the_default(monkeypatch):
    from qlab.autopilot import cli as verbs

    monkeypatch.delenv("QLAB_UI_PORT", raising=False)
    assert verbs.build_parser().parse_args(["cli"]).port == 8765


# -- which checkout `/build` opens ------------------------------------------

def test_the_workstations_env_names_the_launcher_that_started_it():
    # Without this the Rust client spawns whatever `qlab` PATH resolves, which
    # on a machine with a pipx install and a checkout is reliably the wrong
    # one — `/build` would open Claude Code on a different tree than the
    # binary was built from.
    from qlab.autopilot import cli as verbs

    env = verbs._client_env(8765)
    assert env["QLAB_UI_PORT"] == "8765"
    assert os.path.isfile(env["QLAB_BIN"]), env["QLAB_BIN"]
    assert os.path.isabs(env["QLAB_BIN"]), env["QLAB_BIN"]


def test_a_launcher_that_cannot_name_itself_leaves_the_path_fallback_alone(monkeypatch):
    from qlab.autopilot import cli as verbs

    monkeypatch.setattr(verbs.sys, "argv", ["", "tui"])
    monkeypatch.setattr(verbs.shutil, "which", lambda name: None)
    env = verbs._client_env(8765)
    # Absent, not empty: the Rust side treats a blank QLAB_BIN as "not set"
    # too, but an env var that exists and names nothing is a claim this
    # launcher cannot make.
    assert "QLAB_BIN" not in env


# -- the restart offer's one moving part ------------------------------------

def test_a_rename_into_the_desks_trees_counts_and_a_quoted_path_is_unquoted(
        monkeypatch):
    from qlab.autopilot import cli as verbs

    def porcelain(out: str):
        class Done:
            stdout = out
        return lambda *a, **k: Done()

    # A rename: the destination is what was written, so `R  old -> new` counts
    # when `new` is under a desk tree even though `old` is not.
    monkeypatch.setattr(verbs.subprocess, "run",
                        porcelain("R  docs/old.py -> qlab/visuals/new.py\n"))
    assert verbs._desk_sources_changed() is True

    # ...and does not count when only the source was.
    monkeypatch.setattr(verbs.subprocess, "run",
                        porcelain("R  qlab/old.py -> docs/new.py\n"))
    assert verbs._desk_sources_changed() is False

    # A path with a space is quoted by porcelain v1; the quotes are not part
    # of it.
    monkeypatch.setattr(verbs.subprocess, "run",
                        porcelain('?? "qlab/visuals/my chart.py"\n'))
    assert verbs._desk_sources_changed() is True

    monkeypatch.setattr(verbs.subprocess, "run",
                        porcelain(' M planning-docs/notes.md\n'))
    assert verbs._desk_sources_changed() is False


# -- one mapper, no second lookup -------------------------------------------

def test_every_atlas_role_tool_resolves_through_the_one_mapper():
    """`_proxy_tool` alone answers for all eighteen, after the source fix.

    The workaround it replaces (`role_proxy_tool`, a second lookup against the
    union of what this module grants anywhere) existed because nine of atlas's
    tools were spelled wrong in the role file or missing from the table. Both
    are fixed at source, so the fallback is gone — and its absence is asserted,
    because a resolver that answers "yes" two different ways is a resolver
    whose authority is the union of two lists nobody reads together.
    """
    for tool in _atlas_role_tools():
        assert cc._proxy_tool(tool) is not None, tool
    assert not hasattr(cc, "role_proxy_tool")
    assert not hasattr(cc, "_KNOWN_PROXY_TOOLS")


def test_the_five_regime_tools_project_to_the_names_claude_sees():
    """The respell must not move the Claude-visible name by one character."""
    for base in ("regime.turbulence", "regime.absorption",
                 "regime.volatility_term_structure", "regime.drawdown",
                 "regime.tail_risk"):
        assert (cc._proxy_tool(f"mcp__qlab__{base}")
                == f"mcp__qlab-operator__{base.replace('.', '_')}")


# -- the rights file --------------------------------------------------------

def _write_rights(payload):
    from qlab.paths import state_path

    path = state_path("atlas_rights.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload),
                    encoding="utf-8")
    return path


def test_a_desk_that_never_set_rights_has_all_three():
    from qlab.paths import state_path

    assert not state_path("atlas_rights.json").exists()
    assert cc.load_atlas_rights() == {"web": True, "workflows": True,
                                      "build": True}
    assert cc.load_atlas_rights() == cc.ATLAS_RIGHTS_DEFAULTS
    assert set(cc.ATLAS_RIGHTS_KEYS) == {"web", "workflows", "build"}


def test_rights_written_to_the_file_are_what_comes_back():
    _write_rights({"web": False, "workflows": True, "build": False})
    assert cc.load_atlas_rights() == {"web": False, "workflows": True,
                                      "build": False}


def test_a_corrupt_rights_file_refuses_by_name_with_the_remedy():
    path = _write_rights("{not json at all")
    with pytest.raises(RuntimeError) as err:
        cc.load_atlas_rights()
    assert str(path) in str(err.value)
    assert "delete it to restore the defaults" in str(err.value)


def test_an_unknown_right_refuses_rather_than_being_ignored():
    # A key nobody reads is an operator who believes they set something. The
    # panel writes exactly three names; anything else is a file this desk did
    # not write and cannot honour.
    path = _write_rights({"web": True, "workflows": True, "build": True,
                          "execute": True})
    with pytest.raises(RuntimeError) as err:
        cc.load_atlas_rights()
    assert "execute" in str(err.value)
    assert str(path) in str(err.value)
    assert "delete it to restore the defaults" in str(err.value)


def test_a_right_that_is_not_a_boolean_refuses():
    _write_rights({"web": "yes", "workflows": True, "build": True})
    with pytest.raises(RuntimeError, match="delete it to restore the defaults"):
        cc.load_atlas_rights()


# -- what the rights actually withdraw --------------------------------------

def test_without_the_web_right_neither_surface_offers_a_web_tool():
    _write_rights({"web": False, "workflows": True, "build": True})

    chat = cc.chat_tools()
    assert "WebSearch" not in chat and "WebFetch" not in chat

    argv = cc.build_atlas_cli_argv(
        runtime_url="http://127.0.0.1:8765", offline=True)
    # Absent from the tool *universe*, not merely un-allowlisted: `--tools`
    # is what decides whether the tool exists in the session at all.
    assert argv[argv.index("--tools") + 1] == ""
    assert "WebSearch" not in argv[argv.index("--allowedTools") + 1]
    assert "WebFetch" not in argv[argv.index("--allowedTools") + 1]


def test_with_the_web_right_both_surfaces_offer_it():
    _write_rights({"web": True, "workflows": True, "build": True})
    assert {"WebSearch", "WebFetch"} <= set(cc.chat_tools())
    argv = cc.build_atlas_cli_argv(
        runtime_url="http://127.0.0.1:8765", offline=True)
    assert argv[argv.index("--tools") + 1] == "WebSearch,WebFetch"


def test_without_the_workflows_right_the_chat_cannot_start_or_write_work():
    _write_rights({"web": True, "workflows": False, "build": True})
    chat = cc.chat_tools()
    for base in ("workflow.start", "workflow.resume", "atlas.task.create"):
        assert cc._claude_tool(base) not in chat, base
    # Reading what is waiting is not doing anything: `approvals.list` stays.
    assert cc._claude_tool("approvals.list") in chat
    # And the reading tools are untouched.
    assert cc._claude_tool("portfolio.state") in chat


def test_with_the_workflows_right_the_three_action_tools_are_offered():
    _write_rights({"web": True, "workflows": True, "build": True})
    chat = cc.chat_tools()
    for base in ("workflow.start", "workflow.resume", "atlas.task.create"):
        assert cc._claude_tool(base) in chat, base


def test_the_chat_argv_carries_exactly_the_rights_shaped_grant():
    _write_rights({"web": False, "workflows": False, "build": True})
    argv = cc.build_claude_argv(
        "hello", governed=False, runtime_url="http://127.0.0.1:8765",
        offline=True, chat=True)
    assert argv[argv.index("--allowedTools") + 1] == ",".join(cc.chat_tools())
    assert "workflow_start" not in argv[argv.index("--allowedTools") + 1]


def test_without_the_build_right_qlab_build_refuses_naming_the_panel(monkeypatch):
    from qlab.autopilot import cli as verbs

    _write_rights({"web": True, "workflows": True, "build": False})
    monkeypatch.setattr(cc, "resolve_claude_executable", lambda: "/usr/bin/claude")

    def never(*a, **k):
        raise AssertionError("a refused build must not open a session")

    monkeypatch.setattr(verbs, "_run_interactive", never)
    with pytest.raises(SystemExit) as err:
        verbs._cmd_build(type("A", (), {"request": "add a heatmap"})())
    message = str(err.value)
    assert "build" in message
    assert "rights" in message.lower()


def test_with_the_build_right_qlab_build_opens_as_before(monkeypatch):
    from qlab.autopilot import cli as verbs

    _write_rights({"web": True, "workflows": True, "build": True})
    monkeypatch.setattr(cc, "resolve_claude_executable", lambda: "/usr/bin/claude")
    monkeypatch.setattr(verbs, "_desk_sources_changed", lambda: False)
    seen = {}

    def record(argv, cwd):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(verbs, "_run_interactive", record)
    assert verbs._cmd_build(type("A", (), {"request": "add a heatmap"})()) == 0
    assert seen["argv"][0] == "/usr/bin/claude"


# -- fix round 1: the web the operator asked for, declared ------------------

def test_the_chat_prompt_states_the_web_boundary_only_when_the_web_is_granted():
    """A tool granted without a boundary is a tool with no boundary.

    `web: true` gives the desk chat WebSearch/WebFetch it did not have before
    this task, so the prompt has to say what the web is for and — more
    importantly — what a web finding may never become on this desk.
    """
    _write_rights({"web": True, "workflows": True, "build": True})
    granted = cc.chat_system_prompt()
    for phrase in ("read-only", "cite", "news"):
        assert phrase in granted.lower(), phrase
    # The boundary that matters: nothing off the web becomes a number here.
    assert "never as a weight, a size, or a price direction" in granted

    _write_rights({"web": False, "workflows": True, "build": True})
    withheld = cc.chat_system_prompt()
    assert "never as a weight, a size, or a price direction" not in withheld
    assert "WebSearch" not in withheld
    # Everything else about the desk is unchanged: the web paragraph is added,
    # not a different prompt.
    assert withheld in granted


def test_the_chat_agent_definition_carries_the_prompt_for_its_own_rights():
    _write_rights({"web": False, "workflows": True, "build": True})
    agent = cc._chat_agent()["qlab-desk"]
    assert agent["prompt"] == cc.chat_system_prompt()
    assert "WebSearch" not in agent["tools"]


# -- fix round 1: a corrupt rights file never crashes a launcher ------------

def test_a_corrupt_rights_file_leaves_the_chat_launcher_saying_the_remedy(
        monkeypatch):
    path = _write_rights("{not json")
    session = cc.ClaudeSession(lambda event: None,
                               runtime_url="http://127.0.0.1:8765",
                               offline=True)
    monkeypatch.setattr(cc, "resolve_claude_executable", lambda: "/usr/bin/claude")

    def never(*a, **k):
        raise AssertionError("nothing may be spawned on an unreadable rights file")

    monkeypatch.setattr(cc.subprocess, "Popen", never)
    # Refused, not raised: the owner thread that calls this has no handler, and
    # a traceback there is a desk whose chat is simply dead with no sentence.
    assert session.start("hello", chat=True) is False
    assert str(path) in session.last_error
    assert "delete it to restore the defaults" in session.last_error


def test_the_chat_launch_reads_the_rights_once_for_both_halves(monkeypatch):
    """One read per launch: the argv and the agent file must not disagree.

    A POST landing between two reads would otherwise produce a session whose
    allowlist and whose agent definition were built from different rights,
    which is a grant nobody chose.
    """
    reads = []
    real = cc.load_atlas_rights

    def counted():
        reads.append(1)
        return real()

    monkeypatch.setattr(cc, "load_atlas_rights", counted)
    monkeypatch.setattr(cc, "resolve_claude_executable", lambda: "/usr/bin/claude")

    def refuse_to_spawn(*a, **k):
        # Both readers — the argv's `chat_tools` and the agent definition's —
        # run before this, so a launch that dies here has still exercised the
        # pair. Nothing is spawned in a test.
        raise OSError("not spawning anything in a test")

    monkeypatch.setattr(cc.subprocess, "Popen", refuse_to_spawn)
    session = cc.ClaudeSession(lambda event: None,
                               runtime_url="http://127.0.0.1:8765",
                               offline=True)
    assert session.start("hello", chat=True) is False
    assert "not spawning" in session.last_error
    assert len(reads) == 1, reads


def test_the_verbs_refuse_a_corrupt_rights_file_rather_than_traceback(monkeypatch):
    from qlab.autopilot import cli as verbs

    _write_rights("{not json")
    monkeypatch.setattr(cc, "resolve_claude_executable", lambda: "/usr/bin/claude")
    monkeypatch.setattr(verbs, "_owner_answers", lambda url: True)
    monkeypatch.setattr(verbs, "_run_interactive", lambda *a, **k: 0)

    for call in (
        lambda: verbs._cmd_build(type("A", (), {"request": "x"})()),
        lambda: verbs._cmd_cli(type("A", (), {"port": 8765, "offline": True})()),
    ):
        with pytest.raises(SystemExit) as err:
            call()
        assert "delete it to restore the defaults" in str(err.value)
