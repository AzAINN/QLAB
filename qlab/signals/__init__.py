"""Deterministic, injection-immune signals layer (roadmap Amendment A / §3).

The hard-signals half is computed purely from prices or fixed public index
series — no text, no LLM — so it is the referee-auditable floor that
regime-conditional moments (R1-T3) and the operator TUI build on.
"""
from __future__ import annotations

from qlab.signals.hard import (
    absorption_ratio,
    composite_regime,
    fred_series,
    turbulence,
)

__all__ = [
    "turbulence",
    "absorption_ratio",
    "fred_series",
    "composite_regime",
]
