"""Content-addressed data permits.

A permit replaces "trust ``snapshot.source``" with a structured, reproducible
record binding a specific snapshot to what it may be used for (research, paper
proposal, execution). Plans store the permit id; execution rechecks it. Quote
fields (``quote_as_of``/``quote_age_seconds``) are added when the market stream
lands — a permit built here is never on its own execution-eligible for that
reason.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from qlab.core.data import DataPolicy
from qlab.data.health import DataHealth


@dataclass(frozen=True)
class DataPermit:
    permit_id: str
    snapshot_id: str
    purpose: str
    provider: str
    feed: str | None
    as_of: str
    retrieved_at: str
    last_completed_bar: str | None
    bar_age_sessions: int | None
    universe: list[str]
    missing_tickers: list[str]
    integrity_verdict: str
    eligible_for_research: bool
    eligible_for_paper_proposal: bool
    eligible_for_execution: bool
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "permit_id": self.permit_id,
            "snapshot_id": self.snapshot_id,
            "purpose": self.purpose,
            "provider": self.provider,
            "feed": self.feed,
            "as_of": self.as_of,
            "retrieved_at": self.retrieved_at,
            "last_completed_bar": self.last_completed_bar,
            "bar_age_sessions": self.bar_age_sessions,
            "universe": list(self.universe),
            "missing_tickers": list(self.missing_tickers),
            "integrity_verdict": self.integrity_verdict,
            "eligible_for_research": self.eligible_for_research,
            "eligible_for_paper_proposal": self.eligible_for_paper_proposal,
            "eligible_for_execution": self.eligible_for_execution,
            "reasons": list(self.reasons),
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_permit(
    *,
    snapshot_id: str,
    purpose: str,
    policy: DataPolicy,
    health: DataHealth,
    universe: list[str],
    as_of: str,
    retrieved_at: str | None = None,
    feed: str | None = None,
) -> DataPermit:
    """Build a content-addressed permit from a snapshot's health verdict.

    ``permit_id`` is a sha256 prefix over the *deterministic* fields (not the
    wall-clock ``retrieved_at``), so the same snapshot+purpose+verdict always
    yields the same permit id — a stable audit key.
    """
    universe = list(universe)
    missing = list(health.missing_tickers)
    last_bar = health.last_bar.isoformat() if health.last_bar else None
    identity = {
        "snapshot_id": snapshot_id,
        "purpose": purpose,
        "provider": policy.provider,
        "feed": feed if feed is not None else policy.feed,
        "as_of": as_of,
        "last_completed_bar": last_bar,
        "bar_age_sessions": health.bar_age_sessions,
        "universe": sorted(universe),
        "missing_tickers": sorted(missing),
        "integrity_verdict": health.integrity_verdict,
        "eligible_for_research": health.eligible_for_research,
        "eligible_for_paper_proposal": health.eligible_for_paper_proposal,
        "eligible_for_execution": health.eligible_for_execution,
    }
    permit_id = "sha256:" + hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]

    return DataPermit(
        permit_id=permit_id,
        snapshot_id=snapshot_id,
        purpose=purpose,
        provider=policy.provider,
        feed=feed if feed is not None else policy.feed,
        as_of=as_of,
        retrieved_at=retrieved_at or _now_iso(),
        last_completed_bar=last_bar,
        bar_age_sessions=health.bar_age_sessions,
        universe=universe,
        missing_tickers=missing,
        integrity_verdict=health.integrity_verdict,
        eligible_for_research=health.eligible_for_research,
        eligible_for_paper_proposal=health.eligible_for_paper_proposal,
        eligible_for_execution=health.eligible_for_execution,
        reasons=list(health.reasons),
    )
