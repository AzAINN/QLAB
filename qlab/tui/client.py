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


    def stream(self, path: str, **params: Any):
        """Yield durable audit and transient topic events from the owner.

        Blocks between events; callers exit by breaking (Ctrl-C closes the
        socket via the context manager). Heartbeat comments are skipped.
        """
        import json

        with httpx.stream(
            "GET", self.base_url + path, params=params,
            timeout=httpx.Timeout(None, connect=10.0),
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if not payload:
                    continue
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    continue


def gather_snapshot(client, *, offline: bool = True) -> dict:
    """Fetch the complete observer snapshot in one owner-process request."""
    return client.get("/api/tui", offline=int(offline), event_limit=100)
