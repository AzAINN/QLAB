"""The referee gate, as code. Nothing trades without a PASS (research-plan §3).

The deterministic referee re-verifies the mandate-critical facts *independently*
of build_plan (defense in depth) and is the autopilot's gatekeeper. The LLM
referee agent adds qualitative review in interactive sessions and submits its
verdict through the same registry.log_verdict tool - one gate, two reviewers.
"""
from __future__ import annotations

from datetime import date

import numpy as np

from qlab.trader.mandate import Mandate


def deterministic_referee(targets: dict[str, float], mandate: Mandate,
                          as_of: date, moments_summary: dict | None = None,
                          ) -> tuple[str, list[str]]:
    reasons: list[str] = []
    vals = np.array(list(targets.values()), dtype=float)
    if not np.all(np.isfinite(vals)):
        reasons.append("non-finite weight")
    for t in targets:
        if t not in mandate.universe_whitelist:
            reasons.append(f"{t} outside universe whitelist")
    if mandate.long_only and np.any(vals < -1e-4):
        reasons.append("long-only violated")
    if mandate.fully_invested and abs(vals.sum() - 1.0) > 1e-2:
        reasons.append(f"budget violated: sum={vals.sum():.4f}")
    over = {t: v for t, v in targets.items() if v > mandate.max_weight_per_asset + 1e-4}
    if over:
        reasons.append(f"per-asset cap breach: {over}")
    if isinstance(as_of, date) and as_of > date.today():
        reasons.append("look-ahead as_of")
    if moments_summary and moments_summary.get("condition_number", 0) > 1e8:
        reasons.append("ill-conditioned covariance")
    return ("PASS" if not reasons else "FAIL"), reasons
