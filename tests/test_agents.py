"""Agent definitions: parsing, least-privilege tool scoping, adapter sync."""

from __future__ import annotations

from pathlib import Path

import yaml

from qlab.agents.loader import (
    TRADER_TOOLS,
    load_agents,
    role_scopes,
    sync,
    tool_base_name,
)

_GENERATED_CLAUDE = Path(__file__).resolve().parents[1] / ".claude" / "agents"
_GENERATED_BOB = Path(__file__).resolve().parents[1] / ".bob" / "personas"


def _by_name():
    return {a.name: a for a in load_agents()}


def _generated_claude_tool_ids(md_path: Path) -> list[str]:
    """The mcp tool identifiers listed in a generated Claude adapter's tools."""
    text = md_path.read_text(encoding="utf-8")
    _, fm, _ = text.split("---", 2)
    meta = yaml.safe_load(fm) or {}
    raw = meta.get("tools", "") or ""
    return [t.strip() for t in raw.split(",") if t.strip()]


def test_all_roles_present():
    agents = _by_name()
    assert set(agents) == {
        "news-extractor", "data-qa", "signal-qa", "moments-analyst", "challenger",
        "optimization-runner", "referee", "reporter", "atlas",
        "news-analyst", "contender-scout",
    }


def test_estimation_roles_define_the_bounded_debate_protocol():
    agents = _by_name()

    analyst = agents["moments-analyst"].body
    assert "DEBATE_FOLLOW_UP" in analyst
    assert "specific numbers" in analyst
    assert "NEW decision record" in analyst
    assert "never edit or overwrite the old decision" in analyst
    assert "do not continue into a third exchange" in analyst

    challenger = agents["challenger"].body
    assert "one focused counter-case" in challenger
    assert "maximum of two" in challenger
    assert "one rebuttal" in challenger and "round max" in challenger
    assert "There is no third challenge" in challenger
    assert "Never debate target weights, orders, or trades" in challenger

    referee = agents["referee"].body
    assert "Debate adjudication duty" in referee
    assert "window/shrinkage/regime" in referee
    assert "verdict reasons" in referee
    assert "which argument carried and why" in referee
    assert "never adjudicate target weights" in referee


def test_news_extractor_source_keeps_the_return_ban_and_dry_boundary():
    extractor = _by_name()["news-extractor"]
    assert extractor.tools == ["mcp__qlab__research.apply_views"]
    assert '"X will go up"' in extractor.body
    assert "refuse to emit a view" in extractor.body
    assert "at most three views" in extractor.body
    assert "source_quote" in extractor.body
    assert "confidence in `(0, 0.7]`" in extractor.body
    assert "dry=true" in extractor.body
    assert "condition a downstream moment set" in extractor.body


def test_least_privilege_separation():
    """Least-privilege keys off tool *base names* (TRADER_TOOLS), not the server
    prefix — every tool now lives behind the single ``qlab`` runtime server."""
    a = _by_name()

    # The quarantine has one typed construction call and no general reads,
    # registry writes, workflow authority, numeric engines, or book access.
    extractor = a["news-extractor"]
    assert extractor.tools == ["mcp__qlab__research.apply_views"]
    extractor_scopes = role_scopes(extractor.tools)
    assert extractor_scopes["lab"] == {"research.apply_views"}
    assert extractor_scopes["trader"] == set()

    # moments-analyst judges; it runs no solver and cannot touch the book.
    ma = role_scopes(a["moments-analyst"].tools)
    assert not any(base.startswith("solve.") for base in ma["lab"])
    assert ma["trader"] == set()

    # optimization-runner solves; it cannot author decisions and cannot trade.
    orun = role_scopes(a["optimization-runner"].tools)
    assert any(base.startswith("solve.") for base in orun["lab"])
    assert {"algorithms.list", "algorithms.describe", "algorithms.solve"} <= orun["lab"]
    assert not any("quantum" in base or "qubo_resource" in base for base in orun["lab"])
    assert "registry.log_decision" not in orun["lab"]
    assert orun["trader"] == set()

    # referee is read-only w.r.t. the book: no execution-gateway tools at all.
    assert role_scopes(a["referee"].tools)["trader"] == set()

    regime_reads = {
        "regime.turbulence",
        "regime.absorption",
        "regime.volatility_term_structure",
        "regime.drawdown",
        "regime.tail_risk",
    }
    expected_qa_tools = {
        "data-qa": {
            "data.snapshot_summary",
            "qa.data_integrity",
            "registry.log_decision",
            *regime_reads,
        },
        "signal-qa": {
            "research.window_evidence",
            "registry.list_runs",
            "registry.report",
            "registry.log_decision",
            *regime_reads,
        },
    }
    for name, expected in expected_qa_tools.items():
        scopes = role_scopes(a[name].tools)
        assert scopes["lab"] == expected
        assert scopes["trader"] == set()
        assert "registry.log_verdict" not in scopes["lab"]
        assert "backtest.run" not in scopes["lab"]
        assert "algorithms.solve" not in scopes["lab"]
        assert not any(base.startswith(("solve.", "workflow."))
                       for base in scopes["lab"])

    # Only the reporter and Atlas touch the execution gateway at all, and only
    # the reporter may propose. Atlas's trader tools are strictly read-only: the
    # desk manager observes the book and can never act on it.
    with_trader = {name for name, ag in a.items()
                   if role_scopes(ag.tools)["trader"]}
    assert with_trader == {"reporter", "atlas"}
    reporter_trader = role_scopes(a["reporter"].tools)["trader"]
    assert reporter_trader and reporter_trader <= TRADER_TOOLS
    assert "propose_rebalance" in reporter_trader
    assert "execute_plan" not in reporter_trader

    bob_trader = role_scopes(a["atlas"].tools)["trader"]
    assert bob_trader == {"get_portfolio_state", "risk_report"}
    for forbidden in ("propose_rebalance", "execute_plan", "reconcile",
                      "halt", "resume"):
        assert forbidden not in bob_trader


def test_atlas_source_states_its_authority_boundaries():
    """Atlas's own definition must say what it cannot do, in its own words.

    The code already refuses execution structurally; the prompt must not imply
    otherwise, or a reader (or the model) will describe authority it lacks.
    """
    atlas = _by_name()["atlas"].body
    assert "You do not trade" in atlas
    assert "no execution tool and no proposal tool" in atlas
    assert "cannot create, approve, or consume one" in atlas
    assert "You do not compute" in atlas
    assert "You do not forecast returns" in atlas
    assert "You do not overrule the referee" in atlas
    # It may only name registered templates.
    assert "registered" in atlas and "desk_rebalance_review" in atlas
    # Degraded inputs must be reported, not smoothed over.
    assert "Report degraded state honestly" in atlas
    assert "uncertain" in atlas


def test_no_role_has_a_raw_order_tool():
    """No role may hold any tool whose base name references an order."""
    for ag in load_agents():
        for t in ag.tools:
            assert "order" not in tool_base_name(t), (ag.name, t)


def test_no_role_can_invoke_an_offline_algorithm_tool():
    for agent in load_agents():
        bases = {tool_base_name(tool) for tool in agent.tools}
        assert "solve.quantum" not in bases
        assert "solve.qubo_resource_count" not in bases


def test_generated_claude_adapters_use_qlab_prefix():
    """Every mcp tool identifier in every generated .claude adapter must be
    namespaced under the single runtime server ``mcp__qlab__``. This locks the
    generated adapters against drift back to the retired ``quant-lab`` /
    ``quant-trader`` prefixes (which resolve to zero live MCP tools)."""
    files = sorted(_GENERATED_CLAUDE.glob("*.md"))
    assert files, "no generated .claude/agents adapters found"
    for md in files:
        tool_ids = _generated_claude_tool_ids(md)
        assert tool_ids, f"{md.name} has no tools front-matter"
        for tool_id in tool_ids:
            if tool_id.startswith("mcp__"):
                assert tool_id.startswith("mcp__qlab__"), (md.name, tool_id)


def test_sync_writes_both_adapters(tmp_path: Path):
    claude_out = tmp_path / "claude"
    bob_out = tmp_path / "bob"
    written = sync(claude_out=claude_out, bob_out=bob_out)
    assert len(written["claude"]) == 11
    assert len(written["bob"]) == 11
    assert (claude_out / "news-extractor.md").exists()
    assert (bob_out / "news-extractor.yaml").exists()
    assert (claude_out / "data-qa.md").exists()
    assert (bob_out / "data-qa.yaml").exists()
    assert (claude_out / "signal-qa.md").exists()
    assert (bob_out / "signal-qa.yaml").exists()
    assert (claude_out / "referee.md").exists()
    assert (bob_out / "referee.yaml").exists()

    for generated in sorted(claude_out.glob("*.md")):
        checked_in = _GENERATED_CLAUDE / generated.name
        assert generated.read_text(encoding="utf-8") == checked_in.read_text(
            encoding="utf-8"
        ), f"{checked_in} is out of sync with agents/"
    for generated in sorted(bob_out.glob("*.yaml")):
        checked_in = _GENERATED_BOB / generated.name
        assert generated.read_text(encoding="utf-8") == checked_in.read_text(
            encoding="utf-8"
        ), f"{checked_in} is out of sync with agents/"


def test_news_analyst_is_quarantined_and_forecasts_nothing():
    """The news role interprets a supplied window and nothing else.

    It must hold no feed access (the grounded window is handed to it, so every
    record it reasons over has auditable provenance) and must never emit a
    price view — the desk forecasts no returns by design.
    """
    analyst = _by_name()["news-analyst"]
    scopes = role_scopes(analyst.tools)
    assert scopes["trader"] == set()
    assert scopes["lab"] <= {"registry.recent_decisions", "registry.log_decision"}
    assert not any("research.apply_views" in t for t in analyst.tools)

    body = analyst.body
    assert "may not recall events from memory" in body
    assert "may not fetch anything" in body
    assert "No price views, no directions, no weights" in body
    assert "No sentiment scores" in body
    assert "Uncertainty is an answer" in body
    # Support tiers must be respected, not re-derived by article count.
    assert "Primary source" in body and "Single secondary" in body
    assert "one claim, not five confirmations" in body


def test_the_matrix_is_readable_by_the_roles_that_reason_from_the_record():
    agents = _by_name()
    for role in ("atlas", "moments-analyst"):
        assert "mcp__qlab__research.qualitative_matrix" in agents[role].tools


def test_no_role_may_condition_a_moment_set_until_promotion():
    """`views_conditioned_min_variance` is research-stage: the conditioning
    tool stays off every role's list until the catalog entry is operational,
    so widening a role cannot widen what reaches a governed solve."""
    agents = _by_name()
    holders = {name for name, a in agents.items()
               if "mcp__qlab__moments.condition" in a.tools}
    assert holders == set()


def test_the_moments_analyst_reads_the_matrix_and_reaches_no_trader_tool():
    """The record's counts are context for the regime call; the role that
    reads them must still be unable to move the book."""
    agents = _by_name()
    scopes = role_scopes(agents["moments-analyst"].tools)
    assert "research.qualitative_matrix" in scopes["lab"]
    assert "moments.condition" not in scopes["lab"]
    assert scopes["trader"] == set()


def test_atlas_holds_exactly_the_four_new_desk_manager_tools():
    """The desk manager starts its own research — and still cannot book.

    The list is asserted whole rather than by membership: a tool quietly added
    to the persona is authority nobody reviewed, and the point of this task was
    that Atlas gained four named abilities and no others.
    """
    atlas = _by_name()["atlas"]
    assert set(atlas.tools) == {
        "mcp__qlab__policy.current",
        "mcp__qlab__registry.report",
        "mcp__qlab__registry.list_runs",
        "mcp__qlab__registry.recent_decisions",
        "mcp__qlab__registry.log_decision",
        "mcp__qlab__get_portfolio_state",
        "mcp__qlab__risk_report",
        "mcp__qlab__regime_turbulence",
        "mcp__qlab__regime_absorption",
        "mcp__qlab__regime_volatility_term_structure",
        "mcp__qlab__regime_drawdown",
        "mcp__qlab__regime_tail_risk",
        "mcp__qlab__research.predictor_board",
        "mcp__qlab__research.qualitative_matrix",
        "mcp__qlab__workflow.start",
        "mcp__qlab__workflow.resume",
        "mcp__qlab__atlas.task.create",
        "mcp__qlab__approvals.list",
    }
    scopes = role_scopes(atlas.tools)
    # Starting research is not touching the book: the trader scope is unchanged.
    assert scopes["trader"] == {"get_portfolio_state", "risk_report"}
    assert "execute_plan" not in scopes["lab"]
    assert "propose_rebalance" not in scopes["lab"]

    # And the four new grants are grants of something that EXISTS. "not a
    # trader tool" is satisfied by a name nothing serves, which is how a grant
    # silently disappears rather than being refused.
    from qlab.mcp.tui_proxy import register_proxy_tools

    class _Recorder:
        def __init__(self):
            self.names: list[str] = []

        def tool(self, *, name):
            def register(fn):
                self.names.append(name)
                return fn

            return register

    proxy = _Recorder()
    register_proxy_tools(proxy, object())
    for base in ("workflow.start", "workflow.resume", "atlas.task.create",
                 "approvals.list"):
        assert base.replace(".", "_") in proxy.names, base
        assert base in scopes["lab"]


def test_the_atlas_persona_says_it_starts_work_and_never_books():
    # Whitespace-normalized: the sentence is wrapped in the source, and a test
    # that pinned the wrapping would fail on a reflow that changed nothing.
    body = " ".join(_by_name()["atlas"].body.split())
    assert ("You create and run research workflows yourself and say what you "
            "started; you never book — booking is the one click the operator "
            "makes.") in body


def test_contender_scout_holds_exactly_web_and_two_registry_tools():
    """The scout is the one role with eyes outside this desk. Its grant is the
    whole quarantine: it reads the web and writes one memo, and it holds no
    data, solver, news or trader tool — so a contender it likes cannot become
    a weight, a size, or an order by any path it can reach."""
    scout = _by_name()["contender-scout"]
    assert scout.tools == [
        "WebSearch",
        "WebFetch",
        "mcp__qlab__registry.recent_decisions",
        "mcp__qlab__registry.log_decision",
    ]
    scopes = role_scopes(scout.tools)
    assert scopes["trader"] == set()
    assert scopes["lab"] == {"WebSearch", "WebFetch",
                             "registry.recent_decisions", "registry.log_decision"}
    body = scout.body
    assert "scout_memo" in body
    assert "no weight" in body.lower()


def test_the_scout_memo_contract_is_in_the_prompt():
    body = _by_name()["contender-scout"].body
    assert "up to 3" in body.lower()
    assert "two sentences" in body.lower()
    assert "URL" in body


def test_web_tools_are_granted_only_to_the_scout_role():
    from qlab.tui.claude import build_workforce_agents

    agents = build_workforce_agents("watch the holdings and scout contenders")
    scout = agents["contender-scout"]["tools"]
    assert "WebSearch" in scout and "WebFetch" in scout
    assert "mcp__qlab-operator__workflow_scout" in scout
    for name, spec in agents.items():
        if name == "contender-scout":
            continue
        assert "WebSearch" not in spec["tools"]
        assert "WebFetch" not in spec["tools"]


def test_the_coordinator_argv_opens_the_web_only_for_a_scout_graph():
    """One `--allowedTools` list serves the whole dispatch, so the web is the
    template's own property: a regime review never gets it."""
    from qlab.state.registry import agent_for_phase
    from qlab.operator.templates import get_template
    from qlab.tui.claude import build_claude_argv

    def allowed(template_id: str) -> list[str]:
        roles = tuple(agent_for_phase(phase)
                      for phase in get_template(template_id).phases)
        argv = build_claude_argv("go", governed=True,
                                 runtime_url="http://127.0.0.1:1", offline=True,
                                 roles=roles)
        return argv[argv.index("--allowedTools") + 1].split(",")

    watch = allowed("portfolio_watch")
    assert "WebSearch" in watch and "WebFetch" in watch
    review = allowed("regime_review")
    assert "WebSearch" not in review and "WebFetch" not in review
    # Nothing else was widened: the web is the only difference.
    assert set(watch) - set(review) == {"WebSearch", "WebFetch"}


def test_the_scouts_built_grant_is_exactly_its_six_names():
    """The full set, not a membership check: a tool that appeared here without
    being noticed is the whole risk of the one role with web access."""
    from qlab.tui.claude import build_workforce_agents

    agents = build_workforce_agents("watch the holdings and scout contenders")
    assert agents["contender-scout"]["tools"] == [
        "WebSearch",
        "WebFetch",
        "mcp__qlab-operator__registry_recent_decisions",
        "mcp__qlab-operator__registry_log_decision",
        "mcp__qlab-operator__workflow_scout",
        "mcp__qlab-operator__workflow_status",
    ]


def test_the_coordinator_does_not_browse_and_walks_the_persisted_graph():
    """The session allowlist carries the web on a watch run, so the coordinator
    can reach it; the prompt is what says the web belongs to the scout."""
    from qlab.tui.claude import build_workforce_agents

    prompt = build_workforce_agents("watch the holdings")["qlab-coordinator"]["prompt"]
    assert "the web belongs to the scout phase; you do not browse" in prompt
    assert "dispatch exactly the phases workflow_status lists" in prompt
    assert "each step's `agent`" in prompt


def test_atlas_can_name_every_registered_template():
    """The desk manager may start only registered templates, so its own list
    has to be the registry's — a template missing from the prose is one Atlas
    never offers."""
    from qlab.operator.templates import TEMPLATES

    body = _by_name()["atlas"].body
    for template_id in TEMPLATES:
        assert f"`{template_id}`" in body, template_id
