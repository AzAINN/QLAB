"""Load and query the investable universe (configs/universe.yaml)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from qlab.core.types import AssetMeta
from qlab.paths import data_path


class UniverseAssetMeta(AssetMeta):
    """Universe metadata, including optional research classification tags."""

    region: str = ""
    factor: str | None = None
    sector: str | None = None


class Universe:
    """The ETF tiers and research-only stock candidate pool.

    The core is ~7 cross-asset ETFs (genuinely mixed correlation signs); the
    candidate pool (~19) is what the selection QUBO picks ``k`` from. Tiers
    may overlap, but a ticker may appear only once inside any individual tier.
    """

    def __init__(self, data: dict[str, Any]):
        if not isinstance(data, dict):
            raise ValueError("universe config must be a mapping")

        core_data = _tier_list(data, "core")
        candidate_data = _tier_list(data, "candidates")
        extended_data = _tier_list(data, "extended")
        stock_data = _tier_list(data, "stocks")

        core_tickers = [_structured_ticker(entry, "core") for entry in core_data]
        candidate_tickers = [_candidate_ticker(entry) for entry in candidate_data]
        extended_tickers = [
            _structured_ticker(entry, "extended") for entry in extended_data
        ]
        stock_tickers = [
            _structured_ticker(entry, "stocks") for entry in stock_data
        ]
        _require_unique("core", core_tickers)
        _require_unique("candidates", candidate_tickers)
        _require_unique("extended", extended_tickers)
        _require_unique("stocks", stock_tickers)
        _validate_extended_metadata(extended_data)
        _validate_stock_metadata(stock_data)

        self._raw = data
        self.core: list[UniverseAssetMeta] = [
            UniverseAssetMeta(**entry) for entry in core_data
        ]
        self.candidates: list[str] = candidate_tickers
        self.extended: list[UniverseAssetMeta] = [
            UniverseAssetMeta(**entry) for entry in extended_data
        ]
        self.stocks: list[UniverseAssetMeta] = [
            UniverseAssetMeta(**{"asset_class": "stock", **entry})
            for entry in stock_data
        ]
        self.benchmarks: dict[str, dict[str, float]] = data.get("benchmarks", {})
        self.selection_k: int = int(data.get("selection_k", 7))
        self._metadata_by_ticker = {asset.ticker: asset for asset in self.core}
        # Prefer the richer extended record when a ticker appears in both tiers.
        self._metadata_by_ticker.update(
            {asset.ticker: asset for asset in self.extended}
        )
        self._metadata_by_ticker.update(
            {asset.ticker: asset for asset in self.stocks}
        )

    # -- convenience views ---------------------------------------------------
    @property
    def core_tickers(self) -> list[str]:
        return [a.ticker for a in self.core]

    @property
    def extended_tickers(self) -> list[str]:
        return [a.ticker for a in self.extended]

    @property
    def stock_tickers(self) -> list[str]:
        return [a.ticker for a in self.stocks]

    def tickers(self, which: str = "core") -> list[str]:
        """Return tickers for a named universe tier in stable config order."""
        if which == "core":
            return self.core_tickers
        if which == "candidates":
            return list(self.candidates)
        if which == "extended":
            return self.extended_tickers
        if which == "stocks":
            return self.stock_tickers
        raise ValueError(f"unknown universe selector: {which!r}")

    def metadata(self, which: str = "core") -> list[UniverseAssetMeta]:
        """Return metadata records for a tier in the same order as :meth:`tickers`."""
        if which == "core":
            return list(self.core)
        if which == "extended":
            return list(self.extended)
        if which == "candidates":
            return [self.meta(ticker) for ticker in self.candidates]
        if which == "stocks":
            return list(self.stocks)
        raise ValueError(f"unknown universe selector: {which!r}")

    def meta(self, ticker: str, which: str | None = None) -> UniverseAssetMeta:
        """Return one metadata record, optionally constrained to a tier."""
        if which is None:
            return self._metadata_by_ticker.get(
                ticker, UniverseAssetMeta(ticker=ticker)
            )
        for asset in self.metadata(which):
            if asset.ticker == ticker:
                return asset
        return UniverseAssetMeta(ticker=ticker)

    def asset_classes(self, which: str = "core") -> dict[str, str]:
        return {a.ticker: a.asset_class for a in self.metadata(which)}

    def regions(self, which: str = "extended") -> dict[str, str]:
        return {
            asset.ticker: asset.region
            for asset in self.metadata(which)
            if asset.region
        }

    def factors(self, which: str = "extended") -> dict[str, str]:
        return {
            asset.ticker: asset.factor
            for asset in self.metadata(which)
            if asset.factor is not None
        }

    def sectors(self, which: str = "stocks") -> dict[str, str]:
        return {
            asset.ticker: asset.sector
            for asset in self.metadata(which)
            if asset.sector is not None
        }


def _tier_list(data: dict[str, Any], tier: str) -> list[Any]:
    value = data.get(tier, [])
    if not isinstance(value, list):
        raise ValueError(f"universe tier {tier!r} must be a list")
    return value


def _structured_ticker(entry: Any, tier: str) -> str:
    if not isinstance(entry, dict):
        raise ValueError(f"universe tier {tier!r} entries must be mappings")
    ticker = entry.get("ticker")
    if not isinstance(ticker, str) or not ticker.strip():
        raise ValueError(f"universe tier {tier!r} has an invalid ticker")
    return ticker


def _candidate_ticker(entry: Any) -> str:
    if not isinstance(entry, str) or not entry.strip():
        raise ValueError("universe tier 'candidates' has an invalid ticker")
    return entry


def _require_unique(tier: str, tickers: list[str]) -> None:
    seen: set[str] = set()
    duplicates: list[str] = []
    for ticker in tickers:
        if ticker in seen and ticker not in duplicates:
            duplicates.append(ticker)
        seen.add(ticker)
    if duplicates:
        joined = ", ".join(duplicates)
        raise ValueError(f"duplicate ticker(s) in universe tier {tier!r}: {joined}")


def _validate_extended_metadata(entries: list[Any]) -> None:
    required = ("ticker", "name", "asset_class", "region")
    for entry in entries:
        ticker = entry.get("ticker", "<unknown>")
        missing = [
            field
            for field in required
            if not isinstance(entry.get(field), str) or not entry[field].strip()
        ]
        if missing:
            joined = ", ".join(missing)
            raise ValueError(f"extended universe entry {ticker!r} missing: {joined}")
        factor = entry.get("factor")
        if factor is not None and (
            not isinstance(factor, str) or not factor.strip()
        ):
            raise ValueError(
                f"extended universe entry {ticker!r} has an invalid factor"
            )


def _validate_stock_metadata(entries: list[Any]) -> None:
    required = ("ticker", "name", "sector")
    for entry in entries:
        ticker = entry.get("ticker", "<unknown>")
        missing = [
            field
            for field in required
            if not isinstance(entry.get(field), str) or not entry[field].strip()
        ]
        if missing:
            joined = ", ".join(missing)
            raise ValueError(f"stocks universe entry {ticker!r} missing: {joined}")


@lru_cache(maxsize=4)
def load_universe(path: str | Path | None = None) -> Universe:
    """Load the universe config (cached). Pass ``path`` to override the default."""
    p = Path(path) if path else data_path("configs", "universe.yaml")
    with open(p, "r", encoding="utf-8") as f:
        return Universe(yaml.safe_load(f))
