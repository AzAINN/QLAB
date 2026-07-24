"""Exact classical k-of-N selection and its owner-backed research tool."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from qlab.core.selection import select_k_of_n


def test_exact_five_choose_two_has_hand_computed_optimum() -> None:
    # Unit-vol assets represented by five unit vectors. Every pair has score
    # -2 + |cos(angle_i - angle_j)|, so B/E is the unique orthogonal pair.
    angles = np.deg2rad([0.0, 10.0, 30.0, 60.0, 100.0])
    factors = np.column_stack((np.cos(angles), np.sin(angles)))
    covariance = factors @ factors.T

    result = select_k_of_n(
        ["A", "B", "C", "D", "E"],
        2,
        covariance=covariance,
        volatilities=np.ones(5),
    )

    assert result.selected == ["B", "E"]
    assert result.score == pytest.approx(-2.0)
    assert result.contributions == pytest.approx({"B": -1.0, "E": -1.0})
    assert sum(result.contributions.values()) == pytest.approx(result.score)


def test_returns_panel_is_deterministic_and_infers_tickers() -> None:
    rng = np.random.default_rng(17)
    returns = pd.DataFrame(
        rng.normal(size=(80, 5)),
        columns=["A", "B", "C", "D", "E"],
    )

    first = select_k_of_n(returns, 2)
    second = select_k_of_n(returns, 2)

    assert first.to_dict() == second.to_dict()
    assert len(first.selected) == 2
    assert set(first.contributions) == set(first.selected)


def test_exact_selection_refuses_more_than_25_assets() -> None:
    with pytest.raises(ValueError, match=r"N <= 25"):
        select_k_of_n(
            [f"T{i:02d}" for i in range(26)],
            2,
            covariance=np.eye(26),
        )


def test_selection_owner_tool_round_trips_offline_and_persists_result() -> None:
    from qlab.state.registry import Registry
    from qlab.ui.server import UISession, handle_api

    session = UISession(
        offline_default=True,
        seed=11,
        registry=Registry(":memory:"),
    )
    status, response = handle_api(
        session,
        "POST",
        "/api/lab/selection.run",
        {},
        {
            "as_of": "2022-06-30",
            "tickers": ["AAA", "BBB", "CCC", "DDD", "EEE"],
            "k": 2,
            "lookback_days": 80,
            "offline": True,
        },
    )

    assert status == 200
    result = response["result"]
    assert set(result) == {"selected", "score", "run_id", "contributions"}
    assert len(result["selected"]) == 2
    assert set(result["contributions"]) == set(result["selected"])
    assert sum(result["contributions"].values()) == pytest.approx(result["score"])

    run = next(
        row for row in session.registry.list_runs()
        if row["run_id"] == result["run_id"]
    )
    assert run["kind"] == "selection"
    assert run["spec"]["algorithm_id"] == "selection_k_of_n"
    assert run["spec"]["result"] == {
        "selected": result["selected"],
        "score": result["score"],
        "contributions": result["contributions"],
    }
