"""Small synchronous client for the single-owner qlab UI runtime."""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

# How long a stream read waits before giving up and resubscribing. The owner
# must prove liveness inside this window even while a long action holds its
# dispatch lock, or every such action costs a reconnect and a stranded server
# thread — see `_STREAM_LOCK_WAIT_SECONDS` in qlab.ui.server.
STREAM_READ_TIMEOUT_S = 15.0
# Pause between reconnect attempts while the owner is unreachable.
STREAM_RETRY_WAIT_S = 2.0


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
        cancellation even while the desk is quiet.

        The subscription heals itself: transport failures and owner restarts
        reconnect after the last exact event tuple, so an outage never resets
        the cursor — a caller-level retry would resubscribe from the primer and
        silently drop everything past it. A frame that does not parse to an
        object is surfaced as a ``stream.malformed`` event rather than either
        tearing the stream down or being silently discarded; the desk gets to
        say a frame was bad while the subscription lives on.
        """
        import json

        request_params = dict(params)
        last_cursor: tuple[str, str] | None = None

        def stopped() -> bool:
            return stop_event is not None and stop_event.is_set()

        while not stopped():
            if last_cursor is not None:
                request_params["after"], request_params["after_id"] = last_cursor
            try:
                with httpx.stream(
                    "GET", self.base_url + path, params=request_params,
                    timeout=httpx.Timeout(STREAM_READ_TIMEOUT_S, connect=10.0),
                ) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if stopped():
                            return
                        if not line or not line.startswith("data:"):
                            continue
                        payload = line[len("data:"):].strip()
                        if not payload:
                            continue
                        try:
                            event = json.loads(payload)
                        except json.JSONDecodeError:
                            event = None
                        if not isinstance(event, dict):
                            yield {"kind": "stream.malformed",
                                   "payload": {"raw": payload[:120]}}
                            continue
                        event_ts = str(event.get("ts") or "")
                        event_id = str(event.get("event_id") or "")
                        if event_ts and event_id:
                            last_cursor = (event_ts, event_id)
                        yield event
            except httpx.HTTPError:
                # Owner restarting or unreachable. Wait out the gap with the
                # cursor intact; a resumable outage must not become data loss.
                if stop_event is not None:
                    stop_event.wait(STREAM_RETRY_WAIT_S)
                else:
                    time.sleep(STREAM_RETRY_WAIT_S)


def gather_snapshot(client, *, offline: bool = True) -> dict:
    """Fetch the complete observer snapshot in one owner-process request."""
    return client.get("/api/tui", offline=int(offline), event_limit=100)
