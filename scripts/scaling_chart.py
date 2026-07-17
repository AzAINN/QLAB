"""Measure the discretized-MV QAOA arm (Q-B) vs universe size.

Turns "it works" into "we measured it" (spec "Revisions"): for n = 4..8 assets it
runs the QAOA discretized min-variance on the Aer simulator, records the QAOA-vs-
exact optimality gap and wall-clock, and the discretization cost (best-discrete
vs the continuous optimum). Writes a table to ``.lab/reports/scaling.csv`` and a
PNG if matplotlib is installed.

    python scripts/scaling_chart.py
    python scripts/scaling_chart.py --min 4 --max 8 --reps 1
"""

from __future__ import annotations

import argparse
import time
from datetime import date
from pathlib import Path

import numpy as np

from qlab.core.objective import build_objective
from qlab.core.types import MomentSet
from qlab.solvers.base import Constraints, get_solver

_OUT = Path(__file__).resolve().parents[1] / ".lab" / "reports"


def _synthetic_cov(n: int, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    A = rng.standard_normal((n, n)) * 0.01
    return A @ A.T + np.eye(n) * 1e-3


def run(nmin: int, nmax: int, reps: int, resolution_bits: int) -> list[dict]:
    rows = []
    for n in range(nmin, nmax + 1):
        cov = _synthetic_cov(n)
        ms = MomentSet(tickers=[f"T{i}" for i in range(n)], as_of=date.today(), cov=cov)

        # continuous optimum (reference)
        cont = get_solver("classical").solve(build_objective("min_variance", ms),
                                             Constraints())
        cont_var = cont.objective_value

        # discretized MV via QAOA
        obj = build_objective("discretized_mv", ms, extra={"resolution_bits": resolution_bits})
        t0 = time.perf_counter()
        res = get_solver("qaoa", reps=reps).solve(obj, Constraints())
        wall = time.perf_counter() - t0
        d = res.diagnostics
        row = {
            "n": n, "n_qubits": d.get("n_qubits"),
            "qaoa_wall_s": round(wall, 3),
            "optimality_gap": round(d.get("optimality_gap", float("nan")), 5)
            if "optimality_gap" in d else None,
            "best_discrete_var": round(res.objective_value, 8),
            "continuous_var": round(cont_var, 8),
            "discretization_cost": round(res.objective_value - cont_var, 8),
        }
        rows.append(row)
        print(row)
    return rows


def _write_csv(rows: list[dict]) -> Path:
    _OUT.mkdir(parents=True, exist_ok=True)
    path = _OUT / "scaling.csv"
    cols = list(rows[0].keys())
    lines = [",".join(cols)]
    lines += [",".join(str(r[c]) for c in cols) for r in rows]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_png(rows: list[dict]) -> Path | None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    ns = [r["n"] for r in rows]
    fig, ax1 = plt.subplots(figsize=(7, 4))
    gaps = [r["optimality_gap"] or 0.0 for r in rows]
    ax1.plot(ns, gaps, "o-", color="tab:blue", label="QAOA optimality gap")
    ax1.set_xlabel("universe size n")
    ax1.set_ylabel("optimality gap", color="tab:blue")
    ax2 = ax1.twinx()
    ax2.plot(ns, [r["qaoa_wall_s"] for r in rows], "s--", color="tab:red",
             label="wall-clock (s)")
    ax2.set_ylabel("QAOA wall-clock (s)", color="tab:red")
    fig.suptitle("Discretized-MV QAOA (Aer): approximation quality & runtime vs n")
    fig.tight_layout()
    path = _OUT / "scaling.png"
    fig.savefig(path, dpi=120)
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min", type=int, default=4, dest="nmin")
    ap.add_argument("--max", type=int, default=8, dest="nmax")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--bits", type=int, default=2)
    args = ap.parse_args()

    rows = run(args.nmin, args.nmax, args.reps, args.bits)
    csv = _write_csv(rows)
    print(f"[scaling] wrote {csv}")
    png = _write_png(rows)
    if png:
        print(f"[scaling] wrote {png}")
    else:
        print("[scaling] matplotlib not installed; skipped PNG (pip install qlab[viz])")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
