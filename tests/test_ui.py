"""The single-page UI's JSON API (dispatch tested in-process, no sockets)."""

from __future__ import annotations

import pytest

from qlab.state.registry import Registry
from qlab.ui.server import UISession, _INDEX, handle_api


@pytest.fixture
def session():
    # isolated in-memory paper book per test
    return UISession(offline_default=True, registry=Registry(":memory:"))


def test_index_html_is_self_contained():
    html = _INDEX.read_text(encoding="utf-8")
    assert "<title>qlab" in html
    # no external CDN dependencies — must work fully offline
    assert "http://" not in html.split("<script>")[0] or "127.0.0.1" not in html
    assert "cdn" not in html.lower()
    assert 'data-nav="quantum"' in html


def test_bootstrap_has_everything_the_ui_needs(session):
    status, boot = handle_api(session, "GET", "/api/bootstrap", {}, {})
    assert status == 200
    assert {a["name"] for a in boot["agents"]} == {
        "moments-analyst", "challenger", "optimization-runner", "referee", "reporter"}
    assert boot["portfolio"]["equity"] == boot["mandate"]["paper_capital"]
    assert "mock" in boot["solvers"]


def test_resource_count_endpoint_returns_headline(session):
    status, rc = handle_api(session, "GET", "/api/resource_count",
                            {"n": ["7"], "r": ["4"]}, {})
    assert status == 200
    assert rc["total_logical_qubits"] == 434
    assert rc["dirac3_continuous_variables"] == 7


def test_recommend_and_run_once_and_reset(session):
    status, rec = handle_api(session, "POST", "/api/recommend", {},
                             {"as_of": "2026-07-13", "offline": True, "qaoa": False})
    assert status == 200 and abs(sum(rec["recommended_weights"].values()) - 1.0) < 1e-2

    status, summ = handle_api(session, "POST", "/api/run_once", {}, {"offline": True})
    assert status == 200 and summ["trade"]["executed"] is True

    status, port = handle_api(session, "GET", "/api/portfolio", {"offline": ["1"]}, {})
    assert status == 200 and len(port["positions"]) == 7

    status, r = handle_api(session, "POST", "/api/reset", {}, {})
    assert status == 200 and r["reset"] is True


def test_unknown_route_is_404(session):
    status, obj = handle_api(session, "GET", "/api/nope", {}, {})
    assert status == 404 and "error" in obj


def test_tui_snapshot_is_provenance_first(session):
    session.registry.record_event("demo", {"stage": "observe"})
    status, snap = handle_api(
        session, "GET", "/api/tui", {"offline": ["1"]}, {})

    assert status == 200
    assert snap["system"]["mode"] == "paper"
    assert snap["system"]["governed_authority"] == "propose_only"
    assert "human confirmation" in snap["system"]["governed_lock_reason"]
    assert snap["market"]["frequency"] == "daily"
    assert snap["market"]["source"] in {"synthetic", "yfinance"}
    assert len(snap["market"]["assets"]) == 7
    assert snap["events"][-1]["kind"] == "demo"
    assert {agent["name"] for agent in snap["agents"]} == {
        "moments-analyst", "challenger", "optimization-runner", "referee", "reporter"
    }


def test_events_endpoint_supports_initial_window(session):
    session.registry.record_event("one", {})
    session.registry.record_event("two", {})
    status, obj = handle_api(
        session, "GET", "/api/events", {"limit": ["1"]}, {})
    assert status == 200
    assert [event["kind"] for event in obj["events"]] == ["two"]
