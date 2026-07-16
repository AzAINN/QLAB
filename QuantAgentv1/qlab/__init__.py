"""qlab — an agentic quant research lab.

Three layers, hard boundaries, everything logged:

* **Quantum** is applied only where its structure genuinely fits — portfolio
  weight optimization and higher-moment risk modeling — never as a price oracle.
* **The LLM** (the orchestrator + subagents) is applied only where *judgment* is
  genuinely required — estimation windows, shrinkage intensity, regime calls,
  experiment design — never where a number can be computed.
* **Deterministic code** owns *rigor* — constraint enforcement, look-ahead
  tripwires, trial counting, mandate limits.

Positioning: *TradingAgents puts the LLM where the alpha is. We put the LLM
where the judgment is, and machines where the numbers are.*

Package map
-----------
``qlab.core``      pure-Python quant library (math only; no MCP/agents/broker)
``qlab.solvers``   one ``Solver`` protocol, N implementations (classical, HRP,
                   CVaR-LP, QAOA, Dirac-3, mock)
``qlab.state``     DuckDB registry + content-addressed artifact store
``qlab.mcp``       the two MCP servers (quant-lab research + quant-trader gateway)
``qlab.trader``    mandate enforcement + broker gateway + order-plan state machine
``qlab.agents``    orchestrator-agnostic agent definitions + adapter loader
``qlab.autopilot`` the standalone poll->analyze->solve->trade->log loop + CLI
"""

__version__ = "0.1.0"

# Keep the top-level import light: importing ``qlab`` must not drag in numpy,
# qiskit, duckdb, etc. Sub-packages are imported on demand.
__all__ = ["__version__"]
