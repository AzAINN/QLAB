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
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "agents"
_CLAUDE_OUT = _REPO_ROOT / ".claude" / "agents"
_BOB_OUT = _REPO_ROOT / ".bob" / "personas"

# The lab and trader namespaces now live in one combined MCP process
# (qlab.mcp.server) so a single DuckDB writer owns the book. Every persona
# therefore connects to this one server; least-privilege still lives in the
# per-agent tool allowlist (``server_scopes`` distinguishes lab vs trader
# tools), not in a process boundary.
SERVER_NAME = "qlab"


@dataclass
class AgentDef:
    name: str
    description: str
    body: str
    tools: list[str] = field(default_factory=list)
    model: str = "inherit"

    @property
    def server_scopes(self) -> set[str]:
        """The MCP servers this agent may touch (from its tool allowlist)."""
        scopes = set()
        for t in self.tools:
            parts = t.split("__")
            if len(parts) >= 2:
                scopes.add(parts[1])
        return scopes


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
    src = src or _SRC
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
        fm["tools"] = ", ".join(agent.tools)  # Claude accepts a comma list
    front = yaml.safe_dump(fm, sort_keys=False, default_flow_style=False).strip()
    path = out_dir / f"{agent.name}.md"
    path.write_text(f"---\n{front}\n---\n\n{agent.body}\n", encoding="utf-8")
    return path


def _write_bob(agent: AgentDef, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    persona = {
        "persona": agent.name,
        "description": agent.description,
        "model": agent.model,
        "mcp_servers": [SERVER_NAME],
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
    claude_out = claude_out or _CLAUDE_OUT
    bob_out = bob_out or _BOB_OUT
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
            print(f"{a.name:20} model={a.model:8} servers={sorted(a.server_scopes)} "
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
