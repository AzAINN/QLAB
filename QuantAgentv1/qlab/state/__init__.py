"""qlab.state — DuckDB registry + content-addressed artifacts.

This layer holds *no business logic* (research-plan §2.1). It persists runs,
moment sets, objectives, solutions, backtests, decisions/reflections, the paper
portfolio and order plans — and returns ids/hashes, never raw tensors.
"""

from qlab.state.artifacts import ArtifactStore
from qlab.state.registry import Registry

__all__ = ["ArtifactStore", "Registry"]
