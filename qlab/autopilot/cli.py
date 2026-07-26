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
import socket
import subprocess
import sys
import time
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
    summary = run_once(offline=args.offline, execute=not args.dry_run)
    print(render_summary(summary))
    return 0


def _cmd_watch(args) -> int:
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


def _cmd_tui(args) -> int:
    """Launch the Textual client and, when needed, its owner API process."""
    try:
        from qlab.tui.app import QlabTui
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
    if not port_open():
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
        )
        print(
            f"Starting the qlab runtime on port {args.port} "
            "(first launch compiles imports; this can take up to a minute)…",
            flush=True,
        )

        def _owner_stderr(process: subprocess.Popen) -> str:
            # Best-effort drain so a real failure is diagnosable instead of
            # hidden behind a generic timeout. terminate() first so the read
            # cannot block on a still-running child holding the pipe open.
            if process.poll() is None:
                process.terminate()
            try:
                _out, err = process.communicate(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                _out, err = process.communicate()
            return (err or "").strip()

        deadline = time.monotonic() + startup_budget_s
        while time.monotonic() < deadline:
            if port_open():
                break
            if owner.poll() is not None:
                detail = (owner.stderr.read().strip()
                          if owner.stderr else "")
                raise SystemExit(
                    "qlab UI runtime exited before the TUI connected"
                    + (f":\n{detail}" if detail else "")
                )
            time.sleep(0.2)
        else:
            detail = _owner_stderr(owner)
            raise SystemExit(
                f"qlab UI runtime did not open port {args.port} within "
                f"{int(startup_budget_s)}s"
                + (f":\n{detail}" if detail else
                   " and reported no error — the port may be held by another "
                   "process, or startup is unusually slow. Try `qlab ui "
                   "--no-browser` in a separate terminal to see its output.")
            )

    client = ApiClient(f"http://127.0.0.1:{args.port}")
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

    QlabTui(
        client,
        offline=offline,
        refresh_interval=args.refresh,
        owned_server=owner,
        claude_start=args.claude,
        desk_mode=mode,
    ).run()
    return 0


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

    def add_desk_mode(sp):
        # Two words, because the safe choice is the default: bare --live keeps
        # the simulated book, so reaching the real paper account is never a
        # side effect of asking for real prices.
        sp.add_argument("--live", action="store_true",
                        help="use live Alpaca market data (simulated book)")
        sp.add_argument("--alpaca-book", action="store_true",
                        help="trade your Alpaca paper book (implies --live)")

    ui = sub.add_parser("ui", help="launch the single-page web UI (no CLI needed after)")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--online", action="store_true",
                    help="use live yfinance data (default: offline synthetic)")
    add_desk_mode(ui)
    ui.add_argument("--no-browser", action="store_true", help="don't auto-open the browser")
    ui.set_defaults(func=_cmd_ui)

    tui = sub.add_parser("tui", help="launch the terminal operator console")
    tui.add_argument("--port", type=int, default=8765)
    tui.add_argument("--online", action="store_true",
                     help="use live/cached yfinance daily bars (default: offline synthetic)")
    add_desk_mode(tui)
    tui.add_argument("--refresh", type=float, default=2.0,
                     help="snapshot refresh interval in seconds")
    tui.add_argument(
        "--claude", choices=["offer", "auto", "off"], default="offer",
        help=("Claude workforce startup: show readiness without prompting (offer), "
              "launch automatically (auto), or keep it off until requested (off)"),
    )
    tui.set_defaults(func=_cmd_tui)

    from qlab.desk_cli import register_subcommands

    register_subcommands(sub, add_common)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
