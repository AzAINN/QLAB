"""Pre-fill the parquet/pickle price cache so a live demo is network-independent.

Run this immediately before any demo/judging session (spec "demo resilience"):

    python scripts/prewarm_cache.py            # live data if available
    python scripts/prewarm_cache.py --offline  # synthetic (fully deterministic)
"""

from __future__ import annotations

import argparse

from qlab.core import data as market
from qlab.core.universe import load_universe


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--start", default="2008-01-01")
    args = ap.parse_args()

    uni = load_universe()
    for which in ("core", "candidates"):
        tickers = uni.tickers(which)
        df = market.get_prices(tickers, args.start, offline=args.offline)
        print(f"[prewarm] {which:10} {df.shape[0]:5} rows x {df.shape[1]:2} tickers "
              f"({df.index.min().date()} .. {df.index.max().date()})")
    print("[prewarm] cache ready; the demo no longer depends on the network.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
