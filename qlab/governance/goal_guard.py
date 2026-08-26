"""The domain gate on a workflow goal: what this desk will research at all.

A goal is free text, and free text is where an agent desk stops being a
desk. ``qlab workforce run "write me an email to my landlord"`` would spend a
governed research slot, a coordinator lease and five role invocations on a
task no role here can do and no referee could pass — and the refusal would
arrive from the analyst's tools, forty seconds in, phrased as a data problem.

So the gate is deterministic and at the front door, per the boundary the
project is built on: deterministic code owns rigor. Two tests, both about the
*words*, neither about the answer:

* **Off-domain intent is refused by shape.** Composition tasks (an email, an
  essay, a poem, code), personal errands and chit-chat name themselves, and a
  goal that names one is not a research goal however many market words it
  also carries.
* **A goal must anchor in the desk's domain.** The portfolio, a market, a
  regime, a risk question, a method, or a name in the mandate's universe.
  "help me" anchors nowhere and is refused with the sentence that says what
  a goal is made of.

The reasoner may still decline a goal that passes here; this gate is the
floor, not the judgment. No allow-list of goals: research is open-ended, and
a gate that enumerated permitted questions would be a second opinion about
what is worth asking.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

MIN_GOAL_CHARS = 8

# Composition and errand shapes. Each is an intent the desk cannot serve —
# it holds no tool that writes, sends, books or translates — and naming them
# by verb-plus-object rather than by keyword keeps the gate about the task
# asked for, not about a word that happens to appear.
_OFF_DOMAIN = [
    (re.compile(r"\b(write|draft|compose|send|reply to|forward)\b[^.]{0,40}\b"
                r"(e-?mail|letter|memo|essay|poem|story|tweet|post|blog|"
                r"cover letter|resume|cv|speech|toast|apology|invitation)\b", re.I),
     "composing correspondence or prose"),
    (re.compile(r"\b(write|generate|fix|debug|refactor|implement)\b[^.]{0,40}\b"
                r"(code|script|function|class|program|python|rust|javascript|sql|"
                r"regex|unit tests?)\b", re.I),
     "writing or fixing code"),
    (re.compile(r"\b(translate|proofread|paraphrase|rewrite)\b", re.I),
     "language editing"),
    (re.compile(r"\b(book|order|buy me|schedule|remind me|call|text)\b[^.]{0,30}\b"
                r"(flight|hotel|table|dinner|meeting|appointment|uber|taxi|pizza|"
                r"groceries|doctor)\b", re.I),
     "personal errands"),
    (re.compile(r"\b(tell me a joke|riddle|recipe|horoscope|weather|"
                r"who won|movie|song|lyrics)\b", re.I),
     "chit-chat and lookups outside markets"),
]

# What a research goal is made of. Broad on purpose — research is open-ended
# — and extended at runtime with the mandate's own universe, so a goal that
# names a held ticker anchors even when it uses none of these words.
_DOMAIN_ANCHORS = frozenset("""
portfolio allocation allocate rebalance rebalancing weight weights position
positions book exposure hedge hedging overlay regime regimes risk drawdown
volatility vol variance covariance correlation correlations dispersion tail
cvar var sharpe sortino return returns drift breach turnover leverage gross
net market markets sector sectors asset assets class classes equity equities
stock stocks bond bonds treasury treasuries credit spread spreads commodity
commodities gold oil energy metal metals real estate reit reits currency fx
rates rate yield yields inflation macro fed fomc cpi payrolls earnings
momentum trend value carry quality signal signals indicator indicators
predictor predictors forecast forecasting estimate estimation estimator
shrinkage moments kurtosis skew skewness optimizer optimization optimize
backtest backtests walk-forward ablation universe benchmark benchmarks
review research analyze analysis assess assessment challenge referee news
narrative coverage sentiment liquidity concentration diversification
absorption turbulence tension tensions mandate cap caps limit limits
plan proposal proposals recommendation recommend etf etfs holdings
""".split())


class GoalRefused(ValueError):
    """The goal is not something this desk researches; the message says why."""


def _words(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z\-]*", text)}


def check_goal(goal: str, universe: Iterable[str] = ()) -> str:
    """Return the goal, stripped, or raise :class:`GoalRefused` with the reason.

    ``universe`` is the mandate's ticker whitelist: a name in it anchors a
    goal on its own, so "what is going on with GLD" is research here whether
    or not it says the word market.
    """
    text = (goal or "").strip()
    if len(text) < MIN_GOAL_CHARS:
        raise GoalRefused(
            "a research goal needs a sentence: name the portfolio, a market, "
            "a regime, a risk question, or a method to compare")
    for pattern, what in _OFF_DOMAIN:
        if pattern.search(text):
            raise GoalRefused(
                f"not a research goal for this desk — that reads as {what}. "
                "The workforce researches this portfolio and its markets; it "
                "holds no tool for anything else, and a slot spent on it would "
                "fail forty seconds in, phrased as a data problem.")
    words = _words(text)
    anchors = _DOMAIN_ANCHORS | {str(t).lower() for t in universe}
    if words.isdisjoint(anchors):
        raise GoalRefused(
            "not a research goal for this desk: nothing in it names the "
            "portfolio, a market, a regime, a risk question, a method, or a "
            "ticker in the mandate's universe. Say what should be looked at.")
    return text
