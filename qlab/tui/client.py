"""Small synchronous client for the single-owner qlab UI runtime."""

from __future__ import annotations

import threading
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

    def probe(self, path: str = "/readyz", *, timeout: float = 1.0) -> dict:
        """Read a lightweight owner readiness route with a short deadline."""
        response = httpx.get(
            self.base_url + path,
            timeout=httpx.Timeout(timeout, connect=timeout),
        )
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

    def post_control(self, path: str, body: dict | None = None) -> dict:
        """Post a lifecycle control with a short, operator-safe deadline.

        Research calls may legitimately run for minutes. Stop, resume, and
        abandon may not: a wedged owner must never make the stop button hang
        behind the same 30-minute request timeout.
        """
        response = httpx.post(
            self.base_url + path,
            json=body or {},
            timeout=httpx.Timeout(5.0, connect=2.0),
        )
        response.raise_for_status()
        return response.json()


    def stream(
        self,
        path: str,
        *,
        stop_event: threading.Event | None = None,
        **params: Any,
    ):
        """Yield durable audit and transient topic events from the owner.

        The owner emits a heartbeat about every ten seconds, which bounds
        cancellation even while the desk is quiet. A closed connection resumes
        after the last exact event tuple.
        """
        import json

        request_params = dict(params)
        last_cursor: tuple[str, str] | None = None
        while stop_event is None or not stop_event.is_set():
            with httpx.stream(
                "GET", self.base_url + path, params=request_params,
                timeout=httpx.Timeout(15.0, connect=10.0),
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if stop_event is not None and stop_event.is_set():
                        return
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if not payload:
                        continue
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        event_ts = str(event.get("ts") or "")
                        event_id = str(event.get("event_id") or "")
                        if event_ts and event_id:
                            last_cursor = (event_ts, event_id)
                    yield event
            if stop_event is not None and stop_event.is_set():
                return
            if last_cursor is None:
                return
            request_params["after"], request_params["after_id"] = last_cursor


def gather_snapshot(client, *, offline: bool = True) -> dict:
    """Fetch the complete observer snapshot in one owner-process request."""
    return client.get("/api/tui", offline=int(offline), event_limit=100)
