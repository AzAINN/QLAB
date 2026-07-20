"""``qlab`` command-line entrypoint — the standalone autopilot.

    qlab run-once   [--offline] [--dry-run]            one full pipeline iteration
    qlab watch      --interval 15m [--offline]         run run-once on a loop
    qlab daily-ops  [--offline]                        heartbeat (never trades)
    qlab batch      <spec.yaml> [--offline]            the reproducible ablation
    qlab recommend  [--as-of DATE] [--offline]         print an allocation, no trade
    qlab prewarm    [--universe core|candidates]       pre-fill the data cache
    qlab ui         [--no-browser] [--port N]          owner runtime + web client
    qlab tui        [--claude offer|auto|off]          terminal operator console
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

from qlab.autopilot.loop import daily_ops, render_summary, run_once
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


def _cmd_ui(args) -> int:
    from qlab.ui.server import serve

    serve(port=args.port, offline=not args.online, open_browser=not args.no_browser)
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

    def port_open() -> bool:
        with socket.socket() as sock:
            sock.settimeout(0.2)
            return sock.connect_ex(("127.0.0.1", args.port)) == 0

    owner = None
    if not port_open():
        server_argv = [
            sys.executable, "-m", "qlab.autopilot.cli", "ui",
            "--port", str(args.port), "--no-browser",
        ]
        if args.online:
            server_argv.append("--online")
        owner = subprocess.Popen(
            server_argv,
            cwd=str(workspace_root()),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(60):
            if port_open():
                break
            if owner.poll() is not None:
                detail = owner.stderr.read().strip() if owner.stderr else ""
                raise SystemExit(
                    "qlab UI runtime exited before the TUI connected"
                    + (f":\n{detail}" if detail else "")
                )
            time.sleep(0.1)
        else:
            owner.terminate()
            raise SystemExit(f"qlab UI runtime did not open port {args.port}")

    client = ApiClient(f"http://127.0.0.1:{args.port}")
    try:
        client.get("/api/system", offline=int(not args.online))
    except Exception as exc:
        if owner is not None:
            owner.terminate()
        raise SystemExit(
            f"port {args.port} is open but is not a compatible qlab runtime: {exc}") from exc

    QlabTui(
        client,
        offline=not args.online,
        refresh_interval=args.refresh,
        owned_server=owner,
        claude_start=args.claude,
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

    ui = sub.add_parser("ui", help="launch the single-page web UI (no CLI needed after)")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--online", action="store_true",
                    help="use live yfinance data (default: offline synthetic)")
    ui.add_argument("--no-browser", action="store_true", help="don't auto-open the browser")
    ui.set_defaults(func=_cmd_ui)

    tui = sub.add_parser("tui", help="launch the terminal operator console")
    tui.add_argument("--port", type=int, default=8765)
    tui.add_argument("--online", action="store_true",
                     help="use live/cached yfinance daily bars (default: offline synthetic)")
    tui.add_argument("--refresh", type=float, default=2.0,
                     help="snapshot refresh interval in seconds")
    tui.add_argument(
        "--claude", choices=["offer", "auto", "off"], default="offer",
        help=("Claude workforce startup: prompt once (offer), launch automatically "
              "(auto), or keep it off until requested (off)"),
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
