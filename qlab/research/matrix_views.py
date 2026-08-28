"""Matrix cells to risk views, by explicit bounded rule.

Two rules, both about *risk shape*, neither about direction, because the
view types cannot carry direction and the matrix has none to give:

* **Attention with documents behind it fattens the tail.** A name whose
  corroborated primary-document count exceeds its trailing baseline gets a
  ``fatter`` tail view, confidence scaled to the excess and capped at 0.5 —
  half of what the extractor role may claim, because a rule is not a
  reading.
* **Concentrated coverage couples a name to its sleeve.** When one name
  carries most of a sleeve's coverage, a correlation view raises its
  correlation to the sleeve's other names toward 0.6, capped at 0.4
  confidence — coverage concentration is what the absorption ratio sees from
  the price side, stated from the record side.

At most three views, the largest excess first: the KL budget downstream is
finite and a rule that emitted ten would spend it on noise.
"""

from __future__ import annotations

from qlab.news.matrix import MatrixRow, QualitativeMatrix

MAX_VIEWS = 3
TAIL_CAP = 0.5
CORR_CAP = 0.4
_CORR_TARGET = 0.6


def views_from_matrix(
    matrix: QualitativeMatrix,
    baseline: dict[str, MatrixRow] | None,
    sleeves: dict[str, list[str]],
) -> list[dict]:
    """Bounded, unsigned views the entropy-pooling tool accepts, by rule."""
    if not matrix.rows:
        raise ValueError(
            "a qualitative matrix with no rows cannot be read for views"
        )
    candidates: list[tuple[float, dict]] = []
    base = baseline or {}
    for ticker, r in matrix.rows.items():
        b = base.get(ticker)
        prior = b.primary_docs if b else 0
        excess = r.primary_docs - prior
        if excess >= 2 and r.corroborated >= excess:
            confidence = min(TAIL_CAP, 0.15 * excess)
            candidates.append((excess, {
                "type": "tail", "ticker": ticker, "direction": "fatter",
                "confidence": round(confidence, 3),
                "source_claims": list(r.claim_keys)}))
    for members in sleeves.values():
        # A sleeve member the window never mentioned is a name with no record,
        # not an error: the matrix is the universe the window covered.
        rows = [matrix.rows[m] for m in members if m in matrix.rows]
        total = sum(x.coverage for x in rows)
        if total < 4:
            continue
        top = max(rows, key=lambda x: x.coverage)
        if top.coverage / total >= 0.75:
            for other in rows:
                if other.ticker == top.ticker:
                    continue
                candidates.append((top.coverage / total, {
                    "type": "corr", "ticker_a": top.ticker,
                    "ticker_b": other.ticker,
                    "target_corr": _CORR_TARGET,
                    "confidence": round(
                        min(CORR_CAP, top.coverage / total - 0.5), 3),
                    "source_claims": list(top.claim_keys)}))
    candidates.sort(key=lambda c: c[0], reverse=True)
    return [v for _, v in candidates[:MAX_VIEWS]]
