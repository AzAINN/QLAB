"""Role→tier model routing and the invocation audit record (P5 gap 4)."""

from __future__ import annotations

import pytest

from qlab.core.llm_config import SurfaceModel
from qlab.operator.model_routing import (
    DEEP,
    NONE,
    QUICK,
    REQUIRED_CLAUDE_ROLES,
    ROLE_TIER,
    TIER_MODEL,
    degrades_result,
    record_invocation,
    resolve_route,
    tier_for,
)
from qlab.state.registry import Registry


@pytest.fixture
def reg():
    r = Registry(":memory:")
    yield r
    r.close()


def test_judgment_roles_are_deep_and_mechanical_roles_are_quick():
    assert tier_for("moments-analyst") == DEEP
    assert tier_for("challenger") == DEEP
    assert tier_for("referee") == DEEP
    for role in ("optimization-runner", "reporter", "data-qa", "signal-qa"):
        assert tier_for(role) == QUICK


def test_role_resolves_its_tier_predictably():
    d = resolve_route("referee")
    assert d.requested_tier == DEEP and d.source == "tier"
    q = resolve_route("reporter")
    assert q.requested_tier == QUICK and q.resolved_model == "sonnet"


def test_fast_mode_speeds_up_judgment_but_never_the_gate():
    # Fast mode is a speed/quality trade the operator makes explicitly. It is
    # bounded: the approval gate is the one place a cheaper answer is worth
    # nothing, because a PASS must never mean "passed on the fast model".
    from qlab.operator import model_routing as routing

    fast_analyst = routing.resolve_route("moments-analyst", fast=True)
    assert fast_analyst.requested_tier == routing.DEEP
    assert fast_analyst.resolved_model == routing.FAST_TIER_MODEL[routing.DEEP]

    referee = routing.resolve_route("referee", fast=True)
    assert referee.requested_tier == routing.DEEP
    # The gate keeps its tier's model, and says why in the audit record.
    assert referee.resolved_model == routing.TIER_MODEL[routing.DEEP]
    assert "required-deep" in (referee.fallback_reason or "")

    # Every required-deep role is exempt, not just the one we happened to name.
    for role in routing.REQUIRED_DEEP_ROLES:
        assert routing.resolve_route(role, fast=True).resolved_model == (
            routing.TIER_MODEL[routing.DEEP])

    # And an explicit agent override still outranks fast mode.
    override = routing.resolve_route(
        "moments-analyst", source_model="opus", fast=True)
    assert override.resolved_model == "opus"
    assert override.source == "agent_override"


def test_agent_source_override_wins_over_the_tier():
    d = resolve_route("reporter", source_model="opus")
    assert d.resolved_model == "opus" and d.source == "agent_override"
    # 'inherit' is the no-override sentinel, not a concrete model.
    assert resolve_route("reporter", source_model="inherit").source == "tier"


def test_unknown_role_falls_back_with_a_recorded_reason():
    d = resolve_route("mystery-role")
    assert d.requested_tier == NONE and d.source == "unknown_role"
    assert "no configured tier" in d.fallback_reason


def test_swapping_the_model_behind_a_tier_changes_no_authority():
    # A different concrete model for the quick tier must not alter which roles
    # are quick — tiers are the architecture, model names are deployment.
    d = resolve_route("reporter", tier_model={DEEP: "inherit", QUICK: "haiku",
                                              NONE: "inherit"})
    assert d.resolved_model == "haiku"
    assert d.requested_tier == QUICK
    assert ROLE_TIER["reporter"] == QUICK


def test_invocation_is_audited_with_tier_and_resolved_model(reg):
    decision = resolve_route("reporter")
    iid = record_invocation(reg, decision, status="ok", latency_ms=812.5,
                            tokens=1200)
    rows = reg.list_model_invocations()
    assert len(rows) == 1
    row = rows[0]
    assert row["invocation_id"] == iid
    assert row["role"] == "reporter"
    assert row["requested_tier"] == QUICK
    assert row["resolved_model"] == "sonnet"
    assert row["latency_ms"] == pytest.approx(812.5)
    events = [e["kind"] for e in reg.read_events(10)]
    assert "model.route_resolved" in events


def test_fallback_emits_a_distinct_event(reg):
    decision = resolve_route("mystery-role")
    record_invocation(reg, decision, status="ok")
    events = [e["kind"] for e in reg.read_events(10)]
    assert "model.fallback_used" in events
    assert reg.list_model_invocations()[0]["fallback_reason"]


def test_required_deep_role_on_a_fallback_degrades_the_result():
    # The referee must run deep; a fallback cannot be read as a clean PASS.
    good = resolve_route("referee")
    assert degrades_result("referee", good) is False
    degraded = resolve_route("referee")
    degraded = type(degraded)(**{**degraded.to_dict(),
                                 "fallback_reason": "deep tier unavailable"})
    assert degrades_result("referee", degraded) is True
    # A mechanical role is not subject to the deep requirement.
    assert degrades_result("reporter", resolve_route("reporter")) is False


# ---------------------------------------------------------------------------
# the backend dimension: which provider serves the route
# ---------------------------------------------------------------------------

def test_the_approval_gate_never_moves_to_a_configured_backend():
    # The operator may point the workforce at any backend the desk offers. The
    # gate is the one role that does not follow, because a PASS must not mean
    # "passed on whatever was being tried out that week".
    workforce = SurfaceModel("ollama", "granite3.3:8b")
    for role in REQUIRED_CLAUDE_ROLES:
        pinned = resolve_route(role, workforce=workforce)
        assert pinned.backend == "claude"
        assert pinned.resolved_model == TIER_MODEL[DEEP]
        # Audit-visible, the same mechanism fast mode uses for its exemption.
        assert "pinned to claude" in (pinned.fallback_reason or "")
        assert "ollama" in pinned.fallback_reason
    # The pin outranks every other input, including an agent-file override and
    # fast mode (whose own exemption still has to be readable alongside it).
    fast = resolve_route("referee", workforce=workforce, fast=True)
    assert fast.backend == "claude" and fast.resolved_model == TIER_MODEL[DEEP]
    assert "required-deep" in fast.fallback_reason
    override = resolve_route("referee", workforce=workforce, source_model="opus")
    assert override.backend == "claude" and override.resolved_model == "opus"


def test_a_role_that_is_not_pinned_runs_on_the_configured_backend(reg):
    decision = resolve_route("moments-analyst",
                             workforce=SurfaceModel("ollama", "granite3.3:8b"))
    assert decision.backend == "ollama"
    assert decision.resolved_model == "granite3.3:8b"
    assert decision.source == "workforce_config"
    # Swapping the provider changes no authority: the role still asks for the
    # tier it was configured with.
    assert decision.requested_tier == DEEP

    # Whether a role is registered is a fact about the role, not about the
    # provider serving it: a typo stays visible on an experimenting desk.
    unknown = resolve_route("mystery-role",
                            workforce=SurfaceModel("ollama", "granite3.3:8b"))
    assert unknown.backend == "ollama"
    assert "no configured tier" in unknown.fallback_reason

    record_invocation(reg, decision)
    record_invocation(reg, resolve_route("reporter"))
    # The row names the process that served the role. The claude backend keeps
    # the process name the older rows already carry.
    assert {row["backend"] for row in reg.list_model_invocations()} == {
        "ollama", "claude_cli"}


def test_no_configured_backend_is_todays_routing_unchanged():
    for role in (*ROLE_TIER, "mystery-role"):
        for fast in (False, True):
            today = resolve_route(role, fast=fast)
            assert today.backend == "claude"
            assert resolve_route(role, fast=fast, workforce=None) == today
            # A claude workforce is the desk as it already is. Its model is the
            # CLI alias `inherit` already follows, so tier routing still owns
            # the model — the surface only decides the provider.
            assert resolve_route(
                role, fast=fast,
                workforce=SurfaceModel("claude", "inherit")) == today
