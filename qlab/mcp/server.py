"""The combined qlab MCP server - one process, one DuckDB writer, both roles.

DuckDB permits a single read-write process. Running quant-lab and quant-trader
as separate servers (the original .mcp.json) makes them fight over the file
lock; here both tool namespaces mount on one FastMCP app over one shared
Registry. Governance separation is enforced where it lives: per-agent tool
allowlists (agents/*.md), not process boundaries.

This combined server is for **headless orchestrator use** — when no owner UI
runtime is alive. The UI runtime (``UISession``) is the single paper-book owner
while it runs; ``qlab/mcp/tui_proxy.py`` (the ``qlab-operator`` proxy) is the
governed MCP surface for sessions launched under it. So ``main()`` refuses to
start if the owner API answers on the UI port — never two DuckDB writers.
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

from qlab.mcp.guardrails import LabState, require_fastmcp
from qlab.mcp.quant_lab import register_lab_tools
from qlab.mcp.quant_trader import TraderState, register_trader_tools
from qlab.state.registry import Registry


def build_combined_server():
    FastMCP = require_fastmcp()
    offline = os.environ.get("QLAB_OFFLINE") == "1"
    app = FastMCP("qlab")
    registry = Registry()
    register_lab_tools(app, LabState(offline=offline, registry=registry))
    register_trader_tools(app, TraderState(registry=registry, offline=offline))
    return app


# The readiness route is served without taking the owner's dispatch lock,
# which `/api/system` is not: probing that one meant a long action — an
# ablation holding the lock for minutes — timed out and was read as "no owner",
# after which this process opened the registry and died on a raw DuckDB lock
# error instead of refusing clearly. The deadline is generous for the same
# reason: a slow answer is still an answer, and only silence means no owner.
_OWNER_PROBE_PATH = "/readyz"
_OWNER_PROBE_TIMEOUT_S = 5.0


def owner_runtime_alive(port: int) -> bool:
    """True if an owner UI runtime answers on ``port``.

    Any successful HTTP response — including a non-2xx status — means the owner
    process is alive and already holds the paper book. Connection refused means
    no owner is running. Stdlib only.

    A timeout is treated as ALIVE, not absent. Guessing "no owner" from silence
    is the dangerous direction: it lets a second DuckDB writer start. An owner
    that is merely slow to answer still owns the book.
    """
    url = f"http://127.0.0.1:{port}{_OWNER_PROBE_PATH}"
    try:
        with urllib.request.urlopen(url, timeout=_OWNER_PROBE_TIMEOUT_S) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError:
        return True  # server responded (even with an error status) → alive
    except urllib.error.URLError as exc:
        # A refused connection is a real absence; a timeout is not.
        if isinstance(exc.reason, TimeoutError):
            return True
        return False
    except TimeoutError:
        return True
    except OSError:
        return False


def main() -> None:
    port = int(os.environ.get("QLAB_UI_PORT", "8765"))
    if owner_runtime_alive(port):
        print(
            "qlab combined server refused to start: an owner UI runtime is "
            f"alive on port {port} and already owns the paper book. Two DuckDB "
            "writers are not allowed. For a governed session under the owner, "
            "use the 'qlab-operator' proxy (python -m qlab.mcp.tui_proxy).",
            file=sys.stderr,
        )
        sys.exit(3)
    build_combined_server().run()  # pragma: no cover - stdio transport


if __name__ == "__main__":  # pragma: no cover
    main()
