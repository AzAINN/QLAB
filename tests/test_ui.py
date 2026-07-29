"""The single-page UI's JSON API (dispatch tested in-process, no sockets)."""

from __future__ import annotations

import pytest

from qlab.state.registry import Registry
from qlab.ui import server as ui_server
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


def test_reference_marks_champion_and_reports_absent_ablation(session):
    status, payload = handle_api(session, "GET", "/api/reference", {}, {})
    assert status == 200
    entries = payload["entries"]
    champions = [e for e in entries if e["champion"]]
    assert [e["algorithm_key"] for e in champions] == [
        session.mandate.operational_policy]
    by_id = {e["entry_id"]: e for e in entries}
    assert by_id["b2"]["stage"] == "operational"
    assert by_id["a3t"]["stage"] == "research"
    assert by_id["b2"]["ablation"] is None      # empty registry: explicit absence
    assert by_id["sharpe"]["stage"] is None     # metrics carry no stage


def test_leaderboard_reports_method_names_not_codes(session):
    run_id = session.registry.log_run("ablation", {"note": "test"})
    session.registry.log_backtest(run_id, "B2", {
        "sharpe": 0.91, "ann_return": 0.062, "max_drawdown": -0.124,
        "cvar_95": -0.011, "deflated_sharpe": 0.83})
    session.registry.log_backtest(run_id, "B0", {
        "sharpe": 0.55, "ann_return": 0.050, "max_drawdown": -0.180,
        "cvar_95": -0.015, "deflated_sharpe": 0.60})
    rows = session.leaderboard()
    assert [row["name"] for row in rows] == ["HRP", "60/40"]
    assert rows[0]["champion"] and not rows[1]["champion"]
    assert rows[1]["benchmark"]
    # The reference overlays the same ablation numbers on the arm entries.
    status, payload = handle_api(session, "GET", "/api/reference", {}, {})
    assert status == 200
    by_id = {entry["entry_id"]: entry for entry in payload["entries"]}
    assert by_id["b2"]["ablation"]["sharpe"] == 0.91
    assert by_id["b1"]["ablation"] is None


def test_leaderboard_ignores_newer_non_ablation_backtest_runs(session):
    ablation = session.registry.log_run("ablation", {"note": "staged"})
    session.registry.log_backtest(ablation, "B2", {"sharpe": 0.91})
    # The referee's backtest.run and research_apply_views write the same table
    # under other run kinds, with arm ids that match no curated arm. Newer is
    # not the same as comparable — they must never displace the ablation.
    probe = session.registry.log_run("backtest", {"arm": "min_variance:classical"})
    session.registry.log_backtest(probe, "min_variance:classical", {"sharpe": 2.5})
    views = session.registry.log_run("views", {"note": "pasted excerpt"})
    session.registry.log_backtest(views, "views_probe", {"sharpe": 3.0})

    assert [row["arm_id"] for row in session.leaderboard()] == ["B2"]
    status, payload = handle_api(session, "GET", "/api/reference", {}, {})
    assert status == 200
    by_id = {entry["entry_id"]: entry for entry in payload["entries"]}
    assert by_id["b2"]["ablation"]["sharpe"] == 0.91


def test_leaderboard_is_empty_without_any_ablation_evidence(session):
    session.registry.log_run("backtest", {"arm": "hrp:classical"})
    assert session.leaderboard() == []
    assert session.latest_ablation_metrics() == {}


def test_leaderboard_reads_a_real_ablation_run(session):
    """Bind producer to consumer: run_ablation's run kind is what the filter reads.

    Every other leaderboard test writes ``log_run("ablation", …)`` by hand, so a
    rename inside ``run_ablation`` would leave the suite green and both the
    leaderboard and the reference overlay silently blank.
    """
    from qlab.experiment import ABLATION_RUN_KIND, run_ablation

    spec = {
        "name": "leaderboard-binding",
        "data": {"universe": "core", "start": "2018-01-01", "end": "2020-12-31"},
        "backtest": {"rebalance": "quarterly", "lookback_days": 252},
        "arms": [
            {"id": "B1", "objective": "equal_weight", "solver": "none"},
            {"id": "B0", "objective": "sixty_forty", "solver": "none"},
        ],
    }
    run_ablation(spec, registry=session.registry, offline=True)

    assert session.registry.list_runs(1)[0]["kind"] == ABLATION_RUN_KIND
    rows = session.leaderboard()
    assert {row["arm_id"] for row in rows} == {"B1", "B0"}
    assert {row["name"] for row in rows} == {"Equal weight", "60/40"}
    assert all(row["sharpe"] is not None for row in rows)
    status, payload = handle_api(session, "GET", "/api/reference", {}, {})
    by_id = {entry["entry_id"]: entry for entry in payload["entries"]}
    assert by_id["b1"]["ablation"]["sharpe"] == rows[
        next(i for i, r in enumerate(rows) if r["arm_id"] == "B1")]["sharpe"]


def test_tui_snapshot_carries_the_leaderboard(session):
    run_id = session.registry.log_run("ablation", {"note": "snapshot"})
    session.registry.log_backtest(run_id, "B0", {"sharpe": 0.42})
    status, snap = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})
    assert status == 200
    assert [row["name"] for row in snap["leaderboard"]] == ["60/40"]


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


def test_workflow_control_routes_interrupt_resume_and_abandon(session):
    workflow = session.registry.start_workflow(
        "portfolio_review", {"goal": "lifecycle"})
    workflow_id = workflow["workflow_id"]
    session.registry.update_workflow_phase(
        workflow_id, "analyst", "working")

    status, interrupted = handle_api(
        session,
        "POST",
        f"/api/workflows/{workflow_id}/interrupt",
        {},
        {"reason": "operator stopped it"},
    )
    assert status == 200
    assert interrupted["status"] == "interrupted"
    assert interrupted["steps"][0]["status"] == "interrupted"

    status, fenced = handle_api(
        session,
        "POST",
        "/api/workflows/analyst",
        {},
        {"workflow_id": workflow_id, "status": "working"},
    )
    assert status == 409
    assert "resume it explicitly" in fenced["error"]

    status, resumed = handle_api(
        session,
        "POST",
        f"/api/workflows/{workflow_id}/resume",
        {},
        {},
    )
    assert status == 200 and resumed["status"] == "running"

    status, abandoned = handle_api(
        session,
        "POST",
        f"/api/workflows/{workflow_id}/abandon",
        {},
        {"reason": "obsolete run"},
    )
    assert status == 200 and abandoned["status"] == "abandoned"
    assert all(
        step["status"] == "abandoned"
        for step in abandoned["steps"]
    )

    status, conflict = handle_api(
        session,
        "POST",
        f"/api/workflows/{workflow_id}/resume",
        {},
        {},
    )
    assert status == 409
    assert "cannot be resumed" in conflict["error"]


def test_owner_startup_recovers_live_looking_workflows_as_interrupted():
    registry = Registry(":memory:")
    workflow = registry.start_workflow(
        "portfolio_review", {"goal": "survive owner restart"})
    registry.update_workflow_phase(
        workflow["workflow_id"], "analyst", "working")

    recovered = UISession(offline_default=True, registry=registry)
    row = recovered.registry.get_workflow(workflow["workflow_id"])
    assert row["status"] == "interrupted"
    assert row["steps"][0]["status"] == "interrupted"
    assert "owner runtime restarted" in row["steps"][0]["summary"]
    recovered.registry.close()


def test_owner_reaps_a_workflow_older_than_the_coordinator_lease(session):
    workflow = session.registry.start_workflow(
        "portfolio_review", {"goal": "stale"})
    session.registry.update_workflow_phase(
        workflow["workflow_id"], "analyst", "working")
    session.registry.con.execute(
        "UPDATE workflows SET updated_at=? WHERE workflow_id=?",
        ["2000-01-01T00:00:00+00:00", workflow["workflow_id"]],
    )

    reaped = session.reap_stale_workflows(force=True)
    assert [row["workflow_id"] for row in reaped] == [workflow["workflow_id"]]
    row = session.registry.get_workflow(workflow["workflow_id"])
    assert row["status"] == "interrupted"
    assert "lease expired" in row["steps"][0]["summary"]


def test_serve_refuses_the_port_before_opening_or_recovering_registry(
    monkeypatch,
):
    import qlab.ui.server as server_module

    constructed = []

    def refuse_port(*args, **kwargs):
        raise OSError("address already in use")

    monkeypatch.setattr(server_module, "ThreadingHTTPServer", refuse_port)
    monkeypatch.setattr(
        server_module,
        "UISession",
        lambda **kwargs: constructed.append(kwargs),
    )
    with pytest.raises(OSError, match="address already in use"):
        server_module.serve(port=8765, offline=True, open_browser=False)
    assert constructed == []


def test_lifecycle_client_uses_a_short_deadline(monkeypatch):
    import qlab.tui.client as client_module

    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"workflow_id": "wf1", "status": "interrupted"}

    def post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(client_module.httpx, "post", post)
    result = client_module.ApiClient("http://owner").post_control(
        "/api/workflows/wf1/interrupt", {"reason": "stop"})
    assert result["status"] == "interrupted"
    assert captured["timeout"].read == 5.0
    assert captured["timeout"].connect == 2.0


def test_readiness_probe_uses_a_short_deadline(monkeypatch):
    import qlab.tui.client as client_module

    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ready": True}

    def get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(client_module.httpx, "get", get)
    result = client_module.ApiClient("http://owner").probe(timeout=0.75)

    assert result == {"ready": True}
    assert captured["url"] == "http://owner/readyz"
    assert captured["timeout"].read == 0.75
    assert captured["timeout"].connect == 0.75


def test_owner_stderr_drain_survives_a_chatty_child():
    # A PIPE nobody reads deadlocks the child once the OS buffer (~64 KB)
    # fills, so the drain must run for the child's whole life. This drives a
    # multiple of that through the pipe and asserts the child still exits and
    # the tail keeps the end — where a traceback would be.
    import subprocess
    import sys as _sys

    from qlab.autopilot.cli import _OwnerStderrTail

    child = subprocess.Popen(
        [_sys.executable, "-c",
         "import sys\n"
         "for i in range(20000):\n"
         "    print(f'line {i}', file=sys.stderr)\n"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    tail = _OwnerStderrTail(child)
    assert child.wait(timeout=30) == 0  # deadlocks here without the drain
    assert tail.tail().splitlines()[-1] == "line 19999"


def test_tui_launcher_waits_for_owner_readiness_after_spawn(monkeypatch):
    """A bound port is not enough: the owner may still be opening its state."""
    from types import SimpleNamespace

    import qlab.autopilot.cli as cli_module
    import qlab.tui.app as app_module
    import qlab.tui.client as client_module

    calls = {"probe": 0, "system": 0, "run": 0}

    class ClosedPort:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, _timeout):
            pass

        def connect_ex(self, _address):
            return 1

    class Owner:
        stderr = None

        def poll(self):
            return None

        def terminate(self):
            raise AssertionError("a healthy owner must not be terminated")

    class Client:
        def __init__(self, base_url):
            assert base_url == "http://127.0.0.1:8877"

        def probe(self):
            calls["probe"] += 1
            if calls["probe"] == 1:
                raise ConnectionResetError("owner has bound but is not ready")
            return {"ready": True}

        def get(self, path, **params):
            assert path == "/api/system"
            assert params == {"offline": 1}
            calls["system"] += 1
            return {"mode": "offline"}

    class Tui:
        def __init__(self, client, **kwargs):
            assert isinstance(client, Client)
            assert kwargs["owned_server"].poll() is None
            assert kwargs["offline"] is True
            assert kwargs["claude_start"] == "off"

        def run(self):
            calls["run"] += 1

    monkeypatch.setattr(cli_module.socket, "socket", ClosedPort)
    monkeypatch.setattr(
        cli_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: Owner(),
    )
    monkeypatch.setattr(cli_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(client_module, "ApiClient", Client)
    monkeypatch.setattr(app_module, "QlabTui", Tui)

    result = cli_module._cmd_tui(SimpleNamespace(
        port=8877,
        online=False,
        refresh=0.0,
        claude="off",
    ))

    assert result == 0
    assert calls == {"probe": 2, "system": 1, "run": 1}


def test_model_invocations_route(session):
    from qlab.operator.model_routing import record_invocation, resolve_route

    record_invocation(session.registry, resolve_route("reporter"))
    status, out = handle_api(session, "GET", "/api/models/invocations", {}, {})
    assert status == 200 and out["invocations"][0]["role"] == "reporter"


def test_atlas_status_starts_in_observe(session):
    status, out = handle_api(session, "GET", "/api/atlas/status", {}, {})
    assert status == 200
    assert out["mode"] == "observe"
    assert out["manager_id"] == "atlas"


def test_atlas_observe_tick_returns_state_and_brief(session):
    status, out = handle_api(session, "POST", "/api/atlas/observe", {},
                             {"offline": True})
    assert status == 200
    # Demo data is research-only, so Atlas is not blocked on it; coordinator may be
    # absent in CI -> degraded, else observing. Either way a brief is produced.
    assert out["state"] in ("observing", "degraded")
    assert out["brief"]["book"]["equity"] is not None


def test_post_offline_flag_is_parsed_not_cast(session, monkeypatch):
    # bool("0") is True: a POST asking for LIVE data used to be served synthetic
    # data and a 200, with nothing in the response saying so.
    seen: list[bool] = []
    monkeypatch.setattr(
        session, "atlas_observe", lambda offline: seen.append(offline) or {})

    handle_api(session, "POST", "/api/atlas/observe", {"offline": ["0"]}, {})
    handle_api(session, "POST", "/api/atlas/observe", {}, {"offline": "0"})
    handle_api(session, "POST", "/api/atlas/observe", {}, {"offline": "false"})
    assert seen == [False, False, False]

    seen.clear()
    handle_api(session, "POST", "/api/atlas/observe", {"offline": ["1"]}, {})
    handle_api(session, "POST", "/api/atlas/observe", {}, {"offline": True})
    handle_api(session, "POST", "/api/atlas/observe", {}, {})   # session default
    assert seen == [True, True, True]


def test_atlas_mode_and_pause_resume(session):
    status, out = handle_api(session, "POST", "/api/atlas/mode", {},
                             {"mode": "research"})
    assert status == 200 and out["mode"] == "research"
    status, bad = handle_api(session, "POST", "/api/atlas/mode", {},
                             {"mode": "nonsense"})
    assert status == 400
    status, paused = handle_api(session, "POST", "/api/atlas/pause", {}, {})
    assert paused["mode"] == "paused"
    status, resumed = handle_api(session, "POST", "/api/atlas/resume", {}, {})
    assert resumed["mode"] == "observe"


def test_atlas_message_never_grants_authority(session):
    status, out = handle_api(session, "POST", "/api/atlas/message", {},
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
                "regime": "neutral",
                "regime_summary": "offline synthetic backdrop",
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


def test_owner_news_tool_is_offline_synthetic_and_honest(session):
    """Offline news is deterministic, clearly labelled synthetic, and carries a
    risk tilt and an untrusted-data disclaimer — never fabricated live news."""
    status, obj = handle_api(
        session, "POST", "/api/lab/news.market", {},
        {"as_of": "2022-06-30", "universe": "core", "offline": True, "limit": 4},
    )
    assert status == 200, obj
    news = obj["result"]
    assert news["source"] == "synthetic"          # honest about being offline
    assert 1 <= len(news["headlines"]) <= 4
    assert all("[synthetic]" in h["title"] for h in news["headlines"])
    assert -1.0 <= news["risk_tilt"] <= 1.0
    assert news["tilt_label"] in ("risk_off", "neutral", "risk_on")
    assert "untrusted" in news["disclaimer"].lower()

    # deterministic: the same as_of returns the same read
    status2, obj2 = handle_api(
        session, "POST", "/api/lab/news.market", {},
        {"as_of": "2022-06-30", "universe": "core", "offline": True, "limit": 4},
    )
    assert obj2["result"]["headlines"] == news["headlines"]


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
        _execute_body(session, preview["plan_id"]),
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


def test_a_transport_failure_reconnects_with_the_cursor_intact(monkeypatch):
    # The retry used to live in the caller, which rebuilt the stream from
    # scratch — no `after`, a 25-event primer, and everything past it from the
    # outage silently gone. The stream now heals itself with the exact tuple.
    import json

    import httpx

    import qlab.tui.client as client_module

    first = {"event_id": "event-a", "ts": "2026-07-24T12:34:56+00:00",
             "kind": "audit"}
    second = {"event_id": "event-b", "ts": "2026-07-24T12:35:00+00:00",
              "kind": "audit"}

    class DyingResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            return None

        def iter_lines(self):
            yield f"data: {json.dumps(first)}"
            raise httpx.ReadTimeout("owner went away mid-stream")

    class HealthyResponse(DyingResponse):
        def iter_lines(self):
            yield f"data: {json.dumps(second)}"

    responses = iter([DyingResponse(), HealthyResponse()])
    calls = []

    def fake_stream(method, url, *, params, timeout):
        calls.append(dict(params))
        return next(responses)

    monkeypatch.setattr(client_module.httpx, "stream", fake_stream)
    monkeypatch.setattr(client_module, "STREAM_RETRY_WAIT_S", 0.01)
    events = client_module.ApiClient("http://owner").stream("/api/stream")
    try:
        assert next(events) == first
        assert next(events) == second
    finally:
        events.close()

    assert calls[1]["after"] == first["ts"]
    assert calls[1]["after_id"] == first["event_id"]


def test_a_malformed_frame_is_surfaced_not_fatal_and_not_silent(monkeypatch):
    # A frame that fails to parse used to be either silently discarded (bad
    # JSON) or yielded raw to crash the consumer (valid JSON, wrong shape).
    # Both now surface as one loud stream.malformed event, and the events on
    # either side of the bad frame still arrive.
    import json

    import qlab.tui.client as client_module

    good = {"event_id": "event-a", "ts": "2026-07-24T12:34:56+00:00",
            "kind": "audit"}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            return None

        def iter_lines(self):
            yield "data: {not json"
            yield "data: [1, 2, 3]"
            yield f"data: {json.dumps(good)}"

    monkeypatch.setattr(
        client_module.httpx, "stream",
        lambda method, url, *, params, timeout: Response())
    events = client_module.ApiClient("http://owner").stream("/api/stream")
    try:
        received = [next(events) for _ in range(3)]
    finally:
        events.close()

    assert [e.get("kind") for e in received] == [
        "stream.malformed", "stream.malformed", "audit"]
    assert received[2] == good


def test_the_owner_proves_liveness_before_the_stream_reader_gives_up():
    # These two numbers are a contract across two modules. A stream poll waits
    # this long for the dispatch lock and then pings instead; if that wait ever
    # reached the client's read deadline, every long owner action would cost a
    # reconnect and strand a server thread blocked on the lock.
    from qlab.tui.client import STREAM_READ_TIMEOUT_S
    from qlab.ui.server import _STREAM_LOCK_WAIT_SECONDS

    assert _STREAM_LOCK_WAIT_SECONDS * 2 < STREAM_READ_TIMEOUT_S


def test_api_client_stream_stops_on_heartbeat_after_cancellation(monkeypatch):
    """A quiet SSE connection must release when the Textual app unmounts."""
    import threading

    import qlab.tui.client as client_module

    stop = threading.Event()

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            return None

        def iter_lines(self):
            stop.set()
            yield ": ping"
            raise AssertionError("stream read continued after cancellation")

    monkeypatch.setattr(
        client_module.httpx,
        "stream",
        lambda method, url, *, params, timeout: FakeResponse(),
    )
    events = client_module.ApiClient("http://owner").stream(
        "/api/stream",
        stop_event=stop,
    )

    with pytest.raises(StopIteration):
        next(events)


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
        lambda offline_default=True, desk_mode=None: second_session,
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


def test_atlas_task_start_respects_mode_authority(session):
    """Starting a Atlas task through the owner runs the governed workflow — and
    only when the mode allows it. Observe mode must refuse."""
    facts = session.atlas_facts(True)
    facts["regime"]["flip"] = True
    out = session.atlas.observe(facts, trading_date="2020-01-02")
    task_id = out["created_tasks"][0]["task_id"]

    # Observe mode: refused before any workflow is created.
    status, refused = handle_api(
        session, "POST", f"/api/atlas/tasks/{task_id}/start", {},
        {"offline": True})
    assert status == 200
    assert refused["started"] is False and refused["blocked_by"] == "authority"
    assert session.registry.list_workflows(10) == []

    # Research mode: the template dispatches and a durable workflow is
    # registered. The task stays running -- a workflow row is not a finding, and
    # only the workflow's own terminal state may complete the task.
    session.atlas.set_mode("research")
    session.registry.update_atlas_task(task_id, status="queued")
    status, started = handle_api(
        session, "POST", f"/api/atlas/tasks/{task_id}/start", {},
        {"offline": True})
    assert status == 200
    assert started["started"] is True
    assert started["completed"] is False
    assert started["dispatched"] is True
    assert started["workflow_id"]
    stored = session.registry.get_atlas_task(task_id)
    assert stored["status"] == "running"
    assert stored["workflow_id"] == started["workflow_id"]


def test_unknown_atlas_task_start_is_404(session):
    status, out = handle_api(
        session, "POST", "/api/atlas/tasks/nope/start", {}, {"offline": True})
    assert status == 404


def test_performance_payload_from_synthetic_marks(session):
    equity = 10_000.0
    for day in range(1, 31):
        # Alternating drift: a constant daily return has exactly zero realized
        # vol, which compute_metrics reports as sharpe 0.0 by its zero-vol guard.
        equity *= 1.002 if day % 2 else 1.0005
        session.registry.log_equity_mark(
            f"2026-06-{day:02d}T21:00:00+00:00", equity, cash=500.0,
            source="daily", book="simulated_paper")
    status, payload = handle_api(session, "GET", "/api/performance", {}, {})
    assert status == 200
    assert len(payload["series"]) == 30
    assert payload["metrics"]["sharpe"] > 0
    assert payload["since_start"] > 0
    assert payload["note"] is None


def test_performance_is_honest_about_insufficient_history(session):
    session.registry.log_equity_mark(
        "2026-06-01T21:00:00+00:00", 10_000.0, cash=None, source="daily",
        book="simulated_paper")
    status, payload = handle_api(session, "GET", "/api/performance", {}, {})
    assert status == 200
    assert payload["metrics"] is None
    assert "insufficient" in payload["note"]


def test_reset_discards_the_marks_of_the_discarded_book(session):
    """A reset is a book wipe: its marks must not fabricate a later drawdown.

    Book grows to $10,500, the operator resets to $10,000, the next mark lands
    at $10,000. If the pre-reset marks survive, ``performance`` reads a −4.8%
    daily return that no market produced and feeds it to max_drawdown, cvar_95
    and ann_vol.
    """
    for day in range(1, 6):
        session.registry.log_equity_mark(
            f"2026-06-{day:02d}T21:00:00+00:00", 10_000.0 + 100.0 * day,
            cash=None, source="daily", book="simulated_paper")
    status, reset = handle_api(session, "POST", "/api/reset", {}, {})
    assert (status, reset["reset"]) == (200, True)
    assert session.registry.equity_marks() == []

    session.registry.log_equity_mark(
        "2026-06-06T21:00:00+00:00", 10_000.0, cash=None, source="daily",
        book="simulated_paper")
    _, payload = handle_api(session, "GET", "/api/performance", {}, {})
    assert [row["equity"] for row in payload["series"]] == [10_000.0]
    assert payload["metrics"] is None
    assert payload["since_start"] == 0.0


def test_marks_from_two_books_never_compose_one_return_series(session):
    """A venue switch is a bookkeeping event, not a market move.

    A simulated book near $10k and an Alpaca account at $250k must never share
    one return series; the excluded marks are disclosed, never silently dropped.
    """
    for day in range(1, 6):
        session.registry.log_equity_mark(
            f"2026-06-{day:02d}T21:00:00+00:00", 10_000.0 + 10.0 * day,
            cash=None, source="daily", book="simulated_paper")
    for day in range(6, 11):
        session.registry.log_equity_mark(
            f"2026-06-{day:02d}T21:00:00+00:00", 250_000.0, cash=None,
            source="alpaca_backfill", book="alpaca_paper")

    status, payload = handle_api(session, "GET", "/api/performance", {}, {})
    assert status == 200
    assert payload["book"] == "simulated_paper"
    assert [row["equity"] for row in payload["series"]] == [
        10_010.0, 10_020.0, 10_030.0, 10_040.0, 10_050.0]
    assert payload["marks"] == 5
    assert payload["excluded_marks"] == 5
    assert "another book" in payload["note"]
    # The 25x step between books would dominate realized vol; separated, the
    # simulated book's own vol is a rounding error.
    assert payload["metrics"]["ann_vol"] < 0.01


def test_realized_metrics_annualize_on_the_observed_cadence(session):
    """Weekly marks must annualize at ~52/yr, not at the 252-day default.

    Fifteen weekly marks take the book from $10,000 to $10,500 over exactly 98
    days, so the only defensible annualized return is the compound rate over
    that span: 1.05 ** (365.25 / 98) - 1 ≈ 19.9%. At the silent 252 default the
    same series reports 1.05 ** (252 / 14) - 1 ≈ 141%.
    """
    from datetime import datetime, timedelta, timezone

    first = datetime(2026, 3, 2, 21, 0, tzinfo=timezone.utc)
    for step in range(15):
        # Path-independent endpoints with a non-zero step-to-step wobble, so
        # realized vol is real while the total return stays exactly +5%.
        equity = 10_000.0 * (1.05 ** (step / 14)) * (1.002 if step % 2 else 1.0)
        session.registry.log_equity_mark(
            (first + timedelta(days=7 * step)).isoformat(), equity,
            cash=None, source="daily", book="simulated_paper")

    status, payload = handle_api(session, "GET", "/api/performance", {}, {})
    assert status == 200
    cadence = payload["cadence"]
    assert cadence["observed_span_days"] == pytest.approx(98.0)
    assert cadence["mean_step_days"] == pytest.approx(7.0)
    assert cadence["periods_per_year"] == pytest.approx(52.1786, abs=1e-3)
    assert payload["metrics"]["n_obs"] == 14
    assert payload["metrics"]["ann_return"] == pytest.approx(0.19943, abs=1e-4)
    # The 252-day assumption would have claimed a ~141% annual return.
    assert payload["metrics"]["ann_return"] < 0.25


def test_capped_mark_history_cannot_masquerade_as_complete(session, monkeypatch):
    """The newest-N window is a cap, not the whole book — the payload says so."""
    from qlab.ui import server as server_module

    monkeypatch.setattr(server_module, "_MARK_WINDOW", 3)
    for day in range(1, 6):
        session.registry.log_equity_mark(
            f"2026-06-{day:02d}T21:00:00+00:00", 10_000.0 + 10.0 * day,
            cash=None, source="daily", book="simulated_paper")

    status, payload = handle_api(session, "GET", "/api/performance", {}, {})
    assert status == 200
    assert payload["marks"] == 3
    assert payload["marks_total"] == 5
    assert payload["marks_capped"] is True
    assert payload["mark_limit"] == 3
    assert "capped" in payload["note"]
    assert [row["equity"] for row in payload["series"]] == [
        10_030.0, 10_040.0, 10_050.0]


def test_window_change_is_measured_over_the_charted_window(session):
    """The percentage a client renders beside the chart comes from that chart."""
    from datetime import datetime, timedelta, timezone

    first = datetime(2024, 1, 1, 21, 0, tzinfo=timezone.utc)
    for step in range(400):
        session.registry.log_equity_mark(
            (first + timedelta(days=step)).isoformat(), 10_000.0 + step,
            cash=None, source="daily", book="simulated_paper")

    _, payload = handle_api(session, "GET", "/api/performance", {}, {})
    series = payload["series"]
    assert len(series) == 365
    assert payload["window_change"] == pytest.approx(
        series[-1]["equity"] / series[0]["equity"] - 1.0)
    # The full history rose further than the charted window: two different
    # numbers that must never share one label.
    assert payload["since_start"] > payload["window_change"]


def test_backfill_refuses_without_history_capable_broker(session):
    status, payload = handle_api(
        session, "POST", "/api/performance/backfill", {}, {})
    assert status == 400
    assert "portfolio history" in payload["error"]


def test_backfill_merges_alpaca_history_idempotently(session, monkeypatch):
    class StubBroker:
        name = "alpaca_paper"

        def portfolio_history(self):
            return [
                {"ts": "2026-06-01T20:00:00+00:00", "equity": 10_000.0},
                {"ts": "2026-06-02T20:00:00+00:00", "equity": 10_050.0},
            ]

    monkeypatch.setattr(
        "qlab.trader.broker.get_broker", lambda *args, **kwargs: StubBroker())
    status, payload = handle_api(
        session, "POST", "/api/performance/backfill", {}, {})
    assert (status, payload["backfilled"]) == (200, 2)
    status, payload = handle_api(
        session, "POST", "/api/performance/backfill", {}, {})
    assert payload["backfilled"] == 0


def _checked_plan(session, tilt: float = 0.0) -> str:
    """A referee-PASSed persisted checked plan — the only executable shape.

    Plans are content-addressed, so identical targets yield the same plan_id;
    `tilt` perturbs them when a test needs a genuinely different plan.
    """
    from datetime import date

    from qlab.core.types import Decision

    tickers = session.mandate.universe_whitelist
    even = 1.0 / len(tickers)
    targets = {ticker: even for ticker in tickers}
    if tilt:
        first, last = tickers[0], tickers[-1]
        targets[first] = even + tilt
        targets[last] = even - tilt
    decision_id = session.registry.log_decision(Decision(
        as_of=date.today(), kind="rebalance_gate",
        choice={"targets": targets}, rationale="configured HRP policy",
    ))
    session.registry.log_verdict(
        decision_id, "PASS", ["within mandate"], source="referee-agent",
        targets=targets)
    _, preview = handle_api(
        session, "POST", "/api/rebalance_preview", {},
        {"offline": True, "decision_id": decision_id, "targets": targets})
    assert preview["accepted"] is True
    return preview["plan_id"]


def _approve(session, plan_id: str) -> str:
    """The human decision, recorded — execution consumes this, not a boolean."""
    _, created = handle_api(
        session, "POST", "/api/approvals", {},
        {"plan_id": plan_id, "offline": True})
    approval_id = created["approval_id"]
    handle_api(
        session, "POST", f"/api/approvals/{approval_id}/approve", {}, {})
    return approval_id


def _execute_body(session, plan_id: str) -> dict:
    return {"offline": True, "plan_id": plan_id, "human_confirmed": True,
            "approval_id": _approve(session, plan_id)}


def test_a_get_with_a_body_cannot_smuggle_a_second_request(session):
    # Under HTTP/1.1 keep-alive an unconsumed GET body stayed in rfile and the
    # next request on that connection was parsed out of it — so a client could
    # append a reset to a harmless read and have the owner run it.
    import io

    class _Handler(ui_server._Handler):
        def __init__(self, raw: bytes):
            self.rfile = io.BytesIO(raw)
            self.wfile = io.BytesIO()
            self.headers = {"Content-Length": str(len(raw))}
            self.close_connection = False
            self.responses: list[tuple[int, dict]] = []

        def _json(self, status, obj):
            self.responses.append((status, obj))

    smuggled = b"POST /api/reset HTTP/1.1\r\n\r\n"
    handler = _Handler(smuggled)
    assert handler._drain_request_body() is True
    # The body was consumed, so nothing is left to be read as a new request.
    assert handler.rfile.read() == b""

    negative = _Handler(b"")
    negative.headers = {"Content-Length": "-1"}
    assert negative._drain_request_body() is False
    assert negative.responses[0][0] == 400
    assert negative.close_connection is True


def test_a_malformed_mcp_config_is_not_reported_as_absent(session, tmp_path,
                                                          monkeypatch):
    # A file that exists but does not parse is a different fact from no file.
    # Reporting both as "not configured" sent the operator to re-add a server
    # entry that was already there, with the parse error surfaced nowhere.
    monkeypatch.setattr(ui_server, "workspace_root", lambda: tmp_path)
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {"qlab": {},}}')

    status = session.system_status(offline=True)
    assert status["mcp_configured"] is False
    # It names the fault rather than merely flagging one.
    assert "JSONDecodeError" in status["mcp_config_error"]
    assert "trailing comma" in status["mcp_config_error"]


def test_reset_refuses_the_alpaca_book_and_spares_its_history(session):
    # A reset discards qlab's own book; it cannot discard an Alpaca account.
    # Wiping only the local marks would leave the recorded history disagreeing
    # with the untouched real account.
    from qlab.core.desk_mode import DeskMode

    session.registry.log_equity_mark(
        "2026-06-01T21:00:00+00:00", 98_000.0, cash=1_000.0,
        source="alpaca_backfill", book="alpaca_paper")
    session.set_desk_mode(DeskMode("live", "alpaca"))

    status, result = handle_api(session, "POST", "/api/reset", {}, {})
    assert status == 400 and "cannot be reset" in result["error"]
    assert session.registry.equity_marks(book="alpaca_paper") != []


def test_resetting_the_simulated_book_spares_the_alpaca_history(session):
    session.registry.log_equity_mark(
        "2026-06-01T21:00:00+00:00", 98_000.0, cash=1_000.0,
        source="alpaca_backfill", book="alpaca_paper")
    assert session.desk_mode.book == "simulated"

    status, result = handle_api(session, "POST", "/api/reset", {}, {})
    assert (status, result["reset"]) == (200, True)
    assert [m["equity"] for m in
            session.registry.equity_marks(book="alpaca_paper")] == [98_000.0]


def test_a_consumed_approval_cannot_be_revived_and_spent_again(session):
    # The challenge route wrote "pending" over whatever status it found, so a
    # spent approval could be re-opened, re-approved, and used to authorise a
    # second fill against the same human decision.
    plan_id = _checked_plan(session)
    approval_id = _approve(session, plan_id)
    status, result = handle_api(
        session, "POST", "/api/plans/execute", {},
        {"offline": True, "plan_id": plan_id, "human_confirmed": True,
         "approval_id": approval_id})
    assert (status, result["executed"]) == (200, True)
    assert session.registry.get_approval_request(approval_id)["status"] == (
        "consumed")

    status, _ = handle_api(
        session, "POST", f"/api/approvals/{approval_id}/challenge", {},
        {"challenge": "let me have another go"})
    assert status == 400
    assert session.registry.get_approval_request(approval_id)["status"] == (
        "consumed")


def test_a_rejection_and_an_expiry_are_both_durable(session):
    from qlab.state.registry import Registry  # noqa: F401  (documents the writer)

    rejected_plan = _checked_plan(session)
    _, created = handle_api(
        session, "POST", "/api/approvals", {},
        {"plan_id": rejected_plan, "offline": True})
    rejected = created["approval_id"]
    handle_api(session, "POST", f"/api/approvals/{rejected}/reject", {}, {})

    # A rejected decision must not be re-openable into an approvable state.
    status, _ = handle_api(
        session, "POST", f"/api/approvals/{rejected}/challenge", {},
        {"challenge": "reconsider"})
    assert status == 400
    assert session.registry.get_approval_request(rejected)["status"] == "rejected"

    # And an expiry is equally terminal.
    expired_plan = _checked_plan(session, tilt=0.03)
    _, created = handle_api(
        session, "POST", "/api/approvals", {},
        {"plan_id": expired_plan, "offline": True})
    expired = created["approval_id"]
    session.registry.expire_due_approvals("2999-01-01T00:00:00+00:00")
    assert session.registry.get_approval_request(expired)["status"] == "expired"
    with pytest.raises(PermissionError):
        session.registry.transition_approval(expired, "approved")


def test_transitioning_an_unknown_approval_is_refused_not_ignored(session):
    # An unguarded UPDATE matched zero rows and reported success, so the
    # challenge route answered 200 with a digest for an approval that does not
    # exist.
    with pytest.raises(KeyError):
        session.registry.transition_approval("no-such-approval", "approved")
    status, _ = handle_api(
        session, "POST", "/api/approvals/no-such-approval/challenge", {},
        {"challenge": "ghost"})
    assert status == 404


def test_a_bare_human_confirmed_flag_cannot_book_a_trade(session):
    # human_confirmed is a boolean in a request body — self-attestation any
    # local process can send. It used to be the whole gate on this route, so
    # one unauthenticated POST filled legs and the audit trail recorded it as
    # a human decision. The approval record is what authorises now.
    plan_id = _checked_plan(session)
    status, result = handle_api(
        session, "POST", "/api/plans/execute", {},
        {"offline": True, "plan_id": plan_id, "human_confirmed": True})
    assert status == 400
    assert "approval" in result["error"]
    assert [row for row in session.registry.equity_marks()
            if row["source"] == "execution"] == []
    assert session.registry.get_plan(plan_id)["state"] == "checked"


def test_an_approval_for_a_different_plan_cannot_execute_this_one(session):
    # The approval binds a specific plan digest and targets hash, so holding
    # *an* approval is not holding one for this plan.
    mine = _checked_plan(session)
    other_approval = _approve(session, _checked_plan(session, tilt=0.02))
    status, result = handle_api(
        session, "POST", "/api/plans/execute", {},
        {"offline": True, "plan_id": mine, "human_confirmed": True,
         "approval_id": other_approval})
    assert (status, result["executed"]) == (200, False)
    assert result["blocked_by"] == "approval"


def test_rejected_execution_writes_no_execution_mark(session):
    """An "execution" mark asserts a fill happened; a refused plan forges none."""
    plan_id = _checked_plan(session)
    # Approve while the plan is still checked, then refuse it: the point is
    # that a refused plan forges no mark even with a valid human decision
    # behind it, not that an approval cannot be created for a refused plan.
    body = _execute_body(session, plan_id)
    session.registry.set_plan_state(plan_id, "refused")
    status, result = handle_api(
        session, "POST", "/api/plans/execute", {}, body)
    assert (status, result["executed"]) == (200, False)
    assert "mandate_violation" in result
    assert [row for row in session.registry.equity_marks()
            if row["source"] == "execution"] == []


def test_failed_mark_never_masks_a_completed_execution(session, monkeypatch):
    """The legs already filled: a broker hiccup fails into the audit bus."""
    plan_id = _checked_plan(session)

    def unavailable(offline):
        raise RuntimeError("alpaca account read failed")

    monkeypatch.setattr(session, "portfolio", unavailable)
    status, result = handle_api(
        session, "POST", "/api/plans/execute", {},
        _execute_body(session, plan_id))
    assert (status, result["executed"]) == (200, True)
    failures = [event for event in session.registry.read_events(100)
                if event["kind"] == "equity_mark_failed"]
    assert [event["payload"]["source"] for event in failures] == ["execution"]
    assert "alpaca account read failed" in failures[0]["payload"]["error"]


def test_daily_ops_records_a_daily_equity_mark(session):
    """The Book view promises daily ops marks the book; pin the hook itself."""
    status, summary = handle_api(
        session, "POST", "/api/daily_ops", {}, {"offline": True})
    assert status == 200
    assert "rebalance_recommended" in summary
    marks = [row for row in session.registry.equity_marks()
             if row["source"] == "daily"]
    assert len(marks) == 1
    assert marks[0]["book"] == "simulated_paper"
    assert marks[0]["equity"] > 0.0


def test_successful_execution_records_an_execution_mark(session):
    """The mark hook on a real fill, asserted on success rather than on failure."""
    plan_id = _checked_plan(session)
    status, result = handle_api(
        session, "POST", "/api/plans/execute", {},
        _execute_body(session, plan_id))
    assert (status, result["executed"]) == (200, True)
    marks = [row for row in session.registry.equity_marks()
             if row["source"] == "execution"]
    assert len(marks) == 1
    assert marks[0]["book"] == "simulated_paper"
    assert marks[0]["equity"] > 0.0


def test_snapshot_poll_marks_are_throttled_to_one_an_hour(session):
    """The 2s TUI refresh must not turn the marks table into 43k rows a day."""
    first = session.tui_snapshot(offline=True, event_limit=10)
    poll_marks = [row for row in session.registry.equity_marks()
                  if row["source"] == "poll"]
    assert len(poll_marks) == 1
    assert first["performance"]["marks"] == 1
    session.tui_snapshot(offline=True, event_limit=10)
    assert len([row for row in session.registry.equity_marks()
                if row["source"] == "poll"]) == 1


# -- the explicit desk mode ------------------------------------------------
def test_desk_mode_defaults_to_synthetic_and_is_reported(session):
    status, payload = handle_api(session, "GET", "/api/desk_mode", {}, {})
    assert status == 200
    assert (payload["data"], payload["book"]) == ("synthetic", "simulated")
    assert payload["label"] == "SYNTHETIC"
    assert "credentials" in payload          # description string, never a secret


def test_setting_the_desk_mode_switches_the_book(session, monkeypatch):
    from qlab.core import data as market
    from qlab.trader import broker as broker_mod
    from qlab.trader.alpaca_auth import AlpacaCredentials
    monkeypatch.setattr(
        broker_mod, "resolve_alpaca_credentials",
        lambda: AlpacaCredentials("oauth", None, None, "tok", "paper", "/x"))
    # A live-data desk prices off the network, which this suite never touches:
    # pin the provider and serve its one fetch seam from the synthetic feed.
    monkeypatch.setenv("QLAB_DATA_PROVIDER", "yfinance")
    monkeypatch.setattr(market, "_fetch_yfinance", market.synthetic_prices)

    status, payload = handle_api(
        session, "POST", "/api/desk_mode", {},
        {"data": "live", "book": "simulated"})
    assert status == 200 and payload["label"] == "LIVE · SIM BOOK"
    # The simulated book is honoured even though a credential is discoverable.
    assert session.portfolio(offline=False)["broker"] == "simulated_paper"
    assert session.current_book(offline=False) == "simulated_paper"


def test_a_persisted_live_desk_owns_the_default_data_lane():
    """``offline_default`` must not be a second, contradictable answer.

    Bare ``qlab ui`` computes ``offline=True`` from its flags while the session
    loads a persisted live mode from disk. Serving synthetic data under a real
    book is ``synthetic`` + ``alpaca`` — the state ``DeskMode`` forbids —
    rebuilt at runtime out of two independent fields.
    """
    from qlab.core.desk_mode import DeskMode, save_desk_mode

    save_desk_mode(DeskMode("live", "alpaca"))
    session = UISession(offline_default=True, registry=Registry(":memory:"))
    assert session.desk_mode == DeskMode("live", "alpaca")
    assert session.offline_default is False
    assert session.lab_state.offline is False


def test_retuning_the_desk_mode_moves_the_default_data_lane(session):
    """The TUI's only path: an owner spawned with no flags, then POSTed into."""
    from qlab.core.desk_mode import DeskMode

    assert session.offline_default is True
    status, _payload = handle_api(
        session, "POST", "/api/desk_mode", {},
        {"data": "live", "book": "simulated"})
    assert status == 200
    assert session.offline_default is False
    assert session.lab_state.offline is False

    session.set_desk_mode(DeskMode("synthetic", "simulated"))
    assert session.offline_default is True
    assert session.lab_state.offline is True


def _write_unreadable_profile(tmp_path, secret: str) -> None:
    """An Alpaca CLI config dir whose active profile cannot be decoded."""
    (tmp_path / "profiles").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.yaml").write_text(
        "default_profile: paper\n", encoding="utf-8")
    (tmp_path / "profiles" / "paper.yaml").write_bytes(
        f"access_token: {secret}\n".encode("utf-8") + b"\xff\xfe\n")


def test_a_broken_profile_never_reaches_a_response_body(
    session, tmp_path, monkeypatch,
):
    """The always-running escape route for a credential read failure.

    ``desk_mode_payload`` catches only ``AlpacaAuthError``; anything else
    propagates into the handler's ``{"error": repr(exc)}`` and is served on
    every two-second poll of ``/api/tui``.
    """
    secret = "tok-abcdefghijklmnopqrstuvwxyz012345"
    _write_unreadable_profile(tmp_path, secret)
    monkeypatch.setenv("ALPACA_CONFIG_DIR", str(tmp_path))

    status, payload = handle_api(session, "GET", "/api/desk_mode", {}, {})
    assert status == 200
    assert payload["credentials_ok"] is False
    assert secret not in repr(payload)

    status, snap = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})
    assert status == 200
    assert secret not in repr(snap)


def test_an_impossible_desk_mode_is_refused(session):
    status, payload = handle_api(
        session, "POST", "/api/desk_mode", {},
        {"data": "synthetic", "book": "alpaca"})
    assert status == 400
    assert "synthetic" in payload["error"]


@pytest.mark.parametrize(
    "path, body",
    [("/api/daily_ops", {"offline": True}),
     ("/api/run_once", {"offline": True, "execute": False})],
)
def test_autopilot_routes_run_the_book_the_desk_mode_chose(
    session, monkeypatch, path, body,
):
    """`: daily ops` and `: rebalance dry` must not silently use the simulator.

    Both write the registry the Alpaca book is executed against: daily_ops
    evaluates the drawdown kill switch and latches the halt, run_once builds the
    checked plan ``execute_checked_plan`` later fills against Alpaca. A book that
    disagrees with the desk mode makes both of those numbers the wrong book's.
    """
    from qlab.autopilot import loop
    from qlab.core.desk_mode import DeskMode
    from qlab.trader.broker import get_broker as real_get_broker

    session.set_desk_mode(DeskMode("live", "alpaca"))
    requested: list[str | None] = []

    def recording_get_broker(registry, **kwargs):
        # Assert on the requested book, never on a live call: the substitute is
        # always the simulator so the suite stays offline and account-free.
        requested.append(kwargs.get("book"))
        return real_get_broker(registry, **{**kwargs, "book": "simulated"})

    monkeypatch.setattr(loop, "get_broker", recording_get_broker)

    status, _payload = handle_api(session, "POST", path, {}, body)
    assert status == 200
    assert requested == ["alpaca"]


def test_run_once_and_daily_ops_clamp_offline_to_desk_mode_on_the_alpaca_book(
    session, monkeypatch,
):
    """The data lane can never contradict the book.

    The bundled web dashboard's offline checkbox re-defaults to True on every
    page load, so on a live/alpaca desk a body of ``{"offline": true}`` must
    not reach either route as-is: honouring it would run synthetic moments
    into a referee verdict and a cost-gated plan (``run_once``), or evaluate
    the drawdown kill switch on synthetic data while latching it against the
    real book (``daily_ops``) — exactly the pairing ``DeskMode`` forbids.
    """
    from qlab.autopilot import loop
    from qlab.core.desk_mode import DeskMode

    session.set_desk_mode(DeskMode("live", "alpaca"))
    calls: dict[str, dict] = {}

    def fake_run_once(**kwargs):
        calls["run_once"] = kwargs
        return {"rebalance_recommended": False}

    def fake_daily_ops(**kwargs):
        calls["daily_ops"] = kwargs
        return {"rebalance_recommended": False}

    monkeypatch.setattr(loop, "run_once", fake_run_once)
    monkeypatch.setattr(loop, "daily_ops", fake_daily_ops)

    status, _ = handle_api(
        session, "POST", "/api/run_once", {}, {"offline": True, "execute": False})
    assert status == 200
    assert calls["run_once"]["offline"] is False
    assert calls["run_once"]["book"] == "alpaca"

    status, _ = handle_api(session, "POST", "/api/daily_ops", {}, {"offline": True})
    assert status == 200
    assert calls["daily_ops"]["offline"] is False
    assert calls["daily_ops"]["book"] == "alpaca"


def test_run_once_and_daily_ops_still_honor_body_offline_on_the_simulated_book(
    session, monkeypatch,
):
    """The clamp is narrow: a non-Alpaca desk keeps the operator's own flag."""
    from qlab.autopilot import loop

    assert session.desk_mode.book == "simulated"  # default fixture desk mode
    calls: dict[str, dict] = {}

    def fake_run_once(**kwargs):
        calls["run_once"] = kwargs
        return {"rebalance_recommended": False}

    def fake_daily_ops(**kwargs):
        calls["daily_ops"] = kwargs
        return {"rebalance_recommended": False}

    monkeypatch.setattr(loop, "run_once", fake_run_once)
    monkeypatch.setattr(loop, "daily_ops", fake_daily_ops)

    status, _ = handle_api(
        session, "POST", "/api/run_once", {}, {"offline": False, "execute": False})
    assert status == 200
    assert calls["run_once"]["offline"] is False
    assert calls["run_once"]["book"] == "simulated"

    status, _ = handle_api(session, "POST", "/api/daily_ops", {}, {"offline": False})
    assert status == 200
    assert calls["daily_ops"]["offline"] is False
    assert calls["daily_ops"]["book"] == "simulated"

    # Both directions, so the clamp cannot degenerate into "always non-offline":
    # the operator's own True must survive on a book that is not the real one.
    status, _ = handle_api(
        session, "POST", "/api/run_once", {}, {"offline": True, "execute": False})
    assert status == 200
    assert calls["run_once"]["offline"] is True

    status, _ = handle_api(session, "POST", "/api/daily_ops", {}, {"offline": True})
    assert status == 200
    assert calls["daily_ops"]["offline"] is True


def test_the_daily_ops_equity_mark_uses_the_clamped_data_lane(session, monkeypatch):
    """One route must not hold two answers about its own data lane.

    ``_mark_after_mutation`` was still handed the raw body flag while the loop
    beside it got the clamped one. On the Alpaca book the persisted numbers come
    from the account either way, so nothing wrong was written — but the hook
    priced a live desk's portfolio off the synthetic feed to get there, and any
    future mark that reads ``state["marks"]`` would inherit the contradiction.
    """
    from qlab.autopilot import loop
    from qlab.core.desk_mode import DeskMode

    session.set_desk_mode(DeskMode("live", "alpaca"))
    monkeypatch.setattr(loop, "daily_ops", lambda **kwargs: {"kind": "daily_ops"})
    marked: list[bool] = []
    monkeypatch.setattr(
        session, "record_equity_mark",
        lambda source, offline: marked.append(offline))

    status, _payload = handle_api(
        session, "POST", "/api/daily_ops", {}, {"offline": True})
    assert status == 200
    assert marked == [False]


def test_plan_execution_clamps_offline_to_desk_mode_on_the_alpaca_book(
    session, monkeypatch,
):
    """Plan execution cannot contradict the book either.

    A body of ``{"offline": true}`` must not reach the execution path as-is on
    a live/alpaca desk: honouring it would run the P3 execution-time
    data-revalidation gate under a demo policy (never execution-eligible)
    while ``get_broker`` still fills against the real Alpaca account — exactly
    the contradiction ``DeskMode`` forbids.
    """
    from qlab.core.desk_mode import DeskMode

    session.set_desk_mode(DeskMode("live", "alpaca"))
    calls: dict[str, tuple] = {}

    def fake_execute(plan_id, body, offline):
        calls["execute"] = (body, offline)
        return {"executed": False}

    monkeypatch.setattr(session, "execute_plan_with_approval", fake_execute)

    status, _ = handle_api(
        session, "POST", "/api/plans/execute", {},
        {"offline": True, "plan_id": "whatever", "human_confirmed": True,
         "approval_id": "whatever"})
    assert status == 200
    assert calls["execute"][1] is False


def test_plan_execution_still_honors_body_offline_on_the_simulated_book(
    session, monkeypatch,
):
    """The clamp is narrow: a non-Alpaca desk keeps the operator's own flag."""
    assert session.desk_mode.book == "simulated"  # default fixture desk mode
    calls: dict[str, tuple] = {}

    def fake_execute(plan_id, body, offline):
        calls["execute"] = (body, offline)
        return {"executed": False}

    monkeypatch.setattr(session, "execute_plan_with_approval", fake_execute)

    status, _ = handle_api(
        session, "POST", "/api/plans/execute", {},
        {"offline": True, "plan_id": "whatever", "human_confirmed": True,
         "approval_id": "whatever"})
    assert status == 200
    assert calls["execute"][1] is True


@pytest.mark.parametrize(
    "data, book",
    [("synthetic", "simulated"), ("live", "simulated"), ("live", "alpaca")])
def test_the_startup_banner_survives_a_non_utf8_console(data, book):
    """The owner's startup line must encode in any locale codepage.

    ``qlab tui`` spawns the owner with ``stdout=subprocess.DEVNULL``, so CPython
    encodes this line with the locale ANSI codepage under ``errors='strict'``
    rather than UTF-8. U+00B7 — carried by the live mode labels — has no mapping
    in cp932 (Japanese) or cp874 (Thai). The print sits after the port bind and
    before ``serve_forever()``, so an encode error there takes the owner down on
    every launch for those locales, which is exactly the desk mode this branch
    exists to serve. ASCII is the tightest pin: it encodes in every codepage.
    """
    from qlab.core.desk_mode import DeskMode
    from qlab.ui.server import _startup_banner

    banner = _startup_banner(DeskMode(data, book), "http://127.0.0.1:8765/")
    banner.encode("ascii")
    for codepage in ("cp932", "cp874", "cp1252"):
        banner.encode(codepage)
    # It still has to say the two things the operator reads it for.
    assert "127.0.0.1:8765" in banner
    assert data in banner and book in banner


def test_tui_snapshot_carries_the_desk_mode(session):
    status, snap = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})
    assert status == 200
    assert snap["desk_mode"]["label"] == "SYNTHETIC"

def test_autonomy_is_a_runtime_toggle_that_never_widens_authority(session):
    """The UI switch removes the button press, not the boundary."""
    session.atlas.set_mode("observe")
    status, out = handle_api(session, "POST", "/api/atlas/autonomy", {},
                             {"enabled": True})
    assert status == 200 and out["autonomous"] is True
    # Enabled, but Observe mode still starts nothing — said plainly.
    assert "starts no workflows" in out["effect"]

    session.atlas.set_mode("research")
    _, out = handle_api(session, "POST", "/api/atlas/autonomy", {},
                        {"enabled": True})
    assert "on each heartbeat" in out["effect"]

    _, out = handle_api(session, "POST", "/api/atlas/autonomy", {},
                        {"enabled": False})
    assert out["autonomous"] is False
    assert "wait for you" in out["effect"]


def test_autonomy_rejects_a_non_boolean(session):
    status, out = handle_api(session, "POST", "/api/atlas/autonomy", {},
                             {"enabled": "yes"})
    assert status == 400 and "true or false" in out["error"]


def test_autonomy_state_reaches_the_tui_snapshot(session):
    handle_api(session, "POST", "/api/atlas/autonomy", {}, {"enabled": True})
    status, snap = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})
    assert status == 200
    assert snap["atlas_heartbeat"]["autonomous"] is True


def test_news_read_template_refuses_an_empty_window(session):
    """The analyst interprets a window it is handed; with nothing to read it
    must refuse rather than narrate silence."""
    from qlab.operator.templates import TemplateNotAllowed, check_startable

    facts = session.atlas_facts(True)
    facts["news_window_items"] = 0
    with pytest.raises(TemplateNotAllowed, match="nothing to interpret"):
        check_startable("news_read", "research", facts)

    facts["news_window_items"] = 5
    assert check_startable("news_read", "research", facts).template_id == "news_read"


# --- Atlas dispatch honesty at the owner boundary (P1) -----------------------

def test_atlas_workflow_runner_dispatches_instead_of_concluding():
    # The runner starts a durable workflow. That is a dispatch, not a finding,
    # and it must be reported as one or Atlas claims research it never did.
    from qlab.operator.atlas import Dispatched

    session = UISession(offline_default=True, registry=Registry(":memory:"))
    task = {"task_id": "t-1", "trigger_kind": "regime_flip"}

    outcome = session.atlas_workflow_runner(task, "regime_review")

    assert isinstance(outcome, Dispatched)
    assert outcome.workflow_id
    assert session.registry.get_workflow(outcome.workflow_id) is not None


def test_a_deterministic_template_still_concludes_inline():
    # desk_brief needs no coordinator; it genuinely finishes during the runner.
    from qlab.operator.atlas import Dispatched

    session = UISession(offline_default=True, registry=Registry(":memory:"))
    outcome = session.atlas_workflow_runner(
        {"task_id": "t-1", "trigger_kind": "daily_open"}, "desk_brief")

    assert not isinstance(outcome, Dispatched)
    assert outcome["action_taken"] is False
    assert "brief" in outcome


def test_the_runner_fails_loud_when_no_workflow_could_be_started(monkeypatch):
    # Silently returning a handle with workflow_id=None is how a dispatch
    # failure previously became a completed task.
    session = UISession(offline_default=True, registry=Registry(":memory:"))
    monkeypatch.setattr(session, "start_workflow",
                        lambda request, **kwargs: {})

    with pytest.raises(RuntimeError, match="workflow"):
        session.atlas_workflow_runner(
            {"task_id": "t-1", "trigger_kind": "regime_flip"}, "regime_review")


def test_an_autonomous_start_is_never_reported_as_completed():
    session = UISession(offline_default=True, registry=Registry(":memory:"))
    session.atlas.set_mode("research")
    facts = session.atlas_facts(True)
    facts["regime"]["flip"] = True
    session.atlas.observe(facts, trading_date="2026-07-26")

    started = session.atlas_run_startable(True, limit=1)

    assert started, "expected one startable task"
    for entry in started:
        assert entry.get("completed") is not True
    for task in session.registry.list_atlas_tasks(limit=10):
        if task["status"] == "completed":
            assert task["conclusion"].get("workflow_status") == "complete", (
                "a task completed without its workflow reaching terminal state")


def test_the_observe_tick_reconciles_dispatched_tasks():
    # A workflow that finished while nothing was watching must still resolve its
    # task; reconciliation on the observe cycle is what makes that true.
    from qlab.operator.atlas import Dispatched

    session = UISession(offline_default=True, registry=Registry(":memory:"))
    registry = session.registry
    session.atlas.set_mode("research")
    facts = session.atlas_facts(True)
    facts["regime"]["flip"] = True
    created = session.atlas.observe(facts, trading_date="2026-07-26")
    task_id = created["created_tasks"][0]["task_id"]

    workflow = registry.start_workflow(
        "portfolio_review", {"goal": "g"}, phases=("analyst",))
    session.atlas.start_task(
        task_id, facts, runner=lambda t, tid: Dispatched(workflow["workflow_id"]))
    assert registry.get_atlas_task(task_id)["status"] == "running"

    registry.update_workflow_phase(
        workflow["workflow_id"], "analyst", "done", summary="ok",
        artifacts={"moment_set_id": "m", "objective_id": "o",
                   "decision_id": "d", "regime": "calm",
                   "regime_summary": "quiet"})

    session.atlas_observe(True)

    assert registry.get_atlas_task(task_id)["status"] == "completed"


def test_owner_startup_reconciles_a_workflow_that_finished_while_it_was_down():
    # A dispatched workflow can reach terminal state while no owner is running.
    # Without startup reconciliation the task would sit running until the next
    # observe tick -- and a task that outlives its workflow is exactly the state
    # that used to be misreported.
    from qlab.operator.atlas import Dispatched

    registry = Registry(":memory:")
    session = UISession(offline_default=True, registry=registry)
    session.atlas.set_mode("research")
    facts = session.atlas_facts(True)
    facts["regime"]["flip"] = True
    created = session.atlas.observe(facts, trading_date="2026-07-26")
    task_id = created["created_tasks"][0]["task_id"]

    workflow = registry.start_workflow(
        "portfolio_review", {"goal": "g"}, phases=("news-analyst",))
    session.atlas.start_task(
        task_id, facts, runner=lambda t, tid: Dispatched(workflow["workflow_id"]))
    registry.update_workflow_phase(
        workflow["workflow_id"], "news-analyst", "done", summary="ok",
        artifacts={"news_view": "the record supports a narrow reading"})
    assert registry.get_atlas_task(task_id)["status"] == "running"

    # A fresh owner over the same registry, as after a restart.
    UISession(offline_default=True, registry=registry)

    assert registry.get_atlas_task(task_id)["status"] == "completed"


def test_a_dispatched_task_fails_when_the_restart_interrupts_its_workflow():
    # The other half: a workflow still running at restart is interrupted (no
    # coordinator lease survives), so its task must fail rather than hang.
    from qlab.operator.atlas import Dispatched

    registry = Registry(":memory:")
    session = UISession(offline_default=True, registry=registry)
    session.atlas.set_mode("research")
    facts = session.atlas_facts(True)
    facts["regime"]["flip"] = True
    created = session.atlas.observe(facts, trading_date="2026-07-26")
    task_id = created["created_tasks"][0]["task_id"]

    workflow = registry.start_workflow(
        "portfolio_review", {"goal": "g"}, phases=("news-analyst",))
    session.atlas.start_task(
        task_id, facts, runner=lambda t, tid: Dispatched(workflow["workflow_id"]))

    UISession(offline_default=True, registry=registry)

    stored = registry.get_atlas_task(task_id)
    assert stored["status"] == "failed"
    assert "interrupted" in (stored["error"] or "")


def test_an_unparseable_post_body_is_refused_rather_than_silently_emptied(
    session, monkeypatch,
):
    """A truncated body must never become {}.

    The route defaults are permissive: /api/run_once reads execute=True out of
    an empty body. Substituting {} for a body the owner could not parse
    therefore turned a dropped byte into an unrequested paper trade, answered
    200 as if the caller had asked for it.
    """
    import http.client
    import json
    import threading
    from http.server import ThreadingHTTPServer

    import qlab.autopilot.loop as loop_module
    import qlab.ui.server as server_module

    ran: list[dict] = []

    def refuse_to_run(**kwargs):
        ran.append(kwargs)
        return {"executed": True}

    monkeypatch.setattr(loop_module, "run_once", refuse_to_run)

    handler = type("H", (server_module._Handler,), {"session": session})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        # The caller asked NOT to execute; the body is truncated in transit.
        conn.request("POST", "/api/run_once", body=b'{"execute": fal')
        response = conn.getresponse()
        payload = json.loads(response.read())
        assert response.status == 400
        assert "not valid JSON" in payload["error"]
        assert ran == [], "a body the owner could not parse must not trade"

        # Valid JSON that is not an object is refused for the same reason.
        conn.request("POST", "/api/run_once", body=b"[]")
        response = conn.getresponse()
        assert response.status == 400
        assert "JSON object" in json.loads(response.read())["error"]
        assert ran == []

        # A genuinely absent body still means "the defaults", as before.
        conn.request("POST", "/api/run_once")
        response = conn.getresponse()
        assert response.status == 200
        assert len(ran) == 1
        conn.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
