"""Standing authority grants: expiring, revocable, scoped, anomaly-paused.

The whole point of these tests is that a grant is hard to get and easy to lose.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from qlab.governance.authority import (
    MAX_GRANT_DAYS,
    AuthorityError,
    build_grant,
    check_grant_covers,
    detect_anomalies,
)
from qlab.state.registry import Registry

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()


@pytest.fixture
def reg():
    r = Registry(":memory:")
    yield r
    r.close()


def _grant(**over):
    kwargs = dict(
        allowed_universe=["ACWI", "BNDW"], max_notional=5000.0,
        max_turnover=0.5, max_orders=4, allowed_policy="hrp",
        granted_by="operator", ttl_days=7, now=NOW)
    kwargs.update(over)
    return build_grant(**kwargs)


def _plan(**over):
    plan = {
        "plan_id": "p1",
        "targets": {"ACWI": 0.6, "BNDW": 0.4},
        "pre_trade": {"turnover": 0.2},
        "legs": [{"ticker": "ACWI", "notional": 1000.0},
                 {"ticker": "BNDW", "notional": 800.0}],
    }
    plan.update(over)
    return plan


# --- construction is deliberately strict -------------------------------------


def test_there_is_no_live_authority_mode():
    with pytest.raises(AuthorityError, match="no live authority"):
        _grant(mode="live_auto")


def test_grant_cannot_be_open_ended():
    with pytest.raises(AuthorityError, match="never open-ended"):
        _grant(ttl_days=MAX_GRANT_DAYS + 1)
    with pytest.raises(AuthorityError, match="never open-ended"):
        _grant(ttl_days=0)


def test_grant_requires_universe_ceilings_and_an_author():
    with pytest.raises(AuthorityError, match="allowed universe"):
        _grant(allowed_universe=[])
    with pytest.raises(AuthorityError, match="who granted it"):
        _grant(granted_by="  ")
    for field in ("max_notional", "max_turnover", "max_orders"):
        with pytest.raises(AuthorityError, match=f"{field} must be a positive"):
            _grant(**{field: 0})


# --- coverage is fail-closed --------------------------------------------------


def test_a_conforming_plan_is_covered():
    assert check_grant_covers(_grant(), _plan(), now_iso=NOW_ISO,
                              policy_id="hrp") == []


def test_absent_grant_refuses():
    assert check_grant_covers(None, _plan(), now_iso=NOW_ISO,
                              policy_id="hrp") == ["no standing authority grant"]


def test_revocation_outranks_everything():
    grant = _grant()
    grant["revoked_at"] = "2026-07-24T11:00:00+00:00"
    grant["revoked_reason"] = "operator pulled it"
    reasons = check_grant_covers(grant, _plan(), now_iso=NOW_ISO,
                                 policy_id="hrp")
    # Exactly one reason: revocation short-circuits, never outranked.
    assert len(reasons) == 1 and "revoked" in reasons[0]
    assert "operator pulled it" in reasons[0]


def test_expiry_refuses():
    grant = _grant(ttl_days=1)
    reasons = check_grant_covers(grant, _plan(),
                                 now_iso="2026-08-01T00:00:00+00:00",
                                 policy_id="hrp")
    assert any("expired" in r for r in reasons)


def test_symbol_outside_the_universe_refuses_the_whole_plan():
    plan = _plan(targets={"ACWI": 0.5, "GLD": 0.5})
    reasons = check_grant_covers(_grant(), plan, now_iso=NOW_ISO,
                                 policy_id="hrp")
    assert any("outside the grant" in r and "GLD" in r for r in reasons)


def test_ceilings_are_enforced():
    over_notional = _plan(legs=[{"ticker": "ACWI", "notional": 9000.0}])
    assert any("notional" in r for r in check_grant_covers(
        _grant(), over_notional, now_iso=NOW_ISO, policy_id="hrp"))

    over_turnover = _plan(pre_trade={"turnover": 0.9})
    assert any("turnover" in r for r in check_grant_covers(
        _grant(), over_turnover, now_iso=NOW_ISO, policy_id="hrp"))

    too_many = _plan(legs=[{"ticker": "ACWI", "notional": 10.0}] * 9)
    assert any("legs" in r for r in check_grant_covers(
        _grant(), too_many, now_iso=NOW_ISO, policy_id="hrp"))


def test_a_different_policy_is_not_covered():
    reasons = check_grant_covers(_grant(), _plan(), now_iso=NOW_ISO,
                                 policy_id="mvsk")
    assert any("covers policy" in r for r in reasons)


def test_anomalies_suspend_without_revoking():
    anomalies = detect_anomalies(halted=True, reconcile_clean=False,
                                 data_execution_eligible=False,
                                 recent_order_anomaly=True)
    assert len(anomalies) == 4
    grant = _grant()
    reasons = check_grant_covers(grant, _plan(), now_iso=NOW_ISO,
                                 policy_id="hrp", anomalies=anomalies)
    assert all("suspended by anomaly" in r for r in reasons)
    # Suspension is not revocation: the grant itself is untouched.
    assert grant.get("revoked_at") is None
    # Clearing the anomalies restores coverage.
    assert check_grant_covers(grant, _plan(), now_iso=NOW_ISO,
                              policy_id="hrp", anomalies=[]) == []


def test_no_anomalies_when_the_desk_is_clean():
    assert detect_anomalies(halted=False, reconcile_clean=True,
                            data_execution_eligible=True,
                            recent_order_anomaly=False) == []


# --- persistence and revocation ----------------------------------------------


def test_grant_roundtrips_and_revokes(reg):
    grant = _grant()
    gid = reg.create_authority_grant(grant)
    stored = reg.get_authority_grant(gid)
    assert stored["mode"] == "paper_auto"
    assert stored["allowed_universe"] == ["ACWI", "BNDW"]
    assert stored["revoked_at"] is None

    reg.revoke_authority_grant(gid, "shadow evaluation ended")
    revoked = reg.get_authority_grant(gid)
    assert revoked["revoked_at"] and revoked["revoked_reason"]
    assert check_grant_covers(revoked, _plan(), now_iso=NOW_ISO,
                              policy_id="hrp")

    kinds = [e["kind"] for e in reg.read_events(20)]
    assert "authority.granted" in kinds and "authority.revoked" in kinds


def test_no_grant_exists_by_default(reg):
    """The feature is inert: nothing creates a grant on its own."""
    assert reg.list_authority_grants() == []
