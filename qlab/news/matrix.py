"""The qualitative matrix: what the record says about each name, as counts.

Rows are the universe; columns are properties of the *record* — how much
coverage, from how many publishers, how much of it corroborated, how many
primary documents, how far to the next scheduled release. Nothing here has a
sign. ``qualitative.py`` states the reason and it holds twice as hard for a
table that reaches the optimizer: a signed column is a return forecast with a
qualitative name. Every row carries the claim keys it was counted from, so a
cell is traceable to archive rows.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass

# The stamp on a matrix the OWNER logged from the desk's own live window. The
# ablation arm writes its research windows to the same registry under its own
# stamp (``views_arm.ARM_MATRIX_SOURCE``), so a reader with no stamp to filter
# on will serve an arm's window — a different universe, a different day, built
# by rule rather than read from the press — as the desk's record.
DESK_MATRIX_SOURCE = "desk"


@dataclass(frozen=True)
class MatrixRow:
    ticker: str
    coverage: int
    publishers: int
    corroborated: int
    primary_docs: int
    days_to_next_release: int | None
    claim_keys: tuple[str, ...]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class QualitativeMatrix:
    as_of: str
    window_hash: str
    rows: dict[str, MatrixRow]

    def to_dict(self) -> dict:
        return {"as_of": self.as_of, "window_hash": self.window_hash,
                "rows": {k: r.to_dict() for k, r in self.rows.items()}}


def build_matrix(claims, universe, as_of: str, upcoming) -> QualitativeMatrix:
    rows: dict[str, MatrixRow] = {}
    ahead: dict[str, int] = {}
    for event in upcoming:
        for t in event.get("tickers", []):
            ahead[t] = min(ahead.get(t, 10**6), int(event["days_ahead"]))
    for ticker in universe:
        mine = [c for c in claims if ticker in c.tickers]
        publishers = {s for c in mine for s in c.sources}
        rows[ticker] = MatrixRow(
            ticker=ticker,
            coverage=len(mine),
            publishers=len(publishers),
            corroborated=sum(1 for c in mine if c.corroborated),
            primary_docs=sum(1 for c in mine if c.tier == "primary"),
            days_to_next_release=ahead.get(ticker),
            claim_keys=tuple(sorted(c.key for c in mine)),
        )
    # The hash identifies the WINDOW, not the reading of it: the same claims
    # read on a later day are the same window, so a matrix is logged once per
    # window rather than once per day. `as_of` is deliberately not material.
    material = "|".join(sorted(c.key for c in claims))
    return QualitativeMatrix(
        as_of=as_of,
        window_hash=hashlib.sha256(material.encode()).hexdigest()[:16],
        rows=rows,
    )
