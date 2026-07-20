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


def _by_name():
    return {a.name: a for a in load_agents()}


def _generated_claude_tool_ids(md_path: Path) -> list[str]:
    """The mcp tool identifiers listed in a generated Claude adapter's tools."""
    text = md_path.read_text(encoding="utf-8")
    _, fm, _ = text.split("---", 2)
    meta = yaml.safe_load(fm) or {}
    raw = meta.get("tools", "") or ""
    return [t.strip() for t in raw.split(",") if t.strip()]


def test_all_five_roles_present():
    agents = _by_name()
    assert set(agents) == {"moments-analyst", "challenger", "optimization-runner",
                           "referee", "reporter"}


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
    written = sync(claude_out=tmp_path / "claude", bob_out=tmp_path / "bob")
    assert len(written["claude"]) == 5
    assert len(written["bob"]) == 5
    assert (tmp_path / "claude" / "referee.md").exists()
    assert (tmp_path / "bob" / "referee.yaml").exists()
