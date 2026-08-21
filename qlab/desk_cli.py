"""Research-desk CLI verbs — watch the workforce reason from any shell.

The one-shot commands here are the terminal face of the owner runtime,
patterned after research CLIs that submit work and then tail a live event
stream. Everything talks HTTP to the owner (never DuckDB), so these verbs
compose with the TUI and web client instead of competing with them:

    qlab desk                       one status card for the whole desk
    qlab workforce run "GOAL"       headless governed run, streamed live
    qlab workforce status [ID]      durable phase state of a run
    qlab workforce watch [ID]       tail a run someone else is driving
    qlab workforce interrupt --id ID freeze an orphan for explicit resumption
    qlab workforce abandon --id ID permanently close it; retain its audit
    qlab events [--kind K]          tail the owner event bus

Requires the ``operator`` extra (rich + httpx). Paper execution is not
reachable from here; the confirm dialog lives in the TUI alone.
"""

from __future__ import annotations

import sys
from typing import Any


_PHASE_GLYPHS = {
    "queued": ("◐", "gold"),
    "working": ("●", "info"),
    "done": ("✓", "ok"),
    "failed": ("×", "bad"),
    "blocked": ("!", "gold"),
    "interrupted": ("Ⅱ", "gold"),
    "abandoned": ("×", "muted"),
}

_EVENT_STYLES = {
    "workflow_started": "accent",
    "workflow_phase": "accent",
    "workflow_interrupted": "gold",
    "workflow_resumed": "accent",
    "workflow_abandoned": "muted",
    "referee_verdict": "ok",
    "tool_call": "info",
    "quote": "info",
    "plan_built": "gold",
    "order_filled": "gold",
    "mandate_violation": "bad",
}


def make_console(*, record: bool = False, width: int | None = None):
    """The desk console: the TUI's palette, importable without textual."""
    try:
        from rich.console import Console
        from rich.theme import Theme
    except ImportError:
        print("qlab desk verbs need the operator extra: "
              "pip install -e '.[operator]'", file=sys.stderr)
        raise SystemExit(2)
    from qlab.tui.theme import AMBER, CYAN, DOWN, GOLD, MUTED, TEXT, UP

    return Console(theme=Theme({
        "accent": f"bold {AMBER}",
        "gold": GOLD,
        "ok": f"bold {UP}",
        "bad": f"bold {DOWN}",
        "info": CYAN,
        "muted": MUTED,
        "value": TEXT,
    }), highlight=False, record=record, width=width)


def _console():
    return make_console()


def _client(args):
    from qlab.tui.client import ApiClient

    base = f"http://127.0.0.1:{getattr(args, 'port', 8765)}"
    client = ApiClient(base)
    try:
        client.get("/api/system", offline=1)
    except Exception:
        console = _console()
        console.print(f"[bad]✗[/bad] no owner runtime at {base}")
        console.print("[muted]  start one with `qlab` or "
                      "`qlab owner`, or pass --port[/muted]")
        raise SystemExit(1)
    return client


def _post_control(client, path: str, body: dict) -> dict:
    """Use the short lifecycle deadline when the HTTP client supports it."""
    post = getattr(client, "post_control", client.post)
    return post(path, body)


def _banner(console, title: str, subtitle: str = "") -> None:
    console.print()
    console.print(f"[accent]▌ {title}[/accent]")
    if subtitle:
        console.print(f"[muted]  {subtitle}[/muted]")
    console.print()


def _shorten(text: str, width: int) -> str:
    text = str(text)
    return text if len(text) <= width else text[: width - 1] + "…"


# ── event + phase rendering (pure; unit-tested) ─────────────────────────


def format_event(event: dict) -> tuple[str, str]:
    """One compact styled line per bus event: (style, text)."""
    ts = str(event.get("ts", ""))
    clock = ts[11:19] if len(ts) >= 19 else "        "
    kind = str(event.get("kind", "event"))
    payload = event.get("payload") or {}
    if kind == "quote":
        rows = payload.get("rows")
        quote_rows = rows if isinstance(rows, list) else []
        parts = []
        for row in quote_rows[:3]:
            if not isinstance(row, dict):
                continue
            ticker = str(row.get("ticker") or "—")
            try:
                price = f"{float(row.get('price')):.2f}"
                change = f"{float(row.get('change_1d')):+.1%}"
            except (TypeError, ValueError):
                price, change = "—", "—"
            parts.append(f"{ticker} {price} {change}")
        parts.append(f"count={len(quote_rows)}")
    else:
        parts = [f"{key}={_shorten(value, 36)}" for key, value in
                 list(payload.items())[:3]]
    style = _EVENT_STYLES.get(kind, "muted")
    if kind == "referee_verdict" and payload.get("verdict") != "PASS":
        style = "bad"
    return style, f"{clock}  {kind:<18} {'  '.join(parts)}".rstrip()


def phase_rows(workflow: dict) -> list[tuple[str, str, str, str, str]]:
    """(glyph_style, glyph, phase, status, summary) per step."""
    rows = []
    for step in workflow.get("steps", []):
        status = str(step.get("status", "queued"))
        glyph, style = _PHASE_GLYPHS.get(status, ("◌", "muted"))
        rows.append((style, glyph, str(step.get("phase", "")),
                     status, _shorten(step.get("summary") or "", 64)))
    return rows


def render_workflow(console, workflow: dict) -> None:
    request = workflow.get("request") or {}
    console.print(
        f"[value]{workflow.get('workflow_id', '—')}[/value]  "
        f"[accent]{str(workflow.get('status', '')).upper()}[/accent]  "
        f"[muted]as of {request.get('as_of', '—')} · "
        f"{request.get('universe', 'core')}[/muted]")
    goal = _shorten(request.get("goal") or "", 88)
    if goal:
        console.print(f"[muted]{goal}[/muted]")
    for style, glyph, phase, status, summary in phase_rows(workflow):
        line = f"  [{style}]{glyph}[/{style}] {phase:<12} {status:<8}"
        console.print(line + (f" [muted]{summary}[/muted]" if summary else ""))
    result = workflow.get("result") or {}
    if result.get("final_summary"):
        console.print(
            f"[ok]▮[/ok] [value]{_shorten(result['final_summary'], 200)}[/value]")


def render_desk(console, snap: dict) -> None:
    """The whole desk on one card, from a single /api/tui payload."""
    from rich.table import Table

    from qlab.tui.theme import BORDER

    portfolio = snap.get("portfolio", {})
    market = snap.get("market", {})
    system = snap.get("system", {})
    policy = snap.get("policy") or {}
    regime = market.get("regime", {})

    _banner(console, "qlab desk",
            f"paper · {str(market.get('source', 'data')).upper()} · "
            f"as of {market.get('as_of', '—')} · "
            f"bar age {market.get('bar_age_days', '—')}d")
    console.print(
        f"  equity [value]${portfolio.get('equity', 0):,.2f}[/value]"
        f"   cash [value]${portfolio.get('cash', 0):,.2f}[/value]"
        f"   drawdown [value]{portfolio.get('drawdown', 0):.2%}[/value]"
        f"   kill at [gold]{portfolio.get('kill_switch_at', 0):.0%}[/gold]")
    console.print(
        f"  policy [accent]{policy.get('label', policy.get('id', '—'))}[/accent]"
        f"   regime [value]{str(regime.get('regime', 'unknown')).upper()}[/value]"
        f"   workforce "
        + ("[ok]ready[/ok]" if system.get("workforce_available")
           else "[muted]unavailable[/muted]"))

    weights = portfolio.get("weights", {})
    if weights:
        table = Table(show_header=True, header_style="muted",
                      border_style=BORDER, padding=(0, 1))
        table.add_column("ticker", style="value")
        table.add_column("weight", justify="right", style="info")
        table.add_column("target", justify="right", style="muted")
        targets = portfolio.get("target_weights", {})
        for ticker in sorted(weights, key=weights.get, reverse=True):
            target = targets.get(ticker)
            table.add_row(
                ticker, f"{weights[ticker]:.1%}",
                "—" if target is None else f"{target:.1%}")
        console.print(table)

    workflows = snap.get("workflows") or []
    if workflows:
        console.print()
        console.print("[muted]latest workforce run:[/muted]")
        render_workflow(console, workflows[0])
    console.print()
    console.print(
        f"[muted]{len(snap.get('decisions') or [])} decisions · "
        f"{len(snap.get('plans') or [])} proposals · "
        f"{len(snap.get('orders') or [])} orders · "
        f"algorithms {len(snap.get('algorithms') or [])} cataloged[/muted]")


# ── commands ────────────────────────────────────────────────────────────


def cmd_desk(args) -> int:
    console = _console()
    client = _client(args)
    render_desk(console, client.get(
        "/api/tui", offline=int(args.offline), event_limit=20))
    return 0


def cmd_events(args) -> int:
    console = _console()
    client = _client(args)
    console.print("[muted]tailing /api/stream · Ctrl-C to exit[/muted]")
    try:
        params: dict[str, Any] = {}
        if args.kind:
            params["kind"] = args.kind
        for event in client.stream("/api/stream", **params):
            style, line = format_event(event)
            console.print(f"[{style}]{line}[/{style}]")
    except KeyboardInterrupt:
        console.print("[muted]stopped[/muted]")
    return 0


def cmd_workforce(args) -> int:
    console = _console()
    client = _client(args)
    action = args.action

    if action == "status":
        workflows = client.get("/api/workflows", limit=10).get("workflows", [])
        target = _pick_workflow(workflows, args.id)
        if target is None:
            console.print("[muted]no workforce runs recorded yet[/muted]")
            return 0
        render_workflow(console, target)
        return 0

    if action == "watch":
        workflows = client.get("/api/workflows", limit=10).get("workflows", [])
        target = _pick_workflow(workflows, args.id)
        if target is not None:
            render_workflow(console, target)
        console.print("[muted]tailing the run · Ctrl-C detaches; the owner "
                      "keeps durable state[/muted]")
        try:
            for event in client.stream("/api/stream"):
                style, line = format_event(event)
                console.print(f"[{style}]{line}[/{style}]")
        except KeyboardInterrupt:
            console.print("[muted]detached[/muted]")
        return 0

    if action in {"interrupt", "abandon"}:
        if not args.id:
            console.print(
                f"[bad]✗[/bad] `qlab workforce {action}` requires --id")
            return 2
        reason = (
            "operator interrupted the workflow from the desk CLI"
            if action == "interrupt"
            else "operator permanently closed the workflow from the desk CLI"
        )
        try:
            workflow = _post_control(
                client,
                f"/api/workflows/{args.id}/{action}", {"reason": reason})
        except Exception as exc:
            console.print(f"[bad]✗ {exc}[/bad]")
            return 1
        render_workflow(console, workflow)
        if action == "interrupt":
            console.print(
                "[muted]durable writes are fenced; resume explicitly with "
                f"`qlab workforce run --resume {args.id}`[/muted]")
        else:
            console.print(
                "[muted]closed permanently; completed evidence and audit "
                "records were retained[/muted]")
        return 0

    # action == "run"
    return _run_workforce(console, client, args)


def _pick_workflow(workflows: list[dict], workflow_id: str | None):
    if workflow_id:
        for workflow in workflows:
            if workflow.get("workflow_id") == workflow_id:
                return workflow
        return None
    return workflows[0] if workflows else None


def _run_workforce(console, client, args) -> int:
    """Headless governed run: Claude coordinator + live owner event tail."""
    import queue
    import threading

    from qlab.paths import workspace_root
    from qlab.tui.claude import ClaudeSession

    goal = (args.goal or "").strip()
    prompt = f"GOAL: {goal}" if goal else (
        "GOAL: Review the current portfolio and market regime, challenge the "
        "estimation choices, compare the operational allocation policy with "
        "honest benchmarks, apply the referee gate, and prepare a dry human "
        "recommendation.")
    if args.resume:
        prompt = (f"RESUME_WORKFLOW_ID: {args.resume}\n"
                  "Inspect workflow.status first. Continue at the first "
                  f"non-done phase; do not create a new workflow.\n{prompt}")

    feed: queue.Queue = queue.Queue()
    known_workflows = {
        str(row.get("workflow_id") or "")
        for row in client.get("/api/workflows", limit=1000).get("workflows", [])
    }
    active_workflow_id = str(args.resume or "")
    session = ClaudeSession(
        lambda event: feed.put(("claude", event)),
        cwd=workspace_root(), runtime_url=client.base_url,
        offline=bool(args.offline))
    if not session.available:
        console.print("[bad]✗[/bad] the `claude` CLI is not on PATH")
        return 1

    if args.resume:
        try:
            _post_control(
                client,
                f"/api/workflows/{args.resume}/resume",
                {"reason": "operator explicitly resumed the workflow"},
            )
        except Exception as exc:
            console.print(f"[bad]✗ could not resume {args.resume}: {exc}[/bad]")
            return 1

    def resolve_active_workflow() -> str:
        nonlocal active_workflow_id
        if not active_workflow_id:
            try:
                rows = client.get(
                    "/api/workflows", limit=20).get("workflows", [])
            except Exception:
                rows = []
            active_workflow_id = next((
                str(row.get("workflow_id") or "")
                for row in rows
                if str(row.get("workflow_id") or "") not in known_workflows
                and str(row.get("status") or "") == "running"
            ), "")
            if active_workflow_id:
                known_workflows.add(active_workflow_id)
        return active_workflow_id

    def interrupt_active(reason: str) -> None:
        resolve_active_workflow()
        if not active_workflow_id:
            return
        try:
            _post_control(
                client,
                f"/api/workflows/{active_workflow_id}/interrupt",
                {"reason": reason},
            )
        except Exception as exc:
            console.print(
                f"[bad]✗ could not freeze {active_workflow_id}: {exc}[/bad]")

    def tail() -> None:
        try:
            for event in client.stream("/api/stream"):
                feed.put(("bus", event))
        except Exception:
            pass  # stream ends with the process; the Claude feed continues

    threading.Thread(target=tail, daemon=True).start()
    _banner(console, "workforce run",
            args.resume and f"resuming {args.resume}" or goal or "default review")
    if not session.start(prompt, governed=True):
        if args.resume:
            interrupt_active("Claude could not start the resumed workflow")
        console.print("[bad]✗[/bad] could not start the Claude session")
        return 1

    mid_line = False
    exit_code = 0
    try:
        while True:
            source, event = feed.get()
            if source == "bus":
                if event.get("kind") == "workflow_started":
                    # The SSE primer may contain an old workflow.start outside
                    # the newest registry window. Resolve against current
                    # owner state instead of trusting that historical payload.
                    resolve_active_workflow()
                if event.get("kind") not in _EVENT_STYLES:
                    continue
                if mid_line:
                    console.print()
                    mid_line = False
                style, line = format_event(event)
                console.print(f"[{style}]{line}[/{style}]")
                continue
            if event.kind == "text_delta":
                console.print(event.text, end="", style="value",
                              highlight=False, markup=False)
                mid_line = not event.text.endswith("\n")
            elif event.kind == "text":
                if mid_line:
                    console.print()
                    mid_line = False
                console.print(event.text, style="value",
                              highlight=False, markup=False)
            elif event.kind == "tool_start":
                if mid_line:
                    console.print()
                    mid_line = False
                agent = f" [{event.agent}]" if event.agent else ""
                console.print(f"[info]→ {event.tool}{agent}[/info]")
            elif event.kind == "error":
                if mid_line:
                    console.print()
                interrupt_active(
                    f"coordinator exited before completion: {event.text[-600:]}")
                console.print(f"[bad]✗ {event.text}[/bad]", markup=False)
                exit_code = 1
                break
            elif event.kind == "result":
                if mid_line:
                    console.print()
                # A CLI success result can still arrive before all durable
                # phases close; interrupt is idempotent for a completed run.
                interrupt_active(
                    "coordinator returned before the durable workflow completed")
                break
    except KeyboardInterrupt:
        interrupt_active("operator interrupted the headless workforce")
        session.stop("operator interrupted the headless workforce")
        console.print("\n[muted]interrupted — the full Claude/Agent/MCP "
                      "process tree was stopped and durable phase state is kept; "
                      "resume with `qlab workforce run --resume ID`[/muted]")
        return 130

    console.print()
    workflows = client.get("/api/workflows", limit=1).get("workflows", [])
    if workflows:
        render_workflow(console, workflows[0])
    return exit_code


def register_subcommands(sub, add_common) -> None:
    """Mount the desk verbs on the main ``qlab`` argparse parser."""
    desk = sub.add_parser("desk", help="one status card for the whole desk")
    add_common(desk)
    desk.add_argument("--port", type=int, default=8765)
    desk.set_defaults(func=cmd_desk)

    wf = sub.add_parser(
        "workforce", help="run, inspect, or tail a governed workforce run")
    add_common(wf)
    wf.add_argument(
        "action",
        choices=["run", "status", "watch", "interrupt", "abandon"],
    )
    wf.add_argument("goal", nargs="?", default="",
                    help="research goal for `run`")
    wf.add_argument(
        "--id",
        default=None,
        help="workflow id for status/watch/interrupt/abandon",
    )
    wf.add_argument("--resume", default=None,
                    help="resume an interrupted run by workflow id")
    wf.add_argument("--port", type=int, default=8765)
    wf.set_defaults(func=cmd_workforce)

    ev = sub.add_parser("events", help="tail the live owner event bus")
    add_common(ev)
    ev.add_argument("--kind", default=None, help="filter to one event kind")
    ev.add_argument("--port", type=int, default=8765)
    ev.set_defaults(func=cmd_events)
