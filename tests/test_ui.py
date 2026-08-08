"""The single-page UI's JSON API (dispatch tested in-process, no sockets)."""

from __future__ import annotations

from datetime import date, timedelta

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
    """A bound port is not enough: the owner may still be opening its state.

    Driven through ``--classic``, which is the flag that keeps the Textual
    client during the soak. The owner spawn and readiness wait below are shared
    by both paths and unchanged by the cutover; this is the leg that ends in a
    ``QlabTui``.
    """
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
        classic=True,
        operator=False,
    ))

    assert result == 0
    assert calls == {"probe": 2, "system": 1, "run": 1}


def _tui_args(**overrides):
    """The namespace ``qlab tui`` builds, with the defaults its parser sets."""
    from types import SimpleNamespace

    return SimpleNamespace(**{
        "port": 8765, "online": False, "refresh": 2.0, "claude": "offer",
        "classic": False, "glass": False, "operator": False, **overrides,
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

    with pytest.raises(SystemExit) as exit_info:
        cli_module._cmd_tui(_tui_args(port=8899))

    assert exit_info.value.code == 0
    assert seen["path"] == str(binary)
    assert seen["argv"] == [str(binary)]
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

    with pytest.raises(SystemExit):
        cli_module._cmd_tui(_tui_args(glass=True))
    assert seen["argv"] == [str(binary), "--glass"]


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
    assert "--classic" in message


def test_tui_classic_without_textual_names_the_extra_to_install(monkeypatch):
    """The rollback valve may not fail obscurely.

    An operator reaching for --classic is already having a bad day; a bare
    `ModuleNotFoundError: textual` names no remedy. The Textual import moved
    into the branch so the default path does not pay for it, and this is what
    keeps that move from having cost the message.
    """
    import sys

    import qlab.autopilot.cli as cli_module

    _attached_owner(monkeypatch, cli_module)
    monkeypatch.setattr(
        cli_module.os, "execvpe",
        lambda *_a: pytest.fail("--classic must not exec the workstation"))
    # A `None` entry is how the import system is told a module is unavailable;
    # `from qlab.tui.app import QlabTui` then raises ImportError.
    monkeypatch.setitem(sys.modules, "qlab.tui.app", None)

    with pytest.raises(SystemExit) as exit_info:
        cli_module._cmd_tui(_tui_args(classic=True))
    message = str(exit_info.value)
    assert "TUI extra is not installed" in message
    assert "pip install -e '.[operator]'" in message


@pytest.mark.parametrize("classic", [False, True])
def test_tui_refuses_the_retired_operator_flag(monkeypatch, classic):
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


def test_tui_refuses_glass_with_the_classic_client(monkeypatch):
    """A posture word the chosen client cannot honour is refused, not dropped.

    `--glass` is Ratatui vocabulary. The Textual client has no posture to
    decline, so forwarding it nowhere would leave an operator believing this
    window had been made read-only when it still reaches the confirm gate.
    """
    import qlab.autopilot.cli as cli_module

    monkeypatch.setattr(
        cli_module.subprocess, "Popen",
        lambda *_a, **_k: pytest.fail("a refused invocation must not spawn"))
    monkeypatch.setattr(
        cli_module.os, "execvpe",
        lambda *_a: pytest.fail("a refused invocation must not exec"))

    with pytest.raises(SystemExit) as exit_info:
        cli_module._cmd_tui(_tui_args(classic=True, glass=True))
    message = str(exit_info.value)
    assert "--glass" in message and "--classic" in message


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


def test_tui_classic_runs_the_textual_client_against_the_same_owner(monkeypatch):
    """The soak valve: one flag, no revert."""
    import qlab.autopilot.cli as cli_module
    import qlab.tui.app as app_module

    _attached_owner(monkeypatch, cli_module)
    monkeypatch.setattr(
        cli_module.os, "execvpe",
        lambda *_a: pytest.fail("--classic must not exec the workstation"))

    started = {}

    class Tui:
        def __init__(self, _client, **kwargs):
            started.update(kwargs)

        def run(self):
            started["ran"] = True

    monkeypatch.setattr(app_module, "QlabTui", Tui)
    assert cli_module._cmd_tui(_tui_args(classic=True, refresh=0.0)) == 0
    assert started["ran"] is True
    # Attached rather than owned: the Textual client only terminates a server it
    # started itself.
    assert started["owned_server"] is None


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

    # Offline is always synthetic: it is the demo and must not reach the network.
    monkeypatch.setattr(
        "qlab.trader.alpaca_auth.resolve_alpaca_credentials", lambda: object())
    assert session.news_provider_for(True) == "synthetic"
    # Live with a resolvable credential upgrades without being asked.
    assert session.news_provider_for(False) == "alpaca"

    # Live with no credential stays synthetic rather than failing the desk.
    monkeypatch.setattr(
        "qlab.trader.alpaca_auth.resolve_alpaca_credentials", lambda: None)
    assert session.news_provider_for(False) == "synthetic"

    # An explicit provider is an instruction and is never second-guessed —
    # including naming synthetic on a live desk on purpose.
    monkeypatch.setenv("QLAB_NEWS_PROVIDER", "synthetic")
    monkeypatch.setattr(
        "qlab.trader.alpaca_auth.resolve_alpaca_credentials", lambda: object())
    assert session.news_provider_for(False) == "synthetic"


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
    """The other side of the same guard: this project did not turn autonomy off."""
    today = date.today().isoformat()
    session.registry.create_atlas_task(
        "task-trigger", f"regime_shift|{today}|SPY|abc", "regime_shift",
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
        ["task-old", f"regime_shift|{today}|SPY|old", "regime_shift", "{}",
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
        ["task-empty", f"regime_shift|{today}|SPY|e", "regime_shift", "{}",
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


def test_a_non_finite_weight_is_reported_not_rendered_as_a_number():
    """NaN and inf survive float() and rendered as "SPY nan%".

    That is a number-shaped non-number on a trading surface — it reads as a
    real target. Python's json emits and parses NaN by default, so an agent
    artifact carries one all the way to the render; it must be reported the
    same way a string weight is.
    """
    from qlab.tui.app import _format_targets

    out = _format_targets({"SPY": float("nan"), "GLD": 0.4})
    assert "nan%" not in out
    assert "GLD 40.0%" in out and "[unreadable: SPY]" in out
    assert "[unreadable: SPY]" in _format_targets({"SPY": float("inf")})
    # And a clean set is untouched.
    assert _format_targets({"SPY": 0.6, "GLD": 0.4}) == "SPY 60.0% · GLD 40.0%"


# --- the news window ----------------------------------------------------------


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


def test_the_ui_has_a_research_panel_for_the_augmented_lane():
    """The route is useless if nothing renders it."""
    html = _INDEX.read_text(encoding="utf-8")
    assert 'data-nav="research"' in html
    assert 'data-panel="research"' in html
    assert "/api/research/predictors" in html


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


def test_the_fold_strip_draws_both_signs_from_one_zero_line():
    """The first version of this chart bottom-anchored every bar in a flex row
    and pushed it with a margin, so all the positive bars shared a TOP edge and
    the height of a bar no longer meant its magnitude. Driving the real page
    showed +0.324 rendered taller-looking than +0.531.

    The fix is two fixed rows of equal height with the positive bar
    bottom-anchored in the upper one and the negative bar top-anchored in the
    lower one, which is the only layout where "above the line" and "below the
    line" mean the same thing for every bar. This test pins the mechanism,
    because the failure was invisible in the payload and only showed up in
    layout."""
    html = _INDEX.read_text(encoding="utf-8")
    css = html.split("</style>")[0]
    assert ".folds .f{display:grid;grid-template-rows:19px 19px" in css
    assert ".folds .f > i.pos{grid-row:1;align-self:end" in css
    assert ".folds .f > i.neg{grid-row:2;align-self:start" in css
    # No margin-based nudging: that was the bug.
    assert "margin-${v<0?'top':'bottom'}" not in html


# --- the workforce Atlas manages -------------------------------------------
#
# Live gap: /api/atlas/context returned 12 keys and not one of them mentioned
# a workflow, a step, an agent or a phase. The desk had ten durable workflows
# on it -- three blocked at the reporter, two interrupted mid-debate, one
# abandoned by the operator -- each carrying a written step summary saying
# exactly what happened. Atlas, the manager of that workforce, could not see
# any of it: asked "why is the desk stuck", it had nothing to answer from.


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
    html = _INDEX.read_text(encoding="utf-8")
    assert 'data-nav="workforce"' in html
    assert 'data-panel="workforce"' in html
    assert "/api/workforce" in html
    # The stall reason must be rendered, not just fetched.
    assert "stalled_at" in html


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


def test_the_stall_box_distinguishes_absent_from_already_decided():
    """`w.awaiting_operator ? a : b` reads an ABSENT key as false.

    Driving the live page proved this: against an older server that did not
    send the key, all seven stall boxes rendered "already decided" — the UI
    inventing an operator decision that had never been taken. The template
    must test the three states explicitly, so absent renders as unknown.
    """
    html = _INDEX.read_text(encoding="utf-8")
    assert "awaiting_operator===true" in html
    assert "awaiting_operator===false" in html
    # ...and the truthiness form must not come back.
    assert "w.awaiting_operator?" not in html
    assert "unknown" in html


def test_the_agent_stream_has_a_route_and_reaches_the_page(session):
    """The coordinator republishes every agent event onto the audit bus and no
    page ever read it: `grep api/events qlab/ui/index.html` returned nothing.

    Agent reasoning that is recorded but never rendered is not visibility.
    """
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
    html = _INDEX.read_text(encoding="utf-8")
    assert "/api/workforce/stream" in html


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
