"""Opt-in Alpaca PAPER integration tests. Skipped unless explicitly enabled.

These are the only tests in the suite that touch the network, and they are off
by default so `python -m pytest` stays offline and deterministic. Enable with:

    export QLAB_ALPACA_INTEGRATION=1
    export ALPACA_API_KEY=... ALPACA_API_SECRET=...   # or: alpaca profile login
    python -m pytest tests/test_alpaca_integration.py -v

Most cases take the ``broker`` fixture and so need the env keys;
``test_oauth_profile_builds_a_paper_broker`` covers the browser-login path
instead and skips when no such profile exists.

They run against the Alpaca **paper** account only — ``AlpacaPaperBroker``
hard-codes ``paper=True`` and there is no live path to select. They still place
real paper orders, so they use one tiny notional and cancel what they open.
"""

from __future__ import annotations

import os
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("QLAB_ALPACA_INTEGRATION") != "1",
    reason="Alpaca paper integration is opt-in; set QLAB_ALPACA_INTEGRATION=1",
)

# Small enough that a fill is immaterial, large enough to clear minimums.
_PROBE_NOTIONAL = 2.0
_PROBE_SYMBOL = "ACWI"


@pytest.fixture(scope="module")
def broker():
    from qlab.state.registry import Registry
    from qlab.trader.broker import AlpacaPaperBroker

    for name in ("ALPACA_API_KEY", "ALPACA_API_SECRET"):
        if not os.environ.get(name):
            pytest.skip(f"{name} is not set")
    registry = Registry(":memory:")
    try:
        yield AlpacaPaperBroker(registry)
    finally:
        registry.close()


def test_account_is_a_paper_account(broker):
    """Guard the invariant: this adapter must never reach a live account.

    The client is constructed with paper=True, which routes it at the
    paper-api host. Assert the endpoint actually in use rather than trusting
    the constructor argument.
    """
    # alpaca-py holds a BaseURL enum here, whose str() is the member name
    # ("BaseURL.TRADING_PAPER"), not the host. Read .value so the assertion
    # sees the endpoint; the getattr chain keeps it safe if either the
    # attribute or the enum shape changes.
    raw = getattr(broker.trading, "_base_url", "")
    base_url = str(getattr(raw, "value", raw)).lower()
    assert "paper-api" in base_url, (
        f"AlpacaPaperBroker reached a non-paper endpoint: {base_url!r}")
    assert float(broker.trading.get_account().equity) >= 0


def test_oauth_profile_builds_a_paper_broker():
    """The `alpaca profile login` path, end to end. Opt-in like its neighbours.

    Takes no ``broker`` fixture: that one wants env API keys, and this case is
    exactly the credential source that replaces them. Skips — never fails —
    when the operator has no browser-login session.
    """
    from qlab.state.registry import Registry
    from qlab.trader.alpaca_auth import AlpacaAuthError, resolve_alpaca_credentials
    from qlab.trader.broker import AlpacaPaperBroker

    try:
        creds = resolve_alpaca_credentials()
    except AlpacaAuthError as exc:
        # An unusable credential source — a half-set env pair, a live profile,
        # unparseable YAML — is not a browser-login session, so for this case it
        # is absence: skip, as the `broker` fixture above does for absent env
        # keys. Those refusals are each asserted offline in test_alpaca_auth.py,
        # which always runs; swallowing them here costs no coverage. The message
        # is safe to print: alpaca_auth never puts a secret in one.
        pytest.skip(f"no usable OAuth profile ({exc})")
    if creds is None or creds.kind != "oauth":
        pytest.skip("no OAuth profile; run `alpaca profile login`")
    registry = Registry(":memory:")
    try:
        broker = AlpacaPaperBroker(registry, credentials=creds)
        # The OAuth branch builds its clients separately from the API-key
        # branch, so the paper-only invariant is re-checked on this path too.
        raw = getattr(broker.trading, "_base_url", "")
        base_url = str(getattr(raw, "value", raw)).lower()
        assert "paper-api" in base_url, (
            f"OAuth credentials reached a non-paper endpoint: {base_url!r}")
        state = broker.portfolio_state([_PROBE_SYMBOL])
        assert state["equity"] > 0
    finally:
        registry.close()


def test_portfolio_state_reports_broker_truth(broker):
    state = broker.portfolio_state([_PROBE_SYMBOL])
    assert state["equity"] > 0
    assert "cash" in state and "weights" in state
    # The high-water mark is persisted monotonically, never echoed back as equity.
    assert state["high_water_mark"] >= state["equity"] - 1e-6


def test_quote_and_marketable_limit_are_sane(broker):
    bid, ask = broker.quote(_PROBE_SYMBOL)
    assert bid > 0 and ask > 0 and ask >= bid

    from qlab.trader.lifecycle import marketable_limit_price

    buy = marketable_limit_price("buy", bid, ask)
    sell = marketable_limit_price("sell", bid, ask)
    assert buy >= ask and sell <= bid


def test_universe_is_tradable_and_fractionable(broker):
    from qlab.trader.mandate import load_mandate

    for ticker in load_mandate().universe_whitelist:
        asset = broker.assert_tradable(ticker, fractional=True)
        assert asset["tradable"] and asset["fractionable"]


def test_untradable_symbol_fails_loud(broker):
    with pytest.raises(Exception):
        broker.assert_tradable("NOT_A_REAL_SYMBOL_XYZ", fractional=True)


def test_submission_acknowledges_then_reconciles_via_rest(broker):
    """A submitted order is acknowledged, not filled, and REST recovery agrees.

    This is the contract the offline suite proves against a fake client; here
    it is checked against the real venue.
    """
    from qlab.trader.lifecycle import TradeUpdateSupervisor, recover_from_broker

    coid = f"qlab-it-{int(time.time())}"
    result = broker.submit_notional(coid, _PROBE_SYMBOL, "buy", _PROBE_NOTIONAL)
    assert result["acknowledged"] is True
    # Acceptance is never a fill.
    assert str(result["state"]).lower().split(".")[-1] not in ("filled",)

    try:
        supervisor = TradeUpdateSupervisor()
        supervisor.register(coid)
        # Broker truth drives local state; recovery is idempotent.
        open_orders = broker.open_orders()
        assert coid in open_orders
        recover_from_broker(supervisor, open_orders, client_order_ids=[coid])
        first = supervisor.state(coid)
        recover_from_broker(supervisor, open_orders, client_order_ids=[coid])
        assert supervisor.state(coid) == first
    finally:
        # Leave the paper account as we found it where possible.
        try:
            order_id = result.get("id")
            if order_id:
                broker.trading.cancel_order_by_id(order_id)
        except Exception:
            pass  # already terminal; nothing to clean up


def test_data_policy_operational_refuses_synthetic(broker):
    """With real credentials present, an operational fetch must be real data."""
    from qlab.core import data as market

    policy = market.DataPolicy.alpaca_operational(
        os.environ.get("ALPACA_FEED", "iex"))
    prices = market.get_prices(
        [_PROBE_SYMBOL], "2024-01-01", policy=policy)
    assert prices.attrs["source"] == "alpaca"
    assert prices.attrs["synthetic"] is False
