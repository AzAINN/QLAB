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


def test_all_seven_roles_present():
    agents = _by_name()
    assert set(agents) == {
        "data-qa", "signal-qa", "moments-analyst", "challenger",
        "optimization-runner", "referee", "reporter",
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


def test_least_privilege_separation():
    """Least-privilege keys off tool *base names* (TRADER_TOOLS), not the server
    prefix — every tool now lives behind the single ``qlab`` runtime server."""
    a = _by_name()

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

    # reporter is the ONLY role whose tools intersect the execution gateway.
    with_trader = {name for name, ag in a.items()
                   if role_scopes(ag.tools)["trader"]}
    assert with_trader == {"reporter"}
    reporter_trader = role_scopes(a["reporter"].tools)["trader"]
    assert reporter_trader and reporter_trader <= TRADER_TOOLS
    assert "propose_rebalance" in reporter_trader
    assert "execute_plan" not in reporter_trader


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
    assert len(written["claude"]) == 7
    assert len(written["bob"]) == 7
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
