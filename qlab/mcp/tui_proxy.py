"""Stateless MCP proxy for Claude sessions launched by the Textual console.

Unlike the original quant-lab / quant-trader stdio servers, this process never
opens DuckDB. Every tool delegates to the already-running owner API. Its
authority is intentionally capped at observation, research, daily operations,
and dry rebalance previews; paper execution remains a human-confirmed TUI action.
"""

from __future__ import annotations

import os
from typing import Any

import httpx


class RuntimeClient:
    def __init__(self, base_url: str | None = None, *, offline: bool | None = None):
        self.base_url = (base_url or os.environ.get(
            "QLAB_RUNTIME_URL", "http://127.0.0.1:8765")).rstrip("/")
        if offline is None:
            offline = os.environ.get("QLAB_OFFLINE", "1") == "1"
        self.offline = bool(offline)

    def get(self, path: str, **params: Any) -> dict:
        response = httpx.get(
            self.base_url + path, params=params, timeout=httpx.Timeout(30.0))
        response.raise_for_status()
        return response.json()

    def post(self, path: str, body: dict | None = None) -> dict:
        response = httpx.post(
            self.base_url + path, json=body or {},
            timeout=httpx.Timeout(1800.0, connect=10.0))
        response.raise_for_status()
        return response.json()


def register_proxy_tools(app, client: RuntimeClient) -> None:
    """Register the bounded owner-API surface on a FastMCP-like app."""

    @app.tool(name="portfolio.state")
    def portfolio_state() -> dict:
        """Current paper portfolio, exposure, drawdown, and target weights."""
        return client.get("/api/portfolio", offline=int(client.offline))

    @app.tool(name="market.snapshot")
    def market_snapshot() -> dict:
        """Daily-bar market snapshot with source, age, volatility, and regime."""
        return client.get("/api/market", offline=int(client.offline))

    @app.tool(name="audit.events")
    def audit_events(limit: int = 50) -> list[dict]:
        """Recent ordered workflow, mandate, proposal, and execution events."""
        return client.get("/api/events", limit=max(1, min(limit, 200))).get("events", [])

    @app.tool(name="research.runs")
    def research_runs() -> list[dict]:
        """Recent reproducible research and ablation runs."""
        return client.get("/api/runs").get("runs", [])

    @app.tool(name="research.decisions")
    def research_decisions() -> list[dict]:
        """Recent agent-authored judgment records and reflections."""
        return client.get("/api/decisions").get("decisions", [])

    @app.tool(name="workflow.rebalance_preview")
    def rebalance_preview(qaoa: bool = False) -> dict:
        """Compute and record a dry paper rebalance proposal; never execute it."""
        return client.post("/api/run_once", {
            "offline": client.offline,
            "execute": False,
            "qaoa": bool(qaoa),
        })

    @app.tool(name="workflow.daily_ops")
    def daily_ops() -> dict:
        """Run the non-trading reconcile, risk, drift, and regime heartbeat."""
        return client.post("/api/daily_ops", {"offline": client.offline})

    @app.tool(name="research.batch")
    def research_batch() -> dict:
        """Run the compact offline ablation through the owner runtime."""
        return client.post("/api/batch", {
            "offline": client.offline,
            "qaoa": False,
        })


def build_server(client: RuntimeClient | None = None):
    from fastmcp import FastMCP

    app = FastMCP("qlab-operator")
    register_proxy_tools(app, client or RuntimeClient())
    return app


def main() -> None:  # pragma: no cover - stdio transport
    build_server().run()


if __name__ == "__main__":  # pragma: no cover
    main()
