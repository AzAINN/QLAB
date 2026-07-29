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
from qlab.trader.broker import SimulatedPaperBroker, default_price_provider
from qlab.trader.mandate import load_mandate

warnings.filterwarnings("ignore")

CORE = ["ACWI", "BNDW", "GSG", "IGF", "GLD", "VNQ", "EMB"]

# The only module allowed to see the operator's real Alpaca credentials.
_INTEGRATION_MODULE = "test_alpaca_integration.py"


@pytest.fixture(autouse=True)
def isolated_market_cache(tmp_path, monkeypatch):
    """Keep offline tests independent of the operator's real-data cache."""
    monkeypatch.setattr(market, "_CACHE_DIR", tmp_path / "market-cache")


@pytest.fixture(autouse=True)
def isolated_state_root(tmp_path, monkeypatch):
    """Keep the operator's runtime state out of the suite, in both directions.

    ``UISession`` loads the persisted desk mode at construction and the desk-mode
    route saves it, so without this a run would inherit the mode the operator
    left their desk in — and then overwrite it.
    """
    monkeypatch.setenv("QLAB_STATE_DIR", str(tmp_path / "state"))


@pytest.fixture(autouse=True)
def no_ambient_owner_runtime(request, monkeypatch):
    """Tests never consult a desk that happens to be running on this machine.

    The direct-registry commands refuse to start while an owner owns the book,
    and that guard probes a real port — so with `qlab tui` up, `cli.main` in a
    test exits instead of running. A test's result must not depend on whether
    the operator's desk is open. Tests that exercise the guard patch it
    themselves, which takes precedence over this.

    ``tests/test_mcp_server.py`` owns the probe's own behaviour, so it is
    exempt: stubbing the function there would test the stub.
    """
    if request.path.name == "test_mcp_server.py":
        return
    monkeypatch.setattr(
        "qlab.mcp.server.owner_runtime_alive", lambda port: False)


@pytest.fixture(autouse=True)
def isolated_alpaca_credentials(request, tmp_path, monkeypatch):
    """No test may discover the operator's real Alpaca login.

    ``get_broker`` can select the Alpaca book from discoverable credentials, so
    on a machine where ``alpaca profile login`` has run the offline suite would
    otherwise reach the operator's real paper account. Point the CLI config at a
    directory that does not exist and clear the env credentials.

    ``tests/test_alpaca_integration.py`` is the one module that is *supposed* to
    reach the real paper account, and it is skipped unless
    ``QLAB_ALPACA_INTEGRATION=1``. The exemption is per-module rather than per
    environment variable so enabling that suite cannot expose every other test
    to live credentials.
    """
    if request.path.name == _INTEGRATION_MODULE:
        return
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path / "no-alpaca-config"))
    monkeypatch.delenv("ALPACA_PROFILE", raising=False)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    # Clearing the variables is not enough once ``qlab.env`` exists: tests that
    # drive ``cli.main`` call ``load_once()``, and the loader only declines to
    # set a variable that is already *truthy* — so a deleted one is exactly the
    # case it fills from the operator's ``.env``. Setting them to "" would not
    # help for the same reason. Neutralise the loader instead: no test reads the
    # operator's file.
    monkeypatch.setattr("qlab.env.load_once", lambda *a, **k: [])


@pytest.fixture
def reg() -> Registry:
    r = Registry(":memory:")
    yield r
    r.close()


@pytest.fixture
def tmp_registry() -> Registry:
    r = Registry(":memory:")
    yield r
    r.close()


@pytest.fixture
def reg_and_broker(tmp_registry):
    mandate = load_mandate()
    broker = SimulatedPaperBroker(
        tmp_registry, default_price_provider(offline=True),
        mandate.paper_capital, universe=mandate.universe_whitelist)
    return tmp_registry, broker


@pytest.fixture
def snap():
    return market.snapshot(CORE, "2022-06-30", offline=True, seed=7)


@pytest.fixture
def moment_set(snap):
    return estimate_moments(snap, lookback_days=504, higher_moments=True)


@pytest.fixture
def moments_cfg() -> MomentsConfig:
    return MomentsConfig(lookback_days=504)
