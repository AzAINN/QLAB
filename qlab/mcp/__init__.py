"""qlab.mcp — the two MCP servers the orchestrator talks to.

* :mod:`qlab.mcp.quant_lab`    — the research lab (data, moments, objective,
  solve, backtest, registry, report).
* :mod:`qlab.mcp.quant_trader` — the execution gateway (portfolio state,
  reconcile, propose_rebalance, execute_plan, halt/resume/risk_report). It
  deliberately exposes **no raw order tool**.

Both wrap :mod:`qlab.core` / :mod:`qlab.solvers` / :mod:`qlab.trader` and persist
to :mod:`qlab.state`; they hold the guardrails (schema validation, the ``as_of``
tripwire, constraint checks, a per-session call-budget ledger) so the servers are
the *referee of facts* while the agents remain the *authors of choices*.

FastMCP is an optional dependency (``pip install qlab[mcp]``); these modules
import lazily so the rest of the package never depends on it.
"""

__all__ = ["quant_lab", "quant_trader"]
