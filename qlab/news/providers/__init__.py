"""First-party news providers: stdlib-only, keyless, registered by name."""

from __future__ import annotations


def register_first_party(registry: dict) -> None:
    """Add the first-party providers to ``feed.PROVIDERS``.

    Called from ``feed.py`` at import, after every name the provider modules
    import from it exists — these modules import ``feed``, so registering any
    earlier closes the cycle on a half-built module.
    """
    from qlab.news.providers import edgar, macro

    registry.update({"edgar": edgar.fetch, "macro": macro.fetch})
