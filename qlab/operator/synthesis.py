"""Bob's desk read: one qualitative view across three kinds of evidence.

The desk already has three separate truths and no one holding them together:

* **quantitative** — the regime panel, drawdown tier, drift, exposure;
* **research** — what the workforce concluded, what the referee passed or
  failed, what past reflections scored;
* **qualitative** — what the news is saying about the names we hold.

A number tells you *what*; the interesting question is what it means when the
three disagree. This module composes them into a structured read: where they
align, where they contradict, how much conviction that supports, and what would
change the answer. It is deliberately deterministic — the *structure* of the
judgment is code, so it is auditable and reproducible, and an LLM narrates over
it rather than inventing it.

It is not a forecast and not a signal. Conviction describes how much the
evidence agrees with itself, never how likely a price move is.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field

# Words that move a headline's tone. Deliberately small and explicit: a
# transparent lexicon a human can audit beats an opaque model for something
# this consequential, and it never pretends to be sentiment analysis.
_RISK_OFF = (
    "crash", "plunge", "slump", "selloff", "sell-off", "tumble", "rout",
    "recession", "default", "downgrade", "crisis", "shock", "war", "sanction",
    "inflation", "hike", "tightening", "layoff", "bankruptcy", "contagion",
    "volatility", "fear", "slowdown", "deficit", "downturn",
)
_RISK_ON = (
    "rally", "surge", "rebound", "recovery", "upgrade", "beat", "record high",
    "easing", "cut", "stimulus", "growth", "expansion", "optimism", "inflow",
    "breakthrough", "resilient", "soft landing",
)


ALIGNED = "aligned"
DIVERGENT = "divergent"
QUIET = "quiet"


def _lexicon(words: tuple[str, ...]) -> re.Pattern:
    """Compile a word-boundary matcher for a tone lexicon.

    Substring matching is not good enough here: it scores "routine" as "rout"
    and "warning" as "war", which quietly turns filings into a selloff. Phrases
    ("record high", "soft landing") are matched with internal whitespace
    flexible so headline spacing does not defeat them.
    """
    alternatives = "|".join(
        r"\s+".join(re.escape(part) for part in word.split())
        for word in sorted(words, key=len, reverse=True))
    return re.compile(rf"\b(?:{alternatives})\b", re.IGNORECASE)


_RISK_OFF_RE = _lexicon(_RISK_OFF)
_RISK_ON_RE = _lexicon(_RISK_ON)


@dataclass(frozen=True)
class NewsRead:
    """What the qualitative record is saying, and how loudly."""

    item_count: int
    risk_off_hits: int
    risk_on_hits: int
    tone: str                      # risk_off | risk_on | mixed | quiet
    intensity: float               # 0..1, share of items carrying any tone
    top_tickers: list[str]
    headlines: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "item_count": self.item_count,
            "risk_off_hits": self.risk_off_hits,
            "risk_on_hits": self.risk_on_hits,
            "tone": self.tone,
            "intensity": round(self.intensity, 3),
            "top_tickers": list(self.top_tickers),
            "headlines": list(self.headlines),
        }


@dataclass(frozen=True)
class DeskRead:
    """Bob's composed view. Structure is deterministic; prose is layered on."""

    as_of: str
    quantitative_state: str        # calm | stress | uncertain | unknown
    news: NewsRead
    agreement: str                 # aligned | divergent | quiet
    conviction: float              # 0..1 — how much the evidence agrees
    tensions: list[str]
    observations: list[str]
    would_change_my_mind: list[str]
    evidence_refs: list[str]
    read_hash: str

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of,
            "quantitative_state": self.quantitative_state,
            "news": self.news.to_dict(),
            "agreement": self.agreement,
            "conviction": round(self.conviction, 3),
            "tensions": list(self.tensions),
            "observations": list(self.observations),
            "would_change_my_mind": list(self.would_change_my_mind),
            "evidence_refs": list(self.evidence_refs),
            "read_hash": self.read_hash,
            # A read is interpretation over facts, never an instruction.
            "advisory": True,
        }


def read_news(items, *, limit: int = 8) -> NewsRead:
    """Score the qualitative record with an auditable lexicon.

    ``items`` are :class:`qlab.news.feed.NewsItem`-shaped records. Tone counts
    DISTINCT items, not word occurrences, so one breathless headline cannot
    outweigh five calm ones.
    """
    items = list(items or [])
    risk_off = risk_on = toned = 0
    ticker_counter: Counter = Counter()
    headlines: list[dict] = []

    for item in items:
        text = f"{getattr(item, 'headline', '')} {getattr(item, 'summary', '')}"
        off = bool(_RISK_OFF_RE.search(text))
        on = bool(_RISK_ON_RE.search(text))
        if off:
            risk_off += 1
        if on:
            risk_on += 1
        if off or on:
            toned += 1
        for ticker in getattr(item, "tickers", ()) or ():
            ticker_counter[str(ticker)] += 1
        if len(headlines) < limit:
            headlines.append({
                "headline": str(getattr(item, "headline", ""))[:160],
                "source": str(getattr(item, "source", "")),
                "published": str(getattr(item, "published", "")),
                "tickers": list(getattr(item, "tickers", ()) or ()),
                "tone": ("risk_off" if off and not on
                         else "risk_on" if on and not off
                         else "mixed" if off and on else "neutral"),
            })

    if not items:
        tone = "quiet"
    elif risk_off > risk_on * 2:
        tone = "risk_off"
    elif risk_on > risk_off * 2:
        tone = "risk_on"
    elif toned == 0:
        tone = "quiet"
    else:
        tone = "mixed"

    return NewsRead(
        item_count=len(items),
        risk_off_hits=risk_off,
        risk_on_hits=risk_on,
        tone=tone,
        intensity=(toned / len(items)) if items else 0.0,
        top_tickers=[t for t, _ in ticker_counter.most_common(5)],
        headlines=headlines,
    )


def compose_read(
    *,
    as_of: str,
    panel: dict | None,
    news: NewsRead,
    portfolio: dict | None = None,
    recent_verdicts: list[dict] | None = None,
) -> DeskRead:
    """Compose the quantitative panel, the news read, and research evidence.

    The interesting output is not the label — it is the **tensions** list: the
    places where two kinds of evidence disagree. That is what a desk manager is
    for, and what a single number can never express.
    """
    panel = panel or {}
    portfolio = portfolio or {}
    verdicts = list(recent_verdicts or [])

    quant = str(panel.get("robust_state") or "unknown")
    tensions: list[str] = []
    observations: list[str] = []
    change_my_mind: list[str] = []
    refs: list[str] = []

    if panel.get("snapshot_id"):
        refs.append(f"snapshot:{panel['snapshot_id']}")

    # --- where the two kinds of evidence disagree --------------------------
    # Disagreement is recorded whenever the two point different ways. Intensity
    # scales how much weight it carries, never whether it is mentioned: a
    # quiet-but-real divergence is precisely what a desk manager should surface.
    if quant == "calm" and news.tone == "risk_off":
        strength = ("and the coverage is loud" if news.intensity >= 0.3
                    else "though the coverage is thin")
        tensions.append(
            "Prices are calm but the qualitative record is not: indicators "
            f"read calm while {news.risk_off_hits} of {news.item_count} "
            f"stories carry risk-off language ({strength}). Either the market "
            "has not repriced yet, or the coverage is noise.")
        change_my_mind.append(
            "A turbulence or vol-term-structure reading moving into its own "
            "tail would turn this from a narrative into a measurement.")
    elif quant == "stress" and news.tone in ("risk_on", "quiet"):
        tensions.append(
            "The measurements are stressed while the coverage is not. Stress "
            "that no one is writing about is usually positioning or liquidity "
            "rather than a story.")
        change_my_mind.append(
            "Coverage catching up to the measurements would argue the stress "
            "is fundamental rather than technical.")
    elif quant == "uncertain":
        tensions.append(
            "The indicator panel does not agree with itself, so no regime "
            f"label is honest right now: {panel.get('uncertainty_reason') or 'indicators disagree'}.")
        change_my_mind.append(
            "Indicator agreement rising above the panel's floor would make a "
            "regime call meaningful again.")

    # --- observations that stand on their own ------------------------------
    if quant in ("calm", "stress"):
        observations.append(
            f"Indicator panel reads {quant} with "
            f"{panel.get('agreement_count', 0)} of "
            f"{panel.get('agreement_count', 0) + panel.get('disagreement_count', 0)} "
            "usable indicators agreeing.")
    if panel.get("failed_count"):
        observations.append(
            f"{panel['failed_count']} indicator(s) failed to compute; the "
            "panel is thinner than usual and the read is correspondingly weaker.")
    if news.item_count:
        observations.append(
            f"{news.item_count} stories in the window, tone {news.tone}"
            + (f", concentrated in {', '.join(news.top_tickers[:3])}"
               if news.top_tickers else "") + ".")
    else:
        observations.append("No qualitative record in the window.")

    tier = str(portfolio.get("drawdown_tier") or "none")
    if tier != "none":
        observations.append(
            f"The book is in the {tier} drawdown tier, which constrains what "
            "any conclusion here is allowed to do.")
        refs.append(f"drawdown_tier:{tier}")

    fails = [v for v in verdicts if str(v.get("verdict")) == "FAIL"]
    if fails:
        observations.append(
            f"{len(fails)} recent referee FAIL(s); research is not currently "
            "producing an approved change.")
        refs.extend(f"verdict:{v.get('verdict_id')}" for v in fails[:3])

    # --- agreement and conviction ------------------------------------------
    if news.item_count == 0 and quant == "unknown":
        agreement = QUIET
    elif tensions:
        agreement = DIVERGENT
    elif news.tone == "quiet" and quant in ("calm", "unknown"):
        agreement = QUIET
    else:
        agreement = ALIGNED

    conviction = _conviction(agreement, panel, news)
    if conviction < 0.35:
        change_my_mind.append(
            "Conviction is low by construction here; treat this as an "
            "orientation, not a basis for changing the book.")

    read_hash = hashlib.sha256(json.dumps({
        "as_of": as_of, "quant": quant, "tone": news.tone,
        "agreement": agreement, "tensions": tensions,
    }, sort_keys=True, default=str).encode()).hexdigest()[:16]

    return DeskRead(
        as_of=as_of,
        quantitative_state=quant,
        news=news,
        agreement=agreement,
        conviction=conviction,
        tensions=tensions,
        observations=observations,
        would_change_my_mind=change_my_mind,
        evidence_refs=refs,
        read_hash=read_hash,
    )


def _conviction(agreement: str, panel: dict, news: NewsRead) -> float:
    """How much the evidence agrees with itself. Never a probability of a move."""
    if agreement == QUIET:
        return 0.25
    usable = int(panel.get("agreement_count", 0)) + int(
        panel.get("disagreement_count", 0))
    panel_agreement = (
        int(panel.get("agreement_count", 0)) / usable if usable else 0.0)
    # A divergent read is *interesting*, not confident: cap it well below a
    # clean alignment so "the story disagrees with the tape" never masquerades
    # as a strong signal.
    if agreement == DIVERGENT:
        # A loud divergence lowers conviction further: the more the two kinds
        # of evidence insist on different things, the less any single read is
        # worth acting on.
        return round(
            max(0.1, min(0.55, 0.25 + 0.3 * panel_agreement
                         - 0.2 * news.intensity)), 3)
    corroboration = 0.15 if news.item_count and news.intensity >= 0.25 else 0.0
    failed_penalty = 0.08 * int(panel.get("failed_count", 0))
    return round(
        max(0.0, min(1.0, 0.45 + 0.4 * panel_agreement + corroboration
                     - failed_penalty)), 3)


def should_open_debate(read) -> tuple[bool, str | None]:
    """Whether this read is a genuine material disagreement worth debating.

    Bob escalates when the *evidence* conflicts, not when it merely feels
    uncertain — the debate protocol is for material claims about how an
    estimate was formed, and "the news disagrees with the tape" is exactly a
    claim about the regime read. A thin, quiet divergence is still reported as
    a tension but does not earn a multi-agent debate.

    Accepts a :class:`DeskRead` or its ``to_dict()`` form, since the owner
    caches the dict.
    """
    if isinstance(read, DeskRead):
        read = read.to_dict()
    agreement = read.get("agreement")
    tensions = read.get("tensions") or []
    if agreement != DIVERGENT or not tensions:
        return False, None
    if read.get("quantitative_state") == "uncertain":
        return True, "regime_read"
    intensity = float((read.get("news") or {}).get("intensity", 0.0))
    if intensity >= 0.3:
        return True, "regime_read"
    return False, None
