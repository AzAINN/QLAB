"""Qualitative signals: deterministic properties of the news record.

These describe *the record*, never the market. Six numbers computed from an
already-grounded window, following the same discipline as the price-only regime
indicators in :mod:`qlab.signals`: each carries its own state and a one-line
reason, and none has a sign. That last point is the governance boundary and it
is deliberate — a signal that said "coverage is heavy and negative" and fed an
allocation would be a return forecast wearing a qualitative label, however it
were named. Nothing here answers "what will prices do"; they answer "what does
the record actually support, and where is it silent".

Absence is never a value. Every signal returns ``None`` with a state of
``no_window`` or ``insufficient`` rather than ``0.0``, because "the feed
returned nothing" and "the record says nothing about the book" and "the record
is about one name only" are three different claims and a zero conflates them —
the same conflation the desk refuses everywhere else.

Built entirely on what :mod:`qlab.news.grounding` already computes. ``by_ticker``
and ``source_tier`` existed there, tested, with no production caller at all;
this is what they were for.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Sequence

from qlab.news.grounding import GroundedNews, by_ticker, source_tier

# Below this many grounded records, a ratio or a concentration index is not a
# measurement — it is one story rounded to two decimal places. Signals that are
# statistics over the window are gated; signals that merely describe what is
# present are not.
MIN_SIGNAL_ITEMS = 5

NO_WINDOW = "no_window"
INSUFFICIENT = "insufficient"
OK = "ok"


@dataclass(frozen=True)
class Signal:
    """One qualitative reading, with its state and its reason.

    Mirrors the regime-indicator contract: a caller can render any signal
    without knowing which one it is, and a ``None`` value always carries a
    state explaining itself.
    """

    name: str
    value: float | None
    state: str
    reason: str
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class QualitativeSignals:
    """The six signals plus the sufficiency verdict callers gate on."""

    signals: tuple[Signal, ...]
    item_count: int
    claim_count: int
    sufficient: bool
    min_items: int

    def to_dict(self) -> dict:
        return {
            "signals": [s.to_dict() for s in self.signals],
            "by_name": {s.name: s.to_dict() for s in self.signals},
            "item_count": self.item_count,
            "claim_count": self.claim_count,
            "sufficient": self.sufficient,
            "min_items": self.min_items,
        }


def _as_dt(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def qualitative_signals(
    grounded: GroundedNews,
    *,
    universe: Sequence[str],
    asset_classes: Mapping[str, str] | None = None,
    as_of: datetime | None = None,
    lookback_hours: int = 48,
    min_items: int = MIN_SIGNAL_ITEMS,
) -> QualitativeSignals:
    """Compute the six qualitative signals over one grounded window.

    ``asset_classes`` is passed in as a plain mapping rather than resolved here
    so this module never depends on the universe config — it stays a pure
    function of the window it is handed, which is what makes it testable
    without a desk.
    """
    items = list(getattr(grounded, "items", ()) or ())
    claims = list(getattr(grounded, "claims", ()) or ())
    tickers = [str(t) for t in universe]
    classes = dict(asset_classes or {})
    n_items, n_claims = len(items), len(claims)
    enough = n_items >= int(min_items)

    def blank(name: str, reason: str, detail: dict | None = None) -> Signal:
        return Signal(name, None, NO_WINDOW, reason, detail or {})

    def thin(name: str, detail: dict | None = None) -> Signal:
        return Signal(
            name, None, INSUFFICIENT,
            f"only {n_items} record(s) in the window; below the {min_items} "
            f"needed for this to be a measurement rather than one story",
            detail or {})

    per_ticker = {t: c for t, c in by_ticker(grounded).items() if t in set(tickers)}
    covered = sorted(t for t in tickers if per_ticker.get(t))
    silent = [t for t in tickers if t not in set(covered)]

    out: list[Signal] = []

    # -- 1. coverage breadth ------------------------------------------------
    if not items:
        out.append(blank("coverage_breadth",
                         "no window: the feed returned nothing, which is not "
                         "the same as the record being silent about the book",
                         {"covered": [], "silent": tickers}))
    else:
        value = len(covered) / len(tickers) if tickers else None
        out.append(Signal(
            "coverage_breadth", value, OK,
            f"{len(covered)} of {len(tickers)} holdings are named by the record"
            + (f"; silent: {', '.join(silent)}" if silent else ""),
            {"covered": covered, "silent": silent}))

    # -- 2. asset-class reach ----------------------------------------------
    held_classes = {classes.get(t, "unclassified") for t in tickers}
    if not items:
        out.append(blank("asset_class_reach",
                         "no window; every class held is unspoken for",
                         {"silent_classes": sorted(held_classes)}))
    elif not tickers:
        out.append(blank("asset_class_reach", "no universe to classify"))
    else:
        covered_classes = {classes.get(t, "unclassified") for t in covered}
        silent_classes = sorted(held_classes - covered_classes)
        out.append(Signal(
            "asset_class_reach",
            len(covered_classes) / len(held_classes) if held_classes else None,
            OK,
            f"{len(covered_classes)} of {len(held_classes)} asset classes are "
            f"represented in the record"
            + (f"; unspoken for: {', '.join(silent_classes)}"
               if silent_classes else ""),
            {"silent_classes": silent_classes,
             "covered_classes": sorted(covered_classes)}))

    # -- 3. attention concentration ----------------------------------------
    counts = {t: len(c) for t, c in per_ticker.items() if c}
    if not items:
        out.append(blank("attention_concentration", "no window"))
    elif not enough or not counts:
        out.append(thin("attention_concentration", {"top": []}))
    else:
        total = sum(counts.values())
        hhi = sum((c / total) ** 2 for c in counts.values())
        effective = 1.0 / hhi if hhi > 0 else None
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        out.append(Signal(
            "attention_concentration", effective, OK,
            f"the record is effectively about {effective:.1f} of "
            f"{len(counts)} named holdings"
            + (f"; heaviest {top[0][0]} with {top[0][1]}" if top else ""),
            {"top": [{"ticker": t, "claims": c} for t, c in top]}))

    # -- 4. corroboration ---------------------------------------------------
    if not items:
        out.append(blank("corroboration_ratio", "no window"))
    elif not enough or not claims:
        out.append(thin("corroboration_ratio",
                        {"corroborated": 0, "total": n_claims}))
    else:
        corroborated = list(grounded.corroborated_claims)
        primary = sum(1 for c in claims if getattr(c, "tier", "") == "primary")
        out.append(Signal(
            "corroboration_ratio", len(corroborated) / len(claims), OK,
            f"{len(corroborated)} of {len(claims)} claims are corroborated "
            f"({primary} primary-source); the rest are one outlet's take",
            {"corroborated": len(corroborated), "total": len(claims),
             "primary": primary}))

    # -- 5. publisher concentration ----------------------------------------
    if not items:
        out.append(blank("publisher_concentration", "no window"))
    elif not enough:
        out.append(thin("publisher_concentration"))
    else:
        sources = Counter(str(getattr(i, "source", "") or "unknown")
                          for i in items)
        top_share = max(sources.values()) / len(items)
        primary_pubs = sum(1 for s in sources if source_tier(s) == "primary")
        heaviest = max(sources.items(), key=lambda kv: (kv[1], kv[0]))[0]
        out.append(Signal(
            "publisher_concentration", top_share, OK,
            f"{len(sources)} distinct publisher(s); the heaviest ({heaviest}) "
            f"is {top_share:.0%} of the window, {primary_pubs} primary",
            {"distinct_publishers": len(sources),
             "primary_publishers": primary_pubs}))

    # -- 6. window age ------------------------------------------------------
    # Not gated by the sample floor: this describes what is present rather than
    # being a statistic over a sample, so it is meaningful at a single item.
    if not items:
        out.append(blank("window_age_hours", "no window"))
    else:
        now = as_of or datetime.now(timezone.utc)
        ages = [
            (now - dt).total_seconds() / 3600.0
            for dt in (_as_dt(getattr(i, "published", "")) for i in items)
            if dt is not None
        ]
        if not ages:
            out.append(blank("window_age_hours",
                             "no record carried a readable timestamp"))
        else:
            median_age = statistics.median(ages)
            out.append(Signal(
                "window_age_hours", median_age, OK,
                f"median record is {median_age:.1f}h old "
                f"(newest {min(ages):.1f}h, oldest {max(ages):.1f}h) "
                f"in a {lookback_hours}h window",
                {"newest_hours": min(ages), "oldest_hours": max(ages),
                 "lookback_hours": lookback_hours}))

    return QualitativeSignals(
        signals=tuple(out),
        item_count=n_items,
        claim_count=n_claims,
        sufficient=bool(enough),
        min_items=int(min_items),
    )
