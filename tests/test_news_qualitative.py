"""Qualitative signals: properties of the record, never of prices.

The governance line these tests defend: none of the six has a sign. A signal
that said "coverage is heavy and negative" and fed an allocation would be a
return forecast wearing a qualitative label.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from qlab.news.feed import fetch_news
from qlab.news.grounding import ground
from qlab.news.qualitative import (
    INSUFFICIENT,
    MIN_SIGNAL_ITEMS,
    NO_WINDOW,
    OK,
    qualitative_signals,
)

UNIVERSE = ["ACWI", "SPY", "QQQ", "IWM", "EEM", "BNDW", "TLT", "IEF", "TIP",
            "LQD", "HYG", "EMB", "GLD", "SLV", "GSG", "DBC", "USO", "IGF",
            "VNQ", "RWO"]
CLASSES = {t: ("equity" if t in {"ACWI", "SPY", "QQQ", "IWM", "EEM"}
               else "bonds" if t in {"BNDW", "TLT", "IEF", "TIP", "LQD", "HYG", "EMB"}
               else "real_assets") for t in UNIVERSE}
AS_OF = datetime(2026, 7, 30, tzinfo=timezone.utc)


def _window(universe=UNIVERSE, day=date(2026, 7, 30), hours=48):
    items = fetch_news(day, universe, lookback_hours=hours, offline=True)
    return ground(items, as_of=day.isoformat(), provider="synthetic",
                  universe=list(universe))


def _signals(**over):
    kwargs = dict(universe=UNIVERSE, asset_classes=CLASSES, as_of=AS_OF)
    kwargs.update(over)
    grounded = kwargs.pop("grounded", None) or _window()
    return qualitative_signals(grounded, **kwargs)


def _by_name(qs):
    return {s.name: s for s in qs.signals}


def test_all_six_signals_are_present_and_named():
    names = [s.name for s in _signals().signals]
    assert names == [
        "coverage_breadth", "asset_class_reach", "attention_concentration",
        "corroboration_ratio", "publisher_concentration", "window_age_hours",
    ]


def test_no_signal_carries_a_direction():
    """The governance boundary, asserted rather than asserted-about.

    None of these may be a return forecast in disguise, so none may carry a
    sign, a tone, a sentiment, or a direction word.
    """
    forbidden = ("bullish", "bearish", "positive", "negative", "sentiment",
                 "upside", "downside", "buy", "sell", "forecast", "predict")
    for signal in _signals().signals:
        blob = f"{signal.name} {signal.reason}".lower()
        assert not any(word in blob for word in forbidden), signal.name
        # Every value is a count, share, or age — all non-negative.
        assert signal.value is None or signal.value >= 0.0


def test_an_empty_window_is_never_zero():
    """"The feed returned nothing" and "the record is silent about the book"
    are different claims, and 0.0 conflates them."""
    empty = ground([], as_of="2026-07-30", provider="synthetic",
                   universe=UNIVERSE)
    qs = _signals(grounded=empty)
    assert qs.sufficient is False
    for signal in qs.signals:
        assert signal.value is None
        assert signal.state == NO_WINDOW
        assert signal.reason


def test_an_empty_window_still_names_every_holding_as_silent():
    # Stated positively: a reader must not have to infer the silence.
    empty = ground([], as_of="2026-07-30", provider="synthetic",
                   universe=UNIVERSE)
    breadth = _by_name(_signals(grounded=empty))["coverage_breadth"]
    assert breadth.detail["silent"] == UNIVERSE
    assert breadth.detail["covered"] == []


def test_a_thin_window_is_insufficient_not_zero():
    """A ratio over two records is one story rounded to two decimals."""
    thin = ground(
        fetch_news(date(2026, 7, 30), ["GLD", "SLV"], lookback_hours=48,
                   offline=True),
        as_of="2026-07-30", provider="synthetic", universe=["GLD", "SLV"])
    qs = qualitative_signals(thin, universe=["GLD", "SLV"],
                             asset_classes=CLASSES, as_of=AS_OF)
    assert qs.item_count < MIN_SIGNAL_ITEMS and qs.sufficient is False
    named = _by_name(qs)
    for gated in ("attention_concentration", "corroboration_ratio",
                  "publisher_concentration"):
        assert named[gated].value is None
        assert named[gated].state == INSUFFICIENT
    # But what is merely descriptive stays available at any count.
    assert named["window_age_hours"].value is not None
    assert named["coverage_breadth"].value is not None


def test_coverage_breadth_names_the_silent_holdings():
    breadth = _by_name(_signals())["coverage_breadth"]
    assert 0.0 <= breadth.value <= 1.0
    covered, silent = breadth.detail["covered"], breadth.detail["silent"]
    assert set(covered) | set(silent) == set(UNIVERSE)
    assert not set(covered) & set(silent)


def test_asset_class_reach_reports_the_classes_nobody_wrote_about():
    reach = _by_name(_signals())["asset_class_reach"]
    assert 0.0 <= reach.value <= 1.0
    assert "silent_classes" in reach.detail


def test_attention_concentration_is_an_effective_count():
    """1/HHI, so it reads as "the record is effectively about N names"."""
    conc = _by_name(_signals())["attention_concentration"]
    assert conc.state == OK
    # Bounded by the number of names actually mentioned.
    assert 1.0 <= conc.value <= len(UNIVERSE)


def test_corroboration_ratio_is_a_share_of_claims():
    ratio = _by_name(_signals())["corroboration_ratio"]
    assert 0.0 <= ratio.value <= 1.0
    assert ratio.detail["corroborated"] <= ratio.detail["total"]


def test_publisher_concentration_is_the_heaviest_publisher_share():
    pub = _by_name(_signals())["publisher_concentration"]
    assert 0.0 < pub.value <= 1.0
    assert pub.detail["distinct_publishers"] >= 1


def test_window_age_is_inside_the_window():
    age = _by_name(_signals())["window_age_hours"]
    assert 0.0 <= age.detail["newest_hours"] <= age.value <= age.detail["oldest_hours"]
    assert age.detail["oldest_hours"] <= 48.0


def test_signals_are_a_pure_function_of_the_window():
    """No desk, no registry, no config — that is what makes them testable."""
    grounded = _window()
    a = qualitative_signals(grounded, universe=UNIVERSE, asset_classes=CLASSES,
                            as_of=AS_OF)
    b = qualitative_signals(grounded, universe=UNIVERSE, asset_classes=CLASSES,
                            as_of=AS_OF)
    assert a.to_dict() == b.to_dict()


def test_to_dict_is_json_ready_and_keyed_by_name():
    import json

    payload = _signals().to_dict()
    json.dumps(payload)          # must not raise
    assert set(payload["by_name"]) == {s["name"] for s in payload["signals"]}
    assert payload["sufficient"] is True
