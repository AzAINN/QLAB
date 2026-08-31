"""A registry for what a build draws: dependency-free text renderers.

A research run produces numbers; the desk can already read those. What it
could not read is the *shape* of what was fitted. A visual is the smallest
honest answer: a module in this package that exposes ``TITLE: str`` and
``render(params: dict) -> str`` and returns plain text a terminal can print.

Discovery is explicit in the sense that matters — every module here is walked
and checked against the contract, and a module carrying half of it is refused
by name at discovery time rather than silently skipped. A visual that is
present but unlisted would be the same class of bug as a seam with no caller.

A renderer reads only its ``params`` dict. It never opens the registry, never
holds a handle, and never reaches the network: the route half supplies the
parameters of the last predictor run, and this package turns them into text.
"""

from __future__ import annotations

import pkgutil
from dataclasses import dataclass
from importlib import import_module
from types import ModuleType
from typing import Callable

__all__ = ["VisualSpec", "catalog", "render"]


@dataclass(frozen=True)
class VisualSpec:
    """One discovered visual: its module name, its title, its renderer."""

    name: str
    title: str
    render: Callable[[dict], str]


def _discover(package: ModuleType) -> dict[str, VisualSpec]:
    """Walk ``package``'s modules and return the visuals they declare.

    Refuses loudly, naming the module, when a module declares one half of the
    contract without the other — that is a half-finished visual, not a helper,
    and skipping it would hide the mistake from whoever wrote it.
    """
    found: dict[str, VisualSpec] = {}
    for info in pkgutil.iter_modules(package.__path__):
        if info.ispkg or info.name.startswith("_"):
            continue
        module = import_module(f"{package.__name__}.{info.name}")
        title = getattr(module, "TITLE", None)
        renderer = getattr(module, "render", None)
        if title is None and renderer is None:
            continue
        if renderer is None or not callable(renderer):
            raise RuntimeError(
                f"visual {info.name!r} declares TITLE but no callable "
                "render(params: dict) -> str"
            )
        if not isinstance(title, str) or not title:
            raise RuntimeError(
                f"visual {info.name!r} declares render but no non-empty "
                "TITLE: str"
            )
        found[info.name] = VisualSpec(
            name=info.name, title=title, render=renderer
        )
    return found


def catalog() -> dict[str, VisualSpec]:
    """Every visual this build carries, keyed by module name."""
    import qlab.visuals as package

    return _discover(package)


def render(name: str, params: dict) -> str:
    """Render one visual by name, refusing an unknown name with the known."""
    specs = catalog()
    spec = specs.get(name)
    if spec is None:
        known = ", ".join(sorted(specs)) or "none"
        raise KeyError(f"unknown visual {name!r}; available: {known}")
    return spec.render(params)
