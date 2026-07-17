"""qlab.core — the pure-Python quant library.

This package contains **all the math** and knows nothing about MCP, agents, or
brokers (research-plan §2.1). That boundary is what lets the autopilot loop and
the unit tests call the exact same functions the MCP servers wrap, with no
protocol hop and no network.

Import surface is deliberately flat and stable so downstream code (the MCP
servers, the autopilot, future orchestrators) can depend on it directly.
"""

from qlab.core.types import (
    AssetMeta,
    DataSnapshot,
    Decision,
    MomentSet,
    Objective,
    PriceBar,
    SolveResult,
    Weights,
)

__all__ = [
    "AssetMeta",
    "DataSnapshot",
    "Decision",
    "MomentSet",
    "Objective",
    "PriceBar",
    "SolveResult",
    "Weights",
]
