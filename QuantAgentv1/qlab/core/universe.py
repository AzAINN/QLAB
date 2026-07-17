"""Load and query the investable universe (configs/universe.yaml)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from qlab.core.types import AssetMeta

# Repo root = three levels up from this file (qlab/core/universe.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_UNIVERSE = _REPO_ROOT / "configs" / "universe.yaml"


class Universe:
    """The cross-asset ETF universe: a core set plus a wider candidate pool.

    The core is ~7 cross-asset ETFs (genuinely mixed correlation signs); the
    candidate pool (~19) is what the selection QUBO picks ``k`` from.
    """

    def __init__(self, data: dict):
        self._raw = data
        self.core: list[AssetMeta] = [
            AssetMeta(**a) for a in data.get("core", [])
        ]
        self.candidates: list[str] = list(data.get("candidates", []))
        self.benchmarks: dict[str, dict[str, float]] = data.get("benchmarks", {})
        self.selection_k: int = int(data.get("selection_k", 7))

    # -- convenience views ---------------------------------------------------
    @property
    def core_tickers(self) -> list[str]:
        return [a.ticker for a in self.core]

    def tickers(self, which: str = "core") -> list[str]:
        """Return tickers for ``'core'`` or ``'candidates'``."""
        if which == "core":
            return self.core_tickers
        if which == "candidates":
            return list(self.candidates)
        raise ValueError(f"unknown universe selector: {which!r}")

    def meta(self, ticker: str) -> AssetMeta:
        for a in self.core:
            if a.ticker == ticker:
                return a
        return AssetMeta(ticker=ticker)

    def asset_classes(self) -> dict[str, str]:
        return {a.ticker: a.asset_class for a in self.core}


@lru_cache(maxsize=4)
def load_universe(path: str | Path | None = None) -> Universe:
    """Load the universe config (cached). Pass ``path`` to override the default."""
    p = Path(path) if path else _DEFAULT_UNIVERSE
    with open(p, "r", encoding="utf-8") as f:
        return Universe(yaml.safe_load(f))
