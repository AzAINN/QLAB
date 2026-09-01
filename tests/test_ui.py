"""The single-page UI's JSON API (dispatch tested in-process, no sockets)."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta, timezone

import pytest

from qlab.state.registry import Registry
from qlab.ui import server as ui_server
from qlab.ui.server import UISession, handle_api


@pytest.fixture
def session():
    # isolated in-memory paper book per test
    return UISession(offline_default=True, registry=Registry(":memory:"))


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


def test_an_ineligible_desk_tells_the_operator_why_over_the_api(session):
    """The live desk served every check PASS, `eligible_for_paper_proposal:
    false` and `reasons: []`. Invariant 4 says a refusal states its reason,
    and this is the endpoint an operator actually reads."""
    _, health = handle_api(
        session, "GET", "/api/data/health", {"offline": ["1"]}, {})
    assert health["eligible_for_paper_proposal"] is False
    assert health["reasons"], "a refusal with no reason is unactionable"
    assert any("synthetic" in r for r in health["reasons"])
    # Singular `reason` is what atlas_facts and the TUI read; it must not be
    # None while `reasons` is populated, or the gate reports an unexplained
    # refusal to the operator and to Atlas.
    assert health["reason"]
    assert health["reason"] in health["reasons"]


def test_the_gate_carries_the_data_refusal_reason_not_a_bare_false(session):
    """`atlas_facts` read `health["reason"]`, which only the *blocked* branch
    ever set. On the ordinary ineligible path the gate handed Atlas
    `eligible: false, reason: None` — a refusal with no cause attached."""
    facts = session.atlas_facts(True)
    assert facts["data"]["eligible_for_paper_proposal"] is False
    assert facts["data"]["reason"], (
        "the gate refuses paper proposals and states no reason to Atlas")
    assert "synthetic" in facts["data"]["reason"]


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


def test_a_live_desk_mode_attaches_the_market_stream(session, monkeypatch):
    # The comment above `market_stream = None` promised an attachment "under an
    # operational policy"; nothing ever performed it, so live desks priced off
    # the daily bar path forever. The transport is injected: a bare session
    # (every other test here) never opens a socket.
    import threading

    from qlab.core.desk_mode import DeskMode
    from qlab.trader.alpaca_auth import AlpacaCredentials

    monkeypatch.setattr(
        "qlab.trader.alpaca_auth.resolve_alpaca_credentials",
        lambda: AlpacaCredentials(
            kind="api_key", api_key="k", secret_key="s",
            oauth_token=None, profile_name=None, source="test"))

    calls, ran = [], threading.Event()

    def fake_runner(*, supervisor, key, secret, stop_event):
        calls.append((supervisor, key, secret, stop_event))
        ran.set()

    session.attach_market_stream_runner(fake_runner)
    # A runner alone attaches nothing: demo mode has no live feed to hold.
    assert session.market_stream is None

    session.set_desk_mode(DeskMode("live", "simulated"))
    assert session.market_stream is not None
    assert list(session.market_stream.symbols) == \
        list(session.mandate.universe_whitelist)
    assert ran.wait(2.0)
    [(supervisor, key, secret, stop)] = calls
    assert supervisor is session.market_stream
    assert (key, secret) == ("k", "s")

    session.set_desk_mode(DeskMode("synthetic", "simulated"))
    # The switch tears the transport down, not just the handle.
    assert session.market_stream is None
    assert stop.is_set()


def test_live_mode_without_credentials_names_the_gap(session, monkeypatch):
    # Invariant 4: a live desk that cannot stream says which credential is
    # missing, rather than reporting the demo runtime's reason.
    from qlab.core.desk_mode import DeskMode

    monkeypatch.setattr(
        "qlab.trader.alpaca_auth.resolve_alpaca_credentials", lambda: None)
    session.attach_market_stream_runner(
        lambda **kw: pytest.fail("the transport must not run without keys"))
    session.set_desk_mode(DeskMode("live", "simulated"))

    assert session.market_stream is None
    status, out = handle_api(session, "GET", "/api/quotes", {}, {})
    assert status == 200
    assert out["live_stream"] is False
    assert "ALPACA_API_KEY" in out["reason"]


def test_an_oauth_profile_is_named_not_prescribed_again(session, monkeypatch):
    # An operator with a browser-login profile has already done `alpaca profile
    # login`; telling them to do it again is a loop. The refusal must say the
    # data websocket needs an API key pair and that the profile cannot carry it.
    from qlab.core.desk_mode import DeskMode
    from qlab.trader.alpaca_auth import AlpacaCredentials

    monkeypatch.setattr(
        "qlab.trader.alpaca_auth.resolve_alpaca_credentials",
        lambda: AlpacaCredentials(
            kind="oauth", api_key=None, secret_key=None,
            oauth_token="t", profile_name="paper", source="profile"))
    session.attach_market_stream_runner(
        lambda **kw: pytest.fail("an oauth token cannot open the websocket"))
    session.set_desk_mode(DeskMode("live", "simulated"))

    assert session.market_stream is None
    _, out = handle_api(session, "GET", "/api/quotes", {}, {})
    assert "browser login" in out["reason"]
    assert "ALPACA_API_KEY" in out["reason"]
    assert "profile login" not in out["reason"]


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
        server_module.serve(port=8765, offline=True)
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


def test_tui_launcher_waits_for_owner_readiness_after_spawn(
        monkeypatch, tmp_path):
    """A bound port is not enough: the owner may still be opening its state.

    The launcher may hand off to the workstation only after the owner's
    readiness probe answers — a client exec'd against a bound-but-not-ready
    owner opens on a broken desk.
    """
    import qlab.autopilot.cli as cli_module
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
            # Bare launch: the live default lane, no flag needed.
            assert params == {"offline": 0}
            calls["system"] += 1
            return {"mode": "live"}

    binary = tmp_path / "atlas"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("QLAB_ATLAS_BIN", str(binary))
    monkeypatch.setattr(cli_module.socket, "socket", ClosedPort)
    monkeypatch.setattr(
        cli_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: Owner(),
    )
    monkeypatch.setattr(cli_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(client_module, "ApiClient", Client)

    def fake_exec(_path, _argv, _env):
        # The handoff is the pass criterion: reaching it before the probe
        # answered would have failed on Client.get above.
        calls["run"] += 1
        raise SystemExit(0)

    monkeypatch.setattr(cli_module.os, "execvpe", fake_exec)

    def fake_run(_argv, **_kw):
        calls["run"] += 1
        # The launcher raises the child's returncode after this returns, so
        # the SystemExit expectation below holds on Windows too.
        return type("Done", (), {"returncode": 0})()

    monkeypatch.setattr(cli_module.subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exit_info:
        cli_module._cmd_tui(_tui_args(port=8877, claude="off"))
    assert exit_info.value.code == 0

    assert calls == {"probe": 2, "system": 1, "run": 1}


def _tui_args(**overrides):
    """The namespace ``qlab tui`` builds, with the defaults its parser sets."""
    from types import SimpleNamespace

    return SimpleNamespace(**{
        "port": 8765, "offline": False, "alpaca_book": False, "restart": False,
        "refresh": 2.0, "claude": "offer", "classic": False, "live": False,
        "online": False, "glass": False, "operator": False, **overrides,
    })


def _attached_owner(monkeypatch, cli_module):
    """Pretend a compatible owner is already listening, so the launcher's spawn
    and readiness logic is out of the way and only the client choice is left."""
    import qlab.tui.client as client_module

    class OpenPort:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def settimeout(self, _timeout):
            pass

        def connect_ex(self, _address):
            return 0

    class Client:
        def __init__(self, base_url):
            self.base_url = base_url

        def get(self, path, **_params):
            assert path == "/api/system"
            return {"mode": "offline"}

    monkeypatch.setattr(cli_module.socket, "socket", OpenPort)
    monkeypatch.setattr(client_module, "ApiClient", Client)
    monkeypatch.setattr(
        cli_module.subprocess, "Popen",
        lambda *_a, **_k: pytest.fail("an owner is already up; nothing may spawn"))


def test_tui_launches_the_ratatui_workstation_and_tells_it_the_port(
        monkeypatch, tmp_path):
    """The default client is the Rust workstation, exec'd in place.

    In place, not spawned: the launcher has nothing left to do once the owner is
    up, and a Python process parked on top of the client would be one more thing
    between an operator's Ctrl-C and the terminal being restored.
    """
    import qlab.autopilot.cli as cli_module

    _attached_owner(monkeypatch, cli_module)
    binary = tmp_path / "atlas"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("QLAB_ATLAS_BIN", str(binary))

    seen = {}

    def fake_exec(path, argv, env):
        seen.update(path=path, argv=argv, env=env)
        raise SystemExit(0)   # execvpe never returns; neither does this

    monkeypatch.setattr(cli_module.os, "execvpe", fake_exec)
    # The Windows hand-off is spawn-then-exit rather than exec-in-place;
    # capture it through the same `seen` so the assertions hold on every
    # platform. `subprocess.run` calls Popen internally, so leaving it
    # unpatched would also trip any Popen tripwire a test set.
    monkeypatch.setattr(
        cli_module.subprocess, "run",
        lambda argv, env=None, **_kw: seen.update(
            path=argv[0], argv=list(argv), env=env or {})
        or type("Done", (), {"returncode": 0})())

    with pytest.raises(SystemExit) as exit_info:
        cli_module._cmd_tui(_tui_args(port=8899))

    assert exit_info.value.code == 0
    assert seen["path"] == str(binary)
    # The bare launch resolves the live default, and the client is told
    # which view of the owner to poll.
    assert seen["argv"] == [str(binary), "--live"]
    # The one thing the client cannot discover for itself: which owner to talk
    # to. It never opens the registry, so a wrong port is a blank desk.
    assert seen["env"]["QLAB_UI_PORT"] == "8899"


def test_tui_glass_flag_reaches_the_workstation(monkeypatch, tmp_path):
    """--glass is the one posture word a launcher may still say.

    It only ever takes authority away, so forwarding it is safe in a way that
    forwarding an arming word never was: arming is the owner's persisted answer
    to the startup door and no launcher flag can grant it.
    """
    import qlab.autopilot.cli as cli_module

    _attached_owner(monkeypatch, cli_module)
    binary = tmp_path / "atlas"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("QLAB_ATLAS_BIN", str(binary))

    seen = {}
    monkeypatch.setattr(
        cli_module.os, "execvpe",
        lambda path, argv, env: seen.update(argv=argv) or (_ for _ in ()).throw(SystemExit(0)))
    # The Windows leg of the same hand-off, captured the same way.
    monkeypatch.setattr(
        cli_module.subprocess, "run",
        lambda argv, **_kw: seen.update(argv=list(argv))
        or type("Done", (), {"returncode": 0})())

    with pytest.raises(SystemExit):
        cli_module._cmd_tui(_tui_args(glass=True))
    assert seen["argv"] == [str(binary), "--glass", "--live"]


def test_tui_without_a_built_workstation_says_how_to_build_it(monkeypatch, tmp_path):
    """Fail loud. A missing binary must never fall back to the Textual client.

    Silently running a different client would make --classic unfalsifiable as a
    soak valve: an operator soaking the Rust workstation would have been on the
    Textual one the whole week and had no way to tell.
    """
    import qlab.autopilot.cli as cli_module

    _attached_owner(monkeypatch, cli_module)
    monkeypatch.setenv("QLAB_ATLAS_BIN", str(tmp_path / "not-built"))
    monkeypatch.setattr(
        cli_module.os, "execvpe",
        lambda *_a: pytest.fail("a missing binary must not be exec'd"))

    with pytest.raises(SystemExit) as exit_info:
        cli_module._cmd_tui(_tui_args())
    message = str(exit_info.value)
    assert "cargo build --release" in message
    assert "QLAB_ATLAS_BIN" in message


def test_tui_refuses_the_retired_classic_flag(monkeypatch):
    """The Textual client is gone; the word may not quietly do anything else.

    A flag that parses and silently draws a different screen than it used to
    is the worst kind of no-op — refused by name, with the sentence saying
    what the desk's one client is.
    """
    import qlab.autopilot.cli as cli_module

    monkeypatch.setattr(
        cli_module.subprocess, "Popen",
        lambda *_a, **_k: pytest.fail("a refused invocation must not spawn"))
    monkeypatch.setattr(
        cli_module.os, "execvpe",
        lambda *_a: pytest.fail("a refused invocation must not exec"))

    with pytest.raises(SystemExit) as exit_info:
        cli_module._cmd_tui(_tui_args(classic=True))
    message = str(exit_info.value)
    assert "--classic is retired" in message
    assert "Atlas workstation" in message


def test_tui_refuses_the_retired_live_words(monkeypatch):
    """Live data became the default; the old words must name the new one."""
    import qlab.autopilot.cli as cli_module

    monkeypatch.setattr(
        cli_module.subprocess, "Popen",
        lambda *_a, **_k: pytest.fail("a refused invocation must not spawn"))

    for retired in ({"live": True}, {"online": True}):
        with pytest.raises(SystemExit) as exit_info:
            cli_module._cmd_tui(_tui_args(**retired))
        message = str(exit_info.value)
        assert "retired" in message and "--offline" in message


@pytest.mark.parametrize("classic", [False, True])
def test_tui_refuses_the_retired_operator_flag_before_everything(monkeypatch, classic):
    """`--operator` grants nothing now, so it must not parse quietly.

    Arming became the owner's persisted answer to the startup door in this
    branch. A flag that survives as a no-op is exactly the silence invariant 4
    forbids: the operator asks for an armed window, is told nothing, and gets
    whatever posture the desk happened to hold. Refused on either client, and
    the refusal has to name where arming went and what the surviving one-session
    override is.
    """
    import qlab.autopilot.cli as cli_module

    # Refused on the arguments alone: nothing is spawned, probed or exec'd, so
    # the refusal cannot depend on a desk being there.
    monkeypatch.setattr(
        cli_module.subprocess, "Popen",
        lambda *_a, **_k: pytest.fail("a refused invocation must not spawn"))
    monkeypatch.setattr(
        cli_module.os, "execvpe",
        lambda *_a: pytest.fail("a refused invocation must not exec"))

    with pytest.raises(SystemExit) as exit_info:
        cli_module._cmd_tui(_tui_args(classic=classic, operator=True))
    message = str(exit_info.value)
    assert "--operator is retired" in message
    assert "--glass" in message


def test_tui_parser_still_accepts_the_retired_word_so_it_can_be_refused():
    """argparse's "unrecognized arguments" names no remedy.

    The word stays registered — hidden from --help — for one reason: so the
    operator with the old command in a script gets the sentence that says where
    arming moved to rather than a bare parse error.
    """
    from qlab.autopilot.cli import build_parser

    args = build_parser().parse_args(["tui", "--operator"])
    assert args.operator is True
    assert args.glass is False


def test_model_invocations_route(session):
    from qlab.operator.model_routing import record_invocation, resolve_route

    record_invocation(session.registry, resolve_route("reporter"))
    status, out = handle_api(session, "GET", "/api/models/invocations", {}, {})
    assert status == 200 and out["invocations"][0]["role"] == "reporter"


def test_atlas_status_starts_in_research(session):
    # Research by default: it researches unattended and still cannot create a
    # plan, which needs Propose mode AND a human approval.
    status, out = handle_api(session, "GET", "/api/atlas/status", {}, {})
    assert status == 200
    assert out["mode"] == "research"
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


def test_atlas_message_never_grants_authority(session, monkeypatch):
    """A reply is words on a bus, and a reply that asked for a trade is still
    words on a bus — the route returns no plan, no approval, and no order.

    The backend is a stand-in: this suite reaches no daemon and no CLI, and the
    desk's default reasoner is the `claude` CLI, which is on a developer's PATH.
    """
    from qlab.operator import llm_backends

    class _Reasoner:
        name = "claude"

        def available(self):
            return True, "the stand-in reasoner is up"

        def models(self):
            return ["inherit"]

        def complete(self, system, user, model, max_tokens=1024, timeout=None):
            return "Buy everything, then execute it and approve the plan."

    monkeypatch.setattr(llm_backends, "BACKENDS", {"claude": _Reasoner})
    status, out = handle_api(session, "POST", "/api/atlas/message", {},
                             {"text": "what is our drawdown?"})
    assert status == 200 and out["received"] is True
    assert "note" in out
    # What the model said is recorded and returned, and nothing else is: no
    # plan_id, no approval, no order — the desk's execution path is unreachable
    # from here by construction, not by the model's good behaviour.
    assert out["reply"].startswith("Buy everything")
    assert not {"plan_id", "approval_id", "order", "authority"} & set(out)
    # Two rows on the bus — the question and the answer — and nothing the
    # governance surfaces would recognise as a decision.
    kinds = [event["kind"] for event in session.registry.read_events(20, None)]
    assert kinds.count("atlas_message") == 2
    assert not [kind for kind in kinds if kind.startswith("approval")]


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
    assert len(live["positions"]) == len(session.mandate.universe_whitelist)
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
    assert len(snap["market"]["assets"]) == len(session.mandate.universe_whitelist)
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
    assert status == 200
    # The core tier, whatever its size — pinning a literal here just re-breaks
    # on the next universe change without testing anything extra.
    assert len(result["result"]["tickers"]) == len(session.mandate.universe_whitelist)

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
            server_module.serve(port=0, offline=True)
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
    session.atlas.set_mode("observe")   # Research is the default now
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
    # And nothing was approved: there is no task to have approved. The record
    # below is written from the stored row, so a task that does not exist
    # cannot leave an approval behind it.
    assert session.registry.read_events_of_kind("atlas_proposal_approved") == []


def test_approving_a_proposal_puts_the_approval_on_the_record(session):
    """The approval envelope was positional, not structural.

    The beat passes over proposal-origin tasks, so this route IS the approval
    — and "which route was hit" was the only evidence a human gave one. A
    started proposal now carries a durable row saying who asked, written
    before the start so a refused or crashed start still records the asking.
    """
    session.atlas.set_mode("research")
    offered = [item for item in session.atlas_actionables(True)["items"]
               if item["startable"]]
    assert offered, "research mode offers at least one template"
    item = offered[0]

    status, started = handle_api(
        session, "POST", f"/api/atlas/tasks/{item['task_id']}/start", {},
        {"offline": True})

    assert status == 200 and started["started"] is True
    rows = session.registry.read_events_of_kind("atlas_proposal_approved")
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["task_id"] == item["task_id"]
    assert payload["template_id"] == item["template_id"]
    assert payload["task_status"] == "queued"
    # Before the start, not after: an audit trail whose approval follows the
    # work it authorises reads as a permission granted retroactively.
    kinds = [row["kind"] for row in session.registry.read_events(200)]
    assert kinds.index("atlas_proposal_approved") < kinds.index("atlas_task_started")


def test_a_start_the_gate_refuses_still_records_the_approval_that_asked(session):
    """The negative side of writing the row before the start.

    The human approved; the gate then said no. Both are facts, and the one
    this route is the only evidence of is the approval — so it is recorded
    whatever the gate answers, carrying the status it was approved in. A row
    written only on success would lose every approval the desk turned down,
    which is exactly the half an audit asks about.
    """
    session.atlas.set_mode("research")
    offered = [item for item in session.atlas_actionables(True)["items"]
               if item["startable"]]
    assert offered, "research mode offers at least one template"
    item = offered[0]
    # Observe cannot start anything: `check_startable` refuses on authority,
    # and the route answers 200 saying so.
    session.atlas.set_mode("observe")

    status, refused = handle_api(
        session, "POST", f"/api/atlas/tasks/{item['task_id']}/start", {},
        {"offline": True})

    assert status == 200
    assert refused["started"] is False and refused["blocked_by"] == "authority"
    rows = session.registry.read_events_of_kind("atlas_proposal_approved")
    assert [row["payload"]["task_id"] for row in rows] == [item["task_id"]]
    # The status it was approved in, which is what tells this row apart from
    # one that started work: nothing ran, and the task is still queued.
    assert rows[0]["payload"]["task_status"] == "queued"
    assert "atlas_task_started" not in [
        row["kind"] for row in session.registry.read_events(200)]


def test_the_beats_own_work_is_not_recorded_as_an_approval(session):
    """The other side of the guard. A trigger is work the desk raised for
    itself and may start unattended, so a row saying a human approved one
    would put a decision on the record that nobody made."""
    today = date.today().isoformat()
    session.registry.create_atlas_task(
        "task-trigger", f"regime_flip|{today}|SPY|abc", "regime_flip",
        {"why": "flip"}, "regime_review")
    session.atlas.set_mode("research")

    status, started = handle_api(
        session, "POST", "/api/atlas/tasks/task-trigger/start", {},
        {"offline": True})

    assert status == 200 and started["started"] is True
    assert session.registry.read_events_of_kind("atlas_proposal_approved") == []


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


def test_the_tui_snapshot_serves_the_approval_an_execution_can_bind_to(session):
    # The execute gate consumes an APPROVED, unconsumed approval — a pending one
    # authorises nothing. The snapshot used to carry only the pending queue, so
    # a client polling /api/tui could never see the record a legal execution
    # binds to, and the operator surface had no way to offer the key.
    pending_plan = _checked_plan(session)
    approved_plan = _checked_plan(session, tilt=0.02)
    spent_plan = _checked_plan(session, tilt=0.04)

    approved = _approve(session, approved_plan)
    _, created = handle_api(
        session, "POST", "/api/approvals", {},
        {"plan_id": pending_plan, "offline": True})
    pending = created["approval_id"]
    spent = _approve(session, spent_plan)
    handle_api(
        session, "POST", "/api/plans/execute", {},
        {"offline": True, "plan_id": spent_plan, "human_confirmed": True,
         "approval_id": spent})
    assert session.registry.get_approval_request(spent)["status"] == "consumed"

    _, snap = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})
    served = {a["approval_id"]: a["status"] for a in snap["approvals"]}
    assert served.get(pending) == "pending"
    assert served.get(approved) == "approved"
    # Spent, rejected and expired records are history, not a decision queue: an
    # approval a client could act on is exactly one of these two statuses.
    assert spent not in served
    assert set(served.values()) == {"pending", "approved"}
    # Newest first, as every other list in the payload is served.
    stamps = [a["created_at"] for a in snap["approvals"]]
    assert stamps == sorted(stamps, reverse=True)


def test_the_snapshot_approval_cap_cannot_be_starved_by_one_status(session):
    # The cap is per status rather than shared. One busy queue must never crowd
    # the other out — a desk with eleven pending requests would otherwise stop
    # showing the one approval that can actually be executed.
    approved = _approve(session, _checked_plan(session))
    for i in range(1, 13):
        handle_api(
            session, "POST", "/api/approvals", {},
            {"plan_id": _checked_plan(session, tilt=0.001 * i), "offline": True})

    _, snap = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})
    served = [(a["approval_id"], a["status"]) for a in snap["approvals"]]
    assert (approved, "approved") in served
    assert sum(1 for _, status in served if status == "pending") == 10


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


def test_news_follows_the_data_lane_the_operator_chose(session, monkeypatch):
    # A desk on live prices whose qualitative side is deterministic fixtures
    # carries a real market and an invented narrative under one heading. News
    # follows the lane, so signing in and running --live is the whole setup.
    monkeypatch.delenv("QLAB_NEWS_PROVIDER", raising=False)
    monkeypatch.delenv("QLAB_NEWS_PROVIDERS", raising=False)

    # Offline is always synthetic: it is the demo and must not reach the network.
    monkeypatch.setattr(
        "qlab.trader.alpaca_auth.resolve_alpaca_credentials", lambda: object())
    assert session.news_provider_for(True) == ("synthetic",)
    # Live with a resolvable credential upgrades without being asked.
    assert session.news_provider_for(False) == ("alpaca",)

    # Live with no credential stays synthetic rather than failing the desk.
    monkeypatch.setattr(
        "qlab.trader.alpaca_auth.resolve_alpaca_credentials", lambda: None)
    assert session.news_provider_for(False) == ("synthetic",)

    # An explicit provider is an instruction and is never second-guessed —
    # including naming synthetic on a live desk on purpose.
    monkeypatch.setenv("QLAB_NEWS_PROVIDER", "synthetic")
    monkeypatch.setattr(
        "qlab.trader.alpaca_auth.resolve_alpaca_credentials", lambda: object())
    assert session.news_provider_for(False) == ("synthetic",)

    # The plural names a stack and wins over the singular; a credential that
    # resolves does not get to append itself to what the operator asked for.
    monkeypatch.setenv("QLAB_NEWS_PROVIDERS", "edgar,macro")
    assert session.news_provider_for(False) == ("edgar", "macro")


def test_an_opened_debate_can_be_closed_so_the_reporter_can_run(session):
    # `adjudicate()` had no caller anywhere, and the reporter refuses to start
    # while any debate on its workflow is open — so an opened debate was a
    # permanent deadlock: the run could neither finish nor be finished.
    from qlab.governance.debate import open_debate

    workflow = session.registry.start_workflow("portfolio_review", {"goal": "g"})
    wid = workflow["workflow_id"]
    debate_id = open_debate(
        session.registry, workflow_id=wid,
        original_decision_id="dec-1",
        material_claims=["estimation_window"],
        panel_snapshot_id=None)

    # Visible, so the operator can find what is blocking the run.
    status, listing = handle_api(session, "GET", "/api/debates", {}, {})
    assert status == 200
    assert debate_id in [d["debate_id"] for d in listing["debates"]]

    # Closable, with a reasoned adjudication covering every claim.
    status, out = handle_api(
        session, "POST", f"/api/debates/{debate_id}/adjudicate", {},
        {"resolution": "756d retained; conditioning checked out",
         "winning_claim_positions": {"estimation_window": "756d upheld"}})
    assert status == 200 and out["resolution"].startswith("756d retained")
    assert session.registry.get_debate(debate_id)["status"] != "open"

    # An adjudication that leaves a claim undecided is refused, not accepted.
    other = open_debate(
        session.registry, workflow_id=wid, original_decision_id="dec-1",
        material_claims=["estimation_window", "shrinkage"],
        panel_snapshot_id=None)
    status, out = handle_api(
        session, "POST", f"/api/debates/{other}/adjudicate", {},
        {"resolution": "half an answer",
         "winning_claim_positions": {"estimation_window": "upheld"}})
    assert status == 400 and "undecided" in out["error"]


def test_a_real_venue_valuation_is_reused_between_polls(session, monkeypatch):
    # The TUI polls /api/tui every two seconds and that payload carries the
    # valuation, so on the Alpaca book an idle desk was making one or two
    # broker calls a second for a book that only changes when this desk trades.
    from qlab.core.desk_mode import DeskMode

    session.set_desk_mode(DeskMode("live", "alpaca"))
    calls = {"n": 0}

    def counted(offline):
        calls["n"] += 1
        return {"equity": 1.0, "positions": [], "blocked": False}

    monkeypatch.setattr(session, "_compute_live_portfolio", counted)

    for _ in range(5):
        session.live_portfolio(True)
    assert calls["n"] == 1, "each poll re-queried the venue"

    # A fill must not wait out the TTL.
    session.invalidate_valuation()
    session.live_portfolio(True)
    assert calls["n"] == 2


def test_the_simulated_book_is_never_served_from_cache(session, monkeypatch):
    # The simulator is local and free; caching it would only add a way for the
    # demo to show a stale book.
    assert session.desk_mode.book == "simulated"
    calls = {"n": 0}

    def counted(offline):
        calls["n"] += 1
        return {"equity": 1.0, "positions": [], "blocked": False}

    monkeypatch.setattr(session, "_compute_live_portfolio", counted)
    for _ in range(3):
        session.live_portfolio(True)
    assert calls["n"] == 3


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


def test_a_desk_nobody_chose_is_not_served_as_a_chosen_one(session):
    """The pair alone cannot tell "chosen synthetic" from "nobody said".

    Six fields of a concrete pair are what a fresh desk and a deliberately
    synthetic one both serve, byte for byte — so a client asking "has anyone
    answered?" had to guess, and the startup door's own honesty note said so.
    ``chosen`` is that seventh fact and nothing more: it does not say the pair
    is good, only that something named it.
    """
    payload = handle_api(session, "GET", "/api/desk_mode", {}, {})[1]
    assert payload["chosen"] is False
    # And the fallback is still served in full: an unchosen desk is a working
    # desk, not an error state.
    assert (payload["data"], payload["book"]) == ("synthetic", "simulated")


def test_a_posted_desk_mode_is_a_choice_from_then_on(session):
    """Without this the flag would be permanently false on a fresh owner.

    The three-way ``or`` runs once at construction; the POST never goes near
    it, so a session that was asked and answered would keep reporting that
    nobody had — and the door would open again on every run.
    """
    from qlab.core.desk_mode import DeskMode, load_desk_mode

    assert handle_api(session, "GET", "/api/desk_mode", {}, {})[1]["chosen"] is False
    status, payload = handle_api(
        session, "POST", "/api/desk_mode", {},
        {"data": "synthetic", "book": "simulated"})
    assert status == 200
    # The same pair it already had: what changed is that somebody said it.
    assert (payload["data"], payload["book"]) == ("synthetic", "simulated")
    assert payload["chosen"] is True
    assert handle_api(session, "GET", "/api/desk_mode", {}, {})[1]["chosen"] is True
    # And it is durable, which is the fact the flag is about.
    assert load_desk_mode() == DeskMode("synthetic", "simulated")


def test_a_desk_mode_the_launcher_passed_is_chosen_without_being_persisted():
    """Flag-chosen and file-unchosen is a real state, and it reads as chosen.

    ``UISession(desk_mode=…)`` is the launcher's own answer and is deliberately
    not written to disk. The flag means "a flag or the file named this", never
    "there is a file" — a client that reopened the question here would ask
    about a desk the operator had just named on the command line.
    """
    from qlab.core.desk_mode import DeskMode, load_desk_mode

    assert load_desk_mode() is None
    live = UISession(offline_default=True, registry=Registry(":memory:"),
                     desk_mode=DeskMode("live", "simulated"))
    assert live.desk_mode_payload()["chosen"] is True
    assert load_desk_mode() is None, "a launcher flag is not a persisted choice"
    # The file is the other half of the same claim, on its own.
    from qlab.core.desk_mode import save_desk_mode
    save_desk_mode(DeskMode("live", "alpaca"))
    persisted = UISession(offline_default=True, registry=Registry(":memory:"))
    assert persisted.desk_mode_payload()["chosen"] is True


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
    # Today's date, because that is what the production observe tick passes.
    # A hardcoded past date used to work here and now correctly reads as a
    # stale trigger, which is the whole point of max_task_age_days.
    session.atlas.observe(facts, trading_date=date.today().isoformat())

    started = session.atlas_run_startable(True, limit=1)

    assert started, "expected one startable task"
    for entry in started:
        assert entry.get("completed") is not True
    for task in session.registry.list_atlas_tasks(limit=10):
        if task["status"] == "completed":
            assert task["conclusion"].get("workflow_status") == "complete", (
                "a task completed without its workflow reaching terminal state")


def test_the_heartbeat_never_starts_a_proposal(session):
    """The envelope, in one test. A proposal is attended by construction:
    the operator approves it or it does not run. If the heartbeat could start
    one, the approval gate would be decorative on arrival."""
    # Today's date, not a literal: `startable_tasks` refuses a trigger older
    # than max_task_age_days, so a hardcoded day makes this test pass now and
    # go stale-refused later — for the wrong reason, hiding the guard.
    today = date.today().isoformat()
    session.registry.create_atlas_task(
        "task-proposal", f"regime_review|{today}|SPY|abc", "operator_asked",
        {"why": "asked"}, "regime_review", origin="proposal")
    session.atlas.set_mode("research")

    # The premise, asserted rather than assumed: this task is startable on every
    # axis except its origin. Without this, the day `regime_review` stops being
    # startable in Research mode, `started == []` goes true for the wrong reason
    # and the envelope stops being what this test measures.
    entry = next(e for e in session.atlas.startable_tasks(session.atlas_facts(True))
                 if e["task_id"] == "task-proposal")
    assert entry["startable"] is True, entry.get("reason")
    assert entry["origin"] == "proposal"

    started = session.atlas_run_startable(True, limit=5)

    assert started == []
    assert session.registry.get_atlas_task("task-proposal")["status"] == "queued"


def test_the_heartbeat_still_starts_a_trigger_task(session):
    """The other side of the same guard: this project did not turn autonomy off.

    ``regime_flip`` rather than the invented ``regime_shift`` this fixture used
    to carry: the beat now reads the kind as well as the origin, so a kind no
    part of the desk maps or classifies is a fixture that could pass or fail
    for reasons unrelated to what this test measures."""
    today = date.today().isoformat()
    session.registry.create_atlas_task(
        "task-trigger", f"regime_flip|{today}|SPY|abc", "regime_flip",
        {"why": "regime"}, "regime_review")
    session.atlas.set_mode("research")

    started = session.atlas_run_startable(True, limit=5)

    assert [entry["task_id"] for entry in started] == ["task-trigger"]


def test_a_task_written_before_this_column_reads_as_a_trigger(session):
    """An existing dev DB's rows are NULL here, and they are all trigger work.
    Reading NULL as a proposal would silently stop the desk's own autonomy."""
    today = date.today().isoformat()
    session.registry.con.execute(
        "INSERT INTO atlas_tasks (task_id, dedupe_key, trigger_kind, "
        "trigger_payload, template_id, status, attempt_count, created_at, "
        "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ["task-old", f"regime_flip|{today}|SPY|old", "regime_flip", "{}",
         "regime_review", "queued", 0, "2026-08-06T00:00:00Z",
         "2026-08-06T00:00:00Z"])
    session.atlas.set_mode("research")

    started = session.atlas_run_startable(True, limit=5)

    assert [entry["task_id"] for entry in started] == ["task-old"]


def test_an_empty_origin_is_not_read_as_a_trigger(session):
    """The falsy-but-not-NULL case, which is the only one that separates
    `is None` from `or`. The writer refuses `""`, so this row is written
    raw — the point is that the reader does not hand the beat a permit if
    an empty origin ever reaches the column by another route."""
    today = date.today().isoformat()
    session.registry.con.execute(
        "INSERT INTO atlas_tasks (task_id, dedupe_key, trigger_kind, "
        "trigger_payload, template_id, status, attempt_count, origin, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ["task-empty", f"regime_flip|{today}|SPY|e", "regime_flip", "{}",
         "regime_review", "queued", 0, "", "2026-08-06T00:00:00Z",
         "2026-08-06T00:00:00Z"])
    session.atlas.set_mode("research")

    started = session.atlas_run_startable(True, limit=5)

    assert started == []
    assert session.registry.get_atlas_task("task-empty")["status"] == "queued"


def test_asking_for_actionables_lists_refusals_beside_the_offers(session):
    session.atlas.set_mode("research")
    payload = session.atlas_actionables(True)
    by_id = {item["template_id"]: item for item in payload["items"]}
    # Every registered template is represented, permitted or not.
    assert by_id
    refused = [item for item in payload["items"] if not item["startable"]]
    assert all(item["reason"] for item in refused)
    assert all(item.get("task_id") is None for item in refused)


def test_a_startable_actionable_becomes_a_proposal_task(session):
    session.atlas.set_mode("research")
    payload = session.atlas_actionables(True)
    offered = [item for item in payload["items"] if item["startable"]]
    assert offered, "research mode offers at least one template"
    task = session.registry.get_atlas_task(offered[0]["task_id"])
    assert task["origin"] == "proposal"
    assert task["status"] == "queued"
    assert task["template_id"] == offered[0]["template_id"]


def test_asking_twice_on_one_day_proposes_once(session):
    """Same question, same facts, same day — one proposal, not two. The dedupe
    key is the existing shape, so `_task_age` can still read the date out of it."""
    session.atlas.set_mode("research")
    first = session.atlas_actionables(True)
    second = session.atlas_actionables(True)
    ids = lambda p: sorted(i["task_id"] for i in p["items"] if i["startable"])
    assert ids(first) == ids(second)
    assert len(session.registry.list_atlas_tasks(200)) == len(ids(first))


def test_a_proposal_is_startable_rather_than_stale(session):
    """The trap this dedupe shape exists to avoid: a key `_task_age` cannot
    parse reads as age-unknown, and an age-unknown task is refused."""
    session.atlas.set_mode("research")
    payload = session.atlas_actionables(True)
    offered = [i for i in payload["items"] if i["startable"]][0]
    facts = session.atlas_facts(True)
    entry = next(e for e in session.atlas.startable_tasks(facts)
                 if e["task_id"] == offered["task_id"])
    assert entry["startable"] is True, entry.get("reason")


def test_the_actionables_route_answers(session):
    session.atlas.set_mode("research")
    status, payload = handle_api(session, "POST", "/api/atlas/actionables", {}, {})
    assert status == 200
    assert payload["items"]


def test_the_snapshot_carries_the_actionables(session):
    session.atlas.set_mode("research")
    session.atlas_actionables(True)
    status, snap = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})
    assert status == 200
    assert snap["actionables"]["items"]


def test_the_snapshot_never_mints_a_proposal(session):
    """A snapshot is drawn every two seconds. If drawing it composed the menu,
    every poll would write a task row per startable template — and the desk's
    task table would be a log of nobody having asked anything."""
    session.atlas.set_mode("research")
    status, snap = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})
    assert status == 200
    assert snap["actionables"]["items"] == []
    assert session.registry.list_atlas_tasks(200) == []


def test_a_proposal_whose_template_vanished_is_shown_refused(session):
    """A release can unregister a template while a proposal for it is queued.
    Skipping the row would drop an approvable item out of the client's view
    with nothing said; failing the whole snapshot would take the desk down."""
    today = date.today().isoformat()
    session.registry.create_atlas_task(
        "task-orphan", f"proposal:gone_template|{today}|SPY|gone_template",
        "proposal:gone_template", {"template_id": "gone_template"},
        "gone_template", origin="proposal")

    _, snap = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})

    item = next(i for i in snap["actionables"]["items"]
                if i["task_id"] == "task-orphan")
    assert item["startable"] is False
    assert "gone_template" in item["reason"]


def test_a_proposal_written_between_lookup_and_insert_is_refused(session, monkeypatch):
    """The dedupe key is taken but the lookup did not see it — a writer that
    got there first. Returning the id just minted would hand the operator an
    approve button for a task that was never written."""
    today = date.today().isoformat()
    universe = ",".join(sorted(session.mandate.universe_whitelist))
    session.registry.create_atlas_task(
        "hidden", f"proposal:desk_brief|{today}|{universe}|desk_brief",
        "proposal:desk_brief", {"template_id": "desk_brief"}, "desk_brief",
        origin="proposal")
    monkeypatch.setattr(session.registry, "get_atlas_task_by_dedupe",
                        lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="never stored"):
        session.atlas_actionables(True)


def test_asking_today_retires_yesterdays_unapproved_proposals(session):
    """Nothing else expires a proposal. Left queued, one set per day
    accumulates inside the bounded window the gate reads."""
    today = date.today()
    yesterday = (today - timedelta(days=1)).isoformat()
    session.registry.create_atlas_task(
        "stale-proposal", f"proposal:regime_review|{yesterday}|SPY|regime_review",
        "proposal:regime_review", {"template_id": "regime_review"},
        "regime_review", origin="proposal")
    session.registry.create_atlas_task(
        "live-trigger", f"regime_flip|{yesterday}|SPY|abc", "regime_flip",
        {"why": "flip"}, "regime_review")
    session.atlas.set_mode("research")

    session.atlas_actionables(True)

    assert session.registry.get_atlas_task("stale-proposal")["status"] == "expired"
    # A trigger is a claim about a trading day and keeps for max_task_age_days.
    # The cleanup is for proposals only; expiring a fresh trigger here would
    # silently disarm the desk's own autonomy.
    assert session.registry.get_atlas_task("live-trigger")["status"] == "queued"


def test_queued_proposals_do_not_crowd_a_trigger_out_of_the_gate(session):
    """The margin Task 2 was careful to avoid. Proposals are minted per
    template per day, so a desk that is asked daily buries an older — but
    still fresh — trigger below the window `startable_tasks` scans."""
    today = date.today()
    trigger_day = (today - timedelta(days=2)).isoformat()
    # Oldest row first: `list_atlas_tasks` orders newest-first, so the trigger
    # is what falls off the end of a short window.
    session.registry.create_atlas_task(
        "buried-trigger", f"regime_flip|{trigger_day}|SPY|abc", "regime_flip",
        {"why": "flip"}, "regime_review")
    for i in range(60):
        session.registry.create_atlas_task(
            f"proposal-{i}", f"proposal:regime_review|{today.isoformat()}|SPY|{i}",
            "proposal:regime_review", {"template_id": "regime_review"},
            "regime_review", origin="proposal")
    session.atlas.set_mode("research")

    entries = session.atlas.startable_tasks(session.atlas_facts(True))

    assert "buried-trigger" in [entry["task_id"] for entry in entries]


def test_the_snapshot_never_reports_a_verdict_it_did_not_ask_for(session):
    """The poll cannot afford `atlas_facts` — `_atlas_regime_facts` latches the
    regime, so a two-second snapshot would swallow every flip before the
    observe tick saw it. What it must not do is assert the verdict anyway: a
    proposal minted in Research read `startable: true` forever, including after
    the desk was moved to Observe, where the gate refuses all seven."""
    session.atlas.set_mode("research")
    session.atlas_actionables(True)
    _, before = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})
    # The proposals — the items with a task behind them. Nothing was checked
    # here, and nothing claims otherwise. (The ask's own refusals are merged in
    # beside them carrying `startable: false`; those are a verdict the gate DID
    # make, which is the next test.)
    proposed = [item for item in before["actionables"]["items"] if item["task_id"]]
    assert proposed
    assert all(item["startable"] is None for item in proposed)
    assert all("actionables" in (item["reason"] or "") for item in proposed)

    session.atlas.set_mode("observe")
    _, after = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})

    proposed = [item for item in after["actionables"]["items"] if item["task_id"]]
    assert proposed
    assert all(item["startable"] is False for item in proposed)
    assert all("Observe mode" in item["reason"] for item in proposed)


def test_the_snapshot_refuses_a_proposal_that_has_gone_stale(session):
    """`start_task` has no age check, so an item shown as startable is an item
    that will run. `_task_age` needs no facts, which is exactly why this
    surface can and must ask it."""
    old_day = (date.today() - timedelta(days=10)).isoformat()
    session.registry.create_atlas_task(
        "old-proposal", f"proposal:desk_brief|{old_day}|SPY|desk_brief",
        "proposal:desk_brief", {"template_id": "desk_brief"}, "desk_brief",
        origin="proposal")
    session.atlas.set_mode("research")

    _, snap = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})

    item = next(i for i in snap["actionables"]["items"]
                if i["task_id"] == "old-proposal")
    assert item["startable"] is False
    assert "stale" in item["reason"]


def test_an_already_started_proposal_stops_being_offered_today(session):
    """Ask, approve, ask again. The dedupe key survives the day, so the second
    ask found the running task and called it startable — a verdict the route
    itself answers 400 to, and one the snapshot (queued rows only) contradicted."""
    session.atlas.set_mode("research")
    first = session.atlas_actionables(True)
    offered = [item for item in first["items"] if item["startable"]][0]
    session.registry.update_atlas_task(offered["task_id"], status="running")

    second = session.atlas_actionables(True)

    item = next(i for i in second["items"]
                if i["template_id"] == offered["template_id"])
    assert item["task_id"] == offered["task_id"]
    assert item["startable"] is False
    assert item["task_status"] == "running"
    assert "running" in item["reason"]
    # And the two surfaces agree about it rather than one omitting it.
    _, snap = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})
    shown = next(i for i in snap["actionables"]["items"]
                 if i["task_id"] == offered["task_id"])
    assert shown["startable"] is False
    assert shown["task_status"] == "running"


def test_the_snapshot_shows_what_the_ask_refused_and_not_only_what_it_offered(session):
    """§B2: refused candidates are shown with their refusal, not hidden. A
    refusal mints no task, so a block composed from proposal rows alone showed
    the operator only the half the desk agreed to — in Research that silently
    drops every plan-creating template off the panel."""
    session.atlas.set_mode("research")
    asked = session.atlas_actionables(True)
    refused = [i for i in asked["items"]
               if not i["startable"] and i["task_id"] is None]
    assert refused, "premise: Research refuses at least one template outright"

    _, snap = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})

    shown = {i["template_id"]: i for i in snap["actionables"]["items"]}
    for item in refused:
        drawn = shown[item["template_id"]]
        assert drawn["startable"] is False
        # No task, and that is the fact rather than a gap: there is nothing
        # queued to approve, which is what `approvable()` reads.
        assert drawn["task_id"] is None
        assert drawn["task_status"] is None
        assert drawn["reason"] == item["reason"]


def test_an_ask_that_offers_nothing_still_says_what_it_refused(session):
    """Observe refuses every template, so nothing is minted and there is no
    proposal row to compose a block from. The desk still has to say why: an
    empty panel reads as a desk with nothing to do rather than as a mode."""
    session.atlas.set_mode("observe")
    asked = session.atlas_actionables(True)
    assert not [i for i in asked["items"] if i["startable"]]
    assert session.registry.list_atlas_tasks(200, origin="proposal") == []

    _, snap = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})

    block = snap["actionables"]
    assert block["trading_date"] == asked["trading_date"]
    assert {i["template_id"] for i in block["items"]} == {
        i["template_id"] for i in asked["items"]}
    assert all(i["startable"] is False for i in block["items"])
    assert all("Observe mode" in i["reason"] for i in block["items"])


def test_a_template_with_a_proposal_row_is_not_also_shown_as_a_refusal(session):
    """One trading day can hold two asks against different facts: the first
    offers a template and mints its task, the second refuses it. The row is the
    better account of the two — it carries the task an approval binds to, and
    the snapshot re-checks the mode against it — so the stored refusal stands
    down rather than drawing the same template twice."""
    today = date.today().isoformat()
    universe = ",".join(sorted(session.mandate.universe_whitelist))
    session.registry.create_atlas_task(
        "earlier-ask", f"proposal:news_read|{today}|{universe}|news_read",
        "proposal:news_read", {"template_id": "news_read"}, "news_read",
        origin="proposal")
    session.atlas.set_mode("research")
    asked = session.atlas_actionables(True)
    assert next(i for i in asked["items"]
                if i["template_id"] == "news_read")["startable"] is False

    _, snap = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})

    drawn = [i for i in snap["actionables"]["items"]
             if i["template_id"] == "news_read"]
    assert len(drawn) == 1
    assert drawn[0]["task_id"] == "earlier-ask"


def test_the_task_panel_carries_trigger_work_and_not_proposals(session):
    """`atlas_tasks` is what the classic TUI draws as OPEN TASKS and RECENT
    TASKS, beside an AUTHORITY panel about what Atlas starts unattended. One
    ask in Research mints ~5 proposals, which read there as open autonomous
    work nobody authorised and push real trigger work out of the window."""
    session.atlas.set_mode("research")
    session.registry.create_atlas_task(
        "a-trigger", f"regime_flip|{date.today().isoformat()}|SPY|abc",
        "regime_flip", {"why": "flip"}, "regime_review")
    session.atlas_actionables(True)
    assert len(session.registry.list_atlas_tasks(200, origin="proposal")) > 1

    _, snap = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})

    assert [t["task_id"] for t in snap["atlas_tasks"]] == ["a-trigger"]
    # And the proposals are still served — on the block that is about them.
    assert snap["actionables"]["items"]


def test_a_snapshot_item_and_a_menu_item_are_the_same_shape(session):
    """One conceptual item, one shape. Otherwise a client has to make fields
    optional for a reason that is an accident of which surface answered."""
    session.atlas.set_mode("research")
    menu = session.atlas_actionables(True)
    _, snap = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})

    assert snap["actionables"]["items"]
    assert {frozenset(item) for item in snap["actionables"]["items"]} == {
        frozenset(item) for item in menu["items"]}


def test_a_proposal_never_spends_the_unattended_daily_budget(session):
    """Two independent locks keep a proposal out of `_within_daily_budget`:
    its kind sits outside `_WORKFLOW_TRIGGERS`, and the scan also filters
    `origin="trigger"` in SQL, so a proposal is excluded regardless of kind.
    This pins the resulting property — a morning's worth of unapproved
    proposals never exhausts `max_autonomous_workflows_per_day` — not either
    lock by itself; only breaking BOTH would turn the suite red."""
    session.atlas.set_mode("research")
    today = date.today().isoformat()
    offered = [i for i in session.atlas_actionables(True)["items"] if i["startable"]]
    assert len(offered) > session.atlas.config.max_autonomous_workflows_per_day

    assert session.atlas._within_daily_budget(today) is True
    facts = session.atlas_facts(True)
    facts["portfolio"]["drawdown_tier"] = "control"
    created = session.atlas.observe(facts, trading_date=today)["created_tasks"]

    assert [t["trigger"] for t in created] == ["drawdown_control"]


def test_the_gate_still_sees_queued_work_behind_a_long_history(session):
    """Raising the scan window only postponed the burial: Research offers seven
    templates, nothing deletes a task row, and at ~28 days of daily asking the
    window is proposals again. A queued-only scan is bounded by work that is
    actually waiting, which does not grow with history."""
    trigger_day = (date.today() - timedelta(days=2)).isoformat()
    session.registry.create_atlas_task(
        "buried-trigger", f"regime_flip|{trigger_day}|SPY|abc", "regime_flip",
        {"why": "flip"}, "regime_review")
    for i in range(250):
        session.registry.create_atlas_task(
            f"spent-{i}", f"proposal:desk_brief|{trigger_day}|SPY|{i}",
            "proposal:desk_brief", {"template_id": "desk_brief"}, "desk_brief",
            origin="proposal")
        session.registry.update_atlas_task(f"spent-{i}", status="completed")
    session.atlas.set_mode("research")

    entries = session.atlas.startable_tasks(session.atlas_facts(True))

    assert [entry["task_id"] for entry in entries] == ["buried-trigger"]


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


# --- an approved action that could not be driven is driven later -------------

def _drive_screen(session, monkeypatch, verdict=lambda roles: (True, "")):
    """Pin the driver's own pre-screen for the sweep.

    `drive_pending_tasks` asks `available()` before it spends an attempt, so
    without this every sweep test below would be a claim about whether `claude`
    is on the machine running it rather than about the sweep. `verdict` takes
    the graph's roles, because the screen is per-graph and that is the whole
    reason a refusal must not cost the sweep its budget.
    """
    monkeypatch.setattr(
        session.coordinator_driver, "available",
        lambda roles=(), **kwargs: verdict(tuple(roles)))


def test_an_approved_action_that_could_not_be_driven_is_driven_later(session, monkeypatch):
    """One coordinator at a time means an approval can land while the slot is
    busy. Registering a workflow is not running it — without this sweep the
    task sits in `running` with nothing walking its phases."""
    _drive_screen(session, monkeypatch)
    drove: list[str] = []
    monkeypatch.setattr(session, "drive_workflow",
                        lambda wid, goal, roles=(): drove.append(wid) or
                        {"driving": True})
    workflow_id = session.registry.start_workflow(
        "portfolio_review", {"goal": "g", "as_of": "2026-08-06",
                             "universe": "core", "offline": True})["workflow_id"]
    session.registry.create_atlas_task(
        "task-parked", "proposal:regime_review|2026-08-06|SPY|regime_review",
        "proposal:regime_review", {}, "regime_review", origin="proposal")
    session.registry.update_atlas_task("task-parked", status="running",
                                     workflow_id=workflow_id)

    swept = session.drive_pending_tasks()

    assert drove == [workflow_id]
    assert swept == [{"task_id": "task-parked", "workflow_id": workflow_id,
                      "driving": True}]


def test_the_sweep_drives_one_at_a_time(session, monkeypatch):
    """The owner has one coordinator slot; driving two would be the bug
    invariant 9 already caught once."""
    _drive_screen(session, monkeypatch)
    drove: list[str] = []
    monkeypatch.setattr(session, "drive_workflow",
                        lambda wid, goal, roles=(): drove.append(wid) or
                        {"driving": True})
    for n in (1, 2):
        workflow_id = session.registry.start_workflow(
            "portfolio_review", {"goal": f"g{n}", "as_of": "2026-08-06",
                                 "universe": "core", "offline": True},
            phases=("news-analyst",))["workflow_id"]
        session.registry.create_atlas_task(
            f"task-parked-{n}", f"proposal:news_read|2026-08-06|SPY|{n}",
            "proposal:news_read", {}, "news_read", origin="proposal")
        session.registry.update_atlas_task(f"task-parked-{n}", status="running",
                                         workflow_id=workflow_id)

    swept = session.drive_pending_tasks()

    assert len(drove) == 1
    assert len(swept) == 1


def test_a_busy_coordinator_is_not_interrupted(session, monkeypatch):
    """The slot is taken. Spawning beside it is exactly the second coordinator
    the driver's lock exists to refuse — the sweep must not even ask."""
    monkeypatch.setattr(session, "coordinator_status", lambda: {"driving": True})
    drove: list[str] = []
    monkeypatch.setattr(session, "drive_workflow",
                        lambda wid, goal, roles=(): drove.append(wid) or
                        {"driving": True})
    workflow_id = session.registry.start_workflow(
        "portfolio_review", {"goal": "g", "as_of": "2026-08-06",
                             "universe": "core", "offline": True})["workflow_id"]
    session.registry.create_atlas_task(
        "task-parked", "proposal:regime_review|2026-08-06|SPY|regime_review",
        "proposal:regime_review", {}, "regime_review", origin="proposal")
    session.registry.update_atlas_task("task-parked", status="running",
                                     workflow_id=workflow_id)

    assert session.drive_pending_tasks() == []
    assert drove == []


def test_the_sweep_never_drives_work_nobody_approved(session, monkeypatch):
    """The other side of the status filter, and the envelope in one test: a
    queued task has not been through the gate. Driving its workflow would make
    the sweep a second way to start unapproved work."""
    drove: list[str] = []
    monkeypatch.setattr(session, "drive_workflow",
                        lambda wid, goal, roles=(): drove.append(wid) or
                        {"driving": True})
    workflow_id = session.registry.start_workflow(
        "portfolio_review", {"goal": "g"}, phases=("news-analyst",))["workflow_id"]
    session.registry.create_atlas_task(
        "task-queued", "proposal:news_read|2026-08-06|SPY|q", "proposal:news_read",
        {}, "news_read", origin="proposal")
    # A workflow binding without the running status: the shape a task carries
    # before the gate has said anything about it.
    session.registry.update_atlas_task("task-queued", workflow_id=workflow_id)

    assert session.drive_pending_tasks() == []
    assert drove == []


def test_a_running_task_with_no_workflow_is_left_alone(session, monkeypatch):
    """A deterministic template concludes inline and binds no workflow. There
    is nothing to walk, and `str(None)` would hand the driver the id "None"."""
    drove: list[str] = []
    monkeypatch.setattr(session, "drive_workflow",
                        lambda wid, goal, roles=(): drove.append(wid) or
                        {"driving": True})
    session.registry.create_atlas_task(
        "task-inline", "proposal:desk_brief|2026-08-06|SPY|desk_brief",
        "proposal:desk_brief", {}, "desk_brief", origin="proposal")
    session.registry.update_atlas_task("task-inline", status="running")

    assert session.drive_pending_tasks() == []
    assert drove == []


def test_a_finished_workflow_is_left_for_reconciliation(session, monkeypatch):
    """`reconcile_tasks` resolves this task from the workflow's own terminal
    state. Spawning a coordinator for a run that is already complete would
    re-walk finished phases against a referee verdict that has been given."""
    drove: list[str] = []
    monkeypatch.setattr(session, "drive_workflow",
                        lambda wid, goal, roles=(): drove.append(wid) or
                        {"driving": True})
    workflow_id = session.registry.start_workflow(
        "portfolio_review", {"goal": "g"}, phases=("news-analyst",))["workflow_id"]
    session.registry.update_workflow_phase(
        workflow_id, "news-analyst", "done", summary="ok",
        artifacts={"news_view": "the record supports a narrow reading"})
    assert session.registry.get_workflow(workflow_id)["status"] == "complete"
    session.registry.create_atlas_task(
        "task-done", "proposal:news_read|2026-08-06|SPY|d", "proposal:news_read",
        {}, "news_read", origin="proposal")
    session.registry.update_atlas_task("task-done", status="running",
                                     workflow_id=workflow_id)

    assert session.drive_pending_tasks() == []
    assert drove == []


def test_a_workflow_that_vanished_is_left_for_reconciliation(session, monkeypatch):
    """Failing the task is reconciliation's call, not the sweep's — and there
    is no goal, no graph and no phases to drive here anyway."""
    drove: list[str] = []
    monkeypatch.setattr(session, "drive_workflow",
                        lambda wid, goal, roles=(): drove.append(wid) or
                        {"driving": True})
    session.registry.create_atlas_task(
        "task-orphaned", "proposal:news_read|2026-08-06|SPY|o", "proposal:news_read",
        {}, "news_read", origin="proposal")
    session.registry.update_atlas_task("task-orphaned", status="running",
                                     workflow_id="no-such-workflow")

    assert session.drive_pending_tasks() == []
    assert drove == []
    assert session.registry.get_atlas_task("task-orphaned")["status"] == "running"


def test_the_sweep_drives_the_workflow_with_its_own_goal_and_graph(session, monkeypatch):
    """What the coordinator is told is what the workflow says, not a blank.
    The roles decide which provider serves the dispatch — an empty tuple is an
    unnamed graph, which routes every one-role read to the claude coordinator."""
    _drive_screen(session, monkeypatch)
    handed: dict = {}
    monkeypatch.setattr(
        session, "drive_workflow",
        lambda wid, goal, roles=(): handed.update(goal=goal, roles=roles) or
        {"driving": True})
    workflow_id = session.registry.start_workflow(
        "portfolio_review", {"goal": "[news_read] read the window"},
        phases=("news-analyst",))["workflow_id"]
    session.registry.create_atlas_task(
        "task-parked", "proposal:news_read|2026-08-06|SPY|news_read",
        "proposal:news_read", {}, "news_read", origin="proposal")
    session.registry.update_atlas_task("task-parked", status="running",
                                     workflow_id=workflow_id)

    session.drive_pending_tasks()

    assert handed["goal"] == "[news_read] read the window"
    assert handed["roles"] == ("news-analyst",)


def _park(session, n: int, *, phases=("news-analyst",)) -> str:
    """One approved task bound to a workflow nothing is walking."""
    workflow_id = session.registry.start_workflow(
        "portfolio_review", {"goal": f"g{n}"}, phases=phases)["workflow_id"]
    session.registry.create_atlas_task(
        f"task-parked-{n}", f"proposal:news_read|2026-08-06|SPY|{n}",
        "proposal:news_read", {}, "news_read", origin="proposal")
    session.registry.update_atlas_task(f"task-parked-{n}", status="running",
                                     workflow_id=workflow_id)
    return workflow_id


def test_a_refused_candidate_does_not_park_the_one_behind_it(session, monkeypatch):
    """`available()` answers per graph: a one-role read served by a daemon that
    is down is refused while a claude review would start. Stopping at the first
    refusal would leave every later approval parked forever — this method's own
    bug, reintroduced for a subset."""
    _drive_screen(session, monkeypatch)
    refused = _park(session, 1)
    drivable = _park(session, 2)
    monkeypatch.setattr(
        session, "drive_workflow",
        lambda wid, goal, roles=(): {"driving": wid != refused,
                                     "reason": "the daemon is down"})

    swept = session.drive_pending_tasks()

    assert [(row["workflow_id"], row["driving"]) for row in swept] == [
        (refused, False), (drivable, True)]


def test_the_sweep_asks_the_oldest_parked_approval_first(session, monkeypatch):
    """The registry lists newest-first. Swept in that order, a young doomed
    workflow is retried ahead of an older healthy one on every beat."""
    _drive_screen(session, monkeypatch)
    asked: list[str] = []
    monkeypatch.setattr(session, "drive_workflow",
                        lambda wid, goal, roles=(): asked.append(wid) or
                        {"driving": False, "reason": "no"})
    oldest = _park(session, 1)
    newest = _park(session, 2)
    assert [t["task_id"] for t in session.registry.list_atlas_tasks(10)] == [
        "task-parked-2", "task-parked-1"], "premise: the registry lists newest first"

    session.drive_pending_tasks()

    assert asked == [oldest, newest]


def test_a_drive_that_fails_past_the_screen_is_asked_a_bounded_number_of_times(
    session, monkeypatch,
):
    """The cap bounds real `drive()` calls: the screen said yes and the spawn
    failed anyway — a coordinator that could not start, or the slot taken
    between the two. Uncapped, that is one audit row per running task per beat,
    forever."""
    _drive_screen(session, monkeypatch)
    asked: list[str] = []
    monkeypatch.setattr(session, "drive_workflow",
                        lambda wid, goal, roles=(): asked.append(wid) or
                        {"driving": False,
                         "reason": "coordinator failed to start"})
    for n in range(ui_server._DRIVE_ATTEMPTS_PER_SWEEP + 1):
        _park(session, n)

    swept = session.drive_pending_tasks()

    assert len(asked) == ui_server._DRIVE_ATTEMPTS_PER_SWEEP
    assert len(swept) == ui_server._DRIVE_ATTEMPTS_PER_SWEEP


def test_a_screened_out_candidate_costs_the_sweep_nothing(session, monkeypatch):
    """The starvation this screen removes. `available()` is per-graph and
    deterministic in a stable environment, so three parked one-role tasks whose
    daemon is down were re-attempted on every beat — spending the whole budget
    on the same three refusals — while a fourth parked task that WOULD drive
    was never reached on any beat, forever."""
    for n in range(1, ui_server._DRIVE_ATTEMPTS_PER_SWEEP + 1):
        _park(session, n)
    drivable = _park(session, 9, phases=("analyst", "challenger", "optimizer",
                                         "referee", "reporter"))
    _drive_screen(session, monkeypatch,
                  verdict=lambda roles: (roles != ("news-analyst",),
                                         "the daemon is down"))
    drove: list[str] = []
    monkeypatch.setattr(session, "drive_workflow",
                        lambda wid, goal, roles=(): drove.append(wid) or
                        {"driving": True})

    swept = session.drive_pending_tasks()

    assert drove == [drivable]
    # No audit row and no attempt spent on the three the driver would refuse.
    assert [row["workflow_id"] for row in swept] == [drivable]


def test_a_later_sweep_drives_the_next_parked_task(session, monkeypatch):
    """The first drive takes the slot, so the second waits — but it must not
    wait forever. Once the first workflow resolves, the next beat picks up the
    one behind it."""
    _drive_screen(session, monkeypatch)
    drove: list[str] = []
    monkeypatch.setattr(session, "drive_workflow",
                        lambda wid, goal, roles=(): drove.append(wid) or
                        {"driving": True})
    first = _park(session, 1)
    second = _park(session, 2)

    session.drive_pending_tasks()
    assert drove == [first]

    # The first run finishes; reconciliation completes its task, and the slot is
    # free again by the time the next beat asks.
    session.registry.update_workflow_phase(
        first, "news-analyst", "done", summary="ok",
        artifacts={"news_view": "the record supports a narrow reading"})
    session.atlas.reconcile_tasks()

    session.drive_pending_tasks()

    assert drove == [first, second]
    assert session.registry.get_atlas_task("task-parked-1")["status"] == "completed"


def test_an_undriven_dispatch_reports_that_it_was_not_driven(session, monkeypatch):
    """A refusal is not a drive. The sweep reports what actually happened so
    the next beat can try again rather than believing the slot is taken."""
    _drive_screen(session, monkeypatch)
    monkeypatch.setattr(session, "drive_workflow",
                        lambda wid, goal, roles=(): {
                            "driving": False, "reason": "the `claude` CLI is not on PATH"})
    workflow_id = session.registry.start_workflow(
        "portfolio_review", {"goal": "g"}, phases=("news-analyst",))["workflow_id"]
    session.registry.create_atlas_task(
        "task-parked", "proposal:news_read|2026-08-06|SPY|news_read",
        "proposal:news_read", {}, "news_read", origin="proposal")
    session.registry.update_atlas_task("task-parked", status="running",
                                     workflow_id=workflow_id)

    assert session.drive_pending_tasks() == [
        {"task_id": "task-parked", "workflow_id": workflow_id, "driving": False}]


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


def test_news_payload_separates_holding_stories_from_macro_context(session):
    """A cross-asset desk gets almost no symbol-tagged coverage.

    Benzinga tags US equities; six of this desk's seven ETFs return nothing. If
    untagged macro items were dropped, the desk would report "quiet" for a
    market that was not quiet at all — it simply was not naming these tickers.
    Macro items are kept, and labelled so nothing mistakes one for evidence
    about a holding.
    """
    session.fetch_desk_news(True)
    out = session.news_payload(True)
    assert out["counts"]["total"] == len(out["items"])
    assert out["counts"]["holding"] + out["counts"]["macro"] == out["counts"]["total"]
    for row in out["items"]:
        assert row["scope"] == ("holding" if row["tickers"] else "macro")


def test_news_payload_names_the_holdings_no_story_mentioned(session):
    # "0 stories about your holdings" and "the market was quiet" are different
    # facts, and only the coverage list can tell them apart.
    session.fetch_desk_news(True)
    out = session.news_payload(True)
    universe = set(session.mandate.universe_whitelist)
    assert {c["ticker"] for c in out["coverage"]} == universe
    assert set(out["uncovered"]) <= universe
    for ticker in out["uncovered"]:
        assert next(c for c in out["coverage"] if c["ticker"] == ticker)["stories"] == 0


def test_news_route_is_cache_only_and_never_fetches_under_the_lock(session):
    """A cold desk answers, and answers empty, rather than fetching.

    `news_payload` is reached from `tui_snapshot`, which runs under the
    dispatch lock, and `fetch_desk_news` is the seam the network lives behind.
    Filling the window from here — even "only when offline", since offline is a
    runtime value — would block every request on a slow provider.
    """
    def forbidden(*_a, **_k):
        raise AssertionError("news_payload fetched under the dispatch lock")

    session.fetch_desk_news = forbidden
    status, out = handle_api(session, "GET", "/api/news", {}, {})
    assert status == 200
    assert out["items"] == []
    # The window is filled by the heartbeat or by an explicit ?refresh=1, which
    # the HTTP handler runs outside the lock.
    assert out["counts"]["total"] == 0


def test_the_news_window_rides_the_one_consistent_snapshot(session):
    snap = session.tui_snapshot(True)
    assert "news" in snap and "coverage" in snap["news"]


def test_the_desk_read_carries_qualitative_signals(session):
    session.fetch_desk_news(True)
    read = session.compose_desk_read(True, prefetched_news=session.desk_news_window())
    qual = read["qualitative_signals"]
    assert {s["name"] for s in qual["signals"]} == {
        "coverage_breadth", "asset_class_reach", "attention_concentration",
        "corroboration_ratio", "publisher_concentration", "window_age_hours"}
    assert session.atlas_facts(True)["news_window_sufficient"] is qual["sufficient"]


def test_a_failed_recompose_zeroes_the_signals_rather_than_carrying_them(session):
    """Stale coverage is worse than none — it is a number the desk cannot stand
    behind, rendered as though it could."""
    session.fetch_desk_news(True)
    session.compose_desk_read(True, prefetched_news=session.desk_news_window())
    assert session.desk_read(True)["qualitative_signals"]["sufficient"] is True

    session.mark_desk_read_stale("grounding rejected a malformed record")
    qual = session.desk_read(True)["qualitative_signals"]
    assert qual["sufficient"] is False
    assert all(s["value"] is None for s in qual["signals"])
    assert all(s["state"] == "no_window" for s in qual["signals"])


# --- the reasoner's surface ---------------------------------------------------


def test_the_gate_input_stays_narrow(session):
    """`atlas_facts` feeds `check_startable`, the authority gate.

    A gate whose input is narrow, boolean and stable is auditable; one reading a
    large free-form context is not. Enriching this surface — the obvious move
    when Atlas starts reasoning — would quietly put the gate in the same
    epistemic class as the thing it exists to constrain. The reasoning surface
    is `atlas_context`; this one is pinned deliberately.
    """
    facts = session.atlas_facts(True)
    assert set(facts) == {
        "universe", "data", "portfolio", "regime", "open_workflows",
        "pending_approvals", "order_anomaly", "news_window_sufficient",
        "news_window_items",
    }
    # Scalars and small records only — nothing a reasoner would need, and
    # nothing whose meaning depends on reading prose.
    assert isinstance(facts["regime"]["flip"], bool)
    assert isinstance(facts["news_window_items"], int)


def test_atlas_context_carries_content_the_gate_facts_do_not(session):
    """A boolean cannot be reasoned about. This is the whole point of the split."""
    session.fetch_desk_news(True)
    ctx = session.atlas_context(True)

    readings = ctx["regime_panel"]["readings"]
    assert len(readings) == 5
    # Each indicator explains itself: state plus its own trailing threshold,
    # percentile and the sentence saying what the number means.
    for reading in readings:
        assert reading["reasoning"]
        assert reading["state"] in {"calm", "stress"}
        assert reading["threshold"] is not None

    # The six unsigned news properties, each with its own reason.
    assert len(ctx["qualitative_signals"]["signals"]) == 6
    assert ctx["news"]["headlines"]
    assert "coverage" in ctx["news"]

    # The gate's own view is carried verbatim, so the reasoner can see exactly
    # what the deterministic layer will permit rather than guessing.
    assert ctx["gate_facts"] == session.atlas_facts(True)


def test_atlas_context_shows_what_the_gate_would_refuse(session):
    """The reasoner must argue within its authority, not propose refused work."""
    facts = session.atlas_facts(True)
    facts["regime"]["flip"] = True
    session.atlas.observe(facts, trading_date="2026-07-31")
    session.atlas.set_mode("observe")

    startable = session.atlas_context(True)["startable"]
    assert startable and all(not s["startable"] for s in startable)
    assert all(s["reason"] for s in startable)


def test_the_absent_risk_profile_is_named_not_omitted(session):
    # Omitting it would let a reasoner assume a default it was never given.
    assert session.atlas_context(True)["mandate"]["risk_profile"] is None


def test_atlas_context_survives_a_broken_regime_panel(session):
    """A broken panel is a fact about the desk, not a reason to have no context."""
    def boom(_offline):
        raise RuntimeError("panel unavailable")

    session.regime_panel = boom
    ctx = session.atlas_context(True)
    assert ctx["regime_panel"]["readings"] == []
    assert "panel unavailable" in ctx["regime_panel"].get("error", "")
    # Everything else still composes.
    assert ctx["gate_facts"] and "news" in ctx


def test_atlas_context_route_is_reachable(session):
    status, ctx = handle_api(session, "GET", "/api/atlas/context", {}, {})
    assert status == 200 and "regime_panel" in ctx


def test_atlas_context_names_a_board_that_never_ran(session):
    # A desk that never measured its predictors must say so, not show zeros.
    assert session.atlas_context(True)["predictors"] == {"status": "never_ran"}


def test_atlas_context_carries_the_predictor_board(session):
    run_id = session.registry.log_run("predictor_board", {
        "as_of": "2026-07-01",
        "source": "synthetic",
        "board": {
            "baseline": "ridge:none",
            "champion": "kernel:zz",
            "admitted_any": True,
            "ranking": ["kernel:zz", "ridge:none"],
            "models": [
                {"model_id": "kernel:zz", "mean_ic": 0.09,
                 "ic_stability": 1.4, "usable": True,
                 "delta_mean_ic_vs_baseline": 0.03,
                 "paired_t_vs_baseline": 2.1},
                {"model_id": "ridge:none", "mean_ic": 0.06,
                 "ic_stability": 1.1, "usable": True,
                 "delta_mean_ic_vs_baseline": 0.0,
                 "paired_t_vs_baseline": None},
            ],
        },
        "dsr_trial_counted": False,
    })

    predictors = session.atlas_context(True)["predictors"]
    assert predictors["status"] == "ok"
    assert predictors["run_id"] == run_id
    assert predictors["as_of"] == "2026-07-01"
    assert predictors["champion"]["model_id"] == "kernel:zz"
    assert predictors["champion"]["usable"] is True
    assert predictors["baseline"]["mean_ic"] == 0.06
    assert predictors["admitted_any"] is True
    assert predictors["best_delta_vs_baseline"] == 0.03
    # A number, not a judgment: whether it is too old is the reasoner's call.
    assert isinstance(predictors["age_days"], int)


def test_atlas_context_names_an_unreadable_board(session):
    session.registry.log_run(
        "predictor_board", {"as_of": "2026-07-01", "board": "corrupt"}
    )
    predictors = session.atlas_context(True)["predictors"]
    assert predictors["status"] == "unreadable"
    assert predictors["run_id"]


# --- the predictor board on screen -------------------------------------------
#
# The board is the whole quantum feature-augmentation lane, and until now it
# had no web surface at all: no route, and nothing in index.html. An operator
# could not see whether the augmented models were earning their place, and the
# only way to run one was a POST to /api/lab/ that no client issued.


def _board_run(session, **over):
    """A board row shaped exactly like `run_predictor_board`'s output."""
    board = {
        "n_obs": 671, "n_folds": 5, "baseline": "ridge:none",
        "champion": "kernel:angle", "admitted_any": True,
        "target": "next_21d_equal_weight_realized_vol",
        "horizon_days": 21, "embargo_days": 21,
        "kernels": ["linear", "angle", "zz"],
        "admission": {"mean_ic_strictly_above": 0.03,
                      "ic_stability_strictly_above": 0.5},
        "ranking": ["kernel:angle", "ridge:none"],
        "models": [
            {"model_id": "kernel:angle", "family": "kernel", "variant": "angle",
             "mean_ic": 0.178, "ic_std": 0.33, "ic_stability": 0.54,
             "usable": True, "delta_mean_ic_vs_baseline": 0.068,
             "wins_vs_baseline": 3, "paired_t_vs_baseline": 0.237,
             "per_fold": [{"fold": 1, "ic": 0.324}, {"fold": 2, "ic": 0.531},
                          {"fold": 3, "ic": 0.471}, {"fold": 4, "ic": -0.239},
                          {"fold": 5, "ic": -0.195}]},
            {"model_id": "ridge:none", "family": "ridge", "variant": "none",
             "mean_ic": 0.110, "ic_std": 0.41, "ic_stability": 0.27,
             "usable": False, "delta_mean_ic_vs_baseline": 0.0,
             "wins_vs_baseline": 0, "paired_t_vs_baseline": None,
             "per_fold": [{"fold": i, "ic": 0.1} for i in range(1, 6)]},
        ],
    }
    board.update(over)
    return session.registry.log_run("predictor_board", {
        "as_of": "2026-07-30", "source": "yfinance", "universe": "core",
        "board": board, "dsr_trial_counted": False,
    })


def test_the_predictor_board_has_a_route_of_its_own(session):
    """`atlas_context` carried a summary for the reasoner, but no client could
    ask for the board itself, so the augmented lane had no screen."""
    run_id = _board_run(session)
    status, payload = handle_api(session, "GET", "/api/research/predictors",
                                 {}, {})
    assert status == 200
    assert payload["status"] == "ok"
    assert payload["run_id"] == run_id
    # Every model, not just the champion: the ranking IS the finding.
    assert {m["model_id"] for m in payload["models"]} == {
        "kernel:angle", "ridge:none"}


def test_the_route_says_which_models_are_the_augmented_lane(session):
    """A screen showing `kernel:angle` answers "is the quantum augmentation
    working" only if something on it says the kernel and groupwise families
    ARE that augmentation and ridge:none is the control."""
    _board_run(session)
    _, payload = handle_api(session, "GET", "/api/research/predictors", {}, {})
    by_id = {m["model_id"]: m for m in payload["models"]}
    assert by_id["kernel:angle"]["augmented"] is True
    assert by_id["ridge:none"]["augmented"] is False
    assert by_id["ridge:none"]["is_baseline"] is True
    assert "quantum" in payload["lane"].lower()


def test_the_route_carries_the_bar_and_the_folds_not_just_the_verdict(session):
    """Same rule the reasoner block follows: a verdict without its threshold,
    and a t-statistic without its n, are not evidence."""
    _board_run(session)
    _, payload = handle_api(session, "GET", "/api/research/predictors", {}, {})
    assert payload["admission"]["mean_ic_strictly_above"] == 0.03
    assert payload["n_folds"] == 5 and payload["n_obs"] == 671
    champ = next(m for m in payload["models"]
                 if m["model_id"] == "kernel:angle")
    assert champ["per_fold"] == [0.324, 0.531, 0.471, -0.239, -0.195]
    assert champ["negative_folds"] == 2
    # 0.237 on 5 folds cannot separate anything from anything, and the payload
    # says so rather than leaving a bare number to be read as a win.
    assert champ["significant"] is False


def test_a_desk_that_never_ran_the_board_says_so_rather_than_404(session):
    """An empty research lane is a fact about the desk, and a 404 would read
    as a broken endpoint instead."""
    status, payload = handle_api(session, "GET", "/api/research/predictors",
                                 {}, {})
    assert status == 200
    assert payload["status"] == "never_ran"
    assert payload["models"] == []
    assert payload["reason"]


def test_a_board_that_admitted_nothing_is_reported_as_a_result(session):
    _board_run(session, champion=None, admitted_any=False,
               models=[{"model_id": "ridge:none", "family": "ridge",
                        "variant": "none", "mean_ic": 0.01,
                        "ic_stability": 0.02, "usable": False,
                        "delta_mean_ic_vs_baseline": 0.0,
                        "paired_t_vs_baseline": None, "per_fold": []}])
    _, payload = handle_api(session, "GET", "/api/research/predictors", {}, {})
    assert payload["status"] == "ok"
    assert payload["admitted_any"] is False
    assert payload["champion"] is None
    assert payload["reason"], "an empty result still states what happened"


def test_the_linear_kernel_is_not_labelled_a_quantum_map(session):
    """`kernel:linear` is in the `kernel` family but carries NO quantum feature
    map: `quantum_gram` returns early on it, so it is the dual of the plain
    ridge baseline and comes back bit-identical to `ridge:none`. Labelling it
    "quantum-augmented" would put a control in the treatment arm and let the
    lane claim a row it did not earn. Only the angle and ZZ maps are quantum.
    """
    _board_run(session, models=[
        {"model_id": "kernel:linear", "family": "kernel", "variant": "linear",
         "mean_ic": 0.110, "ic_stability": 0.27, "usable": False,
         "delta_mean_ic_vs_baseline": 0.0, "paired_t_vs_baseline": 0.0,
         "per_fold": []},
        {"model_id": "kernel:angle", "family": "kernel", "variant": "angle",
         "mean_ic": 0.178, "ic_stability": 0.54, "usable": True,
         "delta_mean_ic_vs_baseline": 0.068, "paired_t_vs_baseline": 0.237,
         "per_fold": []},
        {"model_id": "groupwise:angle_zz", "family": "groupwise",
         "variant": "angle_zz", "mean_ic": 0.026, "ic_stability": 0.1,
         "usable": False, "delta_mean_ic_vs_baseline": -0.084,
         "paired_t_vs_baseline": -0.286, "per_fold": []},
    ])
    _, payload = handle_api(session, "GET", "/api/research/predictors", {}, {})
    by_id = {m["model_id"]: m for m in payload["models"]}
    assert by_id["kernel:linear"]["augmented"] is False
    assert by_id["kernel:linear"]["control_note"], (
        "a kernel-family row that is really a control must say why")
    assert by_id["kernel:angle"]["augmented"] is True
    assert by_id["groupwise:angle_zz"]["augmented"] is True


@pytest.mark.parametrize("variant,augmented", [
    ("linear", False), ("angle", True), ("zz", True), ("angle_zz", True),
    ("none", False),
])
def test_augmentation_is_decided_by_the_feature_map_not_the_family(
        session, variant, augmented):
    """The invariant over the whole variant space: a model is in the augmented
    lane iff its variant names a quantum feature map, whatever family it is
    filed under."""
    for family in ("kernel", "groupwise", "ridge"):
        _board_run(session, models=[
            {"model_id": f"{family}:{variant}", "family": family,
             "variant": variant, "mean_ic": 0.1, "ic_stability": 0.3,
             "usable": False, "delta_mean_ic_vs_baseline": 0.0,
             "paired_t_vs_baseline": None, "per_fold": []}])
        _, payload = handle_api(session, "GET", "/api/research/predictors",
                                {}, {})
        assert payload["models"][0]["augmented"] is augmented


def _stalled_workflow(session):
    """A workflow that got four phases deep and then blocked, as live ones do.

    The referee phase goes through the real verdict path rather than a stubbed
    id: the registry refuses a verdict_id that is not a persisted PASS bound to
    these exact targets, and that refusal is worth honouring in a fixture.
    """
    targets = {"SPY": 1.0}
    wf = session.registry.start_workflow(
        "portfolio_review", {"as_of": "2026-08-03", "universe": "core",
                             "goal": "[risk_event] brief the human"})
    wid = wf["workflow_id"]
    session.registry.update_workflow_phase(
        wid, "analyst", "done", "Regime = STRESS (3/5 detectors)",
        {"moment_set_id": "m1", "objective_id": "o1", "decision_id": "d1",
         "regime": "stress", "regime_summary": "3 of 5 detectors in stress"})
    session.registry.update_workflow_phase(
        wid, "challenger", "done", "Challenge SUSTAINED; analyst amended",
        {"challenger_view": "sustained; window moved 504d -> 756d"})
    session.registry.update_workflow_phase(
        wid, "optimizer", "done", "Solved amended objective",
        {"targets": targets, "algorithm_id": "hrp"})
    vid = session.registry.log_verdict(
        "d1", "PASS", ["constraints clean"], targets=targets)
    session.registry.update_workflow_phase(
        wid, "referee", "done", f"PASS (verdict {vid})",
        {"verdict": "PASS", "verdict_id": vid, "targets": targets,
         "decision_id": "d1"})
    session.registry.update_workflow_phase(
        wid, "reporter", "blocked",
        "Memo compiled and referee PASS reported, but the paper-trade "
        "preview is blocked: the permit does not allow it",
        {"recommendation": "hold"})
    return wid


def test_atlas_can_see_the_workforce_it_manages(session):
    """Atlas is the manager. A manager whose context contains no key naming a
    workflow, step, phase or agent cannot answer "what is my desk doing"."""
    wid = _stalled_workflow(session)
    ctx = session.atlas_context(True)
    assert "workforce" in ctx, (
        "the reasoning surface carried regime, news, predictors and decisions "
        "but nothing about the agents Atlas directs")
    wf = ctx["workforce"]
    ids = [w["workflow_id"] for w in wf["workflows"]]
    assert wid in ids


def test_a_stalled_workflow_arrives_with_the_step_that_stalled_it(session):
    """The live blocked runs each carried a written reason on the failing
    step. A count of blocked workflows is not that reason; the words are."""
    wid = _stalled_workflow(session)
    wf = session.atlas_context(True)["workforce"]
    row = next(w for w in wf["workflows"] if w["workflow_id"] == wid)
    assert row["status"] == "blocked"
    assert row["stalled_at"]["phase"] == "reporter"
    assert row["stalled_at"]["agent"] == "reporter"
    assert "permit does not allow it" in row["stalled_at"]["summary"]
    # And the phases that DID succeed, so Atlas knows how far the work got
    # rather than only that it stopped.
    assert row["completed_phases"] == [
        "analyst", "challenger", "optimizer", "referee"]


def test_a_healthy_workflow_is_not_reported_as_stalled(session):
    """The absence of a stall is stated, not left as a missing key that reads
    the same as an unknown one."""
    wf_row = session.registry.start_workflow(
        "portfolio_review", {"as_of": "2026-08-03", "universe": "core"})
    wid = wf_row["workflow_id"]
    session.registry.update_workflow_phase(
        wid, "analyst", "done", "ok",
        {"moment_set_id": "m1", "objective_id": "o1", "decision_id": "d1",
         "regime": "calm", "regime_summary": "no detector in stress"})
    wf = session.atlas_context(True)["workforce"]
    row = next(w for w in wf["workflows"] if w["workflow_id"] == wid)
    assert row["stalled_at"] is None
    assert row["status"] == "running"


def test_the_workforce_block_counts_what_needs_a_human(session):
    """Three blocked runs sitting on a desk is the single most actionable
    thing about it, and it should not require Atlas to tally a list itself."""
    _stalled_workflow(session)
    _stalled_workflow(session)
    wf = session.atlas_context(True)["workforce"]
    assert wf["needs_attention"] == 2
    assert wf["counts"]["blocked"] == 2


def test_an_empty_desk_says_so_rather_than_omitting_the_key(session):
    wf = session.atlas_context(True)["workforce"]
    assert wf["workflows"] == []
    assert wf["needs_attention"] == 0
    assert wf["reason"], "no runs is a fact about the desk, and it is stated"


def test_the_workforce_has_a_route_and_a_panel(session):
    """The desk's ten runs were reachable only through /api/workflows, which
    no page called: `grep workflow qlab/ui/index.html` returned zero hits."""
    wid = _stalled_workflow(session)
    status, payload = handle_api(session, "GET", "/api/workforce", {}, {})
    assert status == 200
    assert wid in [w["workflow_id"] for w in payload["workflows"]]
    assert payload["needs_attention"] == 1


def test_an_abandoned_run_stopped_but_is_not_awaiting_the_operator(session):
    """A stall box on an abandoned run reads as "act on me", and it is not.

    The live desk showed seven stalled runs against six needing attention: the
    seventh was abandoned, a decision the operator had already taken. Where it
    stopped is still worth recording, so the row keeps its `stalled_at` and
    answers the separate question of whether anyone is waiting on a human.
    """
    wid = _stalled_workflow(session)
    session.registry.abandon_workflow(wid, "operator closed it out")
    row = [w for w in session.workforce_summary()["workflows"]
           if w["workflow_id"] == wid][0]
    assert row["stalled_at"] is not None, "where it stopped is still a fact"
    assert row["awaiting_operator"] is False
    assert session.workforce_summary()["needs_attention"] == 0
    # ...and every row answers the question, so absent never reads as false.
    assert all("awaiting_operator" in w
               for w in session.workforce_summary()["workflows"])


def test_a_blocked_run_is_awaiting_the_operator(session):
    wid = _stalled_workflow(session)
    row = [w for w in session.workforce_summary()["workflows"]
           if w["workflow_id"] == wid][0]
    assert row["awaiting_operator"] is True


def test_the_agent_stream_has_a_route(session):
    """The coordinator republishes every agent event onto the audit bus; this
    is the route every client renders it from."""
    session.registry.record_event("atlas_coordinator_event", {
        "workflow_id": "wf-1", "event_kind": "tool_start",
        "agent": "moments-analyst", "tool": "Agent", "text": "calling Agent"})
    session.registry.record_event("atlas_coordinator_event", {
        "workflow_id": "wf-1", "event_kind": "text", "agent": "", "tool": "",
        "text": "Realised vol sits in the top decile."})
    status, payload = handle_api(session, "GET", "/api/workforce/stream", {}, {})
    assert status == 200
    kinds = [e["event_kind"] for e in payload["events"]]
    assert "tool_start" in kinds and "text" in kinds
    assert any(e["agent"] == "moments-analyst" for e in payload["events"])
    assert payload["reason"]


def test_an_empty_agent_stream_says_why_rather_than_showing_nothing(session):
    """Nothing recorded and nothing having happened must not look the same."""
    payload = session.agent_stream()
    assert payload["events"] == []
    assert "no coordinator" in payload["reason"].lower()


def test_the_agent_stream_is_not_crowded_out_by_a_noisier_event_kind(session):
    """Filtering a fixed window in Python is not a filter.

    The live desk records a news_archive row per story; 500 of them landed in
    four hours, so reading the newest 500 events and keeping the coordinator
    ones returned nothing on a desk with 31 coordinator events. The panel then
    said "no coordinator has published to this desk's bus", which was a
    confident, wrong reason -- worse than showing nothing.
    """
    session.registry.record_event("atlas_coordinator_event", {
        "workflow_id": "wf-1", "event_kind": "text", "agent": "",
        "tool": "", "text": "the reasoning that must survive the flood"})
    for i in range(600):
        session.registry.record_event("news_archive", {"n": i})
    payload = session.agent_stream()
    assert len(payload["events"]) == 1, payload["reason"]
    assert "flood" in payload["events"][0]["text"]


def test_historic_liveness_rows_are_set_aside_but_counted_not_hidden(session):
    """The bus is durable, so heartbeats recorded before the filter fix stay.

    On the live desk that left the panel at 56 `Claude session task_progress`
    rows against 4 carrying real debate reasoning. The panel sets them aside
    so the reasoning is readable, and says how many it set aside, because
    silently dropping rows from an audit surface is how the desk stops being
    a record of what happened.
    """
    for _ in range(9):
        session.registry.record_event("atlas_coordinator_event", {
            "workflow_id": "wf-1", "event_kind": "session",
            "agent": "", "tool": "", "text": "Claude session task_progress"})
    session.registry.record_event("atlas_coordinator_event", {
        "workflow_id": "wf-1", "event_kind": "text", "agent": "", "tool": "",
        "text": "Challenger has a live, numeric counter-case."})
    payload = session.agent_stream()
    assert [e["event_kind"] for e in payload["events"]] == ["text"]
    assert payload["suppressed_liveness"] == 9
    assert "9" in payload["reason"]
    assert "liveness" in payload["reason"].lower()


def test_heartbeats_only_is_not_reported_as_the_agents_being_silent(session):
    """"Nothing ran" and "something ran and said nothing" are different facts."""
    for _ in range(5):
        session.registry.record_event("atlas_coordinator_event", {
            "workflow_id": "wf-1", "event_kind": "session",
            "agent": "", "tool": "", "text": "Claude session task_progress"})
    payload = session.agent_stream()
    assert payload["events"] == []
    assert "a coordinator ran" in payload["reason"]
    assert "none has run" not in payload["reason"]


def test_the_control_split_matches_what_the_kernel_code_actually_does():
    """The augmented/control split rests on a claim about `quantum_gram`: that
    it returns before the map term for `linear`. That claim is repeated in
    comments in three modules, and a rename or a refactor there would leave
    them confidently describing code that no longer exists -- the same
    producer/consumer drift this whole surface was built to catch.

    So verify the behaviour, not the prose: a linear Gram must equal the raw
    inner product exactly, while angle and zz must not.
    """
    import numpy as np
    from qlab.research.kernels import quantum_gram

    rng = np.random.default_rng(11)
    std = rng.normal(size=(6, 3))
    unit = rng.uniform(-1.0, 1.0, size=(6, 3))
    raw = std @ std.T

    linear = quantum_gram(std, std, unit, unit, "linear")
    assert np.array_equal(linear, raw), (
        "kernel:linear is classified as a control because it carries no "
        "feature map; if quantum_gram now adds one, that classification and "
        "the comments citing it are wrong")
    for kind in ("angle", "zz"):
        mapped = quantum_gram(std, std, unit, unit, kind)
        assert not np.allclose(mapped, raw), (
            f"{kind} is classified as augmented but added nothing to the "
            "raw Gram")


def test_the_summary_carries_whether_the_champion_beat_its_own_null(session):
    """`usable` is a fixed per-model bar applied to a selected maximum.

    Measured: on 100 noise panels the board's own procedure admitted a
    champion 66 times and 84 cleared the 0.03 mean_ic bar. So the bar cannot
    carry the claim, and a summary that ships `usable: true` without the
    null ships the same over-reading this file already fixed once for the
    missing admission bar.
    """
    _board_run(session, champion_established=False,
               selection_null={"trials": 24, "p_value": 0.36,
                               "observed_max_mean_ic": 0.178,
                               "null_median_max_mean_ic": 0.139,
                               "reason": "noise reproduces this routinely"})
    summary = session.predictor_board_summary()
    assert summary["champion_established"] is False
    assert summary["selection_null"]["p_value"] == 0.36


def test_a_board_predating_the_null_says_so_rather_than_claiming_established(session):
    """Absent must not read as either established or refuted."""
    _board_run(session)  # no champion_established key at all
    summary = session.predictor_board_summary()
    assert summary["champion_established"] is None
    assert summary["selection_null"] is None


def test_the_panel_reason_does_not_call_a_noise_champion_admitted_and_stop(session):
    """The operator-facing reason is the one sentence most likely to be read.

    "kernel:angle was admitted: it cleared both admission thresholds" is true
    and, on its own, misleading: 84 of 100 pure-noise panels cleared that
    same mean_ic bar. If the null refuted the champion, the reason has to
    say so in the same breath, not leave it to a field further down.
    """
    _board_run(session, champion_established=False,
               selection_null={"trials": 24, "p_value": 0.36, "exceedances": 8,
                               "p_value_resolution": 0.04,
                               "observed_max_mean_ic": 0.178,
                               "null_median_max_mean_ic": 0.139})
    detail = session.predictor_board_detail()
    assert detail["champion_established"] is False
    assert "not established" in detail["reason"].lower()
    assert "0.36" in detail["reason"]


def test_the_panel_says_when_no_null_was_run_rather_than_implying_one_held(session):
    _board_run(session)
    detail = session.predictor_board_detail()
    assert detail["champion_established"] is None
    assert "not" in detail["reason"].lower()


def test_the_panel_separates_a_withheld_verdict_from_a_null_that_never_ran(session):
    """Both are `champion_established: None` and they are different facts.

    "never null-tested" is true of a board predating the null. It is false of
    a board that ran a 9-trial null, where the test happened and simply could
    not reach alpha. An operator told "never tested" would go looking for a
    missing run instead of raising the trial count.
    """
    _board_run(session, champion_established=None,
               selection_null={"trials": 9, "p_value": 0.10, "exceedances": 0,
                               "p_value_resolution": 0.10,
                               "underpowered_for_alpha": True,
                               "observed_max_mean_ic": 0.31,
                               "null_median_max_mean_ic": 0.088})
    detail = session.predictor_board_detail()
    assert detail["champion_established"] is None
    reason = detail["reason"].lower()
    assert "not tested" not in reason
    assert "withheld" in reason or "cannot" in reason
    assert "9" in detail["reason"]


def test_tui_snapshot_carries_the_predictor_board(session):
    """The workstation renders what the reasoner reads, or the operator is
    the one party at the desk who cannot see the quantum lane's evidence."""
    _board_run(session)
    status, snap = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})
    assert status == 200
    assert snap["predictors"]["champion"]["model_id"] == "kernel:angle"
    assert snap["predictors"]["baseline"]["model_id"] == "ridge:none"


def test_tui_snapshot_names_a_board_that_never_ran(session):
    status, snap = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})
    assert status == 200
    assert snap["predictors"] == {"status": "never_ran"}


def test_tui_snapshot_carries_the_atlas_conversation_unflooded(session):
    """The chat is selected by kind at the store, so a noisy desk cannot push
    it off the payload. 200 unrelated rows land AFTER the exchange; a client
    reading the general `events` window would have lost the entire chat."""
    session.registry.record_event(
        "atlas_message", {"actor": "operator", "text": "what do we hold?"})
    session.registry.record_event(
        "atlas_message", {"actor": "atlas", "text": "Seven ETFs, long only."})
    for i in range(200):
        session.registry.record_event("news_archive", {"returned": i})
    status, snap = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})
    assert status == 200
    texts = [row["payload"]["text"] for row in snap["atlas_chat"]]
    assert texts == ["what do we hold?", "Seven ETFs, long only."]
    assert [row["payload"]["actor"] for row in snap["atlas_chat"]] == [
        "operator", "atlas"]


# -- the remembered posture ------------------------------------------------
def test_a_posture_nobody_chose_is_read_only_and_says_so(session):
    """Unasked and deliberately read-only serve the same ``armed``; only
    ``chosen`` separates them, and a client's startup question keys on it."""
    status, snap = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})
    assert status == 200
    assert snap["posture"] == {"armed": False, "chosen": False}


def test_the_posture_route_records_and_reflows(session):
    status, body = handle_api(
        session, "POST", "/api/desk/posture", {}, {"armed": True})
    assert status == 200
    assert body == {"armed": True, "chosen": True}
    snap = handle_api(session, "GET", "/api/tui", {"offline": ["1"]}, {})[1]
    assert snap["posture"] == {"armed": True, "chosen": True}
    kinds = [e["kind"] for e in session.registry.read_events(20)]
    assert "desk.posture_chosen" in kinds
    # And it outlives this owner: a fresh session reads the same choice.
    from qlab.state.registry import Registry
    revived = UISession(offline_default=True, registry=Registry(":memory:"))
    assert revived.posture_payload() == {"armed": True, "chosen": True}


def test_posting_read_only_is_a_choice_not_a_reset(session):
    """The other side of the boolean: false is recorded, not treated as unset."""
    handle_api(session, "POST", "/api/desk/posture", {}, {"armed": True})
    status, body = handle_api(
        session, "POST", "/api/desk/posture", {}, {"armed": False})
    assert status == 200
    assert body == {"armed": False, "chosen": True}


def test_the_posture_is_persisted_under_the_lock_that_guards_it(session, monkeypatch):
    """Invariant 9: this runtime is threaded, so the disk write and the memory
    write are one critical section. Outside the lock, two concurrent POSTs can
    interleave and leave ``posture.json`` and ``self._posture`` disagreeing —
    a desk that restarts into the posture the *other* operator chose."""
    import qlab.ui.server as server_mod

    held = []
    real = server_mod.save_posture

    def watched(posture):
        # ``threading.Lock`` is not reentrant: if the caller holds it, a
        # non-blocking acquire from this same thread fails. Released again
        # immediately when it does succeed — a probe that kept the lock would
        # deadlock the very call it is reporting on.
        got = session._posture_lock.acquire(blocking=False)
        if got:
            session._posture_lock.release()
        held.append(not got)
        real(posture)

    monkeypatch.setattr(server_mod, "save_posture", watched)
    status, _ = handle_api(
        session, "POST", "/api/desk/posture", {}, {"armed": True})
    assert status == 200
    assert held == [True], "the disk write happened outside the posture lock"


@pytest.mark.parametrize("value", ["yes", 1, [], None])
def test_a_posture_that_is_not_a_boolean_is_refused(session, value):
    status, body = handle_api(
        session, "POST", "/api/desk/posture", {}, {"armed": value})
    assert status == 400 and "true or false" in body["error"]
    # Nothing was recorded: a refused arming must not half-arm the desk.
    assert session.posture_payload() == {"armed": False, "chosen": False}


# -- the chosen mind --------------------------------------------------------
def test_a_mind_nobody_chose_is_not_served_as_a_chosen_one(monkeypatch):
    """The pair alone cannot tell a chosen ``claude · inherit`` from the default.

    ``startup_llm_config`` collapses an absent file into ``DEFAULT_LLM_CONFIG``
    and ``llm_payload`` served both identically, so a desk nobody has asked and
    one whose operator picked exactly that were the same three fields on the
    wire — and a startup door that must ask *which mind runs Atlas* had nothing
    to key on. ``chosen`` is that fact and nothing more: the values stay the
    default's, so a client that ignores the flag reads today's payload.
    """
    monkeypatch.delenv("QLAB_LLM_REASONER", raising=False)
    monkeypatch.delenv("QLAB_LLM_WORKFORCE", raising=False)
    fresh = UISession(offline_default=True, registry=Registry(":memory:"))
    payload = fresh.llm_payload()
    assert payload["chosen"] is False
    # The fallback is still served in full: an unchosen mind is a working desk,
    # not an error state.
    assert payload["reasoner"] == {"backend": "claude", "model": "inherit"}
    assert payload["workforce"] == {"backend": "claude", "model": "inherit"}
    assert payload["reasoner_enabled"] is False
    # And it reaches the client where the client actually looks: the llm block
    # rides on the snapshot, which is what `desk_unchosen` reads for the mode.
    snap = handle_api(fresh, "GET", "/api/tui", {"offline": ["1"]}, {})[1]
    assert snap["llm"]["chosen"] is False


def test_a_persisted_mind_is_served_as_a_chosen_one(monkeypatch):
    """A file is somebody speaking, and it outlives the owner that wrote it."""
    from qlab.core.llm_config import LlmConfig, SurfaceModel, save_llm_config

    monkeypatch.delenv("QLAB_LLM_REASONER", raising=False)
    monkeypatch.delenv("QLAB_LLM_WORKFORCE", raising=False)
    save_llm_config(LlmConfig(reasoner=SurfaceModel("ollama", "granite3.3:8b"),
                              workforce=SurfaceModel("claude", "sonnet")))
    revived = UISession(offline_default=True, registry=Registry(":memory:"))
    payload = revived.llm_payload()
    assert payload["chosen"] is True
    assert payload["reasoner"] == {"backend": "ollama", "model": "granite3.3:8b"}


def test_a_config_written_before_the_flag_reads_as_chosen(monkeypatch):
    """The migration, and the reason the flag is derived rather than stored.

    These are the exact bytes every owner before this change wrote, carrying
    the default pair: no ``chosen`` key, and values indistinguishable from a
    desk that was never asked. Reading the key out of the file would make every
    upgraded desk unchosen and open a door on every launch, so the answer comes
    from the file's existence — which is the whole claim: somebody wrote it.
    """
    from qlab.paths import state_path

    monkeypatch.delenv("QLAB_LLM_REASONER", raising=False)
    monkeypatch.delenv("QLAB_LLM_WORKFORCE", raising=False)
    path = state_path("llm_config.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "reasoner": {"backend": "claude", "model": "inherit"},
        "workforce": {"backend": "claude", "model": "inherit"},
        "reasoner_enabled": False}), encoding="utf-8")
    revived = UISession(offline_default=True, registry=Registry(":memory:"))
    assert revived.llm_payload()["chosen"] is True


def test_a_checked_plan_gets_its_approval_opened_and_announced_once(session):
    """The reporter's checked preview is the desk asking; nothing opened the
    request, so BOOK's `x` returned in silence for want of a covering
    approval. The tick now opens it and says the two chat words that answer.
    """
    plan_id = _checked_plan(session)

    first = session.announce_desk_work(True, [])
    assert len(first["approvals_opened"]) == 1
    aid = first["approvals_opened"][0]
    pending = [a for a in session.registry.list_approval_requests(50)
               if a.get("plan_id") == plan_id]
    assert len(pending) == 1 and pending[0]["status"] == "pending"

    chat = session.registry.read_events_of_kind("atlas_message", 20)
    said = " ".join(str(e.get("payload", {}).get("text")) for e in chat)
    assert f"/approve {aid[:8]}" in said and f"/execute {plan_id[:8]}" in said

    # Once. A second tick must neither reopen nor repeat.
    again = session.announce_desk_work(True, [])
    assert again["approvals_opened"] == []
    assert len([a for a in session.registry.list_approval_requests(50)
                if a.get("plan_id") == plan_id]) == 1


def test_the_tick_leaves_one_proposal_and_the_route_serves_it(session):
    """Two checked plans are two open questions; the tick closes the older."""
    older = _checked_plan(session)
    session.announce_desk_work(True, [])
    newer = _checked_plan(session, tilt=0.02)
    out = session.announce_desk_work(True, [])

    assert out["superseded"] == [older]
    states = {a["plan_id"]: a["status"]
              for a in session.registry.list_approval_requests(50)}
    assert states[older] == "invalidated" and states[newer] == "pending"

    status, payload = handle_api(session, "GET", "/api/desk/proposal", {}, {})
    assert status == 200
    assert payload["proposal"]["plan_id"] == newer
    assert payload["proposal"]["superseded"] == [older]
    assert payload["proposal"]["referee"]["verdict"] == "PASS"

    # A superseded plan is named in the chat, once.
    said = " ".join(str(e["payload"]["text"]) for e
                    in session.registry.read_events_of_kind("atlas_message", 50))
    assert said.count(f"supersedes {older[:8]}") == 1
    assert session.announce_desk_work(True, [])["superseded"] == []
    said = " ".join(str(e["payload"]["text"]) for e
                    in session.registry.read_events_of_kind("atlas_message", 50))
    assert said.count(f"supersedes {older[:8]}") == 1


def test_two_checked_plans_in_one_tick_leave_one_proposal(session):
    """Both requests are opened, then the older is withdrawn in the same tick.

    Opening a request the tick immediately invalidates looks wasteful, and is
    deliberate: the record must show that the desk asked about that plan and
    then withdrew the question, not that the plan was never asked about.
    """
    first = _checked_plan(session)
    second = _checked_plan(session, tilt=0.02)
    out = session.announce_desk_work(True, [])

    assert len(out["approvals_opened"]) == 2
    assert len(out["superseded"]) == 1
    assert out["supersede_failures"] == []

    keeper = out["superseded"][0] == first and second or first
    _, payload = handle_api(session, "GET", "/api/desk/proposal", {}, {})
    assert payload["proposal"]["plan_id"] == keeper
    assert payload["proposal"]["superseded"] == out["superseded"]

    said = " ".join(str(e["payload"]["text"]) for e
                    in session.registry.read_events_of_kind("atlas_message", 50))
    assert said.count("supersedes") == 1


def test_a_fired_trigger_is_announced_in_the_chat(session):
    out = session.announce_desk_work(True, [
        {"task_id": "abc123def456", "trigger": "regime_flip", "action": "workflow"},
    ])
    assert out["triggers"] == ["abc123de"]
    chat = session.registry.read_events_of_kind("atlas_message", 10)
    text = chat[0]["payload"]["text"] if chat else ""
    assert "regime_flip" in text and "regime_review" in text, text


def test_upcoming_releases_are_served_and_dated(session, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from qlab.news.providers import macro

    soon = datetime.now(timezone.utc) + timedelta(days=3, hours=1)
    monkeypatch.setattr(macro, "load_news_sources", lambda: {"calendar": [
        # The 2999 entry is the out-of-horizon control; it also keeps the
        # calendar from reading as exhausted, which is a loud refusal.
        {"name": "FOMC", "when": "2999-01-01T18:00:00+00:00", "tickers": ["TLT"],
         "source": "Federal Reserve"},
        {"name": "CPI", "when": soon.isoformat(), "tickers": ["TIP"],
         "source": "BLS"}]})
    status, out = handle_api(session, "GET", "/api/news/upcoming", {}, {})
    assert status == 200
    assert [e["name"] for e in out["upcoming"]] == ["CPI"], "2999 is beyond 14 days"
    entry = out["upcoming"][0]
    assert entry["when"].startswith(soon.strftime("%Y-%m-%dT%H:"))
    assert entry["days_ahead"] == 3 and entry["source"] == "BLS"


def test_the_desk_reads_a_stack_and_archives_each_member(session, monkeypatch):
    from qlab.news import feed
    from qlab.news.feed import NewsItem
    monkeypatch.setenv("QLAB_NEWS_PROVIDERS", "one,two")
    # An instant, not a literal: the desk's window is `now` minus 48h, so a
    # hard-coded date would make this test pass only in the week it was written.
    published = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    def mk(provider, src):
        return lambda a, u: [NewsItem(source=src, published=published,
                                      headline=f"{src} story", summary="", url=f"https://x/{src}",
                                      tickers=("SPY",), provider=provider)]

    def dead(a, u):
        raise RuntimeError("feed three is unavailable")

    monkeypatch.setenv("QLAB_NEWS_PROVIDERS", "one,two,three")
    monkeypatch.setitem(feed.PROVIDERS, "one", mk("one", "SEC EDGAR"))
    monkeypatch.setitem(feed.PROVIDERS, "two", mk("two", "reuters.com"))
    monkeypatch.setitem(feed.PROVIDERS, "three", dead)
    assert session.news_provider_for(False) == ("one", "two", "three")
    window = session.fetch_desk_news(False)
    assert window["outcomes"]["one"] == "ok" and window["outcomes"]["two"] == "ok"
    # The member that went away is named, not absent: a smaller window with no
    # explanation is indistinguishable from a quiet wire.
    assert "unavailable" in window["outcomes"]["three"]
    result = session.archive_desk_news(window)
    events = [e for e in session.registry.read_events_of_kind("news_archive", 10)]
    by_provider = {e["payload"]["provider"]: e["payload"] for e in events}
    assert set(by_provider) == {"one", "two", "three"}
    assert "unavailable" in by_provider["three"]["outcome"]
    assert by_provider["three"]["returned"] == 0
    assert by_provider["one"]["outcome"] == "ok"
    assert result["stored"] == 2
    assert set(result["per_provider"]) == {"one", "two", "three"}

    # And the wire carries the same facts the archive does.
    status, snapshot = handle_api(session, "GET", "/api/tui", {}, {})
    assert status == 200
    news = snapshot["news"]
    assert news["providers"] == ["one", "two", "three"]
    assert "unavailable" in news["outcomes"]["three"]


def test_news_archive_events_do_not_crowd_the_generic_audit_page(session, monkeypatch):
    # One event per member per tick, on a fixed-size page: a four-member stack
    # on the heartbeat pushes everything an operator is auditing off the page
    # within minutes. Selected by kind at the store, like atlas_chat.
    from qlab.news import feed
    from qlab.news.feed import NewsItem
    monkeypatch.setenv("QLAB_NEWS_PROVIDERS", "one,two")
    published = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    for name in ("one", "two"):
        monkeypatch.setitem(feed.PROVIDERS, name, lambda a, u, n=name: [NewsItem(
            source=n, published=published, headline=f"{n} story", summary="",
            url=f"https://x/{n}", tickers=("SPY",), provider=n)])
    session.registry.record_event("halt", {"reason": "audited"})
    session.archive_desk_news(session.fetch_desk_news(False))

    page = session.read_audit_stream_events(50, after=None)
    assert [e["kind"] for e in page if e["kind"] == "news_archive"] == []
    assert any(e["kind"] == "halt" for e in page)
    # The rows still exist; only the generic page declines to carry them.
    assert len(session.registry.read_events_of_kind("news_archive", 10)) == 2


def test_a_malformed_provider_stack_is_a_loud_window_not_a_dead_heartbeat(
        session, monkeypatch):
    # news_provider_for now parses, so it can refuse. Called outside the try it
    # took the whole heartbeat tick down with it — including the parts of the
    # tick that have nothing to do with news.
    monkeypatch.setenv("QLAB_NEWS_PROVIDERS", ",")
    window = session.fetch_desk_news(False)
    assert window["items"] == []
    assert window["error"]
    assert window["providers"] == []
    # And the card says "no provider", not an empty gap where a name goes.
    assert session.news_payload(False)["provider"] == "—"


def test_an_archive_window_naming_an_undeclared_provider_is_refused(session):
    from qlab.news.feed import NewsItem

    published = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    window = {
        "items": [NewsItem(source="X", published=published, headline="h",
                           summary="", url="https://x/1", tickers=("SPY",),
                           provider="smuggled")],
        "outcomes": {"one": "ok"},
        "providers": ["one"],
        "provider_name": "one",
        "error": None,
    }
    with pytest.raises(RuntimeError, match="smuggled"):
        session.archive_desk_news(window)


def test_a_partial_member_is_archived_as_a_window_not_as_a_failure(
        session, monkeypatch):
    # A batch with records is not a failed batch. Stamping the member's missing
    # feeds on it as `error` would file real primary records under an outage.
    from qlab.news import feed
    from qlab.news.feed import NewsItem
    monkeypatch.setenv("QLAB_NEWS_PROVIDERS", "macro")
    published = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    def partial(a, u):
        raise feed.PartialWindow(
            [NewsItem(source="BEA", published=published, headline="h",
                      summary="", url="https://x/1", tickers=("SPY",),
                      provider="macro")],
            {"BLS": "HTTP Error 403: Forbidden"})

    monkeypatch.setitem(feed.PROVIDERS, "macro", partial)
    window = session.fetch_desk_news(False)
    assert window["outcomes"]["macro"].startswith("partial: ")
    assert len(window["items"]) == 1

    result = session.archive_desk_news(window)
    assert result["stored"] == 1
    payload = session.registry.read_events_of_kind("news_archive", 5)[0]["payload"]
    assert payload["returned"] == 1
    assert payload["error"] is None
    assert "BLS" in payload["outcome"] and "403" in payload["outcome"]

def _pinned_upcoming(monkeypatch, tickers):
    """A fixed look-ahead. The real one reads a hand-maintained yaml against
    today's date, so a test that used it would start failing on a calendar
    edit rather than on a code change."""
    from qlab.news.providers import macro

    monkeypatch.setattr(macro, "upcoming", lambda as_of, horizon_days=14: [
        {"name": "FOMC statement", "when": "2026-09-17T18:00:00+00:00",
         "days_ahead": 7, "tickers": list(tickers), "source": "Federal Reserve"}])


def _push_window(session, headlines, ticker):
    """Publish a news window the way ``fetch_desk_news`` publishes one."""
    from qlab.news.feed import NewsItem

    published = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    window = {
        "items": [NewsItem(source=f"reuters.com/{n}", published=published,
                           headline=headline, summary="", url=f"https://x/{n}",
                           tickers=(ticker,), provider="gdelt")
                  for n, headline in enumerate(headlines)],
        "outcomes": {"gdelt": "ok"}, "providers": ["gdelt"],
        "provider": "gdelt", "provider_name": "gdelt",
        "as_of": datetime.now(timezone.utc).isoformat(), "error": None,
    }
    with session._news_lock:
        session._desk_news = window
    return window


class _FixedDate:
    """`date` with a pinned `today()`, so a second call can be a second day."""

    def __init__(self, iso):
        self._iso = iso

    def today(self):
        return date.fromisoformat(self._iso)

    def fromisoformat(self, iso):
        return date.fromisoformat(iso)


def test_the_qualitative_matrix_is_served_and_logged_once_per_window(
        session, monkeypatch):
    """One row per WINDOW, and `as_of` is not part of what a window is.

    The discriminating case is the same window read on a later day: the spec
    differs, so `log_run`'s content hash collapses nothing, and only the
    window-hash guard stops the registry accumulating a row per day of a tape
    that never moved.
    """
    ticker = session.mandate.universe_whitelist[0]
    _pinned_upcoming(monkeypatch, [ticker])
    _push_window(session, ["gold holds its gain after the auction"], ticker)

    status, out = handle_api(session, "GET", "/api/research/qualitative", {}, {})
    assert status == 200 and set(out["rows"]) == set(session.mandate.universe_whitelist)
    assert out["rows"][ticker]["coverage"] == 1
    assert out["rows"][ticker]["days_to_next_release"] == 7
    first = [r for r in session.registry.list_runs(20) if r["kind"] == "qualitative_matrix"]

    monkeypatch.setattr(ui_server, "date", _FixedDate("2026-09-01"))
    _, same = handle_api(session, "GET", "/api/research/qualitative", {}, {})
    again = [r for r in session.registry.list_runs(20) if r["kind"] == "qualitative_matrix"]
    assert same["run_id"] == out["run_id"]
    assert len(again) == len(first) == 1

    # Stamped as the desk's own: the ablation arm writes qualitative_matrix
    # runs to this same registry, and a reader that cannot tell them apart
    # will serve an arm's research window as the desk's record.
    assert first[0]["spec"]["source"] == "desk"

    # A different window is a different observation and does get its own row.
    _push_window(session, ["oil slips as OPEC delays the quota decision"], ticker)
    _, moved = handle_api(session, "GET", "/api/research/qualitative", {}, {})
    assert moved["run_id"] != out["run_id"]
    assert len([r for r in session.registry.list_runs(20)
                if r["kind"] == "qualitative_matrix"]) == 2


def test_an_exhausted_release_calendar_is_named_not_silently_empty(session, monkeypatch):
    """`upcoming()` refuses loudly once its hand-maintained calendar runs out.
    The matrix must still be built — coverage is a fact about the window, not
    about the yaml — but with the gap named rather than rendered as 'no
    releases ahead', which is a different claim."""
    from qlab.news.providers import macro

    monkeypatch.setattr(macro, "load_news_sources", lambda: {"calendar": [
        {"name": "CPI (stale)", "when": "2020-01-02T12:30:00+00:00",
         "tickers": ["BNDW"], "source": "BLS"}]})
    status, out = handle_api(session, "GET", "/api/research/qualitative", {}, {})
    assert status == 200
    assert "exhausted" in out["calendar_error"]
    assert all(row["days_to_next_release"] is None for row in out["rows"].values())
    context = session.atlas_context(True)
    assert "exhausted" in context["qualitative_matrix"]["calendar_error"]


def test_the_reasoner_gets_matrix_counts_and_not_archive_ids(session):
    context = session.atlas_context(True)
    rows = context["qualitative_matrix"]["rows"]
    assert set(rows) == set(session.mandate.universe_whitelist)
    ticker = sorted(rows)[0]
    assert "coverage" in rows[ticker] and "claim_keys" not in rows[ticker]


def test_a_broken_matrix_does_not_take_the_whole_reasoner_context_down(
        session, monkeypatch):
    """`atlas_judgment_request` drops the entire request when composing the
    context raises, so every surface on it is wrapped — the regime panel two
    lines up for exactly this reason."""
    def boom(offline):
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(session, "qualitative_matrix", boom)
    context = session.atlas_context(True)
    assert context["qualitative_matrix"]["rows"] == {}
    assert "registry unreadable" in context["qualitative_matrix"]["error"]


# --- the Settings pane's news routes ---------------------------------------


def _news_settings_env(monkeypatch, tmp_path):
    """A workspace of this test's own, and no inherited news configuration.

    `setenv` then `delenv` rather than `delenv(raising=False)`: the latter
    records nothing when the name is already absent, so a test that goes on to
    set it leaks the value into the next module.
    """
    monkeypatch.setenv("QLAB_WORKSPACE", str(tmp_path))
    for name in ("QLAB_NEWS_PROVIDERS", "QLAB_NEWS_PROVIDER",
                 "QLAB_EDGAR_CONTACT"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "qlab.trader.alpaca_auth.resolve_alpaca_credentials", lambda: None)


def test_news_settings_says_what_the_desk_reads_and_never_the_contact(
        session, monkeypatch, tmp_path):
    _news_settings_env(monkeypatch, tmp_path)

    status, out = handle_api(session, "GET", "/api/news/settings", {}, {})
    assert status == 200
    # The fixture session is offline: the lane and the stack say so together.
    assert out["lane"] == "synthetic"
    assert out["stack"] == list(session.news_provider_for(True))
    assert out["configured"] is False
    assert out["edgar_contact_set"] is False
    # Nothing has been fetched, so there is no outcome to report — not a
    # guessed one.
    assert out["outcomes"] == {}
    assert [entry["name"] for entry in out["catalog"]] == [
        "alpaca", "edgar", "macro", "rss", "gdelt"]
    assert all(entry["chosen"] is False for entry in out["catalog"])

    monkeypatch.setenv("QLAB_NEWS_PROVIDERS", "macro,rss")
    monkeypatch.setenv("QLAB_EDGAR_CONTACT", "Jane Doe <jane@x.io>")
    status, out = handle_api(
        session, "GET", "/api/news/settings", {"offline": ["0"]}, {})
    assert status == 200
    assert out["lane"] == "live"
    assert out["stack"] == ["macro", "rss"]
    assert out["configured"] is True
    assert [e["name"] for e in out["catalog"] if e["chosen"]] == ["macro", "rss"]
    # The contact is a value this desk sends to the SEC and to nobody else:
    # the pane learns that one is on file, never what it says.
    assert out["edgar_contact_set"] is True
    assert "jane@x.io" not in json.dumps(out)


def test_news_settings_applies_a_stack_and_clears_the_cached_window(
        session, monkeypatch, tmp_path):
    from qlab.env import parse_env

    _news_settings_env(monkeypatch, tmp_path)
    session._desk_news = {"items": [], "outcomes": {"synthetic": "ok"}}

    status, out = handle_api(session, "POST", "/api/news/settings", {},
                             {"providers": ["macro"], "offline": False})
    assert status == 200
    assert out["stack"] == ["macro"]
    assert out["configured"] is True
    assert "verify" not in out
    assert parse_env((tmp_path / ".env").read_text(encoding="utf-8")) == {
        "QLAB_NEWS_PROVIDERS": "macro"}
    # The running process reads it too, or the next heartbeat fetches the old
    # stack and the pane reports a change that did not happen.
    assert os.environ["QLAB_NEWS_PROVIDERS"] == "macro"
    assert session._desk_news is None
    assert out["outcomes"] == {}


@pytest.mark.parametrize("body,expected", [
    ({"providers": "macro"}, "providers must be a list of source names"),
    ({"providers": ["bloomberg"]}, "unknown news provider(s) bloomberg"),
    # The exact sentence, not a word the unknown-name message also carries:
    # both of these passed for the wrong reason on a substring.
    ({"providers": []}, "no news source was named"),
    ({"providers": ["alpaca"]}, "alpaca profile login"),
    ({"providers": ["edgar"]}, "edgar needs a contact"),
    ({"providers": ["macro"], "edgar_contact": "nobody"}, "nobody"),
])
def test_news_settings_refuses_a_stack_it_cannot_honour(
        session, monkeypatch, tmp_path, body, expected):
    _news_settings_env(monkeypatch, tmp_path)

    status, out = handle_api(session, "POST", "/api/news/settings", {},
                             dict(body, offline=False))
    assert status == 400
    assert expected in out["error"]
    assert not (tmp_path / ".env").exists(), "a refusal wrote configuration"


def test_news_settings_takes_an_empty_contact_as_no_change(
        session, monkeypatch, tmp_path):
    """An empty box is "leave the contact alone", not a contact to validate."""
    from qlab.env import parse_env

    _news_settings_env(monkeypatch, tmp_path)
    monkeypatch.setenv("QLAB_EDGAR_CONTACT", "Jane Doe <jane@x.io>")

    status, out = handle_api(
        session, "POST", "/api/news/settings", {},
        {"providers": ["edgar"], "edgar_contact": "  ", "offline": False})
    assert status == 200
    assert out["edgar_contact_set"] is True
    # The stored line is untouched: only the stack was written.
    assert parse_env((tmp_path / ".env").read_text(encoding="utf-8")) == {
        "QLAB_NEWS_PROVIDERS": "edgar"}
    assert os.environ["QLAB_EDGAR_CONTACT"] == "Jane Doe <jane@x.io>"


def test_news_settings_writes_a_repeated_source_once(
        session, monkeypatch, tmp_path):
    """A stack is an order, not a bag: macro twice is macro once, in place."""
    from qlab.env import parse_env

    _news_settings_env(monkeypatch, tmp_path)
    status, out = handle_api(
        session, "POST", "/api/news/settings", {},
        {"providers": ["macro", "rss", "macro"], "offline": False})
    assert status == 200
    assert out["stack"] == ["macro", "rss"]
    assert parse_env((tmp_path / ".env").read_text(encoding="utf-8")) == {
        "QLAB_NEWS_PROVIDERS": "macro,rss"}


def test_news_settings_leaves_no_half_applied_environment(
        session, monkeypatch, tmp_path):
    """A write that dies mid-way must not leave the env holding what .env does not.

    `verify_plan` exports the contact and `write_env_values` sets each name
    before the file lands, so without a guard a raise in between would leave
    the process configured for a stack nobody saved — and the route's
    "nothing was written" refusal would be a lie.
    """
    _news_settings_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "qlab.news.check.check_news",
        lambda universe, provider=None, lookback_hours=72: {
            "ok": True, "members": {"edgar": {"ok": True}}})

    def die(plan, *, root, environ):
        raise RuntimeError("disk went away")

    monkeypatch.setattr("qlab.news.setup.apply_plan", die)
    before = dict(os.environ)
    # do_POST turns this into a 500; what matters is what it leaves behind.
    with pytest.raises(RuntimeError):
        handle_api(session, "POST", "/api/news/settings", {},
                   {"providers": ["edgar"], "verify": True, "offline": False,
                    "edgar_contact": "Jane Doe <jane@x.io>"})
    assert os.environ == before
    assert not (tmp_path / ".env").exists()


def test_news_settings_verify_names_a_dead_member_and_still_applies(
        session, monkeypatch, tmp_path):
    """The pane's drop-a-dead-member affordance: report, then apply.

    The caller chose the stack; the check tells them what it found. Refusing
    the write here would make an unreachable source unconfigurable.
    """
    _news_settings_env(monkeypatch, tmp_path)

    def dead(universe, provider=None, lookback_hours=72):
        assert provider == "macro"
        return {"ok": False,
                "members": {"macro": {"ok": False, "error": "macro feed 503"}}}

    monkeypatch.setattr("qlab.news.check.check_news", dead)
    status, out = handle_api(
        session, "POST", "/api/news/settings", {},
        {"providers": ["macro"], "verify": True, "offline": False})
    assert status == 200
    assert out["verify"]["ok"] is False
    assert out["verify"]["members"]["macro"]["ok"] is False
    assert "503" in out["verify"]["members"]["macro"]["detail"]
    # A member can be ok and still short a feed, so the flags ride along: the
    # pane must not read `ok` as whole-stack health.
    assert out["verify"]["members"]["macro"]["quality_flags"] == []
    assert out["stack"] == ["macro"]
    assert os.environ["QLAB_NEWS_PROVIDERS"] == "macro"


def test_the_upcoming_route_names_an_exhausted_calendar_instead_of_500ing(
        session, monkeypatch):
    """The look-ahead refuses loudly when the hand-maintained file runs out.
    Letting that escape the handler turned a known, expected state into a 500
    with a repr in it — the one shape a client cannot distinguish from the
    owner being broken."""
    from qlab.news.providers import macro

    def exhausted(*a, **k):
        raise RuntimeError("the release calendar is exhausted: ...")

    monkeypatch.setattr(macro, "upcoming", exhausted)
    status, payload = handle_api(session, "GET", "/api/news/upcoming", {}, {})
    assert status == 200
    assert payload["upcoming"] == []
    assert "exhausted" in payload["error"]


def test_system_status_shows_the_calendar_running_out_before_it_does(
        session, monkeypatch):
    from qlab.news.providers import macro

    monkeypatch.setattr(macro, "calendar_days_left", lambda now: 9)
    status = session.system_status(True)
    assert status["calendar_days_left"] == 9

    monkeypatch.setattr(macro, "calendar_days_left", lambda now: None)
    assert session.system_status(True)["calendar_days_left"] is None

    # A calendar this process cannot read is not a status-poll failure.
    def broken(now):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(macro, "calendar_days_left", broken)
    assert session.system_status(True)["calendar_days_left"] is None


def test_one_news_window_is_grounded_once_for_the_read_and_the_matrix(
        session, monkeypatch):
    """Grounding hashes, windows and clusters every record. The read and the
    matrix are two views of ONE window, and re-deriving the identical
    GroundedNews per call put that work on the dispatch lock twice a poll."""
    from qlab.news import grounding

    real = grounding.ground
    calls = []

    def counting(items, **kwargs):
        calls.append(kwargs.get("provider"))
        return real(items, **kwargs)

    monkeypatch.setattr(grounding, "ground", counting)
    window = session.fetch_desk_news(True)
    assert window["items"]
    session.compose_desk_read(True, prefetched_news=window)
    session.compose_desk_read(True, prefetched_news=window)
    session.qualitative_matrix(True)
    assert len(calls) == 1

    # A different window is a different grounding, not a stale reuse.
    other = dict(window)
    other["items"] = list(window["items"])[:1]
    session.compose_desk_read(True, prefetched_news=other)
    assert len(calls) == 2


def test_the_heavy_snapshot_summaries_are_ttl_cached_and_a_run_invalidates_them(
        session):
    """Every /api/tui poll recomposed these under the dispatch lock: two
    `list_runs(200)` scans for the ablation, 1000 rows for the equilibrium and
    100 for the board — with every other request queued behind them."""
    calls = []
    real = session.registry.list_runs

    def counting(limit=50, *a, **k):
        calls.append(limit)
        return real(limit, *a, **k)

    session.registry.list_runs = counting

    assert session.latest_equilibrium_returns() is None
    assert session.latest_ablation_metrics() == {}
    assert session.predictor_board_summary() == {"status": "never_ran"}
    first = len(calls)
    assert first >= 3

    # Within the TTL and with no new run, the second call scans nothing.
    session.latest_equilibrium_returns()
    session.latest_ablation_metrics()
    session.predictor_board_summary()
    assert len(calls) == first

    # A logged run is exactly what these summarise, so it invalidates all three.
    run_id = session.registry.log_run("ablation", {"note": "fresh"})
    session.registry.log_backtest(run_id, "B2", {"sharpe": 0.9})
    session.latest_equilibrium_returns()
    assert session.latest_ablation_metrics() == {"B2": {"sharpe": 0.9}}
    session.predictor_board_summary()
    assert len(calls) > first


def test_the_heartbeat_tick_logs_one_qualitative_matrix_per_window(session):
    """The matrix had only conditional producers — a route no shipped client
    calls, and a reasoner-enabled chat — so a stock desk logged none at all and
    the per-window history the registry is supposed to carry never accrued."""
    import threading
    from datetime import datetime as _dt

    from qlab.news.matrix import DESK_MATRIX_SOURCE
    from qlab.operator.heartbeat import build_owner_tick

    # The synthetic window is seeded by the fetch's second-resolution as_of,
    # so two ticks astride a second boundary are two *different* windows and
    # the guard rightly logs both — which is not what this test measures.
    # Freeze the tick's clock so "the same window" is literally the same.
    class _FrozenNow(_dt):
        @classmethod
        def now(cls, tz=None):
            return _dt(2026, 8, 31, 12, 0, 0, tzinfo=tz)

    import qlab.ui.server as ui_server
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ui_server, "datetime", _FrozenNow)
        tick = build_owner_tick(session, threading.Lock(), offline=True)
        tick()

        rows = session.registry.matrix_runs(source=DESK_MATRIX_SOURCE, limit=5)
        assert len(rows) == 1
        assert rows[0]["spec"]["source"] == DESK_MATRIX_SOURCE

        # One row per WINDOW, not per tick: the same window logs nothing new.
        tick()
        assert len(session.registry.matrix_runs(
            source=DESK_MATRIX_SOURCE, limit=5)) == 1


# --- booking the current proposal in one confirmed call --------------------
#
# `POST /api/desk/proposal/book` is the desk's one-click book: it approves the
# desk's own pending question and executes it. The refusals matter more than
# the happy path — every one of them has to land before any state moves.


def _open_request(session, plan_id: str) -> str:
    """A pending, plan-bound approval request — the desk's open question."""
    _, created = handle_api(
        session, "POST", "/api/approvals", {},
        {"plan_id": plan_id, "offline": True})
    return created["approval_id"]


def _book_body(session, plan_id: str) -> dict:
    plan = session.registry.get_plan(plan_id)
    from qlab.state.registry import targets_hash

    return {"plan_id": plan_id, "targets_hash": targets_hash(plan["targets"]),
            "human_confirmed": True, "offline": True}


def _booked_events(session) -> list[dict]:
    return [event["payload"] for event
            in session.registry.read_events_of_kind("proposal_booked", 20)]


def test_the_current_proposal_books_in_one_confirmed_call(session):
    # Two calls (approve, then execute) meant two chances to leave a desk with
    # an approved-but-unbooked plan on it. One confirmation, one fill.
    plan_id = _checked_plan(session)
    approval_id = _open_request(session, plan_id)

    status, out = handle_api(
        session, "POST", "/api/desk/proposal/book", {},
        _book_body(session, plan_id))

    assert status == 200
    assert out["booked"] is True
    assert out["approval_id"] == approval_id
    assert out["execution"]["executed"] is True
    # The approval was granted and then spent by this one call.
    assert session.registry.get_approval_request(approval_id)["status"] == (
        "consumed")
    assert session.registry.list_orders(50) != []
    booked = _booked_events(session)
    assert len(booked) == 1
    assert booked[0]["plan_id"] == plan_id
    assert booked[0]["approval_id"] == approval_id
    assert booked[0]["targets_hash"] == _book_body(session, plan_id)["targets_hash"]


def test_a_wrong_targets_hash_books_nothing_and_approves_nothing(session):
    # The hash is what the confirm box binds to. A mismatch is not a near-miss
    # to be repaired — it means the operator confirmed a different allocation.
    plan_id = _checked_plan(session)
    approval_id = _open_request(session, plan_id)
    body = _book_body(session, plan_id)
    body["targets_hash"] = "0" * 16

    status, out = handle_api(
        session, "POST", "/api/desk/proposal/book", {}, body)

    assert status == 400
    assert out["error"] == "targets_hash does not match the plan"
    # Nothing moved: still an unanswered question, no fill, no event.
    assert session.registry.get_approval_request(approval_id)["status"] == (
        "pending")
    assert session.registry.list_orders(50) == []
    assert _booked_events(session) == []


def test_a_superseded_plan_is_not_the_current_proposal(session):
    # The older question was withdrawn on the record; a client holding a stale
    # card must not be able to book it back to life.
    from qlab.governance.proposal import supersede

    older = _checked_plan(session)
    older_approval = _open_request(session, older)
    newer = _checked_plan(session, tilt=0.02)
    _open_request(session, newer)
    # (withdrawn, failures) — nothing may fail silently on this path.
    assert supersede(session.registry, newer) == ([older], [])

    status, out = handle_api(
        session, "POST", "/api/desk/proposal/book", {},
        _book_body(session, older))

    assert status == 400
    assert out["error"] == "not the current proposal"
    assert session.registry.get_approval_request(older_approval)["status"] == (
        "invalidated")
    assert session.registry.list_orders(50) == []


def test_a_truthy_human_confirmed_cannot_book(session):
    # Exactly True. "yes" is a client bug or a smuggled confirmation, and
    # either way it is not a human at a confirm box.
    plan_id = _checked_plan(session)
    approval_id = _open_request(session, plan_id)
    body = _book_body(session, plan_id)
    body["human_confirmed"] = "yes"

    status, out = handle_api(
        session, "POST", "/api/desk/proposal/book", {}, body)

    assert status == 400
    assert out["error"] == "human_confirmed=true is required"
    assert session.registry.get_approval_request(approval_id)["status"] == (
        "pending")
    assert _booked_events(session) == []


@pytest.mark.parametrize("confirmation", [
    # Absent entirely — the shape a client that forgot the field sends, and the
    # one an agent composing a body from the route name would send.
    "__absent__",
    # The truthy integer. `if body.get("human_confirmed")` would pass all of
    # these; `is not True` is why none of them do.
    1, 1.0, "true", "True", [True], {"human_confirmed": True},
])
def test_only_the_boolean_true_books(session, confirmation):
    """Invariant 3's `human_confirmed=True` is an identity check, not a truth
    test. Weaken it to truthiness and every value below books a paper trade."""
    plan_id = _checked_plan(session)
    approval_id = _open_request(session, plan_id)
    body = _book_body(session, plan_id)
    if confirmation == "__absent__":
        del body["human_confirmed"]
    else:
        body["human_confirmed"] = confirmation

    status, out = handle_api(
        session, "POST", "/api/desk/proposal/book", {}, body)

    assert status == 400, confirmation
    assert out["error"] == "human_confirmed=true is required"
    assert session.registry.get_approval_request(approval_id)["status"] == (
        "pending")
    assert session.registry.list_orders(50) == []
    assert _booked_events(session) == []


def test_an_already_approved_proposal_books_without_a_second_approval(session):
    # An approved, unspent request is still the desk's live question. Booking
    # it must not try to approve it twice — decide_approval only binds pending.
    plan_id = _checked_plan(session)
    approval_id = _approve(session, plan_id)
    assert session.registry.get_approval_request(approval_id)["status"] == (
        "approved")

    status, out = handle_api(
        session, "POST", "/api/desk/proposal/book", {},
        _book_body(session, plan_id))

    assert (status, out["booked"]) == (200, True)
    assert out["approval_id"] == approval_id
    # Exactly one approval event: the one the human already made.
    approved = session.registry.read_events_of_kind("approval_approved", 20)
    assert len(approved) == 1


def test_a_consumed_request_cannot_be_booked_again(session):
    # The second click on a card that already filled. A consumed request is
    # history, not a question, so there is no current proposal to book.
    plan_id = _checked_plan(session)
    _open_request(session, plan_id)
    body = _book_body(session, plan_id)
    status, _ = handle_api(session, "POST", "/api/desk/proposal/book", {}, body)
    assert status == 200
    orders = len(session.registry.list_orders(50))

    status, out = handle_api(
        session, "POST", "/api/desk/proposal/book", {}, body)

    assert status == 400
    assert out["error"] == "not the current proposal"
    assert len(session.registry.list_orders(50)) == orders
    assert len(_booked_events(session)) == 1


def test_booking_refuses_when_no_pass_covers_the_hash(session):
    # The plan was checked under a PASS, but the referee's latest word on that
    # decision is now a FAIL. The gate re-reads the verdict rather than
    # trusting that `state == "checked"` still means what it meant.
    plan_id = _checked_plan(session)
    approval_id = _open_request(session, plan_id)
    plan = session.registry.get_plan(plan_id)
    session.registry.log_verdict(
        plan["decision_id"], "FAIL", ["mandate drift"],
        source="referee-agent", targets=plan["targets"])

    status, out = handle_api(
        session, "POST", "/api/desk/proposal/book", {},
        _book_body(session, plan_id))

    assert status == 400
    assert "PASS" in out["error"]
    assert session.registry.get_approval_request(approval_id)["status"] == (
        "pending")
    assert session.registry.list_orders(50) == []


def test_a_blocked_data_revalidation_answers_200_and_leaves_the_approval(session,
                                                                        monkeypatch):
    """`booked: false` is not one shape. This one is retryable.

    The execute gate refuses on stale execution data BEFORE it touches the
    approval, so the request is still approved and unspent — the operator can
    fix the data and book the same proposal. Answering this the same way as an
    invalidated approval would send them to re-propose a plan that is fine.
    """
    from types import SimpleNamespace

    plan_id = _checked_plan(session)
    approval_id = _open_request(session, plan_id)
    monkeypatch.setattr(session, "data_policy",
                        lambda offline: SimpleNamespace(execution_eligible=True))
    monkeypatch.setattr(
        session, "data_health",
        lambda offline, purpose=None: {"blocked": True,
                                       "eligible_for_execution": False})

    status, out = handle_api(
        session, "POST", "/api/desk/proposal/book", {},
        _book_body(session, plan_id))

    assert status == 200
    assert out["booked"] is False
    assert out["execution"]["blocked_by"] == "data_revalidation"
    # Retryable: the approval the human just gave is still live and unspent.
    approval = session.registry.get_approval_request(approval_id)
    assert (approval["status"], approval["consumed_at"]) == ("approved", None)
    assert session.registry.list_orders(50) == []
    assert _booked_events(session) == []


def test_a_mandate_violation_answers_200_and_leaves_the_approval(session,
                                                                 monkeypatch):
    # The third shape: the plan violated the mandate at submission. Like a
    # data refusal and unlike an invalidated approval, nothing was withdrawn.
    from qlab.trader.mandate import MandateViolation

    plan_id = _checked_plan(session)
    approval_id = _open_request(session, plan_id)

    def _refuse(*args, **kwargs):
        raise MandateViolation("gross exposure over the cap")

    monkeypatch.setattr("qlab.trader.plan.execute_plan", _refuse)

    status, out = handle_api(
        session, "POST", "/api/desk/proposal/book", {},
        _book_body(session, plan_id))

    assert (status, out["booked"]) == (200, False)
    assert "gross exposure" in out["execution"]["mandate_violation"]
    assert session.registry.get_approval_request(approval_id)["status"] == (
        "approved")
    assert _booked_events(session) == []


def test_an_execution_that_raises_does_not_leave_a_live_approval(session,
                                                                 monkeypatch):
    # An approval granted a microsecond ago and never consumed is live
    # authority to book. An exception on the way to the broker used to leave it
    # behind — a spendable approval for a plan that just proved it cannot
    # execute. The failure still reaches the caller; the authority does not
    # survive it.
    plan_id = _checked_plan(session)
    approval_id = _open_request(session, plan_id)

    def _boom(*args, **kwargs):
        raise RuntimeError("incomplete persisted legs; re-propose")

    monkeypatch.setattr("qlab.trader.plan.execute_plan", _boom)

    with pytest.raises(RuntimeError, match="incomplete persisted legs"):
        session.book_current_proposal(_book_body(session, plan_id), True)

    approval = session.registry.get_approval_request(approval_id)
    assert approval["status"] == "invalidated"
    assert "incomplete persisted legs" in approval["invalidated_reason"]
    assert _booked_events(session) == []
    # What makes this the *before* case, and the boundary the tests below sit
    # on the other side of: the plan never reached `submitted`, so nothing of
    # it is at the broker and the authority has nothing left to authorise.
    assert session.registry.get_plan(plan_id)["state"] == "checked"


# --- A4: a plan that reached the broker keeps its authority -------------------
#
# `execute_plan` sets the plan `submitted` BEFORE it iterates legs
# (`qlab/trader/plan.py`) and accepts that state again on a later call, so a
# run that died mid-execution replays each leg through its stable
# `client_order_id` without double-booking. Withdrawing the approval when the
# broker raises on leg 2 of 20 therefore destroys the very authority the resume
# needs, and strands the filled legs with no request to reconcile them against.
# `withdraw_orphans` (`qlab/governance/proposal.py`) skips a `submitted` plan
# for exactly this reason; this sibling was missed.


def _broker_fails_on_leg(monkeypatch, nth: int, message: str) -> None:
    """Let `execute_plan` run for real, with the venue refusing leg `nth` once.

    Patching `execute_plan` itself would assert around the ordering under test
    — the point is that the plan really is `submitted` when the raise happens,
    which only the real function can establish. Failing once and then healing
    is what lets the same test drive the resume the kept authority is for.
    """
    from qlab.trader.broker import SimulatedPaperBroker

    real = SimulatedPaperBroker.submit_notional
    calls = {"n": 0}

    def _submit(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == nth:
            raise RuntimeError(message)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(SimulatedPaperBroker, "submit_notional", _submit)


def test_a_failure_after_the_legs_reached_the_broker_keeps_the_approval(
        session, monkeypatch):
    # Leg 1 of 20 fills, leg 2 is refused by the venue. The book has moved and
    # the plan is mid-execution: the approval is the only thing that can
    # finish it, so it must survive, and the error must say so — an operator
    # reading only the failure has to know the plan is in flight and its
    # authority intact, not re-propose on top of a half-booked plan.
    plan_id = _checked_plan(session)
    approval_id = _open_request(session, plan_id)
    _broker_fails_on_leg(monkeypatch, 2, "venue rejected the order")

    with pytest.raises(RuntimeError,
                       match="venue rejected the order") as raised:
        session.book_current_proposal(_book_body(session, plan_id), True)

    message = str(raised.value)
    assert "submitted" in message
    assert approval_id in message
    assert "kept live" in message
    # The remedy is branched on whether a leg filled, and this is the filled
    # side: re-executing is refused for a book-revision mismatch and takes the
    # approval with it, so the one thing the error must not say is "resume".
    assert "A leg has already FILLED" in message
    assert "Reconcile the placed legs by hand" in message
    assert "resume by re-executing" not in message
    approval = session.registry.get_approval_request(approval_id)
    assert approval["status"] == "approved"
    assert not approval["invalidated_reason"]
    assert session.registry.get_plan(plan_id)["state"] == "submitted"
    # A half-booked plan, not a failure that changed nothing: leg 1 really
    # filled and leg 2's row rolled back with its transaction.
    assert [row["state"] for row in session.registry.list_orders(50)] == [
        "filled"]
    # Nothing filled the plan, so nothing may claim the desk booked it.
    assert _booked_events(session) == []


def test_a_part_filled_plan_cannot_be_resumed_which_is_why_the_error_says_so(
        session, monkeypatch):
    """The fact the filled branch's remedy rests on, executed rather than
    asserted.

    The approval binds `book_revision` at approval time, and a filled leg moves
    it — so the resume the unfilled branch names is, on a part-filled plan,
    refused for a revision mismatch AND invalidates the approval on the way
    past. Keeping the authority is necessary but not yet sufficient here: the
    carve-out `execute_plan` already makes one layer down (`already_started`,
    `qlab/trader/plan.py`) has no counterpart at the approval layer. When it
    gains one, this test and the sentence it pins change together.
    """
    plan_id = _checked_plan(session)
    approval_id = _open_request(session, plan_id)
    _broker_fails_on_leg(monkeypatch, 2, "venue rejected the order")

    with pytest.raises(RuntimeError, match="venue rejected the order"):
        session.book_current_proposal(_book_body(session, plan_id), True)
    assert session.registry.get_approval_request(approval_id)["status"] == (
        "approved")

    result = session.execute_plan_with_approval(
        plan_id, {"approval_id": approval_id}, True)

    assert result["executed"] is False
    assert result["blocked_by"] == "approval"
    assert "book moved since approval (revision mismatch)" in result["reasons"]
    assert session.registry.get_approval_request(approval_id)["status"] == (
        "invalidated")


def test_the_kept_approval_is_what_finishes_the_half_booked_plan(session,
                                                                 monkeypatch):
    """The resume, end to end — the reason the authority is kept at all.

    The venue refuses the first leg, so the plan is `submitted` with no fill:
    the book has not moved, the approval still binds it, and re-executing
    against that same approval places every leg and spends it. With the
    approval withdrawn this answers `blocked_by: approval` and the plan can
    only be re-proposed — which is a different plan from the one at the broker.
    """
    plan_id = _checked_plan(session)
    approval_id = _open_request(session, plan_id)
    _broker_fails_on_leg(monkeypatch, 1, "venue timed out")

    with pytest.raises(RuntimeError, match="venue timed out") as raised:
        session.book_current_proposal(_book_body(session, plan_id), True)

    assert session.registry.get_plan(plan_id)["state"] == "submitted"
    assert session.registry.list_orders(50) == []
    # The other side of the branch: nothing filled, so the resume below is the
    # remedy the error is allowed to name — and the next lines run it.
    message = str(raised.value)
    assert "No leg has filled" in message
    assert "resume by re-executing this plan against that same approval" in (
        message)

    result = session.execute_plan_with_approval(
        plan_id, {"approval_id": approval_id}, True)

    assert result["executed"] is True
    assert session.registry.get_approval_request(approval_id)["status"] == (
        "consumed")
    assert len(session.registry.list_orders(50)) == len(
        session.mandate.universe_whitelist)


def test_a_withdrawal_that_cannot_move_the_approval_reports_the_real_failure(
        session, monkeypatch):
    """The bookkeeping must not become the story.

    `transition_approval` refuses an illegal edge with `PermissionError`, and
    the book route answers `PermissionError` with a 400 — so a withdrawal that
    cannot run replaced a genuine fault with a refusal describing the approval
    lifecycle, and turned a 500 into a 400. Reached the way it happens: the
    execution consumed the approval and then raised, so `approved -> consumed`
    has already been spent and `consumed -> invalidated` is not a legal edge.
    """
    plan_id = _checked_plan(session)
    approval_id = _open_request(session, plan_id)
    real = session.registry.record_event

    def _record(kind, payload):
        if kind == "approval_consumed":
            raise RuntimeError("the audit write failed")
        return real(kind, payload)

    monkeypatch.setattr(session.registry, "record_event", _record)

    with pytest.raises(RuntimeError, match="the audit write failed") as raised:
        handle_api(session, "POST", "/api/desk/proposal/book", {},
                   _book_body(session, plan_id))

    # Chained onto the real failure rather than replacing it — which is what
    # the comment on this block always claimed and the code did not do.
    assert isinstance(raised.value.__cause__, PermissionError)
    assert session.registry.get_approval_request(approval_id)["status"] == (
        "consumed")


# --- F3: one research workflow at a time -------------------------------------


def _driving(session, workflow_id, template="regime_review"):
    """Pin the owner's one coordinator slot to a named running workflow."""
    session.coordinator_status = lambda: {
        "driving": True, "workflow_id": workflow_id,
        "can_drive": True, "reason": ""}


def test_a_second_start_is_refused_by_the_name_of_the_one_already_running(session):
    status, first = handle_api(
        session, "POST", "/api/workflows/start", {},
        {"goal": "[regime_review] re-read the panel", "offline": True})
    assert status == 200
    workflow_id = first["workflow_id"]
    _driving(session, workflow_id)

    status, refused = handle_api(
        session, "POST", "/api/workflows/start", {},
        {"goal": "another one", "offline": True})
    assert status == 409
    assert refused["error"] == (
        f"a research workflow is already running: regime_review ({workflow_id})")
    assert refused["running"]["workflow_id"] == workflow_id
    assert refused["running"]["template"] == "regime_review"


def test_an_idle_coordinator_starts_as_before(session):
    session.coordinator_status = lambda: {"driving": False, "workflow_id": ""}
    status, started = handle_api(
        session, "POST", "/api/workflows/start", {},
        {"goal": "review the paper portfolio", "offline": True})
    assert status == 200 and started["current_phase"] == "analyst"


# --- K1: the chat starts its own research ------------------------------------


def _no_drive(session):
    """Register the workflow without spawning a coordinator for it."""
    session.drive_workflow = lambda wid, goal, roles=(): {
        "driving": False, "reason": "pinned off in tests"}
    session.coordinator_status = lambda: {"driving": False, "workflow_id": ""}


def test_the_chat_starts_a_research_template_and_gets_its_id(session):
    _no_drive(session)
    session.atlas.set_mode("research")
    status, started = handle_api(
        session, "POST", "/api/workflows/start", {},
        {"template_id": "regime_review", "goal": "the panel moved",
         "offline": True})
    assert status == 200
    assert started["template_id"] == "regime_review"
    assert started["workflow_id"]
    assert [step["phase"] for step in started["steps"]] == [
        "analyst", "challenger", "optimizer", "referee", "reporter"]


def test_a_plan_creating_template_is_refused_by_name_in_research_mode(session):
    _no_drive(session)
    session.atlas.set_mode("research")
    status, refused = handle_api(
        session, "POST", "/api/workflows/start", {},
        {"template_id": "desk_rebalance_review", "offline": True})
    assert status == 400
    assert "desk_rebalance_review" in refused["error"]
    assert "Propose mode" in refused["error"]


def test_the_same_template_starts_in_propose_mode(session):
    _no_drive(session)
    # The data half of the gate, pinned: offline synthetic prices are never
    # paper-proposal-eligible, and this test is about the MODE half.
    facts = session.atlas_facts(True)
    facts["data"]["eligible_for_paper_proposal"] = True
    session.atlas_facts = lambda offline, *, consume_flip=False: facts
    session.atlas.set_mode("propose")
    status, started = handle_api(
        session, "POST", "/api/workflows/start", {},
        {"template_id": "desk_rebalance_review", "offline": True})
    assert status == 200 and started["template_id"] == "desk_rebalance_review"


def test_the_chat_writes_a_task_down_and_never_twice_in_a_day(session):
    status, created = handle_api(
        session, "POST", "/api/atlas/tasks", {},
        {"kind": "regime_review", "reason": "the operator asked for one"})
    assert status == 200
    assert created["created"] is True and created["status"] == "queued"
    assert created["template_id"] == "regime_review"

    status, again = handle_api(
        session, "POST", "/api/atlas/tasks", {},
        {"kind": "regime_review", "reason": "asked twice"})
    assert status == 200
    assert again["created"] is False
    assert again["task_id"] == created["task_id"]

    status, refused = handle_api(
        session, "POST", "/api/atlas/tasks", {},
        {"kind": "do_whatever", "reason": "why not"})
    assert status == 400
    assert "do_whatever" in refused["error"]


# --- K1: stale work expires ---------------------------------------------------


def _stale_trigger(session, day: str, name: str) -> str:
    session.registry.create_atlas_task(
        name, f"drift_breach|{day}|ACWI|{name}", "drift_breach", {},
        "desk_rebalance_review")
    return name


def test_fifty_stale_triggers_expire_in_one_pass_and_a_fresh_one_stays(session):
    from datetime import date, timedelta

    today = date.today()
    old = (today - timedelta(days=40)).isoformat()
    for i in range(50):
        _stale_trigger(session, old, f"old-{i}")
    fresh = _stale_trigger(session, today.isoformat(), "fresh")

    first = session.expire_stale_atlas_work()
    assert len(first["expired_tasks"]) == 50
    assert fresh not in first["expired_tasks"]
    assert session.registry.get_atlas_task("old-0")["status"] == "expired"
    assert "older than the 5-day cutoff" in (
        session.registry.get_atlas_task("old-0")["error"])
    assert session.registry.get_atlas_task(fresh)["status"] == "queued"

    # Idempotent: nothing is expired twice.
    assert session.expire_stale_atlas_work()["expired_tasks"] == []


def test_an_idle_workflow_is_marked_stale_once_and_never_deleted(session):
    from datetime import datetime, timedelta, timezone

    workflow_id = session.registry.start_workflow(
        "portfolio_review", {"goal": "[regime_review] stalled"},
        phases=("analyst", "challenger", "optimizer", "referee", "reporter"),
    )["workflow_id"]
    long_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    session.registry.con.execute(
        "UPDATE workflows SET updated_at=? WHERE workflow_id=?",
        [long_ago, workflow_id])

    first = session.expire_stale_atlas_work()
    assert first["stale_workflows"] == [workflow_id]
    assert session.registry.get_workflow(workflow_id)["status"] == "stale"
    assert session.expire_stale_atlas_work()["stale_workflows"] == []


def test_system_status_counts_what_expired_and_what_went_stale(session):
    from datetime import date, timedelta

    old = (date.today() - timedelta(days=40)).isoformat()
    _stale_trigger(session, old, "old-1")
    session.expire_stale_atlas_work()

    status, payload = handle_api(session, "GET", "/api/system", {}, {})
    assert status == 200
    assert payload["expired_tasks"] == 1
    assert payload["stale_workflows"] == 0


def test_the_desk_task_list_does_not_show_an_expired_trigger_as_pending(session):
    from datetime import date, timedelta

    old = (date.today() - timedelta(days=40)).isoformat()
    _stale_trigger(session, old, "old-1")
    _stale_trigger(session, date.today().isoformat(), "fresh")
    session.expire_stale_atlas_work()

    shown = {row["task_id"] for row in session.atlas_task_rows(10)}
    assert shown == {"fresh"}


# --- fix round 1 --------------------------------------------------------------


def _stale_workflow(session, goal: str = "[regime_review] stalled") -> str:
    """A workflow the owner has already marked stale."""
    from datetime import datetime, timedelta, timezone

    workflow_id = session.registry.start_workflow(
        "portfolio_review", {"goal": goal})["workflow_id"]
    session.registry.con.execute(
        "UPDATE workflows SET updated_at=? WHERE workflow_id=?",
        [(datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
         workflow_id])
    session.expire_stale_atlas_work()
    assert session.registry.get_workflow(workflow_id)["status"] == "stale"
    return workflow_id


def test_the_operator_can_resume_a_workflow_the_desk_marked_stale(session):
    workflow_id = _stale_workflow(session)
    status, resumed = handle_api(
        session, "POST", f"/api/workflows/{workflow_id}/resume", {}, {})
    assert status == 200
    assert resumed["status"] == "running" and resumed["status"] != "stale"


def test_a_stale_workflow_frees_the_task_bound_to_it(session):
    """`stale` is in the unsuccessful set so reconciliation can act on it.

    Without this the task waits on a run nobody will ever walk again, and the
    drive sweep keeps spawning a coordinator for it every beat.
    """
    workflow_id = _stale_workflow(session)
    session.registry.create_atlas_task(
        "task-bound", "regime_flip|2026-07-01|ACWI|x", "regime_flip", {},
        "regime_review")
    session.registry.update_atlas_task(
        "task-bound", status="running", workflow_id=workflow_id)

    session.atlas_observe(True)

    task = session.registry.get_atlas_task("task-bound")
    assert task["status"] == "failed"
    assert workflow_id in task["error"] and "stale" in task["error"]


def test_a_request_path_read_does_not_eat_the_regime_flip(session):
    """Assembling facts to answer a question must not consume a state change.

    `_atlas_regime_facts` latches the robust state it saw. While every caller
    latched, a chat-initiated start between the panel refresh and the next
    observe swallowed the flip, and `regime_review` was never queued — the
    exact bug the hardcoded-regime fix was written to remove.
    """
    session._desk_read = {"panel": {"robust_state": "calm"}}
    session.atlas_facts(True, consume_flip=True)

    session._desk_read = {"panel": {"robust_state": "stress"}}
    # Three request-path reads. Each reports the flip; none may consume it.
    assert session.atlas_facts(True)["regime"]["flip"] is True
    handle_api(session, "GET", "/api/atlas/startable", {}, {})
    session.atlas_actionables(True)

    assert session.atlas_facts(True, consume_flip=True)["regime"]["flip"] is True
    # And the observe DID consume it: the next one is not a second flip.
    assert session.atlas_facts(True, consume_flip=True)["regime"]["flip"] is False


def test_starting_a_template_from_the_chat_leaves_the_flip_for_the_observe(session):
    session.drive_workflow = lambda wid, goal, roles=(): {
        "driving": False, "reason": "pinned off in tests"}
    session.coordinator_status = lambda: {"driving": False, "workflow_id": ""}
    session.atlas.set_mode("research")
    session._desk_read = {"panel": {"robust_state": "calm"}}
    session.atlas_facts(True, consume_flip=True)
    session._desk_read = {"panel": {"robust_state": "stress"}}

    handle_api(session, "POST", "/api/workflows/start", {},
               {"template_id": "regime_review", "offline": True})

    assert session.atlas_facts(True, consume_flip=True)["regime"]["flip"] is True


def test_open_workflows_does_not_count_work_that_is_already_resolved(session):
    _stale_workflow(session)
    assert session.atlas_facts(True)["open_workflows"] == 0
    session.registry.start_workflow("portfolio_review", {"goal": "live one"})
    assert session.atlas_facts(True)["open_workflows"] == 1


def test_a_chat_created_proposal_does_not_outlive_the_day_it_answered(session):
    """`atlas_create_task` is a second minter; it must sweep like the first."""
    from datetime import date, timedelta

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    universe = ",".join(sorted(session.mandate.universe_whitelist))
    session.registry.create_atlas_task(
        "yesterdays", f"proposal:regime_review|{yesterday}|{universe}|regime_review",
        "proposal:regime_review", {}, "regime_review", origin="proposal")

    session.atlas_create_task("research_review", "a fresh question")

    assert session.registry.get_atlas_task("yesterdays")["status"] == "expired"


def test_the_tick_also_sweeps_a_proposal_nobody_minted_over(session):
    from datetime import date, timedelta

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    session.registry.create_atlas_task(
        "yesterdays", f"proposal:regime_review|{yesterday}|ACWI|regime_review",
        "proposal:regime_review", {}, "regime_review", origin="proposal")

    session.expire_stale_atlas_work()

    assert session.registry.get_atlas_task("yesterdays")["status"] == "expired"


def test_resume_is_refused_by_name_for_a_plan_creating_run_below_propose(session):
    """Research mode may not resume its way into a plan.

    A `desk_rebalance_review` started in Propose and interrupted is a run that
    ends at a checked plan. Resuming it in Research would walk that graph to
    its plan under a mode that may not create one.
    """
    workflow_id = session.registry.start_workflow(
        "portfolio_review",
        {"goal": "[desk_rebalance_review] the full review"})["workflow_id"]
    session.registry.interrupt_workflow(workflow_id, "stopped for the test")

    session.atlas.set_mode("research")
    status, refused = handle_api(
        session, "POST", f"/api/workflows/{workflow_id}/resume", {}, {})
    assert status == 400
    assert "desk_rebalance_review" in refused["error"]
    assert "Propose" in refused["error"]

    session.atlas.set_mode("propose")
    status, resumed = handle_api(
        session, "POST", f"/api/workflows/{workflow_id}/resume", {}, {})
    assert status == 200 and resumed["status"] == "running"


def test_a_research_run_resumes_in_research_mode(session):
    workflow_id = session.registry.start_workflow(
        "portfolio_review", {"goal": "[regime_review] read it again"})["workflow_id"]
    session.registry.interrupt_workflow(workflow_id, "stopped for the test")
    session.atlas.set_mode("research")

    status, resumed = handle_api(
        session, "POST", f"/api/workflows/{workflow_id}/resume", {}, {})
    assert status == 200 and resumed["status"] == "running"


def test_a_registered_but_undriven_run_says_why_it_is_not_moving(session):
    """The window between the 409 check and the drive is real; it must speak."""
    session.coordinator_status = lambda: {"driving": False, "workflow_id": ""}
    session.drive_workflow = lambda wid, goal, roles=(): {
        "driving": False, "reason": "a coordinator is already driving wf-other"}
    session.atlas.set_mode("research")

    status, started = handle_api(
        session, "POST", "/api/workflows/start", {},
        {"template_id": "regime_review", "offline": True})

    assert status == 200
    assert started["driving"] is False
    assert started["drive_reason"] == "a coordinator is already driving wf-other"


def test_the_409_names_the_phase_the_goal_and_falls_back_to_the_kind(session):
    stamped = session.registry.start_workflow(
        "portfolio_review", {"goal": "[regime_review] re-read the panel"})
    _driving(session, stamped["workflow_id"])
    status, refused = handle_api(
        session, "POST", "/api/workflows/start", {}, {"goal": "x", "offline": True})
    assert status == 409
    assert refused["running"]["current_phase"] == "analyst"
    assert refused["running"]["goal"] == "[regime_review] re-read the panel"

    # A run a human started carries no template stamp; the kind is the name.
    bare = session.registry.start_workflow(
        "portfolio_review", {"goal": "just have a look"})
    _driving(session, bare["workflow_id"])
    status, refused = handle_api(
        session, "POST", "/api/workflows/start", {}, {"goal": "x", "offline": True})
    assert status == 409
    assert refused["running"]["template"] == "portfolio_review"
    assert refused["error"] == (
        "a research workflow is already running: portfolio_review "
        f"({bare['workflow_id']})")


# --- universe_change approvals: a contender enters only by the operator ------


def _a_contender_outside(session) -> str:
    from qlab.core.universe import load_universe

    held = set(session.mandate.universe_whitelist)
    catalog = load_universe()
    for ticker in catalog.extended_tickers + catalog.stock_tickers:
        if ticker not in held:
            return ticker
    raise AssertionError("the catalog holds nothing outside the mandate")


def test_universe_change_approval_opens_once_and_widens_only_when_approved(session):
    contender = _a_contender_outside(session)
    body = {"kind": "universe_change", "ticker": contender,
            "memo_decision_id": "dec-scout"}

    status, opened = handle_api(session, "POST", "/api/approvals", {}, body)
    assert status == 200, opened
    approval_id = opened["approval_id"]
    assert opened["status"] == "pending"
    assert opened["kind"] == "universe_change"

    # One pending question per ticker: a second scout memo must not re-ask it.
    status, again = handle_api(session, "POST", "/api/approvals", {}, body)
    assert status == 200
    assert again["approval_id"] == approval_id
    assert again["deduped"] is True
    assert len([row for row in session.registry.list_approval_requests(50)
                if row["kind"] == "universe_change"]) == 1

    # The GET shape I4 reads.
    status, listed = handle_api(session, "GET", "/api/approvals", {}, {})
    row = next(r for r in listed["approvals"] if r["approval_id"] == approval_id)
    assert row["kind"] == "universe_change"
    assert row["plan_id"] is None
    assert row["targets_hash"] is None
    assert row["summary"] == {"ticker": contender,
                              "memo_decision_id": "dec-scout"}

    assert contender not in session.mandate.universe_whitelist

    status, decided = handle_api(
        session, "POST", f"/api/approvals/{approval_id}/approve", {}, {})
    assert status == 200, decided
    assert decided["status"] == "approved"
    assert decided["ticker"] == contender
    assert contender in session.mandate.universe_whitelist

    from qlab.trader.mandate import load_mandate, load_mandate_overrides
    assert load_mandate_overrides()["universe_add"] == [contender]
    assert contender in load_mandate().universe_whitelist

    events = session.registry.read_events_of_kind("universe_change_approved")
    assert [e["payload"]["ticker"] for e in events] == [contender]


def test_universe_change_refuses_a_ticker_the_catalog_does_not_carry(session):
    status, refused = handle_api(
        session, "POST", "/api/approvals", {},
        {"kind": "universe_change", "ticker": "NOTATICKER",
         "memo_decision_id": "d"})
    assert status == 400
    assert "universe catalog" in refused["error"]


def test_universe_change_refuses_a_name_already_in_the_mandate(session):
    held = session.mandate.universe_whitelist[0]
    status, refused = handle_api(
        session, "POST", "/api/approvals", {},
        {"kind": "universe_change", "ticker": held, "memo_decision_id": "d"})
    assert status == 400
    assert held in refused["error"]


def test_the_method_route_still_refuses_the_universe_key(session):
    """`universe_add` is overridable, but not from the method route: only an
    approved universe_change writes it."""
    status, refused = handle_api(session, "POST", "/api/desk/method", {},
                                 {"universe_add": ["XLK"]})
    assert status == 400
    assert "universe_add" in refused["error"]


# --- H1: running a predictor lane from the desk ------------------------------
#
# The board is a *paired* comparison, so a lane alone is not evidence: the
# route always runs the requested lane(s) plus the baseline. The panel is the
# offline synthetic one, and the search is deliberately tiny — this exercises
# the route, not the estimator (tests/test_board.py owns the estimator).

_LANE_RUN = {"universe": "core", "lookback_days": 420, "offline": True,
             "as_of": "2022-06-30", "null_trials": 2, "n_splits": 3,
             "alphas": [1.0], "map_weights": [1.0]}


def test_predictor_run_runs_the_lane_with_its_baseline(session):
    from qlab.research.board import BASELINE_MODEL_ID

    status, out = handle_api(
        session, "POST", "/api/research/predictors/run", {},
        dict(_LANE_RUN, model="kernel:zz"))
    assert status == 200, out
    # A challenger without its control is not a comparison, so the baseline is
    # added by the route rather than asked for.
    assert out["models"] == ["kernel:zz", BASELINE_MODEL_ID]
    assert set(out["ranking"]) == {"kernel:zz", BASELINE_MODEL_ID}
    assert out["champion"] is None or out["champion"] in out["ranking"]
    assert out["board"]["baseline"] == BASELINE_MODEL_ID
    assert out["board"]["search"]["models"] == out["models"]

    runs = session.registry.list_runs(limit=5)
    assert [r["kind"] for r in runs] == ["predictor_board"]
    assert runs[0]["run_id"] == out["run_id"]
    spec = runs[0]["spec"]
    assert spec["dsr_trial_counted"] is False
    assert spec["board"]["ranking"] == out["ranking"]
    # Research evidence writes no backtest row and no solution.
    report = session.registry.report(out["run_id"])
    assert report["backtests"] == [] and report["solutions"] == []


def test_predictor_run_accepts_a_list_of_lanes(session):
    status, out = handle_api(
        session, "POST", "/api/research/predictors/run", {},
        dict(_LANE_RUN, model=["kernel:angle", "groupwise:zz"]))
    assert status == 200, out
    assert out["models"] == ["kernel:angle", "groupwise:zz", "ridge:none"]
    assert set(out["ranking"]) == set(out["models"])


def test_predictor_run_refuses_an_unknown_lane_naming_the_lanes(session):
    status, refused = handle_api(
        session, "POST", "/api/research/predictors/run", {},
        dict(_LANE_RUN, model="forest:deep"))
    assert status == 400
    assert "forest:deep" in refused["error"]
    # The refusal names the board's own lane set, so an operator can retry.
    assert "kernel:zz" in refused["error"]
    assert "groupwise:angle_zz" in refused["error"]
    assert session.registry.list_runs(limit=5) == []


def test_predictor_run_refuses_a_lookback_too_short_to_fold(session):
    status, refused = handle_api(
        session, "POST", "/api/research/predictors/run", {},
        dict(_LANE_RUN, model="kernel:zz", lookback_days=120))
    assert status == 400
    assert "300" in refused["error"]
    assert session.registry.list_runs(limit=5) == []


def test_predictor_run_shows_up_in_the_board_read(session):
    status, before = handle_api(
        session, "GET", "/api/research/predictors", {}, {})
    assert status == 200 and before["status"] == "never_ran"
    assert session.predictor_board_summary()["status"] == "never_ran"

    _, out = handle_api(session, "POST", "/api/research/predictors/run", {},
                        dict(_LANE_RUN, model="kernel:zz"))

    status, after = handle_api(
        session, "GET", "/api/research/predictors", {}, {})
    assert status == 200
    assert after["status"] == "ok"
    assert after["run_id"] == out["run_id"]
    # The summary is TTL-cached against the run revision; a new run must move
    # it in the same request, not one TTL later.
    summary = session.predictor_board_summary()
    assert summary["status"] == "ok"
    assert summary["run_id"] == out["run_id"]
    assert summary["ranking"] == out["ranking"]


def _owner_post(session, path, body, timeout=15):
    """POST through a real owner over loopback, so `do_POST`'s dispatch —
    which is where the lock exemption and the 500 handler live — is what runs.

    `handle_api` cannot answer either question: the exemption is a branch above
    it, and an unhandled exception becomes a status only in the handler.
    """
    import json as _json
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    handler = type("H", (ui_server._Handler,), {"session": session})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=_json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, _json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _json.loads(exc.read())
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_predictor_run_is_dispatched_off_the_dispatch_lock(session):
    """Fitting a board is seconds of numpy; holding `_LOCK` across it would
    freeze the snapshot poll and every approval behind it.

    Membership in the tuple is the intent; the request is the proof. The probe
    runs where the real fit would, on the handler thread, and takes the lock
    non-blockingly — which can only succeed if dispatch did not already hold
    it.

    The probe then CALLS the real `run_predictor_lane` rather than standing in
    for it. `run_predictor_lane` takes `_LOCK` itself, so the second half of
    this claim — that the method is reachable without deadlocking on a
    non-reentrant lock — is only proved by running it; a stub that returned a
    dict in its place proved the exemption and nothing about the deadlock.
    """
    assert "/api/research/predictors/run" in ui_server._LOCK_EXEMPT_POSTS

    free = {}
    real = session.run_predictor_lane

    def probe(request):
        free["acquired"] = ui_server._LOCK.acquire(blocking=False)
        if free["acquired"]:
            ui_server._LOCK.release()
        # If the route held `_LOCK`, this call hangs rather than failing — the
        # deadlock IS the failure, and the suite's own timeout is what reports
        # it. There is no way to assert a non-reentrant re-acquire "would have"
        # deadlocked without deadlocking.
        return real(request)

    session.run_predictor_lane = probe
    status, out = _owner_post(session, "/api/research/predictors/run",
                              dict(_LANE_RUN, model="kernel:zz"))
    assert status == 200, out
    assert free["acquired"] is True
    # The real fit ran: a probe that returned a dict of its own would satisfy
    # every assertion above without the lane ever taking `_LOCK`.
    assert out["run_id"]
    assert session.registry.list_runs(limit=5) != []


def test_predictor_run_refuses_a_future_as_of(session):
    """The look-ahead tripwire the MCP twin has. A board fitted on a snapshot
    dated tomorrow is not evidence about anything."""
    from datetime import timedelta as _timedelta

    ahead = (date.today() + _timedelta(days=1)).isoformat()
    status, refused = handle_api(
        session, "POST", "/api/research/predictors/run", {},
        dict(_LANE_RUN, model="kernel:zz", as_of=ahead))
    assert status == 400
    assert "look-ahead" in refused["error"]
    assert ahead in refused["error"]
    assert session.registry.list_runs(limit=5) == []


def test_predictor_run_lets_a_fit_failure_be_a_500_not_a_bad_request(
        session, monkeypatch):
    """A ValueError out of the estimator is not the operator's mistake.

    The route validates the body up front and catches nothing else, so a
    failure in the data layer or the fit reaches the handler's 500 — telling
    an operator their request was wrong when it was not sends them editing a
    correct request forever.
    """
    import qlab.research.board as board_module

    def explode(*args, **kwargs):
        raise ValueError("the folds did not converge")

    monkeypatch.setattr(board_module, "run_predictor_board", explode)
    status, out = _owner_post(session, "/api/research/predictors/run",
                              dict(_LANE_RUN, model="kernel:zz"))
    assert status == 500
    assert "the folds did not converge" in out["error"]
    assert session.registry.list_runs(limit=5) == []


def test_predictor_run_offline_inherits_the_desks_own_default(
        session, monkeypatch):
    """An absent `offline` key is not a choice of False."""
    import qlab.core.data as market_module

    real = market_module.snapshot
    seen = []

    def spy(tickers, as_of, **kwargs):
        seen.append(kwargs.get("offline"))
        # Never actually reach the network from a test: what is under test is
        # which flag the route computed, not what a live fetch would return.
        return real(tickers, as_of, **{**kwargs, "offline": True})

    monkeypatch.setattr(market_module, "snapshot", spy)
    body = {k: v for k, v in _LANE_RUN.items() if k != "offline"}
    for default in (True, False):
        session.offline_default = default
        status, out = handle_api(
            session, "POST", "/api/research/predictors/run", {},
            dict(body, model="kernel:zz"))
        assert status == 200, out
    assert seen == [True, False]


def test_both_predictor_board_writers_build_one_run_spec(session):
    """The route and the MCP tool persist the same row shape, or a reader of
    the runs table has to know which surface wrote it."""
    from qlab.mcp.guardrails import LabState
    from qlab.mcp.quant_lab import register_lab_tools

    shared = {"universe": "core", "lookback_days": 420, "as_of": "2022-06-30",
              "models": ["kernel:zz", "ridge:none"], "alphas": [1.0],
              "map_weights": [1.0], "n_splits": 3, "null_trials": 2}

    _, out = handle_api(
        session, "POST", "/api/research/predictors/run", {},
        dict(_LANE_RUN, model=shared["models"]))
    route_spec = session.registry.list_runs(limit=1)[0]["spec"]

    class _App:
        """The minimal registrar the tool module expects."""

        def __init__(self):
            self.tools = {}

        def tool(self, name):
            def register(fn):
                self.tools[name] = fn
                return fn
            return register

    tool_registry = Registry(":memory:")
    app = _App()
    register_lab_tools(
        app, LabState(offline=True, registry=tool_registry), owner_only=True)
    app.tools["research.predictor_board"](**shared)
    tool_spec = tool_registry.list_runs(limit=1)[0]["spec"]
    tool_registry.close()

    assert sorted(route_spec) == sorted(tool_spec)
    # Same inputs, same row: only the board object may differ, and here it
    # does not either — the same panel through the same estimator.
    for key in sorted(route_spec):
        assert route_spec[key] == tool_spec[key], key
    assert route_spec["as_of"] == "2022-06-30"
    assert out["run_id"] == session.registry.list_runs(limit=1)[0]["run_id"]

    # Agreeing today is not the same as being unable to diverge. Both writers
    # must go through one builder, or the next field added to one is added to
    # only one — which is how `as_of` drifted in the first place.
    from qlab.research.board import predictor_run_spec

    assert route_spec == predictor_run_spec(
        as_of=route_spec["as_of"],
        universe=route_spec["universe"],
        tickers=route_spec["tickers"],
        lookback_days=route_spec["lookback_days"],
        source=route_spec["source"],
        snapshot_id=route_spec["snapshot_id"],
        board=route_spec["board"],
    )


# --- K3b: serving the visuals a build draws ----------------------------------


def test_visuals_route_lists_the_circuit(session):
    status, out = handle_api(session, "GET", "/api/visuals", {}, {})
    assert status == 200
    names = [v["name"] for v in out["visuals"]]
    assert "quantum_circuit" in names
    assert names == sorted(names)
    assert all(v["title"] for v in out["visuals"])


def test_visual_route_renders_one_wire_per_feature(session):
    status, out = handle_api(
        session, "GET", "/api/visuals/quantum_circuit",
        {"features": ["mom_21d,vol_21d,disp_5d"]}, {})
    assert status == 200, out
    assert out["name"] == "quantum_circuit"
    assert out["title"]
    assert out["params"]["features"] == ["mom_21d", "vol_21d", "disp_5d"]
    wires = [line for line in out["text"].splitlines() if "|0>" in line]
    assert len(wires) == 3
    assert all("RY(" in line for line in wires)


def test_visual_route_parses_angles_and_the_kernel(session):
    status, out = handle_api(
        session, "GET", "/api/visuals/quantum_circuit",
        {"features": ["a,b,c"], "angles": ["0.5,1.0,-1.5"],
         "kernel": ["zz"]}, {})
    assert status == 200, out
    assert out["params"]["angles"] == [0.5, 1.0, -1.5]
    assert out["params"]["kernel"] == "zz"
    entangler = [line for line in out["text"].splitlines()
                 if line.startswith("ZZ")]
    assert len(entangler) == 1
    assert "(a,b)" in entangler[0]


def test_visual_route_without_zz_draws_no_entangler(session):
    _, out = handle_api(
        session, "GET", "/api/visuals/quantum_circuit",
        {"features": ["a,b,c"], "kernel": ["angle"]}, {})
    assert not [line for line in out["text"].splitlines()
                if line.startswith("ZZ")]


def test_visual_route_404s_an_unknown_name_naming_the_known(session):
    status, refused = handle_api(
        session, "GET", "/api/visuals/nope", {}, {})
    assert status == 404
    assert "nope" in refused["error"]
    assert "quantum_circuit" in refused["error"]


def test_visual_route_400s_an_unparseable_angle(session):
    status, refused = handle_api(
        session, "GET", "/api/visuals/quantum_circuit",
        {"features": ["a,b"], "angles": ["0.5,x"]}, {})
    assert status == 400
    assert "angles" in refused["error"]


def test_visual_route_400s_more_features_than_the_drawer_will_draw(session):
    features = ",".join(f"f{i}" for i in range(13))
    status, refused = handle_api(
        session, "GET", "/api/visuals/quantum_circuit",
        {"features": [features]}, {})
    assert status == 400
    assert "13" in refused["error"] and "12" in refused["error"]


def _watch_workflow(session, phases=("analyst", "scout", "reporter")):
    """A portfolio_watch run with its analyst done, ready for the scout."""
    started = session.start_workflow(
        {"kind": "portfolio_review", "goal": "[portfolio_watch] watch"},
        phases=tuple(phases))
    workflow_id = started["workflow_id"]
    handle_api(session, "POST", "/api/workflows/analyst", {}, {
        "workflow_id": workflow_id, "status": "done", "summary": "read",
        "artifacts": {"moment_set_id": "m", "objective_id": "o",
                      "decision_id": "d", "regime": "neutral",
                      "regime_summary": "steady"}})
    return workflow_id


def _complete_scout(session, workflow_id, contenders, memo="dec-memo",
                    phase="scout"):
    return handle_api(session, "POST", f"/api/workflows/{phase}", {}, {
        "workflow_id": workflow_id, "status": "done", "summary": "scouted",
        "artifacts": {"memo_decision_id": memo, "contenders": contenders}})


def _pending_universe_changes(session):
    return [row for row in session.registry.list_approval_requests(300, "pending")
            if row["kind"] == "universe_change"]


def test_a_losing_approve_cannot_roll_back_the_winners_ticker(session, monkeypatch):
    """Read -> widen -> transition is one critical section (invariant 9).

    The failure this reproduces: a second approval widens (a no-op append,
    the ticker is already recorded), its transition fails, and its rollback
    removes a ticker the FIRST approval had already recorded as approved.

    The `flaky` patch below is UNREACHABLE BY DESIGN, and that is the finding,
    not an oversight: the route now refuses the second approve as `not pending`
    *before* it reaches the widening, so `transition_approval` is called once
    and the raising branch never runs. That the route never gets that far is
    the stronger statement, and it is asserted (`calls == 1`). The patch is
    kept so this test still fails loudly if the ordering is ever inverted — a
    second call would then raise, the rollback would run, and the widening
    assertions at the end would catch what it removed.
    """
    from qlab.trader.mandate import load_mandate_overrides

    contender = _a_contender_outside(session)
    _, opened = handle_api(session, "POST", "/api/approvals", {}, {
        "kind": "universe_change", "ticker": contender,
        "memo_decision_id": "dec-scout"})
    approval_id = opened["approval_id"]

    real = session.registry.transition_approval
    calls = []

    def flaky(*args, **kwargs):
        calls.append(args)
        if len(calls) > 1:
            raise RuntimeError("the registry refused the second transition")
        return real(*args, **kwargs)

    monkeypatch.setattr(session.registry, "transition_approval", flaky)

    status, _ = handle_api(
        session, "POST", f"/api/approvals/{approval_id}/approve", {}, {})
    assert status == 200
    status, refused = handle_api(
        session, "POST", f"/api/approvals/{approval_id}/approve", {}, {})
    assert status == 400
    assert "not pending" in refused["error"]
    # Once, not twice: the refusal lands before the widening, so the loser
    # never reaches a transition it could fail at.
    assert len(calls) == 1, calls

    # The winner's widening survived the loser.
    assert load_mandate_overrides()["universe_add"] == [contender]
    assert contender in session.mandate.universe_whitelist


def test_a_failed_transition_rolls_back_the_append_it_made(session, monkeypatch):
    """The other half of the same bound: a rollback that DID append undoes its
    own write, so an approval that never took cannot leave the mandate wide."""
    from qlab.trader.mandate import load_mandate_overrides

    contender = _a_contender_outside(session)
    _, opened = handle_api(session, "POST", "/api/approvals", {}, {
        "kind": "universe_change", "ticker": contender,
        "memo_decision_id": "dec-scout"})

    def refuse(*args, **kwargs):
        raise RuntimeError("the registry refused the transition")

    monkeypatch.setattr(session.registry, "transition_approval", refuse)
    with pytest.raises(RuntimeError):
        session.decide_approval(opened["approval_id"], "approve")
    assert contender not in session.mandate.universe_whitelist
    assert load_mandate_overrides().get("universe_add") is None


def test_a_single_name_outside_the_permitted_tiers_is_refused_at_the_door(session):
    """`universe_tier` permits core and extended only; a stocks-tier name needs
    catalog promotion before it can be paper-traded, so it may not enter the
    mandate by one approval."""
    from qlab.core.universe import load_universe

    single_name = load_universe().stock_tickers[0]
    status, refused = handle_api(session, "POST", "/api/approvals", {}, {
        "kind": "universe_change", "ticker": single_name,
        "memo_decision_id": "d"})
    assert status == 400
    assert single_name in refused["error"]
    assert "promotion" in refused["error"]


def test_the_dedupe_sees_past_a_busy_pending_queue(session):
    """A windowed scan is not a dedupe: the desk's oldest open question must
    still block a second copy of itself."""
    from qlab.governance.approval import build_universe_change_request

    contender = _a_contender_outside(session)
    _, opened = handle_api(session, "POST", "/api/approvals", {}, {
        "kind": "universe_change", "ticker": contender,
        "memo_decision_id": "dec-scout"})
    for i in range(250):
        session.registry.create_approval_request(
            build_universe_change_request(f"ZZ{i}", memo_decision_id="d"))

    _, again = handle_api(session, "POST", "/api/approvals", {}, {
        "kind": "universe_change", "ticker": contender,
        "memo_decision_id": "dec-scout"})
    assert again["deduped"] is True
    assert again["approval_id"] == opened["approval_id"]


def test_rejecting_a_universe_change_leaves_the_universe_untouched(session):
    from qlab.trader.mandate import load_mandate_overrides

    contender = _a_contender_outside(session)
    _, opened = handle_api(session, "POST", "/api/approvals", {}, {
        "kind": "universe_change", "ticker": contender,
        "memo_decision_id": "dec-scout"})
    status, decided = handle_api(
        session, "POST", f"/api/approvals/{opened['approval_id']}/reject", {}, {})
    assert status == 200 and decided["status"] == "rejected"
    assert contender not in session.mandate.universe_whitelist
    assert load_mandate_overrides().get("universe_add") is None


def test_re_asking_an_approved_ticker_is_refused_as_already_held(session):
    contender = _a_contender_outside(session)
    _, opened = handle_api(session, "POST", "/api/approvals", {}, {
        "kind": "universe_change", "ticker": contender,
        "memo_decision_id": "dec-scout"})
    handle_api(session, "POST", f"/api/approvals/{opened['approval_id']}/approve",
               {}, {})
    status, refused = handle_api(session, "POST", "/api/approvals", {}, {
        "kind": "universe_change", "ticker": contender,
        "memo_decision_id": "dec-scout-2"})
    assert status == 400
    assert "already in the mandate" in refused["error"]


# --- the scout step files the operator's questions ---------------------------


def test_the_scout_step_files_one_question_per_contender(session):
    """Filed on the SCOUT's completion, from that step's own persisted
    artifacts: the memo is durable there, and a reporter that fails afterwards
    must not take the operator's questions down with it."""
    from qlab.core.universe import load_universe

    held = set(session.mandate.universe_whitelist)
    outside = [t for t in load_universe().extended_tickers if t not in held][:2]
    assert len(outside) == 2
    workflow_id = _watch_workflow(session)
    status, _ = _complete_scout(session, workflow_id, [
        {"ticker": t, "thesis": "a. b.", "urls": ["u1", "u2"]} for t in outside])
    assert status == 200

    rows = _pending_universe_changes(session)
    assert sorted(r["summary"]["ticker"] for r in rows) == sorted(outside)
    assert {r["summary"]["memo_decision_id"] for r in rows} == {"dec-memo"}
    # A watch run creates no plan and no plan approval.
    assert not [r for r in session.registry.list_approval_requests(50)
                if r["kind"] == "plan"]

    events = session.registry.read_events_of_kind("universe_questions_filed")
    assert len(events) == 1
    assert sorted(events[0]["payload"]["opened_tickers"]) == sorted(outside)


def test_completing_the_scout_phase_twice_opens_no_second_question(session):
    from qlab.core.universe import load_universe

    held = set(session.mandate.universe_whitelist)
    contender = next(t for t in load_universe().extended_tickers if t not in held)
    workflow_id = _watch_workflow(session)
    artifacts = [{"ticker": contender, "thesis": "a. b.", "urls": ["u1", "u2"]}]
    _complete_scout(session, workflow_id, artifacts)
    _complete_scout(session, workflow_id, artifacts)     # the resume replay
    assert len(_pending_universe_changes(session)) == 1


def test_a_refused_contender_is_skipped_with_its_reason_beside_a_good_one(session):
    from qlab.core.universe import load_universe

    held = set(session.mandate.universe_whitelist)
    good = next(t for t in load_universe().extended_tickers if t not in held)
    bad = load_universe().stock_tickers[0]
    workflow_id = _watch_workflow(session)
    status, workflow = _complete_scout(session, workflow_id, [
        {"ticker": bad, "thesis": "a. b.", "urls": ["u1", "u2"]},
        {"ticker": good, "thesis": "a. b.", "urls": ["u1", "u2"]}])
    assert status == 200
    steps = {s["phase"]: s for s in workflow["steps"]}
    assert steps["scout"]["status"] == "done"

    rows = _pending_universe_changes(session)
    assert [r["summary"]["ticker"] for r in rows] == [good]
    payload = session.registry.read_events_of_kind(
        "universe_questions_filed")[0]["payload"]
    assert payload["opened_tickers"] == [good]
    skipped = {s["ticker"]: s["reason"] for s in payload["skipped"]}
    assert bad in skipped and "promotion" in skipped[bad]


def test_the_scout_files_at_most_three_questions(session):
    from qlab.core.universe import load_universe

    held = set(session.mandate.universe_whitelist)
    outside = [t for t in load_universe().extended_tickers if t not in held]
    assert len(outside) >= 4
    workflow_id = _watch_workflow(session)
    _complete_scout(session, workflow_id, [
        {"ticker": t, "thesis": "a. b.", "urls": ["u1", "u2"]}
        for t in outside])
    assert len(_pending_universe_changes(session)) == 3


def test_a_review_reporter_files_nothing(session):
    """The whole standard graph, walked to its reporter. No scout step means no
    questions — and no accidental read of a reporter summary as if it were a
    memo, which is the mistake the phase check exists to prevent."""
    reg = session.registry
    targets = {"SPY": 1.0}
    workflow_id = reg.start_workflow(
        "portfolio_review", {"goal": "[regime_review] review"})["workflow_id"]
    reg.update_workflow_phase(
        workflow_id, "analyst", "done", "read",
        {"moment_set_id": "m", "objective_id": "o", "decision_id": "d",
         "regime": "neutral", "regime_summary": "steady"})
    reg.update_workflow_phase(workflow_id, "challenger", "done", "argued",
                              {"challenger_view": "window held"})
    reg.update_workflow_phase(workflow_id, "optimizer", "done", "solved",
                              {"targets": targets, "algorithm_id": "hrp"})
    verdict_id = reg.log_verdict("d", "PASS", ["clean"], targets=targets)
    reg.update_workflow_phase(
        workflow_id, "referee", "done", "PASS",
        {"verdict": "PASS", "verdict_id": verdict_id, "targets": targets,
         "decision_id": "d"})

    # The reporter completes through the route, which is where the hook lives.
    status, workflow = handle_api(session, "POST", "/api/workflows/reporter", {}, {
        "workflow_id": workflow_id, "status": "done", "summary": "memo",
        "artifacts": {"recommendation": "hold"}})
    assert status == 200
    assert {s["phase"]: s["status"] for s in workflow["steps"]}["reporter"] == "done"
    assert _pending_universe_changes(session) == []
    assert not [r for r in reg.list_approval_requests(50)
                if r["kind"] == "universe_change"]
    assert reg.read_events_of_kind("universe_questions_filed") == []


def test_visual_route_404_speaks_the_registrys_own_sentence(session):
    """The route formats its own 404 so the catalog is walked once, which
    means the sentence can drift from the registry's. It must not."""
    import qlab.visuals as visuals

    status, refused = handle_api(
        session, "GET", "/api/visuals/nope", {}, {})
    assert status == 404
    with pytest.raises(KeyError) as raised:
        visuals.render("nope", {})
    assert refused["error"] == raised.value.args[0]


def test_visual_route_treats_an_empty_parameter_as_absent(session):
    """`?kernel=` is not a choice of a kernel. Passing "" through would be
    refused by the drawer as an unknown kernel; dropping it silently to the
    default would be a drawing nobody asked for. Absent is the honest read."""
    status, out = handle_api(
        session, "GET", "/api/visuals/quantum_circuit",
        {"features": ["a,b,c"], "kernel": [""], "angles": [""]}, {})
    assert status == 200, out
    assert "kernel" not in out["params"]
    assert "angles" not in out["params"]


def test_filing_never_undoes_the_phase_that_already_committed(session, monkeypatch):
    """The hook runs after the registry commit. Anything it raises would fail a
    request whose phase update is already durable — the coordinator would read
    a 500 and re-dispatch a phase the registry calls done."""
    from qlab.core.universe import load_universe

    held = set(session.mandate.universe_whitelist)
    contender = next(t for t in load_universe().extended_tickers if t not in held)
    workflow_id = _watch_workflow(session)

    real = session.registry.record_event

    def flaky(kind, payload):
        if kind == "universe_questions_filed":
            raise RuntimeError("the event bus fell over")
        return real(kind, payload)

    monkeypatch.setattr(session.registry, "record_event", flaky)
    status, workflow = _complete_scout(session, workflow_id, [
        {"ticker": contender, "thesis": "a. b.", "urls": ["u1", "u2"]}])
    assert status == 200
    assert {s["phase"]: s["status"] for s in workflow["steps"]}["scout"] == "done"
    # The failure is recorded rather than swallowed.
    monkeypatch.undo()
    assert session.registry.read_events_of_kind("universe_questions_failed")


def test_a_second_scout_branch_files_its_own_contenders(session):
    """The hook reads the phase that just completed, not any done scout step:
    on a suffixed graph the first branch's artifacts would otherwise be refiled
    and the second branch's contenders would never reach the operator."""
    from qlab.core.universe import load_universe

    held = set(session.mandate.universe_whitelist)
    first, second = [t for t in load_universe().extended_tickers
                     if t not in held][:2]
    workflow_id = _watch_workflow(session, phases=("analyst", "scout-1", "scout-2"))
    _complete_scout(session, workflow_id,
                    [{"ticker": first, "thesis": "a. b.", "urls": ["u1", "u2"]}],
                    memo="dec-one", phase="scout-1")
    _complete_scout(session, workflow_id,
                    [{"ticker": second, "thesis": "a. b.", "urls": ["u1", "u2"]}],
                    memo="dec-two", phase="scout-2")

    rows = {r["summary"]["ticker"]: r["summary"]["memo_decision_id"]
            for r in _pending_universe_changes(session)}
    assert rows == {first: "dec-one", second: "dec-two"}


def test_a_rejected_question_is_not_asked_again_by_a_replay(session):
    """The operator said no. A resumed scout re-completing its phase must not
    put the same question back on the desk — a refusal that a replay undoes is
    not a refusal."""
    from qlab.core.universe import load_universe

    held = set(session.mandate.universe_whitelist)
    contender = next(t for t in load_universe().extended_tickers if t not in held)
    workflow_id = _watch_workflow(session)
    artifacts = [{"ticker": contender, "thesis": "a. b.", "urls": ["u1", "u2"]}]
    _complete_scout(session, workflow_id, artifacts)
    approval_id = _pending_universe_changes(session)[0]["approval_id"]
    handle_api(session, "POST", f"/api/approvals/{approval_id}/reject", {}, {})

    _complete_scout(session, workflow_id, artifacts)
    assert _pending_universe_changes(session) == []
    payload = session.registry.read_events_of_kind(
        "universe_questions_filed")[-1]["payload"]
    skipped = {s["ticker"]: s["reason"] for s in payload["skipped"]}
    assert contender in skipped and "rejected" in skipped[contender]


def test_an_expired_question_is_not_asked_again_by_a_replay(session):
    from qlab.core.universe import load_universe

    held = set(session.mandate.universe_whitelist)
    contender = next(t for t in load_universe().extended_tickers if t not in held)
    workflow_id = _watch_workflow(session)
    artifacts = [{"ticker": contender, "thesis": "a. b.", "urls": ["u1", "u2"]}]
    _complete_scout(session, workflow_id, artifacts)
    approval_id = _pending_universe_changes(session)[0]["approval_id"]
    session.registry.transition_approval(approval_id, "expired")

    _complete_scout(session, workflow_id, artifacts)
    assert _pending_universe_changes(session) == []


def test_a_new_memo_may_re_ask_a_rejected_name(session):
    """The refusal is bound to the question, not to the ticker forever: a later
    scout memo with new evidence is a new question the operator may answer."""
    from qlab.core.universe import load_universe

    held = set(session.mandate.universe_whitelist)
    contender = next(t for t in load_universe().extended_tickers if t not in held)
    workflow_id = _watch_workflow(session)
    _complete_scout(session, workflow_id,
                    [{"ticker": contender, "thesis": "a. b.", "urls": ["u"]}],
                    memo="dec-old")
    approval_id = _pending_universe_changes(session)[0]["approval_id"]
    handle_api(session, "POST", f"/api/approvals/{approval_id}/reject", {}, {})

    status, opened = handle_api(session, "POST", "/api/approvals", {}, {
        "kind": "universe_change", "ticker": contender,
        "memo_decision_id": "dec-new"})
    assert status == 200 and opened["deduped"] is False


# -- Atlas rights: who may do what, set on the desk (K4) ---------------------
#
# The rights are the operator's stated intent, persisted like the desk posture.
# They are not a security boundary: `/api/atlas/rights` is exactly as
# unauthenticated as every other owner route, and anyone who can reach the port
# can send or omit the chat-origin header. What they buy is that an Atlas the
# operator narrowed is not carrying the ability, and that the owner refuses it
# BY NAME in the window before the chat session turns over.

_CHAT = {"X-Qlab-Origin": "chat"}


def test_a_desk_that_never_set_rights_serves_all_three_and_the_path(session):
    from qlab.tui.claude import atlas_rights_path

    status, payload = handle_api(session, "GET", "/api/atlas/rights", {}, {})
    assert status == 200
    assert payload["rights"] == {"web": True, "workflows": True, "build": True}
    assert payload["path"] == str(atlas_rights_path())


def test_setting_one_right_persists_all_three_and_is_read_back(session):
    from qlab.tui.claude import atlas_rights_path

    status, out = handle_api(session, "POST", "/api/atlas/rights", {},
                             {"workflows": False})
    assert status == 200
    assert out["rights"] == {"web": True, "workflows": False, "build": True}
    # The file is self-describing: the full three-key object, not the delta.
    on_disk = json.loads(atlas_rights_path().read_text(encoding="utf-8"))
    assert on_disk == {"web": True, "workflows": False, "build": True}
    _, back = handle_api(session, "GET", "/api/atlas/rights", {}, {})
    assert back["rights"]["workflows"] is False


def test_a_rights_change_is_on_the_record_field_by_field(session):
    handle_api(session, "POST", "/api/atlas/rights", {},
               {"workflows": False, "web": False})
    events = [e for e in session.registry.read_events(50)
              if e["kind"] == "desk.rights_changed"]
    by_field = {e["payload"]["field"]: e["payload"] for e in events}
    assert set(by_field) == {"workflows", "web"}
    assert by_field["workflows"] == {"field": "workflows", "value": False,
                                     "previous": True}
    # An unchanged right records nothing: a second identical POST is not news.
    handle_api(session, "POST", "/api/atlas/rights", {}, {"workflows": False})
    again = [e for e in session.registry.read_events(50)
             if e["kind"] == "desk.rights_changed"]
    assert len(again) == len(events)


def test_an_unknown_right_is_refused_by_name(session):
    status, out = handle_api(session, "POST", "/api/atlas/rights", {},
                             {"execute": False})
    assert status == 400
    assert "execute" in out["error"]


def test_a_right_that_is_not_a_boolean_is_refused(session):
    for value in ("yes", 1, [], None):
        status, out = handle_api(session, "POST", "/api/atlas/rights", {},
                                 {"workflows": value})
        assert status == 400, value
        assert "true or false" in out["error"]


def test_a_corrupt_rights_file_refuses_with_the_readers_remedy(session):
    from qlab.tui.claude import atlas_rights_path

    path = atlas_rights_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    status, out = handle_api(session, "GET", "/api/atlas/rights", {}, {})
    assert status == 500
    assert str(path) in out["error"]
    assert "delete it to restore the defaults" in out["error"]


def test_without_the_workflows_right_the_chats_action_routes_refuse_by_name(
        session):
    handle_api(session, "POST", "/api/atlas/rights", {}, {"workflows": False})
    refusal = "the workflows right is off — turn it on in Settings ▸ MODELS"
    calls = [
        ("/api/workflows/start", {"goal": "g", "kind": "portfolio_review"}),
        ("/api/workflows/wf-1/resume", {}),
        ("/api/atlas/tasks", {"kind": "held_record_change", "reason": "r"}),
    ]
    for path, body in calls:
        status, out = handle_api(session, "POST", path, {}, body,
                                 headers=_CHAT)
        assert status == 403, path
        assert out["error"] == refusal, path


def test_the_workflows_right_binds_the_chat_and_not_the_operators_own_client(
        session):
    """Rights bound the chat Atlas. The heartbeat's autonomous dispatch and the
    human at the workstation are not gated — a request with no chat origin is
    served on its own gates, which is how the operator can still work a desk
    they narrowed."""
    handle_api(session, "POST", "/api/atlas/rights", {}, {"workflows": False})
    status, out = handle_api(session, "POST", "/api/atlas/tasks", {},
                             {"kind": "held_record_change", "reason": "r"})
    assert status == 200
    assert out.get("task_id")


def test_with_the_workflows_right_the_chat_reaches_the_routes_own_gates(
        session):
    """The right granted, the refusal is the route's own — not the rights one."""
    status, out = handle_api(session, "POST", "/api/atlas/tasks", {},
                             {"kind": "held_record_change", "reason": "r"},
                             headers=_CHAT)
    assert status == 200 and out.get("task_id")


def test_the_chat_cannot_start_a_workflow_without_naming_a_template(session):
    """`workflow.start` defaults `template_id=""`, which fell through to the
    ungated branch and registered a five-phase `portfolio_review` no template
    gate had seen and no coordinator was dispatched to walk. The mode gate is
    attached to the template, so a start without one is a start without one."""
    session.atlas.set_mode("research")

    status, out = handle_api(
        session, "POST", "/api/workflows/start", {},
        {"goal": "look at the book", "kind": "portfolio_review"},
        headers=_CHAT)

    assert status == 400, out
    assert "template_id" in out["error"]
    # The refusal names what IS startable, so the reasoner's next turn is a
    # correction rather than a guess.
    assert "portfolio_watch" in out["error"]
    assert session.registry.list_workflows(limit=10) == []


def test_the_chat_still_starts_a_named_template(session):
    """The other side: naming a registered template goes through the gate."""
    session.atlas.set_mode("research")

    status, out = handle_api(
        session, "POST", "/api/workflows/start", {},
        {"goal": "read the record on the held names",
         "template_id": "portfolio_watch"}, headers=_CHAT)

    assert status == 200, out
    assert out.get("workflow_id")


def test_the_trigger_line_carries_the_reason_the_task_was_minted_for(session):
    session.announce_desk_work(True, [{
        "task_id": "abcdef1234567890", "trigger": "held_record_change",
        "action": "workflow", "ticker": "ACWI",
        "reason": "ACWI: primary +1, corroborated +0"}])
    lines = [e["payload"]["text"] for e in session.registry.read_events(20)
             if e["kind"] == "atlas_message"]
    fired = [line for line in lines if "held_record_change fired" in line]
    assert fired, lines
    assert "ACWI: primary +1" in fired[0]


_WORKFORCE = {"X-Qlab-Origin": "workforce"}


def test_a_governed_run_is_not_bound_by_the_chats_workflows_right(session):
    """The right is about Atlas starting work from the desk, not about a
    human-started governed run.

    `qlab workforce run` and the owner's own coordinator reach the owner
    through the same proxy the chat does, and their coordinator holds
    `workflow.start` because creating the workflow IS the run. Gating on "came
    through the proxy" refused a headless shell with a sentence pointing it at
    a settings panel it cannot open — so the origin is a value, and only `chat`
    is gated.
    """
    handle_api(session, "POST", "/api/atlas/rights", {}, {"workflows": False})
    status, out = handle_api(session, "POST", "/api/atlas/tasks", {},
                             {"kind": "held_record_change", "reason": "r"},
                             headers=_WORKFORCE)
    assert status == 200 and out.get("task_id")
    # The chat, at the same moment, is refused: the origin is what differs.
    status, refused = handle_api(session, "POST", "/api/atlas/tasks", {},
                                 {"kind": "held_record_change", "reason": "r"},
                                 headers=_CHAT)
    assert status == 403
    assert refused["error"] == ui_server.WORKFLOWS_RIGHT_REFUSAL
    # And the route that actually starts a run, which is the one that mattered.
    status, started = handle_api(
        session, "POST", "/api/workflows/start", {},
        {"goal": "review the core portfolio's regime exposure and risk",
         "kind": "portfolio_review"}, headers=_WORKFORCE)
    assert status == 200, started


def test_atlas_may_not_set_its_own_rights(session):
    """The panel is the operator's. Nothing in the chat's grant reaches this
    route today; the refusal predates the tool on purpose, because a narrowed
    Atlas handing itself back the right just taken away is the one move a
    rights panel exists to prevent."""
    status, out = handle_api(session, "POST", "/api/atlas/rights", {},
                             {"workflows": True}, headers=_CHAT)
    assert status == 403
    assert out["error"] == ui_server.RIGHTS_ARE_THE_OPERATORS
    # Reading is not setting: the chat may still see what it is allowed.
    status, payload = handle_api(session, "GET", "/api/atlas/rights", {}, {},
                                 headers=_CHAT)
    assert status == 200 and payload["rights"]["workflows"] is True


def test_two_unknown_rights_are_refused_in_the_plural(session):
    status, one = handle_api(session, "POST", "/api/atlas/rights", {},
                             {"execute": False})
    assert status == 400 and "execute is not a right" in one["error"]
    status, two = handle_api(session, "POST", "/api/atlas/rights", {},
                             {"execute": False, "trade": False})
    assert status == 400
    assert "execute, trade are not rights" in two["error"]


# --- standing authority: the four conditions that suspend a grant ------------
#
# `qlab.governance.authority.detect_anomalies` names four and takes them as
# booleans nobody supplied. `UISession._grant_anomalies` is where they come
# from — each read off the same live state the clicked booking path consults.
# An input the owner cannot compute is itself an anomaly: unknown suspends, it
# never proceeds, and it says which input it could not read rather than
# asserting something about the book it never saw.


def _execution_lane(session, monkeypatch):
    """Point the desk at a policy whose fills require an execution permit.

    An offline desk runs `DataPolicy.demo`, which is never execution-eligible,
    and the execute gate demands no permit on that lane — so nothing about
    permits is observable until the policy is one that asks for one.
    """
    from qlab.core.data import DataPolicy

    monkeypatch.setattr(
        session, "data_policy", lambda offline: DataPolicy.alpaca_operational())


def _open_book(session):
    """Open the simulated book once so its account row exists to be halted."""
    from qlab.trader.broker import SimulatedPaperBroker

    session.portfolio(True)
    return SimulatedPaperBroker.name


def test_a_clean_desk_suspends_no_grant(session):
    assert session._grant_anomalies(True) == []


def test_a_halted_book_suspends_a_grant(session):
    session.registry.set_halt(True, book=_open_book(session))
    assert session._grant_anomalies(True) == ["account is halted"]


def test_a_dirty_reconcile_suspends_a_grant(session, monkeypatch):
    # The simulated book reads its positions out of the very ledger reconcile
    # compares them against, so it cannot diverge from itself; what a real
    # venue produces is stood in for here.
    monkeypatch.setattr(
        "qlab.trader.reconcile.reconcile",
        lambda *a, **k: {"clean": False, "diffs": {"SPY": {"broker_qty": 1.0}}})
    assert session._grant_anomalies(True) == [
        "ledger and broker do not reconcile"]


def test_a_permit_that_refuses_execution_suspends_a_grant(session, monkeypatch):
    _execution_lane(session, monkeypatch)
    session.registry.record_data_permit({
        "permit_id": "permit-ineligible", "snapshot_id": "snap",
        "purpose": "execution", "provider": "alpaca", "feed": "iex",
        "as_of": "2026-09-01", "eligible_for_execution": False})
    assert session._grant_anomalies(True) == ["data is not execution-eligible"]


def test_an_eligible_permit_suspends_nothing(session, monkeypatch):
    _execution_lane(session, monkeypatch)
    session.registry.record_data_permit({
        "permit_id": "permit-ok", "snapshot_id": "snap", "purpose": "execution",
        "provider": "alpaca", "feed": "iex", "as_of": "2026-09-01",
        "eligible_for_execution": True})
    assert session._grant_anomalies(True) == []


def test_a_recently_rejected_order_suspends_a_grant(session):
    session.registry.add_order("leg-rejected", "plan-1", "SPY", "buy", 100.0)
    session.registry.update_order_state("leg-rejected", "rejected")
    assert session._grant_anomalies(True) == [
        "a recent order was rejected or expired"]


def test_a_recently_expired_order_suspends_a_grant(session):
    session.registry.add_order("leg-expired", "plan-1", "SPY", "buy", 100.0)
    session.registry.update_order_state("leg-expired", "expired")
    assert session._grant_anomalies(True) == [
        "a recent order was rejected or expired"]


def test_a_filled_order_suspends_nothing(session):
    session.registry.add_order("leg-filled", "plan-1", "SPY", "buy", 100.0)
    session.registry.update_order_state("leg-filled", "filled")
    assert session._grant_anomalies(True) == []


def test_a_rejection_older_than_the_window_suspends_nothing(session):
    session.registry.add_order("leg-old", "plan-1", "SPY", "buy", 100.0)
    session.registry.update_order_state("leg-old", "rejected")
    stale = (datetime.now(timezone.utc)
             - timedelta(hours=ui_server._ORDER_ANOMALY_WINDOW_HOURS + 1))
    session.registry.con.execute(
        "UPDATE orders SET created_at=? WHERE client_order_id=?",
        [stale.isoformat(), "leg-old"])
    assert session._grant_anomalies(True) == []


def test_every_condition_the_module_names_has_a_live_source(session, monkeypatch):
    """All four booleans `detect_anomalies` takes are supplied from the desk.

    The module shipped with fourteen tests and no caller (invariant 10); this
    is the pin that it now has one, in the module's own order.
    """
    session.registry.set_halt(True, book=_open_book(session))
    monkeypatch.setattr("qlab.trader.reconcile.reconcile",
                        lambda *a, **k: {"clean": False, "diffs": {"SPY": {}}})
    _execution_lane(session, monkeypatch)
    session.registry.record_data_permit({
        "permit_id": "permit-ineligible", "snapshot_id": "snap",
        "purpose": "execution", "provider": "alpaca", "feed": "iex",
        "as_of": "2026-09-01", "eligible_for_execution": False})
    session.registry.add_order("leg-rejected", "plan-1", "SPY", "buy", 100.0)
    session.registry.update_order_state("leg-rejected", "rejected")

    assert session._grant_anomalies(True) == [
        "account is halted",
        "ledger and broker do not reconcile",
        "data is not execution-eligible",
        "a recent order was rejected or expired",
    ]


def test_a_reconcile_that_raises_suspends_rather_than_proceeds(session,
                                                               monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr("qlab.trader.reconcile.reconcile", _boom)
    anomalies = session._grant_anomalies(True)
    assert anomalies == ["reconcile could not be run: broker unreachable"]
    # And it must not assert what it never read: a reconcile that raised is no
    # evidence that the ledger and the broker disagree.
    assert "ledger and broker do not reconcile" not in anomalies


def test_a_book_that_cannot_be_opened_suspends(session, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("no credentials for the alpaca book")

    monkeypatch.setattr("qlab.trader.broker.get_broker", _boom)
    assert session._grant_anomalies(True) == [
        "the book's halt flag could not be read: no credentials for the "
        "alpaca book",
        "reconcile could not be run: the book could not be opened",
    ]


def test_a_lane_that_needs_a_permit_and_has_none_suspends(session, monkeypatch):
    # Absence is not ineligibility, it is silence — and silence is one of the
    # inputs the owner cannot compute.
    _execution_lane(session, monkeypatch)
    assert session._grant_anomalies(True) == [
        "no execution data permit is on record for this book"]


def test_a_permit_that_cannot_be_read_suspends(session, monkeypatch):
    _execution_lane(session, monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("permit row unreadable")

    monkeypatch.setattr(session.registry, "current_data_permit", _boom)
    assert session._grant_anomalies(True) == [
        "the data permit could not be read: permit row unreadable"]


def test_orders_that_cannot_be_read_suspend(session, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("orders unreadable")

    monkeypatch.setattr(session.registry, "list_orders", _boom)
    assert session._grant_anomalies(True) == [
        "recent orders could not be read: orders unreadable"]


def test_the_anomaly_read_takes_no_lock_of_its_own(session):
    """Callable from a route that already holds the dispatch lock.

    A later caller reaches this from inside `_LOCK` (the booking route) and
    from the heartbeat's lock phase. `_LOCK` is a plain `threading.Lock`, so a
    helper that took it would deadlock the first of those on the first beat.
    """
    with ui_server._LOCK:
        assert session._grant_anomalies(True) == []


def test_the_halt_flag_comes_from_the_book_not_the_default_account_row(
        session, monkeypatch):
    """A venue that blocks trading suspends, with the ledger row unhalted.

    `qlab/trader/plan.py` reads `registry.get_account().get("halted")` — the
    DEFAULT_BOOK row, which knows nothing of a venue's own `trading_blocked`.
    Substituting that source passed every other test here, so this is the pin:
    the halt flag is the broker's own `portfolio_state`, where the per-book
    latch and the venue's block are one field (`qlab/trader/broker.py`).
    """
    from qlab.state.registry import DEFAULT_BOOK

    _open_book(session)
    ledger = session.registry.get_account(DEFAULT_BOOK)
    assert not ledger["halted"]          # the ledger says the desk is fine
    cash = float(ledger["cash"])

    class _BlockedVenue:
        name = "alpaca_paper"

        def portfolio_state(self, tickers):
            # Cash and positions agreeing with the ledger, so the only thing
            # this test can trip is the halt flag.
            return {"halted": True, "positions": {}, "weights": {},
                    "cash": cash, "equity": cash, "high_water_mark": cash}

    monkeypatch.setattr("qlab.trader.broker.get_broker",
                        lambda *a, **k: _BlockedVenue())
    assert session._grant_anomalies(True) == ["account is halted"]


def _fill_a_page(session, *, hours_ago: float) -> None:
    """A full `_ORDER_ANOMALY_SCAN` page of filled legs, stamped in the past."""
    for i in range(ui_server._ORDER_ANOMALY_SCAN):
        session.registry.add_order(f"leg-fill-{i}", "plan-1", "SPY", "buy", 1.0)
        session.registry.update_order_state(f"leg-fill-{i}", "filled")
    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    session.registry.con.execute(
        "UPDATE orders SET created_at=? WHERE client_order_id LIKE 'leg-fill-%'",
        [stamp])


def test_a_rejection_behind_a_full_page_of_newer_legs_still_suspends(session):
    """The page is newest-first, so a busy day can push a rejection off it.

    Reported as unread rather than as a clean desk: this is the one direction
    the design must not fail in, and everywhere else here ignorance suspends.
    """
    session.registry.add_order("leg-rejected", "plan-1", "SPY", "buy", 100.0)
    session.registry.update_order_state("leg-rejected", "rejected")
    session.registry.con.execute(
        "UPDATE orders SET created_at=? WHERE client_order_id=?",
        [(datetime.now(timezone.utc) - timedelta(hours=20)).isoformat(),
         "leg-rejected"])
    assert session._grant_anomalies(True) == [
        "a recent order was rejected or expired"]

    # A full page of NEWER legs, all still inside the window, hides it.
    _fill_a_page(session, hours_ago=19)
    assert session._grant_anomalies(True) == [
        "recent orders could not be read past the newest "
        f"{ui_server._ORDER_ANOMALY_SCAN} legs"]


def test_a_full_page_that_reaches_past_the_window_is_read_to_the_end(session):
    # The opposite guard: the page did reach the window's far edge, so nothing
    # inside it went unread and a long order history must not suspend forever.
    _fill_a_page(session, hours_ago=ui_server._ORDER_ANOMALY_WINDOW_HOURS + 1)
    assert session._grant_anomalies(True) == []


def test_a_data_policy_that_cannot_be_read_names_the_policy(session,
                                                            monkeypatch):
    # Not "the data permit could not be read": no permit was ever looked for.
    def _boom(*a, **k):
        raise RuntimeError("provider unresolvable")

    monkeypatch.setattr(session, "data_policy", _boom)
    assert session._grant_anomalies(True) == [
        "the desk's data policy could not be read: provider unresolvable"]


# --- standing authority: the owner books what a live grant covers -----------
#
# `book_under_grant` is the automatic half of the desk's one booking gate.
# Exactly one step differs from the click — a persisted grant stands in for the
# per-plan human confirmation — and every other check is `_book_checked_plan`,
# shared with `book_current_proposal` rather than copied beside it. A grant
# checks ceilings and never recency, so the automatic path carries its own
# maximum plan age; the click needs none, because the click IS the freshness
# proof.


def _grant_fields(session, **over) -> dict:
    fields = {
        "allowed_universe": list(session.mandate.universe_whitelist),
        "max_notional": 100_000.0,
        "max_turnover": 2.0,
        "max_orders": 50,
        "max_books_per_day": 2,
        "allowed_policy": session.mandate.operational_policy,
        "granted_by": "operator",
        "ttl_days": 7,
    }
    fields.update(over)
    return fields


def _live_grant(session, patch: dict | None = None, **over) -> dict:
    """A persisted grant that covers the desk's own even-weight rebalance.

    `patch` writes fields `build_grant` refuses to compose (an invalid mode, a
    missing ceiling) — the shapes a hand-built or pre-migration row can hold.
    """
    from qlab.governance.authority import build_grant

    grant = build_grant(**_grant_fields(session, **over))
    grant.update(patch or {})
    session.registry.create_authority_grant(grant)
    return grant


def _age_plan(session, plan_id: str, seconds: float) -> None:
    """Backdate a persisted plan: the automatic path reads its age."""
    created = (datetime.now(timezone.utc)
               - timedelta(seconds=seconds)).isoformat()
    session.registry.con.execute(
        "UPDATE plans SET created_at=? WHERE plan_id=?", [created, plan_id])


def _grant_events(session, kind: str) -> list[dict]:
    return [event["payload"]
            for event in session.registry.read_events_of_kind(kind, 50)]


def _refusals(session) -> list[str]:
    events = _grant_events(session, ui_server.GRANT_REFUSED_EVENT)
    assert events, "a refusal that says nothing is a refusal nobody can audit"
    return events[-1]["reasons"]


def _proposal_awaiting(session, tilt: float = 0.0) -> tuple[str, str]:
    plan_id = _checked_plan(session, tilt=tilt)
    return plan_id, _open_request(session, plan_id)


def test_a_covered_proposal_books_itself_and_names_the_grant(session):
    grant = _live_grant(session)
    plan_id, approval_id = _proposal_awaiting(session)

    result = session.book_under_grant(True)

    assert result is not None
    assert result["booked"] is True
    assert result["approval_id"] == approval_id
    assert session.registry.get_approval_request(approval_id)["status"] == (
        "consumed")
    assert session.registry.list_orders(50) != []
    # Every automatic fill says so, naming the grant that covered it.
    booked = _grant_events(session, ui_server.GRANT_BOOKED_EVENT)
    assert len(booked) == 1
    assert booked[0]["grant_id"] == grant["grant_id"]
    assert booked[0]["plan_id"] == plan_id
    assert booked[0]["approval_id"] == approval_id
    assert booked[0]["trading_date"] == date.today().isoformat()


def test_a_desk_asking_nothing_is_not_a_refusal(session):
    """The quiet case. A beat writing a row every 30 s to say the desk has no
    open question would bury the refusals that mean something."""
    _live_grant(session)
    assert session.book_under_grant(True) is None
    assert _grant_events(session, ui_server.GRANT_REFUSED_EVENT) == []
    assert _grant_events(session, ui_server.GRANT_BOOKED_EVENT) == []


def test_no_grant_books_nothing_and_says_which(session):
    plan_id, approval_id = _proposal_awaiting(session)

    assert session.book_under_grant(True) is None

    assert session.registry.get_approval_request(approval_id)["status"] == (
        "pending")
    assert session.registry.list_orders(50) == []
    assert _refusals(session) == ["no standing authority grant"]


def test_a_revoked_grant_books_nothing(session):
    grant = _live_grant(session)
    session.registry.revoke_authority_grant(grant["grant_id"], "operator said stop")
    plan_id, _ = _proposal_awaiting(session)

    assert session.book_under_grant(True) is None

    assert session.registry.list_orders(50) == []
    assert _refusals(session) == [
        f"grant revoked at "
        f"{session.registry.get_authority_grant(grant['grant_id'])['revoked_at']}"
        f": operator said stop"]


def test_an_expired_grant_books_nothing(session):
    _live_grant(session, now=datetime.now(timezone.utc) - timedelta(days=8))
    _proposal_awaiting(session)

    assert session.book_under_grant(True) is None
    assert any("grant expired at" in reason for reason in _refusals(session))


def test_a_grant_not_yet_in_effect_books_nothing(session):
    _live_grant(session, now=datetime.now(timezone.utc) + timedelta(days=1))
    _proposal_awaiting(session)

    assert session.book_under_grant(True) is None
    assert "grant is not yet in effect" in _refusals(session)


@pytest.mark.parametrize("over,patch,fragment", [
    # A plan touching anything outside the grant is refused WHOLE, never
    # trimmed to fit.
    ({"allowed_universe": ["SPY"]}, {},
     "plan touches symbols outside the grant"),
    ({"max_notional": 1.0}, {}, "exceeds the grant ceiling"),
    ({"max_turnover": 0.5}, {}, "turnover"),
    ({"max_orders": 1}, {}, "above the grant ceiling"),
    ({"allowed_policy": "mean_variance"}, {}, "grant covers policy"),
    # Shapes `build_grant` will not compose but a row can still hold.
    ({}, {"mode": "live_auto"}, "is not a paper mode"),
    ({}, {"max_books_per_day": None},
     "grant names no max_books_per_day ceiling"),
])
def test_every_reason_the_grant_check_makes_refuses_the_automatic_path(
        session, over, patch, fragment):
    """`check_grant_covers` is the gate, not a hint: each reason it can return
    books nothing, moves no approval, and lands in the record by name."""
    _live_grant(session, patch, **over)
    plan_id, approval_id = _proposal_awaiting(session)

    assert session.book_under_grant(True) is None

    assert session.registry.list_orders(50) == []
    assert session.registry.get_approval_request(approval_id)["status"] == (
        "pending")
    assert _grant_events(session, ui_server.GRANT_BOOKED_EVENT) == []
    assert any(fragment in reason for reason in _refusals(session))


def test_a_plan_a_second_inside_the_automatic_bound_still_books(session):
    _live_grant(session)
    plan_id, _ = _proposal_awaiting(session)
    _age_plan(session, plan_id, ui_server.MAX_AUTO_BOOK_AGE_S - 1)

    result = session.book_under_grant(True)

    assert result is not None and result["booked"] is True


def test_a_plan_a_second_past_the_automatic_bound_refuses(session):
    """Without this the desk books a correctly-sized fill against a stale
    analysis every 30 s, and no human ever sees the interval."""
    _live_grant(session)
    plan_id, approval_id = _proposal_awaiting(session)
    _age_plan(session, plan_id, ui_server.MAX_AUTO_BOOK_AGE_S + 1)

    assert session.book_under_grant(True) is None

    assert session.registry.list_orders(50) == []
    assert session.registry.get_approval_request(approval_id)["status"] == (
        "pending")
    assert any(
        f"past the {ui_server.MAX_AUTO_BOOK_AGE_S}s the automatic path allows"
        in reason for reason in _refusals(session))


def test_the_automatic_bound_is_strictly_tighter_than_the_approval_ttl():
    """The ordering is the whole point: a grant may only book a plan far
    younger than the approval that authorises it, so a human keeps the
    interval between the two to book by hand or to let lapse."""
    import inspect

    from qlab.governance import approval

    ttl = inspect.signature(approval.build_approval_request).parameters[
        "ttl_seconds"].default
    assert ui_server.MAX_AUTO_BOOK_AGE_S < ttl


def test_the_click_still_books_a_plan_the_automatic_path_calls_stale(session):
    """Freshness is the automatic path's bound alone. The click IS the
    freshness proof — a human looking at a card refuses by not pressing it —
    so a plan the grant will not touch is still theirs to book by hand."""
    _live_grant(session)
    plan_id, _ = _proposal_awaiting(session)
    _age_plan(session, plan_id, ui_server.MAX_AUTO_BOOK_AGE_S + 60)

    assert session.book_under_grant(True) is None

    status, out = handle_api(session, "POST", "/api/desk/proposal/book", {},
                             _book_body(session, plan_id))
    assert (status, out["booked"]) == (200, True)


def test_a_plan_whose_age_cannot_be_read_refuses(session):
    # Absence refuses: an unreadable timestamp is not a fresh one.
    _live_grant(session)
    plan_id, _ = _proposal_awaiting(session)
    session.registry.con.execute(
        "UPDATE plans SET created_at='' WHERE plan_id=?", [plan_id])

    assert session.book_under_grant(True) is None
    assert any("the plan's age could not be read" in reason
               for reason in _refusals(session))


def test_the_days_budget_refuses_once_the_ceiling_is_reached(session):
    _live_grant(session, max_books_per_day=1)
    _proposal_awaiting(session)
    assert session.book_under_grant(True)["booked"] is True

    _proposal_awaiting(session, tilt=0.02)

    assert session.book_under_grant(True) is None
    assert "grant has booked 1 today, at its ceiling of 1" in _refusals(session)


def test_the_day_resets_on_the_next_trading_date(session):
    """Counted by TRADING date off the row, never by the event's wall clock:
    UTC rolls over at midnight while a trading date does not."""
    grant = _live_grant(session, max_books_per_day=1)
    # Recorded a moment ago by the clock, on yesterday's trading date.
    session.registry.record_event(ui_server.GRANT_BOOKED_EVENT, {
        "grant_id": grant["grant_id"], "plan_id": "yesterday",
        "trading_date": (date.today() - timedelta(days=1)).isoformat()})
    _proposal_awaiting(session)

    assert session.book_under_grant(True)["booked"] is True


def test_a_book_spends_the_day_it_records_not_the_day_it_was_written(session):
    """The mirror of the reset: a row whose clock says yesterday but whose
    trading date says today still spends today's budget."""
    grant = _live_grant(session, max_books_per_day=1)
    session.registry.record_event(ui_server.GRANT_BOOKED_EVENT, {
        "grant_id": grant["grant_id"], "plan_id": "earlier-today",
        "trading_date": date.today().isoformat()})
    session.registry.con.execute(
        "UPDATE events SET ts=? WHERE kind=?",
        [(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
         ui_server.GRANT_BOOKED_EVENT])
    _proposal_awaiting(session)

    assert session.book_under_grant(True) is None
    assert "grant has booked 1 today, at its ceiling of 1" in _refusals(session)


def test_another_grants_book_does_not_spend_this_grants_day(session):
    grant = _live_grant(session, max_books_per_day=1)
    session.registry.record_event(ui_server.GRANT_BOOKED_EVENT, {
        "grant_id": "some-other-grant", "plan_id": "elsewhere",
        "trading_date": date.today().isoformat()})
    _proposal_awaiting(session)

    assert session.book_under_grant(True)["booked"] is True
    assert grant["grant_id"] != "some-other-grant"


def test_a_hand_booked_order_spends_none_of_the_grants_day(session):
    """The count is of what this feature books, not of every order: an
    operator's own click must not exhaust the desk's standing budget."""
    _live_grant(session, max_books_per_day=1)
    by_hand, _ = _proposal_awaiting(session)
    status, out = handle_api(session, "POST", "/api/desk/proposal/book", {},
                             _book_body(session, by_hand))
    assert (status, out["booked"]) == (200, True)

    _proposal_awaiting(session, tilt=0.02)

    assert session.book_under_grant(True)["booked"] is True


def test_a_day_that_cannot_be_counted_refuses(session, monkeypatch):
    _live_grant(session)
    _proposal_awaiting(session)
    real = session.registry.read_events_of_kind

    def _boom(kind, limit=60):
        if kind == ui_server.GRANT_BOOKED_EVENT:
            raise RuntimeError("events unreadable")
        return real(kind, limit)

    monkeypatch.setattr(session.registry, "read_events_of_kind", _boom)

    assert session.book_under_grant(True) is None
    assert session.registry.list_orders(50) == []
    assert "the day's book count is unknown; a daily ceiling that cannot be " \
           "evaluated refuses" in _refusals(session)


def test_an_anomaly_suspends_the_grant_without_revoking_it(session):
    grant = _live_grant(session)
    _proposal_awaiting(session)
    session.registry.set_halt(True, book=_open_book(session))

    assert session.book_under_grant(True) is None

    assert session.registry.list_orders(50) == []
    assert "grant suspended by anomaly: account is halted" in _refusals(session)
    # The anomalies recorded beside the refusal are the ones that gated it —
    # read once and carried, not re-read for the record.
    assert _grant_events(session, ui_server.GRANT_REFUSED_EVENT)[-1][
        "anomalies"] == ["account is halted"]
    # Suspension is not revocation: the authority survives it and applies again
    # the moment the desk is out of the state a human should see.
    assert session.registry.get_authority_grant(
        grant["grant_id"])["revoked_at"] is None
    session.registry.set_halt(False, book=_open_book(session))
    assert session.book_under_grant(True)["booked"] is True


def test_a_referee_pass_that_misses_the_hash_refuses_even_under_a_grant(session):
    """A grant replaces the per-plan human confirmation and nothing else. The
    PASS is still pinned to the plan's own targets_hash (invariant 3)."""
    from qlab.state.registry import targets_hash

    _live_grant(session)
    plan_id, approval_id = _proposal_awaiting(session)
    plan = session.registry.get_plan(plan_id)
    # The referee's latest word on this decision covers a different allocation.
    session.registry.log_verdict(
        plan["decision_id"], "PASS", ["a different basket"],
        source="referee-agent", targets={"SPY": 1.0})

    assert session.book_under_grant(True) is None

    assert session.registry.list_orders(50) == []
    assert session.registry.get_approval_request(approval_id)["status"] == (
        "pending")
    assert _refusals(session) == [
        f"no referee PASS covers targets_hash {targets_hash(plan['targets'])}"]


def test_the_automatic_path_reaches_approved_the_way_the_click_does(session):
    """Two ways to reach `approved`, one way to execute. The standing path
    takes the same `decide_approval(..., "approve")` the click takes and the
    same gate consumes it; it never books around the approval record."""
    _live_grant(session)
    plan_id, approval_id = _proposal_awaiting(session)

    assert session.book_under_grant(True)["booked"] is True

    approved = session.registry.read_events_of_kind("approval_approved", 20)
    assert [e["payload"]["approval_id"] for e in approved] == [approval_id]
    consumed = session.registry.read_events_of_kind("approval_consumed", 20)
    assert [e["payload"]["approval_id"] for e in consumed] == [approval_id]
    assert session.registry.get_approval_request(approval_id)["consumed_at"]


def test_an_approval_a_human_already_gave_is_booked_without_a_second_approve(
        session):
    _live_grant(session)
    plan_id = _checked_plan(session)
    approval_id = _approve(session, plan_id)

    assert session.book_under_grant(True)["approval_id"] == approval_id

    approved = session.registry.read_events_of_kind("approval_approved", 20)
    assert len(approved) == 1


def test_an_unchanged_refusal_is_recorded_once_not_on_every_beat(session):
    """Thirty rows an hour saying one thing is not a record of anything."""
    _proposal_awaiting(session)

    for _ in range(3):
        assert session.book_under_grant(True) is None
    assert len(_grant_events(session, ui_server.GRANT_REFUSED_EVENT)) == 1

    # A refusal that changed is a new fact and is recorded.
    _live_grant(session, max_notional=1.0)
    assert session.book_under_grant(True) is None
    refusals = _grant_events(session, ui_server.GRANT_REFUSED_EVENT)
    assert len(refusals) == 2
    assert any("exceeds the grant ceiling" in reason
               for reason in refusals[-1]["reasons"])


def test_booking_under_a_grant_takes_no_lock_of_its_own(session):
    """B3's beat calls this inside the owner dispatch lock, exactly where the
    clicked route already runs. `_LOCK` is a plain `threading.Lock`, so a
    helper that took it would deadlock the desk on the first beat."""
    _live_grant(session)
    _proposal_awaiting(session)

    with ui_server._LOCK:
        assert session.book_under_grant(True)["booked"] is True


def test_the_clicked_paths_refusals_are_word_for_word_unchanged(session):
    """Steps 2-6 are shared with the standing path, so they are read by a
    caller that has no client to answer. Not one word of what a human sees at
    a refused click may move: these are the exact sentences."""
    plan_id, _ = _proposal_awaiting(session)
    body = _book_body(session, plan_id)

    def _error(**over) -> str:
        status, out = handle_api(session, "POST", "/api/desk/proposal/book",
                                 {}, dict(body, **over))
        assert status == 400
        return out["error"]

    assert _error(human_confirmed=False) == "human_confirmed=true is required"
    assert _error(plan_id="0" * 16) == "not the current proposal"
    assert _error(targets_hash="0" * 16) == "targets_hash does not match the plan"

    plan = session.registry.get_plan(plan_id)
    session.registry.log_verdict(
        plan["decision_id"], "FAIL", ["mandate drift"], source="referee-agent",
        targets=plan["targets"])
    assert _error() == (
        f"no referee PASS covers targets_hash {body['targets_hash']}")


def test_the_clicked_path_books_without_any_grant_at_all(session):
    """A grant is the automatic path's authority, never a precondition of the
    human's. The desk that has never granted one books exactly as before."""
    plan_id, approval_id = _proposal_awaiting(session)

    status, out = handle_api(session, "POST", "/api/desk/proposal/book", {},
                             _book_body(session, plan_id))

    assert (status, out["booked"]) == (200, True)
    assert out["approval_id"] == approval_id
    assert _grant_events(session, ui_server.GRANT_BOOKED_EVENT) == []
    assert len(_booked_events(session)) == 1


def test_a_started_plan_is_left_for_a_human_even_under_a_covering_grant(
        session, monkeypatch):
    """`execute_plan` accepts a `submitted` plan so a half-filled book can be
    resumed, and its halt gate sits inside `if not already_started:` — so a
    resumed plan completes past a kill switch latched in the interim. That is a
    trade a person may make and a 30-second beat may not: book, a leg fails,
    the switch latches, the next beat resumes, and a non-liquidating rebalance
    completes through a halt."""
    from qlab.governance import proposal as proposal_mod

    _live_grant(session)
    plan_id, approval_id = _proposal_awaiting(session)
    proposal = proposal_mod.current_proposal(session.registry)
    session.registry.set_plan_state(plan_id, "submitted")
    # The desk's own read already declines to offer a started plan, so the
    # refusal is reached here by handing it back deliberately: the gate must
    # refuse on the plan's own persisted state, not lean on that filter.
    assert proposal_mod.current_proposal(session.registry) is None
    monkeypatch.setattr("qlab.governance.proposal.current_proposal",
                        lambda registry, now_iso=None: proposal)

    assert session.book_under_grant(True) is None

    assert session.registry.list_orders(50) == []
    assert session.registry.get_approval_request(approval_id)["status"] == (
        "pending")
    assert _refusals(session) == [
        "plan is 'submitted', not 'checked'; a plan whose legs may already be "
        "at the broker is resumed by a human, never by a grant"]


def test_a_started_plan_is_never_even_offered_to_the_automatic_path(session):
    """The other half of the same guarantee, and the reason the refusal above
    needs a monkeypatch to reach: a plan at the broker is not the desk's open
    question, so a beat finds nothing to book at all."""
    _live_grant(session)
    plan_id, _ = _proposal_awaiting(session)
    session.registry.set_plan_state(plan_id, "submitted")

    assert session.book_under_grant(True) is None
    assert session.registry.list_orders(50) == []
    assert _grant_events(session, ui_server.GRANT_BOOKED_EVENT) == []


def test_a_grant_book_that_fails_mid_execution_keeps_its_approval_too(
        session, monkeypatch):
    """The `submitted` skip lives in the gate both paths take, so it holds for
    a grant exactly as it does for a click.

    The started-plan rule stops the automatic path *resuming* one, which is a
    different moment: this is the beat's own book failing on leg 2, and the
    plan it half-placed is left for a human with its authority intact. A fault
    on the way to the broker is not a refusal, so it propagates to the beat
    rather than being filed as a reason the grant did not cover the plan.
    """
    _live_grant(session)
    plan_id, approval_id = _proposal_awaiting(session)
    _broker_fails_on_leg(monkeypatch, 2, "venue rejected the order")

    with pytest.raises(RuntimeError, match="venue rejected the order"):
        session.book_under_grant(True)

    approval = session.registry.get_approval_request(approval_id)
    assert approval["status"] == "approved"
    assert not approval["invalidated_reason"]
    assert session.registry.get_plan(plan_id)["state"] == "submitted"
    assert _grant_events(session, ui_server.GRANT_BOOKED_EVENT) == []
    assert _grant_events(session, ui_server.GRANT_REFUSED_EVENT) == []


# --- standing authority: granting it, and taking it back, on the desk ------
#
# The three routes the AUTHORITY card is built on. The payload contract is
# binding: the workstation deserializes the whole thing or none of it, so a
# field of the wrong type takes the card down rather than one row of it, and a
# `{}` where a `null` belongs draws standing authority with no ceilings at all.
# Both writes refuse the chat outright, as the rights route does — an Atlas
# that could grant itself authority would make the whole object decorative.

_AUTHORITY_FIELDS = {
    "grant_id", "mode", "allowed_universe", "max_notional", "max_turnover",
    "max_orders", "max_books_per_day", "valid_from", "expires_at",
    "granted_by", "books_today", "days_left"}


def _grant_body(session, **over) -> dict:
    """Every ceiling, explicitly — which is what the route demands."""
    body = {
        "allowed_universe": list(session.mandate.universe_whitelist),
        "max_notional": 100_000.0,
        # A FRACTION, never a percentage: the card renders this times 100, so a
        # 35 here would draw 3500.0% and nothing would catch it.
        "max_turnover": 0.35,
        "max_orders": 50,
        "max_books_per_day": 2,
        "ttl_days": 7,
        "granted_by": "operator",
    }
    body.update(over)
    return body


def _grant_through_the_route(session, **over) -> dict:
    status, said = handle_api(session, "POST", "/api/desk/authority", {},
                              _grant_body(session, **over))
    assert status == 200, said
    return said["grant"]


def _authority(session) -> dict:
    status, payload = handle_api(session, "GET", "/api/desk/authority", {}, {})
    assert status == 200, payload
    return payload


def test_a_desk_holding_no_grant_answers_null_and_not_an_empty_grant(session):
    """`null`, never `{}`.

    An empty object deserializes into a grant whose every ceiling is absent,
    and the card draws that as standing authority with no bounds instead of the
    remedy for holding none. It is a silent misread, not an error.
    """
    payload = _authority(session)
    assert set(payload) == {"grant", "anomalies"}
    assert payload["grant"] is None
    assert payload["anomalies"] == []


def test_the_authority_read_asks_for_no_lane_or_parameter(session):
    """A route that demanded one would answer non-2xx to the client that has
    none, and the card would read "nothing has said what may book itself"
    forever with no owner-down signal to explain it."""
    status, bare = handle_api(session, "GET", "/api/desk/authority", {}, {})
    assert status == 200 and set(bare) == {"grant", "anomalies"}
    status, with_offline = handle_api(
        session, "GET", "/api/desk/authority", {"offline": ["1"]}, {})
    assert status == 200 and set(with_offline) == {"grant", "anomalies"}


def test_a_standing_grant_is_served_with_every_ceiling_and_its_own_types(
        session):
    """Every scalar's type is load-bearing: one wrong one fails serde for the
    WHOLE payload, and the card never populates. There is no per-field
    tolerance."""
    made = _grant_through_the_route(session)
    payload = _authority(session)
    grant = payload["grant"]

    assert grant == made, "the create answer and the read must be one object"
    assert set(grant) == _AUTHORITY_FIELDS
    assert isinstance(grant["grant_id"], str) and grant["grant_id"]
    assert grant["mode"] == "paper_auto"
    assert grant["allowed_universe"] == sorted(
        session.mandate.universe_whitelist)
    assert all(isinstance(t, str) for t in grant["allowed_universe"])
    assert isinstance(grant["max_notional"], float)
    assert grant["max_notional"] == 100_000.0
    # A fraction on the wire, because a fraction is what the card multiplies.
    assert isinstance(grant["max_turnover"], float)
    assert grant["max_turnover"] == 0.35
    for name in ("max_orders", "max_books_per_day", "books_today",
                 "days_left"):
        assert isinstance(grant[name], int), name
        assert not isinstance(grant[name], bool), name
    assert grant["max_orders"] == 50 and grant["max_books_per_day"] == 2
    assert isinstance(grant["valid_from"], str)
    assert isinstance(grant["expires_at"], str)
    assert grant["granted_by"] == "operator"
    assert all(isinstance(a, str) for a in payload["anomalies"])


def test_days_left_is_whole_days_and_never_claims_one_the_grant_lacks(session):
    """Floored, and computed by the OWNER. A second arithmetic in a client
    whose wall clock is minutes out is how a card comes to disagree with the
    desk about whether anything can still book — and rounding up would promise
    a day the grant does not have."""
    week = _grant_through_the_route(session, ttl_days=7)
    # Seven days granted a moment ago is six whole days plus 23:59 — six.
    assert week["days_left"] == 6
    session.registry.con.execute("DELETE FROM authority_grants")
    day = _grant_through_the_route(session, ttl_days=1)
    assert day["days_left"] == 0


def test_books_today_is_what_the_grant_has_spent_not_what_is_left(session):
    """The client subtracts. A count that meant "remaining" would invert
    `books_left()` silently and draw a spent day as a full one.

    Three per day and one book, so spent (1) and remaining (2) are different
    numbers: a ceiling of two would make the two readings agree and the test
    prove nothing.
    """
    _grant_through_the_route(session, max_turnover=2.0, max_books_per_day=3)
    _proposal_awaiting(session)

    assert session.book_under_grant(True)["booked"] is True

    grant = _authority(session)["grant"]
    assert grant["books_today"] == 1
    assert grant["max_books_per_day"] == 3


def test_a_revoked_grant_no_longer_stands_on_the_desk(session):
    """`live_grant` deliberately returns a revoked row so the gate refuses it
    BY NAME rather than falling back to an older, broader one. What the desk
    SHOWS is narrower: a card reading "standing · 6 d left" over authority the
    operator withdrew an hour ago is the one thing it must never say."""
    _grant_through_the_route(session)
    status, said = handle_api(session, "POST", "/api/desk/authority/revoke",
                              {}, {"reason": "revoked by the operator"})
    assert status == 200, said
    assert _authority(session)["grant"] is None
    assert session.live_grant()["revoked_at"]


def test_an_expired_grant_no_longer_stands_either(session):
    _live_grant(session, now=datetime.now(timezone.utc) - timedelta(days=8))
    assert session.live_grant() is not None
    assert _authority(session)["grant"] is None


def test_the_anomalies_are_served_beside_a_grant_and_without_one(session):
    """Both halves arrive together and neither implies the other: a desk with
    no grant can still have anomalies, and a grant with none is simply live."""
    session.registry.set_halt(True, book=_open_book(session))

    bare = _authority(session)
    assert bare["grant"] is None
    assert bare["anomalies"] == ["account is halted"]

    _grant_through_the_route(session)
    held = _authority(session)
    assert held["grant"] is not None
    assert held["anomalies"] == ["account is halted"]


def test_a_grant_made_on_the_route_is_the_one_the_owner_books_under(session):
    """The round trip that matters: composed by the route, persisted, and then
    honoured by the gate the beat calls."""
    made = _grant_through_the_route(session, max_turnover=2.0)
    _proposal_awaiting(session)

    result = session.book_under_grant(True)

    assert result["booked"] is True
    booked = _grant_events(session, ui_server.GRANT_BOOKED_EVENT)
    assert [row["grant_id"] for row in booked] == [made["grant_id"]]


def test_the_route_composes_the_grant_through_build_grant(session, monkeypatch):
    """A1's review made this a requirement rather than a suggestion: every rule
    about what a grant must carry lives in `build_grant`, so a route that
    assembled its own dict would be a second place for "every ceiling required,
    no defaults" to be true — or to quietly stop being."""
    from qlab.governance import authority as authority_module

    def _boom(**kwargs):
        raise AssertionError(f"composed with {sorted(kwargs)}")

    monkeypatch.setattr(authority_module, "build_grant", _boom)
    with pytest.raises(AssertionError) as excinfo:
        handle_api(session, "POST", "/api/desk/authority", {},
                   _grant_body(session))
    handed = str(excinfo.value)
    for ceiling in ("max_notional", "max_turnover", "max_orders",
                    "max_books_per_day", "ttl_days", "allowed_universe",
                    "allowed_policy", "granted_by"):
        assert ceiling in handed, ceiling


@pytest.mark.parametrize("missing", [
    "allowed_universe", "max_notional", "max_turnover", "max_orders",
    "max_books_per_day", "ttl_days"])
def test_a_missing_ceiling_is_refused_by_name(session, missing):
    body = _grant_body(session)
    body.pop(missing)
    status, refused = handle_api(session, "POST", "/api/desk/authority", {},
                                 body)
    assert status == 400
    # Keyed on `error` and nothing else: a refusal body under any other key is
    # shown to the operator as raw JSON instead of the owner's sentence.
    assert set(refused) == {"error"}
    assert missing in refused["error"], refused
    assert session.live_grant() is None


@pytest.mark.parametrize("ceiling", [
    "max_notional", "max_turnover", "max_orders", "max_books_per_day"])
def test_a_ceiling_of_zero_is_refused_like_a_missing_one(session, ceiling):
    status, refused = handle_api(
        session, "POST", "/api/desk/authority", {},
        _grant_body(session, **{ceiling: 0}))
    assert status == 400
    assert ceiling in refused["error"]
    assert session.live_grant() is None


def test_a_ttl_past_the_month_is_refused_and_so_is_none_at_all(session):
    status, refused = handle_api(session, "POST", "/api/desk/authority", {},
                                 _grant_body(session, ttl_days=31))
    assert status == 400 and "ttl_days" in refused["error"]
    status, zero = handle_api(session, "POST", "/api/desk/authority", {},
                              _grant_body(session, ttl_days=0))
    assert status == 400 and "ttl_days" in zero["error"]
    assert session.live_grant() is None


def test_there_is_no_live_authority_to_grant(session):
    """The module's own refusal, reaching the operator unaltered."""
    status, refused = handle_api(session, "POST", "/api/desk/authority", {},
                                 _grant_body(session, mode="live"))
    assert status == 400
    assert "there is no live authority" in refused["error"]
    assert session.live_grant() is None


def test_a_universe_that_is_one_string_is_not_a_universe_of_letters(session):
    """`sorted("AAPL")` is `['A', 'A', 'L', 'P']` — a grant scoped to four
    letters that name no instrument, which then refuses every plan it sees for
    a reason nobody can read."""
    status, refused = handle_api(
        session, "POST", "/api/desk/authority", {},
        _grant_body(session, allowed_universe="AAPL"))
    assert status == 400 and "allowed_universe" in refused["error"]
    assert session.live_grant() is None


def test_a_universe_of_non_strings_is_refused(session):
    status, refused = handle_api(
        session, "POST", "/api/desk/authority", {},
        _grant_body(session, allowed_universe=["SPY", 7]))
    assert status == 400 and "allowed_universe" in refused["error"]
    assert session.live_grant() is None


@pytest.mark.parametrize("ceiling,value", [
    ("max_notional", "lots"),
    ("max_turnover", None),
    ("max_orders", True),
    ("max_books_per_day", 2.5),
])
def test_a_ceiling_that_is_not_the_number_it_claims_is_refused(
        session, ceiling, value):
    """`float("5")` and `int(True)` both succeed, and `int(2.5)` truncates: a
    ceiling arrived at by coercion is a ceiling nobody typed. `None` is the
    absent case and is refused by `build_grant` under the same name."""
    status, refused = handle_api(
        session, "POST", "/api/desk/authority", {},
        _grant_body(session, **{ceiling: value}))
    assert status == 400
    assert ceiling in refused["error"], refused
    assert session.live_grant() is None


def test_the_grant_pins_the_desks_own_policy_and_not_the_callers(session):
    """A grant is authority over THIS desk's method. `grant_refusals` checks a
    plan against `mandate.operational_policy`, so a policy off the wire either
    covers nothing or covers something the operator never set here."""
    _grant_through_the_route(session, allowed_policy="min_variance:classical")
    stored = session.live_grant()
    assert stored["allowed_policy"] == session.mandate.operational_policy


def test_the_chat_may_not_grant_itself_standing_authority(session):
    """An Atlas that could grant itself authority would make the whole object
    decorative. Nothing in the chat's grant reaches this route today; the
    refusal predates the tool on purpose."""
    status, refused = handle_api(session, "POST", "/api/desk/authority", {},
                                 _grant_body(session), headers=_CHAT)
    assert status == 403
    assert refused["error"] == ui_server.AUTHORITY_IS_THE_OPERATORS
    assert session.live_grant() is None


def test_the_chat_may_not_revoke_the_operators_grant(session):
    """Withdrawing is the safe direction, but it is still the operator's act:
    an Atlas that could revoke could also stop a desk the operator meant to
    leave running, and no agent decides what standing authority exists."""
    made = _grant_through_the_route(session)
    status, refused = handle_api(
        session, "POST", "/api/desk/authority/revoke", {},
        {"reason": "atlas said so"}, headers=_CHAT)
    assert status == 403
    assert refused["error"] == ui_server.AUTHORITY_IS_THE_OPERATORS
    assert _authority(session)["grant"]["grant_id"] == made["grant_id"]
    assert not session.live_grant()["revoked_at"]


def test_reading_what_may_book_itself_is_not_setting_it(session):
    """The GET stays open, exactly as the rights read does."""
    made = _grant_through_the_route(session)
    status, payload = handle_api(session, "GET", "/api/desk/authority", {}, {},
                                 headers=_CHAT)
    assert status == 200
    assert payload["grant"]["grant_id"] == made["grant_id"]


def test_revoking_returns_the_grant_it_withdrew_and_records_the_reason(session):
    made = _grant_through_the_route(session)
    status, said = handle_api(
        session, "POST", "/api/desk/authority/revoke", {},
        {"reason": "revoked by the operator on the desk"})

    assert status == 200
    # Keyed on `grant`: a `revoked` key instead would lose the id off the toast.
    assert said["grant"]["grant_id"] == made["grant_id"]
    assert set(said["grant"]) == _AUTHORITY_FIELDS
    row = session.registry.get_authority_grant(made["grant_id"])
    assert row["revoked_at"] and said["revoked_at"] == row["revoked_at"]
    assert row["revoked_reason"] == "revoked by the operator on the desk"
    assert _authority(session)["grant"] is None
    # Recorded like every other governance transition.
    revoked = _grant_events(session, "authority.revoked")
    assert [row["grant_id"] for row in revoked] == [made["grant_id"]]


def test_revoking_names_no_grant_id_at_all(session):
    """The owner holds one live grant and is the only thing that knows which; a
    body naming one could withdraw the grant a card read seconds ago rather
    than the one standing now. A reason, and nothing else."""
    made = _grant_through_the_route(session)
    status, said = handle_api(session, "POST", "/api/desk/authority/revoke",
                              {}, {"reason": "the operator pressed R"})
    assert status == 200 and said["grant"]["grant_id"] == made["grant_id"]


def test_a_revocation_must_say_why(session):
    _grant_through_the_route(session)
    status, refused = handle_api(session, "POST", "/api/desk/authority/revoke",
                                 {}, {})
    assert status == 400 and "reason" in refused["error"]
    assert not session.live_grant()["revoked_at"]


def test_revoking_nothing_is_a_400_with_a_sentence_and_not_a_conflict(session):
    """A well-formed request about a desk already in the state it asks for. Any
    other status renders as "the grant may still stand" — the opposite of what
    happened — and pressing R twice is the likeliest way to reach this."""
    status, refused = handle_api(session, "POST", "/api/desk/authority/revoke",
                                 {}, {"reason": "the operator pressed R"})
    assert status == 400
    assert set(refused) == {"error"}
    assert refused["error"] == ui_server.NOTHING_TO_REVOKE

    _grant_through_the_route(session)
    handle_api(session, "POST", "/api/desk/authority/revoke", {},
               {"reason": "first press"})
    status, again = handle_api(session, "POST", "/api/desk/authority/revoke",
                               {}, {"reason": "second press"})
    assert status == 400 and again["error"] == ui_server.NOTHING_TO_REVOKE


def test_a_revoked_grant_stops_the_owner_booking_under_it(session):
    """The whole point of the key: what the route withdraws, the beat obeys."""
    _grant_through_the_route(session, max_turnover=2.0)
    handle_api(session, "POST", "/api/desk/authority/revoke", {},
               {"reason": "the operator pressed R"})
    _proposal_awaiting(session)

    assert session.book_under_grant(True) is None
    assert session.registry.list_orders(50) == []
    assert any("revoked" in reason for reason in _refusals(session))
