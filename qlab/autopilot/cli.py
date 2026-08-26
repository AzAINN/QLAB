"""``qlab`` command-line entrypoint — the standalone autopilot.

    qlab run-once   [--offline] [--dry-run]            one full pipeline iteration
    qlab watch      --interval 15m [--offline]         run run-once on a loop
    qlab daily-ops  [--offline]                        heartbeat (never trades)
    qlab autopilot  [--offline] [--once]               trading-day proposal loop
    qlab batch      <spec.yaml> [--offline]            the reproducible ablation
    qlab recommend  [--as-of DATE] [--offline]         print an allocation, no trade
    qlab prewarm    [--universe core|candidates]       pre-fill the data cache
    qlab            [--restart] [--port N]             the desk (owner + workstation)
    qlab owner      [--port N]                         owner runtime, headless
    qlab tui        [--claude offer|auto|off]          the desk, spelled out
    owner/tui       [--offline] [--alpaca-book]        desk mode: which data, whose book
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
import shutil
import signal
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
from qlab.paths import state_root, workspace_root


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
    without naming it. Live/simulated needs no flag — it is the default — and
    ``--offline`` is the explicit word for the synthetic demo desk.
    """
    if getattr(args, "alpaca_book", False):
        return DeskMode("live", "alpaca")
    if getattr(args, "offline", False):
        return DeskMode("synthetic", "simulated")
    return None


def desk_mode_argv(mode: DeskMode | None) -> list[str]:
    """The flags that reproduce ``mode`` — the inverse of ``desk_mode_from_args``.

    The owner subprocess must be launched with the same words the TUI is about
    to display, or the two disagree about whose book is being traded. Live with
    the simulated book needs no flag: it is the default on both sides, and
    synthetic is the lane that must now be named.
    """
    if mode is None:
        return []
    if mode.data != "live":
        return ["--offline"]
    return ["--alpaca-book"] if mode.book == "alpaca" else []


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
    if persisted is not None:
        if persisted.offline or _alpaca_credentials_resolve():
            return persisted
        return None
    # Never chosen: the live desk, on the credential-free provider. Every
    # operation is available without a flag, and the desk's own surfaces say
    # honestly what the feed is doing — which is the fail-loud path, where a
    # synthetic default was the desk quietly showing invented prices.
    return DeskMode("live", "simulated")


def _cmd_owner(args) -> int:
    """Run the owner runtime headless: the one writer, no client attached.

    What `qlab ui --no-browser` used to be, without the web client that rode
    on it. The launcher spawns this for `qlab`; running it by hand is for a
    desk kept up as a service that workstations attach to and outlive.
    """
    from qlab.ui.server import serve

    _publish_owner_port(args.port)
    # No modal on this surface, so the resolution must end in a mode: flags,
    # then the persisted choice, then the live default startup_desk_mode
    # gives a never-chosen desk. Its one None — a persisted live mode whose
    # credential no longer resolves — is a question, and a headless process
    # cannot ask it.
    mode = startup_desk_mode(desk_mode_from_args(args))
    if mode is None:
        raise SystemExit(
            "the persisted desk mode is live but no Alpaca credential "
            "resolves; sign in (alpaca profile login) or choose a lane "
            "explicitly: qlab owner --offline")
    # `startup_desk_mode` already persisted a flagged mode — the operator
    # speaking now — and only that: persisting the resolved default too would
    # quietly convert "never chose" into "chose live" on disk.
    serve(port=args.port, offline=mode.offline, desk_mode=mode)
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


# What `--restart` may take from the base up, in the order of how much goes.
# `runtime` is the old --restart; `book` is the owner's own reset; `everything`
# is a desk that has never been opened.
RESTART_TIERS = ("runtime", "book", "everything")


def _restart_dialog(scope: str, yes: bool, port: int, root: Path) -> str:
    """Warn, choose, agree. Returns the tier to carry out.

    A restart that can take the book or the whole desk with it is not a flag
    an operator should be able to pass by habit, so the ritual is the one the
    desk uses for money: say what will happen, in the operator's own terms
    and with the paths and sizes, and take the tier's own word typed back as
    the agreement. `--restart=<tier> --yes` is the scripted spelling and is
    equally explicit; a non-interactive run with neither is refused rather
    than defaulted, because the safe default and the asked-for default
    disagree here.
    """
    registry = root / "registry.duckdb"
    size_mb = sum(
        f.stat().st_size for f in root.rglob("*") if f.is_file()
    ) / 1e6 if root.exists() else 0.0
    menu = (
        f"\n  --restart takes the desk down from the base up. On port {port}:\n\n"
        f"    runtime     stop the owner and start it fresh; keep the book,\n"
        f"                the history and every setting\n"
        f"    book        that, and reset the paper book to starting capital —\n"
        f"                positions, orders, marks, high-water mark, halt\n"
        f"    everything  that, and archive the whole desk state so it opens\n"
        f"                as new: {root} ({size_mb:.0f} MB incl. the registry\n"
        f"                {'present' if registry.exists() else 'absent'}, caches,\n"
        f"                posture, lane, model settings). Archived, not deleted —\n"
        f"                the record moves to {root.parent / '.lab-archive'}.\n"
    )
    if scope != "ask":
        tier = scope
        if not yes:
            if not sys.stdin.isatty():
                raise SystemExit(
                    f"--restart={tier} needs --yes when there is no terminal to "
                    "agree on; a destructive restart is never defaulted.")
            print(menu, flush=True)
            typed = input(f"  Type {tier} to agree, anything else to stop: ").strip()
            if typed != tier:
                raise SystemExit("restart declined; nothing was touched.")
        return tier
    if not sys.stdin.isatty():
        raise SystemExit(
            "--restart asks which tier to take and there is no terminal to "
            "ask on; spell it out: --restart=runtime|book|everything --yes")
    print(menu, flush=True)
    chosen = input("  Which tier? [runtime/book/everything]: ").strip().lower()
    if chosen not in RESTART_TIERS:
        raise SystemExit(f"{chosen or '(nothing)'} is not a tier; nothing was touched.")
    if chosen != "runtime":
        typed = input(f"  This cannot be undone from the desk. Type {chosen} to agree: ").strip()
        if typed != chosen:
            raise SystemExit("restart declined; nothing was touched.")
    return chosen


def _archive_state(root: Path) -> Path:
    """Move the whole desk state aside so the next owner opens a new desk.

    Moved, not removed: the registry is the audit record and the honest-results
    ledger, and a wipe that could not be reversed from disk would be the one
    destructive act on this desk with no way back. Refuses while anything is
    still holding the registry — one writer, always, including the mover.
    """
    if not root.exists():
        return root
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = root.parent / ".lab-archive" / stamp
    dest.parent.mkdir(parents=True, exist_ok=True)
    wal = root / "registry.duckdb.wal"
    if wal.exists() and wal.stat().st_size > 64 * 1024 * 1024:
        # A large WAL means the owner did not checkpoint on the way down. Still
        # archived whole — DuckDB replays it on the next open of the archive —
        # but said, because "moved" and "moved intact" are different claims.
        print(f"  note: {wal.name} is {wal.stat().st_size / 1e6:.0f} MB; the archive "
              "carries it and DuckDB will replay it on open.", flush=True)
    shutil.move(str(root), str(dest))
    print(f"  archived {root} -> {dest}", flush=True)
    return dest


def _stop_listener_on_port(port: int) -> None:
    """Stop whatever owns ``port``, for ``--restart``.

    The pid is read off the socket rather than off a pidfile: the process an
    operator wants gone is by definition the one actually holding the port,
    whatever started it. TERM first — the owner closes DuckDB cleanly on it —
    and KILL only for a process that ignored a generous grace.
    """
    def port_open() -> bool:
        with socket.socket() as sock:
            sock.settimeout(0.2)
            return sock.connect_ex(("127.0.0.1", port)) == 0

    if not port_open():
        return
    if os.name == "nt":
        out = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True,
        ).stdout
        pids = {
            line.split()[-1]
            for line in out.splitlines()
            if f":{port}" in line and "LISTENING" in line.upper()
        }
        if not pids:
            raise SystemExit(
                f"--restart: port {port} is open but netstat names no "
                "listener; stop the process by hand and run again")
        for pid in pids:
            subprocess.run(
                ["taskkill", "/PID", pid, "/T", "/F"],
                capture_output=True, text=True,
            )
    else:
        probe = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True,
        )
        pids = [int(line) for line in probe.stdout.split() if line.strip()]
        if not pids:
            raise SystemExit(
                f"--restart: port {port} is open but lsof names no listener; "
                "stop the process by hand and run again")
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and port_open():
            time.sleep(0.2)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and port_open():
        time.sleep(0.2)
    if port_open():
        raise SystemExit(
            f"--restart could not free port {port}; the listener survived "
            "TERM and KILL, which is not a process this launcher started")
    print(f"--restart: stopped the previous runtime on port {port}", flush=True)


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


def atlas_client_argv(binary: str, *, glass: bool, offline: bool) -> list[str]:
    """The workstation's argv: the launcher forwards a veto and the data lane.

    Passthroughs, not a second authority. Arming is no longer expressible here
    at all — it is the owner's persisted answer to the startup door, so no
    launcher flag can grant it. What survives is the direction that only ever
    takes authority away: ``--glass`` is this one window declining whatever the
    desk says. The lane only chooses which view of the owner the client polls;
    dropping it here meant a live owner drawn by a client that only ever asked
    for the offline view.
    """
    argv = [binary]
    if glass:
        argv.append("--glass")
    if not offline:
        argv.append("--live")
    return argv


def _cmd_tui(args) -> int:
    """Launch the terminal workstation and, when needed, its owner API process.

    One client over one owner: the Ratatui workstation in
    ``clients/atlas-tui``. The Textual client was the soak valve while the
    workstation was being lived with; the soak is over and the valve is
    retired, so which screen is drawn is no longer a question the launcher
    answers.
    """
    if getattr(args, "operator", False):
        # Retired, and refused rather than dropped. Arming is the owner's
        # persisted answer to the startup door, so no launcher flag can grant
        # it — and a flag that parses but grants nothing is the silent no-op
        # invariant 4 exists to forbid. The word is still *registered* only so
        # this message can be printed instead of argparse's "unrecognized
        # arguments", which names no remedy to the operator holding the old
        # command in their shell history or in a script.
        raise SystemExit(
            "--operator is retired. Arming is the desk's own answer, not a "
            "launcher flag.\n"
            "    qlab tui                the desk asks once at startup, and "
            "remembers the answer\n"
            "    qlab tui --glass        this one window stays read-only "
            "whatever the desk says"
        )
    if getattr(args, "classic", False):
        # Retired with the Textual client itself, and refused by name for the
        # same reason --operator is: a flag that parses and silently draws a
        # different screen than it used to is the worst kind of no-op.
        raise SystemExit(
            "--classic is retired: the Textual client is gone and the Atlas "
            "workstation is the desk's one terminal client. `qlab` opens it."
        )
    if getattr(args, "live", False) or getattr(args, "online", False):
        raise SystemExit(
            "--live/--online are retired: live data is the default now. "
            "`qlab --offline` selects the synthetic no-network demo desk."
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
    offline = True if mode is None else mode.offline

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

    tier = None
    restart = getattr(args, "restart", None)
    if restart:
        tier = _restart_dialog(
            restart, bool(getattr(args, "yes", False)), args.port, state_root())
        _stop_listener_on_port(args.port)
        if tier == "everything":
            _archive_state(state_root())
            # The mode the door persisted went with the archive; a fresh desk
            # is asked, and asked from the live default like a first open.
            flagged = desk_mode_from_args(args)
            mode = startup_desk_mode(flagged)
            offline = True if mode is None else mode.offline

    owner = None
    already_open = port_open()
    client = ApiClient(f"http://127.0.0.1:{args.port}")
    if not already_open:
        server_argv = [
            sys.executable, "-m", "qlab.autopilot.cli", "owner",
            "--port", str(args.port),
            # While the door is still asking, the owner runs the synthetic
            # lane — the only one that invents nothing an unanswered desk
            # could be misread as trading.
            *(desk_mode_argv(mode) if mode is not None else ["--offline"]),
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
                   "Startup may be unusually slow. Try `qlab owner` "
                   "in a separate terminal to see its output.")
            )

    try:
        client.get("/api/system", offline=int(offline))
    except Exception as exc:
        if owner is not None:
            owner.terminate()
        raise SystemExit(
            f"port {args.port} is open but is not a compatible qlab runtime: {exc}") from exc

    if tier == "book":
        # Through the fresh owner, never around it: the registry has exactly
        # one writer, and the launcher is not it.
        try:
            said = client.post("/api/reset", {})
        except Exception as exc:
            raise SystemExit(
                f"the runtime came up but would not reset the book: {exc}") from exc
        print(f"  paper book reset: {said}", flush=True)

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

    binary = _atlas_binary()
    # `isfile` as well as `access`: X_OK is true of every directory, so a path
    # that resolved to `clients/atlas-tui/target/release/` — an interrupted
    # build leaves exactly that — would pass the check and fail at exec, after
    # the owner had already been started.
    if not (os.path.isfile(binary) and os.access(binary, os.X_OK)):
        # Fail loud. There is no other client to fall back to, and a launcher
        # that silently did anything but open the workstation would be the
        # no-op invariant 4 forbids.
        raise SystemExit(
            f"the Atlas workstation is not built at {binary}\n"
            "    cd clients/atlas-tui && cargo build --release\n"
            "($QLAB_ATLAS_BIN overrides where this looks.)"
        )
    argv = atlas_client_argv(
        binary, glass=getattr(args, "glass", False), offline=offline)
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
        # Two words, and the safe defaults need neither: live data with the
        # simulated book is what a bare launch runs, so reaching the real
        # paper account is never a side effect of just opening the desk.
        # Which provider serves the live lane is QLAB_DATA_PROVIDER's job.
        sp.add_argument("--offline", action="store_true",
                        help="synthetic data and the simulated book — the "
                             "no-network demo desk")
        sp.add_argument("--alpaca-book", action="store_true",
                        help="trade your Alpaca paper book (live data)")

    owner = sub.add_parser(
        "owner", help="run the owner runtime headless (workstations attach to it)")
    owner.add_argument("--port", type=int, default=8765)
    add_desk_mode(owner)
    # Retired words, registered without help purely so the refusal below can
    # name the remedy instead of argparse's "unrecognized arguments".
    owner.add_argument("--live", action="store_true", help=argparse.SUPPRESS)
    owner.add_argument("--online", action="store_true", help=argparse.SUPPRESS)
    owner.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    owner.set_defaults(func=_cmd_owner)

    tui = sub.add_parser(
        "tui",
        help="the desk: owner runtime + Atlas workstation (what bare `qlab` runs)")
    tui.add_argument("--port", type=int, default=8765)
    add_desk_mode(tui)
    tui.add_argument(
        "--restart", nargs="?", const="ask", default=None,
        choices=["ask", *RESTART_TIERS], metavar="TIER",
        help="restart from the base up: warn, choose runtime|book|everything, "
             "agree by typing the tier; --restart=TIER --yes is the scripted spelling")
    tui.add_argument(
        "--yes", action="store_true",
        help="agree to --restart=TIER without the dialog (scripts only)")
    tui.add_argument("--refresh", type=float, default=2.0,
                     help="snapshot refresh interval in seconds")
    tui.add_argument(
        "--claude", choices=["offer", "auto", "off"], default="offer",
        help=("Claude workforce startup: show readiness without prompting (offer), "
              "launch automatically (auto), or keep it off until requested (off)"),
    )
    # Retired words, kept registered so the refusals in `_cmd_tui` can name
    # remedies instead of argparse's "unrecognized arguments".
    tui.add_argument("--classic", action="store_true", help=argparse.SUPPRESS)
    tui.add_argument("--live", action="store_true", help=argparse.SUPPRESS)
    tui.add_argument("--online", action="store_true", help=argparse.SUPPRESS)
    # Passthrough, and the only posture word a launcher may still say. It can
    # only take authority away: the window stays read-only whatever the desk
    # answered. A binary built without the operator feature is glass already,
    # so the flag is a no-op there in the one direction where a no-op is safe.
    tui.add_argument(
        "--glass", action="store_true",
        help="keep this window read-only for one session, whatever the desk says")
    # Retired. Registered without help text purely so `_cmd_tui` can refuse it
    # by name; argparse's "unrecognized arguments" names no remedy, and an
    # operator with the old command in a script deserves the sentence that
    # tells them where arming moved to.
    tui.add_argument("--operator", action="store_true", help=argparse.SUPPRESS)
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
    argv = list(sys.argv[1:] if argv is None else argv)
    # `qlab` is the desk. A bare invocation — or one that leads with a flag,
    # like `qlab --restart` — routes to `tui`; a leading `-h` stays top-level
    # so the full command list remains one keystroke away.
    if not argv:
        argv = ["tui"]
    elif argv[0].startswith("-") and argv[0] not in ("-h", "--help"):
        argv = ["tui", *argv]
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
