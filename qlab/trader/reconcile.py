"""Reconcile the registry ledger against broker truth.

Moved out of the ``quant-trader`` MCP tool so both the interactive session
(via the MCP tool) and the autonomous autopilot (:mod:`qlab.autopilot.loop`)
share one implementation — the ledger-vs-broker check must be identical in
both paths, not re-derived per caller.
"""

from __future__ import annotations


def reconcile(registry, broker, tickers: list[str]) -> dict:
    """Diff the registry ledger against broker truth. Must be clean before trading."""
    broker_state = broker.portfolio_state(tickers)
    broker_pos = broker_state["positions"]
    ledger_pos = registry.get_positions()
    diffs = {}
    for t in set(list(broker_pos) + list(ledger_pos)):
        bq = broker_pos.get(t, {}).get("qty", 0.0)
        lq = ledger_pos.get(t, {}).get("qty", 0.0)
        if abs(bq - lq) > 1e-6:
            diffs[t] = {"broker_qty": bq, "ledger_qty": lq}
    # Cash must agree too: matching (empty) positions with divergent cash is a
    # broken book, not a clean one, and must block trading.
    broker_cash = float(broker_state.get("cash", 0.0))
    ledger_cash = float(registry.get_account().get("cash", broker_cash))
    if abs(broker_cash - ledger_cash) > 0.01:
        diffs["__cash__"] = {"broker_cash": broker_cash,
                             "ledger_cash": ledger_cash}
    return {"clean": not diffs, "diffs": diffs}
