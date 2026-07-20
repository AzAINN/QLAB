"""Safe Claude Code stream integration for the operator console.

`ask` sessions use a strict empty MCP config and no built-in tools. Workforce
sessions run an inline qlab coordinator that can only delegate to five inline
domain agents. Those agents receive least-privilege tools from
:mod:`qlab.mcp.tui_proxy`; the proxy calls the owner API and never opens DuckDB.
No Claude role receives filesystem, shell, code-editing, or paper-execution
authority.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal


EventKind = Literal[
    "session", "text", "text_delta", "tool_start", "tool_result", "result", "error"
]

def _claude_tool(base: str) -> str:
    """Full Claude-visible proxy tool name for a dotted qlab base name.

    Claude Code sanitizes MCP tool names (``workflow.status`` registers as
    ``workflow_status``), so every allowlist and agent grant must use the
    underscored form or it matches nothing. Dotted names stay the neutral
    scheme everywhere else (owner HTTP routes, agents/*.md sources).
    """
    return f"mcp__qlab-operator__{base.replace('.', '_')}"


_OBSERVATION_TOOLS = [
    _claude_tool("portfolio.state"),
    _claude_tool("market.snapshot"),
    _claude_tool("policy.current"),
    _claude_tool("audit.events"),
    _claude_tool("research.runs"),
    _claude_tool("research.decisions"),
    _claude_tool("workflow.rebalance_preview"),
    _claude_tool("workflow.daily_ops"),
    _claude_tool("research.batch"),
]

_LAB_TOOL_BASES = {
    "data.fetch_universe",
    "data.snapshot_summary",
    "moments.estimate",
    "objective.build",
    "algorithms.list",
    "algorithms.describe",
    "policy.current",
    "algorithms.solve",
    "solve.classical",
    "backtest.run",
    "registry.list_runs",
    "registry.report",
    "registry.log_decision",
    "registry.recent_decisions",
    "registry.attach_challenge",
    "registry.log_verdict",
    "report.recommendation",
}

_WORKFLOW_PHASE = {
    "moments-analyst": "analyst",
    "challenger": "challenger",
    "optimization-runner": "optimizer",
    "referee": "referee",
    "reporter": "reporter",
}

_PHASE_ARTIFACT_CONTRACT = {
    "analyst": "moment_set_id, objective_id, and decision_id",
    "challenger": "challenger_view",
    "optimizer": "targets (ticker-to-weight object) and algorithm_id",
    "referee": (
        "verdict='PASS', verdict_id, and targets — the exact reviewed "
        "ticker-to-weight object, which must equal the optimizer's persisted "
        "targets; on FAIL use blocked instead of done"
    ),
    "reporter": "recommendation, plus plan_id when a dry preview was accepted",
}

_TRADER_PROXY_MAP = {
    "get_portfolio_state": "portfolio.state",
    "risk_report": "portfolio.state",
    "reconcile": "workflow.daily_ops",
    "propose_rebalance": "workflow.rebalance_preview",
}

_COORDINATOR_TOOLS = [
    _claude_tool("workflow.start"),
    _claude_tool("workflow.status"),
]

# The conversational desk assistant: observation and reading only. No Agent
# dispatch, no workflow phases, no research writes, and (as everywhere) no
# execution surface exists to grant.
_CHAT_TOOLS = [_claude_tool(base) for base in (
    "portfolio.state", "market.snapshot", "policy.current", "audit.events",
    "research.runs", "research.decisions", "algorithms.list",
    "algorithms.describe", "registry.list_runs", "registry.report",
    "registry.recent_decisions", "data.fetch_universe",
    "data.snapshot_summary",
)]

_CHAT_SYSTEM_PROMPT = (
    "You are the qlab desk assistant, chatting inside a quant operator "
    "terminal. Answer questions about the paper portfolio, market snapshot, "
    "operational policy, research runs, and audit trail conversationally and "
    "compactly — this renders in a terminal pane. Use your qlab tools for "
    "every number; never invent data or results. You are read-only: you "
    "cannot trade, modify research, or deploy agents. When the operator "
    "wants the governed five-role pipeline, point them at workforce mode."
)

_PROXY_TOOLS = sorted(set(
    _OBSERVATION_TOOLS
    + [_claude_tool(name) for name in _LAB_TOOL_BASES]
    + _COORDINATOR_TOOLS
    + [_claude_tool(f"workflow.{phase}")
       for phase in _WORKFLOW_PHASE.values()]
))

_COORDINATOR_NAME = "qlab-coordinator"


def _proxy_tool(tool: str) -> str | None:
    base = tool.rsplit("__", 1)[-1]
    if base in _LAB_TOOL_BASES:
        return _claude_tool(base)
    mapped = _TRADER_PROXY_MAP.get(base)
    return _claude_tool(mapped) if mapped else None


def build_workforce_agents() -> dict[str, dict]:
    """Build session-local Claude roles against the owner-backed proxy."""
    from qlab.agents.loader import load_agents

    agents: dict[str, dict] = {}
    for source in load_agents():
        phase = _WORKFLOW_PHASE.get(source.name)
        if phase is None:
            continue
        tools = [mapped for tool in source.tools if (mapped := _proxy_tool(tool))]
        tools.append(_claude_tool(f"workflow.{phase}"))
        if source.name in {
            "moments-analyst", "optimization-runner", "referee", "reporter"
        }:
            tools.append(_claude_tool("policy.current"))
        tools = list(dict.fromkeys(tools))
        override = f"""

QLAB OWNER-WORKFORCE MODE (this section supersedes any execution or fixed-
champion instruction above):
- You are a portfolio/research worker, never a software developer. Do not read,
  edit, write, or search repository files and do not run shell commands.
- The task contains a workflow_id. First call the workflow_{phase} tool with status
  `working`. Before returning, call it with `done` and a concise summary plus
  these required artifacts: {_PHASE_ARTIFACT_CONTRACT[phase]}. On a genuine
  failure call `failed`; when a required fact or approval is missing call
  `blocked` and preserve the available evidence in artifacts.
- Perform only the {phase} phase. Do not spawn other agents. Use owner MCP facts;
  never invent ids, data, solver output, a verdict, or a completed phase.
- MVSK is a research hypothesis, not an assumed live champion. Preserve it in
  comparisons, but use the catalog and current qlab policy for operational work.
- No Claude role can execute a paper order. The reporter may request daily ops
  or a dry rebalance preview only; human confirmation remains outside Claude.
""".strip()
        agents[source.name] = {
            "description": source.description,
            "prompt": source.body + "\n\n" + override,
            "tools": tools,
            "model": source.model or "inherit",
            "permissionMode": "dontAsk",
            "maxTurns": 24,
        }

    role_names = ",".join(_WORKFLOW_PHASE)
    agents[_COORDINATOR_NAME] = {
        "description": "Coordinates qlab's governed portfolio workforce; never develops code.",
        "prompt": (
            "You are the qlab workforce coordinator, not a coding assistant. "
            "You have no filesystem, shell, browser, editing, or trading tools. "
            "For a new portfolio/research goal, call workflow_start once. If the "
            "user message contains RESUME_WORKFLOW_ID, call workflow_status for "
            "that id, do not create a new workflow, and continue at the first "
            "non-done phase. Then invoke moments-analyst, challenger, optimization-runner, "
            "referee, and reporter in exactly that order using the Agent tool. "
            "Pass each worker the workflow_id, the original goal, relevant prior "
            "worker output and persisted artifacts, as_of, and universe. Run them in the foreground and "
            "wait for each result. After every worker, call workflow_status and "
            "do not continue unless its phase is done. If a phase fails or is "
            "blocked, stop and report that state. The reporter may run only after "
            "the referee phase is done and must not claim approval unless the "
            "persisted verdict is PASS. End with the workflow_id, phase outcomes, "
            "recommendation or research conclusion, data provenance, uncertainty, "
            "and what—if anything—requires human action."
        ),
        "tools": [f"Agent({role_names})", *_COORDINATOR_TOOLS],
        "model": "inherit",
        "permissionMode": "dontAsk",
        "maxTurns": 40,
    }
    return agents


@dataclass(frozen=True)
class ClaudeEvent:
    kind: EventKind
    text: str
    tool: str = ""
    raw: dict = field(default_factory=dict, repr=False)
    agent: str = ""


def _agent_from_tool_block(block: dict) -> str:
    if block.get("name") != "Agent":
        return ""
    tool_input = block.get("input") or {}
    return str(tool_input.get("subagent_type") or tool_input.get("agent_type") or "")


def parse_stream_line(line: str) -> list[ClaudeEvent]:
    """Parse one Claude stream-json line without exposing thinking blocks."""
    if not line or not line.strip():
        return []
    try:
        payload = json.loads(line)
    except (TypeError, json.JSONDecodeError):
        return [ClaudeEvent("error", line.strip())]

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
                out.append(ClaudeEvent(
                    "tool_start", f"calling {name}", name, payload,
                    _agent_from_tool_block(block),
                ))

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
                out.append(ClaudeEvent(
                    "tool_start", f"calling {name}", name, payload,
                    _agent_from_tool_block(block),
                ))
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
    resume_session: str | None = None,
    chat: bool = False,
) -> list[str]:
    """Build an auditable Claude command with no ambient MCP/tool access."""
    if governed or chat:
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
        "--disable-slash-commands",
        "--no-chrome",
    ]
    if governed:
        agents = build_workforce_agents()
        # "default", not "Agent": --tools narrows the whole tool universe and
        # would strip the MCP grants from every role. The coordinator's own
        # agent definition (Agent + two workflow tools) is the restriction —
        # verified live: built-ins do not leak into --agent-selected roles.
        argv[argv.index("--tools") + 1] = "default"
        argv.extend(["--allowedTools", ",".join(["Agent", *_PROXY_TOOLS])])
        argv.extend(["--agents", json.dumps(agents)])
        argv.extend(["--agent", _COORDINATOR_NAME])
        argv.extend(["--permission-mode", "dontAsk"])
        argv.extend(["--forward-subagent-text"])
        argv.extend(["--name", "qlab-workforce"])
        argv.extend(["--setting-sources", "project"])
    elif chat:
        # Same restriction mechanism as the workforce (verified live): the
        # selected agent's tools field IS the surface — read-only qlab tools,
        # no Agent dispatch, no built-ins.
        desk_agent = {
            "qlab-desk": {
                "description": "Conversational read-only qlab desk assistant.",
                "prompt": _CHAT_SYSTEM_PROMPT,
                "tools": list(_CHAT_TOOLS),
                "model": "inherit",
                "permissionMode": "dontAsk",
                "maxTurns": 16,
            }
        }
        argv[argv.index("--tools") + 1] = "default"
        argv.extend(["--allowedTools", ",".join(_CHAT_TOOLS)])
        argv.extend(["--agents", json.dumps(desk_agent)])
        argv.extend(["--agent", "qlab-desk"])
        argv.extend(["--permission-mode", "dontAsk"])
        argv.extend(["--name", "qlab-chat"])
        argv.extend(["--setting-sources", "project"])
    if resume_session:
        # Multi-turn chat: continue the persisted CLI session so the
        # coordinator keeps its conversation context between messages.
        argv.extend(["--resume", resume_session])
    # "--" so a prompt beginning with a dash is never parsed as a CLI flag.
    argv.extend(["--", prompt])
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

    def start(self, prompt: str, *, governed: bool = False,
              resume_session: str | None = None, chat: bool = False) -> bool:
        if self.running or not self.available:
            return False
        self.mode = ("workforce" if governed
                     else "chat" if chat else "read-only")
        argv = build_claude_argv(
            prompt,
            governed=governed,
            runtime_url=self.runtime_url,
            offline=self.offline,
            resume_session=resume_session,
            chat=chat,
        )
        env = os.environ.copy()
        process_cwd = self.cwd
        if governed or chat:
            env["CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS"] = "1"
            env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
            # Keep the workforce out of the source checkout's developer context
            # (CLAUDE.md, project agents, git state). The explicit inline agents
            # and strict owner proxy are the complete session configuration.
            process_cwd = Path(tempfile.gettempdir()) / "qlab-claude-workforce"
            process_cwd.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen(
            argv,
            cwd=process_cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
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
        # stderr must be drained concurrently: a long verbose workforce run
        # can fill the stderr pipe buffer before stdout closes, deadlocking
        # both the child (blocked write) and this reader (blocked read).
        stderr_tail: list[str] = []

        def drain_stderr() -> None:
            if self.process is None or self.process.stderr is None:
                return
            for line in self.process.stderr:
                stderr_tail.append(line)
                del stderr_tail[:-40]

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()
        for line in self.process.stdout:
            for event in parse_stream_line(line):
                self.on_event(event)
        returncode = self.process.wait()
        stderr_thread.join(timeout=5.0)
        stderr = "".join(stderr_tail).strip()
        if returncode and stderr:
            self.on_event(ClaudeEvent("error", stderr[-2000:]))
