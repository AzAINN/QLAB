"""qlab.mcp — a combined qlab MCP server (plus the qlab-operator HTTP proxy)
the orchestrator talks to.

* :mod:`qlab.mcp.quant_lab`    — the research-lab namespace (data, moments,
  objective, solve, backtest, registry, report).
* :mod:`qlab.mcp.quant_trader` — the execution-gateway namespace (portfolio
  state, reconcile, propose_rebalance, execute_plan, halt/resume/risk_report).
  It deliberately exposes **no raw order tool**.
* :mod:`qlab.mcp.server`       — mounts both namespaces on one FastMCP app
  over one shared Registry (one DuckDB writer): the single combined ``qlab``
  MCP server.
* :mod:`qlab.mcp.tui_proxy`    — the ``qlab-operator`` HTTP proxy: a
  propose-only surface that never opens DuckDB itself.

Both namespaces wrap :mod:`qlab.core` / :mod:`qlab.solvers` / :mod:`qlab.trader`
and persist to :mod:`qlab.state`; they hold the guardrails (schema validation,
the ``as_of`` tripwire, constraint checks, a per-session call-budget ledger) so
the server is the *referee of facts* while the agents remain the *authors of
choices*.

FastMCP is an optional dependency (``pip install qlab[mcp]``); these modules
import lazily so the rest of the package never depends on it.
"""

__all__ = ["quant_lab", "quant_trader"]
