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
    assert 'data-nav="algorithms"' in html
    assert "434" not in html


def test_bootstrap_has_everything_the_ui_needs(session):
    status, boot = handle_api(session, "GET", "/api/bootstrap", {}, {})
    assert status == 200
    assert {a["name"] for a in boot["agents"]} == {
        "moments-analyst", "challenger", "optimization-runner", "referee", "reporter"}
    assert boot["portfolio"]["equity"] == boot["mandate"]["paper_capital"]
    assert "mock" in boot["solvers"]


def test_algorithm_catalog_endpoint_marks_offline_methods_non_runnable(session):
    status, result = handle_api(session, "GET", "/api/algorithms", {}, {})
    assert status == 200
    offline = [row for row in result["algorithms"] if row["stage"] == "offline"]
    assert offline
    assert not any(row["agent_usable"] for row in offline)


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/resource_count"),
    ("POST", "/api/compare"),
])
def test_offline_quantum_routes_are_not_staged(session, method, path):
    status, result = handle_api(session, method, path, {}, {})
    assert status == 404
    assert "no route" in result["error"]


def test_recommend_and_run_once_and_reset(session):
    status, rec = handle_api(session, "POST", "/api/recommend", {},
                             {"as_of": "2026-07-13", "offline": True})
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
    assert snap["policy"]["id"] == "hrp"
    assert snap["workflows"] == []


def test_owner_exposes_safe_lab_tools_and_durable_workflows(session):
    status, result = handle_api(
        session, "POST", "/api/lab/data.fetch_universe", {},
        {"which": "core", "offline": True},
    )
    assert status == 200 and len(result["result"]["tickers"]) == 7

    status, workflow = handle_api(
        session, "POST", "/api/workflows/start", {},
        {"goal": "review the paper portfolio", "offline": True},
    )
    assert status == 200 and workflow["current_phase"] == "analyst"
    workflow_id = workflow["workflow_id"]

    status, workflow = handle_api(
        session, "POST", "/api/workflows/analyst", {},
        {"workflow_id": workflow_id, "status": "working"},
    )
    assert status == 200 and workflow["steps"][0]["status"] == "working"

    status, fetched = handle_api(
        session, "GET", f"/api/workflows/{workflow_id}", {}, {},
    )
    assert status == 200 and fetched["workflow_id"] == workflow_id


@pytest.mark.parametrize("tool", [
    "regime.turbulence", "regime.absorption", "regime.volatility_term_structure",
    "regime.drawdown", "regime.tail_risk",
])
def test_owner_exposes_regime_indicators_in_one_shared_schema(session, tool):
    status, obj = handle_api(
        session, "POST", f"/api/lab/{tool}", {},
        {"as_of": "2022-06-30", "universe": "core", "offline": True},
    )
    assert status == 200, obj
    reading = obj["result"]
    assert reading["regime"] in ("calm", "stress")
    # the schema is uniform across all five, so the analyst can compare them
    assert {"indicator", "signal", "threshold", "percentile", "reasoning"} <= set(reading)
    assert reading["reasoning"]


def test_owner_refuses_a_regime_tool_outside_the_allowlist(session):
    with pytest.raises(PermissionError):
        session.call_lab_tool("regime.made_up", {"as_of": "2022-06-30"}, True)


def test_owner_preview_uses_exact_referee_reviewed_targets(session):
    from datetime import date

    from qlab.core.types import Decision

    tickers = session.mandate.universe_whitelist
    targets = {ticker: 1.0 / len(tickers) for ticker in tickers}
    decision_id = session.registry.log_decision(Decision(
        as_of=date.today(), kind="rebalance_gate",
        choice={"targets": targets}, rationale="configured HRP policy",
    ))
    session.registry.log_verdict(
        decision_id, "PASS", ["within mandate"], source="referee-agent",
        targets=targets,
    )

    status, preview = handle_api(
        session, "POST", "/api/rebalance_preview", {},
        {"offline": True, "decision_id": decision_id, "targets": targets},
    )
    assert status == 200 and preview["accepted"] is True
    assert preview["state"] == "checked"

    changed = dict(targets)
    changed[tickers[0]] += 0.01
    status, rejected = handle_api(
        session, "POST", "/api/rebalance_preview", {},
        {"offline": True, "decision_id": decision_id, "targets": changed},
    )
    assert status == 200 and rejected["blocked_by"] == "referee"

    status, executed = handle_api(
        session, "POST", "/api/plans/execute", {},
        {"offline": True, "plan_id": preview["plan_id"], "human_confirmed": True},
    )
    assert status == 200 and executed["executed"] is True
    assert executed["plan_id"] == preview["plan_id"]


def test_tui_snapshot_surfaces_verdicts_and_data_provenance(session):
    from datetime import date

    from qlab.core.types import Decision

    dec = Decision(as_of=date.today(), kind="rebalance_gate",
                   choice={"targets": {"GLD": 1.0}}, rationale="calm regime",
                   challenger_view="turnover is defensible under stress")
    did = session.registry.log_decision(dec)
    session.registry.log_verdict(did, "PASS", ["within mandate"],
                                 source="deterministic", targets={"GLD": 1.0})

    status, snap = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})
    assert status == 200

    by_id = {d["decision_id"]: d for d in snap["decisions"]}
    assert by_id[did]["verdict"]["verdict"] == "PASS"
    assert by_id[did]["verdict"]["reasons"] == ["within mandate"]
    assert by_id[did]["verdict"]["source"] == "deterministic"
    # challenger_view already rides along on the SELECT * decision row
    assert by_id[did]["challenger_view"] == "turnover is defensible under stress"

    # provenance is populated (tui_snapshot warms the panel cache first) and
    # never triggers a network fetch
    assert snap["system"]["data_source"] in {"synthetic", "yfinance"}
    assert isinstance(snap["system"]["data_age_days"], int)
    assert snap["system"]["data_age_days"] >= 0


def test_system_status_reports_no_cache_provenance(session):
    # a bare status poll with no warmed cache must not fetch and must say "none"
    status = session.system_status(offline=True)
    assert status["data_source"] == "none"
    assert status["data_age_days"] is None


def test_events_endpoint_supports_initial_window(session):
    session.registry.record_event("one", {})
    session.registry.record_event("two", {})
    status, obj = handle_api(
        session, "GET", "/api/events", {"limit": ["1"]}, {})
    assert status == 200
    assert [event["kind"] for event in obj["events"]] == ["two"]


def test_sse_stream_delivers_live_events(session):
    """A real streamed connection receives bus events as they are recorded."""
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    import qlab.ui.server as server_module

    handler = type("H", (server_module._Handler,), {"session": session})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with server_module._LOCK:
            session.registry.record_event("primer", {"n": 1})
        response = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/stream", timeout=10)
        first = response.readline().decode()
        assert first.startswith("data:")
        assert '"primer"' in first

        with server_module._LOCK:
            session.registry.record_event("workflow_phase", {"phase": "analyst"})
        seen = ""
        for _ in range(40):
            line = response.readline().decode()
            if '"workflow_phase"' in line:
                seen = line
                break
        assert seen.startswith("data:")
        response.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_sse_stream_delivers_transient_quote_events(session, monkeypatch):
    """The producer feeds SSE without writing its quote into DuckDB."""
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    import qlab.ui.server as server_module

    assets = [
        {"ticker": ticker, "price": 100.0 + index, "change_1d": index / 1000}
        for index, ticker in enumerate(["ACWI", "BNDW", "GSG", "IGF"])
    ]
    monkeypatch.setattr(
        session,
        "market",
        lambda offline: {"assets": assets},
    )
    durable_before = session.registry.read_events()
    event = server_module._publish_quote_event(session)
    assert event is not None
    assert event["kind"] == "quote"
    assert event["payload"]["rows"] == assets
    assert server_module._publish_quote_event(session) is None
    assert session.registry.read_events() == durable_before

    handler = type("H", (server_module._Handler,), {"session": session})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        response = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/stream?kind=quote", timeout=10)
        line = response.readline().decode()
        assert line.startswith("data:")
        streamed = json.loads(line.removeprefix("data:").strip())
        assert streamed == event
        response.close()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_quote_event_cli_format_shows_three_tickers_and_total():
    from qlab.desk_cli import format_event

    _, line = format_event({
        "ts": "2026-07-24T12:34:56+00:00",
        "kind": "quote",
        "payload": {"rows": [
            {"ticker": ticker, "price": 100.0 + index, "change_1d": 0.001}
            for index, ticker in enumerate(["ACWI", "BNDW", "GSG", "IGF"])
        ]},
    })

    assert all(ticker in line for ticker in ("ACWI", "BNDW", "GSG"))
    assert "IGF" not in line
    assert "count=4" in line
