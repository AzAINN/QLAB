"""``qlab`` command-line entrypoint — the standalone autopilot.

    qlab run-once   [--offline] [--dry-run]            one full pipeline iteration
    qlab watch      --interval 15m [--offline]         run run-once on a loop
    qlab daily-ops  [--offline]                        heartbeat (never trades)
    qlab autopilot  [--offline] [--once]               trading-day proposal loop
    qlab batch      <spec.yaml> [--offline]            the reproducible ablation
    qlab recommend  [--as-of DATE] [--offline]         print an allocation, no trade
    qlab prewarm    [--universe core|candidates]       pre-fill the data cache
    qlab ui         [--no-browser] [--port N]          owner runtime + web client
    qlab tui        [--claude offer|auto|off]          terminal operator console
    ui/tui          [--live] [--alpaca-book]           desk mode: which data, whose book
    qlab desk                                          one-card desk status
    qlab workforce  run "GOAL" | status | watch        governed runs from any shell
    qlab events     [--kind K]                         tail the live audit bus

All of it runs with zero external accounts thanks to ``--offline`` and the
simulated paper broker.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from qlab.autopilot.loop import daily_ops, render_summary, run_once
from qlab.autopilot.scheduler import is_trading_day, next_trading_morning
from qlab.core.desk_mode import DeskMode, load_desk_mode, save_desk_mode
from qlab.paths import workspace_root


def _f(x) -> str:
    """Format a possibly-None metric for the batch table."""
    return "n/a" if x is None else f"{x:.4f}"


def _parse_interval(s: str) -> int:
    s = s.strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s[-1] in mult:
        return int(float(s[:-1]) * mult[s[-1]])
    return int(float(s))


def _cmd_run_once(args) -> int:
    _refuse_second_writer("run-once")
    summary = run_once(offline=args.offline, execute=not args.dry_run)
    print(render_summary(summary))
    return 0


def _cmd_watch(args) -> int:
    _refuse_second_writer("watch")
    interval = _parse_interval(args.interval)
    print(f"[qlab] watch: running every {interval}s (Ctrl-C to stop). "
          f"Paper capital only.")
    try:
        while True:
            summary = run_once(offline=args.offline, execute=not args.dry_run)
            print(render_summary(summary))
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[qlab] watch stopped.")
    return 0


def _cmd_daily_ops(args) -> int:
    _refuse_second_writer("daily-ops")
    result = daily_ops(offline=args.offline)
    print(render_summary(result))
    return 0


def _print_autopilot_triggers(result: dict) -> None:
    triggers = result.get("triggers") or []
    if not triggers:
        print("[qlab] autopilot triggers: none")
        return
    print(f"[qlab] autopilot triggers fired: {len(triggers)}")
    for trigger in triggers:
        detail = json.dumps(
            trigger.get("detail", {}),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        print(f"  {trigger.get('kind', 'unknown')}: {detail}")
    plan_ids = result.get("proposal_plan_ids") or []
    if plan_ids:
        print(f"[qlab] proposal plan_ids: {', '.join(map(str, plan_ids))}")


def _cmd_autopilot(args) -> int:
    """Run proposal-only daily ops once or each supported trading morning."""
    _refuse_second_writer("autopilot")
    eastern = ZoneInfo("America/New_York")
    if args.once:
        today = datetime.now(eastern).date()
        if not is_trading_day(today):
            print(f"[qlab] autopilot skipped: {today} is not an NYSE trading day")
            return 0
        result = daily_ops(offline=args.offline)
        _print_autopilot_triggers(result)
        return 0

    print(
        "[qlab] autopilot: proposal-only daily ops at 09:30 America/New_York "
        "(Ctrl-C to stop)."
    )
    last_run_date = None
    try:
        while True:
            now = datetime.now(eastern)
            after_morning = (now.hour, now.minute) >= (9, 30)
            if (
                is_trading_day(now.date())
                and after_morning
                and last_run_date != now.date()
            ):
                result = daily_ops(offline=args.offline)
                _print_autopilot_triggers(result)
                last_run_date = now.date()
                now = datetime.now(eastern)

            next_run = next_trading_morning(now)
            delay = max(0.0, (next_run - now).total_seconds())
            print(f"[qlab] next proposal check: {next_run.isoformat()}")
            time.sleep(delay)
    except KeyboardInterrupt:
        print("\n[qlab] autopilot stopped.")
    return 0


def _cmd_batch(args) -> int:
    _refuse_second_writer("batch")

    from qlab.experiment import run_ablation

    report = run_ablation(args.spec, offline=args.offline)
    print(f"\nAblation run_id: {report['run_id']}")
    print(f"{'arm':>4}  {'sortino':>9}  {'ann_vol':>8}  {'maxDD':>8}  {'dSharpe':>8}")
    for row in report["ranking"]:
        print(f"{row['arm']:>4}  {_f(row.get('sortino')):>9}  "
              f"{_f(row.get('ann_vol')):>8}  {_f(row.get('max_drawdown')):>8}  "
              f"{_f(row.get('deflated_sharpe')):>8}")
    return 0


def _cmd_recommend(args) -> int:
    _refuse_second_writer("recommend")

    from qlab.experiment import recommend
    from qlab.trader.mandate import load_mandate

    rec = recommend(as_of=args.as_of, offline=args.offline,
                    policy_id=load_mandate().operational_policy)
    print(json.dumps(rec, indent=2, default=str))
    return 0


def desk_mode_from_args(args) -> DeskMode | None:
    """The mode an explicit flag selected, or None to ask / use the persisted one.

    ``--alpaca-book`` implies live: the real paper account is never reachable
    without naming it, and bare ``--live`` keeps the simulated book.
    """
    if getattr(args, "alpaca_book", False):
        return DeskMode("live", "alpaca")
    if getattr(args, "live", False) or getattr(args, "online", False):
        return DeskMode("live", "simulated")
    return None


def desk_mode_argv(mode: DeskMode | None) -> list[str]:
    """The flags that reproduce ``mode`` — the inverse of ``desk_mode_from_args``.

    The owner subprocess must be launched with the same words the TUI is about
    to display, or the two disagree about whose book is being traded. Synthetic
    needs no flag: it is the default on both sides.
    """
    if mode is None or mode.data != "live":
        return []
    return ["--live"] + (["--alpaca-book"] if mode.book == "alpaca" else [])


def _alpaca_credentials_resolve() -> bool:
    """Whether a credential is available at all.

    The same local env/profile resolution the owner reports as
    ``credentials_ok`` — file and environment only, never a network call.
    """
    from qlab.trader.alpaca_auth import AlpacaAuthError, resolve_alpaca_credentials

    try:
        return resolve_alpaca_credentials() is not None
    except AlpacaAuthError:
        return False


def startup_desk_mode(flagged: DeskMode | None) -> DeskMode | None:
    """The mode to start with given what a flag said, or None to ask.

    Precedence: explicit flag, then the persisted choice, then the modal. A flag
    is the operator speaking now, so it wins over — and replaces — what was
    persisted. A persisted choice makes startup silent, synthetic included:
    persistence is the only thing that separates "chose synthetic" from "has
    never chosen", which otherwise look identical.

    The one persisted mode not reused silently is a live one whose credential no
    longer resolves. Asking again is not a downgrade — nothing is rewritten and
    no safer-looking mode is assumed; the choice goes back to the operator,
    which is what a credential that has gone away actually means.
    """
    if flagged is not None:
        save_desk_mode(flagged)
        return flagged
    persisted = load_desk_mode()
    if persisted is None or persisted.offline or _alpaca_credentials_resolve():
        return persisted
    return None


def _cmd_ui(args) -> int:
    from qlab.ui.server import serve

    _publish_owner_port(args.port)
    # No modal on this surface, so an unflagged run is handed no guess: the
    # session loads the persisted mode itself (and defaults to synthetic).
    mode = desk_mode_from_args(args)
    if mode is not None:
        # The session holds its mode in memory only, and `qlab ui` is a
        # first-class entry point: without this, a later `qlab tui` attaching to
        # this owner would read a stale file and show a desk nobody is trading.
        save_desk_mode(mode)
    serve(port=args.port,
          offline=not args.online if mode is None else mode.offline,
          open_browser=not args.no_browser, desk_mode=mode)
    return 0


def _refuse_second_writer(command: str, port: int = 0) -> None:
    """Refuse a direct-registry command while an owner runtime owns the book.

    These commands construct their own `Registry`, which opens
    `.lab/registry.duckdb`. DuckDB permits exactly one writer, so running one
    while `qlab tui` is up died on a raw lock error naming a pid — for the
    documented invocation, in a second shell, which is precisely how the
    README tells an operator to use them. Refuse in the project's own terms
    instead, and name the way through.
    """
    from qlab.mcp.server import owner_runtime_alive

    resolved = int(port or os.environ.get("QLAB_UI_PORT", "8765"))
    if not owner_runtime_alive(resolved):
        return
    raise SystemExit(
        f"qlab {command} cannot run right now: an owner runtime on port "
        f"{resolved} already owns the paper book, and DuckDB allows one "
        "writer. Stop the desk (or use `qlab desk`, `qlab workforce` and "
        "`qlab events`, which speak to the owner over HTTP)."
    )


def _publish_owner_port(port: int) -> None:
    """Tell descendants which port the owner is on.

    The combined MCP server refuses to start while an owner is alive, but it
    reads the port from `QLAB_UI_PORT` and nothing set it — so on any
    non-default port the guard probed 8765, found nothing, and opened the
    registry as a second writer. A Claude session launched from the desk
    inherits this environment, and `.mcp.json` starts that server.
    """
    os.environ["QLAB_UI_PORT"] = str(int(port))


class _OwnerStderrTail:
    """Continuously drain a child's stderr, keeping the last lines.

    A PIPE nobody reads deadlocks the child once the OS buffer (64 KB) fills,
    so the drain must run for the child's whole life, not just at failure
    time. A bounded deque keeps the part worth showing — the end, where the
    traceback is — without growing with a chatty child.
    """

    def __init__(self, process: subprocess.Popen):
        self._lines: deque[str] = deque(maxlen=200)
        self._thread = threading.Thread(
            target=self._drain,
            args=(process.stderr,),
            daemon=True,
            name="qlab-owner-stderr",
        )
        self._thread.start()

    def _drain(self, pipe) -> None:
        if pipe is None:
            return
        try:
            for line in pipe:
                self._lines.append(line.rstrip("\n"))
        except (OSError, ValueError):
            # The pipe object was closed under the read during teardown; the
            # tail keeps whatever was drained before that.
            return

    def tail(self, join_timeout: float = 1.0) -> str:
        """The last stderr lines; call after the child has been stopped."""
        self._thread.join(join_timeout)
        return "\n".join(self._lines).strip()


_ATLAS_RELATIVE = ("clients", "atlas-tui", "target", "release", "atlas")


def _atlas_binary() -> str:
    """Where the Ratatui workstation lives, in the order the desk resolves it.

    An explicit override first (``QLAB_ATLAS_BIN``), then whatever is on PATH —
    a ``cargo install``ed build, or a packaged one — and only then this
    checkout's own release binary. A developer's rebuild should win over a
    stale installed copy only when they say so, which is what the override is.
    """
    import shutil

    override = os.environ.get("QLAB_ATLAS_BIN", "").strip()
    if override:
        return override
    installed = shutil.which("atlas")
    if installed:
        return installed
    binary = workspace_root().joinpath(*_ATLAS_RELATIVE)
    # Cargo names the artifact atlas.exe on Windows; the extensionless check
    # would report "not built" over a binary sitting right there.
    if os.name == "nt" and not binary.exists():
        exe = binary.with_suffix(".exe")
        if exe.exists():
            return str(exe)
    return str(binary)


def atlas_client_argv(binary: str, *, operator: bool, offline: bool) -> list[str]:
    """The workstation's argv: the launcher forwards posture and data lane.

    Passthroughs, not a second authority: this launcher does not decide what
    the client may do — a binary built without the operator feature refuses
    the flag itself, and the lane only chooses which view of the owner the
    client polls. Dropping the lane here meant a live owner drawn by a client
    that only ever asked for the offline view.
    """
    argv = [binary]
    if operator:
        argv.append("--operator")
    if not offline:
        argv.append("--live")
    return argv


def _cmd_tui(args) -> int:
    """Launch the terminal workstation and, when needed, its owner API process.

    Two clients over one owner. The default is the Ratatui workstation in
    ``clients/atlas-tui``; ``--classic`` keeps the Textual client, which is the
    soak valve — a week of real desk use can find a parity gap while rolling
    back is one flag rather than a revert.

    The owner spawn and readiness wait below are shared by both, deliberately:
    which client is drawn must not change what starts the desk, or a difference
    between the two would be a difference in the runtime rather than in the
    screen.
    """
    classic = bool(getattr(args, "classic", False))
    if classic and getattr(args, "operator", False):
        # Loud, not silent. `--operator` is a word only the Ratatui client
        # understands, and the Textual client has no posture to arm — it is the
        # complete surface and already reaches the confirm gate. Dropping the
        # flag would leave an operator believing they had asked for something.
        raise SystemExit(
            "--operator is a Ratatui-workstation flag and --classic is the "
            "Textual client, which has no operator posture to arm.\n"
            "    qlab tui --operator     the armed workstation\n"
            "    qlab tui --classic      the Textual client (already complete)"
        )
    try:
        from qlab.tui.client import ApiClient
    except ImportError as exc:
        raise SystemExit(
            "The TUI extra is not installed. Run:\n"
            "    pip install -e '.[operator]'\n"
            f"(original error: {exc})"
        ) from exc

    # Only a flag may retune an owner that is already running, so what the flags
    # said is kept apart from what startup resolved. A resolved mode of None
    # means the TUI asks on mount.
    _publish_owner_port(args.port)
    flagged = desk_mode_from_args(args)
    mode = startup_desk_mode(flagged)
    offline = not args.online if mode is None else mode.offline

    def port_open() -> bool:
        with socket.socket() as sock:
            sock.settimeout(0.2)
            return sock.connect_ex(("127.0.0.1", args.port)) == 0

    # A cold first launch imports the full scientific stack (numpy/scipy/pandas/
    # duckdb) and, on a fresh install, macOS scans each newly-downloaded native
    # extension the first time it loads — which can push the owner's startup
    # well past a few seconds. Wait generously; a still-not-ready owner is only
    # an error once this budget is spent.
    startup_budget_s = 45.0

    owner = None
    already_open = port_open()
    client = ApiClient(f"http://127.0.0.1:{args.port}")
    if not already_open:
        server_argv = [
            sys.executable, "-m", "qlab.autopilot.cli", "ui",
            "--port", str(args.port), "--no-browser",
            *desk_mode_argv(mode),
        ]
        owner = subprocess.Popen(
            server_argv,
            cwd=str(workspace_root()),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            # Its own process group: the owner must outlive the client, and on
            # Windows a child in the launcher's group receives the console's
            # Ctrl-C — so quitting the workstation would kill the runtime the
            # banner just promised keeps running. POSIX inherits no such
            # coupling for a detached-intent daemon, but the same isolation is
            # what claude.py uses for its trees; imitate it.
            **({"creationflags":
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
               if os.name == "nt" else {"start_new_session": True}),
        )
        # The pipe is kept (rather than DEVNULL) because failures are diagnosed
        # from it — but a pipe nobody reads is a 64 KB fuse: once the owner has
        # written that much it blocks on the next write and the whole desk
        # wedges with no diagnostic. Drain continuously into a bounded tail.
        stderr_tail = _OwnerStderrTail(owner)
        print(
            f"Starting the qlab runtime on port {args.port} "
            "(first launch compiles imports; this can take up to a minute)...",
            flush=True,
        )

        def _owner_stderr(process: subprocess.Popen) -> str:
            # Stop the child first so the drain thread sees EOF and the tail
            # is complete when read.
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            return stderr_tail.tail()

        deadline = time.monotonic() + startup_budget_s
        last_probe_error = ""
        while time.monotonic() < deadline:
            if owner.poll() is not None:
                detail = stderr_tail.tail()
                raise SystemExit(
                    "qlab UI runtime exited before the TUI connected"
                    + (f":\n{detail}" if detail else "")
                )
            try:
                client.probe()
            except Exception as exc:
                last_probe_error = str(exc) or repr(exc)
            else:
                break
            time.sleep(0.2)
        else:
            detail = _owner_stderr(owner)
            raise SystemExit(
                f"qlab UI runtime was not ready on port {args.port} within "
                f"{int(startup_budget_s)}s"
                + (f":\n{detail}" if detail else
                   f"; last readiness error: {last_probe_error or 'none'}. "
                   "Startup may be unusually slow. Try `qlab ui --no-browser` "
                   "in a separate terminal to see its output.")
            )

    try:
        client.get("/api/system", offline=int(offline))
    except Exception as exc:
        if owner is not None:
            owner.terminate()
        raise SystemExit(
            f"port {args.port} is open but is not a compatible qlab runtime: {exc}") from exc

    if owner is None and flagged is not None:
        # An owner we did not spawn read its mode at construction; a flag has to
        # reach it over the API or the TUI would show a book the owner is not
        # trading. Refuse loudly rather than run with the two disagreeing.
        try:
            client.post("/api/desk_mode", {"data": mode.data, "book": mode.book})
        except Exception as exc:
            raise SystemExit(
                f"the qlab runtime already on port {args.port} would not accept "
                f"the requested desk mode ({mode.label}): {exc}") from exc

    if classic:
        # Imported here rather than beside `ApiClient` above, so the default
        # path never pays for Textual — and wrapped, because the rollback valve
        # is exactly the thing that must not fail obscurely. An operator
        # reaching for `--classic` is already having a bad day; a bare
        # `ModuleNotFoundError: textual` names no remedy.
        try:
            from qlab.tui.app import QlabTui
        except ImportError as exc:
            raise SystemExit(
                "The TUI extra is not installed. Run:\n"
                "    pip install -e '.[operator]'\n"
                f"(original error: {exc})"
            ) from exc

        QlabTui(
            client,
            offline=offline,
            refresh_interval=args.refresh,
            owned_server=owner,
            claude_start=args.claude,
            desk_mode=mode,
        ).run()
        return 0

    binary = _atlas_binary()
    # `isfile` as well as `access`: X_OK is true of every directory, so a path
    # that resolved to `clients/atlas-tui/target/release/` — an interrupted
    # build leaves exactly that — would pass the check and fail at exec, after
    # the owner had already been started.
    if not (os.path.isfile(binary) and os.access(binary, os.X_OK)):
        # Fail loud, and never fall back to the Textual client. Silently running
        # a different client would make `--classic` unfalsifiable as a soak
        # valve: an operator soaking the workstation would have spent the week
        # on the other one with no way to tell.
        raise SystemExit(
            f"the Atlas workstation is not built at {binary}\n"
            "    cd clients/atlas-tui && cargo build --release\n"
            "or run the Textual client instead:  qlab tui --classic\n"
            "($QLAB_ATLAS_BIN overrides where this looks.)"
        )
    argv = atlas_client_argv(
        binary, operator=getattr(args, "operator", False), offline=offline)
    if owner is not None:
        # `execvpe` replaces this process, so nothing is left to terminate the
        # owner when the client quits — say so rather than leaving an operator
        # to discover a runtime they did not know was still up. It is also what
        # the next `qlab tui` attaches to.
        print(
            f"The qlab runtime on port {args.port} keeps running after the "
            "workstation exits; the next `qlab tui` attaches to it.",
            flush=True,
        )
    # In place rather than as a child: the launcher has nothing left to do, and
    # a Python process parked on top of a fullscreen client is one more thing
    # between an operator's Ctrl-C and the terminal being restored.
    #
    # `_publish_owner_port` already put the port in this environment; passing it
    # explicitly is what makes the handover a statement rather than an
    # inheritance nobody can see.
    env = dict(os.environ, QLAB_UI_PORT=str(args.port))
    if os.name == "nt":
        # exec* on Windows is spawn-then-exit-parent: the shell sees the
        # launcher die and prints its prompt INTO the fullscreen client, and
        # console Ctrl-C plumbing goes to a process that no longer exists.
        # A plain child + wait is the faithful translation; the exit code is
        # carried so a supervisor reads the client's, not the launcher's.
        completed = subprocess.run([binary] + argv[1:], env=env)
        raise SystemExit(completed.returncode)
    os.execvpe(binary, argv, env)
    raise SystemExit(f"could not exec the Atlas workstation at {binary}")


def _cmd_prewarm(args) -> int:
    from qlab.core import data as market
    from qlab.core.universe import load_universe

    uni = load_universe()
    tickers = uni.tickers(args.universe)
    df = market.get_prices(
        tickers,
        "2008-01-01",
        offline=args.offline,
        refresh=not args.offline,
    )
    source = df.attrs.get(
        "source", "synthetic" if df.attrs.get("synthetic") else "unknown",
    )
    print(f"[qlab] cached {df.shape[0]} rows x {df.shape[1]} tickers "
          f"({args.universe}, source={source}); demo is now network-independent.")
    if not args.offline and source == "synthetic":
        print(
            "[qlab] real-data prewarm failed; synthetic fallback is cached but "
            "does not satisfy the online data gate.",
            file=sys.stderr,
        )
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qlab", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp):
        sp.add_argument("--offline", action="store_true",
                        help="refuse the network; serve cache/synthetic only")

    ro = sub.add_parser("run-once", help="one full pipeline iteration")
    add_common(ro)
    ro.add_argument("--dry-run", action="store_true", help="analyze + propose, do not trade")
    ro.set_defaults(func=_cmd_run_once)

    w = sub.add_parser("watch", help="run run-once on an interval")
    add_common(w)
    w.add_argument("--interval", default="15m", help="e.g. 30s, 15m, 1h")
    w.add_argument("--dry-run", action="store_true")
    w.set_defaults(func=_cmd_watch)

    do = sub.add_parser("daily-ops", help="heartbeat; reconcile + risk, never trades")
    add_common(do)
    do.set_defaults(func=_cmd_daily_ops)

    ap = sub.add_parser(
        "autopilot",
        help="run proposal-only daily ops on NYSE trading mornings",
    )
    add_common(ap)
    ap.add_argument(
        "--once",
        action="store_true",
        help="run once on a trading day (for cron/tests), then exit",
    )
    ap.set_defaults(func=_cmd_autopilot)

    b = sub.add_parser("batch", help="run the reproducible ablation from a spec")
    add_common(b)
    b.add_argument("spec", help="path to a spec YAML (configs/specs/ablation_v1.yaml)")
    b.set_defaults(func=_cmd_batch)

    r = sub.add_parser("recommend", help="print an allocation recommendation (no trade)")
    add_common(r)
    r.add_argument("--as-of", default=None, help="YYYY-MM-DD (default: today)")
    r.set_defaults(func=_cmd_recommend)

    pw = sub.add_parser("prewarm", help="pre-fill the data cache for demo resilience")
    add_common(pw)
    pw.add_argument("--universe", default="core", choices=["core", "candidates"])
    pw.set_defaults(func=_cmd_prewarm)

    nc = sub.add_parser(
        "news-check",
        help="authenticate the news integration and show what it returns")
    nc.add_argument("--provider", default=None,
                    help="override QLAB_NEWS_PROVIDER for this check")
    nc.set_defaults(func=_cmd_news_check)

    def add_desk_mode(sp):
        # Two words, because the safe choice is the default: bare --live keeps
        # the simulated book, so reaching the real paper account is never a
        # side effect of asking for real prices. --live switches the data lane
        # online only; which provider serves it is QLAB_DATA_PROVIDER's job, and
        # its `alpaca` option reads env API keys, not the browser login.
        sp.add_argument("--live", action="store_true",
                        help="online market data from QLAB_DATA_PROVIDER "
                             "(yfinance by default; simulated book)")
        sp.add_argument("--alpaca-book", action="store_true",
                        help="trade your Alpaca paper book (implies --live)")

    ui = sub.add_parser("ui", help="launch the single-page web UI (no CLI needed after)")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--online", action="store_true",
                    help="same as --live (default: offline synthetic)")
    add_desk_mode(ui)
    ui.add_argument("--no-browser", action="store_true", help="don't auto-open the browser")
    ui.set_defaults(func=_cmd_ui)

    tui = sub.add_parser("tui", help="launch the terminal operator console")
    tui.add_argument("--port", type=int, default=8765)
    tui.add_argument("--online", action="store_true",
                     help="same as --live (default: offline synthetic)")
    add_desk_mode(tui)
    tui.add_argument("--refresh", type=float, default=2.0,
                     help="snapshot refresh interval in seconds")
    tui.add_argument(
        "--claude", choices=["offer", "auto", "off"], default="offer",
        help=("Claude workforce startup: show readiness without prompting (offer), "
              "launch automatically (auto), or keep it off until requested (off)"),
    )
    # The soak valve. Both clients read the same owner over HTTP, so this
    # changes which screen is drawn and nothing about what is running.
    tui.add_argument(
        "--classic", action="store_true",
        help="use the Textual client instead of the Ratatui workstation")
    # Passthrough. The word reaches the workstation and that is all this
    # launcher does with it.
    #
    # A binary built without the feature does *not* refuse it: the flag parses,
    # the posture compiles to Glass, and the window is read-only with nothing to
    # arm. That is safe by construction rather than by check — there is no write
    # path in the artifact — and the mitigation for the operator who expected
    # otherwise is the GLASS chip the status line carries on every frame.
    tui.add_argument(
        "--operator", action="store_true",
        help="arm the workstation's write affordances (Ratatui client only; a "
             "binary built without the feature stays GLASS and says so)")
    tui.set_defaults(func=_cmd_tui)

    from qlab.desk_cli import register_subcommands

    register_subcommands(sub, add_common)

    return p



def _cmd_news_check(args) -> int:
    """Authenticate the news integration and report what it actually does."""
    from qlab.news.check import check_news, render
    from qlab.trader.mandate import load_mandate

    universe = load_mandate().universe_whitelist
    report = check_news(universe, provider=getattr(args, "provider", None))
    print(render(report))
    return 0 if report.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    # Load .env before anything reads a credential. Without this, filling in
    # .env.example did nothing and the desk silently stayed on synthetic data.
    from qlab.env import load_once

    load_once()
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
