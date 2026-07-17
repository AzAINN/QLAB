"""qlab.agents — orchestrator-agnostic agent definitions + adapters.

The declarative source of truth lives in the repo-root ``agents/*.md``. Thin
adapters emit Claude Code ``.claude/agents/`` files *and* IBM Bob personas from
that one source, so the same org chart runs on either orchestrator (research-plan
§0.1). Build with whichever you prefer; the submitted system runs on Bob.
"""

from qlab.agents.loader import AgentDef, load_agents, sync

__all__ = ["AgentDef", "load_agents", "sync"]
