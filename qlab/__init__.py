"""qlab — a governed agentic quant research desk.

The LLM owns logged judgment, algorithms own numerical work, deterministic code
owns rigor, and the algorithm catalog owns deployment stage. Operational methods
are agent-runnable; research and offline methods are visible but not promoted.

Package map
-----------
``qlab.algorithms`` categorized operational/research/offline deployment catalog
``qlab.core``       pure-Python quant library (math only; no MCP/agents/broker)
``qlab.solvers``    one ``Solver`` protocol, N implementation adapters
``qlab.state``      DuckDB registry + content-addressed artifact store
``qlab.mcp``        combined server plus the qlab-operator HTTP proxy
``qlab.trader``     mandate enforcement + broker gateway + order-plan state machine
``qlab.agents``     orchestrator-agnostic agent definitions + adapter loader
``qlab.autopilot``  standalone analyze->solve->trade->log loop + CLI
"""

__version__ = "0.1.0"

# Keep the top-level import light: importing ``qlab`` must not drag in numpy,
# qiskit, duckdb, etc. Sub-packages are imported on demand.
__all__ = ["__version__"]
