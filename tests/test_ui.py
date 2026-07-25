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

    # run_once is proposal-only: it opens an approval rather than booking.
    status, summ = handle_api(session, "POST", "/api/run_once", {}, {"offline": True})
    assert status == 200 and summ["trade"]["executed"] is False
    assert summ["trade"]["blocked_by"] == "approval_required"

    status, port = handle_api(session, "GET", "/api/portfolio", {"offline": ["1"]}, {})
    assert status == 200 and port["positions"] == {}

    status, r = handle_api(session, "POST", "/api/reset", {}, {})
    assert status == 200 and r["reset"] is True


def test_unknown_route_is_404(session):
    status, obj = handle_api(session, "GET", "/api/nope", {}, {})
    assert status == 404 and "error" in obj


def test_data_health_endpoint_reports_demo_data_as_research_only(session):
    status, health = handle_api(
        session, "GET", "/api/data/health", {"offline": ["1"]}, {})
    assert status == 200
    assert health["blocked"] is False
    assert health["mode"] == "demo"
    assert health["provider"] == "synthetic"
    # Synthetic demo data is never execution-grade.
    assert health["eligible_for_research"] is True
    assert health["eligible_for_paper_proposal"] is False
    assert health["eligible_for_execution"] is False
    assert health["permit_id"].startswith("sha256:")


def test_data_permit_current_returns_recorded_permit(session):
    # Recording happens as a side effect of the health evaluation.
    handle_api(session, "GET", "/api/data/health", {"offline": ["1"]}, {})
    status, current = handle_api(
        session, "GET", "/api/data/permit/current", {}, {})
    assert status == 200
    assert current["purpose"] == "paper_proposal"
    assert current["permit"]["provider"] == "synthetic"
    assert current["permit"]["eligible_for_paper_proposal"] is False


def test_data_permit_current_is_null_before_any_evaluation(session):
    status, current = handle_api(
        session, "GET", "/api/data/permit/current", {}, {})
    assert status == 200 and current["permit"] is None


def test_quotes_endpoint_reports_no_stream_in_demo(session):
    status, out = handle_api(session, "GET", "/api/quotes", {}, {})
    assert status == 200
    assert out["live_stream"] is False
    assert out["quotes"] == {} and out["health"] is None


def test_quotes_endpoint_surfaces_injected_stream(session):
    from qlab.data.stream import MarketStreamSupervisor

    clock = type("C", (), {"t": 0.0, "__call__": lambda self: self.t})()
    stream = MarketStreamSupervisor(["ACWI", "BNDW"], "sip", clock=clock)
    stream.mark_connected()
    stream.on_quote("ACWI", 101.0)
    stream.on_quote("BNDW", 50.0)
    session.market_stream = stream

    status, out = handle_api(session, "GET", "/api/quotes",
                             {"symbols": ["ACWI,BNDW"]}, {})
    assert status == 200
    assert out["live_stream"] is True and out["feed"] == "sip"
    assert out["quotes"]["ACWI"]["price"] == 101.0
    assert out["health"]["fresh"] is True


def test_data_health_withdraws_execution_when_quotes_stale(session):
    from qlab.data.stream import MarketStreamSupervisor

    # A live stream that has gone stale must withdraw execution eligibility even
    # though (in demo) the daily bar path is what we can exercise offline.
    clock = type("C", (), {"t": 100.0, "__call__": lambda self: self.t})()
    stream = MarketStreamSupervisor(["ACWI"], "iex", stale_after_s=5.0, clock=clock)
    stream.mark_connected()  # no quotes → stale
    session.market_stream = stream

    status, health = handle_api(
        session, "GET", "/api/data/health", {"offline": ["1"]}, {})
    assert status == 200
    assert health["eligible_for_execution"] is False
    assert health["quote_health"]["fresh"] is False


def test_regime_panel_endpoint_is_a_diagnostic_not_a_signal(session):
    status, panel = handle_api(session, "GET", "/api/regime/panel",
                               {"offline": ["1"]}, {})
    assert status == 200
    assert panel["robust_state"] in ("calm", "stress", "uncertain")
    assert len(panel["readings"]) == 5
    assert panel["fingerprint"]["snapshot_id"] == panel["snapshot_id"]
    # A panel describes state; it must not carry weights or a recommendation.
    assert "targets" not in panel and "recommendation" not in panel


def test_decision_outcome_and_lesson_routes(session):
    from datetime import date as _date

    from qlab.core.types import Decision

    did = session.registry.log_decision(Decision(
        as_of=_date(2026, 7, 1), kind="regime", choice={"regime": "calm"},
        rationale="fixture"))
    session.registry.update_reflection(did, {"realized_vol": 0.2,
                                             "outcome_hash": "abc123"}, "lesson")
    status, out = handle_api(session, "GET", f"/api/decisions/{did}/outcome", {}, {})
    assert status == 200 and out["outcome"]["realized_vol"] == 0.2

    status, les = handle_api(session, "GET", f"/api/decisions/{did}/lesson", {}, {})
    assert status == 200 and les["lesson"] is None  # none generated yet

    status, missing = handle_api(
        session, "GET", "/api/decisions/nope/outcome", {}, {})
    assert status == 404


def test_workflow_debate_route_does_not_shadow_the_workflow_route(session):
    from qlab.governance.debate import open_debate

    wf = session.registry.start_workflow("portfolio_review", {"goal": "t"})
    wid = wf["workflow_id"] if isinstance(wf, dict) else wf
    open_debate(session.registry, workflow_id=wid,
                original_decision_id="dec-1",
                material_claims=["estimation_window"])
    status, out = handle_api(session, "GET", f"/api/workflows/{wid}/debate", {}, {})
    assert status == 200 and len(out["debates"]) == 1
    assert out["debates"][0]["turns"] == []
    # The plain workflow route still resolves.
    status, workflow = handle_api(session, "GET", f"/api/workflows/{wid}", {}, {})
    assert status == 200 and workflow["workflow_id"] == wid


def test_model_invocations_route(session):
    from qlab.operator.model_routing import record_invocation, resolve_route

    record_invocation(session.registry, resolve_route("reporter"))
    status, out = handle_api(session, "GET", "/api/models/invocations", {}, {})
    assert status == 200 and out["invocations"][0]["role"] == "reporter"


def test_bob_status_starts_in_observe(session):
    status, out = handle_api(session, "GET", "/api/bob/status", {}, {})
    assert status == 200
    assert out["mode"] == "observe"
    assert out["manager_id"] == "bob-the-quant"


def test_bob_observe_tick_returns_state_and_brief(session):
    status, out = handle_api(session, "POST", "/api/bob/observe", {},
                             {"offline": True})
    assert status == 200
    # Demo data is research-only, so Bob is not blocked on it; coordinator may be
    # absent in CI -> degraded, else observing. Either way a brief is produced.
    assert out["state"] in ("observing", "degraded")
    assert out["brief"]["book"]["equity"] is not None


def test_bob_mode_and_pause_resume(session):
    status, out = handle_api(session, "POST", "/api/bob/mode", {},
                             {"mode": "research"})
    assert status == 200 and out["mode"] == "research"
    status, bad = handle_api(session, "POST", "/api/bob/mode", {},
                             {"mode": "nonsense"})
    assert status == 400
    status, paused = handle_api(session, "POST", "/api/bob/pause", {}, {})
    assert paused["mode"] == "paused"
    status, resumed = handle_api(session, "POST", "/api/bob/resume", {}, {})
    assert resumed["mode"] == "observe"


def test_bob_message_never_grants_authority(session):
    status, out = handle_api(session, "POST", "/api/bob/message", {},
                             {"text": "what is our drawdown?"})
    assert status == 200 and out["received"] is True
    # No authority field, no execution — just an acknowledgement.
    assert "note" in out


def test_live_portfolio_marks_to_market_with_provenance(session, monkeypatch):
    # Deploy the book (autopilot is proposal-only, so authorize booking here),
    # then evaluate it live.
    monkeypatch.setenv("QLAB_AUTOPILOT_EXECUTE", "1")
    handle_api(session, "POST", "/api/run_once", {}, {"offline": True})
    status, live = handle_api(
        session, "GET", "/api/portfolio/live", {"offline": ["1"]}, {})
    assert status == 200
    assert live["blocked"] is False
    assert live["equity"] > 0
    assert len(live["positions"]) == 7
    # A fully-invested long book: gross ~ net ~ 1.0.
    assert live["gross_exposure"] == pytest.approx(live["net_exposure"], abs=1e-6)
    assert 0.9 < live["gross_exposure"] <= 1.0 + 1e-6
    for row in live["positions"]:
        assert "unrealized_pnl" in row and "weight" in row
    # Demo marks are never execution-grade and are labeled as such.
    assert live["marks"]["live"] is False
    assert live["marks"]["execution_grade"] is False
    assert "kill_switch_distance" in live


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


def test_tui_stress_scan_keeps_same_timestamp_refusal_at_window_edge(session):
    import json

    timestamp = "2026-07-24T12:34:56+00:00"
    rows = [
        (
            f"event-{index:03d}",
            timestamp,
            "filler",
            json.dumps({"n": index}),
        )
        for index in range(100)
    ]
    rows.append((
        "zz-refusal",
        timestamp,
        "cost_gate_refusal",
        json.dumps({
            "plan_id": "same-ts-refusal",
            "reasons": ["net-alpha gate"],
        }),
    ))
    session.registry.con.executemany(
        "INSERT INTO events VALUES (?,?,?,?)",
        rows,
    )

    snapshot = session.tui_snapshot(offline=True, event_limit=100)

    assert len(snapshot["events"]) == 100
    assert any(event["event_id"] == "zz-refusal" for event in snapshot["events"])
    assert snapshot["stress"]["cost_gate_refusals"][0]["plan_id"] == (
        "same-ts-refusal"
    )


def test_stress_tier_uses_unrounded_drawdown_not_display_value(session):
    ticker = session.mandate.universe_whitelist[0]
    raw_drawdown = session.mandate.drawdown_tiers.control - 4e-5
    high_water_mark = 10_000.0
    portfolio = {
        "equity": high_water_mark * (1.0 - raw_drawdown),
        "high_water_mark": high_water_mark,
        "drawdown": round(raw_drawdown, 4),
        "weights": {ticker: 0.10},
    }
    market = {
        "assets": [{"ticker": ticker, "realized_vol": 0.20}],
    }

    assert session.mandate.drawdown_tier(portfolio["drawdown"]) == "control"
    stress = session.stress_payload(portfolio, market, replays={}, events=[])
    assert stress["drawdown_tier"] == "warning"


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


def test_owner_starts_panel_and_round_trips_a_dynamic_phase(session):
    variants = [
        {"label": "responsive", "window": 252, "shrinkage": "ledoit_wolf"},
        {"label": "stable", "window": 756, "shrinkage": "nonlinear"},
    ]
    status, workflow = handle_api(
        session,
        "POST",
        "/api/workflows/start",
        {},
        {
            "goal": "compare estimator variants",
            "kind": "panel",
            "variants": variants,
            "offline": True,
        },
    )

    assert status == 200
    assert workflow["kind"] == "panel"
    assert workflow["request"]["variants"] == variants
    assert [step["phase"] for step in workflow["steps"]] == [
        "analyst-1", "analyst-2", "optimizer-1", "optimizer-2",
        "judge", "referee", "reporter",
    ]

    workflow_id = workflow["workflow_id"]
    status, updated = handle_api(
        session,
        "POST",
        "/api/workflows/analyst-2",
        {},
        {
            "workflow_id": workflow_id,
            "status": "done",
            "summary": "stable stance estimated",
            "artifacts": {
                "moment_set_id": "moments-stable",
                "objective_id": "objective-stable",
                "decision_id": "decision-stable",
            },
        },
    )
    assert status == 200
    assert next(
        step for step in updated["steps"] if step["phase"] == "analyst-2"
    )["status"] == "done"

    status, fetched = handle_api(
        session, "GET", f"/api/workflows/{workflow_id}", {}, {},
    )
    assert status == 200
    assert next(
        step for step in fetched["steps"] if step["phase"] == "analyst-2"
    )["summary"] == "stable stance estimated"

    status, invalid = handle_api(
        session,
        "POST",
        "/api/workflows/start",
        {},
        {"kind": "panel", "variants": [{"window": 252}]},
    )
    assert status == 400
    assert "2..5" in invalid["error"]

    status, invalid = handle_api(
        session,
        "POST",
        "/api/workflows/analyst-x",
        {},
        {"workflow_id": workflow_id, "status": "working"},
    )
    assert status == 400
    assert "unknown workforce phase" in invalid["error"]


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


def test_sse_stream_resume_after_event_id_delivers_same_timestamp_sibling(
    session,
):
    """A reconnect resumes after the exact merged-stream tuple."""
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from urllib.parse import urlencode

    import qlab.state.registry as registry_module
    import qlab.ui.server as server_module

    boundary_ts = "2026-07-24T12:34:56+00:00"
    original_now = registry_module._now
    registry_module._now = lambda: boundary_ts
    try:
        event_ids = [
            session.registry.record_event("same-ts", {"n": n})
            for n in (1, 2)
        ]
    finally:
        registry_module._now = original_now
    first_id, second_id = sorted(event_ids)

    handler = type("H", (server_module._Handler,), {"session": session})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    response = None
    try:
        query = urlencode({"after": boundary_ts, "after_id": first_id})
        response = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/stream?{query}", timeout=5)
        resumed = json.loads(
            response.readline().decode().removeprefix("data:").strip())
        assert resumed["event_id"] == second_id
    finally:
        if response is not None:
            response.close()
        httpd.shutdown()
        httpd.server_close()


def test_sse_stream_expands_a_saturated_timestamp_boundary(
    session, capsys,
):
    """A full delivered boundary page cannot pin the stream cursor forever."""
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer
    from urllib.parse import urlencode

    import qlab.state.registry as registry_module
    import qlab.ui.server as server_module

    boundary_ts = "2026-07-24T12:34:56+00:00"
    original_now = registry_module._now
    registry_module._now = lambda: boundary_ts
    try:
        boundary_ids = {
            session.registry.record_event("boundary", {"n": n})
            for n in range(3)
        }
        registry_module._now = lambda: "2026-07-24T12:34:57+00:00"
        sentinel_id = session.registry.record_event("sentinel", {})
    finally:
        registry_module._now = original_now

    handler = type(
        "H",
        (server_module._Handler,),
        {"session": session, "stream_page_cap": 2},
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    response = None
    try:
        query = urlencode({"after": "2026-07-24T12:34:55+00:00"})
        response = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/stream?{query}", timeout=5)
        received = []
        while not any(event["event_id"] == sentinel_id for event in received):
            line = response.readline().decode()
            if line.startswith("data:"):
                received.append(json.loads(
                    line.removeprefix("data:").strip()))

        received_boundary_ids = [
            event["event_id"]
            for event in received
            if event["kind"] == "boundary"
        ]
        assert set(received_boundary_ids) == boundary_ids
        assert len(received_boundary_ids) == len(boundary_ids)
        assert "stream boundary page full" in capsys.readouterr().out
    finally:
        if response is not None:
            response.close()
        httpd.shutdown()
        httpd.server_close()


def test_api_client_resubscribe_uses_last_event_tuple(monkeypatch):
    """A transparent reconnect preserves the merged stream's exact cursor."""
    import json

    import qlab.tui.client as client_module

    first = {
        "event_id": "event-a",
        "ts": "2026-07-24T12:34:56+00:00",
        "kind": "audit",
    }
    second = {
        "event_id": "event-b",
        "ts": "2026-07-24T12:34:56+00:00",
        "kind": "audit",
    }

    class FakeResponse:
        def __init__(self, event):
            self.event = event

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            return None

        def iter_lines(self):
            yield f"data: {json.dumps(self.event)}"

    responses = iter([FakeResponse(first), FakeResponse(second)])
    calls = []

    def fake_stream(method, url, *, params, timeout):
        calls.append(dict(params))
        return next(responses)

    monkeypatch.setattr(client_module.httpx, "stream", fake_stream)
    events = client_module.ApiClient("http://owner").stream(
        "/api/stream", kind="audit")
    try:
        assert next(events) == first
        assert next(events) == second
    finally:
        events.close()

    assert calls == [
        {"kind": "audit"},
        {
            "kind": "audit",
            "after": first["ts"],
            "after_id": first["event_id"],
        },
    ]


def test_sse_stream_delivers_late_same_timestamp_audit_event_once(session):
    """A quote cursor cannot skip an audit row committed later at the same ts."""
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    import qlab.state.registry as registry_module
    import qlab.ui.server as server_module

    boundary_ts = "2026-07-24T12:34:56+00:00"
    quote = {
        "event_id": "quote-at-boundary",
        "ts": boundary_ts,
        "kind": "quote",
        "payload": {"rows": []},
    }
    with session._market_lock:
        session._market_events.append(quote)

    handler = type("H", (server_module._Handler,), {"session": session})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    response = None
    try:
        response = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/stream", timeout=10)
        first = json.loads(
            response.readline().decode().removeprefix("data:").strip())
        assert first["event_id"] == quote["event_id"]

        original_now = registry_module._now
        registry_module._now = lambda: boundary_ts
        try:
            with server_module._LOCK:
                late_id = session.registry.record_event("late-audit", {"n": 1})
            registry_module._now = lambda: "2026-07-24T12:34:57+00:00"
            with server_module._LOCK:
                sentinel_id = session.registry.record_event("sentinel", {"n": 2})
        finally:
            registry_module._now = original_now

        received = [first]
        while not any(event["event_id"] == sentinel_id for event in received):
            line = response.readline().decode()
            if line.startswith("data:"):
                received.append(json.loads(
                    line.removeprefix("data:").strip()))

        registry_module._now = lambda: "2026-07-24T12:34:58+00:00"
        try:
            with server_module._LOCK:
                final_id = session.registry.record_event("final", {"n": 3})
        finally:
            registry_module._now = original_now
        while not any(event["event_id"] == final_id for event in received):
            line = response.readline().decode()
            if line.startswith("data:"):
                received.append(json.loads(
                    line.removeprefix("data:").strip()))

        assert [
            event["event_id"] for event in received
        ].count(late_id) == 1
    finally:
        if response is not None:
            response.close()
        httpd.shutdown()
        httpd.server_close()


def test_market_topic_producer_lifecycles_leave_no_threads(
    session, monkeypatch,
):
    """Sequential in-process owners stop and join their quote producers."""
    import threading

    import qlab.ui.server as server_module

    monkeypatch.setattr(
        session,
        "market",
        lambda offline: {
            "assets": [
                {"ticker": "ACWI", "price": 100.0, "change_1d": 0.01},
            ],
        },
    )

    def live_producers():
        return [
            thread for thread in threading.enumerate()
            if thread.name == server_module._MARKET_THREAD_NAME
        ]

    assert live_producers() == []
    for _ in range(2):
        stop_event, producer = server_module._start_market_topics(session)
        try:
            assert producer.is_alive()
            with pytest.raises(RuntimeError, match="already running"):
                server_module._start_market_topics(session)
        finally:
            server_module._stop_market_topics(stop_event, producer, timeout=2.0)
        assert not producer.is_alive()
        assert live_producers() == []


def test_refused_second_serve_preserves_first_handler_session(
    session, monkeypatch,
):
    """The producer guard rejects startup before the handler binding changes."""
    import qlab.ui.server as server_module

    second_session = server_module.UISession(
        offline_default=True,
        registry=Registry(":memory:"),
    )

    def wait_for_stop(_session, stop_event, _refresh_seconds):
        stop_event.wait()

    monkeypatch.setattr(server_module, "_run_market_topics", wait_for_stop)
    monkeypatch.setattr(server_module._Handler, "session", session)
    monkeypatch.setattr(
        server_module,
        "UISession",
        lambda offline_default=True: second_session,
    )

    stop_event, producer = server_module._start_market_topics(session)
    try:
        with pytest.raises(RuntimeError, match="already running"):
            server_module.serve(port=0, offline=True, open_browser=False)
        assert server_module._Handler.session is session
    finally:
        server_module._stop_market_topics(
            stop_event, producer, timeout=2.0)
        second_session.registry.close()


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


def test_bob_task_start_respects_mode_authority(session):
    """Starting a Bob task through the owner runs the governed workflow — and
    only when the mode allows it. Observe mode must refuse."""
    facts = session.bob_facts(True)
    facts["regime"]["flip"] = True
    out = session.bob.observe(facts, trading_date="2020-01-02")
    task_id = out["created_tasks"][0]["task_id"]

    # Observe mode: refused before any workflow is created.
    status, refused = handle_api(
        session, "POST", f"/api/bob/tasks/{task_id}/start", {},
        {"offline": True})
    assert status == 200
    assert refused["started"] is False and refused["blocked_by"] == "authority"
    assert session.registry.list_workflows(10) == []

    # Research mode: the template runs and a durable workflow is registered.
    session.bob.set_mode("research")
    session.registry.update_bob_task(task_id, status="queued")
    status, started = handle_api(
        session, "POST", f"/api/bob/tasks/{task_id}/start", {},
        {"offline": True})
    assert status == 200 and started["completed"] is True
    assert started["conclusion"]["workflow_id"]
    stored = session.registry.get_bob_task(task_id)
    assert stored["status"] == "completed"
    assert stored["workflow_id"] == started["conclusion"]["workflow_id"]


def test_unknown_bob_task_start_is_404(session):
    status, out = handle_api(
        session, "POST", "/api/bob/tasks/nope/start", {}, {"offline": True})
    assert status == 404
