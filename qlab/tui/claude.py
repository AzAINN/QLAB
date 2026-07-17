"""Safe Claude Code stream integration for the operator console.

`ask` sessions use a strict empty MCP config and no built-in tools. Governed
sessions load only :mod:`qlab.mcp.tui_proxy`, whose tools call the owner API and
never open DuckDB. That mode can inspect, research, run daily ops, and produce a
dry rebalance proposal; paper execution remains a human-confirmed TUI action.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal


EventKind = Literal[
    "session", "text", "text_delta", "tool_start", "tool_result", "result", "error"
]

_PROXY_TOOLS = [
    "mcp__qlab-operator__portfolio.state",
    "mcp__qlab-operator__market.snapshot",
    "mcp__qlab-operator__audit.events",
    "mcp__qlab-operator__research.runs",
    "mcp__qlab-operator__research.decisions",
    "mcp__qlab-operator__workflow.rebalance_preview",
    "mcp__qlab-operator__workflow.daily_ops",
    "mcp__qlab-operator__research.batch",
]


@dataclass(frozen=True)
class ClaudeEvent:
    kind: EventKind
    text: str
    tool: str = ""
    raw: dict = field(default_factory=dict, repr=False)


def parse_stream_line(line: str) -> list[ClaudeEvent]:
    """Parse one Claude stream-json line without exposing thinking blocks."""
    try:
        payload = json.loads(line)
    except (TypeError, json.JSONDecodeError):
        return [ClaudeEvent("error", line.strip() or "invalid Claude stream event")]

    out: list[ClaudeEvent] = []
    kind = payload.get("type")

    if kind == "system":
        subtype = payload.get("subtype", "ready")
        out.append(ClaudeEvent("session", f"Claude session {subtype}", raw=payload))

    elif kind == "stream_event":
        event = payload.get("event") or {}
        event_type = event.get("type")
        if event_type == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                out.append(ClaudeEvent("text_delta", str(delta["text"]), raw=payload))
        elif event_type == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                name = str(block.get("name", "tool"))
                out.append(ClaudeEvent("tool_start", f"calling {name}", name, payload))

    elif kind in ("assistant", "user"):
        message = payload.get("message") or {}
        content = message.get("content") or []
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for block in content:
            block_type = block.get("type") if isinstance(block, dict) else None
            if block_type == "text" and block.get("text"):
                out.append(ClaudeEvent("text", str(block["text"]), raw=payload))
            elif block_type == "tool_use":
                name = str(block.get("name", "tool"))
                out.append(ClaudeEvent("tool_start", f"calling {name}", name, payload))
            elif block_type == "tool_result":
                text = block.get("content", "tool completed")
                if isinstance(text, list):
                    text = " ".join(
                        str(item.get("text", "")) for item in text
                        if isinstance(item, dict) and item.get("type") == "text"
                    )
                out.append(ClaudeEvent("tool_result", str(text)[:1000], raw=payload))
            # `thinking` and `redacted_thinking` are intentionally ignored.

    elif kind == "result":
        if payload.get("is_error"):
            out.append(ClaudeEvent("error", str(payload.get("result", "Claude failed")), raw=payload))
        else:
            out.append(ClaudeEvent("result", str(payload.get("result", "Claude completed")), raw=payload))

    return out


def build_claude_argv(
    prompt: str,
    *,
    governed: bool,
    runtime_url: str,
    offline: bool,
) -> list[str]:
    """Build an auditable Claude command with no ambient MCP/tool access."""
    if governed:
        config = {
            "mcpServers": {
                "qlab-operator": {
                    "command": sys.executable,
                    "args": ["-m", "qlab.mcp.tui_proxy"],
                    "env": {
                        "QLAB_RUNTIME_URL": runtime_url.rstrip("/"),
                        "QLAB_OFFLINE": "1" if offline else "0",
                    },
                }
            }
        }
    else:
        config = {"mcpServers": {}}

    argv = [
        "claude",
        "--print",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--strict-mcp-config",
        "--mcp-config", json.dumps(config),
        "--tools", "",
    ]
    if governed:
        argv.extend(["--allowedTools", ",".join(_PROXY_TOOLS)])
        argv.extend([
            "--append-system-prompt",
            "You are operating qlab in propose-only mode. Use the bounded "
            "MCP tools for facts and dry proposals. You cannot execute paper "
            "orders. Report data source, age, mandate status, and uncertainty.",
        ])
    argv.append(prompt)
    return argv


class ClaudeSession:
    """One non-interactive streaming Claude turn with explicit authority."""

    def __init__(
        self,
        on_event: Callable[[ClaudeEvent], None],
        *,
        cwd: Path | None = None,
        runtime_url: str = "http://127.0.0.1:8765",
        offline: bool = True,
    ):
        self.on_event = on_event
        self.cwd = cwd or Path.cwd()
        self.runtime_url = runtime_url.rstrip("/")
        self.offline = offline
        self.process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self.mode = "read-only"

    @property
    def available(self) -> bool:
        return bool(shutil.which("claude"))

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, prompt: str, *, governed: bool = False) -> bool:
        if self.running or not self.available:
            return False
        self.mode = "propose-only" if governed else "read-only"
        argv = build_claude_argv(
            prompt,
            governed=governed,
            runtime_url=self.runtime_url,
            offline=self.offline,
        )
        self.process = subprocess.Popen(
            argv,
            cwd=self.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        if self.running and self.process is not None:
            self.process.terminate()

    def _read(self) -> None:
        assert self.process is not None
        assert self.process.stdout is not None
        for line in self.process.stdout:
            for event in parse_stream_line(line):
                self.on_event(event)
        stderr = ""
        if self.process.stderr is not None:
            stderr = self.process.stderr.read().strip()
        returncode = self.process.wait()
        if returncode and stderr:
            self.on_event(ClaudeEvent("error", stderr[-2000:]))
