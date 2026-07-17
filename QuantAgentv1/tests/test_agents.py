"""Agent definitions: parsing, least-privilege tool scoping, adapter sync."""

from __future__ import annotations

from pathlib import Path

from qlab.agents.loader import load_agents, sync


def _by_name():
    return {a.name: a for a in load_agents()}


def test_all_five_roles_present():
    agents = _by_name()
    assert set(agents) == {"moments-analyst", "challenger", "optimization-runner",
                           "referee", "reporter"}


def test_least_privilege_separation():
    a = _by_name()
    # moments-analyst judges; it cannot run solvers
    assert not any("solve." in t for t in a["moments-analyst"].tools)
    # optimization-runner solves; it cannot author decisions or trade
    assert any("solve." in t for t in a["optimization-runner"].tools)
    assert not any("log_decision" in t for t in a["optimization-runner"].tools)
    assert "quant-trader" not in a["optimization-runner"].server_scopes
    # referee is read-only: no trader access, no solve.classical
    assert "quant-trader" not in a["referee"].server_scopes
    assert not any("propose_rebalance" in t or "execute_plan" in t
                   for t in a["referee"].tools)
    # reporter is the only role that can touch the execution gateway
    assert "quant-trader" in a["reporter"].server_scopes
    assert any("execute_plan" in t for t in a["reporter"].tools)


def test_no_agent_has_a_raw_order_tool():
    for a in load_agents():
        assert not any("place_order" in t or "place_stock_order" in t
                       for t in a.tools)


def test_sync_writes_both_adapters(tmp_path: Path):
    written = sync(claude_out=tmp_path / "claude", bob_out=tmp_path / "bob")
    assert len(written["claude"]) == 5
    assert len(written["bob"]) == 5
    assert (tmp_path / "claude" / "referee.md").exists()
    assert (tmp_path / "bob" / "referee.yaml").exists()
