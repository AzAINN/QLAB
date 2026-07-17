"""Shared fixtures. Every test runs offline with an isolated in-memory registry.

Per-test DB isolation keeps the suite order-independent (research-plan §10
revision). All data is the deterministic synthetic feed — no network, ever.
"""

from __future__ import annotations

import warnings

import pytest

from qlab.arms import MomentsConfig
from qlab.core import data as market
from qlab.core.moments import estimate_moments
from qlab.state.registry import Registry

warnings.filterwarnings("ignore")

CORE = ["ACWI", "BNDW", "GSG", "IGF", "GLD", "VNQ", "EMB"]


@pytest.fixture
def reg() -> Registry:
    r = Registry(":memory:")
    yield r
    r.close()


@pytest.fixture
def snap():
    return market.snapshot(CORE, "2022-06-30", offline=True, seed=7)


@pytest.fixture
def moment_set(snap):
    return estimate_moments(snap, lookback_days=504, higher_moments=True)


@pytest.fixture
def moments_cfg() -> MomentsConfig:
    return MomentsConfig(lookback_days=504)
