"""Parse the neutral ``agents/*.md`` and emit orchestrator-specific adapters.

    python -m qlab.agents.loader sync    # write .claude/agents + .bob/personas
    python -m qlab.agents.loader list    # show the parsed org chart

The source files are Claude-compatible by construction (front-matter + system
prompt), so the Claude adapter is a validated copy; the Bob adapter re-expresses
the same fields as a persona YAML. Keeping one source prevents the two
orchestrators from drifting apart.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from qlab.paths import data_path, workspace_root

# The lab and trader tool namespaces now live in one combined MCP process
# (qlab.mcp.server) so a single DuckDB writer owns the book. Every persona
# therefore connects to this one runtime server; least-privilege is NOT a
# process boundary but the per-agent tool allowlist, split into lab vs trader
# *tool sets* by ``role_scopes`` (keyed off ``TRADER_TOOLS``, not a prefix).
SERVER_NAME = "qlab"

# The execution-gateway ("trader") tools, by base name. Every tool not in this
# set is a research-lab tool. This is the least-privilege boundary now that both
# roles share the one ``qlab`` server: a role whose tools intersect this set can
# move the paper book; a role that touches none of them is research-only.
TRADER_TOOLS = {
    "get_portfolio_state",
    "reconcile",
    "propose_rebalance",
    "execute_plan",
    "halt",
    "resume",
    "risk_report",
}


def tool_base_name(tool: str) -> str:
    """A tool's base name: the segment after the last ``__`` separator.

    ``mcp__qlab__solve.classical`` -> ``solve.classical``.
    """
    return tool.rsplit("__", 1)[-1]


def role_scopes(tools: Iterable[str]) -> dict[str, set[str]]:
    """Split tool base names into least-privilege roles.

    Returns ``{"lab": {...}, "trader": {...}}`` of base names. The split is by
    tool identity (``TRADER_TOOLS``), NOT by any server-name prefix: all tools
    are served by the single ``qlab`` process, so the prefix no longer carries
    role information.
    """
    lab: set[str] = set()
    trader: set[str] = set()
    for t in tools:
        base = tool_base_name(t)
        (trader if base in TRADER_TOOLS else lab).add(base)
    return {"lab": lab, "trader": trader}


@dataclass
class AgentDef:
    name: str
    description: str
    body: str
    tools: list[str] = field(default_factory=list)
    model: str = "inherit"

    @property
    def server_scopes(self) -> set[str]:
        """The runtime MCP servers this agent connects to (for ``mcp_servers``
        emission). Both lab and trader tools live in the one combined ``qlab``
        process, so any agent with tools connects to exactly ``{"qlab"}``.
        Least-privilege reasoning lives in ``role_scopes(self.tools)``, not in a
        server prefix.
        """
        return {SERVER_NAME} if self.tools else set()


def parse_agent(path: Path) -> AgentDef:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"{path} missing YAML front-matter")
    _, fm, body = text.split("---", 2)
    meta = yaml.safe_load(fm) or {}
    return AgentDef(
        name=meta.get("name", path.stem),
        description=meta.get("description", ""),
        body=body.strip(),
        tools=list(meta.get("tools", [])),
        model=meta.get("model", "inherit"),
    )


def load_agents(src: Path | None = None) -> list[AgentDef]:
    src = src or data_path("agents")
    return [parse_agent(p) for p in sorted(src.glob("*.md"))]


# ---------------------------------------------------------------------------
# adapters
# ---------------------------------------------------------------------------
def _write_claude(agent: AgentDef, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    fm = {"name": agent.name, "description": agent.description}
    if agent.model and agent.model != "inherit":
        fm["model"] = agent.model
    if agent.tools:
        # Claude Code sanitizes MCP tool names — a dotted registration like
        # ``data.fetch_universe`` is exposed as ``data_fetch_universe`` — so
        # grants must be emitted in the underscored form or they match nothing.
        # A built-in tool (`WebSearch`) carries no `__` prefix and is not an
        # MCP registration: emitting it through the sanitizer produced
        # `__WebSearch`, a name that matches nothing, which is exactly the
        # silent-drop shape `atlas_cli_tools` refuses loudly.
        claude_names = []
        for tool in agent.tools:
            prefix, sep, base = tool.rpartition("__")
            claude_names.append(
                f"{prefix}__{base.replace('.', '_')}" if sep else tool)
        fm["tools"] = ", ".join(claude_names)  # Claude accepts a comma list
    front = yaml.safe_dump(fm, sort_keys=False, default_flow_style=False).strip()
    path = out_dir / f"{agent.name}.md"
    path.write_text(f"---\n{front}\n---\n\n{agent.body}\n", encoding="utf-8")
    return path


def _write_bob(agent: AgentDef, out_dir: Path) -> Path:
    """Project a role into ``.bob/personas/`` — qlab's own neutral YAML.

    These keys (``persona``, ``allowed_tools``, ``system_prompt``) are qlab's,
    not IBM Bob's: the file proves the one-source org chart targets a second
    orchestrator, but Bob does not load it. Bob's loadable equivalent is
    ``.bob/custom_modes.yaml`` (role definition + instructions + a deterministic
    tool set, where the ``MCP`` group alone yields the no-filesystem/no-shell
    boundary every role here asserts). That generator attaches at this function;
    confirm the schema against a live Bob install before writing one — see
    planning-docs/2026-07-26-ibm-bob-integration-options.md. Bob's *runtime*
    connection is not this file at all: it is ``.bob/mcp.json`` -> the
    propose-only owner proxy, which needs no adapter.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    persona = {
        "persona": agent.name,
        "description": agent.description,
        "model": agent.model,
        "mcp_servers": sorted(agent.server_scopes),
        "allowed_tools": agent.tools,
        "system_prompt": agent.body,
        "governance": {
            "human_in_the_loop": agent.name in ("referee", "reporter"),
            "audit": True,
        },
    }
    path = out_dir / f"{agent.name}.yaml"
    path.write_text(yaml.safe_dump(persona, sort_keys=False), encoding="utf-8")
    return path


def sync(src: Path | None = None, claude_out: Path | None = None,
         bob_out: Path | None = None) -> dict:
    """Regenerate both adapter directories from the neutral source."""
    agents = load_agents(src)
    claude_out = claude_out or workspace_root() / ".claude" / "agents"
    bob_out = bob_out or workspace_root() / ".bob" / "personas"
    written = {"claude": [], "bob": []}
    for a in agents:
        written["claude"].append(str(_write_claude(a, claude_out)))
        written["bob"].append(str(_write_bob(a, bob_out)))
    return written


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    cmd = argv[0] if argv else "sync"
    if cmd == "list":
        for a in load_agents():
            roles = role_scopes(a.tools)
            print(f"{a.name:20} model={a.model:8} servers={sorted(a.server_scopes)} "
                  f"lab={len(roles['lab'])} trader={len(roles['trader'])} "
                  f"({len(a.tools)} tools)")
    elif cmd == "sync":
        written = sync()
        print(f"[qlab] synced {len(written['claude'])} Claude agents -> .claude/agents/")
        print(f"[qlab] synced {len(written['bob'])} Bob personas   -> .bob/personas/")
    else:
        print(f"unknown command {cmd!r}; use 'sync' or 'list'")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
