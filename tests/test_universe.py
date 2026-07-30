"""Universe tiers and mandate-to-universe resolution."""

from __future__ import annotations

import pytest
import yaml

from qlab.core.universe import Universe, load_universe
from qlab.paths import data_path
from qlab.trader.mandate import MandateViolation, load_mandate


CORE = ['ACWI', 'SPY', 'QQQ', 'IWM', 'EEM', 'BNDW', 'TLT', 'IEF', 'TIP', 'LQD', 'HYG', 'EMB', 'GLD', 'SLV', 'GSG', 'DBC', 'USO', 'IGF', 'VNQ', 'RWO']
CANDIDATES = ['ACWI', 'SPY', 'EFA', 'EEM', 'IWM', 'BNDW', 'AGG', 'BNDX', 'TLT', 'EMB', 'HYG', 'TIP', 'GLD', 'GSG', 'DBC', 'VNQ', 'RWO', 'IGF', 'USO', 'QQQ', 'IEF', 'LQD', 'SLV']
EXTENDED = ['ACWI', 'SPY', 'EFA', 'EEM', 'IWM', 'BNDW', 'AGG', 'BNDX', 'TLT', 'EMB', 'HYG', 'TIP', 'GLD', 'GSG', 'DBC', 'VNQ', 'RWO', 'IGF', 'USO', 'VTV', 'MTUM', 'QUAL', 'USMV', 'XLK', 'XLF', 'XLV', 'XLE', 'SHY', 'IEF', 'LQD']
STOCKS = [
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "JPM",
    "V",
    "LLY",
    "JNJ",
    "XOM",
    "PG",
    "CAT",
    "NEE",
    "PLD",
]


def _extended_asset(ticker: str) -> dict[str, str]:
    return {
        "ticker": ticker,
        "name": f"{ticker} fund",
        "asset_class": "equity",
        "region": "us",
    }


def test_existing_tier_order_is_unchanged():
    universe = load_universe()
    assert universe.tickers("core") == CORE
    assert universe.tickers("candidates") == CANDIDATES


def test_extended_tier_has_stable_unique_order_and_metadata():
    universe = load_universe()
    tickers = universe.tickers("extended")

    assert tickers == EXTENDED
    assert len(tickers) == 30
    assert len(tickers) == len(set(tickers))

    metadata = universe.metadata("extended")
    assert [asset.ticker for asset in metadata] == tickers
    assert all(asset.name for asset in metadata)
    assert all(asset.asset_class for asset in metadata)
    assert all(asset.region for asset in metadata)
    assert universe.meta("MTUM", "extended").factor == "momentum"
    assert universe.factors("extended") == {
        "VTV": "value",
        "MTUM": "momentum",
        "QUAL": "quality",
        "USMV": "min_volatility",
    }


def test_stock_tier_has_unique_tickers_and_sector_metadata():
    universe = load_universe()
    tickers = universe.tickers("stocks")

    assert tickers == STOCKS
    assert len(tickers) == len(set(tickers))
    assert [asset.ticker for asset in universe.metadata("stocks")] == tickers
    assert all(asset.name for asset in universe.metadata("stocks"))
    assert set(universe.sectors()) == set(tickers)
    assert len(set(universe.sectors().values())) >= 8
    assert universe.meta("AAPL", "stocks").sector == "technology"


@pytest.mark.parametrize(
    ("tier", "entries"),
    [
        ("core", [{"ticker": "DUP"}, {"ticker": "DUP"}]),
        ("candidates", ["DUP", "DUP"]),
        ("extended", [_extended_asset("DUP"), _extended_asset("DUP")]),
        ("stocks", [{"ticker": "DUP"}, {"ticker": "DUP"}]),
    ],
)
def test_duplicate_ticker_within_any_tier_raises(tier, entries):
    with pytest.raises(ValueError, match=rf"duplicate ticker.*{tier}"):
        Universe({tier: entries})


def test_default_mandate_whitelist_stays_core():
    mandate = load_mandate()
    assert mandate.universe_tier == "core"
    assert mandate.universe_whitelist == CORE


@pytest.mark.parametrize(
    ("tier", "expected_whitelist"),
    [("core", CORE), ("extended", EXTENDED)],
)
def test_mandate_etf_universe_tier_resolves_whitelist(
    tmp_path, tier, expected_whitelist
):
    raw = yaml.safe_load(data_path("mandate.yaml").read_text(encoding="utf-8"))
    raw["universe_tier"] = tier
    mandate_path = tmp_path / "mandate.yaml"
    mandate_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    mandate = load_mandate(mandate_path)
    assert mandate.universe_tier == tier
    assert mandate.universe_whitelist == expected_whitelist


@pytest.mark.parametrize("tier", ["stocks", "candidates", "future_pool"])
def test_mandate_rejects_tiers_without_catalog_promotion(tmp_path, tier):
    raw = yaml.safe_load(data_path("mandate.yaml").read_text(encoding="utf-8"))
    raw["universe_tier"] = tier
    mandate_path = tmp_path / "mandate.yaml"
    mandate_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(MandateViolation, match=rf"{tier}.*catalog promotion"):
        load_mandate(mandate_path)
