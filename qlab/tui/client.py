"""Small synchronous client for the single-owner qlab UI runtime."""

from __future__ import annotations

from typing import Any

import httpx


class ApiClient:
    """JSON client used from TUI worker threads.

    A new request connection is used per call. That keeps the client safe when
    a long-running action and a background refresh overlap.
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8765"):
        self.base_url = base_url.rstrip("/")

    def get(self, path: str, **params: Any) -> dict:
        response = httpx.get(
            self.base_url + path, params=params, timeout=httpx.Timeout(15.0))
        response.raise_for_status()
        return response.json()

    def post(self, path: str, body: dict | None = None) -> dict:
        response = httpx.post(
            self.base_url + path,
            json=body or {},
            timeout=httpx.Timeout(1800.0, connect=10.0),
        )
        response.raise_for_status()
        return response.json()


def gather_snapshot(client, *, offline: bool = True) -> dict:
    """Fetch the complete observer snapshot in one owner-process request."""
    return client.get("/api/tui", offline=int(offline), event_limit=100)
