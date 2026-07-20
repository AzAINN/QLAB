"""qlab.agents — orchestrator-agnostic agent definitions + adapters.

The declarative source of truth lives in the repo-root ``agents/*.md``. Thin
adapters emit Claude Code ``.claude/agents/`` files *and* IBM Bob personas from
that one source, so the same org chart runs on either orchestrator (research-plan
§0.1). Build with whichever you prefer; the submitted system runs on Bob.
"""

__all__ = ["AgentDef", "load_agents", "sync"]


def __getattr__(name):
    """Keep package import light and avoid preloading loader for ``python -m``."""
    if name in __all__:
        from qlab.agents import loader

        return getattr(loader, name)
    raise AttributeError(name)
