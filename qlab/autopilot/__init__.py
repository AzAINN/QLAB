"""qlab.autopilot — the standalone poll→analyze→solve→trade→log→summarize loop.

Runs the SAME quant core + solvers + registry the MCP servers wrap, directly and
in-process (no MCP hop, no chat session). This is the ``quant-trader`` operating
model (research-plan §8.2): cron + headless runs, each booting cold from the
registry. Paper capital only, hard-coded; it never places a real order.
"""

from qlab.autopilot.loop import daily_ops, run_once

__all__ = ["daily_ops", "run_once"]
